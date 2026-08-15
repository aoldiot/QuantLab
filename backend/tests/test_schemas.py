from datetime import date
import pytest
from pydantic import ValidationError
from app.schemas import BacktestCreate, GitCommitCreate


def test_rejects_invalid_date_range():
    with pytest.raises(ValidationError):
        BacktestCreate(name="x", strategy_version_id="v", strategy_parameters={}, symbols=["BTCUSDT"],
            timeframes=["15m"], start_date=date(2025, 1, 2), end_date=date(2025, 1, 1), initial_balance=10000, leverage=4)


def test_git_commit_accepts_short_message():
    assert GitCommitCreate(message="修").message == "修"
