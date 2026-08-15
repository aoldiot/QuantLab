# NautilusTrader Strategy Reference for QuantLab

## Standard Imports
```python
from decimal import Decimal
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

# Common Indicators
from nautilus_trader.indicators.macd import MovingAverageConvergenceDivergence
from nautilus_trader.indicators.ema import ExponentialMovingAverage
from nautilus_trader.indicators.sma import SimpleMovingAverage
from nautilus_trader.indicators.atr import AverageTrueRange
from nautilus_trader.indicators.rsi import RelativeStrengthIndex
from nautilus_trader.indicators.bollinger_bands import BollingerBands

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode
```

## Structure & Contracts

Every strategy Python file in `backend/app/strategies/<name>.py` must export 4 elements:

1. `StrategyConfig` subclass (with `frozen=True`):
```python
class MyStrategyConfig(StrategyConfig, frozen=True):
    instrument_ids: list[InstrumentId]
    bar_types: list[BarType]
    trade_size: Decimal = Decimal("0.001")
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
```

2. `Strategy` subclass:
```python
class MyStrategy(Strategy):
    def __init__(self, config: MyStrategyConfig) -> None:
        super().__init__(config)
        self.macd = MovingAverageConvergenceDivergence(
            fast_period=config.fast_period,
            slow_period=config.slow_period,
            signal_period=config.signal_period,
        )

    def on_start(self) -> None:
        for bar_type in self.config.bar_types:
            self.subscribe_bars(bar_type)
            self.register_indicator_for_bars(bar_type, self.macd)

    def on_bar(self, bar: Bar) -> None:
        if not self.macd.initialized:
            return

        instrument_id = bar.bar_type.instrument_id
        position = self.portfolio.position(instrument_id)
        is_long = position is not None and position.is_open and position.quantity > 0
        is_short = position is not None and position.is_open and position.quantity < 0

        # MACD Line value vs Signal line value
        macd_val = float(self.macd.value)
        signal_val = float(self.macd.signal)

        if macd_val > signal_val and not is_long:
            if is_short:
                self.close_all_positions(instrument_id)
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.BUY,
                quantity=Quantity.from_decimal(self.config.trade_size),
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
        elif macd_val < signal_val and not is_short:
            if is_long:
                self.close_all_positions(instrument_id)
            order = self.order_factory.market(
                instrument_id=instrument_id,
                order_side=OrderSide.SELL,
                quantity=Quantity.from_decimal(self.config.trade_size),
                time_in_force=TimeInForce.GTC,
            )
            self.submit_order(order)
```

3. `calculate_indicators` function:
```python
def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    close = pd.to_numeric(dataframe["close"])
    fast = int(parameters.get("fast_period", 12))
    slow = int(parameters.get("slow_period", 26))
    signal = int(parameters.get("signal_period", 9))
    
    fast_ema = close.ewm(span=fast, adjust=False).mean()
    slow_ema = close.ewm(span=slow, adjust=False).mean()
    dataframe["macd_line"] = fast_ema - slow_ema
    dataframe["macd_signal"] = dataframe["macd_line"].ewm(span=signal, adjust=False).mean()
    dataframe["macd_hist"] = dataframe["macd_line"] - dataframe["macd_signal"]
    return dataframe
```

4. `STRATEGY_MANIFEST` instance:
```python
STRATEGY_MANIFEST = StrategyManifest(
    slug="my-strategy-slug",
    name="MyStrategy",
    version="0.1.0",
    description="策略说明",
    category="趋势策略",
    strategy_path="app.strategies.my_strategy:MyStrategy",
    config_path="app.strategies.my_strategy:MyStrategyConfig",
    parameters={
        "trade_size": ParameterSpec("下单数量", "number", 0.001, 0.000001, 1000),
        "fast_period": ParameterSpec("快线周期", "integer", 12, 2, 200),
        "slow_period": ParameterSpec("慢线周期", "integer", 26, 2, 200),
        "signal_period": ParameterSpec("信号线周期", "integer", 9, 2, 100),
    },
    timeframes=("1h",),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {},
        "subplots": {
            "macd": {
                "macd_line": {"name": "MACD", "type": "line", "color": "#26a69a"},
                "macd_signal": {"name": "Signal", "type": "line", "color": "#ef5350"},
                "macd_hist": {"name": "Histogram", "type": "bar", "color": "#42a5f5"},
            }
        },
    },
    mode=StrategyMode.PORTFOLIO,
)
```
