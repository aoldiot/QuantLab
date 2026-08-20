"""HTTP bridge between the DSH runtime plugin and the QuantLab backend.

The DSH agent subprocess talks to FastAPI over loopback HTTP. The plugin
declares domain tools (write_strategy_code / verify_strategy_file /
execute_backtest_tool / dispatch_tool_call); each call lands here, where the
backend acts as the gatekeeper: it runs the 4-level Pre-Flight sandbox, the
isolated backtest gate, Git bookkeeping and the interactive approval registry.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import get_db
from ..models import ResearchProject
from .tools import dispatch_dsh_tool_call

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dsh-tools", tags=["dsh-bridge"])

APPROVAL_TOOLS = {"write_strategy_code", "execute_backtest_tool"}
RESEARCH_DISPATCH_TOOLS = {
    "quant_get_capabilities", "quant_get_research_context", "quant_get_strategy_context",
    "quant_web_research", "quant_market_data_query", "quant_factor_analysis", "quant_run_experiment",
}
_turn_tool_counts: dict[str, int] = {}

_strategy_name_re = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def reset_turn_budget(project_id: str) -> None:
    _turn_tool_counts[project_id] = 0


def _bounded_result(value: Any) -> Any:
    """Bound tool payloads before they enter the model context."""
    raw = json.dumps(value, ensure_ascii=False, default=str)
    limit = settings.dsh_tool_result_max_chars
    if len(raw) <= limit:
        return value
    return {"ok": bool(value.get("ok", True)) if isinstance(value, dict) else True, "truncated": True, "preview": raw[:limit], "original_chars": len(raw)}

# ---------------------------------------------------------------------------
# Approval registry (in-memory + JSON persistence for resilience)
# ---------------------------------------------------------------------------

_registry: dict[str, dict[str, dict[str, Any]]] = {}

def _registry_path() -> Path:
    return settings.data_root / "dsh" / "approvals.json"

def _load_registry() -> None:
    try:
        p = _registry_path()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _registry.update(data)
    except Exception:
        logger.warning("DSH approval registry 读取失败，使用空注册表", exc_info=True)

def _save_registry() -> None:
    try:
        p = _registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_registry, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("DSH approval registry 持久化失败", exc_info=True)

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
            "status": "pending",
            "feedback": "",
            "created_at": datetime.now(UTC).isoformat(),
        }
        _registry.setdefault(project.id, {})[entry["request_id"]] = entry
        _save_registry()
    project.research_phase = (
        "AWAITING_IMPLEMENTATION_APPROVAL"
        if tool == "write_strategy_code"
        else "AWAITING_BACKTEST_APPROVAL"
    )
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
    code = arguments.get("code") or ""
    if not _strategy_name_re.match(name):
        return {"ok": False, "error": f"非法策略名：{name}（需小写下划线标识符）"}
    if not isinstance(code, str) or not code.strip():
        return {"ok": False, "error": "策略代码为空"}
    try:
        compile(code, f"{name}.py", "exec")
    except SyntaxError as exc:
        return {"ok": False, "error": f"策略代码存在语法错误：{exc}"}

    from ..agent.strategy_verifier import extract_python_strategy_code
    from ..strategy_files import save_strategy_code

    clean_code = extract_python_strategy_code(code) if ("```" in code or not code.startswith(("from", "import", "#"))) else code
    if not clean_code.strip():
        clean_code = code.strip()

    target = _workspace_strategy_file(project, name)
    target.write_text(clean_code, encoding="utf-8")

    saved_path = save_strategy_code(name, clean_code)
    v_result = await asyncio.to_thread(  # sync, CPU-bound 4-level sandbox
        lambda: _verify_custom_path(target, name)
    )
    verified = bool(v_result.get("ok")) if isinstance(v_result, dict) else False
    return {
        "ok": verified,
        "status": "written" if verified else "verification_failed",
        "strategy_name": name,
        "saved_path": str(saved_path),
        "workspace_path": str(target),
        "verification": v_result,
        "diff": _diff_vs_baseline(target, name),
        "error": None if verified else f"Pre-Flight 校验未通过 [{v_result.get('failed_level', 'L1')}]: {v_result.get('error_message')}",
    }


def _verify_custom_path(target: Path, name: str) -> dict[str, Any]:
    from ..agent.strategy_verifier import verify_strategy_file

    res = verify_strategy_file(target, strategy_name=name)
    out = res.to_dict() if hasattr(res, "to_dict") else res
    return out


async def _exec_verify_strategy_file(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    name = (arguments.get("strategy_name") or "").strip()
    target = _workspace_strategy_file(project, name)
    if not target.exists():
        return {"ok": False, "error": f"待校验策略文件不存在：{name}.py（请先 write_strategy_code）"}
    return await asyncio.to_thread(lambda: _verify_custom_path(target, name))


async def _exec_execute_backtest(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    from ..quant.backtest import run_nautilus_backtest

    name = (arguments.get("strategy_name") or "").strip()
    symbols = arguments.get("symbols") or ["BTCUSDT"]
    return await run_nautilus_backtest(
        strategy_name=name,
        symbols=[str(s) for s in symbols],
        start_date=str(arguments.get("start_date", "2024-01-01")),
        end_date=str(arguments.get("end_date", "2024-06-30")),
        initial_balance=float(arguments.get("initial_balance", 10000.0)),
        leverage=float(arguments.get("leverage", 1.0)),
        parameters=arguments.get("parameters"),
        project_id=project.id,
        db=db,
    )


async def _exec_dispatch_tool(
    project: ResearchProject, arguments: dict[str, Any], db: AsyncSession
) -> dict[str, Any]:
    tool_name = (arguments.get("tool_name") or "").strip()
    tool_args = arguments.get("arguments") or {}
    phase = project.research_phase or "RESEARCH"
    if phase == "RESEARCH":
        if tool_name not in RESEARCH_DISPATCH_TOOLS:
            return {"ok": False, "error": f"研究阶段不允许调用 {tool_name}"}
        count = _turn_tool_counts.get(project.id, 0) + 1
        _turn_tool_counts[project.id] = count
        if count > settings.dsh_research_max_tool_calls:
            return {"ok": False, "error": "研究工具预算已用完，请停止调用工具并立即输出策略研究结论", "error_code": "RESEARCH_TOOL_BUDGET_EXCEEDED"}
    result = await dispatch_dsh_tool_call(
        tool_name, tool_args, project_id=project.id, db=db
    )
    return _bounded_result(result)


_EXECUTORS = {
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

    # Execute the exact arguments the user reviewed. Keep the entry until the
    # executor completes, making transient failures retryable.
    result = await executor(project, dict(entry.get("arguments") or {}), db)
    _registry.setdefault(project.id, {}).pop(request_id, None)
    _save_registry()
    return {
        "status": "ok",
        "request_id": request_id,
        "tool": tool,
        "result": _bounded_result(result),
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

    if req.tool == "verify_strategy_file" and phase == "RESEARCH":
        return {"status": "error", "error": "研究阶段禁止执行策略验证；请先完成方案并获得开发审批"}
    if req.tool == "execute_backtest_tool" and phase not in {"IMPLEMENTATION", "AWAITING_BACKTEST_APPROVAL", "BACKTEST"}:
        return {"status": "error", "error": "当前阶段不允许提交正式回测"}

    if req.tool in APPROVAL_TOOLS:
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
    return {"status": "ok", "result": _bounded_result(result)}


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
