from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


def compute_technical_factor(
    df: pd.DataFrame,
    factor_name: str,
    params: dict[str, Any] | None = None,
) -> pd.Series:
    """Compute technical alpha factor values on historical OHLCV data."""
    if df.empty or len(df) < 5:
        return pd.Series(index=df.index, dtype=float)

    params = params or {}
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float)

    name = factor_name.lower().strip()

    if name in ("momentum", "roc"):
        period = int(params.get("period", 14))
        return close.pct_change(period)

    elif name in ("ema_spread", "macd_diff"):
        fast = int(params.get("fast_period", 12))
        slow = int(params.get("slow_period", 26))
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        return (ema_fast - ema_slow) / close

    elif name in ("rsi", "relative_strength"):
        period = int(params.get("period", 14))
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
        avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
        rs = avg_gain / (avg_loss + 1e-9)
        rsi = 100 - (100 / (1 + rs))
        return (rsi - 50.0) / 50.0  # normalize to [-1, 1]

    elif name in ("atr_norm", "atr"):
        period = int(params.get("period", 14))
        prev_close = close.shift(1)
        tr1 = high - low
        tr2 = (high - prev_close).abs()
        tr3 = (low - prev_close).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr / close

    elif name in ("bollinger_pct_b", "bb_pct_b"):
        period = int(params.get("period", 20))
        nbdev = float(params.get("std_dev", 2.0))
        ma = close.rolling(window=period).mean()
        std = close.rolling(window=period).std()
        upper = ma + (nbdev * std)
        lower = ma - (nbdev * std)
        width = upper - lower
        pct_b = (close - lower) / (width + 1e-9)
        return pct_b - 0.5  # centered around 0

    elif name in ("macd_hist", "macd"):
        fast = int(params.get("fast_period", 12))
        slow = int(params.get("slow_period", 26))
        signal_p = int(params.get("signal_period", 9))
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=signal_p, adjust=False).mean()
        hist = macd - signal
        return hist / close

    elif name in ("volatility_ratio", "vol_ratio"):
        short_p = int(params.get("short_period", 5))
        long_p = int(params.get("long_period", 20))
        ret = close.pct_change()
        vol_short = ret.rolling(short_p).std()
        vol_long = ret.rolling(long_p).std()
        return vol_short / (vol_long + 1e-9) - 1.0

    elif name in ("volume_price_trend", "vpt"):
        period = int(params.get("period", 14))
        price_diff_pct = close.pct_change()
        vpt = (volume * price_diff_pct).rolling(period).sum()
        vol_sum = volume.rolling(period).sum() + 1e-9
        return vpt / vol_sum

    else:
        # Default simple momentum
        period = int(params.get("period", 14))
        return close.pct_change(period)


def evaluate_factor(
    df: pd.DataFrame,
    factor_series: pd.Series,
    forward_periods: list[int] | None = None,
    quantiles: int = 5,
) -> dict[str, Any]:
    """Perform Information Coefficient (IC) and Quantile Return analysis on a factor."""
    if df.empty or factor_series.empty or len(df) < 20:
        return {
            "ok": False,
            "error": "数据点过少，无法执行有效的因子统计检验",
        }

    forward_periods = forward_periods or [1, 5, 10, 20]
    close = df["close"].astype(float)
    factor = factor_series.astype(float).replace([np.inf, -np.inf], np.nan)

    # Align indices
    aligned_df = pd.DataFrame({"close": close, "factor": factor}).dropna()
    if len(aligned_df) < 20:
        return {"ok": False, "error": "因子有效数值过少"}

    horizon_results = {}
    best_ic = -999.0
    best_horizon = 1

    for period in forward_periods:
        if len(aligned_df) <= period + 10:
            continue

        fwd_ret = aligned_df["close"].pct_change(period).shift(-period)
        eval_data = pd.DataFrame({
            "factor": aligned_df["factor"],
            "forward_return": fwd_ret,
        }).dropna()

        if len(eval_data) < 20:
            continue

        f_vals = eval_data["factor"].values
        r_vals = eval_data["forward_return"].values

        # Pearson IC
        pearson_ic, _pearson_p = stats.pearsonr(f_vals, r_vals)
        # Spearman Rank IC
        spearman_ic, spearman_p = stats.spearmanr(f_vals, r_vals)

        # Rolling IC to calculate IC IR
        rolling_ic = (
            eval_data["factor"]
            .rolling(window=min(30, len(eval_data) // 2))
            .corr(eval_data["forward_return"])
            .dropna()
        )
        ic_std = float(rolling_ic.std()) if not rolling_ic.empty and rolling_ic.std() > 0 else 1.0
        ic_ir = float(spearman_ic / (ic_std + 1e-9))

        # Quantile Analysis
        try:
            eval_data["quantile"] = pd.qcut(
                eval_data["factor"].rank(method="first"),
                q=quantiles,
                labels=[f"Q{i+1}" for i in range(quantiles)],
            )
            quantile_returns = (
                eval_data.groupby("quantile", observed=False)["forward_return"]
                .mean()
                .to_dict()
            )
            q1_ret = float(quantile_returns.get("Q1", 0.0))
            qk_ret = float(quantile_returns.get(f"Q{quantiles}", 0.0))
            long_short_spread = qk_ret - q1_ret

            # Check Monotonicity
            import itertools
            q_vals = [float(quantile_returns[f"Q{i+1}"]) for i in range(quantiles)]
            is_monotonic = all(x <= y for x, y in itertools.pairwise(q_vals)) or all(
                x >= y for x, y in itertools.pairwise(q_vals)
            )
        except Exception:
            quantile_returns = {}
            long_short_spread = 0.0
            is_monotonic = False

        horizon_results[f"horizon_{period}b"] = {
            "period_bars": period,
            "pearson_ic": round(float(pearson_ic), 4),
            "spearman_rank_ic": round(float(spearman_ic), 4),
            "p_value": round(float(spearman_p), 6),
            "ic_ir": round(ic_ir, 3),
            "statistically_significant": bool(spearman_p < 0.05),
            "quantile_avg_returns_pct": {k: round(v * 100, 4) for k, v in quantile_returns.items()},
            "long_short_spread_pct": round(long_short_spread * 100, 4),
            "monotonic_quantiles": is_monotonic,
        }

        if abs(spearman_ic) > abs(best_ic):
            best_ic = spearman_ic
            best_horizon = period

    # Autocorrelation & turnover
    lag1_corr = float(aligned_df["factor"].autocorr(lag=1))

    # Overall verdict
    is_strong = any(
        abs(h["spearman_rank_ic"]) >= 0.03 and h["statistically_significant"]
        for h in horizon_results.values()
    )

    return {
        "ok": True,
        "sample_size": len(aligned_df),
        "factor_autocorrelation_lag1": round(lag1_corr, 4) if not math.isnan(lag1_corr) else 0.0,
        "best_horizon_bars": best_horizon,
        "best_rank_ic": round(float(best_ic), 4),
        "is_alpha_significant": is_strong,
        "verdict": "STRONG_ALPHA" if is_strong and abs(best_ic) >= 0.05 else ("MODERATE_ALPHA" if is_strong else "WEAK_OR_NO_ALPHA"),
        "horizons": horizon_results,
    }
