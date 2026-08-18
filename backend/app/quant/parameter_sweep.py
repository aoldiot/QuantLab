from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from .experiment import run_vectorized_experiment
from .market_data import load_market_bars


def run_parameter_sweep(
    factor_name: str = "ema_spread",
    param_grid: dict[str, list[Any]] | None = None,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    start_date: str | None = None,
    end_date: str | None = None,
    metric_target: str = "sharpe_ratio",
    catalog_path: str | None = None,
    max_combinations: int = 50,
) -> dict[str, Any]:
    """Perform a grid search parameter sweep across historical data."""
    df = load_market_bars(
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        catalog_path=catalog_path,
    )

    if df.empty or len(df) < 30:
        return {
            "ok": False,
            "error": f"标的 {symbol} 在指定区间内数据不足，无法执行参数扫描",
        }

    default_grid = {
        "fast_period": [8, 12, 16, 20],
        "slow_period": [21, 26, 35, 50],
    }
    grid = param_grid or default_grid

    keys = list(grid.keys())
    values = list(grid.values())
    raw_combinations = list(itertools.product(*values))

    # Cap combinations to prevent resource exhaustion
    if len(raw_combinations) > max_combinations:
        step = len(raw_combinations) // max_combinations
        raw_combinations = raw_combinations[::step][:max_combinations]

    results = []

    for comb in raw_combinations:
        p_dict = dict(zip(keys, comb))
        # Filter invalid fast >= slow cases
        if "fast_period" in p_dict and "slow_period" in p_dict and p_dict["fast_period"] >= p_dict["slow_period"]:
            continue

        sim_res = run_vectorized_experiment(
            df=df,
            factor_name=factor_name,
            factor_params=p_dict,
        )

        if sim_res.get("ok"):
            results.append({
                "params": p_dict,
                "total_return_pct": sim_res["total_return_pct"],
                "sharpe_ratio": sim_res["sharpe_ratio"],
                "max_drawdown_pct": sim_res["max_drawdown_pct"],
                "win_rate_pct": sim_res["win_rate_pct"],
                "profit_factor": sim_res["profit_factor"],
                "total_trades": sim_res["total_trades"],
            })

    if not results:
        return {
            "ok": False,
            "error": "未产生有效的参数组合评估结果",
        }

    # Rank by target metric
    sort_key = metric_target if metric_target in results[0] else "sharpe_ratio"
    results.sort(key=lambda item: item.get(sort_key, 0.0), reverse=True)

    best_result = results[0]
    metric_values = [r.get(sort_key, 0.0) for r in results]
    mean_val = float(np.mean(metric_values))
    std_val = float(np.std(metric_values))
    cv = (std_val / (abs(mean_val) + 1e-9)) if mean_val != 0 else 1.0

    # Sensitivity score: lower CV means more robust parameter plateau (less sensitive to curve fitting)
    is_stable = cv < 0.8 and mean_val > 0.5

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "total_combinations_tested": len(results),
        "best_parameters": best_result["params"],
        "best_metrics": {
            "sharpe_ratio": best_result["sharpe_ratio"],
            "total_return_pct": best_result["total_return_pct"],
            "max_drawdown_pct": best_result["max_drawdown_pct"],
            "win_rate_pct": best_result["win_rate_pct"],
        },
        "parameter_stability": {
            "mean_sharpe": round(mean_val, 3),
            "std_sharpe": round(std_val, 3),
            "coefficient_of_variation": round(cv, 3),
            "is_parameter_plateau": is_stable,
            "overfitting_risk": "LOW" if is_stable else ("MODERATE" if cv < 1.5 else "HIGH_CURVE_FITTING"),
        },
        "top_combinations": results[:10],
    }
