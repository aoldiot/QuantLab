# NautilusTrader API Quick Reference for QuantLab

## Standard Imports
```python
from decimal import Decimal
import pandas as pd
import numpy as np

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode
```

## Golden Template
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


class BtcEmaAtrTrendConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    fast_period: int = 12
    slow_period: int = 26
    atr_period: int = 14
    trade_size: Decimal = Decimal("0.01")


class BtcEmaAtrTrendStrategy(Strategy):
    def __init__(self, config: BtcEmaAtrTrendConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id if isinstance(config.instrument_id, InstrumentId) else InstrumentId.from_str(str(config.instrument_id))
        self.bar_type = config.bar_type if isinstance(config.bar_type, BarType) else BarType.from_str(str(config.bar_type))
        self.fast_period = config.fast_period
        self.slow_period = config.slow_period
        self.atr_period = config.atr_period
        self.trade_size = Quantity.from_str(str(config.trade_size)) if isinstance(config.trade_size, (Decimal, float, str)) else config.trade_size
        self.bars: list[Bar] = []

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        warmup = max(self.slow_period, self.atr_period) + 5
        if len(self.bars) < warmup:
            return

        closes = pd.Series([b.close.as_double() for b in self.bars[-100:]])
        fast_ma = closes.ewm(span=self.fast_period, adjust=False).mean().iloc[-1]
        slow_ma = closes.ewm(span=self.slow_period, adjust=False).mean().iloc[-1]
        prev_fast = closes.ewm(span=self.fast_period, adjust=False).mean().iloc[-2]
        prev_slow = closes.ewm(span=self.slow_period, adjust=False).mean().iloc[-2]

        is_long = self.portfolio.is_net_long(self.instrument_id)
        is_flat = self.portfolio.is_flat(self.instrument_id)

        # Long Entry: Golden cross
        if prev_fast <= prev_slow and fast_ma > slow_ma and not is_long:
            if not is_flat:
                self.close_all_positions(self.instrument_id)
            qty = self.instrument.make_qty(self.config.trade_size) if hasattr(self, "instrument") and self.instrument else self.trade_size
            order = self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=OrderSide.BUY,
                quantity=qty,
            )
            self.submit_order(order)
        # Exit: Death cross
        elif prev_fast >= prev_slow and fast_ma < slow_ma and is_long:
            self.close_all_positions(self.instrument_id)

    def on_stop(self) -> None:
        self.unsubscribe_bars(self.bar_type)


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    result = df.copy()
    fast_p = int(parameters.get("fast_period", 12))
    slow_p = int(parameters.get("slow_period", 26))
    atr_p = int(parameters.get("atr_period", 14))

    close = pd.to_numeric(result["close"], errors="coerce")
    high = pd.to_numeric(result["high"], errors="coerce")
    low = pd.to_numeric(result["low"], errors="coerce")

    result["fast_ma"] = close.ewm(span=fast_p, adjust=False).mean().bfill()
    result["slow_ma"] = close.ewm(span=slow_p, adjust=False).mean().bfill()

    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    result["atr"] = tr.rolling(window=atr_p).mean().bfill().fillna(0.0)
    return result


STRATEGY_MANIFEST = StrategyManifest(
    slug="btc_ema_atr_trend",
    name="BTC EMA ATR 趋势策略",
    description="基于双均线金叉与 ATR 波动率风控的趋势跟踪策略",
    category="trend",
    strategy_path="app.strategies.btc_ema_atr_trend:BtcEmaAtrTrendStrategy",
    config_path="app.strategies.btc_ema_atr_trend:BtcEmaAtrTrendConfig",
    parameters={
        "fast_period": ParameterSpec(title="快线周期", type="integer", default=12, minimum=2, maximum=100),
        "slow_period": ParameterSpec(title="慢线周期", type="integer", default=26, minimum=5, maximum=200),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=14, minimum=2, maximum=100),
        "trade_size": ParameterSpec(title="交易数量", type="number", default=0.01, minimum=0.0001, maximum=10.0),
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
    supports_short=True,
)
```

## API Anti-Patterns vs. Best Practices

| ❌ 错误写法 (导致 L1/L4 失败) | ✅ 正确标准写法 | 说明 |
| :--- | :--- | :--- |
| `self.portfolio.account_balance()` | `self.portfolio.equity(self.instrument_id.venue)` | Portfolio 无 account_balance 方法 |
| `self.portfolio.is_net_flat(...)` | `self.portfolio.is_flat(self.instrument_id)` | Portfolio 无 is_net_flat 方法 |
| `self.portfolio.position(instrument_id)` | `self.portfolio.net_position(instrument_id)` 或 `is_flat` | Portfolio 无 position 方法 |
| `self.close_position(instrument_id)` | `self.close_all_positions(self.instrument_id)` | 平仓指定标的必须使用 close_all_positions |
| `self.instrument.round_quantity(qty)` | `self.instrument.make_qty(qty)` | Instrument 无 round_quantity 方法 |
| `quantity=0.01` (裸 float/int) | `quantity=self.instrument.make_qty(...)` | 下单 quantity 必须为 Quantity 对象 |
