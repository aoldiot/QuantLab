from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent.tools import (
    TOOL_DEFINITIONS,
    dispatch_tool_call,
    ensure_strategy_db_record,
    get_strategy_code_tool,
    get_writing_log_tool,
    write_strategy_code,
    write_strategy_with_claude,
)
from .config import settings
from .db import get_db, SessionLocal
from .models import (
    BacktestRun,
    ResearchMessage,
    ResearchProject,
    ResearchStatus,
    Strategy,
    StrategyVersion,
)
from .schemas import (
    ResearchMessageCreate,
    ResearchProjectCreate,
    ResearchWriteStrategyRequest,
)
from .strategy_files import _path, save_strategy_code


router = APIRouter(prefix="/api/research", tags=["strategy-research"])
logger = logging.getLogger(__name__)

RESEARCH_INSTRUCTIONS = """你是 QuantLab 的首席量化负责人 (Quant Lead)，由 DeepSeek Harness (DSH) 驱动。
你的职责是与用户全流程完成量化策略的研讨、设计、开发、回测与归因分析。使用简体中文。

【角色与原则】
1. 核心定位：你是全流程量化总主控 (Quant Lead powered by DeepSeek Harness)。你负责策略假设研讨、规则设计、调用 QuantLab 确定性工具、调度策略编写、回测执行与指标归因分析。
2. 策略研讨与详尽方案输出（CRITICAL - 必须先输出完整的 Markdown 策略设计方案）：
   - 当用户提出策略设想、讨论量化思路或要求开发新策略时，你**必须首先在回复中以 Markdown 格式输出结构完整、详尽专业的《量化策略设计方案》**。
   - 方案必须包含以下完整模块：
     1. 策略核心逻辑与量化假设（解释市场异象、收益来源与逻辑闭环）；
     2. 适用标的与推荐周期（如 BTCUSDT 15m / 1h）；
     3. 向量化指标计算与数学公式（明确指标定义与算法公式）；
     4. 入场触发条件与多空信号判定（明确金叉/死叉/突破/过滤器等具体规则）；
     5. 出场机制与动态止盈止损（如 ATR 跟踪止损、固定比例止损、反向信号平仓）；
     6. 资金管理与单笔仓位控制（如固定比例、波动率逆向加权）；
     7. 策略参数规格清单（清晰 Markdown 表格列出参数名、类型、默认值、范围与说明）；
     8. 潜在风险、防过拟合与稳健性考量（交易成本敏感度、震荡市磨损、流动性等）。
   - **【安全红线 - 严禁在未输出详尽策略方案前直接跳过方案直接发起审批】**：
     严禁直接输出空洞简短的一两句话就要求用户批准！必须先在文本回复中向用户完整输出上述包含详细逻辑、指标公式、入场出场规则、风控和参数表格的策略设计方案！

3. 编码审批机制与代码生成（CRITICAL - 审批通过后由 DSH 调用 Pre-Flight 运行期沙盒编写策略）：
   - 【严禁擅自直接写码】：当策略逻辑设计方案在正文中完整输出后，在方案末尾附上 ```code_approval 机器块（或调用工具 `propose_code_approval`）向用户发起编码审批请求，必须在参数中完整传入 `strategy_name`（建议的小写英文下划线策略名）、`strategy_summary` 与 `key_rules`，严禁传递空参数 `{}`。
   - 【用户批准后编写代码】：只有当用户在界面中点击「批准并开始编写代码」、或在对话中明确回复“同意”、“批准”、“开始编写代码”后，你才可以开始进行策略代码编写与沙盒自愈。
   - 【由 DSH 驱动 QuantLab 策略生成并完成 4 级沙盒自愈（严禁私自手写未经验证代码）】：
     用户在前端点击「批准并开始编写代码」或表达同意即代表已授予完全的代码写入权限，系统界面不存在二级的“批准写入”按钮！
     【极其关键】：编写策略文件必须通过 QuantLab 的 Strategy Manager 与沙盒验证机制执行。
     必须使用专用工具 `write_strategy_with_claude`（必须完整传入 `strategy_name` 与 `instructions`/`specification`，严禁传空字典 `{}`），或通过 `quant_save_strategy_code` 生成策略，并自动运行 4 级 Pre-Flight 运行期沙盒检测与自愈。
     严禁在回复中要求用户点击不存在的“批准写入按钮”或等待二次写入授权！
   - 【Pre-Flight 4 级全自动验证沙盒保证】：
     代码生成后系统会自动执行 4 级沙盒检测（L1 静态语法 -> L2 契约与类加载 -> L3 200根Bar指标计算覆盖与NaN检测 -> L4 Nautilus 运行时实例化与生命周期钩子）。若检测到错误会自动在会话内自愈修复，确保交付的代码直接可运行！

   - 【四大核心导出结构与代码规范（策略代码必须严格涵盖）】：
     编写的代码必须包含且仅包含以下四个标准导出结构，严禁遗漏：
     1. `StrategyConfig` 子类（继承自 `nautilus_trader.config.StrategyConfig`）：
        - 必须包含 `instrument_id: str` 和 `bar_type: str`。
        - 声明策略所需的全部参数，类型推荐使用 `int`, `float`, `str`, `bool`（字段名必须与 `STRATEGY_MANIFEST.parameters` 的 key 严格一致）。
     2. `Strategy` 子类（继承自 `nautilus_trader.trading.strategy.Strategy`）：
        - `__init__(self, config)`：保存属性，初始化状态。
        - `on_start(self)`：获取 `self.instrument = self.cache.instrument(self.instrument_id)` 并订阅行情 `self.subscribe_bars(self.bar_type)`。
        - `on_bar(self, bar: Bar)`：实现入场、出场、止损止盈与持仓管理。通过 `self.order_factory.market(...)` 构建市价单，并通过 `self.submit_order(order)` 下单。
        - `on_stop(self)`：取消订阅 `self.unsubscribe_bars(self.bar_type)`。
     3. `calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame` 向量化指标计算函数：
        - 输入原始行情 DataFrame（含 open, high, low, close, volume 列）及 parameters 字典。
        - 必须返回行数完全相同的 DataFrame。
        - **【极其重要】必须计算并在 DataFrame 中新增 `plot_config` 中声明的所有指标列！**
     4. `STRATEGY_MANIFEST = StrategyManifest(...)` 策略元数据与参数契约定义：
        - `slug`: 策略唯一标识（英文小写与下划线，如 `btc_ema_atr`）。
        - `name`: 策略中文名称。
        - `description`: 策略简述。
        - `category`: 策略分类（如 `"trend"`, `"mean_reversion"`, `"momentum"`）。
        - `strategy_path`: 策略类路径（格式 `"app.strategies.{slug}:{StrategyClassName}"`）。
        - `config_path`: 配置类路径（格式 `"app.strategies.{slug}:{ConfigClassName}"`）。
        - `parameters`: 参数字典，每个参数必须是 `ParameterSpec(title="中文名", type="integer"|"number"|"boolean", default=..., minimum=..., maximum=...)`。
        - `timeframes`: 周期元组，如 `("15m", "1h", "4h", "1d")`。
        - `primary_timeframe`: 主周期，如 `"1h"`。
        - `mode`: `StrategyMode.SINGLE_INSTRUMENT`。
        - `supports_short`: `True` / `False`。
        - `requires_funding`: `True` / `False`。

   - 【`plot_config` 图表契约规范（CRITICAL - 严禁写错字典层级）】：
     `plot_config` 决定了回测完成后在前端 K 线图表上的指标渲染。**必须严格按照以下结构定义：**
     ```python
     plot_config = {
         "main_plot": {
             # 主图指标：Key 必须是 DataFrame 中的指标列名，Value 必须是 {"type": "line", "color": "#hex"}
             "close": {"type": "line", "color": "#ffffff"},
             "fast_ma": {"type": "line", "color": "#ffaa00"},
             "slow_ma": {"type": "line", "color": "#00aaff"}
         },
         "subplots": {
             # 副图指标：【极其关键】必须是两层嵌套字典！
             # 第一层 Key：副图面板显示标题（如 "ATR", "RSI", "Choppiness"）
             # 第二层 Key：DataFrame 中计算出的指标列名（如 "atr", "rsi", "choppiness"）
             # ❌ 严禁写成 "subplots": {"choppiness": {"type": "line"}}（缺少面板标题层会导致回测报错找不到列！）
             # ✅ 正确写法：
             "Choppiness": {
                 "choppiness": {"type": "line", "color": "#00aaff"}
             }
         }
     }
     ```

   - 【QuantLab 标杆策略范本（Golden Template）】：
     编写策略时请严格参考以下标准模板结构：
     ```python
     import pandas as pd
     import numpy as np
     from nautilus_trader.config import StrategyConfig
     from nautilus_trader.trading.strategy import Strategy
     from nautilus_trader.model.data import Bar, BarType
     from nautilus_trader.model.enums import OrderSide, PositionSide
     from nautilus_trader.model.identifiers import InstrumentId
     from nautilus_trader.model.objects import Quantity
     from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


     class BtcEmaAtrConfig(StrategyConfig):
         instrument_id: str
         bar_type: str
         fast_period: int = 12
         slow_period: int = 26
         atr_period: int = 14
         position_size_pct: float = 0.1


     class BtcEmaAtrStrategy(Strategy):
         def __init__(self, config: BtcEmaAtrConfig):
             super().__init__(config)
             self.instrument_id = InstrumentId.from_str(config.instrument_id)
             self.bar_type = BarType.from_str(config.bar_type)

         def on_start(self):
             self.instrument = self.cache.instrument(self.instrument_id)
             self.subscribe_bars(self.bar_type)

         def on_bar(self, bar: Bar):
             bars = list(self.cache.bars(self.bar_type))
             warmup = max(self.config.slow_period, self.config.atr_period)
             if len(bars) < warmup:
                 return

             closes = np.array([b.close.as_double() for b in bars])
             fast_ma = np.mean(closes[-self.config.fast_period:])
             slow_ma = np.mean(closes[-self.config.slow_period:])

             positions = self.cache.positions()
             current_pos = positions[0].side if positions else None

             if current_pos is None:
                 if fast_ma > slow_ma:
                     self.open_position(PositionSide.LONG)
                 elif fast_ma < slow_ma:
                     self.open_position(PositionSide.SHORT)
             elif current_pos == PositionSide.LONG and fast_ma < slow_ma:
                 self.close_position()
             elif current_pos == PositionSide.SHORT and fast_ma > slow_ma:
                 self.close_position()

         def on_stop(self):
             self.unsubscribe_bars(self.bar_type)

         def open_position(self, side: PositionSide):
             account = self.portfolio.account(self.instrument_id.venue)
             free_balance = account.balance_free(self.instrument.quote_currency).as_double()
             position_size = (free_balance * self.config.position_size_pct) / self.instrument.price_increment
             qty = Quantity.from_int(int(position_size)) if position_size.is_integer() else Quantity(str(round(position_size, 4)))
             order_side = OrderSide.BUY if side == PositionSide.LONG else OrderSide.SELL
             order = self.order_factory.market(
                 instrument_id=self.instrument_id,
                 order_side=order_side,
                 quantity=qty,
             )
             self.submit_order(order)

         def close_position(self):
             position = next(iter(self.cache.positions()), None)
             if position is None:
                 return
             order_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY
             order = self.order_factory.market(
                 instrument_id=self.instrument_id,
                 order_side=order_side,
                 quantity=position.quantity,
             )
             self.submit_order(order)


     def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
         df = df.copy()
         for col in ['open', 'high', 'low', 'close', 'volume']:
             if col in df.columns:
                 df[col] = pd.to_numeric(df[col], errors='coerce')

         fast_p = int(parameters.get('fast_period', 12))
         slow_p = int(parameters.get('slow_period', 26))
         atr_p = int(parameters.get('atr_period', 14))

         df['fast_ma'] = df['close'].rolling(window=fast_p).mean()
         df['slow_ma'] = df['close'].rolling(window=slow_p).mean()
         tr = np.maximum(
             df['high'] - df['low'],
             np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1)))
         )
         df['atr'] = tr.rolling(window=atr_p).mean()
         return df


     STRATEGY_MANIFEST = StrategyManifest(
         slug="btc_ema_atr",
         name="BTC EMA+ATR趋势跟踪",
         description="双均线交叉结合ATR的趋势策略",
         version="1.0.0",
         category="trend",
         strategy_path="app.strategies.btc_ema_atr:BtcEmaAtrStrategy",
         config_path="app.strategies.btc_ema_atr:BtcEmaAtrConfig",
         parameters={
             "fast_period": ParameterSpec(title="快线周期", type="integer", default=12, minimum=2, maximum=100),
             "slow_period": ParameterSpec(title="慢线周期", type="integer", default=26, minimum=5, maximum=200),
             "atr_period": ParameterSpec(title="ATR周期", type="integer", default=14, minimum=2, maximum=50),
             "position_size_pct": ParameterSpec(title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0),
         },
         timeframes=("15m", "1h", "4h", "1d"),
         primary_timeframe="1h",
         plot_config={
             "main_plot": {
                 "close": {"type": "line", "color": "#ffffff"},
                 "fast_ma": {"type": "line", "color": "#ffaa00"},
                 "slow_ma": {"type": "line", "color": "#00aaff"},
             },
             "subplots": {
                 "ATR": {
                     "atr": {"type": "line", "color": "#ff55ff"}
                 }
             }
         },
         mode=StrategyMode.SINGLE_INSTRUMENT,
         supports_short=True,
         requires_funding=True,
     )
     ```

   - 【策略代码生成与规范】：必须由 Claude Agent SDK 编写策略文件并确保通过 4 级 Pre-Flight 运行期沙盒校验。
   - 代码编写完成后，向用户汇报策略编写完成情况与 4 级验证摘要，等待用户进一步指令。在用户未明确说明要回测之前，严禁擅自生成回测方案。

4. 回测参数方案生成时机（CRITICAL - 必须用户明确要求回测，且必须先查验 Catalog 真实数据）：
   - 【严格限制生成时机】：只有当用户在对话中【明确提出要进行回测】（例如明确表达“进行回测”、“回测一下”、“运行回测”等意图）时，你才可以生成回测参数方案。
   - 【必须先查验 Catalog 真实可用数据】：在生成回测方案前，你必须调用工具 `get_available_data` 或通过终端检查本地 Catalog 中已存在的交易标的、K线周期与历史起止时间，**严禁臆测本地不存在的远期时间区间**（否则会导致回测 0 根 Bar 空转）！
   - 在用户未明确要求回测之前（如策略讨论、代码编写完成阶段），严禁擅自生成回测参数方案，更严禁直接调用 `execute_backtest` 执行回测！
   - 当用户要求回测时，根据策略 Manifest 规范与可用行情数据提出合理的回测参数，调用工具 `propose_backtest_params` 或输出如下格式的机器块生成回测参数方案卡片：

```backtest_params
{
  "strategy_name": "btc_ema_atr",
  "symbols": ["BTCUSDT"],
  "timeframes": ["1h"],
  "start_date": "2024-01-01",
  "end_date": "2024-06-30",
  "initial_balance": 10000.0,
  "leverage": 1.0,
  "execution_model": "CONSERVATIVE",
  "parameters": {
    "fast_period": 12,
    "slow_period": 26,
    "atr_period": 14,
    "position_size_pct": 0.1
  }
}
```
   - 用户可以点击卡片打开参数配置弹窗对标的、时间区间、资金、杠杆及策略参数进行修改，并在弹窗中确认。

5. 回测执行与结果监控（CRITICAL - 真实工具调度与禁止自动循环）：
   - 【严禁在终端运行回测】：严禁调用 terminal / bash 终端执行回测或运行测试脚本！任何在终端私自运行回测或编造数据的行为均被系统判定为无效。
   - 【必须调用 execute_backtest 工具】：回测必须且只能由 QuantLab 主系统的 NautilusTrader 引擎执行。当收到用户确认回测参数的指令后，你必须调用工具 `execute_backtest`，或在回复中直接输出以下标准的工具调用机器块启动回测：
```tool_call
{
  "name": "execute_backtest",
  "arguments": {
    "strategy_name": "策略英文名",
    "symbols": ["BTCUSDT"],
    "start_date": "2024-01-01",
    "end_date": "2024-06-30",
    "initial_balance": 10000.0,
    "leverage": 1.0,
    "parameters": {}
  }
}
```
   - 【严禁虚构回测数据】：严禁在没有调用 `execute_backtest` 工具的情况下在文本中凭空编造/虚构回测收益、夏普比率等结果！
   - 【禁止自动修改策略报错与自动分析结果（CRITICAL）】：
     - 当回测执行完成（无论成功与否），系统会将回测结果与指标卡片直接呈现在前端界面供用户查看。
     - **严禁在回测报错后自动修改代码！严禁在报错后自动反复调用 execute_backtest 重新回测！**
     - **严禁在回测成功后自动输出冗长分析或自动进行参数调优！** 回测完成后必须等待用户确认后才进行下一步。

6. 策略报错单次受控修复模式（用户确认后执行 1 次，只改代码，禁止回测）：
   - 仅当用户在对话中明确确认修复报错（如点击「确认修复策略代码」或发送明确修复指令）时，你才执行 **1 次策略代码修复**（修复 `backend/app/strategies/{strategy_name}.py` 中的报错代码并自动通过 4 级沙盒自愈，完成后向用户汇报）。
   - **【安全红线 - 严禁自动回测】**：代码修复完成后，**严禁自动调用 `execute_backtest` 执行回测，严禁擅自生成回测参数卡片**！修复完成后仅简要向用户总结修复内容，等待用户下一步指令。

7. 回测结果单次受控归因分析模式（用户确认后执行 1 次，只分析原因，禁止改代码和回测）：
   - 仅当用户在对话中明确确认分析回测结果（如点击「确认进行回测深度分析」或发送分析指令）时，你才执行 **1 次深度回测归因分析**（分析收益率、夏普比率、最大回撤、胜率、盈亏比与市场行情适应性）。
   - **【安全红线 - 严禁改代码与回测】**：在归因分析时，**严格只分析指标与交易原因，严禁修改策略代码，严禁调用 `execute_backtest` 启动回测**。
"""

TOOL_CALL_REGEX = re.compile(
    r"```(?:tool_call|function_call|json:tool_call)\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

BACKTEST_PARAMS_REGEX = re.compile(
    r"```(?:backtest_params|json:backtest_params)\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

CODE_APPROVAL_REGEX = re.compile(
    r"```(?:code_approval|json:code_approval)\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

THINKING_REGEX = re.compile(
    r"<think>(.*?)</think>",
    re.DOTALL | re.IGNORECASE,
)


ACTIVE_RESEARCH_TASKS: dict[str, asyncio.Task[Any]] = {}
RESEARCH_LOCKS: dict[str, asyncio.Lock] = {}
RESEARCH_THINKING_STATUS: dict[str, dict[str, Any]] = {}
HERMES_THINKING_STATUS = RESEARCH_THINKING_STATUS


def _set_thinking_status(project_id: str, status: str, step: str, thought: str = ""):
    RESEARCH_THINKING_STATUS[project_id] = {
        "status": status,
        "step": step,
        "thought": thought,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _get_project_lock(project_id: str) -> asyncio.Lock:
    if project_id not in RESEARCH_LOCKS:
        RESEARCH_LOCKS[project_id] = asyncio.Lock()
    return RESEARCH_LOCKS[project_id]


def _project_out(project: ResearchProject) -> dict[str, Any]:
    task = ACTIVE_RESEARCH_TASKS.get(project.id)
    is_busy = task is not None and not task.done()
    return {
        "id": project.id,
        "client_id": project.client_id,
        "title": project.title,
        "original_idea": project.original_idea,
        "status": project.status.value,
        "strategy_id": project.strategy_id,
        "latest_backtest_id": project.latest_backtest_id,
        "is_busy": is_busy,
        "conclusion": None
        if not project.conclusion_verdict
        else {
            "verdict": project.conclusion_verdict,
            "summary": project.conclusion_summary or "",
            "next_step": project.conclusion_next_step or "",
        },
        "archived_at": project.archived_at,
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def _message_out(msg: ResearchMessage) -> dict[str, Any]:
    return {
        "id": msg.id,
        "project_id": msg.project_id,
        "role": msg.role,
        "content": msg.content,
        "message_type": msg.message_type,
        "metadata": msg.metadata_json or {},
        "created_at": msg.created_at,
    }


async def _project(project_id: str, db: AsyncSession) -> ResearchProject:
    project = await db.get(ResearchProject, project_id)
    if not project:
        raise HTTPException(404, "研究项目不存在")
    return project


def _parse_llm_response(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]], str]:
    """Extract message text, tool calls, and reasoning/thinking content from LLM response payload."""
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_parts: list[str] = []

    # 1. Check standard OpenAI tool_calls and reasoning
    choices = payload.get("choices", [])
    if choices:
        choice = choices[0]
        msg = choice.get("message", {})
        if msg.get("reasoning_content"):
            reasoning_parts.append(str(msg["reasoning_content"]))
        elif msg.get("thought"):
            reasoning_parts.append(str(msg["thought"]))
        elif msg.get("reasoning"):
            reasoning_parts.append(str(msg["reasoning"]))

        if msg.get("content"):
            text_parts.append(str(msg["content"]))
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            try:
                args = (
                    json.loads(fn.get("arguments", "{}"))
                    if isinstance(fn.get("arguments"), str)
                    else fn.get("arguments", {})
                )
            except Exception:
                args = {}
            tool_calls.append({"name": fn.get("name"), "arguments": args, "id": tc.get("id")})

    # 2. Check /responses output schema
    for item in payload.get("output", []):
        kind = item.get("type")
        if kind in ("thought", "reasoning"):
            for part in item.get("content", []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    reasoning_parts.append(part["text"])
                elif isinstance(part, str):
                    reasoning_parts.append(part)
        elif kind == "message":
            for part in item.get("content", []):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text_parts.append(part["text"])
        elif kind == "function_call":
            name = item.get("name")
            args = item.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            tool_calls.append({"name": name, "arguments": args, "id": item.get("id")})

    if not text_parts and isinstance(payload.get("output_text"), str):
        text_parts.append(payload["output_text"])

    full_text = "\n".join(text_parts).strip()

    # 3. Extract <think>...</think> if present inside content
    think_matches = THINKING_REGEX.findall(full_text)
    if think_matches:
        for tm in think_matches:
            reasoning_parts.append(tm.strip())
        full_text = THINKING_REGEX.sub("", full_text).strip()

    # 4. Fallback regex extraction if model wrote tool calls in text
    if not tool_calls:
        for match in TOOL_CALL_REGEX.finditer(full_text):
            try:
                parsed = json.loads(match.group(1).strip())
                if isinstance(parsed, dict) and "name" in parsed:
                    tool_calls.append(
                        {
                            "name": parsed.get("name"),
                            "arguments": parsed.get("arguments") or parsed.get("parameters") or {},
                        }
                    )
            except Exception:
                pass

    reasoning_content = "\n\n".join(reasoning_parts).strip()
    return full_text, tool_calls, reasoning_content


_parse_hermes_response = _parse_llm_response


async def _call_research_llm_stream(
    project: ResearchProject,
    prompt: str,
    instructions: str = RESEARCH_INSTRUCTIONS,
    tools: list[dict[str, Any]] | None = None,
    db: AsyncSession | None = None,
) -> tuple[str, list[dict[str, Any]], str]:
    """Invoke DeepSeek Harness (DSH) LLM streaming endpoint with real-time reasoning and tool execution."""
    from app.dsh.runtime import dsh_runtime
    from app.llm_config import decrypt_api_key
    from app.models import LlmConfiguration

    cfg = await db.get(LlmConfiguration, 1) if db else None

    # Construct chat messages history
    chat_messages: list[dict[str, Any]] = []
    if db is not None:
        prev_rows = (
            await db.scalars(
                select(ResearchMessage)
                .where(ResearchMessage.project_id == project.id)
                .order_by(ResearchMessage.created_at)
            )
        ).all()
        for row in prev_rows[-20:]:
            if row.role in ("user", "assistant"):
                chat_messages.append({"role": row.role, "content": row.content})
            elif row.role == "tool":
                chat_messages.append({"role": "user", "content": f"【工具执行结果】：\n{row.content}"})
    if not chat_messages or chat_messages[-1].get("content") != prompt:
        chat_messages.append({"role": "user", "content": prompt})

    # Resolve DeepSeek Harness primary LLM configuration
    base_url = "https://api.deepseek.com/v1"
    api_key = ""
    model = "deepseek-chat"
    timeout_seconds = 120

    if cfg is not None:
        if cfg.base_url and cfg.model:
            base_url = cfg.base_url.rstrip("/")
            api_key = decrypt_api_key(cfg.api_key_encrypted) if cfg.api_key_encrypted else ""
            model = cfg.model
            timeout_seconds = cfg.timeout_seconds or 120

    headers = {
        "Content-Type": "application/json",
        "X-Session-Id": project.conversation_id,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"

    # Format tools according to OpenAI function tool format
    tool_defs = tools or TOOL_DEFINITIONS
    openai_tools = []
    for t in tool_defs:
        if "function" in t:
            openai_tools.append(t)
        else:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })

    from app.dsh.runtime import normalize_llm_endpoint

    primary_endpoint = normalize_llm_endpoint(base_url)
    candidate_endpoints: list[str] = [primary_endpoint]
    clean_url = base_url.rstrip("/")
    if f"{clean_url}/v1/chat/completions" not in candidate_endpoints:
        candidate_endpoints.append(f"{clean_url}/v1/chat/completions")
    if f"{clean_url}/chat/completions" not in candidate_endpoints:
        candidate_endpoints.append(f"{clean_url}/chat/completions")
    if f"{clean_url}/v1/messages" not in candidate_endpoints:
        candidate_endpoints.append(f"{clean_url}/v1/messages")
    if f"{clean_url}/responses" not in candidate_endpoints:
        candidate_endpoints.append(f"{clean_url}/responses")

    timeout = httpx.Timeout(timeout_seconds)
    full_text_chunks: list[str] = []
    reasoning_chunks: list[str] = []
    tool_calls_map: dict[int, dict[str, Any]] = {}

    _set_thinking_status(
        project.id,
        "THINKING",
        "DeepSeek Harness (DSH) 正在深度思考量化假设与指标计算规则...",
    )

    last_stream_err = ""
    stream_success = False
    for endpoint in candidate_endpoints:
        if endpoint.endswith("/responses"):
            body = {
                "model": model,
                "conversation": project.conversation_id,
                "input": prompt,
                "instructions": instructions,
                "tools": openai_tools,
                "store": True,
                "stream": True,
            }
        elif endpoint.endswith("/messages"):
            body = {
                "model": model,
                "max_tokens": 4096,
                "messages": chat_messages,
                "system": instructions,
                "stream": True,
                "temperature": 0.2,
            }
        else:
            body = {
                "model": model,
                "messages": [{"role": "system", "content": instructions}, *chat_messages],
                "tools": openai_tools,
                "stream": True,
                "temperature": 0.2,
            }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", endpoint, headers=headers, json=body) as resp:
                    if resp.status_code == 404 and len(candidate_endpoints) > 1:
                        err_body = await resp.aread()
                        last_stream_err = f"404: {err_body.decode('utf-8', errors='replace')}"
                        continue
                    if resp.status_code != 200:
                        err_body = await resp.aread()
                        logger.warning("DSH LLM Stream API (%s) 返回非200状态码 (%s): %s", endpoint, resp.status_code, err_body)
                        break

                    cur_event = ""
                    async for raw_line in resp.aiter_lines():
                        if not raw_line:
                            continue
                        if raw_line.startswith("event:"):
                            cur_event = raw_line[6:].strip()
                            continue
                        if not raw_line.startswith("data:"):
                            continue

                        data_str = raw_line[5:].strip()
                        if data_str == "[DONE]":
                            break

                        # Handle custom tool progress events
                        if cur_event.startswith("hermes.tool") or cur_event.startswith("dsh.tool"):
                            try:
                                t_info = json.loads(data_str)
                                t_tool = t_info.get("tool") or t_info.get("label") or "量化工具"
                                t_status = t_info.get("status", "running")
                                _set_thinking_status(
                                    project.id,
                                    "TOOL_RUNNING",
                                    f"DeepSeek Harness 正在调度工具: {t_tool} ({t_status})...",
                                    thought="".join(reasoning_chunks),
                                )
                            except Exception:
                                pass
                            cur_event = ""
                            continue

                        try:
                            chunk = json.loads(data_str)
                        except Exception:
                            continue

                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            if delta.get("reasoning_content"):
                                r_c = str(delta["reasoning_content"])
                                reasoning_chunks.append(r_c)
                                _set_thinking_status(
                                    project.id,
                                    "THINKING",
                                    "DeepSeek Harness (DSH) 正在深度思考量化假设与指标计算规则...",
                                    thought="".join(reasoning_chunks),
                                )
                            elif delta.get("thought"):
                                r_c = str(delta["thought"])
                                reasoning_chunks.append(r_c)
                                _set_thinking_status(
                                    project.id,
                                    "THINKING",
                                    "DeepSeek Harness (DSH) 正在深度思考量化假设与指标计算规则...",
                                    thought="".join(reasoning_chunks),
                                )

                            if delta.get("content"):
                                c = str(delta["content"])
                                full_text_chunks.append(c)
                                _set_thinking_status(
                                    project.id,
                                    "GENERATING",
                                    "DeepSeek Harness 正在组织方案与调度指令...",
                                    thought="".join(reasoning_chunks),
                                )

                            for tc in delta.get("tool_calls", []):
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_map:
                                    tool_calls_map[idx] = {
                                        "id": tc.get("id", ""),
                                        "name": tc.get("function", {}).get("name", ""),
                                        "arguments": "",
                                    }
                                if tc.get("id"):
                                    tool_calls_map[idx]["id"] = tc["id"]
                                if tc.get("function", {}).get("name"):
                                    tool_calls_map[idx]["name"] = tc["function"]["name"]
                                if tc.get("function", {}).get("arguments"):
                                    tool_calls_map[idx]["arguments"] += tc["function"]["arguments"]

                        for item in chunk.get("output", []):
                            k = item.get("type")
                            if k in ("thought", "reasoning"):
                                for part in item.get("content", []):
                                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                                        reasoning_chunks.append(part["text"])
                                    elif isinstance(part, str):
                                        reasoning_chunks.append(part)
                            elif k == "message":
                                for part in item.get("content", []):
                                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                                        full_text_chunks.append(part["text"])

                    stream_success = True
                    break
        except Exception as exc:
            last_stream_err = str(exc)
            logger.warning("DeepSeek Harness 流式请求端点 (%s) 异常: %s", endpoint, exc)
            continue

    if not stream_success:
        logger.info("流式传输未完成，执行 DSH runtime 非流式保底调用...")
        content, tool_calls, reasoning = await dsh_runtime.call_llm(
            messages=chat_messages,
            system_prompt=instructions,
            tools=tool_defs,
            db_config=cfg,
        )
        if not content.startswith("[API Error") and not content.startswith("[LLM Exception"):
            return content, tool_calls, reasoning
        raise HTTPException(502, f"DeepSeek Harness 调用失败：{content or last_stream_err}")

    full_text = "".join(full_text_chunks).strip()
    reasoning_content = "".join(reasoning_chunks).strip()

    parsed_tool_calls: list[dict[str, Any]] = []
    for tc in tool_calls_map.values():
        name = tc.get("name", "")
        raw_args = tc.get("arguments", "{}")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except Exception:
            args = {}
        parsed_tool_calls.append({"name": name, "arguments": args, "id": tc.get("id")})

    think_matches = THINKING_REGEX.findall(full_text)
    if think_matches:
        for tm in think_matches:
            reasoning_content += ("\n\n" if reasoning_content else "") + tm.strip()
        full_text = THINKING_REGEX.sub("", full_text).strip()

    if not parsed_tool_calls:
        for match in TOOL_CALL_REGEX.finditer(full_text):
            try:
                parsed = json.loads(match.group(1).strip())
                if isinstance(parsed, dict) and "name" in parsed:
                    parsed_tool_calls.append(
                        {
                            "name": parsed.get("name"),
                            "arguments": parsed.get("arguments") or parsed.get("parameters") or {},
                        }
                    )
            except Exception:
                pass

    return full_text, parsed_tool_calls, reasoning_content


_call_hermes_stream = _call_research_llm_stream


async def _sync_strategy_code_if_present(
    text: str,
    project: ResearchProject,
    db: AsyncSession,
) -> str | None:
    """Helper to detect python strategy code in text and auto-save/sync to DB."""
    if not text:
        return None
    if "STRATEGY_MANIFEST" in text or "StrategyConfig" in text:
        py_blocks = re.findall(r"```python\s*(.*?)\s*```", text, re.DOTALL)
        for block in py_blocks:
            if "STRATEGY_MANIFEST" in block or "StrategyConfig" in block:
                slug_m = re.search(r'slug\s*=\s*["\']([a-z0-9_]+)["\']', block)
                if not slug_m:
                    slug_m = re.search(r'name\s*=\s*["\']([a-z0-9_]+)["\']', block)
                s_slug = slug_m.group(1).lower() if slug_m else f"strat_{project.id[:8]}"
                save_strategy_code(s_slug, block)
                await ensure_strategy_db_record(s_slug, db, project_id=project.id)
                return s_slug

    strat_dir = settings.strategy_repo_path.resolve() / "backend/app/strategies"
    if strat_dir.exists():
        for py_file in strat_dir.glob("*.py"):
            if py_file.name.startswith("__"):
                continue
            if (datetime.now().timestamp() - py_file.stat().st_mtime) < 120:
                try:
                    c_text = py_file.read_text(encoding="utf-8")
                    if "STRATEGY_MANIFEST" in c_text and "StrategyConfig" in c_text:
                        s_slug = py_file.stem
                        await ensure_strategy_db_record(s_slug, db, project_id=project.id)
                        return s_slug
                except Exception:
                    pass
    return None


async def run_research_agent_cycle(
    project: ResearchProject,
    user_prompt: str,
    db: AsyncSession,
    max_turns: int = 6,
    record_user_prompt: bool = True,
) -> list[ResearchMessage]:
    """Run an autonomous multi-turn cycle: User -> DSH Quant Lead -> Tools -> Result."""
    new_messages: list[ResearchMessage] = []

    # 1. Record user message if not recorded yet
    if record_user_prompt:
        user_msg = ResearchMessage(
            project_id=project.id,
            role="user",
            content=user_prompt,
            message_type="message",
        )
        db.add(user_msg)
        project.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user_msg)
        new_messages.append(user_msg)

    current_prompt = user_prompt
    turn = 0

    try:
        while turn < max_turns:
            turn += 1
            _set_thinking_status(
                project.id,
                "THINKING",
                f"DSH Quant Lead 正在统筹量化假设与指标计算规则（轮次 {turn}）...",
            )
            text, tool_calls, reasoning_content = await _call_research_llm_stream(project, current_prompt, db=db)

            if reasoning_content:
                _set_thinking_status(
                    project.id,
                    "THINKING",
                    "DSH Quant Lead 思考完成，正在组织研讨方案与调度指令...",
                    thought=reasoning_content,
                )

            # Record assistant text response if present
            if text:
                clean_text = TOOL_CALL_REGEX.sub("", text).strip()
                if clean_text:
                    # Check for backtest_params block in text
                    bp_meta: dict[str, Any] = {}
                    bp_match = BACKTEST_PARAMS_REGEX.search(clean_text)
                    if bp_match:
                        try:
                            bp_meta = {"backtest_params": json.loads(bp_match.group(1).strip())}
                        except Exception:
                            pass

                    # Check for code_approval block in text
                    ca_meta: dict[str, Any] = {}
                    ca_match = CODE_APPROVAL_REGEX.search(clean_text)
                    if ca_match:
                        try:
                            ca_meta = {"code_approval": json.loads(ca_match.group(1).strip())}
                        except Exception:
                            pass

                    # Auto-detect and sync strategy code from text or disk
                    await _sync_strategy_code_if_present(clean_text, project, db)

                    meta: dict[str, Any] = {}
                    if reasoning_content:
                        meta["reasoning_content"] = reasoning_content
                    if bp_meta:
                        meta.update(bp_meta)
                    if ca_meta:
                        meta.update(ca_meta)

                    msg_type = (
                        "code_approval"
                        if ca_meta
                        else "backtest_params"
                        if bp_meta
                        else "message"
                    )

                    asst_msg = ResearchMessage(
                        project_id=project.id,
                        role="assistant",
                        content=clean_text,
                        message_type=msg_type,
                        metadata_json=meta,
                    )
                    db.add(asst_msg)
                    project.updated_at = datetime.now(UTC)
                    await db.commit()
                    await db.refresh(asst_msg)
                    new_messages.append(asst_msg)

            # Fallback safeguard: If LLM did not emit a tool call but user confirmed backtest parameters, intercept and execute
            if not tool_calls and turn == 1:
                if "execute_backtest" in current_prompt and ("【回测参数已确认" in current_prompt or "回测参数已确认" in current_prompt):
                    tc_match = TOOL_CALL_REGEX.search(current_prompt)
                    if tc_match:
                        try:
                            p_data = json.loads(tc_match.group(1).strip())
                            if p_data.get("name") == "execute_backtest" and p_data.get("arguments"):
                                tool_calls.append({
                                    "name": "execute_backtest",
                                    "arguments": p_data.get("arguments"),
                                })
                        except Exception:
                            pass
                    if not tool_calls:
                        strat_m = re.search(r'["\']?strategy_name["\']?\s*:\s*["\']([a-zA-Z0-9_]+)["\']', current_prompt)
                        syms_m = re.search(r'["\']?symbols["\']?\s*:\s*(\[[^\]]+\])', current_prompt)
                        sd_m = re.search(r'["\']?start_date["\']?\s*:\s*["\']([0-9\-]+)["\']', current_prompt)
                        ed_m = re.search(r'["\']?end_date["\']?\s*:\s*["\']([0-9\-]+)["\']', current_prompt)
                        if strat_m and sd_m and ed_m:
                            try:
                                s_list = json.loads(syms_m.group(1)) if syms_m else ["BTCUSDT"]
                            except Exception:
                                s_list = ["BTCUSDT"]
                            tool_calls.append({
                                "name": "execute_backtest",
                                "arguments": {
                                    "strategy_name": strat_m.group(1),
                                    "symbols": s_list,
                                    "start_date": sd_m.group(1),
                                    "end_date": ed_m.group(1),
                                }
                            })

            # Guardrails for restricted modes
            is_analysis_mode = bool(re.search(r"归因分析|回测分析|分析本次回测|只分析原因|只分析指标", user_prompt, re.IGNORECASE))
            is_repair_mode = bool(re.search(r"修复策略代码|修复报错|单次代码修复|只修改代码|禁止回测|禁止自动回测", user_prompt, re.IGNORECASE))

            filtered_tool_calls: list[dict[str, Any]] = []
            for tc in tool_calls:
                t_name = tc.get("name", "")
                if is_analysis_mode and t_name in ("execute_backtest", "write_strategy_with_claude"):
                    logger.warning("【安全防护】分析模式拦截工具调用：%s", t_name)
                    continue
                if is_repair_mode and t_name == "execute_backtest":
                    logger.warning("【安全防护】修复模式拦截回测工具调用：%s", t_name)
                    continue
                filtered_tool_calls.append(tc)
            tool_calls = filtered_tool_calls

            # If no tool calls, the cycle is complete
            if not tool_calls:
                break

            # Execute each tool call
            tool_results_summary: list[str] = []
            has_backtest = False
            has_proposal = False
            has_write_strategy = False
            write_strategy_result: dict[str, Any] | None = None

            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_args = tc.get("arguments", {})

                if tool_name == "execute_backtest":
                    has_backtest = True
                elif tool_name in ("propose_code_approval", "propose_backtest_params"):
                    has_proposal = True
                elif tool_name in ("write_strategy_with_claude", "write_strategy_code"):
                    has_write_strategy = True

                # Update thinking status for tool execution
                if tool_name == "propose_code_approval":
                    _set_thinking_status(
                        project.id,
                        "WAITING_APPROVAL",
                        "策略设计方案已就绪，已向用户发起编码审批请求...",
                    )
                elif tool_name in ("write_strategy_with_claude", "write_strategy_code"):
                    _set_thinking_status(
                        project.id,
                        "TOOL_RUNNING",
                        f"正在编写策略「{tool_args.get('strategy_name', '策略')}」...",
                    )
                elif tool_name == "execute_backtest":
                    _set_thinking_status(
                        project.id,
                        "TOOL_RUNNING",
                        f"正在调用 NautilusTrader 执行回测 ({tool_args.get('strategy_name')})...",
                    )
                elif tool_name == "propose_backtest_params":
                    _set_thinking_status(
                        project.id,
                        "TOOL_RUNNING",
                        "正在生成交互式回测参数配置卡片...",
                    )
                else:
                    _set_thinking_status(
                        project.id,
                        "TOOL_RUNNING",
                        f"DeepSeek Harness 正在调用工具：{tool_name}...",
                    )

                # Record tool invocation message
                call_msg = ResearchMessage(
                    project_id=project.id,
                    role="assistant",
                    content=f"调用工具 `{tool_name}`: {json.dumps(tool_args, ensure_ascii=False)}",
                    message_type="tool_call",
                    metadata_json={"tool_name": tool_name, "arguments": tool_args},
                )
                db.add(call_msg)
                project.updated_at = datetime.now(UTC)
                await db.commit()
                await db.refresh(call_msg)
                new_messages.append(call_msg)

                # Execute tool
                try:
                    result = await dispatch_tool_call(tool_name, tool_args, project_id=project.id, db=db)
                except Exception as exc:
                    logger.error("工具执行出错 %s: %s", tool_name, exc)
                    result = {"ok": False, "error": f"工具执行异常：{exc}"}

                if tool_name in ("write_strategy_with_claude", "write_strategy_code"):
                    write_strategy_result = result

                # If backtest was triggered, record latest_backtest_id
                if tool_name == "execute_backtest" and result.get("run_id"):
                    project.latest_backtest_id = result["run_id"]
                    project.updated_at = datetime.now(UTC)
                    await db.commit()

                # Record tool result message
                res_content = json.dumps(result, ensure_ascii=False, indent=2)
                msg_type = (
                    "code_approval"
                    if tool_name == "propose_code_approval"
                    else "backtest_params"
                    if tool_name == "propose_backtest_params"
                    else "backtest_result"
                    if tool_name == "execute_backtest"
                    else "tool_output"
                )
                meta = {
                    "tool_name": tool_name,
                    "result": result,
                }
                if tool_name == "propose_code_approval":
                    meta["code_approval"] = tool_args
                    strat_name = tool_args.get("strategy_name", "custom_strategy")
                    strat_summary = tool_args.get("strategy_summary", "")
                    key_rules = tool_args.get("key_rules", [])
                    param_specs = tool_args.get("parameter_specs", {})

                    rules_md = "\n".join([f"- {r}" for r in key_rules]) if key_rules else "- 待细化"
                    params_rows = ""
                    if isinstance(param_specs, dict) and param_specs:
                        params_rows = (
                            "\n\n**预设参数清单**：\n| 参数名 | 默认值 |\n| :--- | :--- |\n"
                            + "\n".join([f"| `{k}` | `{v}` |" for k, v in param_specs.items()])
                        )

                    res_content = (
                        f"### 📋 量化策略设计方案：`{strat_name}`\n\n"
                        f"**策略核心构想**：\n{strat_summary}\n\n"
                        f"**核心交易规则**：\n{rules_md}"
                        f"{params_rows}\n\n"
                        f"*(策略设计方案已就绪，请核对下方方案卡片并确认是否批准编写代码)*"
                    )
                elif tool_name == "propose_backtest_params":
                    meta["backtest_params"] = tool_args
                elif tool_name == "execute_backtest":
                    meta["backtest_result"] = result

                out_msg = ResearchMessage(
                    project_id=project.id,
                    role="tool",
                    content=res_content,
                    message_type=msg_type,
                    metadata_json=meta,
                )
                db.add(out_msg)
                project.updated_at = datetime.now(UTC)
                await db.commit()
                await db.refresh(out_msg)
                new_messages.append(out_msg)

                tool_results_summary.append(
                    f"【工具 {tool_name} 执行结果】：\n{json.dumps(result, ensure_ascii=False)}"
                )

            # 1. Backtest execution completed: Prohibit automatic error fixes and automatic backtest analysis.
            # Terminate agent cycle immediately and let frontend display results/errors for manual user confirmation.
            if has_backtest:
                logger.info("回测工具执行完成，立即终止自动循环，等待用户确认后续操作。")
                break

            # 2. Proposals (code approval or backtest params) waiting for user manual interaction: Terminate cycle.
            if has_proposal:
                logger.info("审批/参数方案已生成，等待用户操作确认，终止自动循环。")
                break

            # 3. Strategy code write/repair completed: Allow 1 concise summary turn, with strict prohibition of backtesting.
            if has_write_strategy:
                if write_strategy_result and write_strategy_result.get("ok"):
                    current_prompt = (
                        "策略代码已成功编写并通过 4 级 Pre-Flight 运行期沙盒校验。\n"
                        "请用 2-3 句话简短向用户汇报策略编写的核心逻辑与指标，并告知代码已通过 4 级沙盒校验就绪。\n"
                        "【系统安全红线】：严禁调用 execute_backtest 工具，严禁擅自启动回测，严禁生成回测参数卡片。汇报完毕后等待用户下一步明确指令。"
                    )
                else:
                    err_msg = write_strategy_result.get("error", "Pre-Flight 沙盒校验未通过") if write_strategy_result else "代码生成未完成"
                    current_prompt = (
                        f"策略代码编写/修改未通过 4 级 Pre-Flight 运行期沙盒校验。\n"
                        f"错误详情：\n{err_msg}\n\n"
                        f"请用 2-3 句话如实向用户汇报策略编写未能通过沙盒检测的具体报错层级与原因，并说明修复建议，提示用户可查看右侧日志，等待用户下一步修改指令。\n"
                        f"【系统安全红线】：严禁谎称代码编写成功，严禁调用 execute_backtest 工具，严禁擅自启动回测。"
                    )
            else:
                current_prompt = "\n\n".join(tool_results_summary)
                current_prompt += "\n\n请根据上述工具执行结果简短汇报。注意：若未收到用户明确的回测或修复指令，严禁擅自调用回测或修改代码。"

        project.updated_at = datetime.now(UTC)
        await db.commit()
    finally:
        _set_thinking_status(project.id, "IDLE", "就绪", "")

    return new_messages


run_hermes_agent_cycle = run_research_agent_cycle


async def _run_research_background(project_id: str, prompt: str) -> None:
    """Run DSH agent cycle in a background task decoupled from HTTP request lifecycle."""
    lock = _get_project_lock(project_id)
    async with lock:
        async with SessionLocal() as db:
            project = await db.get(ResearchProject, project_id)
            if not project:
                return
            try:
                await run_research_agent_cycle(
                    project, prompt, db=db, record_user_prompt=False
                )
            except Exception as exc:
                logger.error("DSH Agent 运行异常 (project=%s): %s", project_id, exc, exc_info=True)
                try:
                    err_msg = ResearchMessage(
                        project_id=project.id,
                        role="assistant",
                        content=f"⚠️ DeepSeek Harness 处理过程中遇到异常：{exc}",
                        message_type="message",
                    )
                    db.add(err_msg)
                    project.updated_at = datetime.now(UTC)
                    await db.commit()
                except Exception:
                    pass


_run_hermes_background = _run_research_background


def _start_research_task(project_id: str, prompt: str) -> asyncio.Task[None]:
    """Start a background research task and track it in ACTIVE_RESEARCH_TASKS."""
    task = asyncio.create_task(_run_research_background(project_id, prompt))
    ACTIVE_RESEARCH_TASKS[project_id] = task

    def _cleanup(t: asyncio.Task[None]):
        if ACTIVE_RESEARCH_TASKS.get(project_id) is t:
            ACTIVE_RESEARCH_TASKS.pop(project_id, None)

    task.add_done_callback(_cleanup)
    return task


# ------------------ REST Endpoints ------------------


@router.get("")
async def list_projects(client_id: str | None = None, db: AsyncSession = Depends(get_db)):
    stmt = select(ResearchProject)
    if client_id:
        stmt = stmt.where(ResearchProject.client_id == client_id)
    rows = (await db.scalars(stmt.order_by(ResearchProject.updated_at.desc()))).all()
    return [_project_out(row) for row in rows]


@router.post("")
async def create_project(data: ResearchProjectCreate, db: AsyncSession = Depends(get_db)):
    project = ResearchProject(
        client_id=data.client_id,
        title=data.title,
        original_idea=data.original_idea,
        conversation_id=f"quantlab-research-{uuid.uuid4()}",
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # If original_idea is provided, immediately persist user message and trigger background task
    if data.original_idea and data.original_idea.strip():
        idea = data.original_idea.strip()
        user_msg = ResearchMessage(
            project_id=project.id,
            role="user",
            content=idea,
            message_type="message",
        )
        db.add(user_msg)
        project.updated_at = datetime.now(UTC)
        await db.commit()
        _start_research_task(project.id, idea)

    return _project_out(project)


@router.get("/{project_id}")
async def get_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    return _project_out(project)


@router.get("/{project_id}/status")
async def get_project_status(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    task = ACTIVE_RESEARCH_TASKS.get(project.id)
    is_busy = task is not None and not task.done()
    return {
        "id": project.id,
        "status": project.status.value,
        "is_busy": is_busy,
        "updated_at": project.updated_at,
    }


@router.get("/{project_id}/messages")
async def list_messages(project_id: str, db: AsyncSession = Depends(get_db)):
    await _project(project_id, db)
    rows = (
        await db.scalars(
            select(ResearchMessage)
            .where(ResearchMessage.project_id == project_id)
            .order_by(ResearchMessage.created_at)
        )
    ).all()
    return [_message_out(row) for row in rows]


@router.post("/{project_id}/messages")
async def send_message(
    project_id: str,
    data: ResearchMessageCreate,
    db: AsyncSession = Depends(get_db),
):
    project = await _project(project_id, db)
    if project.status == ResearchStatus.ARCHIVED:
        raise HTTPException(409, "研究项目已归档，请先重新打开")

    content = data.content.strip()
    if not content:
        raise HTTPException(400, "消息内容不能为空")

    # 1. Immediately persist user message to DB and commit
    user_msg = ResearchMessage(
        project_id=project.id,
        role="user",
        content=content,
        message_type="message",
    )
    db.add(user_msg)
    project.updated_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(user_msg)

    # 2. Trigger research agent cycle in decoupled background task
    _start_research_task(project.id, content)

    return [_message_out(user_msg)]


@router.get("/{project_id}/backtests")
async def list_project_backtests(project_id: str, db: AsyncSession = Depends(get_db)):
    await _project(project_id, db)
    rows = (
        await db.scalars(
            select(BacktestRun)
            .where(BacktestRun.research_project_id == project_id)
            .order_by(BacktestRun.created_at.desc())
        )
    ).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "status": row.status.value,
            "stage": row.stage,
            "progress": row.progress,
            "config": row.config,
            "metrics": row.metrics,
            "error_message": row.error_message,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.get("/{project_id}/writing-log")
async def get_project_writing_log(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get the live progress and logs of strategy writing."""
    await _project(project_id, db)
    return get_writing_log_tool(project_id)


@router.get("/{project_id}/thinking-status")
async def get_project_thinking_status(project_id: str, db: AsyncSession = Depends(get_db)):
    """Get real-time thinking status and reasoning content for Research Agent."""
    await _project(project_id, db)
    return RESEARCH_THINKING_STATUS.get(
        project_id,
        {"status": "IDLE", "step": "就绪", "thought": "", "updated_at": datetime.now(UTC).isoformat()},
    )


@router.get("/{project_id}/strategy")
async def get_project_strategy(
    project_id: str,
    strategy_name: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get the strategy source code and manifest for this research project."""
    project = await _project(project_id, db)

    target_name = strategy_name
    if not target_name and project.strategy_id:
        strat = await db.get(Strategy, project.strategy_id)
        if strat:
            target_name = strat.slug

    if not target_name:
        # Search messages for strategy_name in tool_calls, tool_outputs, backtest_params, or code_approval
        tool_msgs = (
            await db.scalars(
                select(ResearchMessage)
                .where(
                    ResearchMessage.project_id == project.id,
                    ResearchMessage.message_type.in_(["tool_call", "tool_output", "backtest_params", "code_approval"]),
                )
                .order_by(ResearchMessage.created_at.desc())
            )
        ).all()
        for t_msg in tool_msgs:
            if isinstance(t_msg.metadata_json, dict):
                arg_path = t_msg.metadata_json.get("arguments", {}).get("path") or ""
                s_name = (
                    t_msg.metadata_json.get("strategy_name")
                    or t_msg.metadata_json.get("arguments", {}).get("strategy_name")
                    or t_msg.metadata_json.get("result", {}).get("strategy_name")
                    or t_msg.metadata_json.get("backtest_params", {}).get("strategy_name")
                    or t_msg.metadata_json.get("code_approval", {}).get("strategy_name")
                    or (Path(arg_path).stem if arg_path.endswith(".py") else None)
                )
                if not s_name:
                    s_m = re.search(r'策略名称[：:\s]+([a-z0-9_]+)', str(t_msg.content) + " " + str(t_msg.metadata_json))
                    if s_m:
                        s_name = s_m.group(1)
                if not s_name and "app/strategies/" in t_msg.content:
                    p_match = re.search(r'app/strategies/([a-z0-9_]+)\.py', t_msg.content)
                    if p_match:
                        s_name = p_match.group(1)
                if s_name:
                    target_name = s_name
                    break

    # If target_name found, try to read from disk or persistent storage
    if target_name:
        res = get_strategy_code_tool(target_name)
        if res.get("ok"):
            # Ensure DB record is synced and linked to project
            await ensure_strategy_db_record(target_name, db, project_id=project.id)
            return res

    # If still not found, check assistant messages for python code blocks containing STRATEGY_MANIFEST
    asst_msgs = (
        await db.scalars(
            select(ResearchMessage)
            .where(
                ResearchMessage.project_id == project.id,
                ResearchMessage.role == "assistant",
            )
            .order_by(ResearchMessage.created_at.desc())
        )
    ).all()

    for m in asst_msgs:
        code_blocks = re.findall(r"```python\s*(.*?)\s*```", m.content, re.DOTALL)
        for code_block in code_blocks:
            if "STRATEGY_MANIFEST" in code_block or "StrategyConfig" in code_block:
                slug_match = re.search(r'slug\s*=\s*["\']([a-z0-9_]+)["\']', code_block)
                if not slug_match:
                    slug_match = re.search(r'name\s*=\s*["\']([a-z0-9_]+)["\']', code_block)
                derived_slug = slug_match.group(1).lower() if slug_match else f"strat_{project.id[:8]}"
                save_strategy_code(derived_slug, code_block)
                await ensure_strategy_db_record(derived_slug, db, project_id=project.id)
                return {
                    "ok": True,
                    "strategy_name": derived_slug,
                    "code": code_block,
                }

    return {"ok": False, "message": "尚未生成策略代码"}


@router.post("/{project_id}/archive")
async def archive_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    project.status = ResearchStatus.ARCHIVED
    project.archived_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(project)
    return _project_out(project)


@router.post("/{project_id}/reopen")
async def reopen_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    project.status = ResearchStatus.DISCUSSING
    project.archived_at = None
    await db.commit()
    await db.refresh(project)
    return _project_out(project)


@router.delete("/{project_id}")
async def delete_project(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    # Delete associated messages
    msgs = (
        await db.scalars(
            select(ResearchMessage).where(ResearchMessage.project_id == project.id)
        )
    ).all()
    for m in msgs:
        await db.delete(m)
    await db.delete(project)
    await db.commit()
    return {"ok": True, "message": "研究项目已删除"}


@router.post("/tools/write-strategy")
async def write_strategy_endpoint(
    req: ResearchWriteStrategyRequest,
    db: AsyncSession = Depends(get_db),
):
    """API endpoint for strategy code generation."""
    res = await write_strategy_code(
        strategy_name=req.strategy_name,
        instructions=req.instructions,
        is_fix=req.is_fix,
        error_context=req.error_context,
        specification=req.specification,
        project_id=req.project_id,
        db=db,
    )
    if not res.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=res.get("error", "Strategy generation failed"),
        )
    return res


@router.post("/{project_id}/dsh/run")
async def run_dsh_pipeline_endpoint(
    project_id: str,
    data: ResearchMessageCreate,
    db: AsyncSession = Depends(get_db),
):
    """Execute the full DeepSeek Harness (DSH) Star-Topology Multi-Agent workflow."""
    project = await _project(project_id, db)
    from app.dsh.orchestrator import DSHOrchestrator
    from app.models import LlmConfiguration

    cfg = await db.get(LlmConfiguration, 1)
    orchestrator = DSHOrchestrator(session_id=project.id, db_config=cfg)

    # Record User message
    user_msg = ResearchMessage(
        project_id=project.id,
        role="user",
        content=data.content,
        message_type="message",
    )
    db.add(user_msg)
    await db.commit()

    async def _handle_agent_event(event):
        try:
            async with SessionLocal() as s:
                m_type = "thought" if event.event_type == "thought" else ("tool" if event.agent_role == "tool" else "message")
                m = ResearchMessage(
                    project_id=project.id,
                    role="assistant" if event.agent_role in ("lead", "researcher", "developer", "reviewer") else event.agent_role,
                    content=f"【{event.agent_role.upper()}】: {event.content}" if event.agent_role != "lead" else event.content,
                    message_type=m_type,
                    metadata_json={
                        "agent_role": event.agent_role,
                        "event_type": event.event_type,
                        **event.metadata,
                    },
                )
                s.add(m)
                await s.commit()
        except Exception:
            pass

    res = await orchestrator.execute_task(
        user_prompt=data.content,
        project_id=project.id,
        db=db,
        on_event=None,
    )

    if res.get("strategy_name"):
        strat = await db.scalar(select(Strategy).where(Strategy.slug == res["strategy_name"]))
        if strat:
            project.strategy_id = strat.id
            await db.commit()

    if res.get("backtest", {}).get("run_id"):
        project.latest_backtest_id = res["backtest"]["run_id"]
        project.status = ResearchStatus.RESULT_REVIEW
        await db.commit()

    return res

