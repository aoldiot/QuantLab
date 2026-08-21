"""BTCUSDT 双均线（EMA）金叉死叉多空反手策略。

交易规则（已确认方案）:
1. 标的: BTCUSDT.BINANCE，主周期 1h。
2. 指标: 快线 EMA(fast_period=20)、慢线 EMA(slow_period=60)。
3. 开多: 快线上穿慢线（上一根 fast <= slow，当前 fast > slow）。
4. 开空: 快线下穿慢线（上一根 fast >= slow，当前 fast < slow），受 allow_short 开关控制。
5. 反手: 持有反向仓位时先平仓，再按新方向开仓；allow_short=False 时死叉仅平多。
6. 可选风控: stop_loss_pct > 0 时，按开仓均价的固定百分比止损平仓（默认 0 表示关闭，纯均线信号）。
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from app.strategy_base import QuantLabStrategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class BtcDualMaCrossConfig(StrategyConfig, frozen=True):
    """双均线交叉策略配置。"""

    instrument_id: InstrumentId
    bar_type: BarType
    fast_period: int = 20
    slow_period: int = 60
    trade_size: Decimal = Decimal("0.01")
    stop_loss_pct: float = 0.0
    allow_short: bool = True


class BtcDualMaCrossStrategy(QuantLabStrategy):
    """快慢 EMA 交叉的多空趋势跟随策略。"""

    def __init__(self, config: BtcDualMaCrossConfig) -> None:
        super().__init__(config)
        self.fast_period = int(config.fast_period)
        self.slow_period = int(config.slow_period)
        self.trade_size = Decimal(str(config.trade_size))
        self.stop_loss_pct = float(config.stop_loss_pct)
        self.allow_short = bool(config.allow_short)

        self.prev_fast: float | None = None
        self.prev_slow: float | None = None
        self.entry_price: float = 0.0
        self.entry_side: str = "FLAT"

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()
        self.log.info(
            f"启动双均线策略: fast={self.fast_period} slow={self.slow_period} "
            f"size={self.trade_size} short={self.allow_short} sl={self.stop_loss_pct}"
        )

    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)

        closes = self.get_close_series(bar.bar_type)
        warmup = max(self.fast_period, self.slow_period) + 2
        if len(closes) < warmup:
            return

        fast_series = closes.ewm(span=self.fast_period, adjust=False).mean()
        slow_series = closes.ewm(span=self.slow_period, adjust=False).mean()

        fast_now = float(fast_series.iloc[-1])
        slow_now = float(slow_series.iloc[-1])
        fast_prev = float(fast_series.iloc[-2]) if self.prev_fast is None else float(self.prev_fast)
        slow_prev = float(slow_series.iloc[-2]) if self.prev_slow is None else float(self.prev_slow)

        self.record("fast_ma", fast_now)
        self.record("slow_ma", slow_now)
        self.record("ma_diff", fast_now - slow_now)

        close_price = float(bar.close.as_double())

        # 固定百分比止损（可选风控）
        if self.stop_loss_pct > 0.0 and self.entry_price > 0.0:
            if self.entry_side == "LONG" and close_price <= self.entry_price * (1.0 - self.stop_loss_pct):
                self._flatten("多头固定止损")
                self.prev_fast, self.prev_slow = fast_now, slow_now
                return
            if self.entry_side == "SHORT" and close_price >= self.entry_price * (1.0 + self.stop_loss_pct):
                self._flatten("空头固定止损")
                self.prev_fast, self.prev_slow = fast_now, slow_now
                return

        golden_cross = fast_prev <= slow_prev and fast_now > slow_now
        death_cross = fast_prev >= slow_prev and fast_now < slow_now

        if golden_cross:
            if self.is_short():
                self._flatten("金叉平空反手")
            if not self.is_long():
                self.buy_market(trade_size=self.trade_size)
                self.entry_price = close_price
                self.entry_side = "LONG"
                self.log.info(f"金叉开多 @ {close_price}")
        elif death_cross:
            if self.is_long():
                self._flatten("死叉平多")
            if self.allow_short and not self.is_short():
                self.sell_market(trade_size=self.trade_size)
                self.entry_price = close_price
                self.entry_side = "SHORT"
                self.log.info(f"死叉开空 @ {close_price}")

        self.prev_fast = fast_now
        self.prev_slow = slow_now

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _flatten(self, reason: str) -> None:
        self.close_position()
        self.entry_price = 0.0
        self.entry_side = "FLAT"
        self.log.info(f"平仓: {reason}")


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算 plot_config 声明的全部指标列。"""
    result = dataframe.copy()
    close = pd.to_numeric(result["close"], errors="coerce").ffill().bfill()

    fast_period = int(parameters.get("fast_period") or 20)
    slow_period = int(parameters.get("slow_period") or 60)

    fast_ma = close.ewm(span=max(2, fast_period), adjust=False).mean()
    slow_ma = close.ewm(span=max(2, slow_period), adjust=False).mean()

    result["fast_ma"] = fast_ma.bfill().fillna(0.0)
    result["slow_ma"] = slow_ma.bfill().fillna(0.0)
    result["ma_diff"] = (fast_ma - slow_ma).bfill().fillna(0.0)
    return result


STRATEGY_MANIFEST = StrategyManifest(
    slug="btc_dual_ma_cross",
    name="BTC 双均线交叉多空策略",
    version="1.0.0",
    description="BTCUSDT 1h 双 EMA（20/60）金叉开多、死叉开空的多空反手趋势策略，可选固定百分比止损。",
    category="trend",
    strategy_path="app.strategies.btc_dual_ma_cross:BtcDualMaCrossStrategy",
    config_path="app.strategies.btc_dual_ma_cross:BtcDualMaCrossConfig",
    parameters={
        "fast_period": ParameterSpec(title="快线 EMA 周期", type="integer", default=20, minimum=2, maximum=200),
        "slow_period": ParameterSpec(title="慢线 EMA 周期", type="integer", default=60, minimum=5, maximum=500),
        "trade_size": ParameterSpec(title="下单数量 (BTC)", type="number", default=0.01, minimum=0.0001, maximum=100.0),
        "stop_loss_pct": ParameterSpec(title="固定止损比例 (0=关闭)", type="number", default=0.0, minimum=0.0, maximum=0.5),
        "allow_short": ParameterSpec(title="允许做空", type="boolean", default=True),
    },
    timeframes=("15m", "1h", "4h"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"name": "收盘价", "type": "line", "color": "#ffffff"},
            "fast_ma": {"name": "EMA 快线", "type": "line", "color": "#43a5ff"},
            "slow_ma": {"name": "EMA 慢线", "type": "line", "color": "#ffaa00"},
        },
        "subplots": {
            "均线差值": {
                "ma_diff": {"name": "快线-慢线", "type": "histogram", "color": "#22c55e"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
)
