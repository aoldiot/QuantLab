"""1小时布林带均值回归反转策略。

策略核心：
- 多头信号：当前1小时K线最低价触及或跌破布林带下轨，且Choppiness指数低于阈值
  （按规格归一化到 0~1 后与 chop_threshold 比较，0.4 即原始 CHOP < 40）。
- 空头信号：当前1小时K线最高价触及或突破布林带上轨，且Choppiness指数低于阈值。
- 多头平仓：K线最高价触及或突破布林带中轨。
- 空头平仓：K线最低价触及或跌破布林带中轨。
- 信号在K线收盘时识别，下一根K线开盘按市价单执行（回测引擎按下一根K线开盘撮合）。
- 仓位：账户权益固定比例（默认10%，最大20%），同时仅持一单，不补仓、不加仓。
"""

from __future__ import annotations

import math
from decimal import Decimal

import numpy as np
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.indicators import BollingerBands
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class BollingerMeanReversal1HConfig(StrategyConfig, frozen=True):
    """策略配置：固定参数，便于回测与参数敏感性测试。"""

    instrument_id: InstrumentId
    bar_type: BarType
    trade_size: Decimal = Decimal("0.001")
    bb_period: int = 20
    bb_std_multiplier: float = 2.0
    chop_period: int = 14
    chop_threshold: float = 0.4
    equity_fraction: float = 0.10
    max_equity_fraction: float = 0.20


class BollingerMeanReversal1H(Strategy):
    """1小时布林带均值回归反转策略实现。"""

    def __init__(self, config: BollingerMeanReversal1HConfig) -> None:
        super().__init__(config)
        self._instrument_id: InstrumentId = self.config.instrument_id

        # 布林带指标（同时向 plot_config 暴露 bb_mid / bb_up / bb_down）
        self.bb = BollingerBands(self.config.bb_period, self.config.bb_std_multiplier)

        # Choppiness 指数使用单周期 TR（high - low）的滚动求和，
        # 不使用平滑 ATR，因此直接维护 raw 高/低队列自行计算。
        self._recent_highs: list[float] = []
        self._recent_lows: list[float] = []
        self._chop_value: float = float("nan")

        # 在 K 线收盘时识别到的待执行动作，下一根 K 线开盘执行
        self._pending_action: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def on_start(self) -> None:
        self.register_indicator_for_bars(self.config.bar_type, self.bb)
        self.subscribe_bars(self.config.bar_type)

    def on_bar(self, bar: Bar) -> None:
        # 1) 先执行上一根K线收盘时识别出的待执行动作
        if self._pending_action is not None:
            self._execute_action(bar, self._pending_action)
            self._pending_action = None

        # 2) 更新 Choppiness 计算用的滚动窗口
        self._update_history(bar)
        self._chop_value = self._calculate_choppiness()

        # 3) 在本根K线收盘时评估入场/出场条件
        if not self.bb.initialized:
            return
        if math.isnan(self._chop_value):
            return

        bb_up = float(self.bb.upper)
        bb_mid = float(self.bb.middle)
        bb_down = float(self.bb.lower)
        if any(math.isnan(v) for v in (bb_up, bb_mid, bb_down)):
            return

        bar_high = float(bar.high.as_double())
        bar_low = float(bar.low.as_double())

        positions = self.cache.positions_open(instrument_id=self._instrument_id)
        position = positions[0] if positions else None
        is_flat = position is None or position.quantity == 0
        is_long = position is not None and position.side == PositionSide.LONG
        is_short = position is not None and position.side == PositionSide.SHORT

        # 出场优先
        if is_long and bar_high >= bb_mid:
            self._pending_action = "EXIT_LONG"
        elif is_short and bar_low <= bb_mid:
            self._pending_action = "EXIT_SHORT"
        # 仅在空仓 + Choppiness 确认震荡市时考虑入场
        elif is_flat and self._chop_value < self.config.chop_threshold:
            if bar_low <= bb_down:
                self._pending_action = "LONG"
            elif bar_high >= bb_up:
                self._pending_action = "SHORT"

    # ------------------------------------------------------------------ helpers

    def _update_history(self, bar: Bar) -> None:
        high = float(bar.high.as_double())
        low = float(bar.low.as_double())
        self._recent_highs.append(high)
        self._recent_lows.append(low)
        period = self.config.chop_period
        if len(self._recent_highs) > period:
            self._recent_highs = self._recent_highs[-period:]
            self._recent_lows = self._recent_lows[-period:]

    def _calculate_choppiness(self) -> float:
        """计算 Choppiness 指数并归一化到 [0, 1]。"""
        period = self.config.chop_period
        if len(self._recent_highs) < period or len(self._recent_lows) < period:
            return float("nan")

        # SUM(ATR(1), n) 等价于 SUM(high - low, n)
        sum_tr = sum(
            self._recent_highs[i] - self._recent_lows[i]
            for i in range(-period, 0)
        )
        max_hi = max(self._recent_highs[-period:])
        min_lo = min(self._recent_lows[-period:])

        if max_hi <= min_lo or sum_tr <= 0:
            return float("nan")

        try:
            chop = 100.0 * math.log10(sum_tr / (max_hi - min_lo)) / math.log10(period)
        except (ValueError, ZeroDivisionError):
            return float("nan")

        # 归一化到 0~1，使 chop_threshold = 0.4 与原始 CHOP=40 等价
        return chop / 100.0

    def _execute_action(self, bar: Bar, action: str) -> None:
        instrument: Instrument | None = self.cache.instrument(self._instrument_id)
        if instrument is None:
            self.log.warning(f"未找到 instrument: {self._instrument_id}")
            return

        if action in ("LONG", "SHORT"):
            # 回测引擎按下一根K线开盘价撮合，这里用 bar.open 作为参考价计算数量
            price = float(bar.open.as_double())
            qty = self._calculate_order_qty(price)
            if qty <= 0:
                self.log.info(
                    f"账户权益不足或价格为0，跳过 {action}（price={price}）"
                )
                return
            order_side = OrderSide.BUY if action == "LONG" else OrderSide.SELL
            order = self.order_factory.market(
                instrument_id=self._instrument_id,
                order_side=order_side,
                quantity=instrument.make_qty(Decimal(str(qty))),
            )
            self.submit_order(order)
            self.log.info(
                f"提交市价单 {action} qty={qty} price={price} chop={self._chop_value:.4f}"
            )
        elif action in ("EXIT_LONG", "EXIT_SHORT"):
            self.close_all_positions(self._instrument_id)
            self.log.info(f"触发 {action}，平掉所有 {self._instrument_id} 持仓")

    def _calculate_order_qty(self, price: float) -> float:
        """按账户权益固定比例计算下单数量，并受 max_equity_fraction 约束。"""
        if price <= 0:
            return 0.0
        account = self.portfolio.account(self._instrument_id.venue)
        if account is None:
            return 0.0
        balance_total = account.balance_total()
        if balance_total is None:
            return 0.0
        try:
            equity = float(balance_total.as_double())
        except Exception as exc:  # noqa: BLE001
            self.log.warning(f"读取账户权益失败: {exc}")
            return 0.0
        if equity <= 0:
            return 0.0

        fraction = min(self.config.equity_fraction, self.config.max_equity_fraction)
        target_value = equity * fraction
        qty = target_value / price
        return qty


# ---------------------------------------------------------------- indicator helper

def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算布林带与 Choppiness 指数，供回测图表使用。"""
    bb_period = int(parameters.get("bb_period", 20))
    bb_std_multiplier = float(parameters.get("bb_std_multiplier", 2.0))
    chop_period = int(parameters.get("chop_period", 14))

    if bb_period <= 0 or chop_period <= 0:
        raise ValueError("bb_period 与 chop_period 必须为正整数")

    out = df.copy()

    close = pd.to_numeric(out["close"], errors="coerce")
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")

    # 布林带：SMA ± k * 总体标准差
    out["bb_mid"] = close.rolling(bb_period).mean()
    out["bb_std"] = close.rolling(bb_period).std(ddof=0)
    out["bb_up"] = out["bb_mid"] + bb_std_multiplier * out["bb_std"]
    out["bb_down"] = out["bb_mid"] - bb_std_multiplier * out["bb_std"]

    # Choppiness Index：100 * log10(SUM(TR1,n)/(MaxHi-MinLo)) / log10(n)
    tr = high - low
    sum_atr1 = tr.rolling(chop_period).sum()
    period_max = high.rolling(chop_period).max()
    period_min = low.rolling(chop_period).min()
    range_hl = period_max - period_min
    safe_range = range_hl.replace(0, np.nan)
    chop = 100.0 * np.log10(sum_atr1 / safe_range) / np.log10(chop_period)
    out["chop"] = chop / 100.0  # 归一化到 [0, 1]

    return out


# ---------------------------------------------------------------- manifest

STRATEGY_MANIFEST = StrategyManifest(
    slug="bollinger_mean_reversal_1h",
    name="1小时布林带均值回归反转策略",
    version="1.0.0",
    description=(
        "基于布林带与Choppiness指数的1小时均值回归策略。价格触及布林带上下轨时考虑反转，"
        "仅在Choppiness指数确认的震荡市中入场，平仓信号为布林带中轨。"
        "仓位管理使用账户权益固定比例（默认10%，最大20%），同时仅持一单。"
    ),
    category="mean_reversion",
    strategy_path="app.strategies.bollinger_mean_reversal_1h:BollingerMeanReversal1H",
    config_path="app.strategies.bollinger_mean_reversal_1h:BollingerMeanReversal1HConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=False,
    timeframes=("1h",),
    primary_timeframe="1h",
    parameters={
        "bb_period": ParameterSpec(
            title="布林带均线周期",
            type="integer",
            default=20,
            minimum=5,
            maximum=100,
            description="布林带中轨使用的简单移动平均周期。",
        ),
        "bb_std_multiplier": ParameterSpec(
            title="布林带标准差倍数",
            type="number",
            default=2.0,
            minimum=0.5,
            maximum=4.0,
            description="上下轨相对中轨的标准差倍数，控制带宽。",
        ),
        "chop_period": ParameterSpec(
            title="Choppiness指数周期",
            type="integer",
            default=14,
            minimum=5,
            maximum=60,
            description="Choppiness指数回溯窗口。",
        ),
        "chop_threshold": ParameterSpec(
            title="Choppiness震荡阈值",
            type="number",
            default=0.4,
            minimum=0.1,
            maximum=0.9,
            description="归一化到0~1后的Choppiness阈值，低于该值视为震荡市才允许入场。",
        ),
        "equity_fraction": ParameterSpec(
            title="单笔权益占比",
            type="number",
            default=0.10,
            minimum=0.01,
            maximum=0.20,
            description="每笔交易使用的账户权益比例，会被 max_equity_fraction 上限截断。",
        ),
    },
    plot_config={
        "main_plot": {
            "bb_mid": {"type": "line", "color": "#FFA500", "label": "布林带中轨"},
            "bb_up": {"type": "line", "color": "#E74C3C", "label": "布林带上轨"},
            "bb_down": {"type": "line", "color": "#2ECC71", "label": "布林带下轨"},
        },
        "subplots": {
            "choppiness": {
                "chop": {
                    "type": "line",
                    "color": "#2962FF",
                    "label": "Choppiness指数",
                },
            },
        },
    },
)
