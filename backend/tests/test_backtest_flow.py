import asyncio
import pytest
from datetime import date
from app.config import settings
from app.runner import append_log, get_backtest_logs
from app.schemas import BacktestCreate, BacktestConfirmRequest, BacktestLogsOut


def test_logging_helpers(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "artifact_root", tmp_path)
    run_id = "test-run-123"
    append_log(run_id, "[2026-08-16 12:00:00] [INFO] 开始测试回测")
    append_log(run_id, "[2026-08-16 12:00:01] [WARN] 测试警告")
    logs = get_backtest_logs(run_id)
    assert "[INFO] 开始测试回测" in logs
    assert "[WARN] 测试警告" in logs


def test_backtest_schemas():
    data = BacktestCreate(
        name="测试回测",
        strategy_version_id="ver-1",
        strategy_parameters={"fast_period": 10, "slow_period": 20},
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 6, 1),
        initial_balance=10000.0,
        leverage=3.0,
        check_data_integrity=True,
    )
    assert data.ignore_missing_data is True
    assert data.check_data_integrity is True

    data2 = BacktestCreate(
        name="测试回测直接执行",
        strategy_version_id="ver-1",
        strategy_parameters={"fast_period": 10, "slow_period": 20},
        symbols=["BTCUSDT"],
        timeframes=["1h"],
        start_date=date(2024, 1, 1),
        end_date=date(2024, 6, 1),
        initial_balance=10000.0,
        leverage=3.0,
        check_data_integrity=False,
    )
    assert data2.check_data_integrity is False
    assert data2.ignore_missing_data is True

    confirm_req = BacktestConfirmRequest()
    assert confirm_req.ignore_missing_data is True

    logs_out = BacktestLogsOut(
        id="test-1",
        status="RUNNING",
        stage="正在检查数据完整性 (1/2): BTCUSDT 1h",
        progress=50,
        logs="[INFO] 正在检查 BTCUSDT 1h",
    )
    assert logs_out.progress == 50
    assert logs_out.logs == "[INFO] 正在检查 BTCUSDT 1h"


@pytest.mark.anyio
async def test_runner_update_function():
    from app.runner import _update, research_status_for_run
    from app.models import RunStatus, ResearchStatus

    assert research_status_for_run(RunStatus.QUEUED) == ResearchStatus.BACKTESTING
    assert research_status_for_run(RunStatus.RUNNING) == ResearchStatus.BACKTESTING
    assert research_status_for_run(RunStatus.ANALYZING) == ResearchStatus.BACKTESTING
    assert research_status_for_run(RunStatus.COMPLETED) == ResearchStatus.READY_FOR_ANALYSIS
    assert research_status_for_run(RunStatus.FAILED) == ResearchStatus.READY_FOR_BACKTEST

    # Test _update on non-existent run returns gracefully without error
    await _update("non-existent-run-id", progress=10, stage="测试")


def test_ensure_catalog_coverage_lenient(tmp_path):
    from nautilus_trader.config import BacktestDataConfig, BacktestRunConfig, BacktestVenueConfig, BacktestEngineConfig
    from nautilus_trader.model.data import Bar
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog
    from app.backtests.worker import ensure_catalog_coverage

    cat_path = tmp_path / "catalog"
    cat_path.mkdir()
    catalog = ParquetDataCatalog(str(cat_path))

    data = [
        BacktestDataConfig(
            catalog_path=str(cat_path),
            data_cls=Bar,
            instrument_ids=["NONEXISTENT-PERP.BINANCE"],
            bar_spec="1-HOUR-LAST",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-06-30T23:59:59Z",
        )
    ]
    venue = BacktestVenueConfig(
        name="BINANCE",
        oms_type="HEDGING",
        account_type="MARGIN",
        base_currency="USDT",
        starting_balances=["10000 USDT"],
    )
    run_config = BacktestRunConfig(
        venues=[venue],
        data=data,
        engine=BacktestEngineConfig(trader_id="TEST-001"),
    )

    # In lenient mode (ignore_missing=True), it should NOT raise ValueError
    missing = ensure_catalog_coverage(run_config, ignore_missing=True)
    assert len(missing) == 1
    assert "NONEXISTENT-PERP.BINANCE" in missing[0]

    # In strict mode (ignore_missing=False), it should raise ValueError
    with pytest.raises(ValueError, match="Catalog 数据不覆盖请求范围"):
        ensure_catalog_coverage(run_config, ignore_missing=False)


def test_scan_catalog_summary_pagination(tmp_path):
    from pathlib import Path
    from app.data_downloads import scan_catalog_summary, _calculate_days_span, _get_coverage_bucket_key, _compute_coverage_stats
    from app.config import settings
    import os

    cat_path = tmp_path / "catalog"
    bar_dir = cat_path / "data" / "bar"
    bar_dir.mkdir(parents=True)

    # Test on empty catalog
    res = scan_catalog_summary(cat_path, page=1, page_size=20)
    assert res["total_symbols"] == 0
    assert res["page"] == 1
    assert res["total_pages"] == 1
    assert res["items"] == []
    assert "coverage_stats" in res
    assert len(res["coverage_stats"]) == 5

    # Test helper functions
    assert _calculate_days_span("2023-01-01", "2023-01-31") == 31
    assert _get_coverage_bucket_key(1200) == "gte_3y"
    assert _get_coverage_bucket_key(500) == "1y_3y"
    assert _get_coverage_bucket_key(200) == "6m_1y"
    assert _get_coverage_bucket_key(50) == "1m_6m"
    assert _get_coverage_bucket_key(10) == "lt_1m"

    mock_symbols = [
        {"symbol": "BTCUSDT", "instrument_id": "BTCUSDT-PERP.BINANCE", "market_type": "um", "start_date": "2020-01-01", "end_date": "2024-01-01", "total_bars": 1000, "total_size_bytes": 5000, "timeframes": []},
        {"symbol": "ETHUSDT", "instrument_id": "ETHUSDT-PERP.BINANCE", "market_type": "um", "start_date": "2023-01-01", "end_date": "2023-12-31", "total_bars": 500, "total_size_bytes": 2500, "timeframes": []},
    ]
    stats = _compute_coverage_stats(mock_symbols)
    assert len(stats) == 5
    gte_3y = next(s for s in stats if s["key"] == "gte_3y")
    assert gte_3y["count"] == 1
    assert "BTCUSDT" in gte_3y["symbols"]

    # Test on real local catalog if it exists
    real_cat = Path(settings.catalog_path).resolve()
    if real_cat.exists():
        summary = scan_catalog_summary(real_cat, page=1, page_size=2)
        assert "total_symbols" in summary
        assert "items" in summary
        assert "coverage_stats" in summary
        assert len(summary["items"]) <= 2
        assert summary["page"] == 1
        assert summary["page_size"] == 2

        filtered_summary = scan_catalog_summary(real_cat, duration_bucket="gte_3y", page=1, page_size=10)
        assert "items" in filtered_summary




