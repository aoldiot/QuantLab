from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .agent.tools import get_strategy_code_tool, get_writing_log_tool
from .agent.strategy_verifier import extract_python_strategy_code
from .config import settings
from .db import get_db, SessionLocal
from .models import (
    BacktestRun,
    ResearchMessage,
    ResearchProject,
    ResearchStatus,
    RunStatus,
    SpecificationStatus,
    Strategy,
    StrategySpecification,
    StrategyVersion,
    AgentTask,
    WorkerType,
)
from .quant.strategy_manager import ensure_strategy_db_record
from .schemas import (
    DshActionRequest,
    DshApproveRequest,
    ResearchMessageCreate,
    ResearchProjectCreate,
)
from .strategy_contract import sanitize_strategy_slug
from .strategy_files import _path, save_strategy_code, PERSISTENT_STRATEGY_DIR, STRATEGY_DIR
from .research_workflow import apply_research_phase


router = APIRouter(prefix="/api/research", tags=["strategy-research"])
logger = logging.getLogger(__name__)

RESEARCH_INSTRUCTIONS = """你是 QuantLab 的首席量化负责人 (Quant Lead)，由 DeepSeek Harness (DSH) 驱动。
你的职责是与用户全流程完成量化策略的研讨、设计、开发、回测与归因分析。使用简体中文。
所有用户可见正文、阶段说明和工具调用前说明必须使用简体中文。不要向用户叙述“加载技能、阅读提示词、遵循系统指令”等内部准备过程；需要调用技能或工具时直接执行，并只报告对研究有意义的进展与结果。

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
   - 【严禁擅自直接写码】：当策略逻辑设计方案在正文中完整输出后，调用工具 `write_strategy_code` 提交完整策略代码（必须完整传入 `strategy_name` 与 `code`，严禁传递空参数 `{}`）。该工具会进入审批桩并返回 `awaiting_approval`，此时请立即结束本轮并向用户清晰说明待审批内容，等待用户批准。
   - 【用户批准后编写代码】：用户批准后平台会直接执行已审核的原始 `write_strategy_code` 参数并展示 L1–L4 结果；不要再次调用该工具，也不要再次要求审批。
   - 只有 `write_strategy_code` 已实际返回 `awaiting_approval` 时，才能声称页面存在审批操作。禁止仅在正文中虚构“批准并开始编写代码”按钮。
   - 【由 DSH 驱动 QuantLab 策略生成并完成 4 级沙盒自愈（严禁私自手写未经验证代码）】：
     用户在前端点击「批准并开始编写代码」或表达同意即代表已授予完全的代码写入权限，系统界面不存在二级的“批准写入”按钮！
     【极其关键】：编写策略文件必须通过 QuantLab 的 Strategy Manager 与沙盒验证机制执行。
     必须使用专用工具 `write_strategy_code`（参数含 `strategy_name` 与完整 `code`），经审批落盘后自动运行 4 级 Pre-Flight 运行期沙盒检测；如需复核可用只读工具 `verify_strategy_file`。任何校验失败（L1~L4）都必须在会话内针对报错自愈修复后重新校验，严禁绕开沙盒直接保存代码。
     严禁在回复中要求用户点击不存在的“批准写入按钮”或等待二次写入授权！
   - 【Pre-Flight 4 级全自动验证沙盒保证】：
     代码生成后系统会自动执行 4 级沙盒检测（L1 静态语法 -> L2 契约与类加载 -> L3 200根Bar指标计算覆盖与NaN检测 -> L4 Nautilus 运行时实例化与生命周期钩子）。若检测到错误会自动在会话内自愈修复，确保交付的代码直接可运行！

   - 【四大核心导出结构与代码规范（策略代码必须严格涵盖）】：
     编写的代码必须包含且仅包含以下四个标准导出结构，严禁遗漏：
     1. `StrategyConfig` 子类（继承自 `nautilus_trader.config.StrategyConfig`）：
        - 必须包含 `instrument_id: str` 和 `bar_type: str`。
        - 声明策略所需的全部参数，类型推荐使用 `int`, `float`, `str`, `bool`（字段名必须与 `STRATEGY_MANIFEST.parameters` 的 key 严格一致）。
     2. `Strategy` 子类（继承自 `nautilus_trader.trading.strategy.Strategy`）：
      - 【QuantLab 标杆策略范本（Golden Template）】：
      编写策略时请严格参考以下标准模板结构：
      ```python
      from decimal import Decimal
      import pandas as pd
      import numpy as np

      from nautilus_trader.config import StrategyConfig
      from nautilus_trader.model.data import Bar, BarType
      from nautilus_trader.model.identifiers import InstrumentId
      from app.strategy_base import QuantLabStrategy
      from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode
      from app.quant.indicators import (
          IncWilderADX,
          SqueezeStateTracker,
          ATRTrailingStopTracker,
          calc_standard_indicators,
      )


      class BtcEmaAtrConfig(StrategyConfig, frozen=True):
          instrument_id: InstrumentId
          bar_type: BarType
          fast_period: int = 12
          slow_period: int = 26
          atr_period: int = 14
          trade_size: Decimal = Decimal("0.01")


      class BtcEmaAtrStrategy(QuantLabStrategy):
          def on_bar(self, bar: Bar) -> None:
              super().on_bar(bar)
              if len(self.bars) < max(self.config.slow_period, self.config.atr_period) + 5:
                  return

              closes = self.get_close_series()
              fast_ma = closes.ewm(span=self.config.fast_period, adjust=False).mean().iloc[-1]
              slow_ma = closes.ewm(span=self.config.slow_period, adjust=False).mean().iloc[-1]
              prev_fast = closes.ewm(span=self.config.fast_period, adjust=False).mean().iloc[-2]
              prev_slow = closes.ewm(span=self.config.slow_period, adjust=False).mean().iloc[-2]

              # 自动探针记录指标供前端图表采集
              self.record("fast_ma", fast_ma)
              self.record("slow_ma", slow_ma)

              # 金叉做多（自动处理精度、工厂和提交）
              if prev_fast <= prev_slow and fast_ma > slow_ma and not self.is_long():
                  self.buy_market(trade_size=self.config.trade_size)
              # 死叉平多
              elif prev_fast >= prev_slow and fast_ma < slow_ma and self.is_long():
                  self.close_position()



      def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
          # 推荐直接调用内置向量化计算，自动补齐 fast_ma, slow_ma, atr, adx, bb, kc 等并消除 NaN
          return calc_standard_indicators(df, parameters)


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
              "trade_size": ParameterSpec(title="单笔下单数量", type="number", default=0.01, minimum=0.0001, maximum=100.0),
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
      )
      ```

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

   - 【NautilusTrader API 常见禁忌与标准用法（CRITICAL）】：
     - ❌ 严禁调用 `self.portfolio.account_balance()`（Portfolio 无此方法！如需获取账户净值请使用 `self.portfolio.equity(self.instrument_id.venue)`）。
     - ❌ 严禁调用 `self.portfolio.is_net_flat(...)`（正确方法为 `self.portfolio.is_flat(self.instrument_id)`）。
     - ❌ 严禁调用 `self.portfolio.position(...)`（正确方法为 `self.portfolio.net_position(self.instrument_id)` 或 `self.portfolio.is_flat(...)`）。
     - ❌ 严禁调用 `self.close_position(self.instrument_id)`（平仓标的必须使用 `self.close_all_positions(self.instrument_id)`）。
     - ❌ 严禁调用 `self.instrument.round_quantity(...)`（正确方法为 `self.instrument.make_qty(...)`，直接返回规范精度 Quantity）。
     - ❌ 严禁向订单 `quantity` 传递裸 float/int（必须使用 `Quantity` 或 `self.instrument.make_qty(...)`）。
     - ✅ 强烈推荐使用 `from app.quant.indicators import IncWilderADX, SqueezeStateTracker, ATRTrailingStopTracker, calc_standard_indicators`，大幅减少代码量并杜绝 Token 截断！


   - 【策略代码生成与规范】：必须由 DeepSeek Harness (DSH) Agent 生成完整策略代码，并通过 `write_strategy_code` 工具写入策略文件且确保通过 4 级 Pre-Flight 运行期沙盒校验。
   - 代码编写完成后，向用户汇报策略编写完成情况与 4 级验证摘要，等待用户进一步指令。在用户未明确说明要回测之前，严禁擅自生成回测方案。

4. 回测参数方案生成时机（CRITICAL - 必须用户明确要求回测，且必须先查验 Catalog 真实数据）：
   - 【严格限制生成时机】：只有当用户在对话中【明确提出要进行回测】（例如明确表达“进行回测”、“回测一下”、“运行回测”等意图）时，你才可以生成回测参数方案。
   - 【必须先查验 Catalog 真实可用数据】：在生成回测方案前，你必须调用工具 `quant_market_data_query`（通过 `dispatch_tool_call` 或直接调用）检查本地 Catalog 中已存在的交易标的、K线周期与历史起止时间，**严禁臆测本地不存在的远期时间区间**（否则会导致回测 0 根 Bar 空转）！
   - 在用户未明确要求回测之前（如策略讨论、代码编写完成阶段），严禁擅自生成回测参数方案，更严禁直接调用 `execute_backtest_tool` 执行回测！
   - 当用户要求回测时，根据策略 Manifest 规范与可用行情数据提出合理的回测参数，必须调用 `propose_backtest_params` 生成可编辑的参数方案卡片，然后停止并等待用户确认。只有该工具成功返回后才能声称“卡片已生成”。如果工具不可用，才可在正文末尾输出如下格式的 `backtest_params` 机器块作为兼容回退。此阶段不要调用 `execute_backtest_tool`：

```backtest_params
{
  "strategy_name": "当前编写的策略名（例如 btc_bollinger_regime_mr）",
  "symbols": ["BTCUSDT"],
  "timeframes": ["1h"],
  "start_date": "2024-01-01",
  "end_date": "2024-06-30",
  "initial_balance": 10000.0,
  "leverage": 1.0,
  "execution_model": "CONSERVATIVE",
  "parameters": {
    "bb_period": 20,
    "bb_k": 2.0,
    "atr_period": 14
  }
}
```
   - 用户可以点击卡片打开参数配置弹窗对标的、时间区间、资金、杠杆及策略参数进行修改，并在弹窗中确认。

5. 回测执行与结果监控（CRITICAL - 真实工具调度与禁止自动循环）：
   - 【严禁在终端运行回测】：严禁调用 terminal / bash 终端执行回测或运行测试脚本！任何在终端私自运行回测或编造数据的行为均被系统判定为无效。
   - 【必须调用 execute_backtest_tool 工具】：回测必须且只能由 QuantLab 主系统的 NautilusTrader 引擎执行。当收到用户确认回测参数的指令后，你必须调用工具 `execute_backtest_tool` 启动回测。
   - 【严禁虚构回测数据】：严禁在没有调用 `execute_backtest_tool` 工具的情况下在文本中凭空编造/虚构回测收益、夏普比率等结果！
   - 【禁止自动修改策略报错与自动分析结果（CRITICAL）】：
     - 当回测执行完成（无论成功与否），系统会将回测结果与指标卡片直接呈现在前端界面供用户查看。
     - **严禁在回测报错后自动修改代码！严禁在报错后自动反复调用 execute_backtest_tool 重新回测！**
     - **严禁在回测成功后自动输出冗长分析或自动进行参数调优！** 回测完成后必须等待用户确认后才进行下一步。

6. 策略报错单次受控修复模式（用户确认后执行 1 次，只改代码，禁止回测）：
   - 仅当用户在对话中明确确认修复报错（如点击「确认修复策略代码」或发送明确修复指令）时，你才执行 **1 次策略代码修复**（使用 `patch_strategy_candidate` 修复候选区报错代码并自动通过 4 级沙盒自愈，完成后向用户汇报）。
   - **【安全红线 - 严禁自动回测】**：代码修复完成后，**严禁自动调用 `execute_backtest_tool` 执行回测，严禁擅自生成回测参数卡片**！修复完成后仅简要向用户总结修复内容，等待用户下一步指令。

7. 回测结果单次受控归因分析模式（用户确认后执行 1 次，只分析原因，禁止改代码和回测）：
   - 仅当用户在对话中明确确认分析回测结果（如点击「确认进行回测深度分析」或发送分析指令）时，你才执行 **1 次深度回测归因分析**（分析收益率、夏普比率、最大回撤、胜率、盈亏比与市场行情适应性）。
   - **【安全红线 - 严禁改代码与回测】**：在归因分析时，**严格只分析指标与交易原因，严禁修改策略代码，严禁调用 `execute_backtest_tool` 启动回测**。
"""

RESEARCH_PHASE_INSTRUCTIONS = """你是 QuantLab 策略研究负责人。当前阶段严格限定为 RESEARCH（策略研究），使用简体中文。

目标：根据当前对话提出可审阅的《量化策略设计方案》，而不是理解或审计 QuantLab 项目源码。
你已经拥有 QuantLab 平台能力工具；不得使用终端、Bash、任意文件系统读取或加载 nautilus-strategy-author 开发技能。
研究规则已完整注入系统上下文，不要调用 skill 工具重复加载研究技能，也不要向用户报告加载技能等内部准备过程。
仅在确有必要时调用研究工具：平台能力、当前关联策略、行情数据、因子实验或联网资料。联网资料必须给出来源。
单轮最多调用 5 次工具；连续调用 3 次后必须停止探索并形成结论。不得读取 verifier、builder、strategy_base、指标库或其他项目内部实现。
若用户提到现有策略，只能通过 quant_get_strategy_context 获取该策略上下文。

最终必须输出完整 Markdown《量化策略设计方案》，至少包含：假设与收益来源、适用标的/周期、指标公式、入场/出场、风险与仓位、参数表、防过拟合建议，以及需要用户确认的下一步。
研究方案完成前不得写代码或执行正式回测。开发由独立 Coding Worker 执行；你的输出只需形成明确、可交接的研究规格。
无论资料是否完整，都必须基于已知信息给出明确结论；禁止以工具调用或内部准备说明作为最终答案。
"""


IMPLEMENTATION_PHASE_INSTRUCTIONS = """你是 QuantLab NautilusTrader 策略实现负责人。当前阶段严格限定为 IMPLEMENTATION（策略编码），使用简体中文。

用户已经完成策略方案确认并明确要求开始编码。不要重新输出研究方案，不要再次询问实现授权，不要执行回测，不要调用 skill 加载外部技能。
策略规范与契约已完全注入当前上下文；直接依据本轮用户提示中的需求与确认，生成完整、可运行的单文件 Python 策略。

必须满足以下交付契约：
1. 完整导出 StrategyConfig、Strategy、calculate_indicators、STRATEGY_MANIFEST，配置字段、Manifest 参数、指标列和 plot_config 必须一致。
2. 必须从 app.strategy_contract 导入并实例化 StrategyManifest（严禁写成原生 dict 字典）。
3. 禁止使用 portfolio.account_balance、portfolio.is_net_flat、portfolio.position、close_position、instrument.round_quantity；订单数量必须使用 Quantity 或 instrument.make_qty。
4. calculate_indicators 必须覆盖 plot_config 声明的全部列，并对头部 NaN 使用 bfill().fillna(0.0)。
5. strategy_path/config_path 必须使用 app.strategies.{slug}:ClassName；不得输出残缺代码、占位符或省略实现。
6. 必须使用 stage_strategy_candidate 在项目专属隔离候选区生成策略源码，并自动执行 4 级 Pre-Flight 运行期沙盒；若校验失败则使用 patch_strategy_candidate 定点精准修补，最多三轮；不得因为技术错误改变交易规则。禁止通过通用文件工具直接修改生产策略目录。
7. 你只能使用只读 coding-tools（read_file/search_code/list_files）查看白名单内的策略示例、契约与指标实现；不得使用终端或通用文件写入。
8. 4 级沙盒全部通过后，读取最终权威源码并调用 write_strategy_code 提交正式发布审批。只汇报最终 Diff、验证和烟雾回测结果。
"""


REPAIR_PHASE_INSTRUCTIONS = """你是 QuantLab NautilusTrader 策略诊断与修复专家。当前阶段严格限定为 REPAIR（策略检查与定向修复），使用简体中文。

你的唯一目标是：使用只读 coding-tools 查看白名单内的当前策略、契约与指标实现，并结合结构化报错定位根因，做最小修复并确保通过 Pre-Flight。
禁止重新讨论研究方案，禁止生成回测参数，禁止执行回测。

必须满足以下修复铁律（CRITICAL）：
1. 【四大核心导出声明】：
   - `class <SlugPascalCase>Config(StrategyConfig, frozen=True)`
   - `class <SlugPascalCase>Strategy(Strategy)`
   - `calculate_indicators(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame`：必须计算并返回 `plot_config` 中声明的全部指标列，且使用 `.bfill().fillna(0.0)` 处理头部 NaN。
   - `STRATEGY_MANIFEST = StrategyManifest(...)`：必须从 `app.strategy_contract` 导入 `StrategyManifest`, `ParameterSpec`, `StrategyMode` 并实例化对象（严禁写成原生 dict 字典！），`strategy_path` 与 `config_path` 必须带 `app.strategies.{slug}:` 前缀。
2. 【API 安全与合规】：
   - 禁止 `portfolio.account_balance`、`portfolio.is_net_flat`、`portfolio.position`、`close_position`、`instrument.round_quantity`。
   - 订单数量必须使用 `Quantity` 或 `instrument.make_qty`。
3. 【单轮闭环】：
   - 使用 read_strategy_candidate 读取候选区权威源码并使用 patch_strategy_candidate 进行局部定点编辑；禁止根据 Prompt 中的旧源码盲目重建整份策略。
   - 运行 Pre-Flight 获取结构化 diagnostics，并一次处理同批可确定问题；每轮补丁后重新运行 Pre-Flight。
   - 最多三轮。同一错误重复出现两次时读取真实契约实现，不再猜测；三次仍失败则输出框架缺陷报告。
   - 校验通过后调用 write_strategy_code 同步版本记录并结束，不生成审批卡，不执行正式回测。
"""


BACKTEST_PHASE_INSTRUCTIONS = """你是 QuantLab 回测执行负责人。当前阶段严格限定为 BACKTEST，使用简体中文。

你的职责仅限于读取当前策略上下文、查询 Catalog 中真实可用的行情范围、生成可编辑回测参数卡，或在用户已确认参数后提交正式回测。
必须先调用 quant_market_data_query 核对标的、周期与日期范围；不得猜测数据范围。
不得读取项目文件、修改策略、生成策略代码、运行终端命令、执行参数扫描或分析回测结果。
首次提出参数时调用 propose_backtest_params 后立即停止；只有用户明确确认参数后才可调用 execute_backtest_tool。
"""


ANALYSIS_PHASE_INSTRUCTIONS = """你是 QuantLab 回测结果归因负责人。当前阶段严格限定为 RESULT_REVIEW，使用简体中文。

你的职责仅限于基于指定回测的真实指标与交易结果解释收益来源、亏损来源、市场适应性、风险和证据限制。
可以读取只读回测产物，并使用受限的策略上下文或稳健性分析工具；不得修改策略、生成参数、执行回测、运行参数扫描或使用终端。
必须区分单区间回测、因子实验和稳健性证据；未执行的检验必须明确标注为限制，不得虚构结果。
最终直接输出简洁、可执行的归因结论。
"""


def _instructions_for_phase(phase: str) -> str:
    normalized = (phase or "").upper()
    if normalized == "RESEARCH":
        return RESEARCH_PHASE_INSTRUCTIONS
    if normalized in {"IMPLEMENTATION", "IMPLEMENTED"}:
        return IMPLEMENTATION_PHASE_INSTRUCTIONS
    if normalized in {"REPAIR", "FIX_ERROR"}:
        return REPAIR_PHASE_INSTRUCTIONS
    if normalized in {"BACKTEST", "BACKTEST_RETRY", "AWAITING_BACKTEST_APPROVAL"}:
        return BACKTEST_PHASE_INSTRUCTIONS
    if normalized == "RESULT_REVIEW":
        return ANALYSIS_PHASE_INSTRUCTIONS
    return RESEARCH_INSTRUCTIONS


INTENT_PHASES = {
    "DISCUSS_STRATEGY": "RESEARCH",
    "MODIFY_STRATEGY_PLAN": "RESEARCH",
    "START_IMPLEMENTATION": "IMPLEMENTATION",
    "MODIFY_STRATEGY_CODE": "REPAIR",
    "REQUEST_BACKTEST": "BACKTEST",
    "MODIFY_BACKTEST_PARAMS": "BACKTEST",
    "ANALYZE_BACKTEST": "RESULT_REVIEW",
    "VIEW_STRATEGY_CODE": "RESULT_REVIEW",
}


def _implementation_prompt(
    project: ResearchProject,
    user_confirmation: str,
    approved_plan: str = "",
    candidate_code: str = "",
    original_idea: str = "",
) -> str:
    eff_idea = (original_idea or project.original_idea or "").strip()
    candidate_section = (
        f"\n\n【已有候选源码草稿（已通过 4 级 Pre-Flight 校验）】\n```python\n{candidate_code}\n```\n"
        "如已有草稿完全符合要求，可直接调用 write_strategy_code 提交正式发布审批，或调用 stage_strategy_candidate 进行微调。"
        if candidate_code
        else ""
    )
    return (
        "用户已明确确认进入策略编码阶段。请生成完整策略代码，第一步直接调用 stage_strategy_candidate 提交策略源码并完成 4 级 Pre-Flight 校验；"
        "禁止在写码前调用 list_files/search_code 做多余的漫游检索；"
        "失败时只用 patch_strategy_candidate 修复报错片段；通过后再调用 write_strategy_code 提交真实发布审批。"
        "不要重新研究、不要再次询问授权、不要执行回测。\n\n"
        f"【完整原始需求】\n{eff_idea}\n\n"
        f"【已确认的最新研究方案】\n{approved_plan.strip()}\n\n"
        f"【用户最新确认与范围调整】\n{user_confirmation.strip()}"
        f"{candidate_section}"
    )


async def _resolve_implementation_context(
    project: ResearchProject,
    db: AsyncSession,
) -> tuple[str, str, str]:
    """Retrieve robust original idea, substantive research plan, and existing candidate code."""
    from .models import CandidateRevision

    # 1. Resolve original idea
    original_idea = (getattr(project, "original_idea", "") or "").strip()
    if not original_idea and hasattr(db, "scalar"):
        try:
            first_user_msg = await db.scalar(
                select(ResearchMessage.content)
                .where(ResearchMessage.project_id == project.id, ResearchMessage.role == "user")
                .order_by(ResearchMessage.created_at.asc())
                .limit(1)
            )
            if first_user_msg and first_user_msg.strip():
                original_idea = first_user_msg.strip()
                project.original_idea = original_idea
        except Exception:
            pass

    # 2. Resolve substantive approved plan
    approved_plan = ""
    if hasattr(db, "scalars"):
        try:
            specs = list((await db.scalars(
                select(StrategySpecification)
                .where(StrategySpecification.project_id == project.id)
                .order_by(StrategySpecification.version.asc())
            )).all())
            for spec in reversed(specs):
                content = spec.content or {}
                plan = str(content.get("approved_plan") or "").strip()
                if len(plan) > 100:
                    approved_plan = plan
                    break

            if not approved_plan:
                assistant_msgs = list((await db.scalars(
                    select(ResearchMessage)
                    .where(ResearchMessage.project_id == project.id, ResearchMessage.role == "assistant")
                    .order_by(ResearchMessage.created_at.desc())
                    .limit(15)
                )).all())
                for msg in assistant_msgs:
                    text = (msg.content or "").strip()
                    if len(text) > 100:
                        approved_plan = text
                        break
                if not approved_plan and assistant_msgs:
                    approved_plan = (assistant_msgs[0].content or "").strip()
        except Exception:
            pass

    # 3. Check for existing candidate revision code
    candidate_code = ""
    if hasattr(db, "scalar"):
        try:
            latest_candidate = await db.scalar(
                select(CandidateRevision)
                .where(CandidateRevision.project_id == project.id)
                .order_by(CandidateRevision.created_at.desc())
                .limit(1)
            )
            candidate_code = (latest_candidate.code or "").strip() if latest_candidate else ""
        except Exception:
            pass

    return original_idea, approved_plan, candidate_code


async def _recent_intent_context(
    project_id: str,
    db: AsyncSession,
    limit: int = 12,
) -> list[dict[str, str]]:
    rows = (
        await db.scalars(
            select(ResearchMessage)
            .where(
                ResearchMessage.project_id == project_id,
                ResearchMessage.role.in_(["user", "assistant"]),
            )
            .order_by(ResearchMessage.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        {"role": row.role, "content": (row.content or "")[-6000:]}
        for row in reversed(rows)
    ]


async def _approve_research_specification(
    project: ResearchProject,
    approved_plan: str,
    db: AsyncSession,
) -> StrategySpecification:
    """Freeze the exact research plan used as implementation input."""
    existing = (
        await db.scalars(
            select(StrategySpecification)
            .where(StrategySpecification.project_id == project.id)
            .order_by(StrategySpecification.version.desc())
        )
    ).all()
    for item in existing:
        if item.status == SpecificationStatus.APPROVED:
            item.status = SpecificationStatus.SUPERSEDED
    spec = StrategySpecification(
        project_id=project.id,
        version=(existing[0].version + 1) if existing else 1,
        status=SpecificationStatus.APPROVED,
        content={
            "approved_plan": approved_plan.strip(),
            "original_idea": (project.original_idea or "").strip(),
            "frozen_at": datetime.now(UTC).isoformat(),
        },
        approved_at=datetime.now(UTC),
    )
    db.add(spec)
    await db.commit()
    await db.refresh(spec)
    return spec


def _build_auto_repair_prompt(
    strategy_name: str,
    candidate_code: str,
    verification: dict[str, Any],
    attempt: int,
    max_attempts: int,
) -> str:
    verification_json = json.dumps(verification, ensure_ascii=False, default=str)
    return f"""这是框架自动启动的策略契约修复回合（第 {attempt}/{max_attempts} 次）。
策略名：{strategy_name}

【Pre-Flight 结构化错误】
{verification_json}

【已落盘到项目隔离候选区的失败源码副本】
```python
{candidate_code}
```

只修复上述校验错误以及由同一契约直接暴露的问题，不改变交易假设、信号、参数默认值或风险规则。
必须保持完整单文件，并严格满足当前 QuantLab 合同：
1. 从 app.strategy_contract 导入并实例化 StrategyManifest、ParameterSpec、StrategyMode。
2. STRATEGY_MANIFEST.parameters 必须是 dict[str, ParameterSpec]，不能是 list 或原生 dict 参数项。
3. plot_config 必须使用 main_plot/subplots 双层字典，且 calculate_indicators 覆盖全部声明列。
4. strategy_path/config_path 必须指向当前模块的实际 Strategy/Config 类。
5. 禁止使用被平台列入黑名单的 Nautilus API。

先调用 read_strategy_candidate 读取候选区权威源码，再用 patch_strategy_candidate 仅修复错误片段；每次补丁都会重跑 Pre-Flight。
全部通过后再次读取最终源码，调用 write_strategy_code 提交正式发布审批。收到 awaiting_approval 后立即停止；禁止回测。
"""


async def _auto_repair_attempt_count(project_id: str, db: AsyncSession) -> int:
    rows = (
        await db.scalars(
            select(ResearchMessage).where(
                ResearchMessage.project_id == project_id,
                ResearchMessage.message_type == "system",
            )
        )
    ).all()
    return sum(
        1
        for row in rows
        if (row.metadata_json or {}).get("event_type") == "auto_repair_started"
    )


def _intent_routed_prompt(intent_result: dict[str, Any], user_message: str) -> str:
    normalized = intent_result.get("normalized_request") or user_message
    return (
        "【DSH 意图管理结果】\n"
        f"意图：{intent_result['intent']}\n"
        f"规范化请求：{normalized}\n\n"
        "请依据该意图与当前项目上下文完成本轮任务；卡片必须由真实工具调用产生。\n\n"
        f"【用户原始输入】\n{user_message}"
    )


_FAST_INTENT_COMMANDS = {
    "开始编写策略代码": "START_IMPLEMENTATION",
    "编写策略代码": "START_IMPLEMENTATION",
    "执行回测": "REQUEST_BACKTEST",
    "运行回测": "REQUEST_BACKTEST",
    "修复策略代码": "MODIFY_STRATEGY_CODE",
    "修复报错": "MODIFY_STRATEGY_CODE",
    "回测结果分析": "ANALYZE_BACKTEST",
    "分析回测结果": "ANALYZE_BACKTEST",
}


def _fast_intent_decision(user_message: str, pending: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Route only exact, unambiguous commands; nuanced text still goes to DSH."""
    normalized = re.sub(r"[\s。！!，,]+", "", user_message).strip()
    if pending and normalized in {"批准", "确认", "同意", "批准并继续"}:
        if len(pending) != 1:
            return None
        intent = "APPROVE_PENDING_ACTION"
        request_id = str(pending[0].get("request_id") or "")
    elif pending and normalized in {"拒绝", "取消", "不同意"}:
        if len(pending) != 1:
            return None
        intent = "REJECT_PENDING_ACTION"
        request_id = str(pending[0].get("request_id") or "")
    elif not pending and normalized in _FAST_INTENT_COMMANDS:
        intent = _FAST_INTENT_COMMANDS[normalized]
        request_id = ""
    else:
        return None
    return {
        "intent": intent,
        "confidence": 1.0,
        "normalized_request": user_message.strip(),
        "needs_clarification": False,
        "clarification_question": "",
        "pending_request_id": request_id,
        "reason": "明确的固定动作指令，使用本地快速路由。",
    }


_ACTION_INSTRUCTIONS = {
    "WRITE_STRATEGY": "本轮是固定写码动作。直接实现，不重复研究；最多进行一次集中验证，随后提交 write_strategy_code 审批并停止。",
    "RUN_BACKTEST": "本轮是固定回测动作。只补齐回测参数并生成参数卡，不讨论策略方案，不修改代码，不做结果分析。",
    "FIX_ERROR": "本轮是单次定向修复。只读取指定失败记录和当前策略，完成一次修复与集中验证后提交 write_strategy_code；禁止回测。",
    "ANALYZE_BACKTEST": "本轮只分析指定回测记录。不得修改代码、生成参数或重新回测；直接输出简洁归因结论。",
}


def _instructions_for_task(phase: str, task_profile: str = "") -> str:
    base = _instructions_for_phase(phase)
    focused = _ACTION_INSTRUCTIONS.get(task_profile)
    return f"{base}\n\n【固定动作约束】\n{focused}" if focused else base

ACTIVE_RESEARCH_TASKS: dict[str, asyncio.Task[Any]] = {}
RESEARCH_THINKING_STATUS: dict[str, dict[str, Any]] = {}


def _set_thinking_status(project_id: str, status: str, step: str, thought: str = ""):
    RESEARCH_THINKING_STATUS[project_id] = {
        "status": status,
        "step": step,
        "thought": thought,
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _project_out(project: ResearchProject) -> dict[str, Any]:
    task = ACTIVE_RESEARCH_TASKS.get(project.id)
    is_busy = task is not None and not task.done()
    return {
        "id": project.id,
        "client_id": project.client_id,
        "title": project.title,
        "original_idea": project.original_idea,
        "status": project.status.value,
        "research_phase": project.research_phase or "RESEARCH",
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


async def _build_session_handoff(source: ResearchProject, db: AsyncSession) -> str:
    """Build a bounded, deterministic handoff without copying conversation history."""
    strategy = await db.get(Strategy, source.strategy_id) if source.strategy_id else None
    latest_run = await db.get(BacktestRun, source.latest_backtest_id) if source.latest_backtest_id else None
    recent_rows = (
        await db.scalars(
            select(ResearchMessage)
            .where(
                ResearchMessage.project_id == source.id,
                ResearchMessage.role.in_(["user", "assistant"]),
            )
            .order_by(ResearchMessage.created_at.desc())
            .limit(6)
        )
    ).all()

    lines = [
        "## 会话交接摘要",
        "",
        "这是从上一轮研究生成的结构化背景。新会话拥有独立上下文，不会复制完整历史消息。",
        "",
        f"- 来源会话：{source.title}",
        f"- 当前阶段：{source.research_phase or 'RESEARCH'}",
    ]
    if strategy:
        lines.append(f"- 关联策略：{strategy.name}（`{strategy.slug}`）")
    if source.original_idea:
        lines.extend(["", "### 原始策略目标", source.original_idea[:1_500]])
    if source.conclusion_summary or source.conclusion_next_step:
        lines.extend([
            "",
            "### 已确认结论",
            source.conclusion_summary or "暂无正式结论",
        ])
        if source.conclusion_next_step:
            lines.append(f"下一步：{source.conclusion_next_step}")
    if latest_run:
        lines.extend([
            "",
            "### 最近回测",
            f"- 回测 ID：`{latest_run.id}`",
            f"- 状态：{latest_run.status.value} · {latest_run.stage}",
            f"- 配置：`{json.dumps(latest_run.config or {}, ensure_ascii=False, default=str)[:1_500]}`",
            f"- 指标：`{json.dumps(latest_run.metrics or {}, ensure_ascii=False, default=str)[:1_500]}`",
        ])
    if recent_rows:
        lines.extend(["", "### 最近决策摘要"])
        for row in reversed(recent_rows):
            label = "用户" if row.role == "user" else "Quant Lead"
            compact = re.sub(r"\s+", " ", row.content or "").strip()[:700]
            if compact:
                lines.append(f"- {label}：{compact}")
    return "\n".join(lines)[:8_000]


async def _session_handoff_context(project_id: str, db: AsyncSession) -> str:
    rows = (
        await db.scalars(
            select(ResearchMessage)
            .where(
                ResearchMessage.project_id == project_id,
                ResearchMessage.role == "assistant",
            )
            .order_by(ResearchMessage.created_at.desc())
            .limit(20)
        )
    ).all()
    for row in rows:
        if (row.metadata_json or {}).get("event_type") == "session_handoff":
            return (row.content or "")[:8_000]
    return ""


async def _project(project_id: str, db: AsyncSession) -> ResearchProject:
    project = await db.get(ResearchProject, project_id)
    if not project:
        raise HTTPException(404, "研究项目不存在")
    return project


async def _sync_strategy_code_if_present(
    text: str,
    project: ResearchProject,
    db: AsyncSession,
) -> str | None:
    """Helper to detect full python strategy code in text and auto-save/sync to DB.
    Never overwrite with partial snippets or dummy names.
    """
    if not text:
        return None
    if "STRATEGY_MANIFEST" in text and "StrategyConfig" in text and "calculate_indicators" in text and "Strategy" in text:
        py_blocks = re.findall(r"```python\s*(.*?)\s*```", text, re.DOTALL)
        for block in py_blocks:
            if (
                len(block) > 500
                and "STRATEGY_MANIFEST" in block
                and "StrategyConfig" in block
                and "calculate_indicators" in block
                and "Strategy" in block
            ):
                slug_m = re.search(r'slug\s*=\s*["\']([a-zA-Z0-9_\-]+)["\']', block)
                s_slug = sanitize_strategy_slug(slug_m.group(1)) if slug_m else "custom_strategy"
                try:
                    ast.parse(block)
                    save_strategy_code(s_slug, block)
                    strat_rec = await ensure_strategy_db_record(s_slug, db, project_id=project.id)
                    if strat_rec and strat_rec[0]:
                        project.strategy_id = strat_rec[0].id
                        project.updated_at = datetime.now(UTC)
                        await db.commit()
                    return s_slug
                except Exception:
                    pass

    return None


def _start_dsh_turn(
    project: ResearchProject,
    content: str,
    thought: str | None = None,
    step: str = "DSH Quant Lead 正在拆解任务并调度研究闭环...",
    phase: str | None = None,
    task_profile: str = "",
    agent_task_id: str | None = None,
) -> asyncio.Task[None]:
    """Kick off one DSH orchestrator turn in the background.

    DSH is the single orchestrator driving research -> code -> backtest; QuantLab
    stays the gatekeeper (HTTP bridge + interactive approval registry). The task is
    tracked in ACTIVE_RESEARCH_TASKS so project status surfaces as busy.
    """
    from app.dsh import engine as dsh_engine

    resolved_phase = phase or project.research_phase or "RESEARCH"

    async def _worker() -> None:
        _set_thinking_status(
            project.id,
            "THINKING",
            step,
            thought or "",
        )
        try:
            if agent_task_id:
                from .workflow.task_service import start_task
                async with SessionLocal() as s:
                    durable_task = await s.get(AgentTask, agent_task_id)
                    if durable_task:
                        await start_task(s, durable_task, dsh_engine._sdk_session_id(project, resolved_phase))
            res = await dsh_engine.run_turn(
                project,
                content,
                system_instructions=_instructions_for_task(resolved_phase, task_profile),
                phase=resolved_phase,
                task_profile=task_profile,
            )
        except Exception as exc:
            logger.error("DSH 回合异常 (project=%s): %s", project.id, exc, exc_info=True)
            _set_thinking_status(project.id, "IDLE", "DSH 回合异常", f"运行出错：{exc}")
            if agent_task_id:
                from .workflow.task_service import fail_task
                async with SessionLocal() as s:
                    durable_task = await s.get(AgentTask, agent_task_id)
                    if durable_task:
                        await fail_task(s, durable_task, "DSH_RUN_EXCEPTION", str(exc))
            return
        final_text = res.get("final_response") if res.get("ok") else ""
        if final_text and not res.get("final_response_persisted"):
            async with SessionLocal() as s:
                s.add(
                    ResearchMessage(
                        project_id=project.id,
                        role="assistant",
                        content=final_text,
                        message_type="message",
                        metadata_json={"agent_role": "lead", "event_type": "message", "is_dsh_run": True},
                    )
                )
                await s.commit()
        if not res.get("ok"):
            error = res.get("error") or "DSH 未生成最终研究结论"
            async with SessionLocal() as s:
                s.add(ResearchMessage(
                    project_id=project.id,
                    role="system",
                    content=error,
                    message_type="error",
                    metadata_json={"is_dsh_run": True, "error_code": res.get("error_code", "DSH_RUN_FAILED")},
                ))
                await s.commit()
            _set_thinking_status(project.id, "FAILED", "DSH 回合未完成", error)
            if agent_task_id:
                from .workflow.error_router import classify_error
                from .workflow.task_service import fail_task
                route = classify_error(error)
                async with SessionLocal() as s:
                    durable_task = await s.get(AgentTask, agent_task_id)
                    if durable_task:
                        await fail_task(s, durable_task, route.code, error)
        elif res.get("pending"):
            _set_thinking_status(project.id, "WAITING_APPROVAL", "等待你的审批", res["pending"][0].get("summary", "DSH 提交了待确认操作"))
        else:
            _set_thinking_status(project.id, "IDLE", "就绪", "")
            if agent_task_id:
                from .workflow.task_service import complete_task
                async with SessionLocal() as s:
                    durable_task = await s.get(AgentTask, agent_task_id)
                    if durable_task:
                        await complete_task(s, durable_task, res)

    task = asyncio.create_task(_worker())
    ACTIVE_RESEARCH_TASKS[project.id] = task
    task.add_done_callback(lambda t, pid=project.id: ACTIVE_RESEARCH_TASKS.pop(pid, None) if ACTIVE_RESEARCH_TASKS.get(pid) is t else None)
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
    source: ResearchProject | None = None
    if data.source_project_id:
        source = await db.get(ResearchProject, data.source_project_id)
        if source is None:
            raise HTTPException(404, "用于续接的历史会话不存在")
        if source.client_id != data.client_id:
            raise HTTPException(403, "不能续接其他用户的研究会话")
        if not source.strategy_id:
            raise HTTPException(409, "该历史会话尚未关联策略，不能作为续接来源")

    inherited_phase = source.research_phase if source else "RESEARCH"
    if inherited_phase not in {"RESEARCH", "IMPLEMENTATION", "BACKTEST", "RESULT_REVIEW"}:
        inherited_phase = "IMPLEMENTATION" if source else "RESEARCH"
    project = ResearchProject(
        client_id=data.client_id,
        title=data.title,
        original_idea=data.original_idea,
        conversation_id=f"quantlab-research-{uuid.uuid4()}",
        strategy_id=source.strategy_id if source else None,
        latest_backtest_id=source.latest_backtest_id if source else None,
        research_phase=inherited_phase,
    )
    db.add(project)
    await db.flush()
    if source:
        db.add(ResearchMessage(
            project_id=project.id,
            role="assistant",
            content=await _build_session_handoff(source, db),
            message_type="message",
            metadata_json={
                "event_type": "session_handoff",
                "source_project_id": source.id,
                "strategy_id": source.strategy_id,
                "is_dsh_run": False,
            },
        ))
    await db.commit()
    await db.refresh(project)

    # Every user-authored request, including the initial idea, goes through the
    # same DSH intent manager before a business phase is selected.
    if data.original_idea and data.original_idea.strip():
        idea = data.original_idea.strip()
        await run_dsh_pipeline_endpoint(
            project.id,
            ResearchMessageCreate(content=idea),
            db,
        )

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
    content = data.content.strip()
    if not content:
        raise HTTPException(400, "消息内容不能为空")
    await run_dsh_pipeline_endpoint(
        project_id,
        ResearchMessageCreate(content=content),
        db,
    )
    user_msg = await db.scalar(
        select(ResearchMessage)
        .where(
            ResearchMessage.project_id == project_id,
            ResearchMessage.role == "user",
        )
        .order_by(ResearchMessage.created_at.desc())
        .limit(1)
    )
    return [_message_out(user_msg)]


@router.get("/{project_id}/backtests")
async def list_project_backtests(project_id: str, db: AsyncSession = Depends(get_db)):
    project = await _project(project_id, db)
    scope = BacktestRun.research_project_id == project_id
    if project.latest_backtest_id:
        scope = or_(scope, BacktestRun.id == project.latest_backtest_id)
    rows = (
        await db.scalars(
            select(BacktestRun)
            .where(scope)
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
    """Get the current DSH execution stage without fabricating model reasoning."""
    await _project(project_id, db)
    fallback = RESEARCH_THINKING_STATUS.get(
        project_id,
        {"status": "IDLE", "step": "就绪", "thought": "", "updated_at": datetime.now(UTC).isoformat()},
    )
    from app.dsh import engine as dsh_engine

    live = dsh_engine.get_status(project_id)
    if live.get("status") in {"THINKING", "TOOL_RUNNING", "GENERATING", "WAITING_APPROVAL", "FAILED"}:
        return {
            "status": live["status"],
            "step": live.get("stage") or fallback.get("step", "DSH 正在执行"),
            "thought": live.get("error", "") if live.get("status") == "FAILED" else "",
            "updated_at": live.get("updated_at") or fallback.get("updated_at"),
            "phase": live.get("metrics", {}).get("phase", "RESEARCH"),
            "metrics": live.get("metrics", {}),
            "error": live.get("error", ""),
        }
    return {**fallback, "phase": live.get("metrics", {}).get("phase", "RESEARCH"), "metrics": live.get("metrics", {}), "error": live.get("error", "")}


@router.get("/{project_id}/dsh/events")
async def get_project_dsh_events(project_id: str, db: AsyncSession = Depends(get_db)):
    """Return the latest DSH turn's real SDK events for the polling live UI."""
    await _project(project_id, db)
    from app.dsh import engine as dsh_engine

    return {
        "events": dsh_engine.get_live_session_events(project_id),
        "status": dsh_engine.get_status(project_id),
    }


@router.get("/{project_id}/strategy")
async def get_project_strategy(
    project_id: str,
    strategy_name: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Get the strategy source code and manifest for this research project."""
    project = await _project(project_id, db)

    target_name = strategy_name

    # 1. If project has strategy_id linked, check DB Strategy & StrategyVersion first
    if not target_name and project.strategy_id:
        strat = await db.get(Strategy, project.strategy_id)
        if strat:
            target_name = strat.slug
            res = get_strategy_code_tool(target_name)
            if res.get("ok") and res.get("code"):
                return res
            # Fallback to DB StrategyVersion.code
            await db.refresh(strat, ["versions"])
            for v in sorted(strat.versions, key=lambda x: x.created_at or datetime.min, reverse=True):
                if v.code and v.code.strip():
                    save_strategy_code(strat.slug, v.code)
                    return {
                        "ok": True,
                        "strategy_name": strat.slug,
                        "code": v.code,
                    }

    # 2. Search messages for strategy_name
    if not target_name:
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
                    s_m = re.search(r'策略名称[：:\s]+([a-zA-Z0-9_\-]+)', str(t_msg.content) + " " + str(t_msg.metadata_json))
                    if s_m:
                        s_name = s_m.group(1)
                if not s_name and "app/strategies/" in t_msg.content:
                    p_match = re.search(r'app/strategies/([a-zA-Z0-9_\-]+)\.py', t_msg.content)
                    if p_match:
                        s_name = p_match.group(1)
                if s_name:
                    target_name = s_name
                    break

    # 3. If target_name found, try disk or DB versions with candidate variations
    if target_name:
        clean_slug = sanitize_strategy_slug(target_name)
        candidates = list(dict.fromkeys(filter(None, [
            target_name,
            clean_slug,
            target_name.replace("-", "_"),
            target_name.replace("_", "-"),
            clean_slug.replace("_", "-"),
        ])))
        for c_name in candidates:
            res = get_strategy_code_tool(c_name)
            if res.get("ok") and res.get("code"):
                strat_rec = await ensure_strategy_db_record(c_name, db, project_id=project.id)
                if strat_rec and strat_rec[0] and not project.strategy_id:
                    project.strategy_id = strat_rec[0].id
                    project.updated_at = datetime.now(UTC)
                    await db.commit()
                return res

        # Fallback to query Strategy table in DB
        db_strat = await db.scalar(select(Strategy).where(Strategy.slug.in_(candidates)))
        if db_strat:
            await db.refresh(db_strat, ["versions"])
            for v in sorted(db_strat.versions, key=lambda x: x.created_at or datetime.min, reverse=True):
                if v.code and v.code.strip():
                    save_strategy_code(clean_slug, v.code)
                    if not project.strategy_id:
                        project.strategy_id = db_strat.id
                        project.updated_at = datetime.now(UTC)
                        await db.commit()
                    return {
                        "ok": True,
                        "strategy_name": db_strat.slug,
                        "code": v.code,
                    }

    # 4. Fallback search across STRATEGY_DIR and PERSISTENT_STRATEGY_DIR for recent or matching strategy files
    for p_dir in (STRATEGY_DIR, PERSISTENT_STRATEGY_DIR):
        if p_dir.exists():
            cand_files = sorted(
                [f for f in p_dir.glob("*.py") if not f.name.startswith("__")],
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )
            for f in cand_files:
                f_slug = sanitize_strategy_slug(f.stem)
                if (
                    (project.title and f_slug in sanitize_strategy_slug(project.title))
                    or (f.stat().st_mtime > project.created_at.timestamp() - 600)
                ):
                    c_text = f.read_text(encoding="utf-8")
                    if len(c_text) > 500 and "STRATEGY_MANIFEST" in c_text:
                        strat_rec = await ensure_strategy_db_record(f_slug, db, project_id=project.id)
                        if strat_rec and strat_rec[0] and not project.strategy_id:
                            project.strategy_id = strat_rec[0].id
                            project.updated_at = datetime.now(UTC)
                            await db.commit()
                        return {
                            "ok": True,
                            "strategy_name": f_slug,
                            "code": c_text,
                        }

    # 5. Fallback search in assistant messages for Python code blocks
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
        code_block = extract_python_strategy_code(m.content)
        if (
            len(code_block) > 200
            and "STRATEGY_MANIFEST" in code_block
            and "StrategyConfig" in code_block
            and "calculate_indicators" in code_block
            and "Strategy" in code_block
        ):
            slug_match = re.search(r'slug\s*=\s*["\']([a-zA-Z0-9_\-]+)["\']', code_block)
            derived_slug = sanitize_strategy_slug(slug_match.group(1)) if slug_match else "custom_strategy"
            try:
                ast.parse(code_block)
                save_strategy_code(derived_slug, code_block)
                strat_rec = await ensure_strategy_db_record(derived_slug, db, project_id=project.id)
                if strat_rec and strat_rec[0] and not project.strategy_id:
                    project.strategy_id = strat_rec[0].id
                    project.updated_at = datetime.now(UTC)
                    await db.commit()
                return {
                    "ok": True,
                    "strategy_name": derived_slug,
                    "code": code_block,
                }
            except Exception:
                pass

    return {"ok": False, "message": "尚未生成策略代码"}


@router.get("/{project_id}/export")
async def export_research_project(
    project_id: str,
    format: str = "markdown",
    db: AsyncSession = Depends(get_db),
):
    """Export complete research dialogue, DSH prompts, reasoning, tool calls, sandbox logs, and strategy code."""
    project = await _project(project_id, db)

    # 1. Fetch messages
    messages_rows = (
        await db.scalars(
            select(ResearchMessage)
            .where(ResearchMessage.project_id == project.id)
            .order_by(ResearchMessage.created_at)
        )
    ).all()

    # 2. Fetch DSH events
    from app.dsh import engine as dsh_engine
    dsh_events = dsh_engine.get_session_events(project.id)

    # 3. Fetch writing log
    writing_log = get_writing_log_tool(project.id)

    # 4. Fetch Strategy Code
    strategy_info = await get_project_strategy(project.id, db=db)
    strategy_slug = strategy_info.get("strategy_name") if isinstance(strategy_info, dict) else None
    strategy_code = strategy_info.get("code") if isinstance(strategy_info, dict) else ""

    # 5. Fetch Backtests
    backtests_data = await list_project_backtests(project.id, db=db)

    clean_title = sanitize_strategy_slug(project.title) or "research"
    now_str = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

    if format.lower() == "json":
        data = {
            "project": _project_out(project),
            "system_prompt": RESEARCH_INSTRUCTIONS.strip(),
            "messages": [_message_out(m) for m in messages_rows],
            "dsh_events": dsh_events,
            "writing_log": writing_log,
            "strategy": {
                "slug": strategy_slug,
                "code": strategy_code,
            },
            "backtests": backtests_data,
            "exported_at": datetime.now(UTC).isoformat(),
        }
        filename = f"quantlab_research_{clean_title}_{now_str}.json"
        content_bytes = json.dumps(data, indent=2, ensure_ascii=False, default=str).encode("utf-8")
        return Response(
            content=content_bytes,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # Default format: Markdown
    md_lines = []
    md_lines.append("# 量化策略全流程研究与 DSH 调试审计报告")
    md_lines.append("")
    md_lines.append(f"- **策略/项目名称**: {project.title}")
    md_lines.append(f"- **研究项目 ID**: `{project.id}`")
    md_lines.append(f"- **创建时间**: {project.created_at.isoformat() if project.created_at else ''}")
    md_lines.append(f"- **当前状态**: `{project.status.value}`")
    md_lines.append(f"- **关联策略标识**: `{strategy_slug or '未关联'}`")
    md_lines.append(f"- **导出时间**: {datetime.now(UTC).isoformat()}")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")
    md_lines.append("## 一、 策略研究假设与背景")
    md_lines.append(project.original_idea or "（未填写初始量化假设）")
    if project.conclusion_summary:
        md_lines.append(f"\n- **研究结论评估**: {project.conclusion_summary}")
    if project.conclusion_next_step:
        md_lines.append(f"- **下一步推进建议**: {project.conclusion_next_step}")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")
    md_lines.append("## 二、 DSH 首席量化架构师系统提示词 (System Prompt)")
    md_lines.append("```markdown")
    md_lines.append(RESEARCH_INSTRUCTIONS.strip())
    md_lines.append("```")
    md_lines.append("")
    md_lines.append("---")
    md_lines.append("")
    md_lines.append("## 三、 完整对话研讨、提示词与工具调用流")
    md_lines.append("")

    for idx, msg in enumerate(messages_rows, 1):
        ts = msg.created_at.strftime("%Y-%m-%d %H:%M:%S") if msg.created_at else ""
        role_label = msg.role.upper()
        if msg.role == "assistant":
            role_label = "ASSISTANT (Quant Lead)"
        elif msg.role == "tool":
            role_label = "TOOL RESULT"
        elif msg.message_type == "tool_call":
            role_label = "TOOL CALL"

        md_lines.append(f"### [{idx}] {ts} · `{role_label}`")

        # Check reasoning / CoT
        reasoning = ""
        if isinstance(msg.metadata_json, dict):
            reasoning = msg.metadata_json.get("reasoning") or msg.metadata_json.get("thought") or ""
        if reasoning:
            md_lines.append("> **DeepSeek CoT 思考链 (Reasoning)**:")
            for r_line in reasoning.splitlines():
                md_lines.append(f"> {r_line}")
            md_lines.append("")

        # Message content / tool call details
        if msg.message_type == "tool_call" and isinstance(msg.metadata_json, dict):
            tool_name = msg.metadata_json.get("tool_name") or msg.metadata_json.get("name") or "unknown_tool"
            tool_args = msg.metadata_json.get("arguments") or msg.metadata_json.get("args") or {}
            md_lines.append(f"**调用工具**: `{tool_name}`")
            md_lines.append("```json")
            md_lines.append(json.dumps(tool_args, indent=2, ensure_ascii=False))
            md_lines.append("```")
        elif msg.message_type in ("tool_result", "tool_output") and isinstance(msg.metadata_json, dict):
            tool_name = msg.metadata_json.get("tool_name") or "tool"
            tool_res = msg.metadata_json.get("result") or msg.content
            md_lines.append(f"**工具执行结果 (`{tool_name}`)**:")
            if isinstance(tool_res, (dict, list)):
                md_lines.append("```json")
                md_lines.append(json.dumps(tool_res, indent=2, ensure_ascii=False))
                md_lines.append("```")
            else:
                md_lines.append(str(tool_res))
        else:
            md_lines.append(msg.content)

        md_lines.append("")

    md_lines.append("---")
    md_lines.append("")
    md_lines.append("## 四、 4 级 Pre-Flight 运行期沙盒与自愈日志")
    if writing_log:
        status_text = writing_log.get("status", "IDLE")
        progress = writing_log.get("progress", 0)
        logs = writing_log.get("logs", "")
        steps = writing_log.get("steps") or []
        md_lines.append(f"- **沙盒状态**: `{status_text}` ({progress}%)")
        if steps:
            md_lines.append("- **4 级验证步骤结果**:")
            for s in steps:
                ok_mark = "✅ PASS" if s.get("ok") else "❌ FAIL"
                md_lines.append(f"  - `[{s.get('level')}]` {s.get('name')}: **{ok_mark}** - {s.get('message')}")
        md_lines.append("")
        if logs:
            md_lines.append("**执行日志输出**:")
            md_lines.append("```text")
            md_lines.append(logs)
            md_lines.append("```")
    else:
        md_lines.append("（暂无沙盒执行日志）")
    md_lines.append("")

    md_lines.append("---")
    md_lines.append("")
    md_lines.append("## 五、 生成的 NautilusTrader 策略源码")
    if strategy_code:
        md_lines.append("```python")
        md_lines.append(strategy_code)
        md_lines.append("```")
    else:
        md_lines.append("（尚未生成或持久化策略代码）")
    md_lines.append("")

    md_lines.append("---")
    md_lines.append("")
    md_lines.append("## 六、 回测历史与绩效归因记录")
    if backtests_data:
        md_lines.append("| 回测名称 | 状态 | 收益率 (Return) | 夏普比率 (Sharpe) | 最大回撤 (MaxDD) | 胜率 (WinRate) | 创建时间 |")
        md_lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
        for b in backtests_data:
            metrics = b.get("metrics") or {}
            ret = f"{metrics.get('total_return_pct', metrics.get('return_pct', 0)):.2f}%" if isinstance(metrics.get('total_return_pct') or metrics.get('return_pct'), (int, float)) else "-"
            sharpe = f"{metrics.get('sharpe_ratio', 0):.2f}" if isinstance(metrics.get('sharpe_ratio'), (int, float)) else "-"
            max_dd = f"{metrics.get('max_drawdown_pct', 0):.2f}%" if isinstance(metrics.get('max_drawdown_pct'), (int, float)) else "-"
            win_rate = f"{metrics.get('win_rate_pct', 0):.2f}%" if isinstance(metrics.get('win_rate_pct'), (int, float)) else "-"
            c_time = str(b.get("created_at") or "")[:19]
            md_lines.append(f"| {b.get('name')} | `{b.get('status')}` | {ret} | {sharpe} | {max_dd} | {win_rate} | {c_time} |")
    else:
        md_lines.append("（暂无回测记录）")
    md_lines.append("")

    filename = f"quantlab_research_{clean_title}_{now_str}.md"
    content_bytes = "\n".join(md_lines).encode("utf-8")
    return Response(
        content=content_bytes,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


async def _resolve_action_run(
    project: ResearchProject,
    db: AsyncSession,
    *,
    run_id: str | None = None,
    status: RunStatus | None = None,
) -> BacktestRun:
    scope = BacktestRun.research_project_id == project.id
    if project.latest_backtest_id:
        scope = or_(scope, BacktestRun.id == project.latest_backtest_id)
    query = select(BacktestRun).where(scope)
    if run_id:
        query = query.where(BacktestRun.id == run_id)
    if status is not None:
        query = query.where(BacktestRun.status == status)
    run = await db.scalar(query.order_by(BacktestRun.created_at.desc()).limit(1))
    if run is None:
        label = "失败回测" if status == RunStatus.FAILED else "已完成回测"
        raise HTTPException(409, f"当前项目没有可用的{label}记录")
    return run


def _complete_backtest_arguments(arguments: dict[str, Any]) -> bool:
    return bool(
        str(arguments.get("strategy_name") or "").strip()
        and arguments.get("symbols")
        and arguments.get("start_date")
        and arguments.get("end_date")
    )


@router.post("/{project_id}/dsh/action")
async def run_dsh_action_endpoint(
    project_id: str,
    data: DshActionRequest,
    db: AsyncSession = Depends(get_db),
):
    """Run a typed high-frequency action without an intent-classifier turn."""
    project = await _project(project_id, db)
    if project.status == ResearchStatus.ARCHIVED:
        raise HTTPException(409, "研究项目已归档，请先重新打开")
    if project.id in ACTIVE_RESEARCH_TASKS and not ACTIVE_RESEARCH_TASKS[project.id].done():
        raise HTTPException(409, "当前 DSH 任务仍在执行，请等待完成或先停止任务")

    action_run: BacktestRun | None = None
    if data.action in {"RUN_BACKTEST", "FIX_ERROR"} and not project.strategy_id:
        from .dsh.bridge import _resolve_strategy_name_for_project
        resolved_name = await _resolve_strategy_name_for_project(
            project, (data.arguments or {}).get("strategy_name"), db
        )
        strat_rec = await ensure_strategy_db_record(resolved_name, db, project_id=project.id)
        if strat_rec and strat_rec[0]:
            project.strategy_id = strat_rec[0].id
            await db.commit()
        elif data.action == "RUN_BACKTEST":
            raise HTTPException(409, "当前项目还没有可执行策略，请先编写策略")

    if data.action == "FIX_ERROR":
        try:
            action_run = await _resolve_action_run(
                project,
                db,
                run_id=data.run_id,
                status=RunStatus.FAILED,
            )
        except HTTPException:
            action_run = None
    elif data.action == "ANALYZE_BACKTEST":
        action_run = await _resolve_action_run(
            project,
            db,
            run_id=data.run_id,
            status=RunStatus.COMPLETED,
        )

    labels = {
        "WRITE_STRATEGY": "编写策略",
        "RUN_BACKTEST": "执行回测",
        "FIX_ERROR": "修复报错",
        "ANALYZE_BACKTEST": "回测分析",
    }
    user_content = data.content.strip() or labels[data.action]
    user_msg = ResearchMessage(
        project_id=project.id,
        role="user",
        content=user_content,
        message_type="message",
        metadata_json={
            "is_dsh_run": True,
            "event_type": "fixed_action",
            "action": data.action,
        },
    )
    db.add(user_msg)
    project.updated_at = datetime.now(UTC)
    await db.commit()

    from .workflow.task_service import create_task

    worker_by_action = {
        "WRITE_STRATEGY": WorkerType.CODING,
        "FIX_ERROR": WorkerType.CODING,
        "RUN_BACKTEST": WorkerType.BACKTEST,
        "ANALYZE_BACKTEST": WorkerType.ANALYSIS,
    }
    durable_task = await create_task(
        db,
        project_id=project.id,
        worker_type=worker_by_action[data.action],
        task_type=data.action,
        input_json={
            "content": user_content,
            "arguments": dict(data.arguments or {}),
            "run_id": data.run_id,
        },
        max_attempts=3 if data.action in {"WRITE_STRATEGY", "FIX_ERROR"} else 2,
    )

    if data.action == "RUN_BACKTEST" and _complete_backtest_arguments(data.arguments):
        from .dsh.bridge import _exec_execute_backtest, _normalize_backtest_arguments
        from .workflow.task_service import complete_task, fail_task, start_task

        arguments = dict(data.arguments)
        if "parameters" not in arguments and "strategy_parameters" in arguments:
            arguments["parameters"] = arguments.pop("strategy_parameters")
        arguments = _normalize_backtest_arguments(arguments)
        await start_task(db, durable_task, f"direct-backtest-{project.id}")
        try:
            result = await _exec_execute_backtest(project, arguments, db)
            if not result.get("ok"):
                err_msg = str(result.get("error") or result.get("error_message") or "回测执行失败")
                await fail_task(db, durable_task, "BACKTEST_FAILED", err_msg)
                db.add(ResearchMessage(
                    project_id=project.id,
                    role="system",
                    content=f"回测任务启动失败：{err_msg}",
                    message_type="error",
                    metadata_json={"event_type": "fixed_action_failed", "action": data.action, "result": result},
                ))
                await db.commit()
                _set_thinking_status(project.id, "FAILED", "回测任务启动失败", err_msg)
                return {
                    "ok": False,
                    "error": err_msg,
                    "action": data.action,
                    "phase": project.research_phase,
                    "result": result,
                }
            await complete_task(db, durable_task, result)
        except Exception as exc:
            from .workflow.error_router import classify_error

            route = classify_error(exc)
            await fail_task(db, durable_task, route.code, str(exc))
            raise
        strat_name = arguments.get("strategy_name") or "策略"
        run_id = result.get("run_id")
        metrics = result.get("metrics") or {}
        if result.get("ok") and metrics:
            total_ret = metrics.get("total_return")
            sharpe = (
                metrics.get("sharpe_ratio")
                if metrics.get("sharpe_ratio") is not None
                else metrics.get("sharpe")
            )
            max_dd = metrics.get("max_drawdown")
            trades = (
                metrics.get("total_trades")
                if metrics.get("total_trades") is not None
                else metrics.get("trades")
            )
            ret_str = f"{total_ret:.2f}%" if total_ret is not None else "—"
            sharpe_str = f"{sharpe:.2f}" if sharpe is not None else "—"
            dd_str = f"{max_dd:.2f}%" if max_dd is not None else "—"
            content = (
                f"回测任务 `{run_id}` 已完成！\n"
                f"- **策略**: {strat_name}\n"
                f"- **总收益率**: {ret_str}\n"
                f"- **夏普比率**: {sharpe_str}\n"
                f"- **最大回撤**: {dd_str}\n"
                f"- **交易次数**: {trades or 0} 笔\n\n"
                f"已生成回测指标卡片，可点击下方卡片查看完整报告或发起深度归因分析。"
            )
        else:
            content = f"已直接提交回测任务：{run_id}。"

        db.add(ResearchMessage(
            project_id=project.id,
            role="assistant",
            content=content,
            message_type="message",
            metadata_json={
                "event_type": "fixed_action_executed",
                "action": data.action,
                "tool": "execute_backtest_tool",
                "run_id": run_id,
                "arguments": arguments,
                "result": result,
                "ok": result.get("ok", True),
            },
        ))
        await db.commit()
        _set_thinking_status(project.id, "IDLE", "回测任务已就绪", "")
        return {
            "ok": True,
            "kicked_off": False,
            "action": data.action,
            "phase": project.research_phase,
            "result": result,
        }

    recent_messages = await _recent_intent_context(project.id, db)
    task_profile = data.action
    if data.action == "WRITE_STRATEGY":
        eff_idea, latest_plan, candidate_code = await _resolve_implementation_context(project, db)
        await _approve_research_specification(project, latest_plan, db)
        phase = "IMPLEMENTATION"
        prompt = _implementation_prompt(
            project,
            data.content.strip() or "按已确认方案直接生成策略代码",
            latest_plan,
            candidate_code=candidate_code,
            original_idea=eff_idea,
        )
        step = "DSH 正在直接编写策略并进行一次集中验证..."
    elif data.action == "RUN_BACKTEST":
        phase = "BACKTEST"
        prompt = (
            "这是用户点击的固定回测动作。读取当前策略 Manifest 与本地 Catalog，"
            "只调用必要工具生成一张可编辑的回测参数卡，然后立即停止并等待用户确认。"
            "不要重新讨论策略、不要修改代码、不要执行回测。"
        )
        step = "DSH 正在准备最小回测参数卡..."
    elif data.action == "FIX_ERROR":
        if action_run is not None:
            run = action_run
            config = run.config or {}
            strategy_name = str(config.get("strategy_name") or run.name or "strategy")
            err_text = str(run.error_message or "未知错误")[-6000:]
            run_desc = f"失败回测 ID：{run.id}\n失败阶段：{run.stage}\n"
        else:
            from .dsh.bridge import _resolve_strategy_name_for_project, _workspace_strategy_file
            strategy_name = await _resolve_strategy_name_for_project(
                project, (data.arguments or {}).get("strategy_name"), db
            )
            err_text = str((data.arguments or {}).get("error_message") or data.content or "策略运行/加载报错")[-6000:]
            run_desc = ""

        candidate_path = _workspace_strategy_file(project, strategy_name)
        candidate_code = (
            candidate_path.read_text(encoding="utf-8")
            if candidate_path.exists()
            else ""
        )
        candidate_context = (
            f"\n【当前项目未发布候选源码】\n```python\n{candidate_code}\n```\n"
            if candidate_code
            else ""
        )

        phase = "REPAIR"
        prompt = (
            f"这是用户发起的单次修复动作。\n{run_desc}"
            f"策略：{strategy_name}\n"
            f"报错：{err_text}\n{candidate_context}\n"
            "使用只读工具查看白名单内的当前策略、契约与指标实现，定位根因并做最小修改。"
            "每轮补丁后重新运行统一 Pre-Flight；失败后根据完整错误继续修复，最多三轮。"
            "全部通过后保存结果并停止。禁止改变策略交易规则，禁止执行正式回测。"
        )
        step = "DSH 正在针对策略报错执行单次代码修复..."
    else:
        run = action_run
        assert run is not None
        phase = "RESULT_REVIEW"
        prompt = (
            f"这是用户点击的固定回测分析动作。回测 ID：{run.id}\n"
            f"策略/任务：{run.name}\n"
            f"配置：{json.dumps(run.config or {}, ensure_ascii=False, default=str)[:4000]}\n"
            f"指标：{json.dumps(run.metrics or {}, ensure_ascii=False, default=str)[:4000]}\n\n"
            "直接输出简洁、可执行的绩效归因：收益来源、亏损来源、市场适应性、风险与下一步建议。"
            "必须明确说明当前结果属于单区间策略级回测，不得把因子实验等同于策略级稳健性，"
            "若未执行未见样本或 walk-forward，必须将其列为证据限制。"
            "禁止修改代码、生成参数或重新回测。"
        )
        step = "DSH 正在直接分析指定回测结果..."

    apply_research_phase(project, phase)
    project.updated_at = datetime.now(UTC)
    await db.commit()
    _start_dsh_turn(
        project,
        prompt,
        step=step,
        phase=phase,
        task_profile=task_profile,
        agent_task_id=durable_task.id,
    )
    return {
        "ok": True,
        "kicked_off": True,
        "action": data.action,
        "phase": phase,
        "message": "已跳过意图分析并启动固定任务。",
    }


@router.post("/{project_id}/dsh/run")
async def run_dsh_pipeline_endpoint(
    project_id: str,
    data: ResearchMessageCreate,
    db: AsyncSession = Depends(get_db),
):
    """Kick off an agent turn in the project's official DSH session.

    The DSH SDK runtime runs as a child process and drives the whole
    research -> code -> backtest workflow as the orchestrator. QuantLab acts
    as the gatekeeper: domain tools go through the HTTP bridge, and
    write/backtest actions pause at the interactive approval registry.
    """
    project = await _project(project_id, db)
    if project.status == ResearchStatus.ARCHIVED:
        raise HTTPException(409, "研究项目已归档，请先重新打开")

    user_msg = ResearchMessage(
        project_id=project.id,
        role="user",
        content=data.content,
        message_type="message",
    )
    db.add(user_msg)
    project.updated_at = datetime.now(UTC)
    await db.commit()

    from app.dsh import engine as dsh_engine
    from app.dsh.bridge import pending_approvals

    pending = pending_approvals(project.id)
    recent_messages = await _recent_intent_context(project.id, db)
    intent_result = _fast_intent_decision(data.content, pending)
    _set_thinking_status(
        project.id,
        "THINKING",
        "正在执行明确指令的快速路由" if intent_result else "DSH 正在结合对话判断用户意图",
        "",
    )
    try:
        if intent_result is None:
            intent_result = await dsh_engine.classify_intent(
                project,
                data.content,
                recent_messages,
                pending,
            )
    except Exception as exc:
        logger.error("DSH 意图判断失败 (project=%s): %s", project.id, exc, exc_info=True)
        reply = f"DSH 暂时无法可靠判断本次意图，未执行任何写码或回测操作：{exc}"
        db.add(ResearchMessage(
            project_id=project.id,
            role="system",
            content=reply,
            message_type="error",
            metadata_json={"event_type": "intent_error", "is_dsh_run": True},
        ))
        await db.commit()
        _set_thinking_status(project.id, "FAILED", "DSH 意图判断失败", str(exc))
        return {"ok": False, "kicked_off": False, "error": str(exc), "message": reply}

    intent = intent_result["intent"]
    confidence = float(intent_result.get("confidence", 0.0))
    user_msg.metadata_json = {
        "is_dsh_run": True,
        "event_type": "intent_decision",
        "intent": intent_result,
    }
    approval_intent = intent in {"APPROVE_PENDING_ACTION", "REJECT_PENDING_ACTION"}
    needs_clarification = bool(intent_result.get("needs_clarification"))
    if confidence < (0.9 if approval_intent else 0.6):
        needs_clarification = True

    if needs_clarification or intent == "UNKNOWN":
        reply = intent_result.get("clarification_question") or "我还不能确定你希望继续研究、编写策略还是进行回测，请明确一下下一步。"
        db.add(ResearchMessage(
            project_id=project.id,
            role="assistant",
            content=reply,
            message_type="message",
            metadata_json={
                "event_type": "intent_clarification",
                "is_dsh_run": True,
                "intent": intent_result,
            },
        ))
        await db.commit()
        _set_thinking_status(project.id, "IDLE", "等待你澄清下一步", "")
        return {
            "ok": True,
            "kicked_off": False,
            "intent": intent,
            "needs_clarification": True,
            "message": reply,
        }

    if approval_intent:
        request_id = intent_result.get("pending_request_id")
        pending_ids = {item.get("request_id") for item in pending}
        if not request_id and len(pending) == 1:
            request_id = pending[0].get("request_id")
        if not request_id or request_id not in pending_ids:
            reply = "当前没有与这次确认匹配的待审批请求，请先查看或重新生成操作卡片。"
            db.add(ResearchMessage(
                project_id=project.id,
                role="assistant",
                content=reply,
                message_type="message",
                metadata_json={"event_type": "intent_approval_mismatch", "intent": intent_result},
            ))
            await db.commit()
            _set_thinking_status(project.id, "IDLE", "没有匹配的待审批请求", "")
            return {"ok": True, "kicked_off": False, "intent": intent, "message": reply}
        result = await dsh_approve_request(
            project.id,
            DshApproveRequest(
                request_id=request_id,
                approved=intent == "APPROVE_PENDING_ACTION",
                feedback=(intent_result.get("normalized_request") or data.content)[-2000:],
            ),
            db,
        )
        return {**result, "intent": intent, "kicked_off": False}

    phase = INTENT_PHASES.get(intent, "RESEARCH")
    if phase == "BACKTEST" and not project.strategy_id:
        reply = "DSH 判断你希望进行回测，但当前项目还没有可执行的策略代码。请先确认策略方案并进入编码。"
        db.add(ResearchMessage(
            project_id=project.id,
            role="assistant",
            content=reply,
            message_type="message",
            metadata_json={"event_type": "intent_state_guard", "intent": intent_result},
        ))
        await db.commit()
        _set_thinking_status(project.id, "IDLE", "等待先完成策略编码", "")
        return {"ok": True, "kicked_off": False, "intent": intent, "message": reply}
    if phase == "RESULT_REVIEW" and intent == "ANALYZE_BACKTEST" and not project.latest_backtest_id:
        reply = "DSH 判断你希望分析回测结果，但当前项目还没有已完成的回测记录。"
        db.add(ResearchMessage(
            project_id=project.id,
            role="assistant",
            content=reply,
            message_type="message",
            metadata_json={"event_type": "intent_state_guard", "intent": intent_result},
        ))
        await db.commit()
        _set_thinking_status(project.id, "IDLE", "当前没有可分析的回测结果", "")
        return {"ok": True, "kicked_off": False, "intent": intent, "message": reply}

    apply_research_phase(project, phase)
    project.updated_at = datetime.now(UTC)
    await db.commit()
    if phase == "IMPLEMENTATION":
        eff_idea, latest_plan, candidate_code = await _resolve_implementation_context(project, db)
        await _approve_research_specification(project, latest_plan, db)
        turn_prompt = _implementation_prompt(
            project,
            intent_result.get("normalized_request") or data.content,
            latest_plan,
            candidate_code=candidate_code,
            original_idea=eff_idea,
        )
        step = "DSH 正在生成完整策略代码并准备审批提案..."
    else:
        turn_prompt = _intent_routed_prompt(intent_result, data.content)
        handoff_context = await _session_handoff_context(project.id, db)
        if handoff_context:
            turn_prompt += f"\n\n【上一会话交接摘要】\n{handoff_context}"
        step = {
            "RESEARCH": "DSH 正在研究并完善策略方案...",
            "BACKTEST": "DSH 正在准备回测参数或提交回测任务...",
            "RESULT_REVIEW": "DSH 正在读取并分析当前结果...",
        }.get(phase, "DSH 正在执行当前任务...")

    task_profile = {
        "START_IMPLEMENTATION": "WRITE_STRATEGY",
        "MODIFY_STRATEGY_CODE": "FIX_ERROR",
        "REQUEST_BACKTEST": "RUN_BACKTEST",
        "ANALYZE_BACKTEST": "ANALYZE_BACKTEST",
    }.get(intent, "")
    from .dsh.profiles import worker_for_phase
    from .workflow.task_service import create_task

    durable_task = await create_task(
        db,
        project_id=project.id,
        worker_type=worker_for_phase(phase),
        task_type=task_profile or intent,
        input_json={
            "intent": intent,
            "content": data.content,
            "normalized_request": intent_result.get("normalized_request"),
        },
        max_attempts=3 if phase in {"IMPLEMENTATION", "REPAIR", "FIX_ERROR"} else 2,
    )
    _start_dsh_turn(
        project,
        turn_prompt,
        step=step,
        phase=phase,
        task_profile=task_profile,
        agent_task_id=durable_task.id,
    )
    return {
        "ok": True,
        "kicked_off": True,
        "intent": intent,
        "confidence": confidence,
        "phase": phase,
        "message": "DSH 已完成意图判断并启动对应任务。",
    }


@router.post("/{project_id}/dsh/cancel")
async def dsh_cancel_run(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Cancel the running DSH turn (if any) for a research project."""
    proj = await _project(project_id, db)
    from app.dsh import engine as dsh_engine

    dsh_engine.cancel_turn(proj.id)
    db.add(ResearchMessage(
        project_id=proj.id,
        role="system",
        content="已按用户要求强制停止当前 DSH / LLM 任务。",
        message_type="message",
        metadata_json={"event_type": "user_cancel", "is_dsh_run": True},
    ))
    proj.updated_at = datetime.now(UTC)
    await db.commit()
    _set_thinking_status(proj.id, "IDLE", "已取消 DSH 回合", "")
    return {"ok": True, "message": "已取消 DSH 回合"}


@router.get("/{project_id}/dsh/pending")
async def dsh_pending_approvals(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    await _project(project_id, db)
    from app.dsh.bridge import pending_approvals

    return pending_approvals(project_id)


@router.get("/{project_id}/tasks")
async def research_tasks(
    project_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Expose the durable specialist-worker timeline for recovery and UI diagnostics."""
    await _project(project_id, db)
    rows = list((await db.scalars(
        select(AgentTask).where(AgentTask.project_id == project_id).order_by(AgentTask.created_at.desc())
    )).all())
    return [{
        "id": item.id,
        "worker_type": item.worker_type.value,
        "task_type": item.task_type,
        "status": item.status.value,
        "attempt": item.attempt,
        "max_attempts": item.max_attempts,
        "session_id": item.session_id,
        "input": item.input_json,
        "output": item.output_json,
        "error_code": item.error_code,
        "error_message": item.error_message,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    } for item in rows]


@router.post("/{project_id}/dsh/approve")
async def dsh_approve_request(
    project_id: str,
    data: DshApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Record a user decision and execute the reviewed proposal directly."""
    project = await _project(project_id, db)
    from app.dsh.bridge import approve_proposal, execute_approved_proposal, pending_approvals
    from app.dsh import engine as dsh_engine

    pending_before = {item["request_id"]: item for item in pending_approvals(project.id)}
    pending_entry = pending_before.get(data.request_id, {})
    approved_tool = pending_entry.get("tool")
    decision = approve_proposal(project.id, data.request_id, data.approved, data.feedback)
    if data.approved:
        # Stop the model turn which produced the proposal before executing it;
        # otherwise it may finish later and append a stale "please approve"
        # message after the approval has already been consumed.
        dsh_engine.cancel_turn(project.id)
        if approved_tool == "write_strategy_code":
            apply_research_phase(project, "IMPLEMENTATION")
        elif approved_tool == "execute_backtest_tool":
            apply_research_phase(project, "BACKTEST")
        await db.commit()
        execution = await execute_approved_proposal(project, data.request_id, db)
        result = execution.get("result") or {}
        result_ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        result_run_id = result.get("run_id") if isinstance(result, dict) else None
        result_summary = json.dumps(result, ensure_ascii=False, default=str)[:12_000]
        reviewed_arguments = dict(pending_entry.get("arguments") or {})
        candidate_code = str(reviewed_arguments.get("code") or "")
        audit_arguments = {
            key: value for key, value in reviewed_arguments.items() if key != "code"
        }
        if candidate_code:
            audit_arguments["code_sha256"] = hashlib.sha256(candidate_code.encode()).hexdigest()
            audit_arguments["code_chars"] = len(candidate_code)
        db.add(ResearchMessage(
            project_id=project.id,
            role="assistant",
            content=(
                f"已执行批准的 {approved_tool}。"
                + ("执行成功。" if result_ok else "执行完成，但校验未通过。")
                + f"\n\n```json\n{result_summary}\n```"
            ),
            message_type="message",
            metadata_json={
                "is_dsh_run": False,
                "event_type": "approval_execution",
                "request_id": data.request_id,
                "tool": approved_tool,
                "ok": result_ok,
                "run_id": result_run_id,
                "arguments": audit_arguments,
                "result": result,
            },
        ))
        await db.commit()
        if approved_tool == "write_strategy_code":
            apply_research_phase(project, "IMPLEMENTED" if result_ok else "REPAIR")
            await db.commit()
            if not result_ok:
                attempts = await _auto_repair_attempt_count(project.id, db)
                max_attempts = max(0, settings.dsh_auto_repair_max_attempts)
                verification = result.get("verification") if isinstance(result, dict) else None
                strategy_name = str(
                    (result.get("strategy_name") if isinstance(result, dict) else None)
                    or reviewed_arguments.get("strategy_name")
                    or "strategy"
                )
                if candidate_code and isinstance(verification, dict) and attempts < max_attempts:
                    attempt = attempts + 1
                    repair_prompt = _build_auto_repair_prompt(
                        strategy_name,
                        candidate_code,
                        verification,
                        attempt,
                        max_attempts,
                    )
                    db.add(ResearchMessage(
                        project_id=project.id,
                        role="system",
                        content=(
                            f"Pre-Flight 未通过，框架已自动启动第 {attempt}/{max_attempts} 次受限修复回合。"
                            "修复完成后会生成新的代码审批卡。"
                        ),
                        message_type="system",
                        metadata_json={
                            "event_type": "auto_repair_started",
                            "strategy_name": strategy_name,
                            "attempt": attempt,
                            "max_attempts": max_attempts,
                            "failed_level": verification.get("failed_level"),
                            "error_message": verification.get("error_message"),
                        },
                    ))
                    await db.commit()
                    _set_thinking_status(
                        project.id,
                        "THINKING",
                        f"正在自动修复策略契约错误 ({attempt}/{max_attempts})",
                        "",
                    )
                    _start_dsh_turn(
                        project,
                        repair_prompt,
                        step=f"DSH 正在执行受限契约修复 ({attempt}/{max_attempts})...",
                        phase="REPAIR",
                        task_profile="FIX_ERROR",
                    )
                    return {
                        "ok": True,
                        **decision,
                        "auto_repair_started": True,
                        "auto_repair_attempt": attempt,
                    }
                db.add(ResearchMessage(
                    project_id=project.id,
                    role="system",
                    content=(
                        "Pre-Flight 未通过，自动修复未启动："
                        + ("已达到最大修复次数。" if attempts >= max_attempts else "缺少候选源码或结构化校验信息。")
                    ),
                    message_type="system",
                    metadata_json={"event_type": "auto_repair_exhausted", "attempts": attempts},
                ))
                await db.commit()
        _set_thinking_status(project.id, "IDLE", "审批操作已执行", "")
    else:
        apply_research_phase(project, "RESEARCH" if approved_tool == "write_strategy_code" else "IMPLEMENTATION")
        await db.commit()
        _set_thinking_status(project.id, "IDLE", "已拒绝该操作", data.feedback or "")

    return {"ok": True, **decision}
