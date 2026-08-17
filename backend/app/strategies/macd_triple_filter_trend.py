import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


class MacdTripleFilterConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    fast_ma_period: int = 12
    slow_ma_period: int = 26
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9
    atr_period: int = 10
    atr_min_threshold: float = 0.0
    chop_period: int = 14
    chop_threshold: float = 0.4
    position_size_pct: float = 0.1


class MacdTripleFilterTrendStrategy(Strategy):
    def __init__(self, config: MacdTripleFilterConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        bars = list(self.cache.bars(self.bar_type))
        warmup = max(
            self.config.slow_ma_period,
            self.config.macd_slow_period + self.config.macd_signal_period,
            self.config.atr_period,
            self.config.chop_period
        )
        if len(bars) < warmup:
            return

        closes = np.array([b.close.as_double() for b in bars])
        highs = np.array([b.high.as_double() for b in bars])
        lows = np.array([b.low.as_double() for b in bars])

        fast_ma = np.mean(closes[-self.config.fast_ma_period:])
        slow_ma = np.mean(closes[-self.config.slow_ma_period:])

        ema_fast = self.ema(closes, self.config.macd_fast_period)
        ema_slow = self.ema(closes, self.config.macd_slow_period)
        macd_lines = ema_fast - ema_slow
        signal_lines = self.ema(macd_lines, self.config.macd_signal_period)
        macd_line = macd_lines[-1]
        signal_line = signal_lines[-1]
        prev_macd = macd_lines[-2]
        prev_signal = signal_lines[-2]

        tr = np.maximum(
            highs[-self.config.atr_period:] - lows[-self.config.atr_period:],
            np.maximum(
                abs(highs[-self.config.atr_period:] - np.roll(closes, 1)[-self.config.atr_period:]),
                abs(lows[-self.config.atr_period:] - np.roll(closes, 1)[-self.config.atr_period:])
            )
        )
        atr = np.mean(tr)

        true_range = np.maximum(
            highs - lows,
            np.maximum(abs(highs - np.roll(closes, 1)), abs(lows - np.roll(closes, 1)))
        )
        sum_tr = np.sum(true_range[-self.config.chop_period:])
        range_hilo = np.max(highs[-self.config.chop_period:]) - np.min(lows[-self.config.chop_period:])
        choppiness = np.log10(sum_tr / range_hilo) / np.log10(self.config.chop_period) if range_hilo > 0 else 1.0

        positions = list(self.cache.positions())
        current_pos = positions[0].side if positions else None

        golden_cross = macd_line > signal_line and prev_macd <= prev_signal
        death_cross = macd_line < signal_line and prev_macd >= prev_signal
        bullish_trend = fast_ma > slow_ma
        bearish_trend = fast_ma < slow_ma
        atr_ok = atr >= self.config.atr_min_threshold
        is_trend = choppiness < self.config.chop_threshold

        filters_ok = atr_ok and is_trend

        if current_pos is None:
            if golden_cross and bullish_trend and filters_ok:
                self.open_position(PositionSide.LONG)
            elif death_cross and bearish_trend and filters_ok:
                self.open_position(PositionSide.SHORT)
        elif current_pos == PositionSide.LONG:
            if death_cross:
                self.close_position()
        elif current_pos == PositionSide.SHORT:
            if golden_cross:
                self.close_position()

    def on_stop(self):
        self.unsubscribe_bars(self.bar_type)

    def ema(self, values: np.ndarray, period: int) -> np.ndarray:
        return pd.Series(values).ewm(span=period, adjust=False).mean().values

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

    fast_ma_p = int(parameters.get('fast_ma_period', 12))
    slow_ma_p = int(parameters.get('slow_ma_period', 26))
    macd_fast_p = int(parameters.get('macd_fast_period', 12))
    macd_slow_p = int(parameters.get('macd_slow_period', 26))
    macd_signal_p = int(parameters.get('macd_signal_period', 9))
    atr_p = int(parameters.get('atr_period', 10))
    chop_p = int(parameters.get('chop_period', 14))

    df['fast_ma'] = df['close'].rolling(window=fast_ma_p).mean()
    df['slow_ma'] = df['close'].rolling(window=slow_ma_p).mean()

    df['ema_fast'] = df['close'].ewm(span=macd_fast_p, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=macd_slow_p, adjust=False).mean()
    df['macd'] = df['ema_fast'] - df['ema_slow']
    df['macd_signal'] = df['macd'].ewm(span=macd_signal_p, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    tr = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1)))
    )
    df['atr'] = tr.rolling(window=atr_p).mean()

    true_range = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift(1)), abs(df['low'] - df['close'].shift(1)))
    )
    rolling_sum_tr = true_range.rolling(window=chop_p).sum()
    rolling_max_high = df['high'].rolling(window=chop_p).max()
    rolling_min_low = df['low'].rolling(window=chop_p).min()
    range_hilo = rolling_max_high - rolling_min_low
    df['choppiness'] = np.where(
        range_hilo > 0,
        np.log10(rolling_sum_tr / range_hilo) / np.log10(chop_p),
        1.0
    )

    return df


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd_triple_filter_trend",
    name="MACD三重过滤趋势跟随",
    description="MACD金叉做多死叉做空，结合均线方向+ATR波动率+Choppiness震荡三重过滤，仅在趋势行情中交易",
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendStrategy",
    config_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterConfig",
    parameters={
        "fast_ma_period": ParameterSpec(title="快均线周期", type="integer", default=12, minimum=2, maximum=50),
        "slow_ma_period": ParameterSpec(title="慢均线周期", type="integer", default=26, minimum=5, maximum=200),
        "macd_fast_period": ParameterSpec(title="MACD快线周期", type="integer", default=12, minimum=2, maximum=50),
        "macd_slow_period": ParameterSpec(title="MACD慢线周期", type="integer", default=26, minimum=5, maximum=100),
        "macd_signal_period": ParameterSpec(title="MACD信号周期", type="integer", default=9, minimum=2, maximum=50),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=10, minimum=2, maximum=50),
        "atr_min_threshold": ParameterSpec(title="ATR最小阈值", type="number", default=0.0, minimum=0.0, maximum=1000.0),
        "chop_period": ParameterSpec(title="Choppiness周期", type="integer", default=14, minimum=5, maximum=50),
        "chop_threshold": ParameterSpec(title="Choppiness阈值", type="number", default=0.4, minimum=0.1, maximum=1.0),
        "position_size_pct": ParameterSpec(title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0),
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
            "MACD": {
                "macd": {"type": "line", "color": "#ff5555"},
                "macd_signal": {"type": "line", "color": "#55ff55"},
                "macd_hist": {"type": "bar", "color": "#5555ff"}
            },
            "ATR": {
                "atr": {"type": "line", "color": "#ff55ff"}
            },
            "Choppiness": {
                "choppiness": {"type": "line", "color": "#00aaff"}
            }
        }
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
