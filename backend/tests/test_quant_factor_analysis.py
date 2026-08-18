import numpy as np
import pandas as pd
import pytest

from app.quant.factor_analysis import compute_technical_factor, evaluate_factor


@pytest.fixture
def sample_market_df():
    np.random.seed(42)
    dates = pd.date_range("2024-01-01", periods=200, freq="1h")
    # Upward trending series with noise
    trend = np.linspace(100, 150, 200)
    noise = np.random.normal(0, 1, 200)
    close = trend + noise
    high = close + np.random.uniform(0.5, 2.0, 200)
    low = close - np.random.uniform(0.5, 2.0, 200)
    open_p = close + np.random.normal(0, 0.5, 200)
    volume = np.random.uniform(500, 2000, 200)

    return pd.DataFrame({
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }, index=dates)


def test_compute_technical_factors(sample_market_df):
    for f_name in ("momentum", "ema_spread", "rsi", "atr", "bollinger_pct_b", "macd_hist", "volatility_ratio", "volume_price_trend"):
        res = compute_technical_factor(sample_market_df, f_name)
        assert isinstance(res, pd.Series)
        assert len(res) == len(sample_market_df)
        valid_vals = res.dropna()
        assert len(valid_vals) > 100


def test_evaluate_factor(sample_market_df):
    factor = compute_technical_factor(sample_market_df, "momentum", {"period": 10})
    eval_res = evaluate_factor(sample_market_df, factor, forward_periods=[1, 5, 10], quantiles=5)

    assert eval_res["ok"] is True
    assert "best_horizon_bars" in eval_res
    assert "best_rank_ic" in eval_res
    assert "verdict" in eval_res
    assert "horizon_1b" in eval_res["horizons"]
    h1 = eval_res["horizons"]["horizon_1b"]
    assert "spearman_rank_ic" in h1
    assert "p_value" in h1
    assert "quantile_avg_returns_pct" in h1
