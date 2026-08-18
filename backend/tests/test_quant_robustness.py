import numpy as np

from app.quant.robustness import (
    calculate_deflated_sharpe_ratio,
    run_monte_carlo_stress_test,
)


def test_deflated_sharpe_ratio():
    dsr = calculate_deflated_sharpe_ratio(
        estimated_sharpe=2.0,
        num_trials=10,
        sample_skewness=-0.2,
        sample_kurtosis=3.5,
        sample_length=250,
    )
    assert 0.0 <= dsr <= 1.0
    assert dsr > 0.5


def test_monte_carlo_stress_test():
    np.random.seed(42)
    # 50 trade returns with mean positive return
    returns = list(np.random.normal(0.005, 0.02, 50))
    res = run_monte_carlo_stress_test(trade_returns=returns, n_simulations=100)

    assert res["ok"] is True
    assert "var_worst_drawdown_pct" in res
    assert "median_sharpe" in res
    assert "ruin_probability_pct" in res
    assert "deflated_sharpe_ratio" in res
    assert "verdict" in res
