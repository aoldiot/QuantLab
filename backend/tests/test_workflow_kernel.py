from app.dsh.profiles import PROFILES, worker_for_phase
from app.models import WorkerType
from app.workflow.error_router import classify_error


def test_legacy_code_phases_share_one_coding_worker():
    assert worker_for_phase("IMPLEMENTATION") == WorkerType.CODING
    assert worker_for_phase("REPAIR") == WorkerType.CODING
    assert worker_for_phase("FIX_ERROR") == WorkerType.CODING
    assert PROFILES[WorkerType.CODING].retry_budget == 3


def test_error_router_does_not_send_everything_to_repair():
    assert classify_error("ParameterSpec required").auto_fix is True
    assert classify_error("catalog missing data").worker == WorkerType.BACKTEST
    assert classify_error("Sharpe poor performance").worker == WorkerType.ANALYSIS
    assert classify_error("SyntaxError line 12").worker == WorkerType.CODING
