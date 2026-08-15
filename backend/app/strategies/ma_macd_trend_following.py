from decimal import Decimal
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class MaMacdTrendFollowingConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]
    trade_size: Decimal = Decimal("0.001")


class MaMacdTrendFollowingStrategy(Strategy):
    def __init__(self, config: MaMacdTrendFollowingConfig) -> None:
        super().__init__(config)

    def on_start(self) -> None:
        for bar_type in self.config.bar_types:
            self.subscribe_bars(bar_type)

    # 在这里实现 on_bar、下单和风控逻辑


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    # 所有 plot_config 引用的列都必须在这里计算。
    dataframe["ema_20"] = pd.to_numeric(dataframe["close"]).ewm(span=20, adjust=False).mean()
    return dataframe


STRATEGY_MANIFEST = StrategyManifest(
    slug="ma-macd-trend-following",
    name="MaMacdTrendFollowing",
    version="0.1.0",
    description='价格趋势一旦形成会持续延伸，MA均线多头/空头排列可以确认趋势方向，MACD金叉/死叉可以提供趋势启动的入场信号，双重信号过滤能够降低假信号概率，提升趋势跟踪的收益风险比',
    category='研究策略',
    strategy_path="app.strategies.ma_macd_trend_following:MaMacdTrendFollowingStrategy",
    config_path="app.strategies.ma_macd_trend_following:MaMacdTrendFollowingConfig",
    parameters={
        "trade_size": ParameterSpec("下单数量", "number", 0.001, 0.000001, 1000),
    },
    timeframes=("1h",),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "ema_20": {"name": "EMA 20", "type": "line", "color": "#43a5ff"},
        },
        "subplots": {},
    },
    mode=StrategyMode.PORTFOLIO,
)
