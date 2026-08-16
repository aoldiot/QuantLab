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
    fast_ma_period: int = 12
    slow_ma_period: int = 26
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_period: int = 10
    atr_min_threshold: float = 0.01
    chop_period: int = 14
    chop_threshold: float = 0.4
    position_size_pct: float = 0.1


class MacdTripleFilterTrendStrategy(Strategy):
    def __init__(self, config: MacdTripleFilterTrendConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.prev_macd_diff = None
        self.prev_macd_dea = None

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        self.subscribe_bars(self.bar_type)

    def calculate_current_indicators(self, bars):
        closes = np.array([b.close.as_double() for b in bars])
        highs = np.array([b.high.as_double() for b in bars])
        lows = np.array([b.low.as_double() for b in bars])

        alpha_fast = 2 / (self.config.fast_ma_period + 1)
        alpha_slow = 2 / (self.config.slow_ma_period + 1)
        fast_ema = closes[0]
        slow_ema = closes[0]
        for price in closes[1:]:
            fast_ema = alpha_fast * price + (1 - alpha_fast) * fast_ema
            slow_ema = alpha_slow * price + (1 - alpha_slow) * slow_ema

        alpha_macd_fast = 2 / (self.config.macd_fast + 1)
        alpha_macd_slow = 2 / (self.config.macd_slow + 1)
        macd_fast_ema = closes[0]
        macd_slow_ema = closes[0]
        for price in closes[1:]:
            macd_fast_ema = alpha_macd_fast * price + (1 - alpha_macd_fast) * macd_fast_ema
            macd_slow_ema = alpha_macd_slow * price + (1 - alpha_macd_slow) * macd_slow_ema
        macd_diff = macd_fast_ema - macd_slow_ema

        alpha_signal = 2 / (self.config.macd_signal + 1)
        macd_dea = macd_diff
        diff_list = [macd_diff]
        for d in diff_list:
            macd_dea = alpha_signal * d + (1 - alpha_signal) * macd_dea
        macd_hist = 2 * (macd_diff - macd_dea)

        tr_list = []
        for i in range(1, len(bars)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            tr_list.append(tr)
        atr = sum(tr_list[-self.config.atr_period:]) / len(tr_list[-self.config.atr_period:]) if len(tr_list) >= self.config.atr_period else 0

        if len(highs) >= self.config.chop_period:
            window_high = highs[-self.config.chop_period:].max()
            window_low = lows[-self.config.chop_period:].min()
            sum_tr = sum(tr_list[-self.config.chop_period:])
            if window_high > window_low and sum_tr > 0:
                choppiness = np.log10(sum_tr / (window_high - window_low)) / np.log10(self.config.chop_period)
            else:
                choppiness = 1.0
        else:
            choppiness = 1.0

        return {
            'fast_ema': fast_ema,
            'slow_ema': slow_ema,
            'macd_diff': macd_diff,
            'macd_dea': macd_dea,
            'atr': atr,
            'choppiness': choppiness
        }

    def on_bar(self, bar: Bar):
        bars = list(self.cache.bars(self.bar_type))
        warmup = max(
            self.config.slow_ma_period,
            self.config.macd_slow + self.config.macd_signal,
            self.config.atr_period,
            self.config.chop_period
        )
        if len(bars) < warmup:
            return

        indicators = self.calculate_current_indicators(bars)
        current_diff = indicators['macd_diff']
        current_dea = indicators['macd_dea']

        if self.prev_macd_diff is None or self.prev_macd_dea is None:
            self.prev_macd_diff = current_diff
            self.prev_macd_dea = current_dea
            return

        golden_cross = self.prev_macd_diff < self.prev_macd_dea and current_diff > current_dea
        death_cross = self.prev_macd_diff > self.prev_macd_dea and current_diff < current_dea

        self.prev_macd_diff = current_diff
        self.prev_macd_dea = current_dea

        positions = list(self.cache.positions_open(instrument_id=self.instrument_id, strategy_id=self.id))
        current_pos = positions[0].side if positions else None

        fast_ema = indicators['fast_ema']
        slow_ema = indicators['slow_ema']
        atr = indicators['atr']
        choppiness = indicators['choppiness']

        if atr < self.config.atr_min_threshold or choppiness >= self.config.chop_threshold:
            return

        if golden_cross:
            if fast_ema > slow_ema:
                if current_pos == PositionSide.SHORT:
                    self.close_position(positions[0])
                if current_pos != PositionSide.LONG:
                    self.open_position(PositionSide.LONG)
        elif death_cross:
            if fast_ema < slow_ema:
                if current_pos == PositionSide.LONG:
                    self.close_position(positions[0])
                if current_pos != PositionSide.SHORT:
                    self.open_position(PositionSide.SHORT)

    def on_stop(self):
        self.unsubscribe_bars(self.bar_type)

    def open_position(self, side: PositionSide):
        account = self.portfolio.account(self.instrument_id.venue)
        free_balance = account.balance_free(self.instrument.quote_currency).as_double()
        last_bar = list(self.cache.bars(self.bar_type))[-1]
        position_size = (free_balance * self.config.position_size_pct) / last_bar.close.as_double()
        position_size = position_size * self.instrument.size_multiplier
        qty = Quantity.from_int(int(position_size)) if position_size.is_integer() else Quantity(str(round(position_size, 4)))
        order_side = OrderSide.BUY if side == PositionSide.LONG else OrderSide.SELL
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=order_side,
            quantity=qty,
        )
        self.submit_order(order)

    def close_position(self, position):
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

    fast_ma_period = int(parameters.get('fast_ma_period', 12))
    slow_ma_period = int(parameters.get('slow_ma_period', 26))
    macd_fast = int(parameters.get('macd_fast', 12))
    macd_slow = int(parameters.get('macd_slow', 26))
    macd_signal = int(parameters.get('macd_signal', 9))
    atr_period = int(parameters.get('atr_period', 10))
    chop_period = int(parameters.get('chop_period', 14))

    df['fast_ema'] = df['close'].ewm(span=fast_ma_period, adjust=False).mean()
    df['slow_ema'] = df['close'].ewm(span=slow_ma_period, adjust=False).mean()

    ema_fast = df['close'].ewm(span=macd_fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=macd_slow, adjust=False).mean()
    df['macd_diff'] = ema_fast - ema_slow
    df['macd_dea'] = df['macd_diff'].ewm(span=macd_signal, adjust=False).mean()
    df['macd_histogram'] = 2 * (df['macd_diff'] - df['macd_dea'])

    tr = pd.DataFrame()
    tr['h-l'] = df['high'] - df['low']
    tr['h-pc'] = abs(df['high'] - df['close'].shift(1))
    tr['l-pc'] = abs(df['low'] - df['close'].shift(1))
    df['tr'] = tr.max(axis=1)
    df['atr'] = df['tr'].rolling(window=atr_period, min_periods=atr_period).mean()

    df['chop_high'] = df['high'].rolling(window=chop_period).max()
    df['chop_low'] = df['low'].rolling(window=chop_period).min()
    df['sum_tr'] = df['tr'].rolling(window=chop_period).sum()

    df['choppiness'] = np.log10(df['sum_tr'] / (df['chop_high'] - df['chop_low'])) / np.log10(chop_period)
    df.loc[df['chop_high'] == df['chop_low'], 'choppiness'] = 1.0

    return df


STRATEGY_MANIFEST = StrategyManifest(
    slug="macd_triple_filter_trend",
    name="MACD三重过滤趋势跟随",
    description="MACD金叉死叉趋势策略，配合均线方向、ATR波动率、Choppiness震荡三重过滤",
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendStrategy",
    config_path="app.strategies.macd_triple_filter_trend:MacdTripleFilterTrendConfig",
    parameters={
        "fast_ma_period": ParameterSpec(title="快速均线周期", type="integer", default=12, minimum=5, maximum=50),
        "slow_ma_period": ParameterSpec(title="慢速均线周期", type="integer", default=26, minimum=10, maximum=100),
        "macd_fast": ParameterSpec(title="MACD快线周期", type="integer", default=12, minimum=5, maximum=50),
        "macd_slow": ParameterSpec(title="MACD慢线周期", type="integer", default=26, minimum=10, maximum=100),
        "macd_signal": ParameterSpec(title="MACD信号线周期", type="integer", default=9, minimum=3, maximum=30),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=10, minimum=5, maximum=30),
        "atr_min_threshold": ParameterSpec(title="ATR最小阈值", type="number", default=0.01, minimum=0.001, maximum=1000),
        "chop_period": ParameterSpec(title="Choppiness周期", type="integer", default=14, minimum=5, maximum=50),
        "chop_threshold": ParameterSpec(title="Choppiness阈值", type="number", default=0.4, minimum=0.1, maximum=1.0),
        "position_size_pct": ParameterSpec(title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0),
    },
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "fast_ema": {"type": "line", "color": "#ffaa00"},
            "slow_ema": {"type": "line", "color": "#00aaff"},
        },
        "subplots": {
            "MACD": {
                "macd_diff": {"type": "line", "color": "#ff5555"},
                "macd_dea": {"type": "line", "color": "#55ff55"},
                "macd_histogram": {"type": "bar", "color": "#5555ff"}
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