"""Squeeze Breakout Strategy (Bollinger Bands inside Keltner Channel).

交易逻辑概述
------------
1. 波动率压缩识别（Squeeze）：
   当布林带完全落入肯特纳通道内部（bb_lower > kc_lower 且 bb_upper < kc_upper），
   说明市场波动率被极度压缩，往往是趋势启动前的能量蓄积阶段。

2. 入场（突破）：
   连续至少 min_squeeze_bars 根 K 线处于挤压状态后，一旦收盘价向上突破布林带上轨
   开多；向下跌破布林带下轨开空。挤压释放后设有 10 根 K 线的有效突破窗口，避免使用
   过期的挤压信号。

3. 出场：
   - ATR 动态跟踪止损（多头只上移、空头只下移，绝不回撤）。
   - 反向突破信号立即平仓。
   - 持仓超过 max_hold_bars 根 K 线强制平仓（超时退出）。

4. 资金管理：
   每笔开仓使用 position_size_pct 比例的可用资金，按标的最小交易单位取整。

所有判断只使用"已收盘"的 Bar 数据，指标状态在下单之前完成更新，不存在未来函数。
"""

from __future__ import annotations

from collections import deque

import numpy as np
import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.trading.strategy import Strategy

from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode

# 挤压释放后允许突破入场的有效窗口（根 K 线），防止使用过期的挤压信号
SQUEEZE_RELEASE_WINDOW = 10


# =============================================================================
# 结构 1：StrategyConfig 子类
# =============================================================================
class SqueezeBreakoutConfig(StrategyConfig, frozen=True):
    """挤压突破策略配置。"""

    instrument_id: str
    bar_type: str
    bb_period: int = 20
    bb_std_dev: float = 2.0
    kc_period: int = 20
    kc_atr_period: int = 10
    kc_multiplier: float = 1.5
    min_squeeze_bars: int = 3
    stop_atr_multiplier: float = 2.0
    max_hold_bars: int = 100
    position_size_pct: float = 0.1


# =============================================================================
# 指标工具函数（与事件驱动实现保持完全一致的预热种子逻辑）
# =============================================================================
def _seeded_recursive(series: pd.Series, period: int, alpha: float) -> pd.Series:
    """以前 period 个样本的 SMA 作为种子，随后按 alpha 递归平滑（向量化实现）。

    与 Strategy 中的事件驱动递推完全等价，保证图表与回测引擎数值一致。
    """
    seeded = series.astype(float).copy()
    if period > 1:
        seeded.iloc[: period - 1] = np.nan
    if len(seeded) >= period:
        seeded.iloc[period - 1] = float(series.iloc[:period].mean())
    return seeded.ewm(alpha=alpha, adjust=False, ignore_na=True).mean()


def _wilder_rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder 平滑（RMA），种子为前 period 个值的均值。"""
    return _seeded_recursive(series, period, 1.0 / period)


def _seeded_ema(series: pd.Series, period: int) -> pd.Series:
    """标准 EMA，种子为前 period 个值的 SMA。"""
    return _seeded_recursive(series, period, 2.0 / (period + 1.0))


# =============================================================================
# 结构 3：向量化指标计算（供图表与研究分析使用）
# =============================================================================
def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算挤压突破策略的全部指标（严格保持输入行数不变）。"""
    data = df.copy()

    bb_period = int(parameters.get("bb_period", 20) or 20)
    bb_std_dev = float(parameters.get("bb_std_dev", 2.0) or 2.0)
    kc_period = int(parameters.get("kc_period", 20) or 20)
    kc_atr_period = int(parameters.get("kc_atr_period", 10) or 10)
    kc_multiplier = float(parameters.get("kc_multiplier", 1.5) or 1.5)
    min_squeeze_bars = int(parameters.get("min_squeeze_bars", 3) or 3)

    bb_period = max(2, bb_period)
    kc_period = max(2, kc_period)
    kc_atr_period = max(1, kc_atr_period)
    min_squeeze_bars = max(1, min_squeeze_bars)

    high = pd.to_numeric(data["high"], errors="coerce").astype(float)
    low = pd.to_numeric(data["low"], errors="coerce").astype(float)
    close = pd.to_numeric(data["close"], errors="coerce").astype(float)

    # ---------------- 布林带 ----------------
    bb_middle = close.rolling(window=bb_period, min_periods=bb_period).mean()
    bb_std = close.rolling(window=bb_period, min_periods=bb_period).std(ddof=0)
    bb_upper = bb_middle + bb_std_dev * bb_std
    bb_lower = bb_middle - bb_std_dev * bb_std

    # ---------------- ATR (Wilder RMA) ----------------
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = _wilder_rma(tr, kc_atr_period)

    # ---------------- 肯特纳通道 ----------------
    kc_middle = _seeded_ema(close, kc_period)
    kc_upper = kc_middle + kc_multiplier * atr
    kc_lower = kc_middle - kc_multiplier * atr

    # ---------------- 挤压状态 ----------------
    squeeze_on = ((bb_lower > kc_lower) & (bb_upper < kc_upper)).fillna(False)
    squeeze_flag = squeeze_on.astype(float)

    # 连续挤压根数（同一连续段内累加，非挤压归零）
    groups = (squeeze_on != squeeze_on.shift()).cumsum()
    squeeze_bars = squeeze_on.astype(int).groupby(groups).cumsum().astype(float)

    # 布林带宽度（%），分母做零保护避免 Inf
    safe_middle = bb_middle.replace(0.0, np.nan)
    bb_width = (bb_upper - bb_lower) / safe_middle * 100.0

    # ---------------- 突破信号（仅用于可视化，与事件驱动逻辑一致） ----------------
    prev_squeeze_bars = squeeze_bars.shift(1).fillna(0.0)
    armed = (
        prev_squeeze_bars.rolling(window=SQUEEZE_RELEASE_WINDOW, min_periods=1).max()
        >= min_squeeze_bars
    )
    long_signal = (armed & (close > bb_upper)).astype(float)
    short_signal = (armed & (close < bb_lower)).astype(float)

    data["bb_middle"] = bb_middle
    data["bb_upper"] = bb_upper
    data["bb_lower"] = bb_lower
    data["kc_upper"] = kc_upper
    data["kc_middle"] = kc_middle
    data["kc_lower"] = kc_lower
    data["atr"] = atr
    data["squeeze_on"] = squeeze_flag
    data["squeeze_bars"] = squeeze_bars
    data["bb_width"] = bb_width
    data["long_signal"] = long_signal
    data["short_signal"] = short_signal

    numeric_cols = [
        "bb_middle",
        "bb_upper",
        "bb_lower",
        "kc_upper",
        "kc_middle",
        "kc_lower",
        "atr",
        "squeeze_on",
        "squeeze_bars",
        "bb_width",
        "long_signal",
        "short_signal",
    ]
    for col in numeric_cols:
        data[col] = pd.to_numeric(data[col], errors="coerce").replace([np.inf, -np.inf], np.nan)

    return data


# =============================================================================
# 结构 2：Strategy 子类
# =============================================================================
class SqueezeBreakoutStrategy(Strategy):
    """布林带 / 肯特纳通道挤压突破 + ATR 跟踪止损策略。"""

    def __init__(self, config: SqueezeBreakoutConfig) -> None:
        super().__init__(config)

        self.instrument_id = InstrumentId.from_str(config.instrument_id)
        self.bar_type = BarType.from_str(config.bar_type)
        self.instrument = None

        # 参数
        self.bb_period = max(2, int(config.bb_period))
        self.bb_std_dev = float(config.bb_std_dev)
        self.kc_period = max(2, int(config.kc_period))
        self.kc_atr_period = max(1, int(config.kc_atr_period))
        self.kc_multiplier = float(config.kc_multiplier)
        self.min_squeeze_bars = max(1, int(config.min_squeeze_bars))
        self.stop_atr_multiplier = float(config.stop_atr_multiplier)
        self.max_hold_bars = max(1, int(config.max_hold_bars))
        self.position_size_pct = float(config.position_size_pct)

        # 滚动状态
        self.closes: deque[float] = deque(maxlen=self.bb_period)
        self.prev_close: float | None = None
        self.atr: float | None = None
        self.tr_seed: list[float] = []
        self.ema_kc: float | None = None
        self.ema_seed: list[float] = []
        self.ema_alpha = 2.0 / (self.kc_period + 1.0)

        # 挤压 / 持仓状态
        self.squeeze_streak = 0
        self.armed_bars_left = 0
        self.bars_held = 0
        self.trailing_stop: float | None = None
        self.entry_side: OrderSide | None = None
        self.bar_count = 0

    # ------------------------------------------------------------------ 生命周期
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self.log.error(f"未在缓存中找到交易对: {self.instrument_id}，策略停止")
            self.stop()
            return
        self.subscribe_bars(self.bar_type)
        self.log.info(
            f"SqueezeBreakout 启动 | BB({self.bb_period},{self.bb_std_dev}) "
            f"KC({self.kc_period},{self.kc_atr_period},{self.kc_multiplier}) "
            f"minSqueeze={self.min_squeeze_bars} stopATR={self.stop_atr_multiplier}"
        )

    def on_stop(self) -> None:
        self.unsubscribe_bars(self.bar_type)

    def on_reset(self) -> None:
        self.closes.clear()
        self.prev_close = None
        self.atr = None
        self.tr_seed = []
        self.ema_kc = None
        self.ema_seed = []
        self.squeeze_streak = 0
        self.armed_bars_left = 0
        self.bars_held = 0
        self.trailing_stop = None
        self.entry_side = None
        self.bar_count = 0

    # ------------------------------------------------------------------ 指标更新
    def _update_indicators(self, bar: Bar) -> dict | None:
        high = float(bar.high)
        low = float(bar.low)
        close = float(bar.close)

        # True Range
        if self.prev_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - self.prev_close),
                abs(low - self.prev_close),
            )

        # Wilder ATR
        if self.atr is None:
            self.tr_seed.append(true_range)
            if len(self.tr_seed) >= self.kc_atr_period:
                self.atr = sum(self.tr_seed) / len(self.tr_seed)
        else:
            self.atr = (self.atr * (self.kc_atr_period - 1) + true_range) / self.kc_atr_period

        # KC 中轨 EMA
        if self.ema_kc is None:
            self.ema_seed.append(close)
            if len(self.ema_seed) >= self.kc_period:
                self.ema_kc = sum(self.ema_seed) / len(self.ema_seed)
        else:
            self.ema_kc = self.ema_kc + self.ema_alpha * (close - self.ema_kc)

        self.closes.append(close)
        self.prev_close = close

        if self.atr is None or self.ema_kc is None or len(self.closes) < self.bb_period:
            return None

        values = np.fromiter(self.closes, dtype=float, count=len(self.closes))
        bb_middle = float(values.mean())
        bb_std = float(values.std(ddof=0))
        bb_upper = bb_middle + self.bb_std_dev * bb_std
        bb_lower = bb_middle - self.bb_std_dev * bb_std
        kc_upper = self.ema_kc + self.kc_multiplier * self.atr
        kc_lower = self.ema_kc - self.kc_multiplier * self.atr

        return {
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "bb_middle": bb_middle,
            "kc_upper": kc_upper,
            "kc_lower": kc_lower,
            "atr": float(self.atr),
        }

    # ------------------------------------------------------------------ 主事件
    def on_bar(self, bar: Bar) -> None:
        self.bar_count += 1

        state = self._update_indicators(bar)
        if state is None:
            return
        if self.instrument is None:
            self.instrument = self.cache.instrument(self.instrument_id)
            if self.instrument is None:
                return

        close = float(bar.close)
        atr = state["atr"]
        if atr <= 0.0:
            return

        # ---------------- 挤压状态判定（使用当前已收盘 Bar） ----------------
        squeeze_now = state["bb_lower"] > state["kc_lower"] and state["bb_upper"] < state["kc_upper"]
        prev_streak = self.squeeze_streak
        if squeeze_now:
            self.squeeze_streak += 1
        else:
            self.squeeze_streak = 0

        # 达到最小挤压根数即"武装"突破窗口
        if max(prev_streak, self.squeeze_streak) >= self.min_squeeze_bars:
            self.armed_bars_left = SQUEEZE_RELEASE_WINDOW
        elif self.armed_bars_left > 0:
            self.armed_bars_left -= 1

        long_signal = self.armed_bars_left > 0 and close > state["bb_upper"]
        short_signal = self.armed_bars_left > 0 and close < state["bb_lower"]

        net_position = self._net_position()

        # ---------------- 持仓管理（优先出场） ----------------
        if net_position != 0.0:
            self.bars_held += 1

            if net_position > 0.0:
                # 多头跟踪止损：只上移
                new_stop = close - atr * self.stop_atr_multiplier
                self.trailing_stop = (
                    new_stop if self.trailing_stop is None else max(self.trailing_stop, new_stop)
                )
                if self.trailing_stop is not None and float(bar.low) <= self.trailing_stop:
                    self._exit_position(f"多头 ATR 跟踪止损触发 @ {self.trailing_stop:.4f}")
                    return
                if short_signal:
                    self._exit_position("出现向下反向突破信号，多头平仓")
                    return
            else:
                # 空头跟踪止损：只下移
                new_stop = close + atr * self.stop_atr_multiplier
                self.trailing_stop = (
                    new_stop if self.trailing_stop is None else min(self.trailing_stop, new_stop)
                )
                if self.trailing_stop is not None and float(bar.high) >= self.trailing_stop:
                    self._exit_position(f"空头 ATR 跟踪止损触发 @ {self.trailing_stop:.4f}")
                    return
                if long_signal:
                    self._exit_position("出现向上反向突破信号，空头平仓")
                    return

            if self.bars_held >= self.max_hold_bars:
                self._exit_position(f"持仓已达 {self.bars_held} 根 K 线，超时强制平仓")
            return

        # 空仓：重置持仓期状态
        self.bars_held = 0
        self.trailing_stop = None
        self.entry_side = None

        # ---------------- 入场 ----------------
        if not long_signal and not short_signal:
            return
        if long_signal and short_signal:
            return

        side = OrderSide.BUY if long_signal else OrderSide.SELL
        quantity = self._calculate_quantity(close)
        if quantity is None:
            return

        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=quantity,
        )
        self.submit_order(order)

        self.entry_side = side
        self.bars_held = 0
        self.armed_bars_left = 0
        self.squeeze_streak = 0
        self.trailing_stop = (
            close - atr * self.stop_atr_multiplier
            if side == OrderSide.BUY
            else close + atr * self.stop_atr_multiplier
        )
        direction = "多" if side == OrderSide.BUY else "空"
        self.log.info(
            f"挤压突破开{direction} | close={close:.4f} "
            f"bb_upper={state['bb_upper']:.4f} bb_lower={state['bb_lower']:.4f} "
            f"atr={atr:.4f} qty={quantity} stop={self.trailing_stop:.4f}"
        )

    # ------------------------------------------------------------------ 辅助方法
    def _net_position(self) -> float:
        try:
            return float(self.portfolio.net_position(self.instrument_id))
        except Exception:
            return 0.0

    def _exit_position(self, reason: str) -> None:
        self.log.info(f"平仓: {reason}")
        try:
            self.close_all_positions(self.instrument_id)
        except Exception as exc:  # pragma: no cover - 防御性处理
            self.log.error(f"平仓失败: {exc}")
        self.trailing_stop = None
        self.entry_side = None
        self.bars_held = 0

    def _available_balance(self) -> float:
        """获取可用计价货币余额，失败时返回 0。"""
        try:
            account = self.portfolio.account(self.instrument_id.venue)
            if account is None:
                return 0.0
            currency = self.instrument.quote_currency
            balance = account.balance_free(currency)
            if balance is None:
                balance = account.balance_total(currency)
            if balance is None:
                return 0.0
            return float(balance.as_double())
        except Exception:
            return 0.0

    def _calculate_quantity(self, price: float):
        """按可用资金的固定比例计算下单数量，并按最小交易单位取整。"""
        if price <= 0.0 or self.instrument is None:
            return None

        balance = self._available_balance()
        if balance <= 0.0:
            self.log.warning("可用资金为 0，跳过本次开仓")
            return None

        notional = balance * self.position_size_pct
        raw_qty = notional / price
        if raw_qty <= 0.0:
            return None

        try:
            quantity = self.instrument.make_qty(raw_qty)
        except Exception as exc:
            self.log.warning(f"数量取整失败 ({raw_qty}): {exc}")
            return None

        if float(quantity) <= 0.0:
            self.log.warning(f"取整后下单数量为 0（raw={raw_qty}），跳过开仓")
            return None

        min_qty = getattr(self.instrument, "min_quantity", None)
        if min_qty is not None and float(quantity) < float(min_qty):
            self.log.warning(f"下单数量 {quantity} 低于最小下单量 {min_qty}，跳过开仓")
            return None

        return quantity


# =============================================================================
# 结构 4：STRATEGY_MANIFEST
# =============================================================================
STRATEGY_MANIFEST = StrategyManifest(
    slug="squeeze_breakout",
    name="布林肯特纳挤压突破",
    description=(
        "利用布林带完全落入肯特纳通道判定波动率压缩（Squeeze），"
        "在持续挤压后向上突破布林带上轨开多、向下跌破下轨开空，"
        "使用 ATR 动态跟踪止损，并支持反向信号平仓与持仓超时退出。"
    ),
    version="1.0.0",
    category="breakout",
    strategy_path="app.strategies.squeeze_breakout:SqueezeBreakoutStrategy",
    config_path="app.strategies.squeeze_breakout:SqueezeBreakoutConfig",
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
    timeframes=("15m", "1h", "4h", "1d"),
    primary_timeframe="1h",
    parameters={
        "bb_period": ParameterSpec(
            title="布林带周期",
            type="integer",
            default=20,
            minimum=5,
            maximum=200,
            step=1,
            description="布林带中轨 SMA 计算周期。",
        ),
        "bb_std_dev": ParameterSpec(
            title="布林带标准差倍数",
            type="number",
            default=2.0,
            minimum=0.5,
            maximum=5.0,
            step=0.1,
            description="布林带上下轨的标准差倍数。",
        ),
        "kc_period": ParameterSpec(
            title="肯特纳通道周期",
            type="integer",
            default=20,
            minimum=5,
            maximum=200,
            step=1,
            description="肯特纳通道中轨 EMA 计算周期。",
        ),
        "kc_atr_period": ParameterSpec(
            title="肯特纳 ATR 周期",
            type="integer",
            default=10,
            minimum=2,
            maximum=100,
            step=1,
            description="肯特纳通道与跟踪止损共用的 ATR (Wilder) 周期。",
        ),
        "kc_multiplier": ParameterSpec(
            title="肯特纳 ATR 倍数",
            type="number",
            default=1.5,
            minimum=0.5,
            maximum=5.0,
            step=0.1,
            description="肯特纳通道上下轨的 ATR 倍数。",
        ),
        "min_squeeze_bars": ParameterSpec(
            title="最小挤压根数",
            type="integer",
            default=3,
            minimum=1,
            maximum=50,
            step=1,
            description="入场前要求连续处于挤压状态的最少 K 线数量。",
        ),
        "stop_atr_multiplier": ParameterSpec(
            title="跟踪止损 ATR 倍数",
            type="number",
            default=2.0,
            minimum=0.5,
            maximum=10.0,
            step=0.1,
            description="ATR 动态跟踪止损距离倍数。",
        ),
        "max_hold_bars": ParameterSpec(
            title="最大持仓根数",
            type="integer",
            default=100,
            minimum=5,
            maximum=2000,
            step=1,
            description="单笔持仓允许的最大 K 线数量，超时强制平仓。",
        ),
        "position_size_pct": ParameterSpec(
            title="单仓资金比例",
            type="number",
            default=0.1,
            minimum=0.01,
            maximum=1.0,
            step=0.01,
            description="每笔开仓使用的可用资金比例。",
        ),
    },
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "bb_upper": {"type": "line", "color": "#ff5555"},
            "bb_middle": {"type": "line", "color": "#888888"},
            "bb_lower": {"type": "line", "color": "#55ff55"},
            "kc_upper": {"type": "line", "color": "#ffaa00"},
            "kc_lower": {"type": "line", "color": "#00aaff"},
        },
        "subplots": {
            "Squeeze": {
                "squeeze_on": {"type": "histogram", "color": "#ffcc00"},
                "squeeze_bars": {"type": "line", "color": "#ff55ff"},
            },
            "BB Width %": {
                "bb_width": {"type": "line", "color": "#00ddaa"},
            },
            "ATR": {
                "atr": {"type": "line", "color": "#ff8855"},
            },
        },
    },
)
