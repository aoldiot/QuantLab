import pandas as pd

from app.backtests.chart_data import load_chart


def test_load_chart_filters_symbol_and_normalizes_nanoseconds(tmp_path):
    pd.DataFrame([
        {"timestamp": 1_700_000_000_000, "symbol": "BTC.X", "open": 1, "high": 3, "low": 1, "close": 2, "volume": 4},
        {"timestamp": 1_700_000_060_000, "symbol": "ETH.X", "open": 2, "high": 4, "low": 1, "close": 3, "volume": 5},
    ]).to_parquet(tmp_path / "bars.parquet")
    pd.DataFrame([
        {"timestamp": 1_700_000_000_000_000_000, "symbol": "BTC.X", "price": 2, "quantity": 1, "side": "BUY"},
    ]).to_parquet(tmp_path / "fills.parquet")
    result = load_chart(tmp_path, "BTC.X", None, None)
    assert result["symbols"] == ["BTC.X", "ETH.X"]
    assert result["bars"][0]["time"] == 1_700_000_000
    assert result["fills"][0]["time"] == 1_700_000_000


def test_load_chart_accepts_timestamp_fill_columns(tmp_path):
    pd.DataFrame([
        {"ts_init": 1_700_000_000_000_000_000, "symbol": "BTC.X", "open": "1", "high": "3", "low": "1", "close": "2", "volume": "4"},
    ]).to_parquet(tmp_path / "bars.parquet")
    pd.DataFrame([
        {"ts_event": pd.Timestamp("2023-11-14 22:13:20", tz="UTC"), "instrument_id": "BTC.X",
         "last_px": "2.0", "last_qty": "1.0", "order_side": "BUY"},
    ]).to_parquet(tmp_path / "fills.parquet")
    result = load_chart(tmp_path, "BTC.X", None, None)
    assert result["bars"][0]["time"] == 1_700_000_000
    assert result["fills"][0]["time"] == 1_700_000_000


def test_load_chart_selects_timeframe_and_removes_duplicate_times(tmp_path):
    pd.DataFrame([
        {"ts_init": 1_700_000_000_000_000_000, "symbol": "BTC.X", "bar_type": "BTC.X-1-MINUTE-LAST-EXTERNAL", "open": 1, "high": 2, "low": 1, "close": 2},
        {"ts_init": 1_700_000_000_000_000_000, "symbol": "BTC.X", "bar_type": "BTC.X-1-HOUR-LAST-EXTERNAL", "open": 2, "high": 3, "low": 2, "close": 3},
        {"ts_init": 1_700_003_600_000_000_000, "symbol": "BTC.X", "bar_type": "BTC.X-1-HOUR-LAST-EXTERNAL", "open": 3, "high": 4, "low": 3, "close": 4},
    ]).to_parquet(tmp_path / "bars.parquet")
    result = load_chart(tmp_path, "BTC.X", None, None, timeframe="1h")
    assert [bar["close"] for bar in result["bars"]] == [3, 4]
    assert len({bar["time"] for bar in result["bars"]}) == len(result["bars"])


def test_load_chart_returns_dynamic_plot_series(tmp_path):
    pd.DataFrame([
        {"ts_init": 1_700_000_000_000_000_000, "symbol": "BTC.X", "bar_type": "BTC.X-1-HOUR-LAST-EXTERNAL", "open": 1, "high": 2, "low": 1, "close": 2},
    ]).to_parquet(tmp_path / "bars.parquet")
    pd.DataFrame([
        {"ts_init": 1_700_000_000_000_000_000, "symbol": "BTC.X", "bar_type": "BTC.X-1-HOUR-LAST-EXTERNAL", "slow_macd": 0.25},
    ]).to_parquet(tmp_path / "indicators.parquet")
    (tmp_path / "plot_config.json").write_text(
        '{"main_plot": {}, "subplots": {"Custom": {"slow_macd": {"type": "line"}}}}',
        encoding="utf-8",
    )
    result = load_chart(tmp_path, "BTC.X", None, None, timeframe="1h")
    assert result["plot_config"]["subplots"]["Custom"]["slow_macd"]["type"] == "line"
    assert result["indicator_series"]["slow_macd"][0]["value"] == 0.25
