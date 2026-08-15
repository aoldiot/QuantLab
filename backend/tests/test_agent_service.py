from app.agent.service import _normalize_backtest_payload, _safe_bash, _strip_control_markers
from app.llm_config import decrypt_api_key, encrypt_api_key, mask_api_key


def test_api_key_round_trip_and_masking():
    encrypted = encrypt_api_key("sk-ant-secret-1234")
    assert "sk-ant-secret-1234" not in encrypted
    assert decrypt_api_key(encrypted) == "sk-ant-secret-1234"
    assert mask_api_key("sk-ant-secret-1234") == "sk-a••••••••1234"


def test_bash_allowlist():
    assert _safe_bash("pytest tests/test_strategy_contract.py")
    assert _safe_bash("uv add pandas")
    assert _safe_bash("git diff -- backend/app/strategies/foo.py")
    assert not _safe_bash("git push origin main")
    assert not _safe_bash("rm -rf backend")
    assert not _safe_bash("python -m app.backtests.worker")


def test_normalizes_agent_execution_model_alias():
    normalized = _normalize_backtest_payload({"execution_model": "market", "start_date": "2024-01-01T00:00:00Z"})
    assert normalized["execution_model"] == "STANDARD"
    assert normalized["start_date"] == "2024-01-01"
    assert _normalize_backtest_payload({"execution_model": "fast"})["execution_model"] == "FAST"


def test_hides_backtest_control_marker_from_conversation():
    event = {"content": [{"text": '完成。 QUANTLAB_BACKTEST_REQUEST:{"execution_model":"FAST"}'}]}
    assert _strip_control_markers(event) == {"content": [{"text": "完成。"}]}
