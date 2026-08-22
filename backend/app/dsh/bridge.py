"""HTTP bridge between the DSH runtime plugin and the QuantLab backend.

The DSH agent subprocess talks to FastAPI over loopback HTTP. The plugin
declares domain tools (candidate staging and patching / write_strategy_code / verify_strategy_file /
execute_backtest_tool / dispatch_tool_call); each call lands here, where the
backend acts as the gatekeeper: it runs the 4-level Pre-Flight sandbox, the
isolated backtest gate, Git bookkeeping and the interactive approval registry.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from ..config import settings
from ..db import get_db
from ..models import ResearchProject
from ..research_workflow import apply_research_phase
from .tools import dispatch_dsh_tool_call

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dsh-tools", tags=["dsh-bridge"])

APPROVAL_TOOLS = {"write_strategy_code", "execute_backtest_tool"}
RESEARCH_DISPATCH_TOOLS = {
    "quant_get_capabilities", "quant_get_research_context", "quant_get_strategy_context",
    "quant_web_research", "quant_market_data_query", "quant_factor_analysis", "quant_run_experiment",
}
DISPATCH_TOOLS_BY_PHASE = {
    "RESEARCH": RESEARCH_DISPATCH_TOOLS,
    "REPAIR": {"quant_get_strategy_context", "quant_get_strategy", "quant_preflight_verify"},
    "FIX_ERROR": {"quant_get_strategy_context", "quant_get_strategy", "quant_preflight_verify"},
    "BACKTEST": {"quant_get_research_context", "quant_get_strategy_context", "quant_get_strategy", "quant_market_data_query"},
    "BACKTEST_RETRY": {"quant_get_research_context", "quant_get_strategy_context", "quant_get_strategy", "quant_market_data_query"},
    "RESULT_REVIEW": {"quant_get_research_context", "quant_get_strategy_context", "quant_get_strategy", "quant_robustness_test"},
}
_turn_tool_counts: dict[str, int] = {}

_strategy_name_re = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _arguments_hash(tool: str, arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"tool": tool, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_backtest_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "strategy_name", "symbols", "timeframes", "start_date", "end_date",
        "initial_balance", "leverage", "venue", "market_type",
        "execution_model", "parameters", "check_data_integrity",
        "catalog_path", "chunk_size", "ignore_missing_data", "project_id", "request_id",
    }
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise HTTPException(422, f"回测参数包含未知字段：{', '.join(unknown)}")
    normalized = {key: value for key, value in arguments.items() if value is not None}
    name = str(normalized.get("strategy_name") or "").strip()
    if not _strategy_name_re.fullmatch(name):
        raise HTTPException(422, "回测策略名不合法")
    symbols = [str(item).strip().upper() for item in normalized.get("symbols") or [] if str(item).strip()]
    timeframes = [str(item).strip() for item in normalized.get("timeframes") or [] if str(item).strip()]
    if not symbols or not timeframes:
        raise HTTPException(422, "回测必须明确提供 symbols 与 timeframes")
    try:
        start = date.fromisoformat(str(normalized.get("start_date") or ""))
        end = date.fromisoformat(str(normalized.get("end_date") or ""))
    except ValueError as exc:
        raise HTTPException(422, "回测日期必须使用 YYYY-MM-DD") from exc
    if end <= start:
        raise HTTPException(422, "回测结束日期必须晚于开始日期")
    balance = float(normalized.get("initial_balance", 10000.0))
    leverage = float(normalized.get("leverage", 1.0))
    if balance <= 0 or not 0 < leverage <= 125:
        raise HTTPException(422, "初始资金必须大于 0，杠杆必须在 (0, 125] 范围")
    market_type = str(normalized.get("market_type") or "um")
    execution_model = str(normalized.get("execution_model") or "CONSERVATIVE")
    if market_type not in {"spot", "um"} or execution_model != "CONSERVATIVE":
        raise HTTPException(422, "仅支持 spot/um 市场与 CONSERVATIVE 成交模型")
    return {
        "strategy_name": name,
        "symbols": list(dict.fromkeys(symbols)),
        "timeframes": list(dict.fromkeys(timeframes)),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "initial_balance": balance,
        "leverage": leverage,
        "venue": str(normalized.get("venue") or "BINANCE").upper(),
        "market_type": market_type,
        "execution_model": execution_model,
        "parameters": dict(normalized.get("parameters") or {}),
        "check_data_integrity": bool(normalized.get("check_data_integrity", True)),
    }


def reset_turn_budget(project_id: str) -> None:
    _turn_tool_counts[project_id] = 0


def _bounded_result(value: Any) -> Any:
    """Bound tool payloads before they enter the model context."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    limit = settings.dsh_tool_result_max_chars
    if len(raw) <= limit:
        return value
    return {"ok": bool(value.get("ok", True)) if isinstance(value, dict) else True, "truncated": True, "preview": raw[:limit], "original_chars": len(raw)}


def _compact_approval_result(tool: str, value: Any) -> Any:
    """Keep actionable verification data while excluding large source diffs."""
    if tool != "write_strategy_code" or not isinstance(value, dict):
        return _bounded_result(value)
    compact = dict(value)
    diff = compact.get("diff")
    if isinstance(diff, dict):
        compact["diff"] = {
            "files": diff.get("files", []),
            "additions": diff.get("additions", 0),
            "deletions": diff.get("deletions", 0),
        }
    return _bounded_result(compact)

# ---------------------------------------------------------------------------
# Approval registry (in-memory + JSON persistence for resilience)
# ---------------------------------------------------------------------------

_registry: dict[str, dict[str, dict[str, Any]]] = {}
_registry_lock = threading.RLock()

def _registry_path() -> Path:
    return settings.data_root / "dsh" / "approvals.json"

def _load_registry() -> None:
    with _registry_lock:
        try:
            p = _registry_path()
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _registry.clear()
                    _registry.update(data)
        except Exception:
            logger.warning("DSH approval registry 读取失败，保留当前注册表", exc_info=True)

def _save_registry() -> None:
    with _registry_lock:
        tmp_path: Path | None = None
        try:
            p = _registry_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".approvals.", suffix=".tmp", dir=p.parent)
            tmp_path = Path(tmp_name)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(_registry, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, p)
        except Exception:
            logger.warning("DSH approval registry 持久化失败", exc_info=True)
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink()

_load_registry()


def _project_workspace(project: ResearchProject) -> Path:
    ws = (settings.data_root / "dsh" / "workspaces" / project.id).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws

def _workspace_strategy_file(project: ResearchProject, strategy_name: str) -> Path:
    ws = _project_workspace(project)
    target = (ws / "backend/app/strategies" / f"{strategy_name}.py").resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _clean_strategy_code(name: str, code: Any) -> str:
    if not isinstance(code, str) or not code.strip():
        raise ValueError("策略代码为空")
    from ..agent.strategy_verifier import extract_python_strategy_code

    clean = extract_python_strategy_code(code) if "```" in code else code
    clean = clean.strip() + "\n"
    try:
        compile(clean, f"{name}.py", "exec")
    except SyntaxError as exc:
        raise ValueError(f"策略代码存在语法错误：{exc}") from exc
    return clean


def _atomic_workspace_write(target: Path, code: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.stem}.", suffix=".tmp", dir=target.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(code)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    finally:
        tmp_path.unlink(missing_ok=True)


def _diff_vs_baseline(target: Path, name: str) -> dict[str, Any]:
    from ..strategy_files import _path

    baseline_path = _path(name)
    old_lines = baseline_path.read_text(encoding="utf-8").splitlines(keepends=True) if baseline_path.exists() else []
    new_lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{name}.py", tofile=f"b/{name}.py"))
    additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    return {
        "files": [{"path": f"backend/app/strategies/{name}.py", "additions": additions, "deletions": deletions}],
        "additions": additions,
        "deletions": deletions,
        "diff": "".join(diff),
    }


async def _authorize(authorization: str | None = Header(default=None)) -> None:
    if not settings.dsh_bridge_token:
        return
    expected = f"Bearer {settings.dsh_bridge_token}"
    if authorization != expected:
        raise HTTPException(403, "DSH bridge 鉴权失败")

async def _project_or_404(project_id: str, db: AsyncSession) -> ResearchProject:
    project = await db.get(ResearchProject, project_id)
    if not project:
        raise HTTPException(404, "研究项目不存在")
    return project


def _find_registry_entry(project_id: str, tool: str, proposal_key: str, request_id: str | None) -> dict[str, Any] | None:
    bucket = _registry.get(project_id, {})
    if request_id and request_id in bucket and bucket[request_id].get("tool") == tool:
        return bucket[request_id]
    for entry in bucket.values():
        if entry.get("tool") == tool and entry.get("proposal_key") == proposal_key:
            return entry
    return None


async def create_pending_proposal(
    project: ResearchProject,
    tool: str,
    arguments: dict[str, Any],
    db: AsyncSession,
    proposal_key: str = "",
) -> dict[str, Any]:
    """Create or reuse an approval proposal without spending another model turn."""
    if tool not in APPROVAL_TOOLS:
        raise HTTPException(400, f"{tool} 不是受审批保护的工具")
    if tool == "execute_backtest_tool":
        arguments = _normalize_backtest_arguments(arguments)
    if tool == "write_strategy_code":
        name = str(arguments.get("strategy_name") or "").strip()
        if not _strategy_name_re.fullmatch(name):
            raise HTTPException(422, "策略名不合法")
        try:
            proposed_code = _clean_strategy_code(name, arguments.get("code"))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        target = _workspace_strategy_file(project, name)
        if not target.exists():
            raise HTTPException(409, "正式发布前必须先调用 stage_strategy_candidate 落盘并校验候选代码")
        staged_code = target.read_text(encoding="utf-8")
        if staged_code != proposed_code:
            raise HTTPException(409, "发布源码与候选区不一致；请先暂存或补丁修复，再读取候选源码提交")
        verification = await asyncio.to_thread(lambda: _verify_custom_path(target, name))
        if not verification.get("ok"):
            return {
                "status": "verification_failed",
                "tool": tool,
                "verification": _bounded_result(verification),
                "message": "候选代码尚未通过 Pre-Flight；请只修复报告中的问题，禁止发起发布审批。",
            }
    existing = _find_registry_entry(project.id, tool, proposal_key, None)
    if existing is not None and existing.get("status") == "pending":
        entry = existing
    else:
        entry = {
            "request_id": str(uuid.uuid4()),
            "project_id": project.id,
            "tool": tool,
            "proposal_key": proposal_key,
            "arguments": arguments,
            "arguments_hash": _arguments_hash(tool, arguments),
            "status": "pending",
            "feedback": "",
            "created_at": datetime.now(UTC).isoformat(),
        }
        _registry.setdefault(project.id, {})[entry["request_id"]] = entry
        _save_registry()
    apply_research_phase(project, (
        "AWAITING_IMPLEMENTATION_APPROVAL"
        if tool == "write_strategy_code"
        else "AWAITING_BACKTEST_APPROVAL"
    ))
    await db.commit()
    return {
        "status": "awaiting_approval",
        "request_id": entry["request_id"],
        "tool": tool,
        "summary": _status_for_awaiting(tool),
        "message": "请审核卡片中的固定参数；批准后将直接执行，不再调用模型。",
    }


# ---------------------------------------------------------------------------
# Domain tool executors
# ---------------------------------------------------------------------------

async def _exec_write_strategy_code(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    name = (arguments.get("strategy_name") or "").strip()
    code = arguments.get("code")
    if not _strategy_name_re.fullmatch(name):
        return {"ok": False, "error": f"非法策略名：{name}（需小写下划线标识符）"}
    try:
        clean_code = _clean_strategy_code(name, code)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    from ..strategy_files import save_strategy_code

    target = _workspace_strategy_file(project, name)
    _atomic_workspace_write(target, clean_code)

    v_result = await asyncio.to_thread(  # sync, CPU-bound 4-level sandbox
        lambda: _verify_custom_path(target, name)
    )
    verified = bool(v_result.get("ok")) if isinstance(v_result, dict) else False
    await _record_candidate_revision(db, project, name, clean_code, v_result, source="AGENT")
    diff = _diff_vs_baseline(target, name)
    saved_path: Path | None = None
    strategy_version_id: str | None = None
    if verified:
        # Publish only a verified candidate. Invalid candidates stay isolated in
        # the project workspace and can never replace the executable strategy.
        from ..strategy_files import PERSISTENT_STRATEGY_DIR, STRATEGY_DIR
        canonical_path = STRATEGY_DIR / f"{name}.py"
        persistent_path = PERSISTENT_STRATEGY_DIR / f"{name}.py"
        previous_code = canonical_path.read_text(encoding="utf-8") if canonical_path.exists() else None
        saved_path = save_strategy_code(name, clean_code)
        from ..quant.strategy_manager import ensure_strategy_db_record

        record = await ensure_strategy_db_record(name, db, project_id=project.id)
        if record is None:
            if previous_code is None:
                canonical_path.unlink(missing_ok=True)
                persistent_path.unlink(missing_ok=True)
            else:
                save_strategy_code(name, previous_code)
            return {
                "ok": False,
                "status": "publish_failed",
                "strategy_name": name,
                "workspace_path": str(target),
                "verification": v_result,
                "diff": diff,
                "error": "策略已通过 Pre-Flight，但创建不可变版本记录失败",
            }
        strategy_version_id = record[1].id
    return {
        "ok": verified,
        "status": "written" if verified else "verification_failed",
        "strategy_name": name,
        "saved_path": str(saved_path) if saved_path else None,
        "strategy_version_id": strategy_version_id,
        "workspace_path": str(target),
        "verification": v_result,
        "diff": diff,
        "error": None if verified else f"Pre-Flight 校验未通过 [{v_result.get('failed_level', 'L1')}]: {v_result.get('error_message')}",
    }


async def _exec_read_strategy_candidate(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    del db
    name = str(arguments.get("strategy_name") or "").strip()
    if not _strategy_name_re.fullmatch(name):
        return {"ok": False, "error": "策略名不合法"}
    target = _workspace_strategy_file(project, name)
    if not target.exists():
        return {"ok": False, "error": f"候选文件不存在：{name}.py，请先调用 stage_strategy_candidate"}
    code = target.read_text(encoding="utf-8")
    if len(code) > settings.dsh_candidate_code_max_chars:
        return {"ok": False, "error": "候选文件超过允许读取大小", "code_chars": len(code)}
    return {
        "ok": True,
        "strategy_name": name,
        "code": code,
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
    }


async def _exec_stage_strategy_candidate(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    name = str(arguments.get("strategy_name") or "").strip()
    if not _strategy_name_re.fullmatch(name):
        return {"ok": False, "error": "策略名不合法"}
    try:
        code = _clean_strategy_code(name, arguments.get("code"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    target = _workspace_strategy_file(project, name)
    _atomic_workspace_write(target, code)
    verification = await asyncio.to_thread(lambda: _verify_custom_path(target, name))
    await _record_candidate_revision(db, project, name, code, verification, source="AGENT")
    return {
        "ok": bool(verification.get("ok")),
        "status": "candidate_verified" if verification.get("ok") else "candidate_staged",
        "strategy_name": name,
        "workspace_path": str(target),
        "code_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "verification": verification,
    }


async def _exec_patch_strategy_candidate(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    name = str(arguments.get("strategy_name") or "").strip()
    edits = arguments.get("edits")
    if not _strategy_name_re.fullmatch(name):
        return {"ok": False, "error": "策略名不合法"}
    if not isinstance(edits, list) or not edits or len(edits) > 20:
        return {"ok": False, "error": "edits 必须包含 1 至 20 个精确替换"}
    target = _workspace_strategy_file(project, name)
    if not target.exists():
        return {"ok": False, "error": f"候选文件不存在：{name}.py，请先调用 stage_strategy_candidate"}
    original = target.read_text(encoding="utf-8")
    patched = original
    for index, edit in enumerate(edits, 1):
        if not isinstance(edit, dict) or not isinstance(edit.get("old"), str) or not isinstance(edit.get("new"), str):
            return {"ok": False, "error": f"第 {index} 个 edit 必须包含字符串 old/new；候选文件未修改"}
        old = edit["old"]
        occurrences = patched.count(old) if old else 0
        if occurrences != 1:
            return {
                "ok": False,
                "error": f"第 {index} 个 old 匹配 {occurrences} 次，必须恰好匹配一次；候选文件未修改",
            }
        patched = patched.replace(old, edit["new"], 1)
    try:
        patched = _clean_strategy_code(name, patched)
    except ValueError as exc:
        return {"ok": False, "error": f"补丁结果无效：{exc}；候选文件未修改"}
    _atomic_workspace_write(target, patched)
    verification = await asyncio.to_thread(lambda: _verify_custom_path(target, name))
    patch_text = "".join(difflib.unified_diff(
        original.splitlines(keepends=True),
        patched.splitlines(keepends=True),
        fromfile=f"a/{name}.py",
        tofile=f"b/{name}.py",
    ))
    await _record_candidate_revision(
        db, project, name, patched, verification, source="AGENT", patch=patch_text
    )
    return {
        "ok": bool(verification.get("ok")),
        "status": "candidate_verified" if verification.get("ok") else "candidate_patched",
        "strategy_name": name,
        "applied_edits": len(edits),
        "code_sha256": hashlib.sha256(patched.encode("utf-8")).hexdigest(),
        "verification": verification,
    }


def _verify_custom_path(target: Path, name: str) -> dict[str, Any]:
    from ..agent.strategy_verifier import verify_strategy_file

    res = verify_strategy_file(target, strategy_name=name)
    out = res.to_dict() if hasattr(res, "to_dict") else res
    if isinstance(out, dict):
        out.setdefault("diagnostics", _verification_diagnostics(out, target))
    return out


def _verification_diagnostics(result: dict[str, Any], target: Path) -> list[dict[str, Any]]:
    if result.get("ok"):
        return []
    message = str(result.get("error_message") or result.get("error") or "策略校验失败")
    level = str(result.get("failed_level") or "L1")
    lowered = message.lower()
    category = "CONTRACT_ERROR" if any(
        item in lowered for item in ("manifest", "parameterspec", "plot_config", "contract")
    ) else "STRATEGY_RUNTIME_ERROR"
    line_match = re.search(r"(?:line|第)\s*(\d+)", message, re.IGNORECASE)
    return [{
        "code": f"QL-{level}-001",
        "level": level,
        "severity": "error",
        "file": str(target),
        "line": int(line_match.group(1)) if line_match else None,
        "message": message,
        "category": category,
        "auto_fixable": category == "CONTRACT_ERROR",
        "repair_scope": "generated" if category == "CONTRACT_ERROR" else "strategy_logic",
    }]


async def _record_candidate_revision(
    db: AsyncSession,
    project: ResearchProject,
    name: str,
    code: str,
    verification: dict[str, Any],
    *,
    source: str,
    patch: str = "",
) -> None:
    from ..models import CandidateRevision, VerificationRun

    if not all(hasattr(db, attr) for attr in ("scalar", "add", "flush", "commit")):
        return

    parent = await db.scalar(
        select(CandidateRevision).where(
            CandidateRevision.project_id == project.id,
            CandidateRevision.strategy_name == name,
        ).order_by(CandidateRevision.created_at.desc())
    )
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    revision = CandidateRevision(
        project_id=project.id,
        strategy_name=name,
        parent_revision_id=parent.id if parent else None,
        code_sha256=digest,
        code=code,
        patch=patch,
        source=source,
        verification_json=verification,
    )
    db.add(revision)
    await db.flush()
    db.add(VerificationRun(
        project_id=project.id,
        candidate_revision_id=revision.id,
        code_sha256=digest,
        contract_version=str(verification.get("contract_version") or "1"),
        ok=bool(verification.get("ok")),
        diagnostics=list(verification.get("diagnostics") or []),
        result_json=verification,
    ))
    await db.commit()


async def _resolve_strategy_name_for_project(
    project: ResearchProject,
    given_name: str | None,
    db: AsyncSession,
) -> str:
    from ..strategy_files import _path
    from ..models import Strategy
    from ..strategy_contract import sanitize_strategy_slug

    clean_name = sanitize_strategy_slug(given_name or "") if given_name else ""
    # A project binding is authoritative. A caller-supplied name may only
    # confirm that binding; it can never select a global file from another
    # project.
    if project.strategy_id:
        strat = await db.get(Strategy, project.strategy_id)
        if strat and strat.slug and _path(strat.slug).exists():
            if clean_name and clean_name != strat.slug:
                logger.info(
                    "项目 %s 绑定策略为 %s，调用方提供的策略名 %s 已自动对齐为绑定策略",
                    project.id,
                    strat.slug,
                    clean_name,
                )
            return strat.slug

    # Before first publication, only this project's isolated workspace is a
    # valid source. Never guess from the shared strategy directory.
    ws = _project_workspace(project)
    candidates = [f.stem for f in (ws / "backend/app/strategies").glob("*.py") if not f.name.startswith("__")]
    if clean_name and clean_name in candidates:
        return clean_name
    if not clean_name and len(candidates) == 1:
        return candidates[0]
    raise HTTPException(409, "当前项目没有明确绑定的策略版本，请先完成策略编写与校验")


async def _exec_verify_strategy_file(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    raw_name = (arguments.get("strategy_name") or "").strip()
    name = await _resolve_strategy_name_for_project(project, raw_name, db)
    target = _workspace_strategy_file(project, name)
    if not target.exists():
        from ..strategy_files import _path
        p_target = _path(name)
        if p_target.exists():
            target = p_target
        else:
            return {"ok": False, "error": f"待校验策略文件不存在：{name}.py（请先 write_strategy_code）"}
    return await asyncio.to_thread(lambda: _verify_custom_path(target, name))


async def _exec_execute_backtest(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    from ..quant.backtest import run_nautilus_backtest

    raw_name = (arguments.get("strategy_name") or "").strip()
    name = await _resolve_strategy_name_for_project(project, raw_name, db)
    symbols = arguments.get("symbols") or ["BTCUSDT"]
    return await run_nautilus_backtest(
        strategy_name=name,
        symbols=[str(s) for s in symbols],
        start_date=str(arguments.get("start_date", "2024-01-01")),
        end_date=str(arguments.get("end_date", "2024-06-30")),
        initial_balance=float(arguments.get("initial_balance", 10000.0)),
        leverage=float(arguments.get("leverage", 1.0)),
        timeframes=[str(item) for item in (arguments.get("timeframes") or [])],
        venue=str(arguments.get("venue") or "BINANCE"),
        market_type=str(arguments.get("market_type") or "um"),
        execution_model=str(arguments.get("execution_model") or "CONSERVATIVE"),
        approval_hash=_arguments_hash("execute_backtest_tool", arguments),
        parameters=arguments.get("parameters"),
        check_data_integrity=bool(arguments.get("check_data_integrity", True)),
        ignore_missing_data=bool(arguments.get("ignore_missing_data", True)),
        project_id=project.id,
        db=db,
    )


async def _exec_dispatch_tool(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    tool_name = (arguments.get("tool_name") or "").strip()
    tool_args = arguments.get("arguments") or {}
    phase = project.research_phase or "RESEARCH"
    allowed_tools = DISPATCH_TOOLS_BY_PHASE.get(phase)
    if allowed_tools is None:
        return {"ok": False, "error": f"当前阶段 {phase} 禁止调用分析工具"}
    if tool_name not in allowed_tools:
        return {"ok": False, "error": f"{phase} 阶段不允许调用 {tool_name}"}
    if phase == "RESEARCH":
        count = _turn_tool_counts.get(project.id, 0) + 1
        _turn_tool_counts[project.id] = count
        if count > settings.dsh_research_max_tool_calls:
            return {"ok": False, "error": "研究工具预算已用完，请停止调用工具并立即输出策略研究结论", "error_code": "RESEARCH_TOOL_BUDGET_EXCEEDED"}
    result = await dispatch_dsh_tool_call(
        tool_name, tool_args, project_id=project.id, db=db
    )
    return _bounded_result(result)


_EXECUTORS = {
    "read_strategy_candidate": _exec_read_strategy_candidate,
    "stage_strategy_candidate": _exec_stage_strategy_candidate,
    "patch_strategy_candidate": _exec_patch_strategy_candidate,
    "write_strategy_code": _exec_write_strategy_code,
    "verify_strategy_file": _exec_verify_strategy_file,
    "execute_backtest_tool": _exec_execute_backtest,
    "dispatch_tool_call": _exec_dispatch_tool,
}


async def execute_approved_proposal(
    project: ResearchProject,
    request_id: str,
    db: AsyncSession,
) -> dict[str, Any]:
    """Execute an approved proposal without requiring another model turn."""
    entry = _registry.get(project.id, {}).get(request_id)
    if entry is None:
        raise HTTPException(404, "审批请求不存在或已消费")
    if entry.get("status") != "approved":
        raise HTTPException(409, "审批请求尚未批准")

    tool = str(entry.get("tool") or "")
    executor = _EXECUTORS.get(tool)
    if executor is None:
        raise HTTPException(400, f"未知审批工具：{tool}")
    arguments = dict(entry.get("arguments") or {})
    expected_hash = str(entry.get("arguments_hash") or "")
    actual_hash = _arguments_hash(tool, arguments)
    if expected_hash and not secrets.compare_digest(expected_hash, actual_hash):
        raise HTTPException(409, "审批参数在审核后发生变化，已拒绝执行")

    # Execute the exact arguments the user reviewed. Keep the entry until the
    # executor completes, making transient failures retryable.
    try:
        result = await executor(project, arguments, db)
    except Exception as exc:
        logger.exception("执行审批提案失败")
        result = {"ok": False, "error": f"执行失败：{exc}"}

    _registry.setdefault(project.id, {}).pop(request_id, None)
    _save_registry()
    return {
        "status": "ok",
        "request_id": request_id,
        "tool": tool,
        "result": _compact_approval_result(tool, result),
    }


def _status_for_awaiting(name: str) -> str:
    if name == "write_strategy_code":
        return "write_strategy_code 需要审批"
    if name == "execute_backtest_tool":
        return "execute_backtest_tool 需要审批"
    return f"{name} 需要审批"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class CallRequest(BaseModel):
    project_id: str
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    proposal_key: str | None = None
    request_id: str | None = None


class ApproveRequest(BaseModel):
    project_id: str
    request_id: str
    approved: bool
    feedback: str = ""


@router.post("/call")
async def dsh_call(
    req: CallRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    await _authorize(authorization)
    project = await _project_or_404(req.project_id, db)
    phase = project.research_phase or "RESEARCH"
    executor = _EXECUTORS.get(req.tool)
    if not executor:
        return {"status": "error", "error": f"未知工具：{req.tool}"}

    coding_phases = {"IMPLEMENTATION", "REPAIR", "FIX_ERROR"}
    if req.tool in {"verify_strategy_file", "write_strategy_code"} and phase not in coding_phases:
        return {"status": "error", "error": "当前阶段禁止验证或发布策略；请进入 Coding Worker"}
    if req.tool in {"read_strategy_candidate", "stage_strategy_candidate", "patch_strategy_candidate"} and phase not in {"IMPLEMENTATION", "REPAIR", "FIX_ERROR"}:
        return {"status": "error", "error": "当前阶段不允许读写策略候选区"}
    if req.tool == "execute_backtest_tool" and phase not in {"IMPLEMENTATION", "IMPLEMENTED", "AWAITING_BACKTEST_APPROVAL", "BACKTEST", "BACKTEST_RETRY"}:
        return {"status": "error", "error": "当前阶段不允许提交正式回测"}

    if req.tool in APPROVAL_TOOLS and settings.dsh_require_action_approvals:
        entry = _find_registry_entry(project.id, req.tool, req.proposal_key or "", req.request_id)
        if entry is None:
            return await create_pending_proposal(
                project,
                req.tool,
                req.arguments,
                db,
                proposal_key=req.proposal_key or "",
            )
        if entry["status"] == "pending":
            return {
                "status": "awaiting_approval",
                "request_id": entry["request_id"],
                "tool": req.tool,
                "summary": _status_for_awaiting(req.tool),
                "message": "该请求仍在等待审批，请结束本轮并向用户征询批准。",
            }
        if entry["status"] == "declined":
            return {
                "status": "declined",
                "request_id": entry["request_id"],
                "tool": req.tool,
                "feedback": entry.get("feedback", ""),
                "message": "用户拒绝该请求，请根据反馈调整方案后重新提出新的审批请求。",
            }
        # approved -> consume and execute the original reviewed arguments
        return await execute_approved_proposal(project, entry["request_id"], db)

    result = await executor(project, req.arguments, db)
    # Candidate source is deliberately available only inside the isolated
    # project workspace; do not truncate it with the generic 8K tool budget.
    bounded = result if req.tool == "read_strategy_candidate" else _bounded_result(result)
    return {"status": "ok", "result": bounded}


@router.post("/approve")
async def dsh_approve(
    req: ApproveRequest,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    await _authorize(authorization)
    await _project_or_404(req.project_id, db)
    return approve_proposal(req.project_id, req.request_id, req.approved, req.feedback)


def approve_proposal(
    project_id: str, request_id: str, approved: bool, feedback: str = ""
) -> dict[str, Any]:
    bucket = _registry.setdefault(project_id, {})
    entry = bucket.get(request_id)
    if entry is None:
        raise HTTPException(404, "审批请求不存在或已处理")
    if entry.get("status") == "approved":
        raise HTTPException(409, "该审批请求已批准")
    entry["status"] = "approved" if approved else "declined"
    entry["feedback"] = feedback
    entry["decided_at"] = datetime.now(UTC).isoformat()
    _save_registry()
    return {"ok": True, "request_id": request_id, "status": entry["status"], "feedback": feedback}


def pending_approvals(project_id: str) -> list[dict[str, Any]]:
    if not settings.dsh_require_action_approvals:
        return []
    bucket = _registry.get(project_id, {})
    return [e for e in bucket.values() if e.get("status") == "pending"]


@router.get("/pending")
async def dsh_pending(
    project_id: str,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(default=None),
):
    await _authorize(authorization)
    await _project_or_404(project_id, db)
    return pending_approvals(project_id)
