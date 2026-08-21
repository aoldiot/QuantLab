"""BTCUSDT 单均线（SMA）上穿买入、下穿卖出策略。

交易规则（已确认方案，保持最简单）:
1. 标的: 单标的（默认 BTCUSDT.BINANCE），主周期 1h。
2. 指标: 单条简单移动平均线 SMA(ma_period=20)，基于收盘价。
3. 开多: 收盘价上穿均线（上一根 close <= ma，当前 close > ma）且当前无多头仓位。
4. 平多: 收盘价下穿均线（上一根 close >= ma，当前 close < ma）且当前持有多头仓位。
5. 仅做多，不做空；每次固定数量 trade_size 下单，不叠加仓位。
6. 无额外过滤与止损，纯单均线信号。
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from app.strategy_base import QuantLabStrategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class BtcSingleMaCrossConfig(StrategyConfig, frozen=True):
    """单均线交叉策略配置。"""

    instrument_id: InstrumentId
    bar_type: BarType
    ma_period: int = 20
    trade_size: Decimal = Decimal("0.01")


class BtcSingleMaCrossStrategy(QuantLabStrategy):
    """价格上穿单均线买入、下穿单均线卖出的最简趋势跟随策略。"""

    def __init__(self, config: BtcSingleMaCrossConfig) -> None:
        super().__init__(config)
        self.ma_period = max(2, int(config.ma_period))
        self.trade_size = Decimal(str(config.trade_size))

        self.prev_close: float | None = None
        self.prev_ma: float | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()
        self.log.info(
            f"启动单均线策略: ma_period={self.ma_period} trade_size={self.trade_size}"
        )

    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)

        closes = self.get_close_series(bar.bar_type)
        if len(closes) < self.ma_period + 1:
            return

        ma_series = closes.rolling(window=self.ma_period, min_periods=self.ma_period).mean()
        ma_now = float(ma_series.iloc[-1])
        close_now = float(closes.iloc[-1])

        if ma_now != ma_now:  # NaN 保护
            return

        self.record("ma", ma_now)
        self.record("ma_dist", close_now - ma_now)

        prev_close = self.prev_close
        prev_ma = self.prev_ma
        if prev_close is None or prev_ma is None:
            prev_close = float(closes.iloc[-2])
            prev_ma = float(ma_series.iloc[-2])

        if prev_ma != prev_ma:  # 上一根均线仍为 NaN
            self.prev_close = close_now
            self.prev_ma = ma_now
            return

        cross_up = prev_close <= prev_ma and close_now > ma_now
        cross_down = prev_close >= prev_ma and close_now < ma_now

        if cross_up and not self.is_long():
            self.buy_market(trade_size=self.trade_size)
            self.log.info(f"上穿均线开多 @ {close_now} ma={ma_now}")
        elif cross_down and self.is_long():
            self.close_all_positions(self.instrument_id)
            self.log.info(f"下穿均线平多 @ {close_now} ma={ma_now}")

        self.prev_close = close_now
        self.prev_ma = ma_now


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算 plot_config 声明的全部指标列。"""
    result = dataframe.copy()
    close = pd.to_numeric(result["close"], errors="coerce").ffill().bfill()

    ma_period = max(2, int(parameters.get("ma_period") or 20))
    ma = close.rolling(window=ma_period, min_periods=1).mean()

    result["ma"] = ma.bfill().fillna(0.0)
    result["ma_dist"] = (close - ma).bfill().fillna(0.0)
    return result


STRATEGY_MANIFEST = StrategyManifest(
    slug="btc_single_ma_cross",
    name="BTC 单均线上下穿策略",
    version="1.0.0",
    description="单标的 1h 单条 SMA(20)：收盘价上穿均线买入、下穿均线卖出的最简纯多头趋势跟随策略。",
    category="trend",
    strategy_path="app.strategies.btc_single_ma_cross:BtcSingleMaCrossStrategy",
    config_path="app.strategies.btc_single_ma_cross:BtcSingleMaCrossConfig",
    parameters={
        "ma_period": ParameterSpec(
            title="均线周期",
            type="integer",
            default=20,
            minimum=2,
            maximum=400,
            description="单条简单移动平均线的计算周期。",
        ),
        "trade_size": ParameterSpec(
            title="下单数量 (BTC)",
            type="number",
            default=0.01,
            minimum=0.0001,
            maximum=100.0,
            description="每次开多的固定下单数量。",
        ),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"name": "收盘价", "type": "line", "color": "#ffffff"},
            "ma": {"name": "SMA 均线", "type": "line", "color": "#43a5ff"},
        },
        "subplots": {
            "价格-均线偏离": {
                "ma_dist": {"name": "收盘价-均线", "type": "histogram", "color": "#22c55e"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=False,
)
