"""模式A 均值回归腿 v3：BBWidth 分位 + CHOP 状态识别 + %B 尾部极端反转（1h 执行）。

已确认的交易规则（严格对应 v3 研究方案，H3 状态修正 + H1 短持有期）：

1. 标的与周期: 单标的 PERP（默认 BTCUSDT），执行周期 1h；状态判定使用 4h 等效尺度
   （在 1h 序列上把状态类指标周期乘以 htf_factor=4，取已收盘 bar，决策后于下一根
   bar 开盘执行，等价于 shift(1)，不含未来函数）。

2. 状态定义（H3：取代原 ADX<20）
   - BBWidth   = (BB_up - BB_low) / BB_mid          （state_bb_period × htf_factor）
   - BBW_pctile= BBWidth 在 state_lookback × htf_factor 根内的滚动分位（0-100）
   - CHOP(n)   = 100 * log10(sum(TR,n) / (max(H,n) - min(L,n))) / log10(n)
   - RANGE  := bbw_pctile_low <= BBW_pctile <= bbw_pctile_high  AND  CHOP > chop_range
   - TREND  := BBW_pctile > bbw_trend_pctile AND CHOP < chop_trend
   - NEUTRAL:= 其余；本腿只在 RANGE 状态开仓，TREND/NEUTRAL 一律空仓。

3. 信号（H1：只取尾部极端）
   - percent_b = (close - BB_low) / (BB_up - BB_low)   （1h BB(bb_period, bb_std)）
   - long_sig  := percent_b < pct_b_long(0.05) AND RSI(14) < rsi_long(30)
                  AND close > open AND volume > vol_mult(1.3) * SMA(volume, vol_period)
   - short_sig := percent_b > pct_b_short(0.95) AND RSI(14) > rsi_short(70)
                  AND close < open AND volume > vol_mult * SMA(volume, vol_period)

4. 执行: 信号 bar 收盘确认，下一根 bar 开盘市价单成交（pending 信号机制）。同时最多 1 个持仓。

5. 出场（v3 三项全改，优先级自上而下）
   a) 止损 = 入场价 ∓ sl_atr(1.0) × ATR(14, 入场时)
   b) 止盈 = 入场价 ± tp_atr(0.5) × ATR(14, 入场时)
   c) %B 回到 pct_b_exit(0.5) 中枢 → 平仓
   d) 时间止损: 持仓 hold_bars(3) 根 1h 仍未触发 → 平仓
   （不使用 BB 中轨目标：10-20 根窗口存在负漂移，中轨目标与漂移冲突）

6. 熔断
   - 同向连续 consecutive_stop_limit(2) 次止损 → 该 RANGE 周期内停用该方向，
     状态离开 RANGE 后计数清零解锁；
   - 单日累计亏损 ≥ daily_loss_limit(2%) 权益 → 当日停止开新仓；
   - 滚动权益回撤 ≥ dd_scale_down(12%) → 仓位减半；≥ dd_halt(20%) → 停止开新仓。

7. 风险与仓位
   - risk_per_trade = 0.4% 权益，size = risk × equity / (sl_atr × ATR_1h)
   - 波动缩放 vol_scalar: ATR_Ratio <= atr_ratio_mid(1.0) → 1.0；
     <= atr_ratio_high(1.5) → 0.75；否则 0.5（ATR_Ratio = ATR / SMA(ATR, atr_ratio_lookback)）
   - 名义敞口上限 max_notional_mult = 1.5 × 权益

8. allow_short=False 时退化为纯多头版本，用于方向消融对比。

风险提示（研究结论如实保留）: 因子分析显示 %B 在 10-20 根 1h 尺度上呈动量特征，
本腿依赖的是 1-3 根超短窗口修正，IC 量级仅 0.02-0.05，属极弱信号；3 根持有期换手高，
成本敏感度是首要风险，必须在 2× 成本情形下复核。
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
# 配置（字段与 STRATEGY_MANIFEST.parameters 严格一一对应）
# =============================================================================
class BtcBbRangeReversionV3Config(StrategyConfig, frozen=True):
    """模式A 均值回归腿 v3 配置。"""

    instrument_id: InstrumentId
    bar_type: BarType
    bb_period: int = 20
    bb_std: float = 2.0
    rsi_period: int = 14
    atr_period: int = 14
    htf_factor: int = 4
    state_bb_period: int = 20
    state_lookback: int = 250
    bbw_pctile_low: float = 20.0
    bbw_pctile_high: float = 50.0
    bbw_trend_pctile: float = 70.0
    chop_period: int = 14
    chop_range: float = 60.0
    chop_trend: float = 40.0
    pct_b_long: float = 0.05
    pct_b_short: float = 0.95
    rsi_long: float = 30.0
    rsi_short: float = 70.0
    vol_period: int = 20
    vol_mult: float = 1.3
    hold_bars: int = 3
    tp_atr: float = 0.5
    sl_atr: float = 1.0
    pct_b_exit: float = 0.5
    risk_per_trade: float = 0.004
    atr_ratio_lookback: int = 100
    atr_ratio_mid: float = 1.0
    atr_ratio_high: float = 1.5
    max_notional_mult: float = 1.5
    consecutive_stop_limit: int = 2
    daily_loss_limit: float = 0.02
    dd_window_bars: int = 720
    dd_scale_down: float = 0.12
    dd_halt: float = 0.20
    allow_short: bool = True


# =============================================================================
# 向量化指标工具
# =============================================================================
def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    return pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    period = max(2, int(period))
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    period = max(2, int(period))
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=1).mean()
    rs = avg_gain / avg_loss.where(avg_loss > 1e-12, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.where(avg_loss > 1e-12, 100.0).where(avg_gain > 1e-12, rsi.fillna(50.0))


def _rolling_percentile_rank(series: pd.Series, lookback: int, min_periods: int = 40) -> pd.Series:
    lookback = max(10, int(lookback))
    min_periods = max(5, min(int(min_periods), lookback))
    try:
        ranked = series.rolling(lookback, min_periods=min_periods).rank(pct=True)
    except (AttributeError, TypeError, ValueError):
        ranked = series.rolling(lookback, min_periods=min_periods).apply(
            lambda arr: float((arr <= arr[-1]).mean()) if len(arr) > 0 else np.nan,
            raw=True,
        )
    return ranked * 100.0


def _choppiness(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    period = max(3, int(period))
    tr_sum = _true_range(high, low, close).rolling(period, min_periods=2).sum()
    rng = high.rolling(period, min_periods=2).max() - low.rolling(period, min_periods=2).min()
    safe_rng = rng.where(rng > 1e-12, np.nan)
    ratio = (tr_sum / safe_rng).where(lambda s: s > 1e-12, np.nan)
    return 100.0 * np.log10(ratio) / float(np.log10(period))


def _clean(series: pd.Series) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce")
    out = out.replace([np.inf, -np.inf], np.nan)
    return out.bfill().fillna(0.0)


def _resolve_params(parameters: dict) -> dict:
    """把外部参数字典解析为带默认值与下界保护的纯数值字典。"""

    def _i(key: str, default: int, low: int = 1) -> int:
        raw = parameters.get(key)
        try:
            val = int(raw) if raw is not None else int(default)
        except (TypeError, ValueError):
            val = int(default)
        return max(low, val)

    def _f(key: str, default: float) -> float:
        raw = parameters.get(key)
        try:
            val = float(raw) if raw is not None else float(default)
        except (TypeError, ValueError):
            val = float(default)
        if not np.isfinite(val):
            val = float(default)
        return val

    return {
        "bb_period": _i("bb_period", 20, 2),
        "bb_std": _f("bb_std", 2.0),
        "rsi_period": _i("rsi_period", 14, 2),
        "atr_period": _i("atr_period", 14, 2),
        "htf_factor": _i("htf_factor", 4, 1),
        "state_bb_period": _i("state_bb_period", 20, 2),
        "state_lookback": _i("state_lookback", 250, 20),
        "bbw_pctile_low": _f("bbw_pctile_low", 20.0),
        "bbw_pctile_high": _f("bbw_pctile_high", 50.0),
        "bbw_trend_pctile": _f("bbw_trend_pctile", 70.0),
        "chop_period": _i("chop_period", 14, 3),
        "chop_range": _f("chop_range", 60.0),
        "chop_trend": _f("chop_trend", 40.0),
        "pct_b_long": _f("pct_b_long", 0.05),
        "pct_b_short": _f("pct_b_short", 0.95),
        "rsi_long": _f("rsi_long", 30.0),
        "rsi_short": _f("rsi_short", 70.0),
        "vol_period": _i("vol_period", 20, 2),
        "vol_mult": _f("vol_mult", 1.3),
        "hold_bars": _i("hold_bars", 3, 1),
        "tp_atr": _f("tp_atr", 0.5),
        "sl_atr": _f("sl_atr", 1.0),
        "pct_b_exit": _f("pct_b_exit", 0.5),
        "risk_per_trade": _f("risk_per_trade", 0.004),
        "atr_ratio_lookback": _i("atr_ratio_lookback", 100, 10),
        "atr_ratio_mid": _f("atr_ratio_mid", 1.0),
        "atr_ratio_high": _f("atr_ratio_high", 1.5),
        "max_notional_mult": _f("max_notional_mult", 1.5),
        "consecutive_stop_limit": _i("consecutive_stop_limit", 2, 1),
        "daily_loss_limit": _f("daily_loss_limit", 0.02),
        "dd_window_bars": _i("dd_window_bars", 720, 30),
        "dd_scale_down": _f("dd_scale_down", 0.12),
        "dd_halt": _f("dd_halt", 0.20),
        "allow_short": bool(parameters.get("allow_short", True)),
    }


# =============================================================================
# 向量化指标（覆盖 plot_config 声明的全部列，行数不变，头部 NaN 统一 bfill().fillna(0.0)）
# =============================================================================
def calculate_indicators(dataframe: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    """计算 1h 信号指标 + 4h 等效状态指标，全部列与 plot_config 一致。"""
    result = dataframe.copy()
    cfg = _resolve_params(parameters or {})

    def _num(col: str, fallback: float = 0.0) -> pd.Series:
        if col not in result.columns:
            return pd.Series(fallback, index=result.index, dtype=float)
        return pd.to_numeric(result[col], errors="coerce").ffill().bfill().fillna(fallback)

    close = _num("close")
    high = _num("high")
    low = _num("low")
    open_ = _num("open")
    volume = _num("volume")

    # --- 1h 布林带与 %B ---
    bb_period = cfg["bb_period"]
    bb_mid = close.rolling(bb_period, min_periods=2).mean()
    bb_sd = close.rolling(bb_period, min_periods=2).std(ddof=0).fillna(0.0)
    bb_upper = bb_mid + cfg["bb_std"] * bb_sd
    bb_lower = bb_mid - cfg["bb_std"] * bb_sd
    band = (bb_upper - bb_lower)
    percent_b = (close - bb_lower) / band.where(band > 1e-12, np.nan)
    percent_b = percent_b.clip(lower=-0.5, upper=1.5)

    # --- 1h RSI / ATR / 量能 ---
    rsi = _wilder_rsi(close, cfg["rsi_period"])
    atr = _wilder_atr(high, low, close, cfg["atr_period"])
    atr_ma = atr.rolling(cfg["atr_ratio_lookback"], min_periods=10).mean()
    atr_ratio = atr / atr_ma.where(atr_ma > 1e-12, np.nan)
    vol_ma = volume.rolling(cfg["vol_period"], min_periods=2).mean()
    vol_ratio = volume / vol_ma.where(vol_ma > 1e-12, np.nan)

    # --- 4h 等效状态（周期 × htf_factor，取已收盘 1h bar 聚合尺度） ---
    factor = cfg["htf_factor"]
    state_period = max(2, cfg["state_bb_period"] * factor)
    state_mid = close.rolling(state_period, min_periods=5).mean()
    state_sd = close.rolling(state_period, min_periods=5).std(ddof=0).fillna(0.0)
    state_width = (2.0 * cfg["bb_std"] * state_sd) / state_mid.where(state_mid.abs() > 1e-12, np.nan)
    bbw_pctile = _rolling_percentile_rank(
        state_width,
        cfg["state_lookback"] * factor,
        min_periods=max(20, state_period // 2),
    )
    chop = _choppiness(high, low, close, max(3, cfg["chop_period"] * factor))

    regime_range = (
        (bbw_pctile >= cfg["bbw_pctile_low"])
        & (bbw_pctile <= cfg["bbw_pctile_high"])
        & (chop > cfg["chop_range"])
    )
    regime_trend = (bbw_pctile > cfg["bbw_trend_pctile"]) & (chop < cfg["chop_trend"])

    long_signal = (
        regime_range
        & (percent_b < cfg["pct_b_long"])
        & (rsi < cfg["rsi_long"])
        & (close > open_)
        & (vol_ratio > cfg["vol_mult"])
    )
    short_signal = (
        regime_range
        & (percent_b > cfg["pct_b_short"])
        & (rsi > cfg["rsi_short"])
        & (close < open_)
        & (vol_ratio > cfg["vol_mult"])
    )
    if not cfg["allow_short"]:
        short_signal = short_signal & False

    result["bb_upper"] = _clean(bb_upper)
    result["bb_mid"] = _clean(bb_mid)
    result["bb_lower"] = _clean(bb_lower)
    result["percent_b"] = _clean(percent_b)
    result["rsi"] = _clean(rsi)
    result["atr"] = _clean(atr)
    result["atr_ratio"] = _clean(atr_ratio)
    result["vol_ratio"] = _clean(vol_ratio)
    result["bbw_pctile"] = _clean(bbw_pctile)
    result["chop"] = _clean(chop)
    result["regime_range"] = _clean(regime_range.astype(float))
    result["regime_trend"] = _clean(regime_trend.astype(float))
    result["long_signal"] = _clean(long_signal.astype(float))
    result["short_signal"] = _clean(short_signal.astype(float))
    return result


# =============================================================================
# 策略
# =============================================================================
class BtcBbRangeReversionV3Strategy(QuantLabStrategy):
    """RANGE 状态下的 %B 尾部极端反转腿（超短持有期 + ATR 双闸门 + 多重熔断）。"""

    def __init__(self, config: BtcBbRangeReversionV3Config) -> None:
        super().__init__(config)
        self.p = _resolve_params(
            {
                "bb_period": config.bb_period,
                "bb_std": config.bb_std,
                "rsi_period": config.rsi_period,
                "atr_period": config.atr_period,
                "htf_factor": config.htf_factor,
                "state_bb_period": config.state_bb_period,
                "state_lookback": config.state_lookback,
                "bbw_pctile_low": config.bbw_pctile_low,
                "bbw_pctile_high": config.bbw_pctile_high,
                "bbw_trend_pctile": config.bbw_trend_pctile,
                "chop_period": config.chop_period,
                "chop_range": config.chop_range,
                "chop_trend": config.chop_trend,
                "pct_b_long": config.pct_b_long,
                "pct_b_short": config.pct_b_short,
                "rsi_long": config.rsi_long,
                "rsi_short": config.rsi_short,
                "vol_period": config.vol_period,
                "vol_mult": config.vol_mult,
                "hold_bars": config.hold_bars,
                "tp_atr": config.tp_atr,
                "sl_atr": config.sl_atr,
                "pct_b_exit": config.pct_b_exit,
                "risk_per_trade": config.risk_per_trade,
                "atr_ratio_lookback": config.atr_ratio_lookback,
                "atr_ratio_mid": config.atr_ratio_mid,
                "atr_ratio_high": config.atr_ratio_high,
                "max_notional_mult": config.max_notional_mult,
                "consecutive_stop_limit": config.consecutive_stop_limit,
                "daily_loss_limit": config.daily_loss_limit,
                "dd_window_bars": config.dd_window_bars,
                "dd_scale_down": config.dd_scale_down,
                "dd_halt": config.dd_halt,
                "allow_short": config.allow_short,
            }
        )

        # 运行状态
        self.pending_signal: int = 0
        self.position_side: str = "FLAT"
        self.entry_price: float = 0.0
        self.entry_atr: float = 0.0
        self.stop_price: float = 0.0
        self.target_price: float = 0.0
        self.bars_held: int = 0

        # 熔断状态
        self.long_stop_streak: int = 0
        self.short_stop_streak: int = 0
        self.prev_regime_range: bool = False
        self.equity_history: list[float] = []
        self.current_day: int = -1
        self.day_start_equity: float = 0.0
        self.day_blocked: bool = False
        self.last_equity: float = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_start(self) -> None:
        super().on_start()
        p = self.p
        self.log.info(
            "启动 BB %B RANGE 反转腿 v3: "
            f"BB({p['bb_period']},{p['bb_std']}) RSI{p['rsi_period']} ATR{p['atr_period']} "
            f"状态=BBW分位[{p['bbw_pctile_low']},{p['bbw_pctile_high']}] & CHOP>{p['chop_range']} "
            f"(htf_factor={p['htf_factor']}) 信号=%B<{p['pct_b_long']}/>{p['pct_b_short']} "
            f"RSI<{p['rsi_long']}/>{p['rsi_short']} vol>{p['vol_mult']}x "
            f"出场: TP={p['tp_atr']}ATR SL={p['sl_atr']}ATR %B回{p['pct_b_exit']} "
            f"时间止损={p['hold_bars']}根 risk={p['risk_per_trade']} short={p['allow_short']}"
        )

    def on_bar(self, bar: Bar) -> None:
        super().on_bar(bar)

        p = self.p
        warmup = max(
            p["state_bb_period"] * p["htf_factor"] + 5,
            p["chop_period"] * p["htf_factor"] + 5,
            p["atr_ratio_lookback"] + 5,
            p["bb_period"] + 5,
            p["vol_period"] + 5,
            60,
        )
        frame = self.get_df()
        if len(frame) < warmup:
            return

        window = min(len(frame), max(1200, p["state_lookback"] * p["htf_factor"] + 200))
        ind = self._snapshot(frame.iloc[-window:])
        if ind is None:
            return

        self._track_equity()
        self._track_daily(bar)
        self._track_regime_cycle(ind)

        # 1) 执行上一根收盘确认的信号（下一根 bar 开盘执行，杜绝自引用）
        if self.pending_signal != 0:
            signal = self.pending_signal
            self.pending_signal = 0
            if self._is_flat_sync():
                self._enter(signal, bar, ind)
                return

        # 2) 持仓管理
        if not self._is_flat_sync():
            self.bars_held += 1
            self._manage_position(bar, ind)
            return

        # 3) 空仓：评估入场信号（本根收盘确认）
        self.position_side = "FLAT"
        self.pending_signal = self._evaluate_entry(ind)

    # ------------------------------------------------------------------
    # 指标快照（复用向量化实现，保证回测与图表完全一致）
    # ------------------------------------------------------------------
    def _snapshot(self, frame: pd.DataFrame) -> dict[str, float] | None:
        if len(frame) < 30:
            return None
        try:
            calculated = calculate_indicators(frame, dict(self.p))
        except Exception as exc:  # noqa: BLE001 - 保护回测主循环
            self.log.warning(f"指标计算失败，跳过本根: {exc}")
            return None

        row = calculated.iloc[-1]
        snapshot = {
            "close": float(row["close"]),
            "open": float(row["open"]),
            "percent_b": float(row["percent_b"]),
            "rsi": float(row["rsi"]),
            "atr": float(row["atr"]),
            "atr_ratio": float(row["atr_ratio"]),
            "vol_ratio": float(row["vol_ratio"]),
            "bbw_pctile": float(row["bbw_pctile"]),
            "chop": float(row["chop"]),
            "regime_range": float(row["regime_range"]),
            "long_signal": float(row["long_signal"]),
            "short_signal": float(row["short_signal"]),
        }
        if not np.isfinite(snapshot["atr"]) or snapshot["atr"] <= 0.0:
            return None
        if not np.isfinite(snapshot["close"]) or snapshot["close"] <= 0.0:
            return None

        self.record("percent_b", snapshot["percent_b"])
        self.record("bbw_pctile", snapshot["bbw_pctile"])
        self.record("chop", snapshot["chop"])
        self.record("regime_range", snapshot["regime_range"])
        return snapshot

    # ------------------------------------------------------------------
    # 状态与熔断
    # ------------------------------------------------------------------
    def _track_equity(self) -> None:
        equity = float(self.get_equity())
        if np.isfinite(equity) and equity > 0.0:
            self.last_equity = equity
            self.equity_history.append(equity)
            if len(self.equity_history) > self.p["dd_window_bars"]:
                self.equity_history = self.equity_history[-self.p["dd_window_bars"] :]

    def _track_daily(self, bar: Bar) -> None:
        day = int(bar.ts_event // 86_400_000_000_000)
        equity = self.last_equity if self.last_equity > 0.0 else float(self.get_equity())
        if day != self.current_day:
            self.current_day = day
            self.day_start_equity = equity
            self.day_blocked = False
            return
        if self.day_start_equity > 0.0:
            day_pnl = (equity - self.day_start_equity) / self.day_start_equity
            if day_pnl <= -abs(self.p["daily_loss_limit"]):
                self.day_blocked = True

    def _track_regime_cycle(self, ind: dict[str, float]) -> None:
        in_range = ind["regime_range"] > 0.5
        if self.prev_regime_range and not in_range:
            # 离开 RANGE 周期：连续止损计数清零解锁
            self.long_stop_streak = 0
            self.short_stop_streak = 0
        self.prev_regime_range = in_range

    def _rolling_drawdown(self) -> float:
        if len(self.equity_history) < 2:
            return 0.0
        peak = max(self.equity_history)
        if peak <= 0.0:
            return 0.0
        return max(0.0, (peak - self.equity_history[-1]) / peak)

    def _risk_scale(self) -> float:
        drawdown = self._rolling_drawdown()
        if drawdown >= self.p["dd_halt"]:
            return 0.0
        if drawdown >= self.p["dd_scale_down"]:
            return 0.5
        return 1.0

    def _vol_scalar(self, atr_ratio: float) -> float:
        if not np.isfinite(atr_ratio) or atr_ratio <= 0.0:
            return 1.0
        if atr_ratio <= self.p["atr_ratio_mid"]:
            return 1.0
        if atr_ratio <= self.p["atr_ratio_high"]:
            return 0.75
        return 0.5

    # ------------------------------------------------------------------
    # 入场
    # ------------------------------------------------------------------
    def _evaluate_entry(self, ind: dict[str, float]) -> int:
        if self.day_blocked or self._risk_scale() <= 0.0:
            return 0
        if ind["regime_range"] <= 0.5:
            return 0

        limit = self.p["consecutive_stop_limit"]
        if ind["long_signal"] > 0.5 and self.long_stop_streak < limit:
            return 1
        if (
            self.p["allow_short"]
            and ind["short_signal"] > 0.5
            and self.short_stop_streak < limit
        ):
            return -1
        return 0

    def _enter(self, signal: int, bar: Bar, ind: dict[str, float]) -> None:
        price = float(bar.open.as_double())
        if not np.isfinite(price) or price <= 0.0:
            price = float(bar.close.as_double())
        atr_now = ind["atr"]
        qty = self._position_size(price, atr_now, ind["atr_ratio"])
        if qty <= 0.0:
            self.log.info("仓位为 0，跳过入场")
            return

        stop_distance = self.p["sl_atr"] * atr_now
        target_distance = self.p["tp_atr"] * atr_now
        if signal > 0:
            self.buy_market(trade_size=qty)
            self.position_side = "LONG"
            self.stop_price = price - stop_distance
            self.target_price = price + target_distance
        else:
            self.sell_market(trade_size=qty)
            self.position_side = "SHORT"
            self.stop_price = price + stop_distance
            self.target_price = price - target_distance

        self.entry_price = price
        self.entry_atr = atr_now
        self.bars_held = 0
        self.log.info(
            f"{self.position_side} 开仓 @ {price:.2f} qty={qty:.6f} atr={atr_now:.2f} "
            f"stop={self.stop_price:.2f} target={self.target_price:.2f} "
            f"%B={ind['percent_b']:.3f} RSI={ind['rsi']:.1f} BBW分位={ind['bbw_pctile']:.1f} "
            f"CHOP={ind['chop']:.1f}"
        )

    def _position_size(self, price: float, atr_now: float, atr_ratio: float) -> float:
        equity = float(self.get_equity())
        scale = self._risk_scale() * self._vol_scalar(atr_ratio)
        if equity <= 0.0 or scale <= 0.0 or atr_now <= 0.0 or price <= 0.0:
            return 0.0
        stop_distance = self.p["sl_atr"] * atr_now
        if stop_distance <= 0.0:
            return 0.0
        qty = (equity * self.p["risk_per_trade"] * scale) / stop_distance
        max_qty = (equity * self.p["max_notional_mult"]) / price
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
        percent_b = ind["percent_b"]

        if self.position_side == "LONG":
            if low <= self.stop_price:
                self._exit(f"多头 ATR 止损 @ {self.stop_price:.2f}", stopped=True)
                return
            if high >= self.target_price:
                self._exit(f"多头 ATR 止盈 @ {self.target_price:.2f}", stopped=False)
                return
            if percent_b >= self.p["pct_b_exit"]:
                self._exit(f"多头 %B 回中枢离场 (%B={percent_b:.3f})", stopped=False)
                return
            if self.bars_held >= self.p["hold_bars"]:
                self._exit("多头时间止损（超出短窗口修正区）", stopped=False)
                return
            return

        if self.position_side == "SHORT":
            if high >= self.stop_price:
                self._exit(f"空头 ATR 止损 @ {self.stop_price:.2f}", stopped=True)
                return
            if low <= self.target_price:
                self._exit(f"空头 ATR 止盈 @ {self.target_price:.2f}", stopped=False)
                return
            if percent_b <= self.p["pct_b_exit"]:
                self._exit(f"空头 %B 回中枢离场 (%B={percent_b:.3f})", stopped=False)
                return
            if self.bars_held >= self.p["hold_bars"]:
                self._exit("空头时间止损（超出短窗口修正区）", stopped=False)
                return
            return

    def _exit(self, reason: str, stopped: bool) -> None:
        side = self.position_side
        if self.instrument_id is not None:
            self.close_all_positions(self.instrument_id)
        if stopped:
            if side == "LONG":
                self.long_stop_streak += 1
            elif side == "SHORT":
                self.short_stop_streak += 1
        else:
            if side == "LONG":
                self.long_stop_streak = 0
            elif side == "SHORT":
                self.short_stop_streak = 0
        self.log.info(f"平仓: {reason}")
        self.position_side = "FLAT"
        self.entry_price = 0.0
        self.entry_atr = 0.0
        self.stop_price = 0.0
        self.target_price = 0.0
        self.bars_held = 0
        self.pending_signal = 0

    # ------------------------------------------------------------------
    # 持仓状态同步
    # ------------------------------------------------------------------
    def _is_flat_sync(self) -> bool:
        flat = self.is_flat()
        if flat and self.position_side != "FLAT":
            self.position_side = "FLAT"
            self.bars_held = 0
            self.stop_price = 0.0
            self.target_price = 0.0
        if not flat and self.position_side == "FLAT":
            self.position_side = "LONG" if self.is_long() else "SHORT"
        return flat


# =============================================================================
# Manifest
# =============================================================================
STRATEGY_MANIFEST = StrategyManifest(
    slug="btc_bb_range_reversion_v3",
    name="模式A 均值回归腿 v3（BBW 分位 + CHOP 状态 + %B 尾部反转）",
    version="3.0.0",
    description=(
        "1h 执行的 %B 尾部极端反转腿：以 4h 等效尺度的 BBWidth 滚动分位 + CHOP 双条件"
        "重新定义 RANGE 状态（取代 ADX<20），仅在 RANGE 内取 %B<0.05/RSI<30（做多）与"
        " %B>0.95/RSI>70（做空）的尾部极端信号，并要求同向 K 线方向与 1.3 倍量能确认；"
        "出场为 0.5×ATR 止盈、1.0×ATR 止损、%B 回到 0.5 中枢、3 根 1h 时间止损四者先到先平；"
        "风险控制含 0.4% 单笔风险、ATR_Ratio 波动缩放、1.5× 名义上限、同向连续 2 次止损锁定、"
        "单日亏损 2% 停开与 12%/20% 滚动回撤降半仓/停机。"
    ),
    category="mean_reversion",
    strategy_path="app.strategies.btc_bb_range_reversion_v3:BtcBbRangeReversionV3Strategy",
    config_path="app.strategies.btc_bb_range_reversion_v3:BtcBbRangeReversionV3Config",
    parameters={
        "bb_period": ParameterSpec(title="1h 布林带周期", type="integer", default=20, minimum=10, maximum=60),
        "bb_std": ParameterSpec(title="布林带标准差倍数", type="number", default=2.0, minimum=1.0, maximum=3.5, step=0.1),
        "rsi_period": ParameterSpec(title="RSI 周期", type="integer", default=14, minimum=5, maximum=50),
        "atr_period": ParameterSpec(title="ATR 周期", type="integer", default=14, minimum=5, maximum=50),
        "htf_factor": ParameterSpec(title="状态周期放大倍数(4h/1h)", type="integer", default=4, minimum=1, maximum=12),
        "state_bb_period": ParameterSpec(title="状态布林带周期(HTF)", type="integer", default=20, minimum=10, maximum=60),
        "state_lookback": ParameterSpec(title="BBW 分位回看根数(HTF)", type="integer", default=250, minimum=50, maximum=500),
        "bbw_pctile_low": ParameterSpec(title="RANGE 带宽分位下界", type="number", default=20.0, minimum=5.0, maximum=45.0, step=1.0),
        "bbw_pctile_high": ParameterSpec(title="RANGE 带宽分位上界", type="number", default=50.0, minimum=30.0, maximum=80.0, step=1.0),
        "bbw_trend_pctile": ParameterSpec(title="TREND 带宽分位阈值", type="number", default=70.0, minimum=50.0, maximum=95.0, step=1.0),
        "chop_period": ParameterSpec(title="CHOP 周期(HTF)", type="integer", default=14, minimum=5, maximum=40),
        "chop_range": ParameterSpec(title="RANGE CHOP 下限", type="number", default=60.0, minimum=45.0, maximum=75.0, step=1.0),
        "chop_trend": ParameterSpec(title="TREND CHOP 上限", type="number", default=40.0, minimum=20.0, maximum=55.0, step=1.0),
        "pct_b_long": ParameterSpec(title="做多 %B 上限", type="number", default=0.05, minimum=0.0, maximum=0.2, step=0.01),
        "pct_b_short": ParameterSpec(title="做空 %B 下限", type="number", default=0.95, minimum=0.8, maximum=1.0, step=0.01),
        "rsi_long": ParameterSpec(title="做多 RSI 上限", type="number", default=30.0, minimum=15.0, maximum=40.0, step=1.0),
        "rsi_short": ParameterSpec(title="做空 RSI 下限", type="number", default=70.0, minimum=60.0, maximum=85.0, step=1.0),
        "vol_period": ParameterSpec(title="量能均值周期", type="integer", default=20, minimum=5, maximum=100),
        "vol_mult": ParameterSpec(title="量能确认倍数", type="number", default=1.3, minimum=0.8, maximum=3.0, step=0.1),
        "hold_bars": ParameterSpec(title="时间止损持仓根数(1h)", type="integer", default=3, minimum=1, maximum=12),
        "tp_atr": ParameterSpec(title="止盈 ATR 倍数", type="number", default=0.5, minimum=0.2, maximum=2.0, step=0.1),
        "sl_atr": ParameterSpec(title="止损 ATR 倍数", type="number", default=1.0, minimum=0.5, maximum=3.0, step=0.1),
        "pct_b_exit": ParameterSpec(title="%B 中枢离场阈值", type="number", default=0.5, minimum=0.3, maximum=0.7, step=0.05),
        "risk_per_trade": ParameterSpec(title="单笔风险占权益比例", type="number", default=0.004, minimum=0.001, maximum=0.02, step=0.0005),
        "atr_ratio_lookback": ParameterSpec(title="ATR 均值回看根数", type="integer", default=100, minimum=20, maximum=400),
        "atr_ratio_mid": ParameterSpec(title="波动缩放中枢(ATR_Ratio)", type="number", default=1.0, minimum=0.5, maximum=2.0, step=0.05),
        "atr_ratio_high": ParameterSpec(title="波动缩放高位(ATR_Ratio)", type="number", default=1.5, minimum=1.0, maximum=3.0, step=0.05),
        "max_notional_mult": ParameterSpec(title="名义敞口上限(倍权益)", type="number", default=1.5, minimum=0.2, maximum=5.0, step=0.1),
        "consecutive_stop_limit": ParameterSpec(title="同向连续止损锁定次数", type="integer", default=2, minimum=1, maximum=6),
        "daily_loss_limit": ParameterSpec(title="单日亏损停开阈值", type="number", default=0.02, minimum=0.005, maximum=0.1, step=0.005),
        "dd_window_bars": ParameterSpec(title="滚动回撤窗口(根)", type="integer", default=720, minimum=60, maximum=3000),
        "dd_scale_down": ParameterSpec(title="回撤减半阈值", type="number", default=0.12, minimum=0.03, maximum=0.4, step=0.01),
        "dd_halt": ParameterSpec(title="回撤停开新仓阈值", type="number", default=0.20, minimum=0.05, maximum=0.6, step=0.01),
        "allow_short": ParameterSpec(title="允许做空", type="boolean", default=True),
    },
    timeframes=("1h", "4h"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"name": "收盘价", "type": "line", "color": "#ffffff"},
            "bb_upper": {"name": "布林上轨", "type": "line", "color": "#60a5fa"},
            "bb_mid": {"name": "布林中轨", "type": "line", "color": "#93c5fd"},
            "bb_lower": {"name": "布林下轨", "type": "line", "color": "#60a5fa"},
        },
        "subplots": {
            "%B 与 RSI": {
                "percent_b": {"name": "布林 %B", "type": "line", "color": "#f472b6"},
                "rsi": {"name": "RSI14", "type": "line", "color": "#facc15"},
            },
            "状态识别(4h 等效)": {
                "bbw_pctile": {"name": "BBW 分位", "type": "line", "color": "#38bdf8"},
                "chop": {"name": "CHOP", "type": "line", "color": "#c084fc"},
                "regime_range": {"name": "RANGE 状态", "type": "histogram", "color": "#22c55e"},
                "regime_trend": {"name": "TREND 状态", "type": "histogram", "color": "#ef4444"},
            },
            "波动": {
                "atr": {"name": "ATR14", "type": "line", "color": "#fb923c"},
                "atr_ratio": {"name": "ATR 比率", "type": "line", "color": "#a855f7"},
            },
            "量能比": {
                "vol_ratio": {"name": "量能/均量", "type": "histogram", "color": "#4ade80"},
            },
            "信号": {
                "long_signal": {"name": "做多信号", "type": "histogram", "color": "#16a34a"},
                "short_signal": {"name": "做空信号", "type": "histogram", "color": "#dc2626"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
)
