import pandas as pd
import numpy as np
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from app.strategy_contract import StrategyManifest, ParameterSpec, StrategyMode


class MacdTripleFilterTrendConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    fast_ema_period: int = 12
    slow_ema_period: int = 26
    signal_period: int = 9
    ma_trend_period: int = 200
    atr_period: int = 10
    atr_min_threshold: float = 0.005
    chop_period: int = 14
    chop_threshold: float = 0.4
    position_size_pct: float = 0.1


class MacdTripleFilterTrendStrategy(Strategy):
    def __init__(self, config: MacdTripleFilterTrendConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.config = config

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        bars = list(self.cache.bars(self.bar_type))
        warmup = max(self.config.ma_trend_period, self.config.slow_ema_period, 
                     self.config.atr_period, self.config.chop_period)
        if len(bars) < warmup:
            return

        # Extract price data
        closes = np.array([b.close.as_double() for b in bars])
        highs = np.array([b.high.as_double() for b in bars])
        lows = np.array([b.low.as_double() for b in bars])

        # Calculate indicators
        ma_trend = np.mean(closes[-self.config.ma_trend_period:])
        
        # EMA for MACD
        alpha_fast = 2 / (self.config.fast_ema_period + 1)
        alpha_slow = 2 / (self.config.slow_ema_period + 1)
        alpha_signal = 2 / (self.config.signal_period + 1)
        
        ema_fast = closes[-self.config.slow_ema_period:].copy()
        ema_slow = closes[-self.config.slow_ema_period:].copy()
        for i in range(1, len(ema_fast)):
            ema_fast[i] = alpha_fast * closes[-self.config.slow_ema_period + i] + (1 - alpha_fast) * ema_fast[i-1]
            ema_slow[i] = alpha_slow * closes[-self.config.slow_ema_period + i] + (1 - alpha_slow) * ema_slow[i-1]
        dif = ema_fast[-1] - ema_slow[-1]
        
        # Get historical DIF to calculate DEA
        dif_history = []
        for i in range(self.config.signal_period):
            if i >= len(ema_fast):
                break
            current_dif = ema_fast[-(self.config.signal_period - i)] - ema_slow[-(self.config.signal_period - i)]
            dif_history.append(current_dif)
        dea = dif_history[0]
        for d in dif_history[1:]:
            dea = alpha_signal * d + (1 - alpha_signal) * dea
        macd_hist = dif - dea

        # Calculate ATR
        tr = []
        for i in range(1, len(highs)):
            tr_i = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            tr.append(tr_i)
        atr = np.mean(tr[-self.config.atr_period:])

        # Calculate Choppiness Index
        def calculate_choppiness(highs, lows, closes, period):
            atr_values = []
            for i in range(1, len(highs)):
                tr_i = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i-1]),
                    abs(lows[i] - closes[i-1])
                )
                atr_values.append(tr_i)
            sum_atr = sum(atr_values[-period:])
            highest_high = max(highs[-period:])
            lowest_low = min(lows[-period:])
            if highest_high == lowest_low:
                return 0.5
            choppiness = 100 * np.log10(sum_atr / (highest_high - lowest_low)) / np.log10(period)
            return choppiness / 100  # Normalize to 0-1
        
        chop = calculate_choppiness(highs, lows, closes, self.config.chop_period)

        # Check current position
        positions = self.cache.positions()
        current_pos = positions[0].side if positions else None

        # Filter conditions
        price_current = closes[-1]
        trend_bullish = price_current > ma_trend
        atr_ok = atr >= self.config.atr_min_threshold
        trend_ok = chop <= self.config.chop_threshold

        # Check MACD crossover
        # Get previous DIF and DEA
        prev_dif = ema_fast[-2] - ema_slow[-2]
        prev_dea = dea  # Simplified approximation
        golden_cross = dif > dea and prev_dif <= prev_dea
        death_cross = dif < dea and prev_dif >= prev_dea

        # Execute strategy logic
        if current_pos is None:
            # No position, look for entry
            if trend_bullish and atr_ok and trend_ok and golden_cross:
                self.open_position(PositionSide.LONG)
            elif not trend_bullish and atr_ok and trend_ok and death_cross:
                self.open_position(PositionSide.SHORT)
        elif current_pos == PositionSide.LONG:
            # Hold long, check for exit/reversal
            if death_cross:
                self.close_position()
                if not trend_bullish and atr_ok and trend_ok:
                    self.open_position(PositionSide.SHORT)
        elif current_pos == PositionSide.SHORT:
            # Hold short, check for exit/reversal
            if golden_cross:
                self.close_position()
                if trend_bullish and atr_ok and trend_ok:
                    self.open_position(PositionSide.LONG)

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

    fast_p = int(parameters.get('fast_ema_period', 12))
    slow_p = int(parameters.get('slow_ema_period', 26))
    signal_p = int(parameters.get('signal_period', 9))
    ma_trend_p = int(parameters.get('ma_trend_period', 200))
    atr_p = int(parameters.get('atr_period', 10))
    chop_p = int(parameters.get('chop_period', 14))

    # Calculate indicators
    df['ma_trend'] = df['close'].rolling(window=ma_trend_p).mean()
    df['ema_fast'] = df['close'].ewm(span=fast_p, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=slow_p, adjust=False).mean()
    df['dif'] = df['ema_fast'] - df['ema_slow']
    df['dea'] = df['dif'].ewm(span=signal_p, adjust=False).mean()
    df['macd_histogram'] = df['dif'] - df['dea']

    # Calculate ATR
    tr = pd.DataFrame()
    tr['h-l'] = df['high'] - df['low']
    tr['h-pc'] = abs(df['high'] - df['close'].shift(1))
    tr['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = tr.max(axis=1)
    df['atr'] = df['tr'].rolling(window=atr_p).mean()

    # Calculate Choppiness Index
    def rolling_choppiness(window):
        if len(window.dropna()) < chop_p:
            return np.nan
        high = window['high'].values
        low = window['low'].values
        close = window['close'].values
        
        atr_sum = 0
        for i in range(1, len(high)):
            tr_i = max(
                high[i] - low[i],
                abs(high[i] - close[i-1]),
                abs(low[i] - close[i-1])
            )
            atr_sum += tr_i
        
        highest_high = np.max(high)
        lowest_low = np.min(low)
        if highest_high == lowest_low:
            return 0.5
        
        try:
            chop = 100 * np.log10(atr_sum / (highest_high - lowest_low)) / np.log10(chop_p)
            return chop / 100  # Normalize to 0-1
        except:
            return np.nan

    # Use rolling apply for choppiness
    df['choppiness'] = df.rolling(window=chop_p).apply(rolling_choppiness, raw=False)['close']

    return df


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd_triple_filter_trend",
    name="MACD三重过滤趋势跟随",
    description="MACD金叉死叉结合三重过滤（均线方向+ATR波动率+Choppiness震荡）的双向趋势跟随策略，信号反转平仓",
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendStrategy",
    config_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendConfig",
    parameters={
        "fast_ema_period": ParameterSpec(title="MACD快线周期", type="integer", default=12, minimum=5, maximum=50),
        "slow_ema_period": ParameterSpec(title="MACD慢线周期", type="integer", default=26, minimum=10, maximum=100),
        "signal_period": ParameterSpec(title="MACD信号线周期", type="integer", default=9, minimum=3, maximum=30),
        "ma_trend_period": ParameterSpec(title="趋势均线周期", type="integer", default=200, minimum=50, maximum=500),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=10, minimum=5, maximum=50),
        "atr_min_threshold": ParameterSpec(title="ATR最低阈值", type="number", default=0.005, minimum=0.001, maximum=0.05),
        "chop_period": ParameterSpec(title="Choppiness周期", type="integer", default=14, minimum=5, maximum=50),
        "chop_threshold": ParameterSpec(title="Choppiness阈值", type="number", default=0.4, minimum=0.2, maximum=0.8),
        "position_size_pct": ParameterSpec(title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "ma_trend": {"type": "line", "color": "#ffaa00"},
        },
        "subplots": {
            "MACD": {
                "dif": {"type": "line", "color": "#ffaa00"},
                "dea": {"type": "line", "color": "#00aaff"},
            },
            "ATR": {
                "atr": {"type": "line", "color": "#ff55ff"},
            },
            "Choppiness": {
                "choppiness": {"type": "line", "color": "#00ffaa"},
            }
        }
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
