"""挤压突破 v2：波动率状态择时 + 外生方向（4h 主周期）。

已确认的交易规则（v1 在 1h 上 PF≈0.998 被证伪后的机制性修补）:

1. 标的与周期: 单标的 PERP（默认 BTCUSDT），信号周期 4h。
2. 状态变量（只提供时机，不提供方向）:
   - BB(20, 2.0) 归一化带宽 bb_width = (BB_up - BB_dn) / BB_mid
   - KC(20, 1.5*ATR14): TTM 挤压布尔态 SQZ_on = (BB_up < KC_up) and (BB_dn > KC_dn)
   - 带宽分位 bb_width_pct = percentile_rank(bb_width, width_lookback=120) < squeeze_q(0.20)
   - 压缩成熟度 squeeze_bars = 连续满足 (SQZ_on or 低分位) 的根数，要求 >= min_squeeze_bars(6)
3. 外生方向源（v1 缺失的核心修补）: EMA50 > EMA200 只做多，EMA50 < EMA200 只做空。
4. 突破与确认:
   - 多头 close > 前一根 Donchian 上轨 highest(high, 20)[1]
   - 空头 close < 前一根 Donchian 下轨 lowest(low, 20)[1]
   - 量能确认 volume > vol_mult(1.5) * SMA(volume, 20)
   - 扩张确认 ExpOK: bb_width > bb_width[1]（只交易“释放”，不预测释放）
5. 执行: 信号 bar 收盘确认，下一根 bar 开盘以市价单执行（pending 信号机制，杜绝自引用）。
6. 出场优先级:
   a) 初始止损 = 入场价 ∓ atr_stop_mult(2.0) * ATR(入场时)
   b) 失败快速离场: 入场后 fail_exit_bars(3) 根内价格回到 BB 中轨之内 → 立即平仓
   c) 吊灯移动止损 = highest(high, trail_lookback=10) - trail_mult(2.5) * ATR（仅单向移动）
   d) 时间止损: 持仓 time_stop_bars(60) 根仍未触发任何条件 → 平仓
   e) 无固定止盈（收益来自右尾，截断止盈会摧毁非对称性）
7. 风险与仓位:
   - 单笔风险 risk_per_trade = 0.75% 权益，qty = equity * risk / (atr_stop_mult * ATR)
   - 名义上限 max_notional_mult = 1.5 × 权益（防止低 ATR 期仓位爆炸）
   - 回撤熔断: 滚动 dd_window_bars(180 根 ≈ 30 天) 回撤 > dd_scale_down(12%) → 仓位减半；
     > dd_halt(18%) → 停止开新仓（已有持仓继续按出场规则管理）
8. allow_short=False 时退化为纯多头版本，用于消融对比。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.identifiers import InstrumentId

from app.strategy_base import QuantLabStrategy
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode


# =============================================================================
# 配置
# =============================================================================
class SqueezeBreakoutV2Config(StrategyConfig, frozen=True):
    """挤压突破 v2 策略配置（字段与 STRATEGY_MANIFEST.parameters 严格一致）。"""

    instrument_id: InstrumentId
    bar_type: BarType
    bb_period: int = 20
    bb_std: float = 2.0
    kc_period: int = 20
    kc_atr_mult: float = 1.5
    atr_period: int = 14
    squeeze_q: float = 0.20
    width_lookback: int = 120
    min_squeeze_bars: int = 6
    breakout_lookback: int = 20
    vol_period: int = 20
    vol_mult: float = 1.5
    ema_fast: int = 50
    ema_slow: int = 200
    atr_stop_mult: float = 2.0
    trail_mult: float = 2.5
    trail_lookback: int = 10
    fail_exit_bars: int = 3
    time_stop_bars: int = 60
    risk_per_trade: float = 0.0075
    max_notional_mult: float = 1.5
    dd_window_bars: int = 180
    dd_scale_down: float = 0.12
    dd_halt: float = 0.18
    allow_short: bool = True


# =============================================================================
# 向量化指标（覆盖 plot_config 声明的全部列）
# =============================================================================
def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    period = max(2, int(period))
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


def _rolling_percentile_rank(series: pd.Series, lookback: int) -> pd.Series:
    lookback = max(5, int(lookback))
    try:
        ranked = series.rolling(lookback, min_periods=5).rank(pct=True)
    except (AttributeError, TypeError, ValueError):
        ranked = series.rolling(lookback, min_periods=5).apply(
            lambda arr: float((arr[:-1] < arr[-1]).mean()) if len(arr) > 1 else 0.5,
            raw=True,
        )
    return ranked


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算 plot_config 声明的全部指标列（不改变行数，不 dropna）。"""
    result = dataframe.copy()

    def _num(col: str) -> pd.Series:
        return pd.to_numeric(result[col], errors="coerce").ffill().bfill()

    close = _num("close")
    high = _num("high")
    low = _num("low")
    volume = _num("volume") if "volume" in result.columns else pd.Series(0.0, index=result.index)

    bb_period = max(2, int(parameters.get("bb_period") or 20))
    bb_std = float(parameters.get("bb_std") or 2.0)
    kc_period = max(2, int(parameters.get("kc_period") or 20))
    kc_atr_mult = float(parameters.get("kc_atr_mult") or 1.5)
    atr_period = max(2, int(parameters.get("atr_period") or 14))
    width_lookback = max(5, int(parameters.get("width_lookback") or 120))
    breakout_lookback = max(2, int(parameters.get("breakout_lookback") or 20))
    vol_period = max(2, int(parameters.get("vol_period") or 20))
    squeeze_q = float(parameters.get("squeeze_q") or 0.20)
    ema_fast = max(2, int(parameters.get("ema_fast") or 50))
    ema_slow = max(3, int(parameters.get("ema_slow") or 200))

    # --- 布林带 ---
    bb_mid = close.rolling(bb_period, min_periods=2).mean()
    bb_sd = close.rolling(bb_period, min_periods=2).std(ddof=0).fillna(0.0)
    bb_upper = bb_mid + bb_std * bb_sd
    bb_lower = bb_mid - bb_std * bb_sd
    safe_mid = bb_mid.where(bb_mid.abs() > 1e-12, np.nan)
    bb_width = (bb_upper - bb_lower) / safe_mid

    # --- 肯特纳通道 ---
    kc_mid = close.ewm(span=kc_period, adjust=False).mean()
    atr = _wilder_atr(high, low, close, atr_period)
    kc_upper = kc_mid + kc_atr_mult * atr
    kc_lower = kc_mid - kc_atr_mult * atr

    # --- 挤压状态 ---
    width_pct = _rolling_percentile_rank(bb_width, width_lookback)
    ttm_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    pct_on = width_pct < squeeze_q
    squeeze_on = (ttm_on | pct_on).astype(float)

    # --- 方向源 ---
    ema_fast_line = close.ewm(span=ema_fast, adjust=False).mean()
    ema_slow_line = close.ewm(span=ema_slow, adjust=False).mean()

    # --- 突破轨道（前一根，避免自引用） ---
    don_high = high.rolling(breakout_lookback, min_periods=2).max().shift(1)
    don_low = low.rolling(breakout_lookback, min_periods=2).min().shift(1)

    # --- 量能比 ---
    vol_ma = volume.rolling(vol_period, min_periods=2).mean()
    vol_ratio = pd.Series(
        np.where(vol_ma.to_numpy() > 1e-12, volume.to_numpy() / vol_ma.where(vol_ma > 1e-12, 1.0).to_numpy(), 0.0),
        index=result.index,
        dtype=float,
    )

    def _clean(series: pd.Series) -> pd.Series:
        out = pd.to_numeric(series, errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan)
        return out.bfill().fillna(0.0)

    result["bb_mid"] = _clean(bb_mid)
    result["bb_upper"] = _clean(bb_upper)
    result["bb_lower"] = _clean(bb_lower)
    result["kc_upper"] = _clean(kc_upper)
    result["kc_lower"] = _clean(kc_lower)
    result["ema_fast_line"] = _clean(ema_fast_line)
    result["ema_slow_line"] = _clean(ema_slow_line)
    result["don_high"] = _clean(don_high)
    result["don_low"] = _clean(don_low)
    result["atr"] = _clean(atr)
    result["bb_width"] = _clean(bb_width)
    result["bb_width_pct"] = _clean(width_pct)
    result["squeeze_on"] = _clean(squeeze_on)
    result["vol_ratio"] = _clean(vol_ratio)
    return result


# =============================================================================
# 策略
# =============================================================================
class SqueezeBreakoutV2Strategy(QuantLabStrategy):
    """挤压成熟 + 带宽扩张 + EMA 趋势方向 + 量能确认的突破策略。"""

    def __init__(self, config: SqueezeBreakoutV2Config) -> None:
        super().__init__(config)
        self.bb_period = max(2, int(config.bb_period))
        self.bb_std = float(config.bb_std)
        self.kc_period = max(2, int(config.kc_period))
        self.kc_atr_mult = float(config.kc_atr_mult)
        self.atr_period = max(2, int(config.atr_period))
        self.squeeze_q = float(config.squeeze_q)
        self.width_lookback = max(5, int(config.width_lookback))
        self.min_squeeze_bars = max(1, int(config.min_squeeze_bars))
        self.breakout_lookback = max(2, int(config.breakout_lookback))
        self.vol_period = max(2, int(config.vol_period))
        self.vol_mult = float(config.vol_mult)
        self.ema_fast = max(2, int(config.ema_fast))
        self.ema_slow = max(3, int(config.ema_slow))
        self.atr_stop_mult = float(config.atr_stop_mult)
        self.trail_mult = float(config.trail_mult)
        self.trail_lookback = max(2, int(config.trail_lookback))
        self.fail_exit_bars = max(0, int(config.fail_exit_bars))
        self.time_stop_bars = max(1, int(config.time_stop_bars))
        self.risk_per_trade = float(config.risk_per_trade)
        self.max_notional_mult = float(config.max_notional_mult)
        self.dd_window_bars = max(10, int(config.dd_window_bars))
        self.dd_scale_down = float(config.dd_scale_down)
        self.dd_halt = float(config.dd_halt)
        self.allow_short = bool(config.allow_short)

        # 运行状态
        self.squeeze_bars: int = 0
        self.pending_signal: int = 0  # +1 开多, -1 开空, 0 无
        self.position_side: str = "FLAT"
        self.entry_price: float = 0.0
        self.entry_atr: float = 0.0
        self.bars_held: int = 0
        self.stop_price: float = 0.0
        self.trail_price: float = 0.0
        self.equity_history: list[float] = []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()
        self.log.info(
            "启动挤压突破 v2: "
            f"BB({self.bb_period},{self.bb_std}) KC({self.kc_period},{self.kc_atr_mult}) "
            f"ATR{self.atr_period} squeeze_q={self.squeeze_q} min_bars={self.min_squeeze_bars} "
            f"don={self.breakout_lookback} vol_mult={self.vol_mult} "
            f"EMA({self.ema_fast}/{self.ema_slow}) stop={self.atr_stop_mult}ATR "
            f"trail={self.trail_mult}ATR risk={self.risk_per_trade} short={self.allow_short}"
        )

    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)

        warmup = max(
            self.ema_slow + 2,
            self.width_lookback + self.bb_period + 2,
            self.breakout_lookback + 2,
            self.vol_period + 2,
            self.atr_period + 2,
        )
        frame = self.get_df()
        if len(frame) < warmup:
            return

        window = min(len(frame), max(600, self.width_lookback + self.bb_period + 100, self.ema_slow * 3))
        indicators = self._compute_indicators(frame.iloc[-window:])
        if indicators is None:
            return

        self._track_equity()
        self._update_squeeze_state(indicators)

        # 1) 执行上一根收盘确认的信号（次 bar 开盘执行）
        if self.pending_signal != 0:
            signal = self.pending_signal
            self.pending_signal = 0
            if self._is_flat_sync():
                self._enter(signal, bar, indicators)
                return

        # 2) 持仓管理（出场优先级：初始止损 → 失败快速离场 → 吊灯止损 → 时间止损）
        if not self._is_flat_sync():
            self.bars_held += 1
            if self._manage_position(bar, indicators):
                return
            return

        # 3) 空仓：评估入场信号（本根收盘确认，下一根执行）
        self.position_side = "FLAT"
        self.pending_signal = self._evaluate_entry_signal(indicators)

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------
    def _compute_indicators(self, frame: pd.DataFrame) -> dict[str, float] | None:
        close = pd.to_numeric(frame["close"], errors="coerce").ffill().bfill()
        high = pd.to_numeric(frame["high"], errors="coerce").ffill().bfill()
        low = pd.to_numeric(frame["low"], errors="coerce").ffill().bfill()
        volume = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)
        if len(close) < 5:
            return None

        bb_mid = close.rolling(self.bb_period, min_periods=2).mean()
        bb_sd = close.rolling(self.bb_period, min_periods=2).std(ddof=0).fillna(0.0)
        bb_upper = bb_mid + self.bb_std * bb_sd
        bb_lower = bb_mid - self.bb_std * bb_sd
        mid_last = float(bb_mid.iloc[-1])
        if not np.isfinite(mid_last) or abs(mid_last) < 1e-12:
            return None
        bb_width = (bb_upper - bb_lower) / bb_mid.where(bb_mid.abs() > 1e-12, np.nan)

        kc_mid = close.ewm(span=self.kc_period, adjust=False).mean()
        atr = _wilder_atr(high, low, close, self.atr_period)
        kc_upper = kc_mid + self.kc_atr_mult * atr
        kc_lower = kc_mid - self.kc_atr_mult * atr

        ema_fast_line = close.ewm(span=self.ema_fast, adjust=False).mean()
        ema_slow_line = close.ewm(span=self.ema_slow, adjust=False).mean()

        don_high = float(high.iloc[-(self.breakout_lookback + 1) : -1].max())
        don_low = float(low.iloc[-(self.breakout_lookback + 1) : -1].min())

        vol_ma = float(volume.iloc[-self.vol_period :].mean())
        vol_now = float(volume.iloc[-1])
        vol_ratio = vol_now / vol_ma if vol_ma > 1e-12 else 0.0

        width_now = float(bb_width.iloc[-1])
        width_prev = float(bb_width.iloc[-2])
        if not np.isfinite(width_now) or not np.isfinite(width_prev):
            return None

        width_window = bb_width.iloc[-self.width_lookback :].dropna()
        if len(width_window) >= 5:
            width_pct = float((width_window.to_numpy() < width_now).mean())
        else:
            width_pct = 0.5

        atr_now = float(atr.iloc[-1])
        if not np.isfinite(atr_now) or atr_now <= 0.0:
            return None

        trail_high = float(high.iloc[-self.trail_lookback :].max())
        trail_low = float(low.iloc[-self.trail_lookback :].min())

        return {
            "close": float(close.iloc[-1]),
            "bb_mid": mid_last,
            "bb_upper": float(bb_upper.iloc[-1]),
            "bb_lower": float(bb_lower.iloc[-1]),
            "kc_upper": float(kc_upper.iloc[-1]),
            "kc_lower": float(kc_lower.iloc[-1]),
            "bb_width": width_now,
            "bb_width_prev": width_prev,
            "bb_width_pct": width_pct,
            "atr": atr_now,
            "ema_fast": float(ema_fast_line.iloc[-1]),
            "ema_slow": float(ema_slow_line.iloc[-1]),
            "don_high": don_high,
            "don_low": don_low,
            "vol_ratio": vol_ratio,
            "trail_high": trail_high,
            "trail_low": trail_low,
        }

    def _update_squeeze_state(self, ind: dict[str, float]) -> None:
        ttm_on = ind["bb_upper"] < ind["kc_upper"] and ind["bb_lower"] > ind["kc_lower"]
        pct_on = ind["bb_width_pct"] < self.squeeze_q
        if ttm_on or pct_on:
            self.squeeze_bars += 1
        else:
            self.squeeze_bars = 0

    # ------------------------------------------------------------------
    # 入场
    # ------------------------------------------------------------------
    def _evaluate_entry_signal(self, ind: dict[str, float]) -> int:
        if self._risk_scale() <= 0.0:
            return 0
        if self.squeeze_bars < self.min_squeeze_bars:
            return 0
        if ind["bb_width"] <= ind["bb_width_prev"]:  # ExpOK：只交易挤压的“释放”
            return 0
        if ind["vol_ratio"] <= self.vol_mult:
            return 0

        trend_up = ind["ema_fast"] > ind["ema_slow"]
        trend_dn = ind["ema_fast"] < ind["ema_slow"]

        if ind["close"] > ind["don_high"] and trend_up:
            return 1
        if self.allow_short and ind["close"] < ind["don_low"] and trend_dn:
            return -1
        return 0

    def _enter(self, signal: int, bar: Bar, ind: dict[str, float]) -> None:
        price = float(bar.open.as_double())
        if price <= 0.0:
            price = float(bar.close.as_double())
        atr_now = ind["atr"]
        qty = self._position_size(price, atr_now)
        if qty <= 0.0:
            self.log.info("仓位为 0，跳过入场")
            return

        if signal > 0:
            self.buy_market(trade_size=qty)
            self.position_side = "LONG"
            self.stop_price = price - self.atr_stop_mult * atr_now
            self.trail_price = self.stop_price
        else:
            self.sell_market(trade_size=qty)
            self.position_side = "SHORT"
            self.stop_price = price + self.atr_stop_mult * atr_now
            self.trail_price = self.stop_price

        self.entry_price = price
        self.entry_atr = atr_now
        self.bars_held = 0
        self.squeeze_bars = 0
        self.log.info(
            f"{self.position_side} 开仓 @ {price:.2f} qty={qty:.6f} "
            f"atr={atr_now:.2f} stop={self.stop_price:.2f}"
        )

    def _position_size(self, price: float, atr_now: float) -> float:
        equity = float(self.get_equity())
        scale = self._risk_scale()
        if equity <= 0.0 or scale <= 0.0 or atr_now <= 0.0 or price <= 0.0:
            return 0.0
        stop_distance = self.atr_stop_mult * atr_now
        if stop_distance <= 0.0:
            return 0.0
        qty = (equity * self.risk_per_trade * scale) / stop_distance
        max_qty = (equity * self.max_notional_mult) / price
        qty = min(qty, max_qty)
        if not np.isfinite(qty) or qty <= 1e-9:
            return 0.0
        return float(qty)

    # ------------------------------------------------------------------
    # 出场
    # ------------------------------------------------------------------
    def _manage_position(self, bar: Bar, ind: dict[str, float]) -> bool:
        low = float(bar.low.as_double())
        high = float(bar.high.as_double())
        close = float(bar.close.as_double())
        atr_now = ind["atr"]

        if self.position_side == "LONG":
            if low <= self.stop_price:
                return self._exit(f"多头 ATR 初始/移动止损 @ {self.stop_price:.2f}")
            if self.bars_held <= self.fail_exit_bars and close < ind["bb_mid"]:
                return self._exit("多头失败快速离场（回落至 BB 中轨之内）")
            chandelier = ind["trail_high"] - self.trail_mult * atr_now
            if chandelier > self.trail_price:
                self.trail_price = chandelier
                self.stop_price = max(self.stop_price, chandelier)
            if self.bars_held >= self.time_stop_bars:
                return self._exit("多头时间止损")
            return False

        if self.position_side == "SHORT":
            if high >= self.stop_price:
                return self._exit(f"空头 ATR 初始/移动止损 @ {self.stop_price:.2f}")
            if self.bars_held <= self.fail_exit_bars and close > ind["bb_mid"]:
                return self._exit("空头失败快速离场（回升至 BB 中轨之内）")
            chandelier = ind["trail_low"] + self.trail_mult * atr_now
            if chandelier < self.trail_price:
                self.trail_price = chandelier
                self.stop_price = min(self.stop_price, chandelier)
            if self.bars_held >= self.time_stop_bars:
                return self._exit("空头时间止损")
            return False

        return False

    def _exit(self, reason: str) -> bool:
        self.close_position()
        self.log.info(f"平仓: {reason}")
        self.position_side = "FLAT"
        self.entry_price = 0.0
        self.entry_atr = 0.0
        self.bars_held = 0
        self.stop_price = 0.0
        self.trail_price = 0.0
        self.pending_signal = 0
        return True

    # ------------------------------------------------------------------
    # 风控与状态同步
    # ------------------------------------------------------------------
    def _track_equity(self) -> None:
        equity = float(self.get_equity())
        if np.isfinite(equity) and equity > 0.0:
            self.equity_history.append(equity)
            if len(self.equity_history) > self.dd_window_bars:
                self.equity_history = self.equity_history[-self.dd_window_bars :]

    def _rolling_drawdown(self) -> float:
        if len(self.equity_history) < 2:
            return 0.0
        peak = max(self.equity_history)
        if peak <= 0.0:
            return 0.0
        return max(0.0, (peak - self.equity_history[-1]) / peak)

    def _risk_scale(self) -> float:
        drawdown = self._rolling_drawdown()
        if drawdown > self.dd_halt:
            return 0.0
        if drawdown > self.dd_scale_down:
            return 0.5
        return 1.0

    def _is_flat_sync(self) -> bool:
        flat = self.is_flat()
        if flat and self.position_side != "FLAT":
            self.position_side = "FLAT"
            self.bars_held = 0
            self.stop_price = 0.0
            self.trail_price = 0.0
        if not flat and self.position_side == "FLAT":
            self.position_side = "LONG" if self.is_long() else "SHORT"
        return flat


# =============================================================================
# Manifest
# =============================================================================
STRATEGY_MANIFEST = StrategyManifest(
    slug="squeeze_breakout_v2",
    name="挤压突破 v2（波动率状态 + EMA 方向）",
    version="1.0.0",
    description=(
        "4h 挤压突破 v2：TTM 挤压 + 带宽分位识别压缩状态（只提供时机），"
        "EMA50/200 趋势提供外生方向，Donchian 突破配合量能与带宽扩张确认，"
        "ATR 初始止损 + 3 根失败快速离场 + 吊灯移动止损 + 时间止损，"
        "0.75% 单笔风险、1.5× 名义上限、12%/18% 滚动回撤熔断。"
    ),
    category="breakout",
    strategy_path="app.strategies.squeeze_breakout_v2:SqueezeBreakoutV2Strategy",
    config_path="app.strategies.squeeze_breakout_v2:SqueezeBreakoutV2Config",
    parameters={
        "bb_period": ParameterSpec(title="布林带周期", type="integer", default=20, minimum=10, maximum=60),
        "bb_std": ParameterSpec(title="布林带标准差倍数", type="number", default=2.0, minimum=1.0, maximum=3.5, step=0.1),
        "kc_period": ParameterSpec(title="肯特纳通道周期", type="integer", default=20, minimum=10, maximum=60),
        "kc_atr_mult": ParameterSpec(title="肯特纳 ATR 倍数", type="number", default=1.5, minimum=0.5, maximum=3.0, step=0.1),
        "atr_period": ParameterSpec(title="ATR 周期", type="integer", default=14, minimum=5, maximum=50),
        "squeeze_q": ParameterSpec(title="带宽压缩分位阈值", type="number", default=0.20, minimum=0.05, maximum=0.50, step=0.01),
        "width_lookback": ParameterSpec(title="带宽分位回看根数", type="integer", default=120, minimum=40, maximum=400),
        "min_squeeze_bars": ParameterSpec(title="最小压缩成熟根数", type="integer", default=6, minimum=1, maximum=30),
        "breakout_lookback": ParameterSpec(title="Donchian 突破回看根数", type="integer", default=20, minimum=5, maximum=80),
        "vol_period": ParameterSpec(title="成交量均值周期", type="integer", default=20, minimum=5, maximum=100),
        "vol_mult": ParameterSpec(title="量能确认倍数", type="number", default=1.5, minimum=1.0, maximum=4.0, step=0.1),
        "ema_fast": ParameterSpec(title="趋势快线 EMA", type="integer", default=50, minimum=10, maximum=120),
        "ema_slow": ParameterSpec(title="趋势慢线 EMA", type="integer", default=200, minimum=50, maximum=400),
        "atr_stop_mult": ParameterSpec(title="初始止损 ATR 倍数", type="number", default=2.0, minimum=1.0, maximum=5.0, step=0.1),
        "trail_mult": ParameterSpec(title="吊灯止损 ATR 倍数", type="number", default=2.5, minimum=1.0, maximum=6.0, step=0.1),
        "trail_lookback": ParameterSpec(title="吊灯止损回看根数", type="integer", default=10, minimum=3, maximum=60),
        "fail_exit_bars": ParameterSpec(title="失败快速离场窗口(根)", type="integer", default=3, minimum=0, maximum=10),
        "time_stop_bars": ParameterSpec(title="时间止损持仓根数", type="integer", default=60, minimum=10, maximum=300),
        "risk_per_trade": ParameterSpec(title="单笔风险占权益比例", type="number", default=0.0075, minimum=0.001, maximum=0.03, step=0.0005),
        "max_notional_mult": ParameterSpec(title="名义敞口上限(倍权益)", type="number", default=1.5, minimum=0.2, maximum=5.0, step=0.1),
        "dd_window_bars": ParameterSpec(title="滚动回撤窗口(根)", type="integer", default=180, minimum=30, maximum=1000),
        "dd_scale_down": ParameterSpec(title="回撤减半阈值", type="number", default=0.12, minimum=0.02, maximum=0.5, step=0.01),
        "dd_halt": ParameterSpec(title="回撤停开新仓阈值", type="number", default=0.18, minimum=0.05, maximum=0.8, step=0.01),
        "allow_short": ParameterSpec(title="允许做空", type="boolean", default=True),
    },
    timeframes=("1h", "4h", "1d"),
    primary_timeframe="4h",
    plot_config={
        "main_plot": {
            "close": {"name": "收盘价", "type": "line", "color": "#ffffff"},
            "bb_upper": {"name": "布林上轨", "type": "line", "color": "#60a5fa"},
            "bb_mid": {"name": "布林中轨", "type": "line", "color": "#93c5fd"},
            "bb_lower": {"name": "布林下轨", "type": "line", "color": "#60a5fa"},
            "kc_upper": {"name": "肯特纳上轨", "type": "line", "color": "#f59e0b"},
            "kc_lower": {"name": "肯特纳下轨", "type": "line", "color": "#f59e0b"},
            "ema_fast_line": {"name": "EMA50", "type": "line", "color": "#22c55e"},
            "ema_slow_line": {"name": "EMA200", "type": "line", "color": "#ef4444"},
            "don_high": {"name": "突破上轨", "type": "line", "color": "#a855f7"},
            "don_low": {"name": "突破下轨", "type": "line", "color": "#a855f7"},
        },
        "subplots": {
            "挤压状态": {
                "bb_width_pct": {"name": "带宽分位", "type": "line", "color": "#38bdf8"},
                "squeeze_on": {"name": "挤压开启", "type": "histogram", "color": "#facc15"},
            },
            "带宽": {
                "bb_width": {"name": "归一化带宽", "type": "line", "color": "#c084fc"},
            },
            "ATR": {
                "atr": {"name": "ATR14", "type": "line", "color": "#fb923c"},
            },
            "量能比": {
                "vol_ratio": {"name": "量能/均量", "type": "histogram", "color": "#4ade80"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
)