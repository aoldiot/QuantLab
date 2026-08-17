import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.indicators import ExponentialMovingAverage, MovingAverage, MovingAverageConvergenceDivergence, AverageTrueRange
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


class MacdTripleFilterTrendConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    ema_fast_period: int = 12
    ema_slow_period: int = 26
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9
    atr_period: int = 10
    atr_ma_period: int = 20
    atr_multiplier_threshold: float = 0.8
    chop_period: int = 14
    chop_threshold: float = 0.4
    position_size_pct: float = 0.1


class ChoppinessIndex:
    """Custom Choppiness Index indicator for manual calculation"""
    def __init__(self, period: int):
        self.period = period
        self.highs = []
        self.lows = []
        self.closes = []
        self.initialized = False

    def update(self, high: float, low: float, close: float):
        self.highs.append(high)
        self.lows.append(low)
        self.closes.append(close)
        
        if len(self.highs) > self.period:
            self.highs.pop(0)
            self.lows.pop(0)
            self.closes.pop(0)
            
        self.initialized = len(self.highs) == self.period

    def value(self) -> float:
        if not self.initialized:
            return np.nan
            
        highest_high = max(self.highs)
        lowest_low = min(self.lows)
        sum_tr = sum([abs(self.highs[i] - self.lows[i]) for i in range(len(self.highs))])
        
        if highest_high == lowest_low or sum_tr == 0:
            return 1.0
            
        ci = 100 * np.log10(sum_tr / (highest_high - lowest_low)) / np.log10(float(self.period))
        return ci / 100


class MacdTripleFilterTrendStrategy(Strategy):
    def __init__(self, config: MacdTripleFilterTrendConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        
        # Store config parameters as instance variables
        self.ema_fast_period = config.ema_fast_period
        self.ema_slow_period = config.ema_slow_period
        self.macd_fast_period = config.macd_fast_period
        self.macd_slow_period = config.macd_slow_period
        self.macd_signal_period = config.macd_signal_period
        self.atr_period = config.atr_period
        self.atr_ma_period = config.atr_ma_period
        self.atr_multiplier_threshold = config.atr_multiplier_threshold
        self.chop_period = config.chop_period
        self.chop_threshold = config.chop_threshold
        self.position_size_pct = config.position_size_pct
        
        # Indicators will be initialized in on_start()
        self.ema_fast = None
        self.ema_slow = None
        self.macd = None
        self.atr = None
        self.atr_ma = None
        self.chop = None
        
        # Track previous MACD state for cross detection
        self.prev_macd_diff = None
        self.instrument = None

    def on_start(self):
        # Initialize indicators
        self.ema_fast = ExponentialMovingAverage(self.ema_fast_period)
        self.ema_slow = ExponentialMovingAverage(self.ema_slow_period)
        self.macd = MovingAverageConvergenceDivergence(
            fast_period=self.macd_fast_period,
            slow_period=self.macd_slow_period,
            signal_period=self.macd_signal_period
        )
        self.atr = AverageTrueRange(self.atr_period)
        self.atr_ma = MovingAverage.simple(self.atr_ma_period)
        self.chop = ChoppinessIndex(self.chop_period)
        
        # Register indicators for auto-update
        self.instrument = self.cache.instrument(self.instrument_id)
        self.register_indicator_for_bars(self.bar_type, self.ema_fast)
        self.register_indicator_for_bars(self.bar_type, self.ema_slow)
        self.register_indicator_for_bars(self.bar_type, self.macd)
        self.register_indicator_for_bars(self.bar_type, self.atr)
        self.subscribe_bars(self.bar_type)

    def get_current_position(self) -> PositionSide | None:
        for pos in self.cache.positions():
            if pos.instrument_id == self.instrument_id:
                return pos.side
        return None

    def close_all_positions(self):
        for pos in self.cache.positions():
            if pos.instrument_id == self.instrument_id:
                order_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
                order = self.order_factory.market(
                    instrument_id=self.instrument_id,
                    order_side=order_side,
                    quantity=pos.quantity,
                )
                self.submit_order(order)

    def open_position(self, side: PositionSide):
        account = self.portfolio.account(self.instrument_id.venue)
        free_balance = account.balance_free(self.instrument.quote_currency).as_double()
        position_size = (free_balance * self.position_size_pct) / self.instrument.price_increment
        qty = Quantity.from_int(int(position_size)) if position_size.is_integer() else Quantity(str(round(position_size, 4)))
        order_side = OrderSide.BUY if side == PositionSide.LONG else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=order_side,
            quantity=qty,
        )
        self.submit_order(order)

    def on_bar(self, bar: Bar):
        # Update manual indicators (Choppiness Index and ATR MA)
        high = bar.high.as_double()
        low = bar.low.as_double()
        close = bar.close.as_double()
        self.chop.update(high, low, close)
        
        if self.atr.initialized:
            self.atr_ma.update(self.atr.value)
        
        # Check if all indicators are initialized
        warmup_needed = max(
            self.ema_slow_period,
            self.macd_slow_period + self.macd_signal_period,
            self.atr_period + self.atr_ma_period,
            self.chop_period
        )
        
        if not all([
            self.ema_fast.initialized,
            self.ema_slow.initialized,
            self.macd.initialized,
            self.atr.initialized,
            self.atr_ma.initialized,
            self.chop.initialized
        ]):
            self.prev_macd_diff = self.macd.value - self.macd.signal
            return

        # Get current indicator values
        ema_fast_val = self.ema_fast.value
        ema_slow_val = self.ema_slow.value
        macd_val = self.macd.value
        signal_val = self.macd.signal
        current_macd_diff = macd_val - signal_val
        atr_val = self.atr.value
        atr_ma_val = self.atr_ma.value
        chop_val = self.chop.value()

        # Check for crosses
        golden_cross = False
        death_cross = False
        if self.prev_macd_diff is not None:
            golden_cross = self.prev_macd_diff < 0 and current_macd_diff >= 0
            death_cross = self.prev_macd_diff > 0 and current_macd_diff <= 0

        # Update previous MACD state
        self.prev_macd_diff = current_macd_diff

        # Get current position
        current_pos = self.get_current_position()

        # Triple Filter check
        # Filter 1: EMA direction matches signal direction
        ema_bullish = ema_fast_val > ema_slow_val
        ema_bearish = ema_fast_val < ema_slow_val
        
        # Filter 2: ATR volatility threshold check
        atr_ok = atr_val >= (atr_ma_val * self.atr_multiplier_threshold)
        
        # Filter 3: Choppiness check (value < threshold = trending)
        trend_ok = chop_val < self.chop_threshold
        
        # Process exits first (signal reversal close)
        if current_pos is not None:
            if current_pos == PositionSide.LONG and death_cross:
                self.close_all_positions()
            elif current_pos == PositionSide.SHORT and golden_cross:
                self.close_all_positions()

        # Process entries (only if no position)
        if current_pos is None:
            # Long entry: all filters + golden cross
            if golden_cross and ema_bullish and atr_ok and trend_ok:
                self.open_position(PositionSide.LONG)
            # Short entry: all filters + death cross
            elif death_cross and ema_bearish and atr_ok and trend_ok:
                self.open_position(PositionSide.SHORT)

    def on_stop(self):
        self.unsubscribe_bars(self.bar_type)


def calculate_choppiness(df: pd.DataFrame, period: int) -> pd.Series:
    """Vectorized calculation of Choppiness Index"""
    highest_high = df['high'].rolling(window=period).max()
    lowest_low = df['low'].rolling(window=period).min()
    tr = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    sum_tr = tr.rolling(window=period).sum()
    
    ci = pd.Series(np.full(len(df), np.nan), index=df.index)
    mask = (highest_high != lowest_low) & (sum_tr != 0) & (~highest_high.isna())
    ci[mask] = 100 * np.log10(sum_tr[mask] / (highest_high[mask] - lowest_low[mask])) / np.log10(float(period))
    ci[mask] = ci[mask] / 100
    return ci


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    df = df.copy()
    for col in ['open', 'high', 'low', 'close', 'volume']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    # Get parameters
    ema_fast_period = int(parameters.get('ema_fast_period', 12))
    ema_slow_period = int(parameters.get('ema_slow_period', 26))
    macd_fast_period = int(parameters.get('macd_fast_period', 12))
    macd_slow_period = int(parameters.get('macd_slow_period', 26))
    macd_signal_period = int(parameters.get('macd_signal_period', 9))
    atr_period = int(parameters.get('atr_period', 10))
    atr_ma_period = int(parameters.get('atr_ma_period', 20))
    chop_period = int(parameters.get('chop_period', 14))

    # Calculate EMA
    df['ema_fast'] = df['close'].ewm(span=ema_fast_period, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=ema_slow_period, adjust=False).mean()

    # Calculate MACD
    ema_fast_macd = df['close'].ewm(span=macd_fast_period, adjust=False).mean()
    ema_slow_macd = df['close'].ewm(span=macd_slow_period, adjust=False).mean()
    df['macd'] = ema_fast_macd - ema_slow_macd
    df['macd_signal'] = df['macd'].ewm(span=macd_signal_period, adjust=False).mean()
    df['macd_hist'] = df['macd'] - df['macd_signal']

    # Calculate ATR
    tr = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    df['atr'] = tr.rolling(window=atr_period).mean()
    df['atr_ma'] = df['atr'].rolling(window=atr_ma_period).mean()

    # Calculate Choppiness
    df['choppiness'] = calculate_choppiness(df, chop_period)

    return df


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd_triple_filter_trend",
    name="MACD三重过滤趋势策略",
    description="MACD金叉死叉趋势策略，结合均线方向+ATR波动率+Choppiness震荡三重过滤提高信号质量，信号反转平仓",
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendStrategy",
    config_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendConfig",
    parameters={
        "ema_fast_period": ParameterSpec(title="快线EMA周期", type="integer", default=12, minimum=2, maximum=50),
        "ema_slow_period": ParameterSpec(title="慢线EMA周期", type="integer", default=26, minimum=5, maximum=100),
        "macd_fast_period": ParameterSpec(title="MACD快线周期", type="integer", default=12, minimum=2, maximum=50),
        "macd_slow_period": ParameterSpec(title="MACD慢线周期", type="integer", default=26, minimum=5, maximum=100),
        "macd_signal_period": ParameterSpec(title="MACD信号周期", type="integer", default=9, minimum=2, maximum=50),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=10, minimum=5, maximum=30),
        "atr_ma_period": ParameterSpec(title="ATR均线周期", type="integer", default=20, minimum=5, maximum=50),
        "atr_multiplier_threshold": ParameterSpec(title="ATR波动率阈值倍数", type="number", default=0.8, minimum=0.1, maximum=3.0),
        "chop_period": ParameterSpec(title="Choppiness周期", type="integer", default=14, minimum=5, maximum=50),
        "chop_threshold": ParameterSpec(title="Choppiness阈值", type="number", default=0.4, minimum=0.1, maximum=0.8),
        "position_size_pct": ParameterSpec(title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "ema_fast": {"type": "line", "color": "#ffaa00"},
            "ema_slow": {"type": "line", "color": "#00aaff"},
        },
        "subplots": {
            "MACD": {
                "macd": {"type": "line", "color": "#ff5555"},
                "macd_signal": {"type": "line", "color": "#55ff55"},
            },
            "ATR": {
                "atr": {"type": "line", "color": "#ff55ff"},
                "atr_ma": {"type": "line", "color": "#aaaaaa"},
            },
            "Choppiness": {
                "choppiness": {"type": "line", "color": "#00aaff"},
            }
        }
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
