"""MACD 趋势择时 + 均线状态闸门 + 多重过滤器（MACD-MA-Gated Trend）。

已确认的交易规则（BTCUSDT-PERP.BINANCE，1h 执行 / 4h 闸门，多空双向）:

1. 收益来源: MACD 柱在 1 根 / 5 根尺度上是反转（IC 为负），在 ≈20 根尺度上是趋势延续
   （rank IC +0.064, p≈2e-6）。因此只在均线确认的方向上，用 MACD 动能转折捕捉趋势中继段，
   并主动规避动能过热的追高点（Q5 分位收益转负）。
2. 均线闸门（Gate，只给方向、不给 alpha，因为 4h ema_spread 判定 WEAK_OR_NO_ALPHA）:
   Gate=+1: close > EMA100(1h) 且 close_4h > EMA50(4h) 且 4h 均线斜率 S > +0.001
   Gate=-1: close < EMA100(1h) 且 close_4h < EMA50(4h) 且 S < -0.001
   Gate= 0: 禁止开仓。S = (EMA50_4h[k] - EMA50_4h[k-6]) / EMA50_4h[k]。
   4h 序列由 1h Bar 按 4 根一组聚合，且只使用「已完成」的上一组，杜绝未来函数。
3. 做多入场（1h 收盘确认，下一根开盘市价成交）:
   a) Gate = +1
   b) 动能转折: H[t-1] <= 0 < H[t]，或 H[t] > 0 且 H[t] > H[t-1] > H[t-2]（连续放大的中继）
   c) DIF > DEA
   d) ADX(14) > 20（剔除震荡市）
   e) 0.25% <= ATR14/close <= 4%（波动率闸门）
   f) 归一化动能分位 q = PctRank_500(H / ATR14) <= 0.90（不追动能极值）
   g) 当前空仓，且同方向冷却期（6 根）已过
   做空为完全镜像（Gate=-1、H[t-1] >= 0 > H[t] 或连续两根走弱、DIF < DEA、q >= 0.10）。
4. 出场优先级（高 → 低）:
   1) 硬止损: 入场价 ∓ 1.8 * ATR（入场时冻结）
   2) 闸门反转: Gate 翻向反方向 → 市价平仓
   3) 动能反向: MACD 柱反向穿越 0 轴 → 平仓
   4) 移动止盈: 浮盈达到 1.0 * ATR 后启动追踪止损，回撤 1.2 * ATR（ATR 冻结于入场）
   5) 时间止损: 持仓 30 根仍未达 +0.5 * ATR 浮盈 → 平仓
   6) 最大持仓 72 根强制平仓
   不设固定止盈目标，让追踪止损承接右侧趋势。
5. 风险与仓位:
   - 固定风险法 qty = equity * 0.75% / (1.8 * ATR14)
   - 名义敞口上限 = equity * 3（超出按上限截断）
   - 单向单笔持仓，不加仓、不对冲
   - 日内熔断: 当日权益回撤 >= 3% → 当日停止开新仓
   - 连亏保护: 连续 4 笔亏损 → 仓位系数 0.5，直到出现一笔盈利恢复
6. allow_short=False 时退化为纯多头版本（用于消融对比）。
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
# 配置（字段与 STRATEGY_MANIFEST.parameters 严格一致）
# =============================================================================
class BtcMacdMaGatedTrendConfig(StrategyConfig, frozen=True):
    """MACD-MA-Gated Trend 策略配置。"""

    instrument_id: InstrumentId
    bar_type: BarType
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    ema_gate_period: int = 100
    ema_trend_4h: int = 50
    bars_per_4h: int = 4
    slope_lag: int = 6
    slope_th: float = 0.001
    adx_period: int = 14
    adx_th: float = 20.0
    atr_period: int = 14
    vol_lo: float = 0.0025
    vol_hi: float = 0.04
    q_window: int = 500
    q_cap: float = 0.90
    sl_atr: float = 1.8
    trail_atr: float = 1.2
    trail_trigger: float = 1.0
    time_stop_bars: int = 30
    time_stop_min_atr: float = 0.5
    max_hold_bars: int = 72
    cooldown_bars: int = 6
    risk_pct: float = 0.0075
    max_leverage: float = 3.0
    daily_loss_limit: float = 0.03
    loss_streak_limit: int = 4
    loss_streak_scale: float = 0.5
    allow_short: bool = True


# =============================================================================
# 向量化指标工具
# =============================================================================
def _clean_series(series: pd.Series) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan)
    return out.bfill().fillna(0.0)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    period = max(2, int(period))
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


def _wilder_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    period = max(2, int(period))
    up = high.diff()
    dn = -low.diff()
    up_arr = up.to_numpy(dtype=float)
    dn_arr = dn.to_numpy(dtype=float)
    plus_dm = np.where((up_arr > dn_arr) & (up_arr > 0.0), up_arr, 0.0)
    minus_dm = np.where((dn_arr > up_arr) & (dn_arr > 0.0), dn_arr, 0.0)
    plus_dm = pd.Series(np.nan_to_num(plus_dm, nan=0.0), index=high.index, dtype=float)
    minus_dm = pd.Series(np.nan_to_num(minus_dm, nan=0.0), index=high.index, dtype=float)

    atr = _wilder_atr(high, low, close, period)
    safe_atr = atr.where(atr.abs() > 1e-12, np.nan)
    pdi = 100.0 * plus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean() / safe_atr
    mdi = 100.0 * minus_dm.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean() / safe_atr
    denom = (pdi + mdi).abs()
    denom = denom.where(denom > 1e-12, np.nan)
    dx = 100.0 * (pdi - mdi).abs() / denom
    dx = dx.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


def _macd_lines(close: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast = max(2, int(fast))
    slow = max(fast + 1, int(slow))
    signal = max(2, int(signal))
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    hist = dif - dea
    return dif, dea, hist


def _rolling_pct_rank(series: pd.Series, window: int) -> pd.Series:
    window = max(10, int(window))
    min_periods = max(10, min(50, window))
    try:
        ranked = series.rolling(window, min_periods=min_periods).rank(pct=True)
    except (AttributeError, TypeError, ValueError):
        ranked = series.rolling(window, min_periods=min_periods).apply(
            lambda arr: float((arr[:-1] <= arr[-1]).mean()) if len(arr) > 1 else 0.5,
            raw=True,
        )
    return ranked


def _four_hour_gate_maps(
    close: pd.Series,
    ema_period: int,
    slope_lag: int,
    bars_per_group: int,
    offset: int = 0,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """把 1h 收盘价聚合为 4h 序列，并把「上一根已完成 4h Bar」的状态映射回 1h 索引。

    返回 (close_4h, ema_4h, slope_4h)，三者与输入 1h 序列等长、无未来信息。
    """
    bars_per_group = max(1, int(bars_per_group))
    ema_period = max(2, int(ema_period))
    slope_lag = max(1, int(slope_lag))

    n = len(close)
    if n == 0:
        empty = pd.Series(dtype=float)
        return empty, empty, empty

    abs_idx = np.arange(int(offset), int(offset) + n)
    gid = abs_idx // bars_per_group

    grouped_close = pd.Series(close.to_numpy(dtype=float)).groupby(gid).last()
    grouped_close.index = pd.Index(np.unique(gid))
    ema_4h = grouped_close.ewm(span=ema_period, adjust=False).mean()
    safe_ema = ema_4h.where(ema_4h.abs() > 1e-12, np.nan)
    slope_4h = (ema_4h - ema_4h.shift(slope_lag)) / safe_ema

    prev_gid = pd.Index(gid - 1)

    def _map(src: pd.Series) -> pd.Series:
        mapped = src.reindex(prev_gid)
        return pd.Series(mapped.to_numpy(dtype=float), index=close.index, dtype=float)

    return _map(grouped_close), _map(ema_4h), _map(slope_4h)


def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """向量化计算 plot_config 声明的全部指标列（保持行数不变，不 dropna）。"""
    result = dataframe.copy()

    def _num(col: str) -> pd.Series:
        if col in result.columns:
            return pd.to_numeric(result[col], errors="coerce").ffill().bfill()
        return pd.Series(0.0, index=result.index, dtype=float)

    close = _num("close")
    high = _num("high")
    low = _num("low")

    macd_fast = max(2, int(parameters.get("macd_fast") or 12))
    macd_slow = max(macd_fast + 1, int(parameters.get("macd_slow") or 26))
    macd_signal = max(2, int(parameters.get("macd_signal") or 9))
    ema_gate_period = max(2, int(parameters.get("ema_gate_period") or 100))
    ema_trend_4h = max(2, int(parameters.get("ema_trend_4h") or 50))
    bars_per_4h = max(1, int(parameters.get("bars_per_4h") or 4))
    slope_lag = max(1, int(parameters.get("slope_lag") or 6))
    slope_th = float(parameters.get("slope_th") or 0.001)
    adx_period = max(2, int(parameters.get("adx_period") or 14))
    atr_period = max(2, int(parameters.get("atr_period") or 14))
    q_window = max(10, int(parameters.get("q_window") or 500))

    # --- MACD 动能 ---
    dif, dea, hist = _macd_lines(close, macd_fast, macd_slow, macd_signal)

    # --- ATR / 波动率 ---
    atr = _wilder_atr(high, low, close, atr_period)
    safe_close = close.where(close.abs() > 1e-12, np.nan)
    atr_pct = atr / safe_close

    # --- 归一化动能与滚动分位 ---
    safe_atr = atr.where(atr.abs() > 1e-12, np.nan)
    hist_norm = hist / safe_atr
    hist_q = _rolling_pct_rank(hist_norm, q_window)

    # --- ADX 趋势强度 ---
    adx = _wilder_adx(high, low, close, adx_period)

    # --- 均线闸门 ---
    ema_gate = close.ewm(span=ema_gate_period, adjust=False).mean()
    close_4h, ema_4h, slope_4h = _four_hour_gate_maps(
        close, ema_trend_4h, slope_lag, bars_per_4h, offset=0
    )

    long_gate = (close > ema_gate) & (close_4h > ema_4h) & (slope_4h > slope_th)
    short_gate = (close < ema_gate) & (close_4h < ema_4h) & (slope_4h < -slope_th)
    gate = pd.Series(
        np.where(long_gate.fillna(False).to_numpy(), 1.0, np.where(short_gate.fillna(False).to_numpy(), -1.0, 0.0)),
        index=result.index,
        dtype=float,
    )

    result["macd_dif"] = _clean_series(dif)
    result["macd_dea"] = _clean_series(dea)
    result["macd_hist"] = _clean_series(hist)
    result["hist_norm"] = _clean_series(hist_norm)
    result["hist_q"] = _clean_series(hist_q)
    result["ema_gate"] = _clean_series(ema_gate)
    result["ema_trend_4h_line"] = _clean_series(ema_4h)
    result["slope_4h"] = _clean_series(slope_4h)
    result["gate"] = _clean_series(gate)
    result["adx"] = _clean_series(adx)
    result["atr"] = _clean_series(atr)
    result["atr_pct"] = _clean_series(atr_pct)
    return result


# =============================================================================
# 策略
# =============================================================================
class BtcMacdMaGatedTrendStrategy(QuantLabStrategy):
    """MACD 动能转折 + 1h/4h 均线闸门 + ADX / 波动率 / 动能极值三重过滤的趋势策略。"""

    def __init__(self, config: BtcMacdMaGatedTrendConfig) -> None:
        super().__init__(config)
        self.macd_fast = max(2, int(config.macd_fast))
        self.macd_slow = max(self.macd_fast + 1, int(config.macd_slow))
        self.macd_signal = max(2, int(config.macd_signal))
        self.ema_gate_period = max(2, int(config.ema_gate_period))
        self.ema_trend_4h = max(2, int(config.ema_trend_4h))
        self.bars_per_4h = max(1, int(config.bars_per_4h))
        self.slope_lag = max(1, int(config.slope_lag))
        self.slope_th = float(config.slope_th)
        self.adx_period = max(2, int(config.adx_period))
        self.adx_th = float(config.adx_th)
        self.atr_period = max(2, int(config.atr_period))
        self.vol_lo = float(config.vol_lo)
        self.vol_hi = float(config.vol_hi)
        self.q_window = max(10, int(config.q_window))
        self.q_cap = float(config.q_cap)
        self.sl_atr = float(config.sl_atr)
        self.trail_atr = float(config.trail_atr)
        self.trail_trigger = float(config.trail_trigger)
        self.time_stop_bars = max(1, int(config.time_stop_bars))
        self.time_stop_min_atr = float(config.time_stop_min_atr)
        self.max_hold_bars = max(self.time_stop_bars, int(config.max_hold_bars))
        self.cooldown_bars = max(0, int(config.cooldown_bars))
        self.risk_pct = float(config.risk_pct)
        self.max_leverage = float(config.max_leverage)
        self.daily_loss_limit = float(config.daily_loss_limit)
        self.loss_streak_limit = max(1, int(config.loss_streak_limit))
        self.loss_streak_scale = float(config.loss_streak_scale)
        self.allow_short = bool(config.allow_short)

        # 运行状态
        self.bar_index: int = 0
        self.pending_signal: int = 0
        self.position_side: str = "FLAT"
        self.entry_price: float = 0.0
        self.entry_atr: float = 0.0
        self.entry_equity: float = 0.0
        self.bars_held: int = 0
        self.stop_price: float = 0.0
        self.best_price: float = 0.0
        self.trail_active: bool = False
        self.last_long_exit_bar: int = -10_000
        self.last_short_exit_bar: int = -10_000
        self.loss_streak: int = 0
        self.current_day: int = -1
        self.day_start_equity: float = 0.0
        self.day_blocked: bool = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()
        self.log.info(
            "启动 MACD-MA-Gated Trend: "
            f"MACD({self.macd_fast},{self.macd_slow},{self.macd_signal}) "
            f"EMA闸门1h={self.ema_gate_period} EMA趋势4h={self.ema_trend_4h} "
            f"slope_th={self.slope_th} ADX({self.adx_period})>{self.adx_th} "
            f"vol[{self.vol_lo},{self.vol_hi}] q_cap={self.q_cap} "
            f"SL={self.sl_atr}ATR trail={self.trail_atr}ATR@{self.trail_trigger}ATR "
            f"time_stop={self.time_stop_bars} max_hold={self.max_hold_bars} "
            f"cooldown={self.cooldown_bars} risk={self.risk_pct} lev<={self.max_leverage} "
            f"short={self.allow_short}"
        )

    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)
        self.bar_index += 1

        self._update_daily_state(bar)

        warmup = max(
            self.macd_slow + self.macd_signal + 5,
            self.ema_gate_period + 5,
            self.ema_trend_4h * self.bars_per_4h + self.slope_lag * self.bars_per_4h + 5,
            self.adx_period * 3 + 5,
            self.atr_period * 3 + 5,
            60,
        )
        frame = self.get_df()
        if len(frame) < warmup:
            return

        window = min(len(frame), max(900, self.q_window + 200, self.ema_gate_period * 4))
        offset = len(frame) - window
        ind = self._compute_indicators(frame.iloc[offset:], offset)
        if ind is None:
            return

        # 1) 执行上一根收盘确认的信号（下一根开盘市价成交）
        if self.pending_signal != 0:
            signal = self.pending_signal
            self.pending_signal = 0
            if self._is_flat_sync() and not self.day_blocked:
                self._enter(signal, bar, ind)
                return

        # 2) 持仓管理
        if not self._is_flat_sync():
            self.bars_held += 1
            self._manage_position(bar, ind)
            return

        # 3) 空仓：评估入场信号（本根收盘确认，下一根执行）
        self.position_side = "FLAT"
        self.pending_signal = self._evaluate_entry_signal(ind)

    # ------------------------------------------------------------------
    # 指标计算
    # ------------------------------------------------------------------
    def _compute_indicators(self, frame: pd.DataFrame, offset: int) -> dict[str, float] | None:
        close = pd.to_numeric(frame["close"], errors="coerce").ffill().bfill()
        high = pd.to_numeric(frame["high"], errors="coerce").ffill().bfill()
        low = pd.to_numeric(frame["low"], errors="coerce").ffill().bfill()
        if len(close) < 10:
            return None

        dif, dea, hist = _macd_lines(close, self.macd_fast, self.macd_slow, self.macd_signal)
        atr = _wilder_atr(high, low, close, self.atr_period)
        adx = _wilder_adx(high, low, close, self.adx_period)
        ema_gate = close.ewm(span=self.ema_gate_period, adjust=False).mean()
        close_4h, ema_4h, slope_4h = _four_hour_gate_maps(
            close, self.ema_trend_4h, self.slope_lag, self.bars_per_4h, offset=offset
        )

        atr_now = float(atr.iloc[-1])
        close_now = float(close.iloc[-1])
        if not np.isfinite(atr_now) or atr_now <= 0.0 or not np.isfinite(close_now) or close_now <= 0.0:
            return None

        safe_atr = atr.where(atr.abs() > 1e-12, np.nan)
        hist_norm = hist / safe_atr
        q_series = _rolling_pct_rank(hist_norm, self.q_window)
        q_now = float(q_series.iloc[-1])
        if not np.isfinite(q_now):
            q_now = 0.5

        h0 = float(hist.iloc[-1])
        h1 = float(hist.iloc[-2])
        h2 = float(hist.iloc[-3]) if len(hist) >= 3 else h1
        if not (np.isfinite(h0) and np.isfinite(h1) and np.isfinite(h2)):
            return None

        c4 = float(close_4h.iloc[-1]) if np.isfinite(close_4h.iloc[-1]) else float("nan")
        e4 = float(ema_4h.iloc[-1]) if np.isfinite(ema_4h.iloc[-1]) else float("nan")
        s4 = float(slope_4h.iloc[-1]) if np.isfinite(slope_4h.iloc[-1]) else float("nan")

        gate = 0
        if np.isfinite(c4) and np.isfinite(e4) and np.isfinite(s4):
            if close_now > float(ema_gate.iloc[-1]) and c4 > e4 and s4 > self.slope_th:
                gate = 1
            elif close_now < float(ema_gate.iloc[-1]) and c4 < e4 and s4 < -self.slope_th:
                gate = -1

        adx_now = float(adx.iloc[-1])
        if not np.isfinite(adx_now):
            adx_now = 0.0

        return {
            "close": close_now,
            "atr": atr_now,
            "atr_pct": atr_now / close_now,
            "dif": float(dif.iloc[-1]),
            "dea": float(dea.iloc[-1]),
            "hist": h0,
            "hist_prev": h1,
            "hist_prev2": h2,
            "hist_q": q_now,
            "adx": adx_now,
            "ema_gate": float(ema_gate.iloc[-1]),
            "gate": float(gate),
        }

    # ------------------------------------------------------------------
    # 入场
    # ------------------------------------------------------------------
    def _evaluate_entry_signal(self, ind: dict[str, float]) -> int:
        if self.day_blocked:
            return 0

        gate = int(ind["gate"])
        if gate == 0:
            return 0
        if ind["adx"] <= self.adx_th:
            return 0
        if not (self.vol_lo <= ind["atr_pct"] <= self.vol_hi):
            return 0

        h0 = ind["hist"]
        h1 = ind["hist_prev"]
        h2 = ind["hist_prev2"]

        if gate > 0:
            if ind["dif"] <= ind["dea"]:
                return 0
            if ind["hist_q"] > self.q_cap:
                return 0
            turn_up = (h1 <= 0.0) and (h0 > 0.0)
            continuation = (h0 > 0.0) and (h0 > h1 > h2)
            if not (turn_up or continuation):
                return 0
            if self.bar_index - self.last_long_exit_bar < self.cooldown_bars:
                return 0
            return 1

        if not self.allow_short:
            return 0
        if ind["dif"] >= ind["dea"]:
            return 0
        if ind["hist_q"] < 1.0 - self.q_cap:
            return 0
        turn_dn = (h1 >= 0.0) and (h0 < 0.0)
        continuation_dn = (h0 < 0.0) and (h0 < h1 < h2)
        if not (turn_dn or continuation_dn):
            return 0
        if self.bar_index - self.last_short_exit_bar < self.cooldown_bars:
            return 0
        return -1

    def _enter(self, signal: int, bar: Bar, ind: dict[str, float]) -> None:
        price = float(bar.open.as_double())
        if not np.isfinite(price) or price <= 0.0:
            price = float(bar.close.as_double())
        atr_now = ind["atr"]
        raw_qty = self._position_size(price, atr_now)
        if raw_qty <= 0.0:
            self.log.info("仓位为 0，跳过入场")
            return

        qty = self.make_qty(raw_qty)
        if float(qty.as_double()) <= 0.0:
            self.log.info("下单数量精度取整后为 0，跳过入场")
            return

        if signal > 0:
            self.buy_market(trade_size=qty)
            self.position_side = "LONG"
            self.stop_price = price - self.sl_atr * atr_now
        else:
            self.sell_market(trade_size=qty)
            self.position_side = "SHORT"
            self.stop_price = price + self.sl_atr * atr_now

        self.entry_price = price
        self.entry_atr = atr_now
        self.entry_equity = float(self.get_equity())
        self.bars_held = 0
        self.best_price = price
        self.trail_active = False
        self.log.info(
            f"{self.position_side} 开仓 @ {price:.2f} qty={qty.as_double():.6f} "
            f"atr={atr_now:.2f} stop={self.stop_price:.2f} gate={int(ind['gate'])} "
            f"adx={ind['adx']:.1f} q={ind['hist_q']:.2f}"
        )

    def _position_size(self, price: float, atr_now: float) -> float:
        equity = float(self.get_equity())
        if equity <= 0.0 or price <= 0.0 or atr_now <= 0.0:
            return 0.0
        stop_distance = self.sl_atr * atr_now
        if stop_distance <= 0.0:
            return 0.0
        scale = self.loss_streak_scale if self.loss_streak >= self.loss_streak_limit else 1.0
        qty = (equity * self.risk_pct * scale) / stop_distance
        max_qty = (equity * self.max_leverage) / price
        qty = min(qty, max_qty)
        if not np.isfinite(qty) or qty <= 1e-9:
            return 0.0
        return float(qty)

    # ------------------------------------------------------------------
    # 出场
    # ------------------------------------------------------------------
    def _manage_position(self, bar: Bar, ind: dict[str, float]) -> None:
        high = float(bar.high.as_double())
        low = float(bar.low.as_double())
        close = float(bar.close.as_double())
        atr_ref = self.entry_atr if self.entry_atr > 0.0 else ind["atr"]
        gate = int(ind["gate"])

        if self.position_side == "LONG":
            # 1) 硬止损 / 追踪止损
            if low <= self.stop_price:
                self._exit(f"多头止损触发 @ {self.stop_price:.2f}")
                return
            # 2) 闸门反转
            if gate < 0:
                self._exit("多头闸门反转平仓")
                return
            # 3) 动能反向
            if ind["hist"] < 0.0:
                self._exit("多头 MACD 柱反向穿越 0 轴平仓")
                return
            # 4) 移动止盈
            self.best_price = max(self.best_price, high)
            if not self.trail_active and (self.best_price - self.entry_price) >= self.trail_trigger * atr_ref:
                self.trail_active = True
            if self.trail_active:
                trail_stop = self.best_price - self.trail_atr * atr_ref
                if trail_stop > self.stop_price:
                    self.stop_price = trail_stop
            # 5) 时间止损
            if self.bars_held >= self.time_stop_bars and (close - self.entry_price) < self.time_stop_min_atr * atr_ref:
                self._exit("多头时间止损（超期未达浮盈门槛）")
                return
            # 6) 最大持仓时长
            if self.bars_held >= self.max_hold_bars:
                self._exit("多头最大持仓时长强制平仓")
            return

        if self.position_side == "SHORT":
            if high >= self.stop_price:
                self._exit(f"空头止损触发 @ {self.stop_price:.2f}")
                return
            if gate > 0:
                self._exit("空头闸门反转平仓")
                return
            if ind["hist"] > 0.0:
                self._exit("空头 MACD 柱反向穿越 0 轴平仓")
                return
            self.best_price = min(self.best_price, low) if self.best_price > 0.0 else low
            if not self.trail_active and (self.entry_price - self.best_price) >= self.trail_trigger * atr_ref:
                self.trail_active = True
            if self.trail_active:
                trail_stop = self.best_price + self.trail_atr * atr_ref
                if trail_stop < self.stop_price:
                    self.stop_price = trail_stop
            if self.bars_held >= self.time_stop_bars and (self.entry_price - close) < self.time_stop_min_atr * atr_ref:
                self._exit("空头时间止损（超期未达浮盈门槛）")
                return
            if self.bars_held >= self.max_hold_bars:
                self._exit("空头最大持仓时长强制平仓")
            return

    def _exit(self, reason: str) -> None:
        side = self.position_side
        if self.instrument_id is not None and not self.is_flat(self.instrument_id):
            self.close_all_positions(self.instrument_id)

        equity_now = float(self.get_equity())
        if self.entry_equity > 0.0:
            if equity_now < self.entry_equity:
                self.loss_streak += 1
            else:
                self.loss_streak = 0

        if side == "LONG":
            self.last_long_exit_bar = self.bar_index
        elif side == "SHORT":
            self.last_short_exit_bar = self.bar_index

        self.log.info(f"平仓({side}): {reason}")
        self.position_side = "FLAT"
        self.entry_price = 0.0
        self.entry_atr = 0.0
        self.entry_equity = 0.0
        self.bars_held = 0
        self.stop_price = 0.0
        self.best_price = 0.0
        self.trail_active = False
        self.pending_signal = 0

    # ------------------------------------------------------------------
    # 风控与状态同步
    # ------------------------------------------------------------------
    def _update_daily_state(self, bar: Bar) -> None:
        day = int(bar.ts_event // 86_400_000_000_000)
        equity = float(self.get_equity())
        if day != self.current_day:
            self.current_day = day
            self.day_start_equity = equity if equity > 0.0 else 0.0
            self.day_blocked = False
            return
        if self.day_start_equity > 0.0 and equity > 0.0:
            loss = (self.day_start_equity - equity) / self.day_start_equity
            if loss >= self.daily_loss_limit:
                if not self.day_blocked:
                    self.log.info(f"日内熔断触发（当日回撤 {loss:.2%}），停止开新仓")
                self.day_blocked = True
                self.pending_signal = 0

    def _is_flat_sync(self) -> bool:
        flat = self.is_flat()
        if flat and self.position_side != "FLAT":
            self.position_side = "FLAT"
            self.bars_held = 0
            self.stop_price = 0.0
            self.best_price = 0.0
            self.trail_active = False
        if not flat and self.position_side == "FLAT":
            self.position_side = "LONG" if self.is_long() else "SHORT"
        return flat


# =============================================================================
# Manifest
# =============================================================================
STRATEGY_MANIFEST = StrategyManifest(
    slug="btc_macd_ma_gated_trend",
    name="MACD 趋势择时 + 均线闸门（BTC 1h/4h）",
    version="1.0.0",
    description=(
        "BTCUSDT-PERP 1h 执行、4h 闸门的多空双向趋势策略：MACD 柱动能转折/中继放大提供入场时机，"
        "1h EMA100 与 4h EMA50 及其斜率构成方向闸门，ADX(14)>20、ATR/价格 0.25%~4%、"
        "归一化动能滚动分位 <=0.90（镜像 >=0.10）三重过滤剔除震荡与追高，"
        "1.8ATR 硬止损 + 闸门反转 + MACD 柱反向 + 1.2ATR 追踪止盈（1.0ATR 启动）+ 30 根时间止损 + 72 根强制平仓，"
        "0.75% 单笔风险、3 倍名义上限、3% 日内熔断、连亏 4 笔仓位减半。"
    ),
    category="trend",
    strategy_path="app.strategies.btc_macd_ma_gated_trend:BtcMacdMaGatedTrendStrategy",
    config_path="app.strategies.btc_macd_ma_gated_trend:BtcMacdMaGatedTrendConfig",
    parameters={
        "macd_fast": ParameterSpec(title="MACD 快线周期", type="integer", default=12, minimum=5, maximum=30),
        "macd_slow": ParameterSpec(title="MACD 慢线周期", type="integer", default=26, minimum=10, maximum=60),
        "macd_signal": ParameterSpec(title="MACD 信号线周期", type="integer", default=9, minimum=3, maximum=20),
        "ema_gate_period": ParameterSpec(title="1h 均线闸门周期", type="integer", default=100, minimum=20, maximum=300),
        "ema_trend_4h": ParameterSpec(title="4h 趋势均线周期", type="integer", default=50, minimum=10, maximum=150),
        "bars_per_4h": ParameterSpec(title="每个 4h Bar 的 1h 根数", type="integer", default=4, minimum=1, maximum=24),
        "slope_lag": ParameterSpec(title="4h 斜率回看根数", type="integer", default=6, minimum=1, maximum=30),
        "slope_th": ParameterSpec(title="4h 斜率阈值", type="number", default=0.001, minimum=0.0, maximum=0.02, step=0.0005),
        "adx_period": ParameterSpec(title="ADX 周期", type="integer", default=14, minimum=5, maximum=50),
        "adx_th": ParameterSpec(title="ADX 阈值", type="number", default=20.0, minimum=5.0, maximum=45.0, step=1.0),
        "atr_period": ParameterSpec(title="ATR 周期", type="integer", default=14, minimum=5, maximum=50),
        "vol_lo": ParameterSpec(title="波动率下限 (ATR/价格)", type="number", default=0.0025, minimum=0.0005, maximum=0.02, step=0.0005),
        "vol_hi": ParameterSpec(title="波动率上限 (ATR/价格)", type="number", default=0.04, minimum=0.01, maximum=0.15, step=0.005),
        "q_window": ParameterSpec(title="动能分位滚动窗口", type="integer", default=500, minimum=50, maximum=2000),
        "q_cap": ParameterSpec(title="动能分位上限", type="number", default=0.90, minimum=0.60, maximum=1.0, step=0.01),
        "sl_atr": ParameterSpec(title="硬止损 ATR 倍数", type="number", default=1.8, minimum=0.8, maximum=5.0, step=0.1),
        "trail_atr": ParameterSpec(title="追踪回撤 ATR 倍数", type="number", default=1.2, minimum=0.5, maximum=5.0, step=0.1),
        "trail_trigger": ParameterSpec(title="追踪启动浮盈 ATR 倍数", type="number", default=1.0, minimum=0.2, maximum=5.0, step=0.1),
        "time_stop_bars": ParameterSpec(title="时间止损根数", type="integer", default=30, minimum=5, maximum=200),
        "time_stop_min_atr": ParameterSpec(title="时间止损浮盈门槛 (ATR)", type="number", default=0.5, minimum=0.0, maximum=3.0, step=0.1),
        "max_hold_bars": ParameterSpec(title="最大持仓根数", type="integer", default=72, minimum=10, maximum=500),
        "cooldown_bars": ParameterSpec(title="同方向冷却根数", type="integer", default=6, minimum=0, maximum=48),
        "risk_pct": ParameterSpec(title="单笔风险占权益比例", type="number", default=0.0075, minimum=0.001, maximum=0.03, step=0.0005),
        "max_leverage": ParameterSpec(title="名义敞口上限(倍权益)", type="number", default=3.0, minimum=0.5, maximum=10.0, step=0.5),
        "daily_loss_limit": ParameterSpec(title="日内熔断亏损阈值", type="number", default=0.03, minimum=0.005, maximum=0.2, step=0.005),
        "loss_streak_limit": ParameterSpec(title="连亏减仓触发笔数", type="integer", default=4, minimum=2, maximum=15),
        "loss_streak_scale": ParameterSpec(title="连亏后仓位系数", type="number", default=0.5, minimum=0.1, maximum=1.0, step=0.1),
        "allow_short": ParameterSpec(title="允许做空", type="boolean", default=True),
    },
    timeframes=("1h", "4h"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"name": "收盘价", "type": "line", "color": "#ffffff"},
            "ema_gate": {"name": "EMA100 (1h 闸门)", "type": "line", "color": "#22c55e"},
            "ema_trend_4h_line": {"name": "EMA50 (4h 趋势)", "type": "line", "color": "#ef4444"},
        },
        "subplots": {
            "MACD": {
                "macd_dif": {"name": "DIF", "type": "line", "color": "#60a5fa"},
                "macd_dea": {"name": "DEA", "type": "line", "color": "#f59e0b"},
                "macd_hist": {"name": "MACD 柱", "type": "histogram", "color": "#a855f7"},
            },
            "动能分位": {
                "hist_norm": {"name": "H/ATR", "type": "line", "color": "#38bdf8"},
                "hist_q": {"name": "滚动分位 q", "type": "line", "color": "#facc15"},
            },
            "闸门与趋势强度": {
                "gate": {"name": "均线闸门", "type": "histogram", "color": "#4ade80"},
                "slope_4h": {"name": "4h 均线斜率", "type": "line", "color": "#c084fc"},
                "adx": {"name": "ADX14", "type": "line", "color": "#fb923c"},
            },
            "波动率": {
                "atr": {"name": "ATR14", "type": "line", "color": "#fbbf24"},
                "atr_pct": {"name": "ATR/价格", "type": "line", "color": "#f472b6"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
)
