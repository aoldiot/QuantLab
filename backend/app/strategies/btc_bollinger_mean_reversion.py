import numpy as np
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


class BtcBollingerMeanReversionConfig(StrategyConfig):
    instrument_id: str
    bar_type: str
    bollinger_period: int = 20
    bollinger_std: float = 2.0
    chop_period: int = 14
    chop_threshold: float = 0.4
    position_size_pct: float = 0.1


class BtcBollingerMeanReversionStrategy(Strategy):
    """布林带均值回归策略：触及下轨做多、上轨做空，回到中轨平仓。

    仅在 Choppiness 指数高于阈值（震荡市）时开仓，趋势市中不入场。
    """

    def __init__(self, config: BtcBollingerMeanReversionConfig):
        super().__init__(config)
        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.instrument = None
        # 真实波幅需要前一根 Bar 的收盘价，因此 chop 至少需要 period + 1 根
        self.warmup = max(config.bollinger_period, config.chop_period + 1)

    def on_start(self):
        self.instrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self.log.error(f"找不到交易对 {self.instrument_id}，策略停止")
            self.stop()
            return
        self.subscribe_bars(self.bar_type)

    def on_stop(self):
        self.unsubscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar):
        # Cache.add_bar() 使用 appendleft，缓存内是「最新在前」，需反转成时间正序
        bars = list(self.cache.bars(self.bar_type))[::-1]
        if len(bars) < self.warmup:
            return

        closes = np.array([b.close.as_double() for b in bars])
        highs = np.array([b.high.as_double() for b in bars])
        lows = np.array([b.low.as_double() for b in bars])

        mid, upper, lower = self._bollinger(closes)
        choppiness = self._choppiness(highs, lows, closes)
        if choppiness is None or np.isnan(mid):
            return

        close = bar.close.as_double()
        # positions() 含已平仓记录，且必须按本策略与该交易对过滤
        positions = self.cache.positions_open(
            instrument_id=self.instrument_id,
            strategy_id=self.id,
        )
        position = positions[0] if positions else None

        if position is None:
            # 已有在途订单时不重复开仓
            if self.cache.orders_inflight(
                instrument_id=self.instrument_id,
                strategy_id=self.id,
            ):
                return
            if choppiness <= self.config.chop_threshold:
                return
            if close <= lower:
                self._open_position(OrderSide.BUY, close)
            elif close >= upper:
                self._open_position(OrderSide.SELL, close)
        else:
            # 价格回到中轨即均值回归完成，平仓
            if position.side == PositionSide.LONG and close >= mid:
                self.close_position(position)
            elif position.side == PositionSide.SHORT and close <= mid:
                self.close_position(position)

    def _bollinger(self, closes: np.ndarray) -> tuple[float, float, float]:
        window = closes[-self.config.bollinger_period:]
        mid = float(np.mean(window))
        # ddof=0 与 calculate_indicators 中的 std(ddof=0) 保持一致
        std = float(np.std(window))
        return mid, mid + self.config.bollinger_std * std, mid - self.config.bollinger_std * std

    def _choppiness(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> float | None:
        period = self.config.chop_period
        prev_closes = closes[-period - 1:-1]
        window_highs = highs[-period:]
        window_lows = lows[-period:]
        true_range = np.maximum(
            window_highs - window_lows,
            np.maximum(
                np.abs(window_highs - prev_closes),
                np.abs(window_lows - prev_closes),
            ),
        )
        price_range = float(np.max(window_highs) - np.min(window_lows))
        tr_sum = float(np.sum(true_range))
        # 窗口内价格完全无波动时公式无意义
        if price_range <= 0.0 or tr_sum <= 0.0 or period <= 1:
            return None
        return float(np.log10(tr_sum / price_range) / np.log10(period))

    def _open_position(self, order_side: OrderSide, price: float) -> None:
        account = self.portfolio.account(self.instrument_id.venue)
        if account is None or price <= 0.0:
            return
        balance = account.balance_free(self.instrument.quote_currency)
        if balance is None:
            return
        # 名义金额换算为数量：资金 * 占比 / 价格
        notional = balance.as_double() * self.config.position_size_pct
        try:
            quantity = self.instrument.make_qty(notional / price, round_down=True)
        except ValueError:
            self.log.warning(f"可用资金不足，无法满足最小下单量: {notional}")
            return
        if quantity <= 0:
            return
        self.submit_order(
            self.order_factory.market(
                instrument_id=self.instrument_id,
                order_side=order_side,
                quantity=quantity,
            )
        )


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    df = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    bollinger_period = int(parameters.get("bollinger_period", 20))
    bollinger_std = float(parameters.get("bollinger_std", 2.0))
    chop_period = int(parameters.get("chop_period", 14))

    # 布林带：ddof=0 与策略内 np.std 的总体标准差保持一致
    mid = df["close"].rolling(window=bollinger_period).mean()
    std = df["close"].rolling(window=bollinger_period).std(ddof=0)
    df["bollinger_mid"] = mid
    df["bollinger_upper"] = mid + bollinger_std * std
    df["bollinger_lower"] = mid - bollinger_std * std

    # Choppiness 指数：真实波幅之和 / 区间高低差，取对数归一化到 0~1
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr_sum = true_range.rolling(window=chop_period).sum()
    price_range = (
        df["high"].rolling(window=chop_period).max()
        - df["low"].rolling(window=chop_period).min()
    )
    ratio = (tr_sum / price_range.where(price_range > 0)).where(tr_sum > 0)
    denominator = np.log10(chop_period) if chop_period > 1 else np.nan
    df["choppiness"] = np.log10(ratio) / denominator

    return df


STRATEGY_MANIFEST = StrategyManifest(
    slug="btc_bollinger_mean_reversion",
    name="BTC 布林带均值回归策略",
    description="布林带均值回归策略，触及上轨做空、下轨做多，回归中轨平仓，并用 Choppiness 指数过滤趋势行情",
    version="1.0.0",
    category="mean_reversion",
    strategy_path="app.strategies.btc_bollinger_mean_reversion:BtcBollingerMeanReversionStrategy",
    config_path="app.strategies.btc_bollinger_mean_reversion:BtcBollingerMeanReversionConfig",
    parameters={
        "bollinger_period": ParameterSpec(
            title="布林带周期", type="integer", default=20, minimum=10, maximum=50,
            description="计算中轨与标准差的回看周期",
        ),
        "bollinger_std": ParameterSpec(
            title="布林带标准差倍数", type="number", default=2.0, minimum=1.0, maximum=3.0,
            description="上下轨距中轨的标准差倍数",
        ),
        "chop_period": ParameterSpec(
            title="Choppiness 周期", type="integer", default=14, minimum=5, maximum=30,
            description="Choppiness 指数的计算周期",
        ),
        "chop_threshold": ParameterSpec(
            title="Choppiness 阈值", type="number", default=0.4, minimum=0.2, maximum=0.6,
            description="高于该值视为震荡市，才允许开仓",
        ),
        "position_size_pct": ParameterSpec(
            title="单仓资金占比", type="number", default=0.1, minimum=0.01, maximum=1.0,
            description="每次开仓使用的可用资金比例",
        ),
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
                "choppiness": {"type": "line", "color": "#00aaff"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
