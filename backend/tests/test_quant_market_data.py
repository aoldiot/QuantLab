import pandas as pd

from app.quant.market_data import (
    compute_market_stats,
    get_catalog_instruments,
    load_market_bars,
)


def test_get_catalog_instruments():
    instruments = get_catalog_instruments()
    assert isinstance(instruments, list)
    if instruments:
        inst = instruments[0]
        assert "symbol" in inst
        assert "timeframes" in inst
        assert "instrument_id" in inst


def test_load_market_bars_and_stats():
    # Load sample bars from catalog or fallback
    df = load_market_bars("BTCUSDT", "1h")
    assert isinstance(df, pd.DataFrame)
    if not df.empty:
        assert "open" in df.columns
        assert "high" in df.columns
        assert "low" in df.columns
        assert "close" in df.columns
        assert "volume" in df.columns
        stats = compute_market_stats(df)
        assert "total_bars" in stats
        assert "annualized_volatility_pct" in stats
        assert "total_return_pct" in stats


def test_compute_market_stats_synthetic():
    dates = pd.date_range("2024-01-01", periods=100, freq="1h")
    df = pd.DataFrame({
        "open": [100.0 + i for i in range(100)],
        "high": [101.0 + i for i in range(100)],
        "low": [99.0 + i for i in range(100)],
        "close": [100.5 + i for i in range(100)],
        "volume": [1000.0 for _ in range(100)],
    }, index=dates)

    stats = compute_market_stats(df)
    assert stats["total_bars"] == 100
    assert stats["start_close"] == 100.5
    assert stats["end_close"] == 199.5
    assert stats["total_return_pct"] > 90.0
