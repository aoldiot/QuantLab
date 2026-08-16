from __future__ import annotations

import asyncio
import dataclasses
import difflib
import json
import logging
import re
import shlex
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
from ..llm_config import MAX_API_RETRIES, format_sdk_error, get_config, sdk_env
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

logger = logging.getLogger(__name__)

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

NAUTILUS_STRATEGY_CHEATSHEET = """
【NautilusTrader 策略开发核心速查表与规范】
1. 依赖与模块导入规范（按需复制，严禁臆造不存在的模块）：
```python
from decimal import Decimal
import math
from collections import deque
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.indicators import (
    MovingAverageConvergenceDivergence, # MACD(fast_period, slow_period, signal_period)
    AverageTrueRange,                   # ATR(period)
    ExponentialMovingAverage,           # EMA(period)
    SimpleMovingAverage,                # SMA(period)
    RelativeStrengthIndex,              # RSI(period)
    BollingerBands,                     # BollingerBands(period, std_dev)
)
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode
```

2. 核心四大导出结构规范：
- 结构 1：`StrategyConfig` 子类（`frozen=True`）：包含 `instrument_ids: list[InstrumentId]`、`bar_types: list[BarType]`、`trade_size: Decimal` 及策略参数。
- 结构 2：`Strategy` 子类：
  - `__init__(self, config)`：初始化指标与历史缓存。
  - `on_start(self)`：
      for bar_type in self.config.bar_types:
          self.register_indicator_for_bars(self.indicator, bar_type)
          self.subscribe_bars(bar_type)
  - `on_bar(self, bar: Bar)`：
      - 指标取值：`val = float(self.indicator.value)`
      - 查当前持仓：`pos = self.cache.position(self._instrument_id)`
      - 持仓方向：`pos.side == PositionSide.LONG` 或 `PositionSide.SHORT`
      - 平仓：`self.close_all_positions(self._instrument_id)`
      - 开仓下单：
          instrument = self.cache.instrument(self._instrument_id)
          quantity = instrument.make_qty(Decimal(str(qty_value)))
          order = self.order_factory.market(
              instrument_id=self._instrument_id,
              order_side=OrderSide.BUY,
              quantity=quantity,
          )
          self.submit_order(order)
      - 净值查询：
          account = self.portfolio.account(self._instrument_id.venue)
          equity = float(account.equity(self._instrument_id.venue).as_double())
- 结构 3：`calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame`：向量化计算所有 plot_config 中的指标列。
- 结构 4：`STRATEGY_MANIFEST = StrategyManifest(...)`：声明元数据、parameters 规范字典及 plot_config。

3. 行为准则（CRITICAL）：
- 所有 API 和标准结构均已在上方完整给出。**严禁使用 Grep / Glob / Bash / Read 去遍历系统 site-packages 源码或全局目录**。
- 收到任务后，**必须分块写入 `backend/app/strategies/{strategy_name}.py`**，不要试图一次 Write 整个文件：
  1. 第一次 Write 只写文件骨架 —— 完整的 import 段、类声明、`STRATEGY_MANIFEST`，以及每个待填方法的占位行 `# __CHUNK_1__`、`# __CHUNK_2__`……
  2. 之后每次 Edit 只把一个 `# __CHUNK_N__` 替换成该方法的真实实现，一次一个，写完一个再写下一个。
  这不是风格偏好：上游网关会在单次响应过长时截断连接，导致工具入参 JSON 不完整、Write 静默失败并重试，实测一次失败会浪费十几分钟。每次回复保持简短是最有效的规避手段。
- 编写完成后，仅需调用一次 `python3 -m py_compile backend/app/strategies/{strategy_name}.py` 验证语法即可。
"""

SPEC_DEVIATION_CONTRACT = """

实现偏差报告（强制）：本轮如果你修改了策略文件，回复末尾必须追加一行
QUANTLAB_SPEC_DEVIATION:{紧凑JSON}
字段：conforms（布尔，代码是否完全落实规格）、deviations（数组，每项含 spec_field/what/why）、assumptions（数组，规格未明确而你自行假定的取值或口径）、unimplemented（数组，规格要求但本轮未实现的部分）。
这份报告会交给研究员 Hermes 用于回测归因，所以必须诚实：完全一致就填 conforms=true 且三个数组为空，不要为了显得完整而编造偏差；反之凡是你自己做过的取舍、猜测的阈值、简化的规则，都必须写进来，漏报会导致把实现缺陷误判为策略假设失效。这一行不会展示给用户，不要在正文里重复它的内容。"""


def _agent_root() -> Path:
    root = (settings.data_root / "agent").resolve()
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    (root / "transcripts").mkdir(parents=True, exist_ok=True)
    (root / "baselines").mkdir(parents=True, exist_ok=True)
    return root


def _canonical_strategy_file(strategy_name: str) -> Path:
    from ..strategy_files import _path
    return _path(strategy_name)


def _run_git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    repo = settings.strategy_repo_path.resolve()
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "Git 操作失败")
    return result


def create_worktree(session_id: str, strategy_name: str) -> Path:
    target = _agent_root() / "worktrees" / session_id
    target.mkdir(parents=True, exist_ok=True)
    repo = settings.strategy_repo_path.resolve()
    if (repo / ".git").exists():
        try:
            _run_git("worktree", "add", "--detach", str(target), "HEAD", check=False)
        except Exception:
            pass
    source = _canonical_strategy_file(strategy_name)
    destination = target / STRATEGY_RELATIVE / f"{strategy_name}.py"
    if source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        baseline = _agent_root() / "baselines" / session_id / f"{strategy_name}.py"
        baseline.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, baseline)
    contract = (Path(__file__).resolve().parent.parent / "strategy_contract.py").resolve()
    if not contract.exists():
        contract = settings.strategy_repo_path.resolve() / "backend/app/strategy_contract.py"
    if contract.exists():
        (target / "backend/app").mkdir(parents=True, exist_ok=True)
        shutil.copy2(contract, target / "backend/app/strategy_contract.py")
    tests = (Path(__file__).resolve().parent.parent.parent / "tests").resolve()
    if not tests.exists():
        tests = settings.strategy_repo_path.resolve() / "backend/tests"
    if tests.exists():
        shutil.copytree(tests, target / "backend/tests", dirs_exist_ok=True)
    skill = (Path(__file__).resolve().parents[3] / ".claude/skills/nautilus-strategy-author").resolve()
    if not skill.exists():
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
    """Rebase persisted worktree paths after the project directory is moved and restore strategies."""
    from ..strategy_files import ensure_strategy_storage
    ensure_strategy_storage()
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


def _bash_segments(command: str) -> list[list[str]] | None:
    """Split a compound command into per-segment token lists, respecting quoting.

    Returns None when the command cannot be tokenized, so the caller falls back
    to requiring approval rather than guessing at the intent.
    """
    segments: list[list[str]] = []
    for line in command.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            lexer = shlex.shlex(line, punctuation_chars=True, posix=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return None
        current: list[str] = []
        for token in tokens:
            if token and all(char in ";|&" for char in token):
                if current:
                    segments.append(current)
                    current = []
                continue
            current.append(token)
        if current:
            segments.append(current)
    return segments


# Command substitution lets an allowed command smuggle an arbitrary one, e.g.
# `ls "$(rm -rf /)"`, so any segment containing it must go to approval.
_SUBSTITUTION = re.compile(r"\$\(|\$\{|`|<\(")

_ALLOWED_COMMANDS = frozenset({
    "python", "python3", "pytest", "ruff", "uv", "pip", "pip3",
    "ls", "find", "which", "whereis", "echo", "cat", "head", "tail",
    "grep", "wc", "curl", "git",
})
_ALLOWED_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "branch", "show", "rev-parse"})
# `find` can execute arbitrary commands or delete files through these flags.
_FORBIDDEN_FIND_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf"})


def _safe_bash(command: str) -> bool:
    """Whitelist check applied to every segment of a compound command.

    Previously any command containing a newline was rejected outright, which
    pushed routine multi-line shell into the approval path and stalled the turn
    until the 300s approval timeout.
    """
    if not command.strip():
        return False
    segments = _bash_segments(command)
    if not segments:
        return False
    return all(_safe_bash_segment(tokens) for tokens in segments)


def _safe_bash_segment(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if any(_SUBSTITUTION.search(token) for token in tokens):
        return False
    name = Path(tokens[0]).name.lower()
    if name not in _ALLOWED_COMMANDS:
        return False
    if name == "git":
        return len(tokens) > 1 and tokens[1].lower() in _ALLOWED_GIT_SUBCOMMANDS
    if name == "find":
        return not any(token.lower() in _FORBIDDEN_FIND_FLAGS for token in tokens[1:])
    return True


def _is_strictly_forbidden(command: str) -> bool:
    """Commands to reject outright rather than offer for approval.

    Matched against a token stream so that `(`, quotes and command substitution
    cannot hide the verb, e.g. `ls "$(rm -rf /)"`.
    """
    cmd = command.strip()
    if not cmd:
        return True
    if any(re.search(p, cmd, re.IGNORECASE) for p in (
        r"(?:^|\s)git\s+(?:push|reset\s+--hard|clean\s+-fdx)",
    )):
        return True
    verbs = {"sudo", "mkfs", "shutdown", "reboot", "halt", "poweroff"}
    for tokens in _bash_segments(cmd) or []:
        for index, token in enumerate(tokens):
            bare = Path(token.strip("\"'`$(){}")).name.lower()
            if bare in verbs:
                return True
            rest = tokens[index + 1:]
            if bare == "rm" and any(flag.startswith("-") and "r" in flag.lower() for flag in rest):
                return True
            if bare == "dd" and any(flag.startswith("if=") for flag in rest):
                return True
    return False


async def bash_guard(input_data: dict[str, Any], _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
    command = str(input_data.get("tool_input", {}).get("command", "")).strip()
    if _safe_bash(command):
        return {}
    if _is_strictly_forbidden(command):
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "平台安全策略：禁止执行 sudo / 强制删除 / 远程推送等破坏性命令"}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "命令需要用户审批"}}


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
        return re.sub(r"\s*(?:QUANTLAB_BACKTEST_REQUEST|QUANTLAB_SPEC_DEVIATION):\{.*\}\s*", "", value, flags=re.DOTALL)
    return value


def _marker_payload(text: str, marker: str) -> dict[str, Any] | None:
    """Read the JSON object following `marker:`.

    Uses raw_decode rather than a regex so that nested braces parse correctly
    and a second marker later in the reply is not swallowed.
    """
    index = text.find(f"{marker}:{{")
    if index < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(text[index + len(marker) + 1:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


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
        if not session:
            return
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


async def build_options(
    session: AgentSession,
    stderr_collector: list[str] | None = None,
    use_resume: bool = True,
) -> ClaudeAgentOptions:
    # Ensure worktree directory exists on disk
    workspace_path = Path(session.workspace_path)
    if not workspace_path.exists() or session.workspace_path == "pending":
        workspace_path = create_worktree(session.id, session.strategy_name)
        session.workspace_path = str(workspace_path)
        async with SessionLocal() as db:
            db_session = await db.get(AgentSession, session.id)
            if db_session:
                db_session.workspace_path = str(workspace_path)
                await db.commit()

    async with SessionLocal() as db:
        config = await get_config(db)
        # The caller's AgentSession is detached and was loaded once when the
        # WebSocket opened, so its sdk_session_id is stale from turn 2 onward —
        # resuming with None makes Claude re-orient from scratch every turn.
        resume_id = (
            await db.scalar(select(AgentSession.sdk_session_id).where(AgentSession.id == session.id))
            if use_resume
            else None
        )
        specification = await db.get(StrategySpecification, session.specification_id) if session.specification_id else None
        specification_context = ""
        if specification:
            # Late import: research.py imports this module, so a top-level
            # import here would be circular.
            from ..research import _decisions, _resolved_decision_brief
            settled = _resolved_decision_brief(await _decisions(session.research_project_id, db)) if session.research_project_id else ""
            specification_context = (
                "\n当前任务绑定了用户已确认的策略规格。必须严格实现，发现歧义时停止猜测并明确指出：\n"
                + json.dumps(specification.content, ensure_ascii=False, indent=2)
                + settled
                + SPEC_DEVIATION_CONTRACT
            )
        tools = ["Read", "Glob", "Grep"] if session.permission_mode == "plan" else ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "Skill"]
        approved_tools = ["Read", "Glob", "Grep", "Skill(nautilus-strategy-author)"] if session.permission_mode == "plan" else (["Read", "Glob", "Grep", "Edit", "Write", "Skill(nautilus-strategy-author)"] if session.permission_mode == "default" else tools)
        async def request_approval(tool_name: str, tool_input: dict[str, Any], _context: Any = None):
            request_id = str(uuid.uuid4())
            future = asyncio.get_running_loop().create_future()
            APPROVALS[request_id] = future
            delivered = await send_session_event(session.id, {"type": "approval_required", "request_id": request_id, "tool": tool_name, "input": tool_input})
            if not delivered:
                APPROVALS.pop(request_id, None)
                return PermissionResultDeny(behavior="deny", message="Agent 面板连接已断开，无法完成敏感操作审批", interrupt=False)
            try:
                # 300s meant one unattended approval could burn an entire turn
                # (observed: 304s / 1 turn / 0 tokens). 120s is still ample for
                # a user who is watching the panel.
                approved = await asyncio.wait_for(future, timeout=120)
            except TimeoutError:
                approved = False
            finally:
                APPROVALS.pop(request_id, None)
            if approved:
                return PermissionResultAllow(behavior="allow", updated_input=None, updated_permissions=None)
            return PermissionResultDeny(behavior="deny", message="用户拒绝或审批超时", interrupt=False)

        async def session_bash_guard(input_data: dict[str, Any], _tool_use_id: str | None, _context: Any) -> dict[str, Any]:
            tool_input = input_data.get("tool_input", {})
            command = str(tool_input.get("command", "")).strip()
            # 1. 绝对破坏性高危命令：任何模式下一律硬性拦截
            if _is_strictly_forbidden(command):
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "平台安全策略：禁止执行 sudo / 强制删除 / 远程推送等破坏性命令"}}
            # 2. 完全自动模式 (bypassPermissions)：在安全边界内全自动执行，无需用户审批
            if session.permission_mode == "bypassPermissions":
                return {}
            # 3. 规划模式 (plan)：只读分析，不执行任何 Bash 操作
            if session.permission_mode == "plan":
                return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "规划模式下不支持执行系统命令"}}
            # 4. 安全白名单命令：在手动审批和自动编辑模式下自动放行，无需重复弹窗
            if _safe_bash(command):
                return {}
            # 5. 非白名单命令：在手动审批 (default) 和 自动编辑 (acceptEdits) 模式下弹出前端审批
            decision = await request_approval("Bash", tool_input, _context)
            if isinstance(decision, PermissionResultAllow) or (isinstance(decision, dict) and decision.get("behavior") == "allow"):
                return {}
            reason = getattr(decision, "message", "用户拒绝执行该命令或审批超时")
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}

        system_append = (
            "你是 QuantLab 的 NautilusTrader 策略开发 Agent，在一个已隔离的 git worktree 中工作，只负责编写和验证单个策略文件。\n"
            "所有面向用户的分析、计划、进度、提问、错误说明和最终回答必须使用简体中文；代码、命令、文件名和 API 字段保持原格式。\n"
            f"目标策略文件路径：`backend/app/strategies/{session.strategy_name}.py`。\n"
            "工作约定：\n"
            "- 直接动手，不要复述任务或征求许可。回复保持简短，不要在正文里重复代码内容。\n"
            "- 修改文件用 Edit（先 Read 再 Edit），新建文件用 Write。不要用 Bash 的重定向或 sed 改文件。\n"
            "- 只在必要时使用 Bash，且限于 py_compile、pytest、ruff、git status/diff 这类只读或验证命令。\n"
            "- 不要提交、推送或改动 git 状态：diff 审批和版本管理由平台完成。\n"
            "- 不要新建 README、说明文档或计划文档，除非用户明确要求。\n"
            + NAUTILUS_STRATEGY_CHEATSHEET.replace("{strategy_name}", session.strategy_name)
            + specification_context
        )

        def _on_stderr(line: str) -> None:
            if stderr_collector is not None:
                stderr_collector.append(line)
            logger.debug("Claude CLI stderr [%s]: %s", session.id, line)

        return ClaudeAgentOptions(
            cwd=Path(session.workspace_path),
            model=config.model,
            env=sdk_env(config),
            tools=tools,
            allowed_tools=approved_tools,
            permission_mode=session.permission_mode,
            setting_sources=["project"],
            skills=["nautilus-strategy-author"],
            max_turns=config.max_turns or 60,
            resume=resume_id,
            enable_file_checkpointing=True,
            sandbox={"enabled": True, "autoAllowBashIfSandboxed": False},
            system_prompt=system_append,
            hooks={"PreToolUse": [HookMatcher(matcher="Bash", hooks=[session_bash_guard])]},
            can_use_tool=request_approval if session.permission_mode == "default" else None,
            stderr=_on_stderr,
        )


async def run_prompt(session: AgentSession, prompt: str) -> None:
    original_prompt = prompt
    backtest_requested = bool(re.search(r"回测|backtest", original_prompt, flags=re.IGNORECASE))
    if not prompt.startswith("/"):
        action_rule = "用户本轮明确要求回测。完成策略编写与验证后，可以在回复末尾输出 QUANTLAB_BACKTEST_REQUEST:{紧凑JSON} 请求平台回测；execution_model 只能是 FAST、STANDARD、CONSERVATIVE。" if backtest_requested else "用户本轮没有明确要求回测。严禁发起回测或输出 QUANTLAB_BACKTEST_REQUEST。请专注完成策略代码的编写与测试。"
        prompt = (
            f"请始终使用简体中文与用户交流，代码、命令、路径和字段名除外。\n"
            f"{action_rule}\n"
            f"目标策略代码文件路径：backend/app/strategies/{session.strategy_name}.py\n"
            f"请先 Write 骨架（import、类声明、STRATEGY_MANIFEST 和 # __CHUNK_N__ 占位行），再逐个用 Edit 把占位行替换成方法实现，一次只写一个；不要一次 Write 整个文件，否则响应过长会被上游网关截断。最后用 python3 -m py_compile 验证语法。无需在外部文件系统中做额外搜索。\n\n"
            f"{original_prompt}"
        )

    client_lock = CLIENT_LOCKS.setdefault(session.client_id, asyncio.Lock())
    strategy_lock = STRATEGY_LOCKS.setdefault(session.strategy_name, asyncio.Lock())
    execution_lock = strategy_lock if session.permission_mode != "plan" else asyncio.Lock()
    await send_session_event(session.id, {"type": "queued"})
    async with GLOBAL_SEMAPHORE, client_lock, execution_lock:
        await update_status(session.id, AgentSessionStatus.RUNNING)
        await persist_event(session.id, "user", "message", {"text": original_prompt})
        await send_session_event(session.id, {"type": "status", "status": "RUNNING"})
        client: ClaudeSDKClient | None = None
        stderr_lines: list[str] = []
        try:
            options = await build_options(session, stderr_collector=stderr_lines)
            client = ClaudeSDKClient(options=options)
            ACTIVE_CLIENTS[session.id] = client
            response_text: list[str] = []
            fallback_context: dict[str, Any] | None = None
            retry_attempt = 0
            try:
                await client.connect()
            except Exception as connect_exc:
                conn_err_text = f"{connect_exc}\n" + "\n".join(stderr_lines)
                if options.resume and ("No conversation found" in conn_err_text or "session ID" in conn_err_text):
                    logger.warning("Session %s resume_id %s invalid, retrying as a fresh session...", session.id, options.resume)
                    async with SessionLocal() as db:
                        db_session = await db.get(AgentSession, session.id)
                        if db_session:
                            db_session.sdk_session_id = None
                            await db.commit()
                    stderr_lines.clear()
                    options = await build_options(session, stderr_collector=stderr_lines, use_resume=False)
                    client = ClaudeSDKClient(options=options)
                    ACTIVE_CLIENTS[session.id] = client
                    await client.connect()
                else:
                    raise

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
                # Thinking deltas arrive back-to-back and carry no replay value;
                # persisting each one cost ~186s of serialized DB writes per run.
                if event_type != "thinking_tokens":
                    await persist_event(session.id, "assistant", str(event_type), visible_event)
                await send_session_event(session.id, {"type": "sdk_event", "event": visible_event})
                if event_type == "api_retry":
                    retry_data = event.get("data") or {}
                    retry_attempt = int(retry_data.get("attempt") or retry_attempt + 1)
                    await send_session_event(session.id, {
                        "type": "api_retry",
                        "attempt": retry_attempt,
                        "max_retries": MAX_API_RETRIES,
                        "reason": retry_data.get("error_status") or retry_data.get("error") or "unknown",
                    })
                    if retry_attempt >= MAX_API_RETRIES:
                        raise RuntimeError(
                            f"上游模型接口连续 {MAX_API_RETRIES} 次调用失败，已停止重试。"
                            "请前往「系统设置 - LLM 配置」检查 Base URL、模型和超时时间，然后重新发送任务。"
                        )
            joined = "\n".join(response_text)
            deviation = _marker_payload(joined, "QUANTLAB_SPEC_DEVIATION")
            if deviation is not None:
                await persist_event(session.id, "assistant", "spec_deviation", deviation)
            backtest_payload = _marker_payload(joined, "QUANTLAB_BACKTEST_REQUEST")
            if backtest_requested and backtest_payload is not None:
                try:
                    request = BacktestCreate.model_validate(await _resolve_backtest_version(backtest_payload, session))
                    async with SessionLocal() as db:
                        run = await create_backtest_run(request, db)
                    await send_session_event(session.id, {"type": "backtest_created", "run_id": run.id, "status": run.status.value, "name": run.name})
                except Exception:
                    await send_session_event(session.id, {"type": "backtest_error", "message": "策略修改已完成，但回测参数不符合平台要求，请补充或修正参数后重试。"})
            changes = await asyncio.to_thread(_diff_summary, session)
            if changes["files"]:
                await send_session_event(session.id, {"type": "changes_ready", **changes})
            
            # If Claude did not output trailing summary text after tool use, append a completion notification
            has_summary_text = any(bool(t.strip()) for t in response_text)
            if not has_summary_text:
                completion_msg = f"策略 `{session.strategy_name}` 代码已生成并同步！您可以在代码区预览，或直接进入回测阶段进行验证。"
                await persist_event(session.id, "assistant", "AssistantMessage", {
                    "message_type": "AssistantMessage",
                    "content": [{"text": completion_msg}]
                })
                await send_session_event(session.id, {
                    "type": "sdk_event",
                    "event": {
                        "message_type": "AssistantMessage",
                        "content": [{"text": completion_msg}]
                    }
                })

            # Auto-save and sync generated strategy code from worktree to canonical & persistent storage
            worktree_file = Path(session.workspace_path) / STRATEGY_RELATIVE / f"{session.strategy_name}.py"
            if worktree_file.exists():
                try:
                    code_content = worktree_file.read_text(encoding="utf-8")
                    if code_content.strip():
                        from ..strategy_files import save_strategy_code
                        save_strategy_code(session.strategy_name, code_content)
                        baseline = _agent_root() / "baselines" / session.id / f"{session.strategy_name}.py"
                        baseline.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(worktree_file, baseline)
                        logger.info("已自动保存会话 %s 策略代码到持久化存储", session.id)
                except Exception as err:
                    logger.warning("自动保存策略代码失败: %s", err)

            if session.research_project_id:
                async with SessionLocal() as db:
                    project = await db.get(ResearchProject, session.research_project_id)
                    if project and project.status in {ResearchStatus.IMPLEMENTING, ResearchStatus.DISCUSSING, ResearchStatus.SPEC_REVIEW}:
                        strat_file = _canonical_strategy_file(session.strategy_name)
                        if strat_file.exists() or worktree_file.exists():
                            project.status = ResearchStatus.CODE_REVIEW
                            await db.commit()

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
            logger.exception("Agent execution failed for session %s", session.id)
            formatted_error = format_sdk_error(exc, stderr_lines)
            await update_status(session.id, AgentSessionStatus.FAILED, formatted_error[:2000])
            await send_session_event(session.id, {"type": "error", "message": formatted_error})
        finally:
            ACTIVE_CLIENTS.pop(session.id, None)
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass


@router.post("/sessions")
async def create_session(data: AgentSessionCreate, db: AsyncSession = Depends(get_db)):
    await get_config(db)
    canonical = _canonical_strategy_file(data.strategy_name)
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
async def list_sessions(client_id: str | None = None, strategy_name: str | None = None, db: AsyncSession = Depends(get_db)):
    query_stmt = select(AgentSession)
    if client_id:
        query_stmt = query_stmt.where(AgentSession.client_id == client_id)
    if strategy_name:
        query_stmt = query_stmt.where(AgentSession.strategy_name == strategy_name)
    rows = (await db.scalars(query_stmt.order_by(AgentSession.updated_at.desc()))).all()
    return [session_out(row) for row in rows]


@router.get("/sessions/{session_id}/messages")
async def list_messages(session_id: str, db: AsyncSession = Depends(get_db)):
    rows = (await db.scalars(select(AgentMessage).where(AgentMessage.session_id == session_id).order_by(AgentMessage.created_at))).all()
    return [{"id": row.id, "role": row.role, "event_type": row.event_type, "content": row.content, "created_at": row.created_at} for row in rows]


async def cancel_active_session(session_id: str) -> None:
    client = ACTIVE_CLIENTS.get(session_id)
    if client:
        try:
            await client.interrupt()
        except Exception:
            pass
    task = ACTIVE_TASKS.get(session_id)
    if task and not task.done():
        task.cancel()
    async with SessionLocal() as db:
        session = await db.get(AgentSession, session_id)
        if session and session.status in {AgentSessionStatus.RUNNING, AgentSessionStatus.QUEUED}:
            session.status = AgentSessionStatus.CANCELED
            await db.commit()


@router.post("/sessions/{session_id}/cancel")
async def cancel_session(session_id: str, db: AsyncSession = Depends(get_db)):
    session = await db.get(AgentSession, session_id)
    if not session:
        raise HTTPException(404, "Agent 会话不存在")
    await cancel_active_session(session_id)
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
    destination = _canonical_strategy_file(session.strategy_name)
    try:
        code = source.read_text(encoding="utf-8")
        compile(code, destination.name, "exec")
    except (OSError, SyntaxError) as exc:
        raise HTTPException(422, f"策略验证失败：{exc}") from exc
    from ..strategy_files import save_strategy_code
    save_strategy_code(session.strategy_name, code)
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
    from ..auth import verify_token
    token = websocket.query_params.get("token")
    if not token or not verify_token(token):
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "未登录或登录凭据已过期"})
        await websocket.close(code=4401)
        return

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
