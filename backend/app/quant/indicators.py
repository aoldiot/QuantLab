"""QuantLab High-Performance Quantitative Indicators and Strategy Building Blocks.

This module provides standard, pre-verified building blocks for NautilusTrader strategies
in QuantLab to dramatically reduce boilerplate code and prevent token truncation in LLM generation.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


class IncWilderADX:
    """Incremental Wilder Average Directional Index (ADX) calculator.

    Compatible with NautilusTrader event-driven bar processing.
    Computes Wilder RMA-smoothed +DI, -DI, and ADX per bar.
    """

    def __init__(self, period: int = 14) -> None:
        self.period = int(period)
        self.alpha = 1.0 / self.period
        self.prev_high: float | None = None
        self.prev_low: float | None = None
        self.prev_close: float | None = None

        self.smooth_tr: float = 0.0
        self.smooth_pdm: float = 0.0
        self.smooth_mdm: float = 0.0
        self.smooth_dx: float = 0.0

        self.plus_di: float = 0.0
        self.minus_di: float = 0.0
        self.adx: float = 0.0

        self.bar_count: int = 0
        self.is_ready: bool = False

    def update(self, high: float, low: float, close: float) -> tuple[float, float, float]:
        """Update with a new bar and return (adx, plus_di, minus_di)."""
        self.bar_count += 1
        if self.prev_close is None:
            self.prev_high = high
            self.prev_low = low
            self.prev_close = close
            return 0.0, 0.0, 0.0

        # True Range
        tr1 = high - low
        tr2 = abs(high - self.prev_close)
        tr3 = abs(low - self.prev_close)
        tr = max(tr1, tr2, tr3)

        # Directional Movement
        up_move = high - (self.prev_high or high)
        down_move = (self.prev_low or low) - low
        pdm = up_move if up_move > down_move and up_move > 0 else 0.0
        mdm = down_move if down_move > up_move and down_move > 0 else 0.0

        self.prev_high = high
        self.prev_low = low
        self.prev_close = close

        # Exponential moving smoothing (Wilder RMA)
        if self.bar_count <= self.period:
            self.smooth_tr += tr
            self.smooth_pdm += pdm
            self.smooth_mdm += mdm
            if self.bar_count == self.period:
                self.smooth_tr /= self.period
                self.smooth_pdm /= self.period
                self.smooth_mdm /= self.period
        else:
            self.smooth_tr = self.smooth_tr * (1.0 - self.alpha) + tr * self.alpha
            self.smooth_pdm = self.smooth_pdm * (1.0 - self.alpha) + pdm * self.alpha
            self.smooth_mdm = self.smooth_mdm * (1.0 - self.alpha) + mdm * self.alpha

        # Compute DI & DX
        if self.smooth_tr > 1e-12:
            self.plus_di = 100.0 * (self.smooth_pdm / self.smooth_tr)
            self.minus_di = 100.0 * (self.smooth_mdm / self.smooth_tr)
            di_sum = self.plus_di + self.minus_di
            dx = 100.0 * abs(self.plus_di - self.minus_di) / di_sum if di_sum > 1e-12 else 0.0
        else:
            self.plus_di = 0.0
            self.minus_di = 0.0
            dx = 0.0

        if self.bar_count <= self.period * 2:
            self.smooth_dx += dx
            if self.bar_count == self.period * 2:
                self.adx = self.smooth_dx / self.period
                self.is_ready = True
        else:
            self.adx = self.adx * (1.0 - self.alpha) + dx * self.alpha
            self.is_ready = True

        return self.adx, self.plus_di, self.minus_di

    def reset(self) -> None:
        """Reset state."""
        self.prev_high = None
        self.prev_low = None
        self.prev_close = None
        self.smooth_tr = 0.0
        self.smooth_pdm = 0.0
        self.smooth_mdm = 0.0
        self.smooth_dx = 0.0
        self.plus_di = 0.0
        self.minus_di = 0.0
        self.adx = 0.0
        self.bar_count = 0
        self.is_ready = False


class SqueezeStateTracker:
    """Bollinger Bands + Keltner Channel squeeze energy accumulation & breakout state machine."""

    def __init__(self, min_bars: int = 5, expiry_bars: int = 6) -> None:
        self.min_bars = int(min_bars)
        self.expiry_bars = int(expiry_bars)

        self.bars_inside: int = 0
        self.squeeze_completed: bool = False
        self.bars_since_squeeze: int = 0

        self.frozen_bb_upper: float | None = None
        self.frozen_bb_lower: float | None = None
        self.frozen_kc_upper: float | None = None
        self.frozen_kc_lower: float | None = None

    def update(
        self,
        bb_upper: float,
        bb_lower: float,
        kc_upper: float,
        kc_lower: float,
        close: float,
    ) -> bool:
        """Update squeeze state per bar. Returns True if squeeze is currently primed/ready."""
        is_in_kc = kc_lower <= close <= kc_upper
        is_bb_inside_kc = (bb_upper - bb_lower) < (kc_upper - kc_lower)

        if is_in_kc and is_bb_inside_kc:
            self.bars_inside += 1
            if self.bars_inside >= self.min_bars:
                self.squeeze_completed = True
                self.bars_since_squeeze = 0
                self.frozen_bb_upper = bb_upper
                self.frozen_bb_lower = bb_lower
                self.frozen_kc_upper = kc_upper
                self.frozen_kc_lower = kc_lower
        else:
            self.bars_inside = 0
            if self.squeeze_completed:
                self.bars_since_squeeze += 1
                if self.bars_since_squeeze > self.expiry_bars:
                    self.reset()

        return self.squeeze_completed

    def reset(self) -> None:
        """Reset squeeze state machine."""
        self.bars_inside = 0
        self.squeeze_completed = False
        self.bars_since_squeeze = 0
        self.frozen_bb_upper = None
        self.frozen_bb_lower = None
        self.frozen_kc_upper = None
        self.frozen_kc_lower = None


class ATRTrailingStopTracker:
    """Manages multi-tier trailing stop, breakeven arming, and hard ATR stop loss."""

    def __init__(self) -> None:
        self.side: str | None = None  # "LONG" or "SHORT"
        self.entry_price: float = 0.0
        self.avg_price: float = 0.0
        self.entry_atr: float = 0.0
        self.trailing_stop_price: float | None = None
        self.hard_stop_price: float | None = None
        self.fixed_stop_price: float | None = None

        self.highest_price: float = 0.0
        self.lowest_price: float = float("inf")
        self.breakeven_armed: bool = False

        self.hard_atr_mult: float = 2.5
        self.trail_atr_mult: float = 2.0
        self.arm_atr_mult: float = 0.0
        self.fixed_sl_pct: float = 0.15

    def on_entry(
        self,
        side: str,
        entry_price: float,
        entry_atr: float,
        hard_atr_mult: float = 2.5,
        trail_atr_mult: float = 2.0,
        arm_atr_mult: float = 0.0,
        fixed_sl_pct: float = 0.15,
    ) -> None:
        """Initialize tracker on position opening."""
        self.side = side.upper()
        self.entry_price = float(entry_price)
        self.avg_price = float(entry_price)
        self.entry_atr = max(float(entry_atr), self.entry_price * 0.001)

        self.hard_atr_mult = float(hard_atr_mult)
        self.trail_atr_mult = float(trail_atr_mult)
        self.arm_atr_mult = float(arm_atr_mult)
        self.fixed_sl_pct = float(fixed_sl_pct)

        self.highest_price = self.entry_price
        self.lowest_price = self.entry_price
        self.breakeven_armed = False

        if self.side == "LONG":
            self.trailing_stop_price = self.entry_price - self.trail_atr_mult * self.entry_atr
            self.hard_stop_price = self.avg_price - self.hard_atr_mult * self.entry_atr
            self.fixed_stop_price = self.avg_price * (1.0 - self.fixed_sl_pct)
        else:
            self.trailing_stop_price = self.entry_price + self.trail_atr_mult * self.entry_atr
            self.hard_stop_price = self.avg_price + self.hard_atr_mult * self.entry_atr
            self.fixed_stop_price = self.avg_price * (1.0 + self.fixed_sl_pct)

    def on_addon(self, add_price: float, new_avg_price: float, current_atr: float | None = None) -> None:
        """Update price anchor and stop levels on pyramid addon."""
        self.avg_price = float(new_avg_price)
        atr_ref = float(current_atr) if current_atr else self.entry_atr

        if self.side == "LONG":
            new_hard = self.avg_price - self.hard_atr_mult * atr_ref
            if self.hard_stop_price is not None:
                self.hard_stop_price = max(self.hard_stop_price, new_hard)
            else:
                self.hard_stop_price = new_hard
            self.fixed_stop_price = self.avg_price * (1.0 - self.fixed_sl_pct)
        else:
            new_hard = self.avg_price + self.hard_atr_mult * atr_ref
            if self.hard_stop_price is not None:
                self.hard_stop_price = min(self.hard_stop_price, new_hard)
            else:
                self.hard_stop_price = new_hard
            self.fixed_stop_price = self.avg_price * (1.0 + self.fixed_sl_pct)

    def check_exit(self, close: float, high: float | None = None, low: float | None = None) -> tuple[bool, str]:
        """Update trailing lines and evaluate exit conditions. Returns (should_exit, exit_reason)."""
        if self.side is None or self.entry_price <= 0:
            return False, ""

        h = high if high is not None else close
        l = low if low is not None else close

        if self.side == "LONG":
            self.highest_price = max(self.highest_price, h)

            # Breakeven arming check
            if self.arm_atr_mult > 0 and not self.breakeven_armed:
                if (close - self.entry_price) >= self.arm_atr_mult * self.entry_atr:
                    self.breakeven_armed = True
                    if self.trailing_stop_price is not None:
                        self.trailing_stop_price = max(self.trailing_stop_price, self.avg_price)

            # Ratchet trailing stop line (monotonic upward)
            new_trail = close - self.trail_atr_mult * self.entry_atr
            if self.trailing_stop_price is not None:
                self.trailing_stop_price = max(self.trailing_stop_price, new_trail)
            else:
                self.trailing_stop_price = new_trail

            # Evaluate triggers
            if self.trailing_stop_price is not None and close <= self.trailing_stop_price:
                reason = "BREAKEVEN_STOP" if self.breakeven_armed else "TRAILING_STOP"
                return True, reason

            if self.hard_stop_price is not None and close <= self.hard_stop_price:
                return True, "HARD_ATR_STOP"

            if self.fixed_stop_price is not None and close <= self.fixed_stop_price:
                return True, "FIXED_STOP"

        elif self.side == "SHORT":
            self.lowest_price = min(self.lowest_price, l)

            # Breakeven arming check
            if self.arm_atr_mult > 0 and not self.breakeven_armed:
                if (self.entry_price - close) >= self.arm_atr_mult * self.entry_atr:
                    self.breakeven_armed = True
                    if self.trailing_stop_price is not None:
                        self.trailing_stop_price = min(self.trailing_stop_price, self.avg_price)

            # Ratchet trailing stop line (monotonic downward)
            new_trail = close + self.trail_atr_mult * self.entry_atr
            if self.trailing_stop_price is not None:
                self.trailing_stop_price = min(self.trailing_stop_price, new_trail)
            else:
                self.trailing_stop_price = new_trail

            # Evaluate triggers
            if self.trailing_stop_price is not None and close >= self.trailing_stop_price:
                reason = "BREAKEVEN_STOP" if self.breakeven_armed else "TRAILING_STOP"
                return True, reason

            if self.hard_stop_price is not None and close >= self.hard_stop_price:
                return True, "HARD_ATR_STOP"

            if self.fixed_stop_price is not None and close >= self.fixed_stop_price:
                return True, "FIXED_STOP"

        return False, ""

    def reset(self) -> None:
        """Reset state."""
        self.side = None
        self.entry_price = 0.0
        self.avg_price = 0.0
        self.entry_atr = 0.0
        self.trailing_stop_price = None
        self.hard_stop_price = None
        self.fixed_stop_price = None
        self.highest_price = 0.0
        self.lowest_price = float("inf")
        self.breakeven_armed = False


def calc_standard_indicators(
    df: pd.DataFrame,
    parameters: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Calculate all standard vectorized indicators in one high-performance call.

    Guarantees no NaN values across columns via `.bfill().fillna(0.0)` for safe chart plotting.
    """
    p = parameters or {}
    result = df.copy()

    # Cast numeric columns safely
    close = pd.to_numeric(result["close"], errors="coerce").ffill().bfill()
    high = pd.to_numeric(result["high"], errors="coerce").ffill().bfill()
    low = pd.to_numeric(result["low"], errors="coerce").ffill().bfill()
    open_p = pd.to_numeric(result["open"], errors="coerce").ffill().bfill()
    volume = pd.to_numeric(result.get("volume", 0), errors="coerce").fillna(0.0)

    # MA Fast / Slow
    fast_p = int(p.get("fast_period") or p.get("ma_fast_period") or 20)
    slow_p = int(p.get("slow_period") or p.get("ma_slow_period") or 50)
    result["fast_ma"] = close.ewm(span=fast_p, adjust=False).mean().bfill().fillna(0.0)
    result["slow_ma"] = close.ewm(span=slow_p, adjust=False).mean().bfill().fillna(0.0)
    result["ma_fast"] = close.rolling(window=fast_p, min_periods=1).mean().bfill().fillna(0.0)
    result["ma_slow"] = close.rolling(window=slow_p, min_periods=1).mean().bfill().fillna(0.0)

    # ATR (14)
    atr_p = int(p.get("atr_period") or 14)
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    result["tr"] = tr.bfill().fillna(0.0)
    result["atr"] = tr.ewm(alpha=1.0 / max(2, atr_p), adjust=False).mean().bfill().fillna(0.0)

    # ADX (14)
    adx_p = int(p.get("adx_period") or 14)
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    smooth_tr = tr.ewm(alpha=1.0 / max(2, adx_p), adjust=False).mean()
    smooth_pdm = pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / max(2, adx_p), adjust=False).mean()
    smooth_mdm = pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / max(2, adx_p), adjust=False).mean()

    denom_tr = smooth_tr.replace(0, np.nan)
    plus_di = (100.0 * smooth_pdm / denom_tr).fillna(0.0)
    minus_di = (100.0 * smooth_mdm / denom_tr).fillna(0.0)
    sum_di = (plus_di + minus_di).replace(0, np.nan)
    dx = (100.0 * (plus_di - minus_di).abs() / sum_di).fillna(0.0)
    adx = dx.ewm(alpha=1.0 / max(2, adx_p), adjust=False).mean()

    result["plus_di"] = plus_di.bfill().fillna(0.0)
    result["minus_di"] = minus_di.bfill().fillna(0.0)
    result["adx"] = adx.bfill().fillna(0.0)

    # Bollinger Bands (20, 2.0)
    bb_p = int(p.get("bb_period") or 20)
    bb_mult = float(p.get("bb_multiplier") or 2.0)
    bb_mid = close.rolling(window=bb_p, min_periods=1).mean()
    bb_std = close.rolling(window=bb_p, min_periods=1).std().fillna(0.0)
    result["bb_mid"] = bb_mid.bfill().fillna(0.0)
    result["bb_upper"] = (bb_mid + bb_mult * bb_std).bfill().fillna(0.0)
    result["bb_lower"] = (bb_mid - bb_mult * bb_std).bfill().fillna(0.0)

    # Keltner Channels (20, 2.0)
    kc_p = int(p.get("kc_period") or 20)
    kc_mult = float(p.get("kc_multiplier") or 2.0)
    kc_mid = close.ewm(span=kc_p, adjust=False).mean()
    result["kc_mid"] = kc_mid.bfill().fillna(0.0)
    result["kc_upper"] = (kc_mid + kc_mult * result["atr"]).bfill().fillna(0.0)
    result["kc_lower"] = (kc_mid - kc_mult * result["atr"]).bfill().fillna(0.0)

    # Donchian Channels (20)
    donchian_p = int(p.get("donchian_period") or p.get("lookback_high_low_period") or 20)
    result["donchian_upper"] = high.rolling(window=donchian_p, min_periods=1).max().bfill().fillna(0.0)
    result["donchian_lower"] = low.rolling(window=donchian_p, min_periods=1).min().bfill().fillna(0.0)
    result["donchian_mid"] = ((result["donchian_upper"] + result["donchian_lower"]) / 2.0).bfill().fillna(0.0)

    # Volume MA (20)
    vol_p = int(p.get("vol_ma_period") or p.get("volume_period") or p.get("volume_avg_period") or 20)
    result["vol_ma"] = volume.rolling(window=vol_p, min_periods=1).mean().bfill().fillna(0.0)
    result["volume_avg"] = result["vol_ma"]

    # Common MA / Custom Aliases
    ma20_p = int(p.get("ma20_period") or 20)
    ma50_p = int(p.get("ma50_period") or 50)
    result["ma20"] = close.rolling(window=ma20_p, min_periods=1).mean().bfill().fillna(0.0)
    result["ma50"] = close.rolling(window=ma50_p, min_periods=1).mean().bfill().fillna(0.0)

    # 4h BB Mid (approximation on 1h data using 4x period if not separately resampled)
    bb4h_p = int(p.get("bb_4h_period") or 20)
    result["bb_4h_mid"] = close.rolling(window=bb4h_p * 4, min_periods=1).mean().bfill().fillna(0.0)

    # Runtime Execution Probe Placeholders (for safe plot_config resolution)
    result["stop_loss_price"] = 0.0
    result["trailing_stop_price"] = 0.0

    # RSI (14)
    rsi_p = int(p.get("rsi_period") or 14)
    delta = close.diff()
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain, index=df.index).ewm(alpha=1.0 / max(2, rsi_p), adjust=False).mean()
    avg_loss = pd.Series(loss, index=df.index).ewm(alpha=1.0 / max(2, rsi_p), adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result["rsi"] = (100.0 - (100.0 / (1.0 + rs))).bfill().fillna(50.0)

    return result
