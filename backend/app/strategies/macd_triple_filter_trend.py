"""
MACD 三重过滤趋势跟随策略
==========================

策略逻辑:
    1. EMA 方向过滤：fast_ema > slow_ema 视为多头趋势，反之为空头趋势
    2. ATR 波动率过滤：当前 ATR 必须 > 近 N 周期 ATR 最小值，过滤低波动率区间
    3. Choppiness 震荡过滤：Choppiness 指数（归一化到 0-1）< 阈值，认为是趋势行情
    4. 入场:三重过滤同时通过后，MACD 金叉做多 / 死叉做空
    5. 出场:反向信号且三重过滤通过时,平仓并反手

无固定止损止盈，依靠反向信号 + 过滤条件触发平仓。
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar
from nautilus_trader.model.data import BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.enums import PositionSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec
from app.strategy_contract import StrategyManifest
from app.strategy_contract import StrategyMode


# ---------------------------------------------------------------------------
# 1. StrategyConfig
# ---------------------------------------------------------------------------
class MacdTripleFilterTrendConfig(StrategyConfig, frozen=True):
    """MACD 三重过滤趋势跟随策略配置。"""

    instrument_id: str = "BTCUSDT.BINANCE"
    bar_type: str = "BINANCE.BTCUSDT-1h-LAST-EXTERNAL"

    # EMA 周期
    ema_fast_period: int = 12
    ema_slow_period: int = 26

    # MACD 信号线周期
    macd_signal_period: int = 9

    # ATR 周期与回看窗口
    atr_period: int = 14
    atr_lookback_period: int = 10

    # Choppiness 指数周期与阈值
    chop_period: int = 14
    chop_threshold: float = 0.4

    # 单仓资金占比
    position_size_pct: float = 0.1


# ---------------------------------------------------------------------------
# 2. Strategy
# ---------------------------------------------------------------------------
class MacdTripleFilterTrendStrategy(Strategy):
    """MACD + EMA 方向 + ATR 波动率 + Choppiness 三重过滤的趋势策略。"""

    def __init__(self, config: MacdTripleFilterTrendConfig) -> None:
        super().__init__(config)

        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)

        self.ema_fast_period: int = int(config.ema_fast_period)
        self.ema_slow_period: int = int(config.ema_slow_period)
        self.macd_signal_period: int = int(config.macd_signal_period)
        self.atr_period: int = int(config.atr_period)
        self.atr_lookback_period: int = int(config.atr_lookback_period)
        self.chop_period: int = int(config.chop_period)
        self.chop_threshold: float = float(config.chop_threshold)
        self.position_size_pct: float = float(config.position_size_pct)

        self.instrument = None
        self._bars_seen: int = 0
        self._has_position_state: dict[str, bool] = {"want_open": False}

    # -----------------------------------------------------------------
    # 生命周期
    # -----------------------------------------------------------------
    def on_start(self) -> None:
        """策略启动:获取 instrument 并订阅 K 线。"""
        self.instrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self.log.error(f"无法获取 instrument: {self.instrument_id}")
            self.stop()
            return

        # 请求一定长度的历史 K 线,确保指标有足够的预热数据
        warmup = self._required_warmup_bars()
        self.request_bars(
            self.bar_type,
            start=self._clock.utc_now() - pd.Timedelta(
                minutes=int(warmup * self._bar_minutes() * 2)
            ),
        )
        self.subscribe_bars(self.bar_type)

    def on_stop(self) -> None:
        """策略停止:取消订阅。"""
        self.unsubscribe_bars(self.bar_type)

    def on_bar(self, bar: Bar) -> None:
        """每根 K 线驱动指标计算与开平仓逻辑。"""
        self._bars_seen += 1
        if self._bars_seen < self._required_warmup_bars():
            return

        bars = self.cache.bars(self.bar_type)
        if bars is None or len(bars) < self._required_warmup_bars():
            return

        df = self._bars_to_df(bars)
        if df.empty or len(df) < 2:
            return

        params = {
            "ema_fast_period": self.ema_fast_period,
            "ema_slow_period": self.ema_slow_period,
            "macd_signal_period": self.macd_signal_period,
            "atr_period": self.atr_period,
            "atr_lookback_period": self.atr_lookback_period,
            "chop_period": self.chop_period,
            "chop_threshold": self.chop_threshold,
        }
        df_ind = calculate_indicators(df, params)

        prev = df_ind.iloc[-2]
        cur = df_ind.iloc[-1]

        # 任意指标为 NaN 时跳过
        needed = (
            "fast_ema",
            "slow_ema",
            "diff",
            "dea",
            "atr",
            "atr_min",
            "choppiness",
        )
        for col in needed:
            v_prev = prev[col]
            v_cur = cur[col]
            if pd.isna(v_prev) or pd.isna(v_cur):
                return

        fast_ema = float(cur["fast_ema"])
        slow_ema = float(cur["slow_ema"])
        diff = float(cur["diff"])
        dea = float(cur["dea"])
        atr = float(cur["atr"])
        atr_min = float(cur["atr_min"])
        choppiness = float(cur["choppiness"])

        prev_diff = float(prev["diff"])
        prev_dea = float(prev["dea"])

        # 三重过滤
        ema_long = fast_ema > slow_ema
        ema_short = fast_ema < slow_ema
        atr_filter = atr > atr_min
        chop_filter = choppiness < self.chop_threshold

        long_trend = bool(ema_long and atr_filter and chop_filter)
        short_trend = bool(ema_short and atr_filter and chop_filter)

        # MACD 金叉 / 死叉
        golden_cross = (prev_diff <= prev_dea) and (diff > dea)
        death_cross = (prev_diff >= prev_dea) and (diff < dea)

        # 当前持仓方向
        position = self.portfolio.net_position(self.instrument_id)
        side = position.side if position is not None else PositionSide.FLAT
        is_long = side == PositionSide.LONG
        is_short = side == PositionSide.SHORT
        is_flat = side == PositionSide.FLAT

        # 出/入仓决策
        if golden_cross and long_trend:
            if is_short:
                self.close_position(position)
            if not is_long:
                self._open_long(bar)
        elif death_cross and short_trend:
            if is_long:
                self.close_position(position)
            if not is_short:
                self._open_short(bar)

    # -----------------------------------------------------------------
    # 内部辅助
    # -----------------------------------------------------------------
    def _required_warmup_bars(self) -> int:
        """计算指标预热所需的最小 Bar 数量。"""
        ema_warmup = self.ema_slow_period + self.macd_signal_period
        atr_warmup = self.atr_period + self.atr_lookback_period
        chop_warmup = self.chop_period
        return max(ema_warmup, atr_warmup, chop_warmup) + 2

    def _bar_minutes(self) -> int:
        """根据 bar_type 估算每根 K 线的分钟数,用于历史数据回溯。"""
        try:
            spec_str = str(self.bar_type)
        except Exception:
            return 60
        # 形如 "1m" "5m" "15m" "1h" "4h" "1d"
        for token in spec_str.replace("-", " ").split():
            token = token.lower()
            if token.endswith("m") and token[:-1].isdigit():
                return max(1, int(token[:-1]))
            if token.endswith("h") and token[:-1].isdigit():
                return 60 * int(token[:-1])
            if token.endswith("d") and token[:-1].isdigit():
                return 60 * 24 * int(token[:-1])
        return 60

    def _bars_to_df(self, bars) -> pd.DataFrame:
        """将 Bar 列表转换为标准 OHLCV DataFrame。"""
        rows = []
        for b in bars:
            rows.append(
                {
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
            )
        return pd.DataFrame(rows)

    def _calc_order_qty(self, price: float) -> Quantity | None:
        """根据账户权益百分比计算下单数量。"""
        if self.instrument is None or price <= 0:
            return None
        account = self.portfolio.account(self.instrument.quote_currency)
        if account is None:
            return None
        try:
            equity = float(account.equity())
        except Exception:
            return None
        if equity <= 0:
            return None
        notional = equity * self.position_size_pct
        size = notional / price
        try:
            return self.instrument.make_qty(Quantity.from_str(f"{size:.8f}"))
        except Exception:
            return None

    def _open_long(self, bar: Bar) -> None:
        """市价开多。"""
        price = float(bar.close)
        qty = self._calc_order_qty(price)
        if qty is None or qty.as_double() <= 0:
            return
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY,
            quantity=qty,
        )
        self.submit_order(order)

    def _open_short(self, bar: Bar) -> None:
        """市价开空。"""
        price = float(bar.close)
        qty = self._calc_order_qty(price)
        if qty is None or qty.as_double() <= 0:
            return
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.SELL,
            quantity=qty,
        )
        self.submit_order(order)


# ---------------------------------------------------------------------------
# 3. calculate_indicators —— 完全向量化指标计算
# ---------------------------------------------------------------------------
def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """
    计算 MACD 三重过滤策略所需的所有指标列。

    返回的 DataFrame 必须与输入 df 行数一致,并至少包含
    plot_config 中声明的全部指标列:
        main_plot: close, fast_ema, slow_ema
        subplots:
            ATR:      atr
            MACD:     diff, dea
            Choppiness: choppiness, chop_threshold
    """
    df = df.copy()

    ema_fast_period = int(parameters.get("ema_fast_period", 12))
    ema_slow_period = int(parameters.get("ema_slow_period", 26))
    macd_signal_period = int(parameters.get("macd_signal_period", 9))
    atr_period = int(parameters.get("atr_period", 14))
    atr_lookback_period = int(parameters.get("atr_lookback_period", 10))
    chop_period = int(parameters.get("chop_period", 14))
    chop_threshold = float(parameters.get("chop_threshold", 0.4))

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    # ----- EMA 快/慢线 -----
    df["fast_ema"] = close.ewm(span=ema_fast_period, adjust=False).mean()
    df["slow_ema"] = close.ewm(span=ema_slow_period, adjust=False).mean()

    # ----- MACD: DIFF / DEA / MACD 柱 -----
    df["diff"] = df["fast_ema"] - df["slow_ema"]
    df["dea"] = df["diff"].ewm(span=macd_signal_period, adjust=False).mean()
    df["macd"] = (df["diff"] - df["dea"]) * 2.0

    # ----- True Range -----
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # ----- ATR (Wilder 平滑) + ATR 最小值 -----
    df["atr"] = tr.ewm(alpha=1.0 / atr_period, adjust=False).mean()
    df["atr_min"] = df["atr"].rolling(window=atr_lookback_period, min_periods=1).min()

    # ----- Choppiness 指数 (归一化到 0-1) -----
    sum_tr = tr.rolling(window=chop_period, min_periods=chop_period).sum()
    hh = high.rolling(window=chop_period, min_periods=chop_period).max()
    ll = low.rolling(window=chop_period, min_periods=chop_period).min()
    range_hl = (hh - ll).replace(0, np.nan)

    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = sum_tr / range_hl
        ratio = ratio.where(ratio > 0)  # 避免 log10(<=0)
        log_ratio = np.log10(ratio)
        log_n = np.log10(float(chop_period))
        chop_raw = 100.0 * log_ratio / log_n
    # 标准 Choppiness 范围 0~100,归一化到 0~1 以匹配阈值 0.4
    df["choppiness"] = (chop_raw / 100.0).clip(lower=0.0, upper=1.0)

    # ----- 阈值基线(用于绘图面板的水平参考线) -----
    df["chop_threshold"] = float(chop_threshold)

    # 最终兜底:清理意外产生的 inf
    numeric_cols = [
        "fast_ema",
        "slow_ema",
        "diff",
        "dea",
        "macd",
        "atr",
        "atr_min",
        "choppiness",
        "chop_threshold",
    ]
    for col in numeric_cols:
        df[col] = df[col].replace([np.inf, -np.inf], np.nan)

    return df


# ---------------------------------------------------------------------------
# 4. STRATEGY_MANIFEST
# ---------------------------------------------------------------------------
STRATEGY_MANIFEST = StrategyManifest(
    slug="macd_triple_filter_trend",
    name="MACD 三重过滤趋势跟随",
    description=(
        "MACD 金叉/死叉信号叠加三重过滤(EMA 方向 + ATR 波动率 + Choppiness 趋势度),"
        "在强趋势行情中顺势开仓,震荡区间自动规避,无固定止损止盈,"
        "通过反向信号反手或平仓。"
    ),
    version="1.0.0",
    category="trend",
    parameters={
        "ema_fast_period": ParameterSpec(
            title="EMA 快线周期",
            type="integer",
            default=12,
            minimum=2,
            maximum=200,
            description="MACD 计算中快线 EMA 的周期",
        ),
        "ema_slow_period": ParameterSpec(
            title="EMA 慢线周期",
            type="integer",
            default=26,
            minimum=2,
            maximum=400,
            description="MACD 计算中慢线 EMA 的周期",
        ),
        "macd_signal_period": ParameterSpec(
            title="MACD 信号线周期",
            type="integer",
            default=9,
            minimum=2,
            maximum=100,
            description="MACD DEA 信号的平滑周期",
        ),
        "atr_period": ParameterSpec(
            title="ATR 周期",
            type="integer",
            default=14,
            minimum=2,
            maximum=200,
            description="ATR(真实波幅均值)计算周期",
        ),
        "atr_lookback_period": ParameterSpec(
            title="ATR 回看周期",
            type="integer",
            default=10,
            minimum=2,
            maximum=200,
            description="用于计算 ATR 最小值的回看窗口",
        ),
        "chop_period": ParameterSpec(
            title="Choppiness 周期",
            type="integer",
            default=14,
            minimum=2,
            maximum=200,
            description="Choppiness 指数的滚动窗口",
        ),
        "chop_threshold": ParameterSpec(
            title="Choppiness 阈值",
            type="number",
            default=0.4,
            minimum=0.0,
            maximum=1.0,
            description="Choppiness < 阈值 时允许入场(归一化到 0-1)",
        ),
        "position_size_pct": ParameterSpec(
            title="单仓资金占比",
            type="number",
            default=0.1,
            minimum=0.0,
            maximum=1.0,
            description="单次开仓占账户权益的比例",
        ),
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
            "ATR": {
                "atr": {"type": "line", "color": "#ff55ff"},
            },
            "MACD": {
                "diff": {"type": "line", "color": "#ff5555"},
                "dea": {"type": "line", "color": "#55ffff"},
            },
            "Choppiness": {
                "choppiness": {"type": "line", "color": "#00aaff"},
                "chop_threshold": {"type": "baseline", "color": "#888888"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=False,
)
