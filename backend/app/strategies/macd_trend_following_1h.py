from decimal import Decimal
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import BarType
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class MacdTrendFollowing1hConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]
    trade_size: Decimal = Decimal("0.001")


class MacdTrendFollowing1hStrategy(Strategy):
    def __init__(self, config: MacdTrendFollowing1hConfig) -> None:
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
    slug="macd-trend-following-1h",
    name="MacdTrendFollowing1h",
    version="0.1.0",
    description='MACD金叉死叉可以有效捕捉中期趋势行情，结合均线方向、ATR波动率、Choppiness指数三重过滤，可以大幅过滤震荡市中的无效信号，提升策略整体胜率和盈亏比，策略绩效优于无过滤的纯MACD金叉死叉策略',
    category='研究策略',
    strategy_path="app.strategies.macd_trend_following_1h:MacdTrendFollowing1hStrategy",
    config_path="app.strategies.macd_trend_following_1h:MacdTrendFollowing1hConfig",
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
