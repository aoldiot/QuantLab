import pandas as pd
import pytest

from app.strategy_contract import calculate_plot_indicators, load_manifest, validate_parameters


def test_standard_strategy_manifest_and_defaults():
    manifest = load_manifest("app.strategies.macd_btc")
    values = validate_parameters(manifest, {"fast_period": 10})
    assert manifest.primary_timeframe == "1h"
    assert values["fast_period"] == 10
    assert values["slow_period"] == 26
    assert "fast_ema" in manifest.plot_config["main_plot"]


def test_rejects_unknown_strategy_parameter():
    manifest = load_manifest("app.strategies.macd_btc")
    with pytest.raises(ValueError, match="未知策略参数"):
        validate_parameters(manifest, {"not_exists": 1})


def test_calculates_every_configured_plot_column():
    frame = pd.DataFrame({"close": [100.0, 101.0, 99.0, 103.0]})
    result, config = calculate_plot_indicators(
        "app.strategies.macd_btc", frame,
        {"fast_period": 2, "slow_period": 3, "signal_period": 2},
    )
    configured = set(config["main_plot"])
    configured.update(column for pane in config["subplots"].values() for column in pane)
    assert configured <= set(result.columns)
    assert result["macd_histogram"].notna().all()
