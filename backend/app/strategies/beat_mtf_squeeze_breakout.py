from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
from app.strategy_contract import ParameterSpec, StrategyManifest, StrategyMode
from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy

# --- 资金费率视角（优化 11）：平台未提供 funding 数据，按用户确认固定写死为常量 ---
FUNDING_RATE_CONST: float = 0.0001          # 假定同向资金费率 0.01% / 8h
FUNDING_EXTREME_THRESHOLD: float = 0.001    # 极端分位阈值 0.1% / 8h，超过则禁止同向新开仓
MAX_HISTORY_BARS: int = 1500


class BeatMtfSqueezeBreakoutConfig(StrategyConfig, frozen=True):
    instrument_id: InstrumentId
    bar_type: BarType
    bb_4h_period: int = 20
    adx4h_period: int = 14
    adx4h_threshold: float = 20.0
    lookback_high_low_period: int = 20
    atr_multiplier_entry: float = 0.6
    atr_period: int = 14
    bb_length: int = 20
    bb_mult: float = 2.0
    kc_length: int = 20
    kc_mult: float = 1.5
    squeeze_pct_window: int = 100
    squeeze_pct_threshold: float = 0.20
    vol_pct_window: int = 200
    volume_pct_threshold: float = 0.75
    er_period: int = 10
    er_threshold: float = 0.35
    mom_z_period: int = 10
    mom_z_threshold: float = 0.5
    ma_fast_period: int = 20
    ma_slow_period: int = 50
    max_atr_pct_no_entry: float = 0.06
    sl_atr_mult: float = 2.2
    trail_atr_mult: float = 2.8
    max_bars_in_trade: int = 48
    risk_per_trade: float = 0.008
    dir_gate_buffer_atr: float = 0.0
    max_add_times: int = 1
    add_size_pct: float = 0.5
    max_total_exposure_pct: float = 1.0
    trade_size: Decimal = Decimal("0.01")


def _wilder_rma(series: pd.Series, period: int) -> pd.Series:
    period = max(int(period), 1)
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    return _wilder_rma(_true_range(high, low, close), period)


def _wilder_adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    period = max(int(period), 1)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0), index=high.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0), index=high.index)
    atr = _wilder_rma(_true_range(high, low, close), period)
    safe_atr = atr.replace(0.0, np.nan)
    plus_di = 100.0 * _wilder_rma(plus_dm, period) / safe_atr
    minus_di = 100.0 * _wilder_rma(minus_dm, period) / safe_atr
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return _wilder_rma(dx.fillna(0.0), period)


def _efficiency_ratio(close: pd.Series, period: int) -> pd.Series:
    period = max(int(period), 1)
    direction = (close - close.shift(period)).abs()
    volatility = close.diff().abs().rolling(window=period, min_periods=period).sum()
    return (direction / volatility.replace(0.0, np.nan)).clip(0.0, 1.0)


def _datetime_index(df: pd.DataFrame) -> pd.DatetimeIndex | None:
    if isinstance(df.index, pd.DatetimeIndex):
        return df.index
    for col in ("timestamp", "time", "datetime", "date", "open_time", "ts_event"):
        if col in df.columns:
            try:
                idx = pd.DatetimeIndex(pd.to_datetime(df[col], utc=True, errors="coerce"))
            except Exception:
                continue
            if idx.notna().all():
                return idx
    return None


def _htf_frame(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    dt_index: pd.DatetimeIndex | None,
    bb_period: int,
    adx_period: int,
    atr_period: int,
) -> pd.DataFrame:
    """计算 4h 方向门指标；始终只引用最近一根已收盘 4h bar（无未来函数）。"""
    if dt_index is not None:
        tmp = pd.DataFrame({"high": high.values, "low": low.values, "close": close.values}, index=dt_index)
        agg = tmp.resample("4h", label="right", closed="right").agg(
            {"high": "max", "low": "min", "close": "last"}
        ).dropna(how="any")
        if len(agg) >= 2:
            htf = pd.DataFrame(index=agg.index)
            htf["bb4h_mid"] = agg["close"].rolling(window=max(int(bb_period), 1), min_periods=1).mean()
            htf["atr_4h"] = _wilder_atr(agg["high"], agg["low"], agg["close"], atr_period)
            htf["adx_4h"] = _wilder_adx(agg["high"], agg["low"], agg["close"], adx_period)
            htf["close_4h"] = agg["close"]
            aligned = htf.reindex(dt_index, method="ffill")
            aligned.index = close.index
            return aligned
    # 回退方案：无可用时间索引时，用 4 倍窗口在 1h 序列上近似 4h 指标
    scale = 4
    fallback = pd.DataFrame(index=close.index)
    fallback["bb4h_mid"] = close.rolling(window=max(int(bb_period) * scale, 1), min_periods=1).mean()
    fallback["atr_4h"] = _wilder_atr(high, low, close, max(int(atr_period) * scale, 1))
    fallback["adx_4h"] = _wilder_adx(high, low, close, max(int(adx_period) * scale, 1))
    fallback["close_4h"] = close
    return fallback


def _compute_frame(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    p = parameters or {}

    def _i(key: str, default: int) -> int:
        try:
            return max(int(p.get(key, default)), 1)
        except Exception:
            return int(default)

    def _f(key: str, default: float) -> float:
        try:
            return float(p.get(key, default))
        except Exception:
            return float(default)

    bb_4h_period = _i("bb_4h_period", 20)
    adx4h_period = _i("adx4h_period", 14)
    lookback = _i("lookback_high_low_period", 20)
    atr_period = _i("atr_period", 14)
    bb_length = _i("bb_length", 20)
    bb_mult = _f("bb_mult", 2.0)
    kc_length = _i("kc_length", 20)
    kc_mult = _f("kc_mult", 1.5)
    squeeze_pct_window = _i("squeeze_pct_window", 100)
    vol_pct_window = _i("vol_pct_window", 200)
    er_period = _i("er_period", 10)
    mom_z_period = _i("mom_z_period", 10)
    ma_fast_period = _i("ma_fast_period", 20)
    ma_slow_period = _i("ma_slow_period", 50)
    dir_gate_buffer_atr = _f("dir_gate_buffer_atr", 0.0)
    squeeze_pct_threshold = _f("squeeze_pct_threshold", 0.20)

    result = df.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    high = pd.to_numeric(result["high"], errors="coerce")
    low = pd.to_numeric(result["low"], errors="coerce")
    if "volume" in result.columns:
        volume = pd.to_numeric(result["volume"], errors="coerce").fillna(0.0)
    else:
        volume = pd.Series(0.0, index=result.index)

    # --- 1h 趋势 / 波动 ---
    result["ma_fast"] = close.rolling(window=ma_fast_period, min_periods=1).mean()
    result["ma_slow"] = close.rolling(window=ma_slow_period, min_periods=1).mean()
    atr = _wilder_atr(high, low, close, atr_period)
    result["atr"] = atr
    result["atr_pct"] = atr / close.replace(0.0, np.nan)
    result["er"] = _efficiency_ratio(close, er_period)

    # --- Donchian 通道（只取前一根及更早，避免自身穿越） ---
    result["dc_high"] = high.shift(1).rolling(window=lookback, min_periods=1).max()
    result["dc_low"] = low.shift(1).rolling(window=lookback, min_periods=1).min()

    # --- 布林 / 肯特纳 与挤压 ---
    bb_mid = close.rolling(window=bb_length, min_periods=1).mean()
    bb_std = close.rolling(window=bb_length, min_periods=2).std(ddof=0)
    result["bb_mid"] = bb_mid
    result["bb_upper"] = bb_mid + bb_mult * bb_std
    result["bb_lower"] = bb_mid - bb_mult * bb_std
    kc_mid = close.ewm(span=kc_length, adjust=False).mean()
    result["kc_upper"] = kc_mid + kc_mult * atr
    result["kc_lower"] = kc_mid - kc_mult * atr
    bb_width = (result["bb_upper"] - result["bb_lower"]) / bb_mid.replace(0.0, np.nan)
    result["bb_width"] = bb_width
    bb_width_pct = bb_width.rolling(window=squeeze_pct_window, min_periods=max(squeeze_pct_window // 4, 5)).rank(pct=True)
    result["bb_width_pct"] = bb_width_pct
    squeeze_on = (bb_width_pct < squeeze_pct_threshold).astype(bool)
    result["squeeze_on"] = squeeze_on.astype(float)
    expanding = (bb_width > bb_width.shift(1)).fillna(False).astype(bool)
    prev_squeeze = squeeze_on.shift(1, fill_value=False).astype(bool)
    result["squeeze_release"] = (prev_squeeze & expanding).astype(float)

    # --- 量能滚动分位 ---
    result["volume_pct"] = volume.rolling(window=vol_pct_window, min_periods=max(vol_pct_window // 4, 5)).rank(pct=True)

    # --- 动量 z-score ---
    ret_n = close / close.replace(0.0, np.nan).shift(mom_z_period) - 1.0
    z_win = max(vol_pct_window // 2, 20)
    ret_mean = ret_n.rolling(window=z_win, min_periods=max(z_win // 4, 5)).mean()
    ret_std = ret_n.rolling(window=z_win, min_periods=max(z_win // 4, 5)).std(ddof=0)
    result["mom_z"] = (ret_n - ret_mean) / ret_std.replace(0.0, np.nan)

    # --- 4h 方向门（已收盘对齐） ---
    htf = _htf_frame(high, low, close, _datetime_index(result), bb_4h_period, adx4h_period, atr_period)
    result["bb4h_mid"] = htf["bb4h_mid"].astype(float)
    result["atr_4h"] = htf["atr_4h"].astype(float)
    result["adx_4h"] = htf["adx_4h"].astype(float)
    close_4h = htf["close_4h"].astype(float)
    buffer = dir_gate_buffer_atr * result["atr_4h"].fillna(0.0)
    result["long_gate"] = (close_4h > (result["bb4h_mid"] + buffer)).astype(bool).astype(float)
    result["short_gate"] = (close_4h < (result["bb4h_mid"] - buffer)).astype(bool).astype(float)

    numeric_cols = [
        "ma_fast", "ma_slow", "atr", "atr_pct", "er", "dc_high", "dc_low",
        "bb_mid", "bb_upper", "bb_lower", "kc_upper", "kc_lower", "bb_width",
        "bb_width_pct", "squeeze_on", "squeeze_release", "volume_pct", "mom_z",
        "bb4h_mid", "atr_4h", "adx_4h", "long_gate", "short_gate",
    ]
    for col in numeric_cols:
        result[col] = pd.to_numeric(result[col], errors="coerce").bfill().fillna(0.0)
    return result


def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return _compute_frame(df, parameters or {})


class BeatMtfSqueezeBreakoutStrategy(Strategy):
    def __init__(self, config: BeatMtfSqueezeBreakoutConfig) -> None:
        super().__init__(config)
        self.instrument_id = (
            config.instrument_id
            if isinstance(config.instrument_id, InstrumentId)
            else InstrumentId.from_str(str(config.instrument_id))
        )
        self.bar_type = (
            config.bar_type if isinstance(config.bar_type, BarType) else BarType.from_str(str(config.bar_type))
        )
        self.instrument = None
        self.bars: list[Bar] = []

        self.params: dict = {
            "bb_4h_period": int(config.bb_4h_period),
            "adx4h_period": int(config.adx4h_period),
            "adx4h_threshold": float(config.adx4h_threshold),
            "lookback_high_low_period": int(config.lookback_high_low_period),
            "atr_multiplier_entry": float(config.atr_multiplier_entry),
            "atr_period": int(config.atr_period),
            "bb_length": int(config.bb_length),
            "bb_mult": float(config.bb_mult),
            "kc_length": int(config.kc_length),
            "kc_mult": float(config.kc_mult),
            "squeeze_pct_window": int(config.squeeze_pct_window),
            "squeeze_pct_threshold": float(config.squeeze_pct_threshold),
            "vol_pct_window": int(config.vol_pct_window),
            "volume_pct_threshold": float(config.volume_pct_threshold),
            "er_period": int(config.er_period),
            "er_threshold": float(config.er_threshold),
            "mom_z_period": int(config.mom_z_period),
            "mom_z_threshold": float(config.mom_z_threshold),
            "ma_fast_period": int(config.ma_fast_period),
            "ma_slow_period": int(config.ma_slow_period),
            "max_atr_pct_no_entry": float(config.max_atr_pct_no_entry),
            "sl_atr_mult": float(config.sl_atr_mult),
            "trail_atr_mult": float(config.trail_atr_mult),
            "max_bars_in_trade": int(config.max_bars_in_trade),
            "risk_per_trade": float(config.risk_per_trade),
            "dir_gate_buffer_atr": float(config.dir_gate_buffer_atr),
            "max_add_times": int(config.max_add_times),
            "add_size_pct": float(config.add_size_pct),
            "max_total_exposure_pct": float(config.max_total_exposure_pct),
        }
        self.trade_size = Decimal(str(config.trade_size))

        # 持仓状态
        self.position_dir: int = 0
        self.entry_price: float = 0.0
        self.stop_price: float = 0.0
        self.initial_qty: float = 0.0
        self.add_count: int = 0
        self.bars_in_trade: int = 0
        self.max_favorable_atr: float = 0.0
        self.trail_active: bool = False
        self.pending_entry_dir: int = 0

    # ------------------------------------------------------------------ #
    def on_start(self) -> None:
        self.instrument = self.cache.instrument(self.instrument_id)
        if self.instrument is None:
            self.log.error(f"Instrument not found: {self.instrument_id}")
            return
        self.subscribe_bars(self.bar_type)

    def on_stop(self) -> None:
        try:
            self.unsubscribe_bars(self.bar_type)
        except Exception:  # pragma: no cover - defensive
            pass

    # ------------------------------------------------------------------ #
    def _warmup_bars(self) -> int:
        return int(
            max(
                self.params["ma_slow_period"],
                self.params["lookback_high_low_period"] + 2,
                self.params["squeeze_pct_window"] // 2,
                self.params["bb_4h_period"] * 4,
                self.params["adx4h_period"] * 8,
                60,
            )
            + 5
        )

    def _build_frame(self) -> pd.DataFrame | None:
        if len(self.bars) < self._warmup_bars():
            return None
        window = self.bars[-MAX_HISTORY_BARS:]
        idx = pd.DatetimeIndex([pd.Timestamp(b.ts_event, unit="ns", tz="UTC") for b in window])
        df = pd.DataFrame(
            {
                "open": [b.open.as_double() for b in window],
                "high": [b.high.as_double() for b in window],
                "low": [b.low.as_double() for b in window],
                "close": [b.close.as_double() for b in window],
                "volume": [b.volume.as_double() for b in window],
            },
            index=idx,
        )
        return _compute_frame(df, self.params)

    def _equity(self) -> float:
        try:
            eq = self.portfolio.equity(self.instrument_id.venue)
            if eq is not None:
                return float(eq)
        except Exception:
            pass
        try:
            account = self.portfolio.account(self.instrument_id.venue)
            if account is not None:
                bal = account.balance_total()
                if bal is not None:
                    return float(bal.as_double())
        except Exception:
            pass
        return 0.0

    def _risk_qty(self, atr: float, price: float, equity: float) -> float:
        sl_dist = self.params["sl_atr_mult"] * atr
        if sl_dist <= 0.0 or price <= 0.0 or equity <= 0.0:
            return 0.0
        qty = equity * self.params["risk_per_trade"] / sl_dist
        max_qty = equity * self.params["max_total_exposure_pct"] / price
        return max(min(qty, max_qty), 0.0)

    def _submit_market(self, side: OrderSide, qty_float: float) -> float:
        if self.instrument is None or qty_float <= 0.0:
            return 0.0
        try:
            qty: Quantity = self.instrument.make_qty(qty_float)
        except Exception:
            return 0.0
        if qty.as_double() <= 0.0:
            return 0.0
        order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=side,
            quantity=qty,
        )
        self.submit_order(order)
        return qty.as_double()

    def _reset_state(self) -> None:
        self.position_dir = 0
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.initial_qty = 0.0
        self.add_count = 0
        self.bars_in_trade = 0
        self.max_favorable_atr = 0.0
        self.trail_active = False

    # ------------------------------------------------------------------ #
    def on_bar(self, bar: Bar) -> None:
        self.bars.append(bar)
        if len(self.bars) > MAX_HISTORY_BARS + 50:
            self.bars = self.bars[-MAX_HISTORY_BARS:]
        if self.instrument is None:
            self.instrument = self.cache.instrument(self.instrument_id)
            if self.instrument is None:
                return

        frame = self._build_frame()
        if frame is None or len(frame) < 3:
            return

        row = frame.iloc[-1]
        prev = frame.iloc[-2]
        close = float(row["close"])
        atr = float(row["atr"])
        if atr <= 0.0 or close <= 0.0:
            return

        net_pos = 0.0
        try:
            net_pos = float(self.portfolio.net_position(self.instrument_id))
        except Exception:
            net_pos = 0.0
        is_flat = abs(net_pos) <= 1e-12

        if is_flat:
            if self.position_dir != 0:
                self._reset_state()
            self.pending_entry_dir = 0
        else:
            actual_dir = 1 if net_pos > 0 else -1
            if self.position_dir == 0:
                self.position_dir = actual_dir
                self.entry_price = close
                self.initial_qty = abs(net_pos)
                self.stop_price = (
                    close - self.params["sl_atr_mult"] * atr
                    if actual_dir > 0
                    else close + self.params["sl_atr_mult"] * atr
                )
            self.position_dir = actual_dir

        long_gate = bool(row["long_gate"] > 0.5)
        short_gate = bool(row["short_gate"] > 0.5)

        # ---------------- 出场（优先级：止损 > 方向门反转 > 追踪 > 时间止损） ---------------- #
        if not is_flat and self.position_dir != 0:
            self.bars_in_trade += 1
            direction = self.position_dir
            fav = (close - self.entry_price) * direction
            self.max_favorable_atr = max(self.max_favorable_atr, fav / atr if atr > 0 else 0.0)

            # 1. 初始/当前止损
            if (direction > 0 and float(bar.low.as_double()) <= self.stop_price) or (
                direction < 0 and float(bar.high.as_double()) >= self.stop_price
            ):
                self.close_all_positions(self.instrument_id)
                self._reset_state()
                return

            # 2. 4h 方向门反转 → 全平
            if (direction > 0 and short_gate) or (direction < 0 and long_gate):
                self.close_all_positions(self.instrument_id)
                self._reset_state()
                return

            # 3. ATR 追踪止损（浮盈 >= 1 ATR 后启动）
            if self.max_favorable_atr >= 1.0:
                self.trail_active = True
            if self.trail_active:
                trail = (
                    close - self.params["trail_atr_mult"] * atr
                    if direction > 0
                    else close + self.params["trail_atr_mult"] * atr
                )
                self.stop_price = max(self.stop_price, trail) if direction > 0 else min(self.stop_price, trail)

            # 4. 时间止损：max_bars_in_trade 内未达 1 ATR 浮盈
            if self.bars_in_trade >= self.params["max_bars_in_trade"] and self.max_favorable_atr < 1.0:
                self.close_all_positions(self.instrument_id)
                self._reset_state()
                return

            # 5. 金字塔加仓：浮盈 >= 1 ATR、加仓次数受限、止损上移至保本
            if (
                self.add_count < self.params["max_add_times"]
                and self.max_favorable_atr >= 1.0
                and self.initial_qty > 0.0
            ):
                equity = self._equity()
                add_qty = self.initial_qty * max(min(self.params["add_size_pct"], 0.5), 0.0)
                notional_cap = equity * self.params["max_total_exposure_pct"]
                if notional_cap > 0.0 and (abs(net_pos) + add_qty) * close <= notional_cap:
                    side = OrderSide.BUY if direction > 0 else OrderSide.SELL
                    filled = self._submit_market(side, add_qty)
                    if filled > 0.0:
                        self.add_count += 1
                        self.stop_price = (
                            max(self.stop_price, self.entry_price)
                            if direction > 0
                            else min(self.stop_price, self.entry_price)
                        )
            return

        # ---------------- 入场 ---------------- #
        if self.pending_entry_dir != 0:
            return

        # 硬性风控门
        if float(row["atr_pct"]) > self.params["max_atr_pct_no_entry"]:
            return
        if float(row["adx_4h"]) <= self.params["adx4h_threshold"]:
            return

        ma_fast = float(row["ma_fast"])
        ma_slow = float(row["ma_slow"])
        prev_ma_fast = float(prev["ma_fast"])
        prev_close = float(prev["close"])
        prev_high = float(prev["high"])
        prev_low = float(prev["low"])
        bar_open = float(bar.open.as_double())
        bar_move = close - prev_close
        vol_ok = float(row["volume_pct"]) >= self.params["volume_pct_threshold"]
        mom_ok = float(row["mom_z"]) >= self.params["mom_z_threshold"]
        mom_ok_short = float(row["mom_z"]) <= -self.params["mom_z_threshold"]
        er_ok = float(row["er"]) >= self.params["er_threshold"]
        entry_move = self.params["atr_multiplier_entry"] * atr

        long_signal = False
        short_signal = False

        if long_gate and FUNDING_RATE_CONST <= FUNDING_EXTREME_THRESHOLD:
            # A 突破
            breakout = close > float(row["dc_high"]) and bar_move > entry_move
            score = int(vol_ok) + int(mom_ok) + int(er_ok)
            branch_a = breakout and close > ma_fast > ma_slow and score >= 2
            # B 挤压释放
            branch_b = (
                float(row["squeeze_release"]) > 0.5
                and close > float(row["bb_upper"])
                and close > ma_fast
            )
            # C 回踩
            touched = prev_low <= ma_fast + 0.3 * atr
            branch_c = (
                ma_fast > ma_slow
                and ma_fast > prev_ma_fast
                and touched
                and close > prev_high
                and close > bar_open
            )
            long_signal = bool(branch_a or branch_b or branch_c)

        if short_gate and -FUNDING_RATE_CONST <= FUNDING_EXTREME_THRESHOLD:
            breakout_s = close < float(row["dc_low"]) and (-bar_move) > entry_move
            score_s = int(vol_ok) + int(mom_ok_short) + int(er_ok)
            branch_a_s = breakout_s and close < ma_fast < ma_slow and score_s >= 2
            branch_b_s = (
                float(row["squeeze_release"]) > 0.5
                and close < float(row["bb_lower"])
                and close < ma_fast
            )
            touched_s = prev_high >= ma_fast - 0.3 * atr
            branch_c_s = (
                ma_fast < ma_slow
                and ma_fast < prev_ma_fast
                and touched_s
                and close < prev_low
                and close < bar_open
            )
            short_signal = bool(branch_a_s or branch_b_s or branch_c_s)

        if long_signal == short_signal:
            return

        equity = self._equity()
        qty = self._risk_qty(atr, close, equity)
        if qty <= 0.0:
            qty = float(self.trade_size)
        side = OrderSide.BUY if long_signal else OrderSide.SELL
        filled = self._submit_market(side, qty)
        if filled <= 0.0:
            return

        direction = 1 if long_signal else -1
        self.pending_entry_dir = direction
        self.position_dir = direction
        self.entry_price = close
        self.initial_qty = filled
        self.add_count = 0
        self.bars_in_trade = 0
        self.max_favorable_atr = 0.0
        self.trail_active = False
        self.stop_price = (
            close - self.params["sl_atr_mult"] * atr
            if direction > 0
            else close + self.params["sl_atr_mult"] * atr
        )


STRATEGY_MANIFEST = StrategyManifest(
    slug="beat_mtf_squeeze_breakout",
    name="BEAT 多周期挤压突破趋势策略",
    description=(
        "1h 信号 / 4h 方向门的多空双向趋势策略：4h 中轨方向门 + 4h ADX 强度过滤，"
        "1h 侧融合 Donchian 突破（评分制）、布林挤压释放、MA20 回踩三条支路；"
        "出场为四层结构（初始 ATR 止损、4h 方向门反转全平、ATR 追踪止损、时间止损），"
        "仓位采用风险平价 qty = equity × risk_per_trade / (sl_atr_mult × ATR)，"
        "dir_gate_buffer_atr 默认关闭，资金费率按固定常量建模。"
    ),
    version="1.0.0",
    category="trend",
    strategy_path="app.strategies.beat_mtf_squeeze_breakout:BeatMtfSqueezeBreakoutStrategy",
    config_path="app.strategies.beat_mtf_squeeze_breakout:BeatMtfSqueezeBreakoutConfig",
    parameters={
        "bb_4h_period": ParameterSpec(title="4h方向门周期", type="integer", default=20, minimum=10, maximum=60),
        "adx4h_period": ParameterSpec(title="4h ADX周期", type="integer", default=14, minimum=5, maximum=40),
        "adx4h_threshold": ParameterSpec(title="4h ADX阈值", type="number", default=20.0, minimum=10.0, maximum=40.0),
        "lookback_high_low_period": ParameterSpec(title="Donchian回看周期", type="integer", default=20, minimum=8, maximum=80),
        "atr_multiplier_entry": ParameterSpec(title="突破动量ATR门槛", type="number", default=0.6, minimum=0.0, maximum=3.0),
        "atr_period": ParameterSpec(title="ATR周期", type="integer", default=14, minimum=5, maximum=40),
        "bb_length": ParameterSpec(title="布林周期", type="integer", default=20, minimum=10, maximum=60),
        "bb_mult": ParameterSpec(title="布林倍数", type="number", default=2.0, minimum=1.0, maximum=4.0),
        "kc_length": ParameterSpec(title="肯特纳周期", type="integer", default=20, minimum=10, maximum=60),
        "kc_mult": ParameterSpec(title="肯特纳倍数", type="number", default=1.5, minimum=0.5, maximum=3.0),
        "squeeze_pct_window": ParameterSpec(title="挤压分位窗口", type="integer", default=100, minimum=30, maximum=400),
        "squeeze_pct_threshold": ParameterSpec(title="挤压分位阈值", type="number", default=0.20, minimum=0.05, maximum=0.50),
        "vol_pct_window": ParameterSpec(title="量能分位窗口", type="integer", default=200, minimum=50, maximum=600),
        "volume_pct_threshold": ParameterSpec(title="量能分位阈值", type="number", default=0.75, minimum=0.3, maximum=0.99),
        "er_period": ParameterSpec(title="效率系数周期", type="integer", default=10, minimum=3, maximum=60),
        "er_threshold": ParameterSpec(title="效率系数阈值", type="number", default=0.35, minimum=0.0, maximum=1.0),
        "mom_z_period": ParameterSpec(title="动量Z周期", type="integer", default=10, minimum=2, maximum=60),
        "mom_z_threshold": ParameterSpec(title="动量Z阈值", type="number", default=0.5, minimum=0.0, maximum=3.0),
        "ma_fast_period": ParameterSpec(title="1h快均线", type="integer", default=20, minimum=5, maximum=60),
        "ma_slow_period": ParameterSpec(title="1h慢均线", type="integer", default=50, minimum=20, maximum=200),
        "max_atr_pct_no_entry": ParameterSpec(title="禁入波动上限", type="number", default=0.06, minimum=0.01, maximum=0.30),
        "sl_atr_mult": ParameterSpec(title="初始止损ATR倍数", type="number", default=2.2, minimum=1.0, maximum=5.0),
        "trail_atr_mult": ParameterSpec(title="追踪止损ATR倍数", type="number", default=2.8, minimum=1.0, maximum=6.0),
        "max_bars_in_trade": ParameterSpec(title="时间止损（1h根数）", type="integer", default=48, minimum=6, maximum=240),
        "risk_per_trade": ParameterSpec(title="单笔风险比例", type="number", default=0.008, minimum=0.001, maximum=0.05),
        "dir_gate_buffer_atr": ParameterSpec(title="方向门ATR缓冲", type="number", default=0.0, minimum=0.0, maximum=1.0),
        "max_add_times": ParameterSpec(title="最大加仓次数", type="integer", default=1, minimum=0, maximum=3),
        "add_size_pct": ParameterSpec(title="加仓比例", type="number", default=0.5, minimum=0.0, maximum=0.5),
        "max_total_exposure_pct": ParameterSpec(title="总敞口上限", type="number", default=1.0, minimum=0.1, maximum=2.0),
        "trade_size": ParameterSpec(title="备用交易数量", type="number", default=0.01, minimum=0.0001, maximum=100.0),
    },
    timeframes=("1h", "4h"),
    primary_timeframe="1h",
    plot_config={
        "main_plot": {
            "close": {"type": "line", "color": "#ffffff"},
            "ma_fast": {"type": "line", "color": "#ffaa00"},
            "ma_slow": {"type": "line", "color": "#00aaff"},
            "bb_upper": {"type": "line", "color": "#8888ff"},
            "bb_lower": {"type": "line", "color": "#8888ff"},
            "dc_high": {"type": "line", "color": "#55ff99"},
            "dc_low": {"type": "line", "color": "#ff7777"},
            "bb4h_mid": {"type": "line", "color": "#ffff55"},
        },
        "subplots": {
            "波动率": {
                "atr": {"type": "line", "color": "#ff55ff"},
                "atr_pct": {"type": "line", "color": "#ffaa55"},
            },
            "挤压": {
                "bb_width": {"type": "line", "color": "#55ffff"},
                "bb_width_pct": {"type": "line", "color": "#aa88ff"},
                "squeeze_release": {"type": "bar", "color": "#ffcc00"},
            },
            "趋势强度": {
                "adx_4h": {"type": "line", "color": "#00ddaa"},
                "er": {"type": "line", "color": "#dd8800"},
            },
            "量能与动量": {
                "volume_pct": {"type": "line", "color": "#88ff88"},
                "mom_z": {"type": "line", "color": "#ff8888"},
            },
        },
    },
    mode=StrategyMode.SINGLE_INSTRUMENT,
    supports_short=True,
    requires_funding=True,
)
