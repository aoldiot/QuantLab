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


class BollingerMeanReversionConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    bollinger_period: int = 20
    std_dev_multiplier: float = 2.0
    exit_on_mid_band: bool = True
    stop_on_opposite_band: bool = True
    position_size_pct: float = 0.1


class BollingerMeanReversionStrategy(Strategy):
    def __init__(self, config: BollingerMeanReversionConfig) -> None:
        super().__init__(config)
        self.instrument_id = config.instrument_id
        self.bar_type = config.bar_type
        self.bollinger_period = config.bollinger_period
        self.std_dev_multiplier = config.std_dev_multiplier
        self.exit_on_mid_band = config.exit_on_mid_band
        self.stop_on_opposite_band = config.stop_on_opposite_band
        self.position_size_pct = config.position_size_pct
        self.bars: list[Bar] = []
        self.middle_band: float | None = None
        self.upper_band: float | None = None
        self.lower_band: float | None = None

    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def _calculate_bollinger(self) -> None:
        closes = np.array([b.close.as_double() for b in self.bars[-self.bollinger_period:]])
        self.middle_band = closes.mean()
        std_dev = closes.std(ddof=0)
        self.upper_band = self.middle_band + self.std_dev_multiplier * std_dev
        self.lower_band = self.middle_band - self.std_dev_multiplier * std_dev

    def _calculate_position_size(self) -> Quantity:
        equity_dict = self.portfolio.equity(self.instrument_id.venue)
        if equity_dict:
            total_equity = sum(m.as_double() for m in equity_dict.values())
        else:
            total_equity = 10000.0
        price = self.bars[-1].close.as_double()
        notional = total_equity * self.position_size_pct
        size = notional / price if price > 0 else 0.001
        return self.instrument.make_qty(Decimal(str(round(size, 8))))

    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        if len(self.bars) < self.bollinger_period:
            return

        self._calculate_bollinger()
        current_close = bar.close.as_double()
        is_long = self.portfolio.is_net_long(self.instrument_id)
        is_short = self.portfolio.is_net_short(self.instrument_id)
        is_flat = self.portfolio.is_flat(self.instrument_id)
        position_size = self._calculate_position_size()

        if is_flat:
            if current_close < self.lower_band:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=OrderSide.BUY,
                    quantity=position_size,
                )
                self.submit_order(order)
                self.log.info(f"Enter LONG: price {current_close:.2f}, lower_band {self.lower_band:.2f}")
            elif current_close > self.upper_band:
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=OrderSide.SELL,
                    quantity=position_size,
                )
                self.submit_order(order)
                self.log.info(f"Enter SHORT: price {current_close:.2f}, upper_band {self.upper_band:.2f}")

        elif is_long:
            if self.exit_on_mid_band and current_close >= self.middle_band:
                self.close_all_positions(self.instrument_id)
                self.log.info(f"Exit LONG (TP): price {current_close:.2f}, mid_band {self.middle_band:.2f}")
            elif self.stop_on_opposite_band and current_close >= self.upper_band:
                self.close_all_positions(self.instrument_id)
                self.log.info(f"Exit LONG (SL): price {current_close:.2f}, upper_band {self.upper_band:.2f}")

        elif is_short:
            if self.exit_on_mid_band and current_close <= self.middle_band:
                self.close_all_positions(self.instrument_id)
                self.log.info(f"Exit SHORT (TP): price {current_close:.2f}, mid_band {self.middle_band:.2f}")
            elif self.stop_on_opposite_band and current_close <= self.lower_band:
                self.close_all_positions(self.instrument_id)
                self.log.info(f"Exit SHORT (SL): price {current_close:.2f}, lower_band {self.lower_band:.2f}")

    def on_stop(self) -> None:
        self.unsubscribe_bars(self.bar_type)


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    result = df.copy()
    period = int(parameters.get("bollinger_period", 20))
    std_multiplier = float(parameters.get("std_dev_multiplier", 2.0))
    close = pd.to_numeric(result["close"], errors="coerce")

    result["middle_band"] = close.rolling(window=period).mean().bfill()
    result["rolling_std"] = close.rolling(window=period).std(ddof=0).bfill()
    result["upper_band"] = result["middle_band"] + (std_multiplier * result["rolling_std"])
    result["lower_band"] = result["middle_band"] - (std_multiplier * result["rolling_std"])
    result = result.drop(columns=["rolling_std"])

    return result


STRATEGY_MANIFEST = StrategyManifest(
    name="Bollinger Bands Mean Reversion",
    slug="bollinger_mean_reversion",
    description="Mean reversion strategy based on Bollinger Bands: enter on break of outer bands, exit on return to mid band or opposite band stop loss",
    strategy_path="app.strategies.bollinger_mean_reversion:BollingerMeanReversionStrategy",
    config_path="app.strategies.bollinger_mean_reversion:BollingerMeanReversionConfig",
    parameters={
        "bollinger_period": ParameterSpec(
            title="Bollinger Period",
            type="integer",
            default=20,
            minimum=5,
            maximum=200,
        ),
        "std_dev_multiplier": ParameterSpec(
            title="Standard Deviation Multiplier",
            type="number",
            default=2.0,
            minimum=0.5,
            maximum=5.0,
        ),
        "exit_on_mid_band": ParameterSpec(
            title="Exit on Middle Band",
            type="boolean",
            default=True,
            minimum=None,
            maximum=None,
        ),
        "stop_on_opposite_band": ParameterSpec(
            title="Stop on Opposite Band",
            type="boolean",
            default=True,
            minimum=None,
            maximum=None,
        ),
        "position_size_pct": ParameterSpec(
            title="Position Size (%)",
            type="number",
            default=0.1,
            minimum=0.01,
            maximum=1.0,
        )
    },
    timeframes=("1h", "4h", "1d"),
    primary_timeframe="1h",
    mode=StrategyMode.BOTH,
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "middle_band": {"type": "line", "color": "#ffaa00"},
            "upper_band": {"type": "line", "color": "#ff4444"},
            "lower_band": {"type": "line", "color": "#44ff44"},
        },
        "subplots": {}
    }
)