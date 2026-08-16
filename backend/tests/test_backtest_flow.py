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

    confirm_req = BacktestConfirmRequest(ignore_missing_data=True)
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
