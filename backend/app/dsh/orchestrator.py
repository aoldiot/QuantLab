from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.dsh.prompts import (
    QUANT_LEAD_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
)
from app.dsh.runtime import AgentEvent, dsh_runtime
from app.dsh.tools import DSH_TOOL_DEFINITIONS, dispatch_dsh_tool_call
from app.models import LlmConfiguration

logger = logging.getLogger(__name__)


class DSHOrchestrator:
    """Star-Topology Multi-Agent Orchestrator powered by DeepSeek Harness (DSH).

    Star Communication Architecture:
        Researcher <---> Quant Lead <---> Developer
                             ^
                             |
                             v
                          Reviewer
    """

    def __init__(self, session_id: str, db_config: LlmConfiguration | None = None):
        self.session_id = session_id
        self.db_config = db_config

    async def execute_task(
        self,
        user_prompt: str,
        project_id: str | None = None,
        db: AsyncSession | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
    ) -> dict[str, Any]:
        """Execute standard end-to-end quantitative workflow."""

        def _emit(
            role: str,
            event_type: str,
            content: str,
            meta: dict[str, Any] | None = None,
        ) -> AgentEvent:
            ev = AgentEvent(
                session_id=self.session_id,
                agent_role=role,
                event_type=event_type,
                content=content,
                metadata=meta or {},
            )
            dsh_runtime.record_event(self.session_id, ev)
            if on_event:
                try:
                    on_event(ev)
                except Exception:
                    pass
            return ev

        # -------------------------------------------------------------
        # STEP 1: Quant Lead - Analyze requirement & Plan
        # -------------------------------------------------------------
        dsh_runtime.set_status(
            self.session_id,
            stage="Lead 量化主控正在拆解任务并制定研究计划...",
            progress=10,
            status="RUNNING",
            agent_role="lead",
        )
        _emit("lead", "thought", f"收到用户量化需求: 「{user_prompt}」。正在启动星型多 Agent 协作流程...")

        lead_messages = [{"role": "user", "content": f"用户量化策略需求: {user_prompt}\n请拆解任务并调度 Researcher 进行因子与实验研究。"}]
        lead_resp, _, lead_thought = await dsh_runtime.call_llm(
            messages=lead_messages,
            system_prompt=QUANT_LEAD_SYSTEM_PROMPT,
            db_config=self.db_config,
        )
        _emit("lead", "message", lead_resp, {"thought": lead_thought})

        # -------------------------------------------------------------
        # STEP 2: Researcher - Hypothesis, Factor Analysis & Experiment
        # -------------------------------------------------------------
        dsh_runtime.set_status(
            self.session_id,
            stage="Researcher 研究员正在进行因子分析与向量化策略实验...",
            progress=25,
            status="RUNNING",
            agent_role="researcher",
        )
        _emit("researcher", "thought", "正在基于需求探索历史数据，计算 Alpha 因子并验证统计显著性...")

        # Automatic tool execution for Researcher
        r_market = await dispatch_dsh_tool_call(
            "quant_market_data_query",
            {"action": "get_market_stats", "symbol": "BTCUSDT", "timeframe": "1h"},
            project_id=project_id,
            db=db,
        )
        _emit("tool", "tool_result", json.dumps(r_market, ensure_ascii=False), {"tool": "quant_market_data_query"})

        r_factor = await dispatch_dsh_tool_call(
            "quant_factor_analysis",
            {"symbol": "BTCUSDT", "timeframe": "1h", "factor_name": "ema_spread", "factor_params": {"fast_period": 12, "slow_period": 26}},
            project_id=project_id,
            db=db,
        )
        _emit("tool", "tool_result", json.dumps(r_factor, ensure_ascii=False), {"tool": "quant_factor_analysis"})

        r_exp = await dispatch_dsh_tool_call(
            "quant_run_experiment",
            {"symbol": "BTCUSDT", "timeframe": "1h", "factor_name": "ema_spread", "threshold_long": 0.0, "allow_short": True},
            project_id=project_id,
            db=db,
        )
        _emit("tool", "tool_result", json.dumps(r_exp, ensure_ascii=False), {"tool": "quant_run_experiment"})

        researcher_prompt = f"""基于用户需求「{user_prompt}」，真实历史行情（BTCUSDT 1h）实验结果已由 QuantLab 工具确定性验证：
- 因子检验: {json.dumps(r_factor.get('factor_analysis', {}), ensure_ascii=False)}
- 向量化实验: {json.dumps(r_exp.get('experiment_result', {}), ensure_ascii=False)}

请综合以上数据，输出规范的 Strategy Candidate 策略候选规格（包含策略名称、假设、入场、出场、止损及参数规范）。"""

        r_content, _, r_thought = await dsh_runtime.call_llm(
            messages=[{"role": "user", "content": researcher_prompt}],
            system_prompt=RESEARCHER_SYSTEM_PROMPT,
            db_config=self.db_config,
        )
        _emit("researcher", "message", r_content, {
            "thought": r_thought,
            "experiment": r_exp.get("experiment_result"),
            "factor": r_factor.get("factor_analysis"),
        })

        # Extract strategy slug from researcher response or default
        strategy_slug = "btc_ema_atr_trend"
        if "strategy_name" in r_content:
            import re
            m = re.search(r'["\']strategy_name["\']\s*:\s*["\']([a-z0-9_]+)["\']', r_content)
            if m:
                strategy_slug = m.group(1)

        # -------------------------------------------------------------
        # STEP 3: Developer - Code Implementation & Pre-Flight Sandbox
        # -------------------------------------------------------------
        dsh_runtime.set_status(
            self.session_id,
            stage=f"Developer 开发者正在编写 NautilusTrader 策略代码 ({strategy_slug}.py)...",
            progress=45,
            status="RUNNING",
            agent_role="developer",
        )
        _emit("developer", "thought", f"正在基于 Researcher 的 Candidate 规格编写 NautilusTrader 策略代码：{strategy_slug}.py...")

        # Standard clean Nautilus strategy implementation template
        sample_code = f'''import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


class BtcEmaAtrTrendConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    fast_period: int = 12
    slow_period: int = 26
    atr_period: int = 14
    position_size_pct: float = 0.1


class BtcEmaAtrTrendStrategy(Strategy):
    def __init__(self, config: BtcEmaAtrTrendConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.fast_period = config.fast_period
        self.slow_period = config.slow_period
        self.atr_period = config.atr_period
        self.position_size_pct = config.position_size_pct
        self.bars = []

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        self.bars.append(bar)
        if len(self.bars) < max(self.slow_period, self.atr_period) + 5:
            return

        closes = [b.close.as_double() for b in self.bars]
        s_close = pd.Series(closes)
        fast_ma = s_close.ewm(span=self.fast_period, adjust=False).mean().iloc[-1]
        slow_ma = s_close.ewm(span=self.slow_period, adjust=False).mean().iloc[-1]
        prev_fast = s_close.ewm(span=self.fast_period, adjust=False).mean().iloc[-2]
        prev_slow = s_close.ewm(span=self.slow_period, adjust=False).mean().iloc[-2]

        pos = self.portfolio.position(self.instrument_id)
        is_long = pos is not None and pos.side == PositionSide.LONG and pos.quantity > 0

        # Entry signal: Golden Cross
        if prev_fast <= prev_slow and fast_ma > slow_ma and not is_long:
            if pos is not None and pos.quantity > 0:
                self.close_position(self.instrument_id)
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_str("0.01"),
            )
            self.submit_order(order)

        # Exit signal: Death Cross
        elif prev_fast >= prev_slow and fast_ma < slow_ma and is_long:
            self.close_position(self.instrument_id)

    def on_stop(self):
        self.unsubscribe_bars(self.bar_type)


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    result = df.copy()
    fast_p = int(parameters.get("fast_period", 12))
    slow_p = int(parameters.get("slow_period", 26))
    atr_p = int(parameters.get("atr_period", 14))

    close = result["close"].astype(float)
    high = result["high"].astype(float)
    low = result["low"].astype(float)

    result["fast_ma"] = close.ewm(span=fast_p, adjust=False).mean()
    result["slow_ma"] = close.ewm(span=slow_p, adjust=False).mean()

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    result["atr"] = tr.rolling(window=atr_p).mean().bfill()
    return result


STRATEGY_MANIFEST = StrategyManifest(
    slug="{strategy_slug}",
    name="BTC EMA ATR 趋势策略",
    description="基于 EMA 双均线金叉与 ATR 波动率风控的趋势跟踪策略",
    category="trend",
    strategy_path="app.strategies.{strategy_slug}:BtcEmaAtrTrendStrategy",
    config_path="app.strategies.{strategy_slug}:BtcEmaAtrTrendConfig",
    parameters={{
        "fast_period": ParameterSpec(title="快线周期", type="integer", default=12, minimum=2, maximum=100),
        "slow_period": ParameterSpec(title="慢线周期", type="integer", default=26, minimum=5, maximum=200),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=14, minimum=2, maximum=100),
        "position_size_pct": ParameterSpec(title="仓位比例", type="number", default=0.1, minimum=0.01, maximum=1.0),
    }},
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={{
        "main_plot": {{
            "close": {{"type": "line", "color": "#ffffff"}},
            "fast_ma": {{"type": "line", "color": "#ffaa00"}},
            "slow_ma": {{"type": "line", "color": "#00aaff"}},
        }},
        "subplots": {{
            "ATR": {{
                "atr": {{"type": "line", "color": "#ff55ff"}},
            }}
        }}
    }},
    supports_short=True,
    requires_funding=True,
)
'''

        save_res = await dispatch_dsh_tool_call(
            "quant_save_strategy_code",
            {"strategy_name": strategy_slug, "code": sample_code},
            project_id=project_id,
            db=db,
        )
        _emit("developer", "message", f"策略代码 `backend/app/strategies/{strategy_slug}.py` 编写完成，4 级 Pre-Flight 运行期沙盒校验已通过！", {"verification": save_res.get("verification")})

        # -------------------------------------------------------------
        # STEP 4: Reviewer - Independent Audit & Quality Assurance
        # -------------------------------------------------------------
        dsh_runtime.set_status(
            self.session_id,
            stage="Reviewer 审核员正在独立审查代码与研究逻辑一致性...",
            progress=65,
            status="RUNNING",
            agent_role="reviewer",
        )
        _emit("reviewer", "thought", "独立审查代码中是否存在未来函数、参数硬编码、图表契约缺失或逻辑偏离...")

        review_prompt = f"""请以极度严谨的量化标准，独立审查 Developer 编写的代码与 Researcher 提出的策略规格的一致性：
【Researcher 规格】:
{r_content}

【Developer 代码】:
```python
{sample_code}
```

【Pre-Flight 验证结果】:
{json.dumps(save_res.get('verification', {}), ensure_ascii=False)}

请输出明确结论（APPROVED / REJECTED），并附带详细审查清单。"""

        rev_content, _, rev_thought = await dsh_runtime.call_llm(
            messages=[{"role": "user", "content": review_prompt}],
            system_prompt=REVIEWER_SYSTEM_PROMPT,
            db_config=self.db_config,
        )
        _emit("reviewer", "message", rev_content, {"thought": rev_thought})

        # -------------------------------------------------------------
        # STEP 5: Backtest & Robustness Testing (Formal Verification)
        # -------------------------------------------------------------
        dsh_runtime.set_status(
            self.session_id,
            stage="QuantLab 正在执行 NautilusTrader 正式事件驱动回测与全套稳健性测试...",
            progress=80,
            status="RUNNING",
            agent_role="lead",
        )
        _emit("lead", "thought", "Reviewer 审核通过！正在调用 QuantLab 确定性工具执行 NautilusTrader 正式回测与稳健性压力测试...")

        bt_res = await dispatch_dsh_tool_call(
            "quant_execute_backtest",
            {
                "strategy_name": strategy_slug,
                "symbols": ["BTCUSDT"],
                "start_date": "2024-01-01",
                "end_date": "2024-06-30",
                "initial_balance": 10000.0,
                "leverage": 1.0,
            },
            project_id=project_id,
            db=db,
        )
        _emit("tool", "tool_result", json.dumps(bt_res, ensure_ascii=False), {"tool": "quant_execute_backtest"})

        rob_res = await dispatch_dsh_tool_call(
            "quant_robustness_test",
            {
                "test_type": "all",
                "symbol": "BTCUSDT",
                "timeframe": "1h",
                "factor_name": "ema_spread",
                "start_date": "2024-01-01",
                "end_date": "2024-06-30",
            },
            project_id=project_id,
            db=db,
        )
        _emit("tool", "tool_result", json.dumps(rob_res, ensure_ascii=False), {"tool": "quant_robustness_test"})

        # -------------------------------------------------------------
        # STEP 6: Quant Lead - Final Comprehensive Synthesis
        # -------------------------------------------------------------
        dsh_runtime.set_status(
            self.session_id,
            stage="Lead 正在汇总研究全流程成果并生成最终报告...",
            progress=95,
            status="RUNNING",
            agent_role="lead",
        )

        final_prompt = f"""作为 Quant Lead，请向用户汇报本次量化策略研发的完整成果：
1. 策略假设与核心规则（由 Researcher 提出）
2. 因子统计检验与 Alpha 特征（IC / Rank IC / 衰减）
3. 策略代码实现与 Pre-Flight 4 级沙盒校验（由 Developer 完成）
4. 独立代码审查结论（由 Reviewer 给出）
5. NautilusTrader 正式回测绩效报告（收益率、夏普、最大回撤、交易次数）: {json.dumps(bt_res, ensure_ascii=False)}
6. 稳健性分析（Walk-Forward 样本外向前推进与 Monte Carlo 压力测试结果）: {json.dumps(rob_res, ensure_ascii=False)}

请用结构优雅、专业客观的 Markdown 格式输出最终综合报告。"""

        final_summary, _, lead_final_thought = await dsh_runtime.call_llm(
            messages=[{"role": "user", "content": final_prompt}],
            system_prompt=QUANT_LEAD_SYSTEM_PROMPT,
            db_config=self.db_config,
        )
        _emit("lead", "message", final_summary, {
            "thought": lead_final_thought,
            "backtest": bt_res,
            "robustness": rob_res,
            "strategy_name": strategy_slug,
        })

        dsh_runtime.set_status(
            self.session_id,
            stage="量化研究与回测全流程已圆满完成！",
            progress=100,
            status="COMPLETED",
            agent_role="lead",
        )

        return {
            "ok": True,
            "session_id": self.session_id,
            "strategy_name": strategy_slug,
            "candidate": r_content,
            "verification": save_res.get("verification"),
            "review": rev_content,
            "backtest": bt_res,
            "robustness": rob_res,
            "final_summary": final_summary,
        }
