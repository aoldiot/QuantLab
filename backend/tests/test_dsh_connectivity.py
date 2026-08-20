from types import SimpleNamespace

import pytest

from app.dsh import engine


@pytest.mark.anyio
async def test_connectivity_test_surfaces_runtime_transport_error(monkeypatch, tmp_path):
    class FakeHarness:
        def __init__(self, **kwargs):
            pass

        def run(self, prompt, session_id):
            assert session_id.startswith("dsh_connectivity_test_")
            notification = SimpleNamespace(
                payload={
                    "event": {
                        "data": {
                            "reason": {
                                "kind": "error",
                                "error": {
                                    "message": "DeepSeek API request failed",
                                    "code": "TRANSPORT",
                                },
                            }
                        }
                    }
                }
            )
            return SimpleNamespace(
                final_response="",
                finish_reason="error",
                notifications=[notification],
            )

        def close(self):
            pass

    fake_sdk = SimpleNamespace(DeepSeekHarness=FakeHarness)
    monkeypatch.setitem(__import__("sys").modules, "deepseek_harness", fake_sdk)
    monkeypatch.setattr(engine.settings, "data_root", tmp_path)

    ok, message = await engine.run_llm_connectivity_test(api_key="test-key")

    assert ok is False
    assert message == "DSH SDK 运行失败: DeepSeek API request failed (TRANSPORT)"
