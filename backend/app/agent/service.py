from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..backtest_service import create_backtest_run
from ..config import settings
from ..db import SessionLocal, get_db
from ..llm_config import MAX_API_RETRIES, get_config
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
from .strategy_verifier import verify_strategy_file

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["strategy-agent"])
STRATEGY_RELATIVE = Path("backend/app/strategies")
ALLOWED_MODES = {"plan", "default", "acceptEdits", "bypassPermissions"}
GLOBAL_SEMAPHORE = asyncio.Semaphore(settings.agent_max_concurrency)
CLIENT_LOCKS: dict[str, asyncio.Lock] = {}
STRATEGY_LOCKS: dict[str, asyncio.Lock] = {}
ACTIVE_TASKS: dict[str, asyncio.Task[None]] = {}
ACTIVE_WEBSOCKETS: dict[str, WebSocket] = {}
APPROVALS: dict[str, asyncio.Future[bool]] = {}

NAUTILUS_STRATEGY_CHEATSHEET = """
【NautilusTrader 策略开发核心速查表与规范】
1. 策略命名与文件规范（严禁使用简陋的 Strategy/Custom/CustomStrategy）：
- 必须根据策略的核心量化逻辑、指标、标的与交易模式命名为具体的蛇形英文标识符 (slug)，例如：
  - `volatility_squeeze_breakout`（波动率挤压突破策略）
  - `btc_ema_atr_trend`（BTC EMA ATR 趋势策略）
  - `eth_rsi_mean_reversion`（ETH RSI 均值回归策略）
  - `bollinger_momentum_breakout`（布林带动量突破策略）
- 严禁直接使用空泛的 "Strategy", "MyStrategy", "CustomStrategy", "TradingStrategy"！
- 策略配置类与策略类必须采用 PascalCase 风格且与标识符对应：例如 `VolatilitySqueezeBreakoutConfig` 与 `VolatilitySqueezeBreakoutStrategy`。

2. 依赖与模块导入规范：
```python
from decimal import Decimal
import pandas as pd
import numpy as np

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode
```

3. 核心四大导出结构规范（严禁遗漏任何一项）：
- 结构 1：`StrategyConfig` 子类（继承自 `StrategyConfig, frozen=True`）：
  ```python
  class XxxConfig(StrategyConfig, frozen=True):
      instrument_id: InstrumentId
      bar_type: BarType
      fast_period: int = 12
      slow_period: int = 26
      atr_period: int = 14
      trade_size: Decimal = Decimal("0.01")
  ```
- 结构 2：`Strategy` 子类（继承自 `Strategy`）：
  ```python
  class XxxStrategy(Strategy):
      def __init__(self, config: XxxConfig) -> None:
          super().__init__(config)
          self.instrument_id = config.instrument_id
          self.bar_type = config.bar_type
          self.fast_period = config.fast_period
          self.slow_period = config.slow_period
          self.trade_size = Quantity.from_str(str(config.trade_size)) if isinstance(config.trade_size, (Decimal, float, str)) else config.trade_size
          self.bars: list[Bar] = []

      def on_start(self) -> None:
          self.instrument = self.cache.instrument(self.instrument_id)
          self.subscribe_bars(self.bar_type)

      def on_bar(self, bar: Bar) -> None:
          self.bars.append(bar)
          if len(self.bars) < self.slow_period + 5:
              return

          closes = pd.Series([b.close.as_double() for b in self.bars])
          fast_ma = closes.ewm(span=self.fast_period, adjust=False).mean().iloc[-1]
          slow_ma = closes.ewm(span=self.slow_period, adjust=False).mean().iloc[-1]
          prev_fast = closes.ewm(span=self.fast_period, adjust=False).mean().iloc[-2]
          prev_slow = closes.ewm(span=self.slow_period, adjust=False).mean().iloc[-2]

          is_long = self.portfolio.is_net_long(self.instrument_id)
          is_flat = self.portfolio.is_net_flat(self.instrument_id)

          if prev_fast <= prev_slow and fast_ma > slow_ma and not is_long:
              if not is_flat:
                  self.close_all_positions(self.instrument_id)
              order = self.order_factory.market(
                  instrument_id=self.instrument_id,
                  order_side=OrderSide.BUY,
                  quantity=self.trade_size,
              )
              self.submit_order(order)
          elif prev_fast >= prev_slow and fast_ma < slow_ma and is_long:
              self.close_all_positions(self.instrument_id)

      def on_stop(self) -> None:
          self.unsubscribe_bars(self.bar_type)
  ```
- 结构 3：`calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame`：
  - 必须返回行数完全相同的 DataFrame（严禁 dropna）。
  - **CRITICAL：必须计算并在返回的 DataFrame 中包含 `plot_config` 中声明的所有指标列！**
  - 对 rolling/ewm 计算产生的头部 NaN，必须使用 `.bfill()` 或 `.fillna(0.0)` 填充，保证预热后无 NaN。
- 结构 4：`STRATEGY_MANIFEST = StrategyManifest(...)`：
  - `strategy_path="app.strategies.{slug}:XxxStrategy"`（必须带 `app.strategies.{slug}:` 前缀）
  - `config_path="app.strategies.{slug}:XxxConfig"`（必须带 `app.strategies.{slug}:` 前缀）
  - `parameters`: 参数字典，每个参数必须为 `ParameterSpec(title="中文名", type="integer"|"number"|"boolean", default=..., minimum=..., maximum=...)`，且必须满足 `minimum <= default <= maximum`。
  - `timeframes=("15m", "1h", "4h", "1d")`, `primary_timeframe="1h"`（`primary_timeframe` 必须包含在 `timeframes` 中）。
  - `plot_config` 必须是双层嵌套字典规范。
"""


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


def _diff_summary(session: AgentSession) -> dict[str, Any]:
    baseline_file = _agent_root() / "baselines" / session.id / f"{session.strategy_name}.py"
    current_file = Path(session.workspace_path) / STRATEGY_RELATIVE / f"{session.strategy_name}.py"
    if not current_file.exists():
        return {"files": [], "additions": 0, "deletions": 0, "diff": ""}
    old_lines = baseline_file.read_text(encoding="utf-8").splitlines(keepends=True) if baseline_file.exists() else []
    new_lines = current_file.read_text(encoding="utf-8").splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{session.strategy_name}.py", tofile=f"b/{session.strategy_name}.py"))
    additions = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
    if not additions and not deletions:
        return {"files": [], "additions": 0, "deletions": 0, "diff": ""}
    return {
        "files": [{"path": f"backend/app/strategies/{session.strategy_name}.py", "additions": additions, "deletions": deletions}],
        "additions": additions,
        "deletions": deletions,
        "diff": "".join(diff),
    }


async def send_session_event(session_id: str, event: dict[str, Any]) -> None:
    ws = ACTIVE_WEBSOCKETS.get(session_id)
    if ws:
        try:
            await ws.send_json(event)
        except Exception:
            pass


async def update_status(session_id: str, status: AgentSessionStatus) -> None:
    async with SessionLocal() as db:
        session = await db.get(AgentSession, session_id)
        if session:
            session.status = status
            await db.commit()


async def persist_event(session_id: str, role: str, event_type: str, content: dict[str, Any]) -> None:
    async with SessionLocal() as db:
        msg = AgentMessage(session_id=session_id, role=role, event_type=event_type, content=content)
        db.add(msg)
        await db.commit()


def session_out(session: AgentSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "client_id": session.client_id,
        "strategy_name": session.strategy_name,
        "permission_mode": session.permission_mode,
        "status": session.status.value,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


async def run_prompt(session: AgentSession, prompt: str) -> None:
    original_prompt = prompt
    client_lock = CLIENT_LOCKS.setdefault(session.client_id, asyncio.Lock())
    strategy_lock = STRATEGY_LOCKS.setdefault(session.strategy_name, asyncio.Lock())
    execution_lock = strategy_lock if session.permission_mode != "plan" else asyncio.Lock()

    await send_session_event(session.id, {"type": "queued"})
    async with GLOBAL_SEMAPHORE, client_lock, execution_lock:
        await update_status(session.id, AgentSessionStatus.RUNNING)
        await persist_event(session.id, "user", "message", {"text": original_prompt})
        await send_session_event(session.id, {"type": "status", "status": "RUNNING"})

        try:
            async with SessionLocal() as db:
                cfg = await get_config(db)

            from app.dsh.runtime import dsh_runtime

            target_file = Path(session.workspace_path) / STRATEGY_RELATIVE / f"{session.strategy_name}.py"
            target_file.parent.mkdir(parents=True, exist_ok=True)
            existing_code = target_file.read_text(encoding="utf-8") if target_file.exists() else ""

            system_prompt = f"""你是 QuantLab 策略开发专家。
你正在开发策略文件：backend/app/strategies/{session.strategy_name}.py。
请严格遵循 NautilusTrader 规范与 4 级 Pre-Flight 运行期沙盒校验要求。

{NAUTILUS_STRATEGY_CHEATSHEET}
"""

            user_msg = f"""【开发任务】
{original_prompt}

【现有策略代码】
```python
{existing_code[:12000]}
```

请根据任务直接输出修改后的完整 Python 策略代码（包含 StrategyConfig、Strategy、calculate_indicators 与 STRATEGY_MANIFEST），并简要说明改动要点。"""

            content, tool_calls, reasoning = await dsh_runtime.call_llm(
                messages=[{"role": "user", "content": user_msg}],
                system_prompt=system_prompt,
                db_config=cfg,
            )

            # Extract python code
            code_match = re.search(r"```(?:python)?\s*([\s\S]*?)\s*```", content)
            if code_match:
                extracted_code = code_match.group(1).strip()
                summary_text = re.sub(r"```(?:python)?\s*[\s\S]*?\s*```", "", content).strip()
            elif "class " in content and "Strategy" in content:
                extracted_code = content.strip()
                summary_text = "策略代码已生成并保存。"
            else:
                extracted_code = ""
                summary_text = content.strip()

            if extracted_code and session.permission_mode != "plan":
                target_file.write_text(extracted_code, encoding="utf-8")
                v_res = verify_strategy_file(target_file, strategy_name=session.strategy_name)
                verification_note = f"\n\n✓ 4 级 Pre-Flight 校验全部通过！" if v_res.ok else f"\n\n⚠ 沙盒校验提示 [{v_res.failed_level}]: {v_res.error_message}"
                summary_text += verification_note

            if not summary_text:
                summary_text = f"策略 `{session.strategy_name}` 代码已更新，请查看修改 Diff。"

            assistant_payload = {
                "message_type": "AssistantMessage",
                "content": [{"text": summary_text}],
            }
            await persist_event(session.id, "assistant", "AssistantMessage", assistant_payload)
            await send_session_event(session.id, {"type": "sdk_event", "event": assistant_payload})

            changes = await asyncio.to_thread(_diff_summary, session)
            if changes["files"]:
                await send_session_event(session.id, {"type": "changes_ready", **changes})

            await update_status(session.id, AgentSessionStatus.IDLE)
            await send_session_event(session.id, {"type": "status", "status": "IDLE"})

        except Exception as exc:
            logger.exception("Agent 会话执行失败: %s", exc)
            await update_status(session.id, AgentSessionStatus.FAILED)
            await send_session_event(session.id, {"type": "error", "message": f"Agent 执行异常：{exc}"})


@router.post("/sessions")
async def create_session(data: AgentSessionCreate, db: AsyncSession = Depends(get_db)):
    session_id = str(uuid.uuid4())
    workspace = create_worktree(session_id, data.strategy_name)
    session = AgentSession(
        id=session_id,
        client_id=data.client_id,
        strategy_name=data.strategy_name,
        permission_mode=data.permission_mode,
        workspace_path=str(workspace),
    )
    db.add(session)
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
                await cancel_active_session(session.id)
            elif kind == "compact":
                current_task = ACTIVE_TASKS.get(session.id)
                if current_task is None or current_task.done():
                    current_task = asyncio.create_task(run_prompt(session, "请精简总结保留策略需求、已完成修改与验证结果"))
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
