import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


class BollingerMeanReversionConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    chop_period: int = 14
    chop_threshold: float = 0.4
    position_size_pct: float = 0.1


class BollingerMeanReversionStrategy(Strategy):
    def __init__(self, config: BollingerMeanReversionConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        bars = list(self.cache.bars(self.bar_type))
        warmup = max(self.config.bollinger_period, self.config.chop_period)
        if len(bars) < warmup:
            return

        closes = np.array([b.close.as_double() for b in bars])
        highs = np.array([b.high.as_double() for b in bars])
        lows = np.array([b.low.as_double() for b in bars])

        # Calculate Bollinger Bands
        sma = np.mean(closes[-self.config.bollinger_period:])
        std = np.std(closes[-self.config.bollinger_period:])
        upper_band = sma + self.config.bollinger_std * std
        lower_band = sma - self.config.bollinger_std * std

        # Calculate Choppiness Index
        true_ranges = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            true_ranges.append(tr)
        sum_tr = sum(true_ranges[-self.config.chop_period:])
        highest_high = max(highs[-self.config.chop_period:])
        lowest_low = min(lows[-self.config.chop_period:])
        choppiness = np.log10(sum_tr / (highest_high - lowest_low)) / np.log10(self.config.chop_period)

        current_close = bar.close.as_double()
        positions = self.cache.positions()
        current_pos = positions[0].side if positions else None

        if current_pos is None:
            # Check entry conditions
            if current_close <= lower_band and choppiness > self.config.chop_threshold:
                self.open_position(PositionSide.LONG)
            elif current_close >= upper_band and choppiness > self.config.chop_threshold:
                self.open_position(PositionSide.SHORT)
        else:
            # Check exit condition (close at middle band)
            if (current_pos == PositionSide.LONG and current_close >= sma) or \
               (current_pos == PositionSide.SHORT and current_close <= sma):
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

    bollinger_period = int(parameters.get('bollinger_period', 20))
    bollinger_std = float(parameters.get('bollinger_std', 2.0))
    chop_period = int(parameters.get('chop_period', 14))

    # Calculate Bollinger Bands
    df['bollinger_mid'] = df['close'].rolling(window=bollinger_period).mean()
    df['bollinger_std'] = df['close'].rolling(window=bollinger_period).std()
    df['bollinger_upper'] = df['bollinger_mid'] + bollinger_std * df['bollinger_std']
    df['bollinger_lower'] = df['bollinger_mid'] - bollinger_std * df['bollinger_std']

    # Calculate Choppiness Index
    tr = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1)))
    )
    tr_sum = tr.rolling(window=chop_period).sum()
    highest_high = df['high'].rolling(window=chop_period).max()
    lowest_low = df['low'].rolling(window=chop_period).min()
    df['choppiness'] = np.log10(tr_sum / (highest_high - lowest_low)) / np.log10(chop_period)

    return df


STRATEGY_MANIFEST = StrategyManifest(
    slug="bollinger_mean_reversion",
    name="布林带均值回归反转策略",
    description="布林带均值回归策略，上轨做空下轨做多，中轨平仓，配合Choppiness震荡过滤",
    version="1.0.0",
    category="mean_reversion",
    strategy_path="app.strategies.bollinger_mean_reversion:BollingerMeanReversionStrategy",
    config_path="app.strategies.bollinger_mean_reversion:BollingerMeanReversionConfig",
    parameters={
        "bollinger_period": ParameterSpec(title="布林带周期", type="integer", default=20, minimum=10, maximum=50),
        "bollinger_std": ParameterSpec(title="布林带标准差倍数", type="number", default=2.0, minimum=1.0, maximum=3.0),
        "chop_period": ParameterSpec(title="Choppiness周期", type="integer", default=14, minimum=5, maximum=30),
        "chop_threshold": ParameterSpec(title="Choppiness阈值", type="number", default=0.4, minimum=0.2, maximum=0.6),
        "position_size_pct": ParameterSpec(title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "bollinger_mid": {"type": "line", "color": "#aaaaaa"},
            "bollinger_upper": {"type": "line", "color": "#ff5555"},
            "bollinger_lower": {"type": "line", "color": "#55ff55"},
        },
        "subplots": {
            "Choppiness": {
                "choppiness": {"type": "line", "color": "#00aaff"}
            }
        }
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)