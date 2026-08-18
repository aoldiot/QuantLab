import numpy as np
import pandas as pd
import pytest

from app.quant.experiment import StrategyCandidate, run_vectorized_experiment


@pytest.fixture
def sample_df():
    dates = pd.date_range("2024-01-01", periods=150, freq="1h")
    trend = np.linspace(100, 140, 150)
    noise = np.random.normal(0, 0.5, 150)
    close = trend + noise
    return pd.DataFrame({
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": [1000.0] * 150,
    }, index=dates)


def test_run_vectorized_experiment(sample_df):
    res = run_vectorized_experiment(
        df=sample_df,
        factor_name="ema_spread",
        factor_params={"fast_period": 8, "slow_period": 20},
        threshold_long=0.0,
        allow_short=True,
    )

    assert res["ok"] is True
    assert "total_return_pct" in res
    assert "sharpe_ratio" in res
    assert "max_drawdown_pct" in res
    assert "win_rate_pct" in res
    assert "profit_factor" in res
    assert "equity_curve_preview" in res
    assert len(res["equity_curve_preview"]) > 0


def test_strategy_candidate_dataclass():
    cand = StrategyCandidate(
        strategy_name="btc_momentum",
        category="trend",
        hypothesis="Momentum anomaly on 1h timeframe",
        symbols=["BTCUSDT"],
        timeframe="1h",
        factors=[{"name": "momentum", "params": {"period": 14}}],
        entry_rules=["momentum > 0"],
        exit_rules=["momentum < 0"],
        risk_rules=["atr stop loss 2.0x"],
        parameters={"period": 14},
    )

    d = cand.to_dict()
    assert d["strategy_name"] == "btc_momentum"
    assert d["category"] == "trend"
    assert len(d["factors"]) == 1
