import pytest

from app.dsh.tools import DSH_TOOL_DEFINITIONS, dispatch_dsh_tool_call


def test_dsh_tool_definitions():
    assert len(DSH_TOOL_DEFINITIONS) >= 8
    tool_names = {t["name"] for t in DSH_TOOL_DEFINITIONS}
    expected = {
        "quant_market_data_query",
        "quant_factor_analysis",
        "quant_run_experiment",
        "quant_save_strategy_code",
        "quant_preflight_verify",
        "quant_execute_backtest",
        "quant_parameter_sweep",
        "quant_robustness_test",
        "quant_get_strategy",
    }
    assert expected.issubset(tool_names)


@pytest.mark.anyio
async def test_dispatch_dsh_tool_market_data():
    res = await dispatch_dsh_tool_call("quant_market_data_query", {"action": "list_instruments"})
    assert res["ok"] is True
    assert "instruments" in res


@pytest.mark.anyio
async def test_dispatch_dsh_tool_factor_analysis():
    res = await dispatch_dsh_tool_call(
        "quant_factor_analysis",
        {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "factor_name": "ema_spread",
            "factor_params": {"fast_period": 12, "slow_period": 26},
        },
    )
    assert res["ok"] is True
    assert "factor_analysis" in res


@pytest.mark.anyio
async def test_dispatch_dsh_tool_experiment():
    res = await dispatch_dsh_tool_call(
        "quant_run_experiment",
        {
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "factor_name": "ema_spread",
        },
    )
    assert res["ok"] is True
    assert "experiment_result" in res


@pytest.mark.anyio
async def test_dispatch_dsh_unknown_tool():
    res = await dispatch_dsh_tool_call("unknown_tool_xyz", {})
    assert res["ok"] is False
    assert "未知" in res["error"]
