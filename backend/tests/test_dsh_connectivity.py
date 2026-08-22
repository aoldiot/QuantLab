import queue
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.dsh import engine


def test_quantlab_tools_load_without_research_phase():
    runtime = Path(__file__).parents[1] / "dsh_runtime"
    script = """
      const plugin = await import('./src/quantlab-tools.mjs')
      const registered = []
      plugin.apply({
        tools: { register(tool) { registered.push(tool.name) } },
        systemPrompt: { section() {} },
      })
      console.log(JSON.stringify(registered))
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=runtime,
        env={"PATH": __import__("os").environ["PATH"]},
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "[]"


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

    ok, message = await engine.run_llm_connectivity_test(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
    )

    assert ok is False
    assert message == "DSH SDK 运行失败: DeepSeek API request failed (TRANSPORT)"


@pytest.mark.anyio
async def test_connectivity_accepts_completed_turn_when_sdk_run_hangs(monkeypatch, tmp_path):
    class Subscription:
        def __init__(self):
            self.items = queue.Queue()

        def next(self):
            return self.items.get()

        def close(self):
            pass

    class Client:
        def __init__(self):
            self.subscription = Subscription()

        def subscribe_session_notifications(self, session_id):
            self.session_id = session_id
            return self.subscription

    class FakeHarness:
        def __init__(self, **kwargs):
            self.client = Client()
            self.closed = threading.Event()

        def start(self):
            pass

        def run(self, prompt, session_id):
            events = [
                {
                    "type": "assistant/message",
                    "data": {"message": {"content": [{"type": "text", "text": "quantlab-ok"}]}},
                },
                {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
            ]
            for event in events:
                self.client.subscription.items.put(
                    SimpleNamespace(
                        method="session.event",
                        payload={"sessionId": session_id, "event": event},
                    )
                )
            self.closed.wait()
            raise RuntimeError("runtime closed after completed turn")

        def close(self):
            self.closed.set()

    fake_sdk = SimpleNamespace(DeepSeekHarness=FakeHarness)
    monkeypatch.setitem(__import__("sys").modules, "deepseek_harness", fake_sdk)
    monkeypatch.setattr(engine.settings, "data_root", tmp_path)

    ok, message = await engine.run_llm_connectivity_test(
        base_url="https://example.test/v1",
        api_key="test-key",
        model="test-model",
    )

    assert ok is True
    assert message == "quantlab-ok"
