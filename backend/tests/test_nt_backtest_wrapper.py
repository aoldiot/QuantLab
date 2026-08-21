from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from nautilus_trader.backtest.config import FillModelFactory
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

from app.backtests.analytics import fixed_funding_cost, native_metrics
from app.backtests.builder import (
    FIXED_EXECUTION_MODEL,
    _execution_fill_model,
    inclusive_day_end,
    instrument_id,
)
from app.backtests.coverage import date_bounds, query_coverage
from app.data_downloads import make_instrument

BINANCE_INFO = {
    "symbol": "BTCUSDT",
    "baseAsset": "BTC",
    "quoteAsset": "USDT",
    "marginAsset": "USDT",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {
            "filterType": "LOT_SIZE",
            "stepSize": "0.001",
            "minQty": "0.001",
            "maxQty": "1000",
        },
    ],
}

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_downloaded_instruments_have_nonzero_native_trading_terms():
    perpetual = make_instrument(BINANCE_INFO, "um")
    spot = make_instrument(BINANCE_INFO, "spot")

    assert perpetual.maker_fee > 0
    assert perpetual.taker_fee > 0
    assert perpetual.margin_init > 0
    assert perpetual.margin_maint > 0
    assert spot.maker_fee > 0
    assert spot.taker_fee > 0


def test_framework_uses_one_fixed_nt_fill_model():
    config = _execution_fill_model(FIXED_EXECUTION_MODEL)

    assert FIXED_EXECUTION_MODEL == "CONSERVATIVE"
    assert type(FillModelFactory.create(config)).__name__ == "OneTickSlippageFillModel"


def test_market_type_and_final_day_bound_are_explicit():
    assert instrument_id("BTCUSDT", "BINANCE", "um") == "BTCUSDT-PERP.BINANCE"
    assert instrument_id("BTCUSDT", "BINANCE", "spot") == "BTCUSDT.BINANCE"
    assert inclusive_day_end("2024-02-02") == "2024-02-02T23:59:59.999999999Z"


def test_public_catalog_coverage_detects_internal_bar_gap(tmp_path):
    instrument = make_instrument(BINANCE_INFO, "um")
    bar_type = BarType.from_str(f"{instrument.id}-1-HOUR-LAST-EXTERNAL")
    bars = [
        Bar.from_dict(
            {
                "bar_type": str(bar_type),
                "open": "100.0",
                "high": "101.0",
                "low": "99.0",
                "close": "100.0",
                "volume": "1.000",
                "ts_event": timestamp,
                "ts_init": timestamp,
            }
        )
        for timestamp in (3_599_999_000_000, 10_799_999_000_000)
    ]
    catalog = ParquetDataCatalog(str(tmp_path))
    catalog.write_data([instrument])
    catalog.write_data(bars)
    start_ns, end_ns = date_bounds(date(1970, 1, 1), date(1970, 1, 1))

    coverage = query_coverage(catalog, Bar, str(bar_type), start_ns, end_ns, "1h")

    assert coverage.complete is False
    assert coverage.expected_count == 24
    assert coverage.actual_count == 2
    assert coverage.missing_count == 22


def test_headline_metrics_are_directly_mapped_from_nt_result():
    result = SimpleNamespace(
        stats_pnls={"USDT": {"PnL% (total)": 12.5, "Win Rate": 0.4, "Profit Factor": 1.3}},
        stats_returns={"Sharpe Ratio (252 days)": 1.7, "Max Drawdown": -0.2},
        total_positions=8,
        total_orders=11,
        total_events=23,
    )

    metrics = native_metrics(result)

    assert metrics["total_return"] == 12.5
    assert metrics["sharpe"] == 1.7
    assert metrics["profit_factor"] == 1.3
    assert metrics["win_rate"] == 40.0
    assert metrics["max_drawdown"] == -0.2
    assert metrics["source"] == "NautilusTrader BacktestResult"


def test_wrapper_does_not_call_nt_private_catalog_selector():
    targets = [
        "app/backtests/worker.py",
        "app/main.py",
        "app/runner.py",
    ]
    for target in targets:
        assert "_query_files" not in (BACKEND_ROOT / target).read_text(encoding="utf-8")


def test_wrapper_does_not_invent_latency_or_force_close_positions():
    builder = (BACKEND_ROOT / "app/backtests/builder.py").read_text(encoding="utf-8")
    worker = (BACKEND_ROOT / "app/backtests/worker.py").read_text(encoding="utf-8")

    assert "latency_model=" not in builder
    assert "close_all_positions" not in worker
    assert "close_position" not in worker


def test_sandbox_copies_every_worker_backtest_dependency():
    runner = (BACKEND_ROOT / "app/runner.py").read_text(encoding="utf-8")

    assert '"backtests" / "coverage.py"' in runner


def test_fixed_funding_is_directional_and_uses_frozen_end_time():
    import pandas as pd

    positions = pd.DataFrame([
        {"ts_opened": "2024-01-01T01:00:00Z", "ts_closed": None, "quantity": "2", "avg_px_open": "100", "side": "LONG"},
        {"ts_opened": "2024-01-01T01:00:00Z", "ts_closed": "2024-01-01T09:00:00Z", "quantity": "1", "avg_px_open": "100", "side": "SHORT"},
    ])
    cost, settlements = fixed_funding_cost(positions, {
        "enabled": True, "rate_per_8h": 0.0001, "settlement_hours_utc": [8],
        "end_time": "2024-01-01T09:00:00Z",
    })

    assert settlements == 2
    assert cost == -0.01
