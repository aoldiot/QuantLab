from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from .experiment import run_vectorized_experiment
from .market_data import load_market_bars


def calculate_deflated_sharpe_ratio(
    estimated_sharpe: float,
    num_trials: int = 10,
    sample_skewness: float = 0.0,
    sample_kurtosis: float = 3.0,
    sample_length: int = 250,
    benchmark_sharpe: float = 0.0,
) -> float:
    """Calculate the Deflated Sharpe Ratio (DSR) by Marcos Lopez de Prado.

    Tests the probability that the estimated Sharpe ratio exceeds zero after adjusting
    for skewness, fat tails (kurtosis), and multiple testing (selection bias).
    """
    if sample_length <= 2 or num_trials <= 0:
        return 0.0

    # Euler-Mascheroni constant
    euler_mascheroni = 0.57721566490153286

    # Expected maximum Sharpe ratio among N independent trials under the null
    if num_trials > 1:
        z_approx = (
            (1.0 - euler_mascheroni) * stats.norm.ppf(1.0 - 1.0 / num_trials)
            + euler_mascheroni * stats.norm.ppf(1.0 - 1.0 / (num_trials * math.e))
        )
        expected_max_sharpe = max(0.0, float(z_approx / math.sqrt(sample_length)))
    else:
        expected_max_sharpe = benchmark_sharpe

    # Asymptotic variance of the Sharpe ratio estimator
    sr_var = (
        1.0
        - sample_skewness * estimated_sharpe
        + ((sample_kurtosis - 1.0) / 4.0) * (estimated_sharpe**2)
    ) / max(1, sample_length - 1)

    if sr_var <= 0:
        return 0.0

    sr_std = math.sqrt(sr_var)
    z_stat = (estimated_sharpe - expected_max_sharpe) / (sr_std + 1e-9)
    dsr = float(stats.norm.cdf(z_stat))
    return round(float(np.clip(dsr, 0.0, 1.0)), 4)


def run_walk_forward_analysis(
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    factor_name: str = "ema_spread",
    factor_params: dict[str, Any] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    n_splits: int = 4,
    train_ratio: float = 0.7,
    catalog_path: str | None = None,
) -> dict[str, Any]:
    """Perform Walk-Forward Out-of-Sample (OOS) robustness analysis."""
    df = load_market_bars(
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        catalog_path=catalog_path,
    )

    if df.empty or len(df) < 100:
        return {
            "ok": False,
            "error": "历史数据不足以进行 Walk-Forward 切分（至少需要 100 根 Bar）",
        }

    total_len = len(df)
    window_size = total_len // n_splits
    train_size = int(window_size * train_ratio)
    test_size = window_size - train_size

    if train_size < 20 or test_size < 10:
        return {
            "ok": False,
            "error": "单个切片样本量过小，请减少切分段数或扩大时间跨度",
        }

    windows = []
    is_sharpes = []
    oos_sharpes = []

    for i in range(n_splits):
        start_idx = i * window_size
        train_df = df.iloc[start_idx : start_idx + train_size]
        test_df = df.iloc[start_idx + train_size : start_idx + window_size]

        if len(train_df) < 15 or len(test_df) < 10:
            continue

        res_is = run_vectorized_experiment(train_df, factor_name=factor_name, factor_params=factor_params)
        res_oos = run_vectorized_experiment(test_df, factor_name=factor_name, factor_params=factor_params)

        is_sr = res_is.get("sharpe_ratio", 0.0)
        oos_sr = res_oos.get("sharpe_ratio", 0.0)

        is_sharpes.append(is_sr)
        oos_sharpes.append(oos_sr)

        windows.append({
            "split_index": i + 1,
            "is_start": str(train_df.index[0]),
            "is_end": str(train_df.index[-1]),
            "is_sharpe": is_sr,
            "is_return_pct": res_is.get("total_return_pct", 0.0),
            "oos_start": str(test_df.index[0]),
            "oos_end": str(test_df.index[-1]),
            "oos_sharpe": oos_sr,
            "oos_return_pct": res_oos.get("total_return_pct", 0.0),
            "oos_max_drawdown_pct": res_oos.get("max_drawdown_pct", 0.0),
            "passed_oos": bool(oos_sr > 0),
        })

    avg_is_sharpe = float(np.mean(is_sharpes)) if is_sharpes else 0.0
    avg_oos_sharpe = float(np.mean(oos_sharpes)) if oos_sharpes else 0.0
    wfe = (avg_oos_sharpe / (avg_is_sharpe + 1e-9)) if avg_is_sharpe > 0 else 0.0
    positive_oos_ratio = float(np.mean([1.0 if s > 0 else 0.0 for s in oos_sharpes])) if oos_sharpes else 0.0

    is_robust = wfe >= 0.50 and positive_oos_ratio >= 0.60

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "n_splits": len(windows),
        "avg_in_sample_sharpe": round(avg_is_sharpe, 3),
        "avg_out_of_sample_sharpe": round(avg_oos_sharpe, 3),
        "walk_forward_efficiency": round(wfe, 3),
        "positive_oos_ratio": round(positive_oos_ratio * 100, 1),
        "is_robust": is_robust,
        "verdict": "ROBUST_OOS_CONSISTENT" if is_robust and wfe >= 0.7 else ("ACCEPTABLE_OOS" if is_robust else "OVERFITTING_DEGRADATION"),
        "splits": windows,
    }


def run_monte_carlo_stress_test(
    trade_returns: list[float] | None = None,
    df: pd.DataFrame | None = None,
    factor_name: str = "ema_spread",
    factor_params: dict[str, Any] | None = None,
    n_simulations: int = 1000,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Perform Monte Carlo trade resampling and stress test."""
    returns = trade_returns or []

    if not returns and df is not None and not df.empty:
        # Run base experiment to extract return series
        base_exp = run_vectorized_experiment(df, factor_name=factor_name, factor_params=factor_params)
        if base_exp.get("ok"):
            close = df["close"].astype(float)
            returns = list(close.pct_change().dropna().values)

    if not returns or len(returns) < 10:
        return {
            "ok": False,
            "error": "收益率序列不足以进行 Monte Carlo 模拟（至少需要 10 个数据点）",
        }

    ret_arr = np.array(returns, dtype=float)
    n_samples = len(ret_arr)
    sim_max_drawdowns = []
    sim_final_returns = []
    sim_sharpes = []

    ann_factor = math.sqrt(365 * 24)

    np.random.seed(42)
    for _ in range(n_simulations):
        sampled = np.random.choice(ret_arr, size=n_samples, replace=True)
        equity = np.cumprod(1.0 + sampled)
        peak = np.maximum.accumulate(equity)
        dd = (equity - peak) / (peak + 1e-9)
        max_dd = float(np.abs(np.min(dd)))

        total_ret = float(equity[-1] - 1.0)
        std = float(np.std(sampled))
        sharpe = float((np.mean(sampled) / (std + 1e-9)) * ann_factor) if std > 0 else 0.0

        sim_max_drawdowns.append(max_dd)
        sim_final_returns.append(total_ret)
        sim_sharpes.append(sharpe)

    sim_max_drawdowns.sort()
    sim_final_returns.sort()
    sim_sharpes.sort()

    var_idx = int(n_simulations * (1.0 - confidence_level))
    worst_drawdown_ci = float(sim_max_drawdowns[int(n_simulations * confidence_level)])
    var_return_ci = float(sim_final_returns[var_idx])
    median_sharpe = float(np.median(sim_sharpes))

    ruin_probability = float(np.mean([1.0 if dd > 0.50 else 0.0 for dd in sim_max_drawdowns]))

    # Deflated Sharpe Ratio
    skew = float(stats.skew(ret_arr)) if len(ret_arr) >= 3 else 0.0
    kurt = float(stats.kurtosis(ret_arr, fisher=False)) if len(ret_arr) >= 4 else 3.0
    dsr = calculate_deflated_sharpe_ratio(
        estimated_sharpe=median_sharpe,
        num_trials=20,
        sample_skewness=skew,
        sample_kurtosis=kurt,
        sample_length=len(ret_arr),
    )

    is_stress_tested = worst_drawdown_ci < 0.35 and ruin_probability == 0.0

    return {
        "ok": True,
        "n_simulations": n_simulations,
        "confidence_level_pct": int(confidence_level * 100),
        "var_worst_drawdown_pct": round(worst_drawdown_ci * 100, 2),
        "var_return_pct": round(var_return_ci * 100, 2),
        "median_sharpe": round(median_sharpe, 3),
        "ruin_probability_pct": round(ruin_probability * 100, 2),
        "deflated_sharpe_ratio": dsr,
        "is_stress_passed": is_stress_tested,
        "verdict": "PASSED_STRESS_TEST" if is_stress_tested and dsr >= 0.8 else ("MODERATE_STRESS_RESILIENCE" if is_stress_tested else "HIGH_RUIN_OR_DRAWDOWN_RISK"),
    }
