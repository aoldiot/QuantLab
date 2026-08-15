from __future__ import annotations

import asyncio
import dataclasses
import difflib
import json
import re
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..backtest_service import create_backtest_run
from ..config import settings
from ..db import SessionLocal, get_db
from ..llm_config import get_config, sdk_env
from ..models import (
    AgentMessage,
    AgentSession,
    AgentSessionStatus,
    ResearchProject,
    ResearchStatus,
    StrategySpecification,
    StrategyVersion,
)
from ..schemas import AgentApplyRequest, AgentSessionCreate, BacktestCreate

router = APIRouter(prefix="/api/agent", tags=["strategy-agent"])
STRATEGY_RELATIVE = Path("backend/app/strategies")
ALLOWED_MODES = {"plan", "default", "acceptEdits", "bypassPermissions"}
GLOBAL_SEMAPHORE = asyncio.Semaphore(settings.agent_max_concurrency)
CLIENT_LOCKS: dict[str, asyncio.Lock] = {}
STRATEGY_LOCKS: dict[str, asyncio.Lock] = {}
ACTIVE_CLIENTS: dict[str, ClaudeSDKClient] = {}
ACTIVE_TASKS: dict[str, asyncio.Task[None]] = {}
ACTIVE_WEBSOCKETS: dict[str, WebSocket] = {}
APPROVALS: dict[str, asyncio.Future[bool]] = {}


def _agent_root() -> Path:
    root = (settings.data_root / "agent").resolve()
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(parents=True, exist_ok=True)
    (root / "baselines").mkdir(parents=True, exist_ok=True)
    return root


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(settings.strategy_repo_path.resolve()), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git 操作失败")
    return result


def create_worktree(session_id: str, strategy_name: str) -> Path:
    target = _agent_root() / "worktrees" / session_id
    _run_git("worktree", "add", "--detach", str(target), "HEAD")
    source = settings.strategy_repo_path.resolve() / STRATEGY_RELATIVE / f"{strategy_name}.py"
    destination = target / STRATEGY_RELATIVE / f"{strategy_name}.py"
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        baseline = _agent_root() / "baselines" / session_id / f"{strategy_name}.py"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, baseline)
    contract = settings.strategy_repo_path.resolve() / "backend/app/strategy_contract.py"
    shutil.copy2(contract, target / "backend/app/strategy_contract.py")
    tests = settings.strategy_repo_path.resolve() / "backend/tests"
    if tests.exists():
        shutil.copytree(tests, target / "backend/tests", dirs_exist_ok=True)
    skill = settings.strategy_repo_path.resolve() / ".claude/skills/nautilus-strategy-author"
    if skill.exists():
        shutil.copytree(skill, target / ".claude/skills/nautilus-strategy-author", dirs_exist_ok=True)
    return target


def cleanup_expired_worktrees() -> None:
    cutoff = datetime.now(UTC) - timedelta(days=settings.agent_workspace_retention_days)
    root = _agent_root() / "worktrees"
    for path in root.iterdir():
        if path.is_dir():
            _run_git("worktree", "repair", str(path), check=False)
        if path.is_dir() and datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
            _run_git("worktree", "remove", "--force", str(path), check=False)


async def repair_agent_session_paths() -> None:
    """Rebase persisted worktree paths after the project directory is moved."""
    root = _agent_root() / "worktrees"
    async with SessionLocal() as db:
        sessions = (await db.scalars(select(AgentSession))).all()
        changed = False
        for session in sessions:
            current = root / session.id
            if not Path(session.workspace_path).exists() and current.exists():
                session.workspace_path = str(current)
                changed = True
        if changed:
            await db.commit()


def _safe_bash(command: str) -> bool:
    patterns = (
        r"python(?:3)? -m compileall(?: .*)?",
        r"pytest(?: .*)?",
        r"ruff check(?: .*)?",
        r"git (?:status|diff)(?: .*)?",
        r"uv (?:add|sync)(?: .*)?",
        r"pip install(?: .*)?",
        r"curl (?:-[-\w]+ )*https?://[^;&|]+",
    )
    return "\n" not in command and not re.search(r"(?:^|\s)(?:sudo|rm|ssh|scp|git\s+push)(?:\s|$)", command) and any(re.fullmatch(p, command) for p in patterns)


async def bash_guard(input_data: dict[str, Any], _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
    command = str(input_data.get("tool_input", {}).get("command", "")).strip()
    if _safe_bash(command):
        return {}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "命令不在 QuantLab Bash 白名单内"}}


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _text_values(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [value["text"]] if isinstance(value.get("text"), str) else []
        return own + [text for child in value.values() for text in _text_values(child)]
    if isinstance(value, list):
        return [text for child in value for text in _text_values(child)]
    return []


def _normalize_backtest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = payload.copy()
    execution = str(normalized.get("execution_model", "CONSERVATIVE")).upper()
    aliases = {"MARKET": "STANDARD", "DEFAULT": "STANDARD", "NORMAL": "STANDARD", "QUICK": "FAST", "SAFE": "CONSERVATIVE"}
    normalized["execution_model"] = aliases.get(execution, execution)
    for field in ("start_date", "end_date"):
        if isinstance(normalized.get(field), str):
            normalized[field] = normalized[field][:10]
    return normalized


def _strip_control_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_control_markers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_strip_control_markers(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\s*QUANTLAB_BACKTEST_REQUEST:\{.*\}\s*", "", value, flags=re.DOTALL)
    return value


async def _resolve_backtest_version(payload: dict[str, Any], session: AgentSession) -> dict[str, Any]:
    normalized = _normalize_backtest_payload(payload)
    version_id = str(normalized.get("strategy_version_id", ""))
    if len(version_id) == 36:
        return normalized
    entrypoint = version_id if version_id.startswith("app.strategies.") else f"app.strategies.{session.strategy_name}"
    async with SessionLocal() as db:
        version = await db.scalar(select(StrategyVersion).where(StrategyVersion.entrypoint == entrypoint).order_by(StrategyVersion.created_at.desc()).limit(1))
    if not version:
        raise ValueError("策略尚未发布正式版本")
    normalized["strategy_version_id"] = version.id
    return normalized


def session_out(session: AgentSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "client_id": session.client_id,
        "strategy_name": session.strategy_name,
        "permission_mode": session.permission_mode,
        "status": session.status.value,
        "sdk_session_id": session.sdk_session_id,
        "error_message": session.error_message,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _diff_summary(session: AgentSession) -> dict[str, Any]:
    relative = STRATEGY_RELATIVE / f"{session.strategy_name}.py"
    baseline = _agent_root() / "baselines" / session.id / f"{session.strategy_name}.py"
    target = Path(session.workspace_path) / relative
    before = baseline.read_text(encoding="utf-8").splitlines(keepends=True) if baseline.exists() else []
    after = target.read_text(encoding="utf-8").splitlines(keepends=True) if target.exists() else []
    diff = "".join(difflib.unified_diff(before, after, fromfile=f"a/{relative.as_posix()}", tofile=f"b/{relative.as_posix()}"))
    additions = deletions = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            additions += 1
        elif line.startswith("-") and not line.startswith("---"):
            deletions += 1
    return {"diff": diff, "files": [{"path": relative.as_posix(), "additions": additions, "deletions": deletions}] if diff else [], "additions": additions, "deletions": deletions}


async def persist_event(session_id: str, role: str, event_type: str, content: dict[str, Any]) -> None:
    async with SessionLocal() as db:
        db.add(AgentMessage(session_id=session_id, role=role, event_type=event_type, content=content))
        await db.commit()


async def update_status(session_id: str, status: AgentSessionStatus, error: str | None = None, sdk_session_id: str | None = None) -> None:
    async with SessionLocal() as db:
        session = await db.get(AgentSession, session_id)
        if session:
            session.status = status
            session.error_message = error
            if sdk_session_id:
                session.sdk_session_id = sdk_session_id
            await db.commit()


async def send_session_event(session_id: str, payload: dict[str, Any]) -> bool:
    """Best-effort live delivery; persisted Agent events remain the source of truth."""
    websocket = ACTIVE_WEBSOCKETS.get(session_id)
    if websocket is None:
        return False
    try:
        await websocket.send_json(payload)
        return True
    except (WebSocketDisconnect, RuntimeError):
        if ACTIVE_WEBSOCKETS.get(session_id) is websocket:
            ACTIVE_WEBSOCKETS.pop(session_id, None)
        return False


async def build_options(session: AgentSession) -> ClaudeAgentOptions:
    async with SessionLocal() as db:
        config = await get_config(db)
        specification = await db.get(StrategySpecification, session.specification_id) if session.specification_id else None
        specification_context = ""
        if specification:
            specification_context = "\n当前任务绑定了用户已确认的策略规格。必须严格实现，发现歧义时停止猜测并明确指出：\n" + json.dumps(specification.content, ensure_ascii=False, indent=2)
        tools = ["Read", "Glob", "Grep"] if session.permission_mode == "plan" else ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Skill"]
        approved_tools = ["Read", "Glob", "Grep", "Skill(nautilus-strategy-author)"] if session.permission_mode == "plan" else (["Read", "Glob", "Grep", "Edit", "Write", "Skill(nautilus-strategy-author)"] if session.permission_mode == "default" else tools)
        async def request_approval(tool_name: str, tool_input: dict[str, Any], _context: Any):
            request_id = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            APPROVALS[request_id] = future
            delivered = await send_session_event(session.id, {"type": "approval_required", "request_id": request_id, "tool": tool_name, "input": tool_input})
            if not delivered:
                APPROVALS.pop(request_id, None)
                return PermissionResultDeny(behavior="deny", message="Agent 面板连接已断开，无法完成敏感操作审批", interrupt=False)
            try:
                approved = await asyncio.wait_for(future, timeout=300)
            except TimeoutError:
                approved = False
            finally:
                APPROVALS.pop(request_id, None)
            if approved:
                return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
            return PermissionResultDeny(behavior="deny", message="用户拒绝或审批超时", interrupt=False)

        return ClaudeAgentOptions(
            cwd=Path(session.workspace_path),
            model=config.model,
            env=sdk_env(config),
            tools=tools,
            allowed_tools=approved_tools,
            permission_mode=session.permission_mode,
            setting_sources=["project"],
            skills=["nautilus-strategy-author"],
            max_turns=config.max_turns,
            resume=session.sdk_session_id,
            enable_file_checkpointing=True,
            sandbox={"enabled": True, "autoAllowBashIfSandboxed": False},
            system_prompt={"type": "preset", "preset": "claude_code", "append": "你是 QuantLab 的 NautilusTrader 策略开发 Agent。所有面向用户的分析、计划、进度、提问、错误说明和最终回答必须使用简体中文；代码、命令、文件名和 API 字段保持原格式。不要用英文描述工具调用。只处理当前策略、策略测试与已有回测结果。不得通过 Bash 运行回测，不得自行决定发起回测。用户要求修改策略参数、默认值、参数范围或策略逻辑时，必须编辑当前策略 Python 文件并验证，不能只给出一次性参数。不得访问凭据或工作区之外的文件。完成修改后必须执行语法和相关测试验证。" + specification_context},
            hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[bash_guard])]},
            can_use_tool=request_approval if session.permission_mode == "default" else None,
        )


async def run_prompt(session: AgentSession, prompt: str) -> None:
    original_prompt = prompt
    backtest_requested = bool(re.search(r"回测|backtest", original_prompt, flags=re.IGNORECASE))
    if not prompt.startswith("/"):
        action_rule = "用户本轮明确要求回测。完成其他要求后，可以在回复末尾输出 QUANTLAB_BACKTEST_REQUEST:{紧凑JSON} 请求平台回测；execution_model 只能是 FAST、STANDARD、CONSERVATIVE。" if backtest_requested else "用户本轮没有明确要求回测。严禁发起回测或输出 QUANTLAB_BACKTEST_REQUEST。若用户要求修改策略参数，必须修改当前策略 Python 文件中的配置或 STRATEGY_MANIFEST。"
        prompt = f"请始终使用简体中文与用户交流，代码、命令、路径和字段名除外。\n{action_rule}\n当前目标策略文件：backend/app/strategies/{session.strategy_name}.py\n\n{original_prompt}"
    client_lock = CLIENT_LOCKS.setdefault(session.client_id, asyncio.Lock())
    strategy_lock = STRATEGY_LOCKS.setdefault(session.strategy_name, asyncio.Lock())
    execution_lock = strategy_lock if session.permission_mode != "plan" else asyncio.Lock()
    await send_session_event(session.id, {"type": "queued"})
    async with GLOBAL_SEMAPHORE, client_lock, execution_lock:
        await update_status(session.id, AgentSessionStatus.RUNNING)
        await persist_event(session.id, "user", "message", {"text": original_prompt})
        await send_session_event(session.id, {"type": "status", "status": "RUNNING"})
        client = ClaudeSDKClient(options=await build_options(session))
        ACTIVE_CLIENTS[session.id] = client
        try:
            response_text: list[str] = []
            fallback_context: dict[str, Any] | None = None
            await client.connect()
            await client.query(prompt)
            async for message in client.receive_response():
                event = _jsonable(message)
                event["message_type"] = message.__class__.__name__
                event_type = event.get("subtype") or event.get("type") or message.__class__.__name__
                if event_type == "success" and isinstance(event.get("usage"), dict):
                    event_usage = event["usage"]
                    total_tokens = int(event_usage.get("input_tokens", 0)) + int(event_usage.get("output_tokens", 0))
                    fallback_context = {"totalTokens": total_tokens, "maxTokens": 200_000, "percentage": total_tokens / 200_000 * 100}
                response_text.extend(_text_values(event))
                if event_type == "init" and event.get("data", {}).get("session_id"):
                    await update_status(session.id, AgentSessionStatus.RUNNING, sdk_session_id=event["data"]["session_id"])
                visible_event = _strip_control_markers(event)
                await persist_event(session.id, "assistant", str(event_type), visible_event)
                await send_session_event(session.id, {"type": "sdk_event", "event": visible_event})
            joined = "\n".join(response_text)
            match = re.search(r"QUANTLAB_BACKTEST_REQUEST:(\{.*\})", joined)
            if backtest_requested and match:
                try:
                    request = BacktestCreate.model_validate(await _resolve_backtest_version(json.loads(match.group(1)), session))
                    async with SessionLocal() as db:
                        run = await create_backtest_run(request, db)
                    await send_session_event(session.id, {"type": "backtest_created", "run_id": run.id, "status": run.status.value, "name": run.name})
                except Exception:
                    await send_session_event(session.id, {"type": "backtest_error", "message": "策略修改已完成，但回测参数不符合平台要求，请补充或修正参数后重试。"})
            changes = await asyncio.to_thread(_diff_summary, session)
            if changes["files"]:
                await send_session_event(session.id, {"type": "changes_ready", **changes})
            try:
                usage = _jsonable(await client.get_context_usage())
                if not usage.get("percentage") and fallback_context:
                    usage = fallback_context
                await persist_event(session.id, "system", "context_usage", usage)
                await send_session_event(session.id, {"type": "context_usage", "usage": usage})
            except Exception:
                if fallback_context:
                    await persist_event(session.id, "system", "context_usage", fallback_context)
                    await send_session_event(session.id, {"type": "context_usage", "usage": fallback_context})
            await update_status(session.id, AgentSessionStatus.IDLE)
            await send_session_event(session.id, {"type": "status", "status": "IDLE"})
        except asyncio.CancelledError:
            await update_status(session.id, AgentSessionStatus.FAILED, "Agent 后台任务被中断，请重新发送任务继续开发")
            raise
        except Exception as exc:
            await update_status(session.id, AgentSessionStatus.FAILED, str(exc)[:2000])
            await send_session_event(session.id, {"type": "error", "message": str(exc)})
        finally:
            ACTIVE_CLIENTS.pop(session.id, None)
            await client.disconnect()


@router.post("/sessions")
async def create_session(data: AgentSessionCreate, db: AsyncSession = Depends(get_db)):
    await get_config(db)
    canonical = settings.strategy_repo_path.resolve() / STRATEGY_RELATIVE / f"{data.strategy_name}.py"
    if not canonical.exists():
        raise HTTPException(404, "策略文件不存在")
    session = AgentSession(client_id=data.client_id, strategy_name=data.strategy_name, permission_mode=data.permission_mode, workspace_path="pending")
    db.add(session)
    await db.flush()
    try:
        session.workspace_path = str(await asyncio.to_thread(create_worktree, session.id, session.strategy_name))
    except Exception as exc:
        await db.rollback()
        raise HTTPException(500, str(exc)) from exc
    await db.commit()
    await db.refresh(session)
    return session_out(session)


@router.get("/sessions")
async def list_sessions(client_id: str, strategy_name: str | None = None, db: AsyncSession = Depends(get_db)):
    query_stmt = select(AgentSession).where(AgentSession.client_id == client_id)
    if strategy_name:
        query_stmt = query_stmt.where(AgentSession.strategy_name == strategy_name)
    rows = (await db.scalars(query_stmt.order_by(AgentSession.updated_at.desc()))).all()
    return [session_out(row) for row in rows]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AgentMessage).where(AgentMessage.session_id == session_id).order_by(AgentMessage.created_at))).all()
    return [{"id": row.id, "role": row.role, "event_type": row.event_type, "content": row.content, "created_at": row.created_at} for row in rows]


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(AgentSession, session_id)
    if not session:
        raise HTTPException(404, "Agent 会话不存在")
    client = ACTIVE_CLIENTS.get(session_id)
    if client:
        await client.interrupt()
    session.status = AgentSessionStatus.CANCELED
    await db.commit()
    await db.refresh(session)
    return session_out(session)


@router.get("/sessions/{session_id}/diff")
async def get_diff(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(AgentSession, session_id)
    if not session:
        raise HTTPException(404, "Agent 会话不存在")
    return await asyncio.to_thread(_diff_summary, session)


@router.post("/sessions/{session_id}/reject")
async def reject_session_changes(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(AgentSession, session_id)
    if not session:
        raise HTTPException(404, "Agent 会话不存在")
    baseline = _agent_root() / "baselines" / session.id / f"{session.strategy_name}.py"
    target = Path(session.workspace_path) / STRATEGY_RELATIVE / f"{session.strategy_name}.py"
    if not baseline.exists():
        raise HTTPException(409, "找不到本次会话的原始策略快照")
    shutil.copy2(baseline, target)
    return {"rejected": True}


@router.post("/sessions/{session_id}/apply")
async def apply_session(session_id: str, data: AgentApplyRequest, db: AsyncSession = Depends(get_db)):
    session = await db.get(AgentSession, session_id)
    if not session:
        raise HTTPException(404, "Agent 会话不存在")
    source = Path(session.workspace_path) / STRATEGY_RELATIVE / f"{session.strategy_name}.py"
    destination = settings.strategy_repo_path.resolve() / STRATEGY_RELATIVE / f"{session.strategy_name}.py"
    try:
        code = source.read_text(encoding="utf-8")
        compile(code, destination.name, "exec")
    except (OSError, SyntaxError) as exc:
        raise HTTPException(422, f"策略验证失败：{exc}") from exc
    destination.write_text(code, encoding="utf-8")
    baseline = _agent_root() / "baselines" / session.id / f"{session.strategy_name}.py"
    baseline.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, baseline)
    session.status = AgentSessionStatus.COMPLETED
    if session.research_project_id:
        project = await db.get(ResearchProject, session.research_project_id)
        if project and project.status != ResearchStatus.ARCHIVED:
            project.status = ResearchStatus.CODE_REVIEW
    await db.commit()
    return {"applied": True, "create_version_requested": data.create_version, "description": data.description, "requires_publish_confirmation": True}


@router.websocket("/ws/{session_id}")
async def agent_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    previous = ACTIVE_WEBSOCKETS.get(session_id)
    ACTIVE_WEBSOCKETS[session_id] = websocket
    if previous is not None and previous is not websocket:
        try:
            await previous.close(code=1000)
        except RuntimeError:
            pass
    async with SessionLocal() as db:
        session = await db.get(AgentSession, session_id)
        if not session:
            await websocket.send_json({"type": "error", "message": "Agent 会话不存在"})
            await websocket.close(code=4404)
            return
        if session.status in {AgentSessionStatus.RUNNING, AgentSessionStatus.QUEUED} and session.id not in ACTIVE_TASKS:
            session.status = AgentSessionStatus.IDLE
            await db.commit()
        await websocket.send_json({"type": "status", "status": session.status.value})
    try:
        while True:
            payload = await websocket.receive_json()
            kind = payload.get("type", "message")
            if kind == "message":
                prompt = str(payload.get("content", "")).strip()
                current_task = ACTIVE_TASKS.get(session.id)
                if prompt and (current_task is None or current_task.done()):
                    current_task = asyncio.create_task(run_prompt(session, prompt))
                    ACTIVE_TASKS[session.id] = current_task
                    current_task.add_done_callback(
                        lambda task, sid=session.id: ACTIVE_TASKS.pop(sid, None)
                        if ACTIVE_TASKS.get(sid) is task else None
                    )
            elif kind == "set_mode" and payload.get("mode") in ALLOWED_MODES:
                session.permission_mode = payload["mode"]
                async with SessionLocal() as db:
                    stored = await db.get(AgentSession, session.id)
                    stored.permission_mode = session.permission_mode
                    await db.commit()
                await websocket.send_json({"type": "mode", "mode": session.permission_mode})
            elif kind == "cancel":
                client = ACTIVE_CLIENTS.get(session.id)
                if client:
                    await client.interrupt()
            elif kind == "compact":
                current_task = ACTIVE_TASKS.get(session.id)
                if current_task is None or current_task.done():
                    current_task = asyncio.create_task(run_prompt(session, "/compact 保留策略需求、已完成修改、验证结果和待办事项"))
                    ACTIVE_TASKS[session.id] = current_task
                    current_task.add_done_callback(
                        lambda task, sid=session.id: ACTIVE_TASKS.pop(sid, None)
                        if ACTIVE_TASKS.get(sid) is task else None
                    )
            elif kind == "approval":
                future = APPROVALS.get(str(payload.get("request_id", "")))
                if future and not future.done():
                    future.set_result(bool(payload.get("approved")))
    except WebSocketDisconnect:
        pass
    finally:
        if ACTIVE_WEBSOCKETS.get(session_id) is websocket:
            ACTIVE_WEBSOCKETS.pop(session_id, None)
