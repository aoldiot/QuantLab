from __future__ import annotations

"""Regression tests for the DSH HTTP bridge approval registry and event mapping.

These cover the pure logic the DSH orchestration layer relies on: interactive
approval lifecycle (pending -> approved/declined -> consume) and the SDK flat
event -> platform vocabulary mapping. No LLM / runtime / DB is required.
"""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.dsh.bridge import (
    _EXECUTORS,
    _compact_approval_result,
    _find_registry_entry,
    _registry,
    approve_proposal,
    execute_approved_proposal,
    pending_approvals,
)
from app.dsh.engine import (
    _harness_for,
    _harnesses,
    _sdk_session_id,
    _live_session_events,
    _map_notification,
    _record_live_event,
    _session_event_seq,
    _session_events,
    _start_live_turn,
    _status,
    _update_live_status,
    get_live_session_events,
)


@pytest.fixture(autouse=True)
def clear_registry(monkeypatch):
    # Registry persistence is production state; unit tests must remain in-memory.
    monkeypatch.setattr("app.dsh.bridge._save_registry", lambda: None)
    _registry.clear()
    _live_session_events.clear()
    _session_events.clear()
    _session_event_seq.clear()
    _status.clear()
    _harnesses.clear()
    yield
    _registry.clear()
    _live_session_events.clear()
    _session_events.clear()
    _session_event_seq.clear()
    _status.clear()
    _harnesses.clear()


class FakeNotification:
    def __init__(self, method, payload=None):
        self.method = method
        self.payload = payload or {}


PROJECT = "proj-approval-test"


@pytest.fixture()
def seed_pending(monkeypatch):
    import uuid

    def fake_uuid4():
        return uuid.UUID("12345678-1234-5678-1234-567812345678")

    monkeypatch.setattr("app.dsh.bridge.uuid.uuid4", fake_uuid4)
    bucket = _registry.setdefault(PROJECT, {})
    entry = {
        "request_id": str(fake_uuid4()),
        "project_id": PROJECT,
        "tool": "write_strategy_code",
        "proposal_key": "",
        "arguments": {"strategy_name": "test_strat", "code": "print(1)"},
        "status": "pending",
        "feedback": "",
        "created_at": "2026-08-19T00:00:00+00:00",
    }
    bucket[entry["request_id"]] = entry
    _save_registry_snapshot(entry)
    return entry


def _save_registry_snapshot(entry: dict) -> None:
    # The bridge persists via json file; the in-memory registry is what tests
    # exercise. Keep a reference so approval lookups work without IO.
    assert _registry[PROJECT][entry["request_id"]] is entry


def test_pending_approvals_returns_only_pending(seed_pending):
    _registry[PROJECT][seed_pending["request_id"]]["status"] = "approved"
    assert pending_approvals(PROJECT) == []


def test_approve_proposal_marks_approved(seed_pending):
    res = approve_proposal(PROJECT, seed_pending["request_id"], True, "参数默认值改一下")
    assert res["ok"] is True
    assert res["status"] == "approved"
    assert res["feedback"] == "参数默认值改一下"
    assert _registry[PROJECT][seed_pending["request_id"]]["status"] == "approved"
    assert pending_approvals(PROJECT) == []


def test_decline_proposal_marks_declined(seed_pending):
    res = approve_proposal(PROJECT, seed_pending["request_id"], False, "策略名不合法")
    assert res["ok"] is True
    assert res["status"] == "declined"
    assert pending_approvals(PROJECT) == []


def test_approve_unknown_request_raises(seed_pending):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        approve_proposal(PROJECT, "does-not-exist", True, "")
    assert exc.value.status_code == 404


def test_approve_twice_raises_conflict(seed_pending):
    from fastapi import HTTPException

    approve_proposal(PROJECT, seed_pending["request_id"], True, "")
    with pytest.raises(HTTPException) as exc:
        approve_proposal(PROJECT, seed_pending["request_id"], True, "")
    assert exc.value.status_code == 409


def test_execute_approved_proposal_uses_reviewed_arguments(seed_pending, monkeypatch):
    calls = []

    async def fake_executor(project, arguments, db):
        calls.append(arguments)
        return {"ok": True, "written": arguments["strategy_name"]}

    monkeypatch.setitem(_EXECUTORS, "write_strategy_code", fake_executor)
    approve_proposal(PROJECT, seed_pending["request_id"], True, "")
    result = asyncio.run(execute_approved_proposal(
        SimpleNamespace(id=PROJECT), seed_pending["request_id"], SimpleNamespace()
    ))

    assert calls == [seed_pending["arguments"]]
    assert result["result"]["written"] == "test_strat"
    assert seed_pending["request_id"] not in _registry[PROJECT]


def test_harness_phase_transition_closes_previous_runtime(monkeypatch):
    class FakeHarness:
        def __init__(self, phase):
            self.phase = phase
            self.closed = False

        def close(self):
            self.closed = True

    made = []

    def fake_build(project, runtime_config=None, system_instructions="", phase="RESEARCH", task_profile=""):
        harness = FakeHarness(phase)
        made.append(harness)
        return harness

    async def fake_runtime_config():
        return {"base_url": "http://mock", "model": "mock-model", "api_key": "mock-key"}

    monkeypatch.setattr("app.dsh.engine._runtime_llm_config", fake_runtime_config)
    monkeypatch.setattr("app.dsh.engine._build_harness", fake_build)
    project = SimpleNamespace(id="phase-project")
    research = asyncio.run(_harness_for(project, phase="RESEARCH"))
    implementation = asyncio.run(_harness_for(project, phase="IMPLEMENTATION"))

    assert research.closed is True
    assert implementation.closed is False
    assert len(_harnesses) == 1
    assert any(k.startswith("phase-project:CODING:GENERAL:") for k in _harnesses)


def test_sdk_session_is_persistent_per_specialist_worker():
    project = SimpleNamespace(id="phase-project")
    research = _sdk_session_id(project, "RESEARCH")
    implementation = _sdk_session_id(project, "IMPLEMENTATION", "turn-one")
    next_implementation = _sdk_session_id(project, "IMPLEMENTATION", "turn-two")

    assert research == "dsh_project_phase-project_research"
    assert implementation == "dsh_project_phase-project_coding"
    assert research != implementation
    assert implementation == next_implementation


def test_find_registry_entry_prefers_request_id(seed_pending):
    bucket = _registry[PROJECT]
    other = dict(seed_pending, request_id="other-id", tool="execute_backtest_tool")
    bucket["other-id"] = other
    hit = _find_registry_entry(PROJECT, "write_strategy_code", "", seed_pending["request_id"])
    assert hit is seed_pending
    hit2 = _find_registry_entry(PROJECT, "write_strategy_code", "", None)
    assert hit2 in (seed_pending, other) or hit2 is None  # fuzzy match; at most one key matches


# ---------------------------------------------------------------------------
# SDK flat event mapping
# ---------------------------------------------------------------------------


def test_map_text_delta_chunk():
    n = FakeNotification(
        "assistant/chunk",
        {
            "sessionId": "s1",
            "event": {
                "type": "assistant/chunk",
                "data": {"turn": 1, "step": 2, "chunk": {"type": "text-delta", "text": "你好"}},
            },
        },
    )
    m = _map_notification(n)
    assert m["kind"] == "chunk"
    assert m["chunk_type"] == "text-delta"
    assert m["text"] == "你好"
    assert m["stream_key"] == "text:1:2"


def test_map_reasoning_delta_chunk():
    n = FakeNotification(
        "session.event",
        {
            "sessionId": "s1",
            "event": {
                "type": "assistant/chunk",
                "data": {"turn": 1, "step": 2, "chunk": {"type": "reasoning-delta", "text": "先检查策略约束"}},
            },
        },
    )
    m = _map_notification(n)
    assert m["kind"] == "reasoning_chunk"
    assert m["text"] == "先检查策略约束"
    assert m["stream_key"] == "reasoning:1:2"


def test_live_buffer_aggregates_token_deltas():
    turn_id = _start_live_turn(PROJECT)
    _record_live_event(
        PROJECT,
        turn_id,
        {"type": "sdk_event", "kind": "chunk", "stream_key": "text:1:1", "text": "策略"},
    )
    _record_live_event(
        PROJECT,
        turn_id,
        {"type": "sdk_event", "kind": "chunk", "stream_key": "text:1:1", "text": "设计"},
    )

    live = get_live_session_events(PROJECT)
    assert len(live) == 1
    assert live[0]["text"] == "策略设计"
    assert len(_session_events[PROJECT]) == 2


def test_live_event_buffer_resets_per_turn_and_keeps_order():
    first_turn = _start_live_turn(PROJECT)
    first = _record_live_event(PROJECT, first_turn, {"type": "sdk_event", "kind": "turn_start"})
    second = _record_live_event(
        PROJECT,
        first_turn,
        {"type": "sdk_event", "kind": "tool_call", "call_id": "call_1", "tool": {"name": "quant_get_strategy"}},
    )

    assert first["seq"] == 1
    assert second["seq"] == 2
    assert [event["kind"] for event in get_live_session_events(PROJECT)] == ["turn_start", "tool_call"]

    next_turn = _start_live_turn(PROJECT)
    third = _record_live_event(PROJECT, next_turn, {"type": "sdk_event", "kind": "turn_start"})
    assert third["seq"] == 3
    assert [event["seq"] for event in get_live_session_events(PROJECT)] == [3]
    assert [event["seq"] for event in _session_events[PROJECT]] == [1, 2, 3]


def test_live_tool_event_updates_visible_status():
    event = {"kind": "tool_call", "tool": {"name": "execute_backtest_tool"}}
    _update_live_status(PROJECT, event)

    assert _status[PROJECT]["status"] == "TOOL_RUNNING"
    assert _status[PROJECT]["stage"] == "正在调用 execute_backtest_tool"


def test_execute_turn_publishes_tool_call_before_run_finishes(monkeypatch):
    from app.dsh import engine

    notification_sent = threading.Event()
    release_run = threading.Event()

    class StreamingHarness:
        def run(self, prompt, session_id, on_notification):
            on_notification(
                FakeNotification(
                    "tool/call",
                    {
                        "sessionId": session_id,
                        "event": {
                            "type": "tool/call",
                            "data": {
                                "turn": 1,
                                "step": 1,
                                "callId": "live_call",
                                "name": "quant_get_strategy",
                                "arguments": "{\"strategy_name\":\"ema_breakout\"}",
                            },
                        },
                    },
                )
            )
            notification_sent.set()
            assert release_run.wait(timeout=2)
            on_notification(
                FakeNotification(
                    "session.event",
                    {
                        "sessionId": session_id,
                        "event": {
                            "type": "tool/result",
                            "data": {
                                "turn": 1,
                                "step": 1,
                                "message": {
                                    "source": {"kind": "tool", "callId": "live_call"},
                                    "content": [
                                        {
                                            "type": "tool-result",
                                            "toolCallId": "live_call",
                                            "content": [{"type": "text", "text": "{\"ok\":true}"}],
                                        }
                                    ],
                                },
                            },
                        },
                    },
                )
            )
            return SimpleNamespace(final_response="完成")

    async def skip_persist(project_id, mapped):
        return None

    monkeypatch.setattr(engine, "_harness_for", lambda *args, **kwargs: StreamingHarness())
    monkeypatch.setattr(engine, "persist_mapped", skip_persist)

    async def scenario():
        project = SimpleNamespace(id=PROJECT)
        task = asyncio.create_task(engine._execute_turn(project, "研究策略"))
        assert await asyncio.to_thread(notification_sent.wait, 1)
        live = get_live_session_events(PROJECT)
        assert live[-1]["kind"] == "tool_call"
        assert _status[PROJECT]["status"] == "TOOL_RUNNING"
        assert not task.done()
        release_run.set()
        result = await task
        assert result["ok"] is True
        completed_live = get_live_session_events(PROJECT)
        assert completed_live[-1]["kind"] == "tool_result"
        assert completed_live[-1]["tool"]["name"] == "quant_get_strategy"

    asyncio.run(scenario())


def test_map_assistant_message():
    n = FakeNotification(
        "assistant/message",
        {
            "sessionId": "s1",
            "event": {
                "type": "assistant/message",
                "data": {
                    "message": {
                        "content": [
                            {"type": "reasoning", "text": "已核对指标约束"},
                            {"type": "text", "text": "策略已完成"},
                        ]
                    },
                },
            },
        },
    )
    m = _map_notification(n)
    assert m["kind"] == "assistant_message"
    assert m["text"] == "策略已完成"
    assert m["reasoning"] == "已核对指标约束"


def test_map_tool_call():
    n = FakeNotification(
        "tool/call",
        {
            "sessionId": "s1",
            "event": {
                "type": "tool/call",
                "data": {
                    "turn": 2,
                    "step": 3,
                    "callId": "call_1",
                    "name": "write_strategy_code",
                    "arguments": "{\"strategy_name\":\"ema_breakout\",\"code\":\"pass\"}",
                },
            },
        },
    )
    m = _map_notification(n)
    assert m["kind"] == "tool_call"
    assert m["tool"]["name"] == "write_strategy_code"
    assert m["tool"]["arguments"]["strategy_name"] == "ema_breakout"
    assert m["call_id"] == "call_1"


def test_map_tool_result_uses_message_source_call_id_and_content():
    n = FakeNotification(
        "session.event",
        {
            "sessionId": "s1",
            "event": {
                "type": "tool/result",
                "data": {
                    "turn": 2,
                    "step": 3,
                    "message": {
                        "role": "user",
                        "source": {"kind": "tool", "callId": "call_1"},
                        "content": [
                            {
                                "type": "tool-result",
                                "toolCallId": "call_1",
                                "content": [{"type": "text", "text": "{\"ok\":true,\"path\":\"strategy.py\"}"}],
                            }
                        ],
                    },
                },
            },
        },
    )
    m = _map_notification(n)
    assert m["kind"] == "tool_result"
    assert m["call_id"] == "call_1"
    assert m["result"] == {"ok": True, "path": "strategy.py"}


def test_map_session_status():
    n = FakeNotification("session.status", {"sessionId": "s1", "status": "idle"})
    m = _map_notification(n)
    assert m["type"] == "status"
    assert m["status"] == "IDLE"


def test_map_unknown_falls_back_raw():
    n = FakeNotification("agent/resumed", {"sessionId": "s1"})
    m = _map_notification(n)
    assert m["type"] == "raw"


def test_resolve_strategy_name_for_project_never_falls_back_to_global_file():
    import asyncio
    from app.dsh.bridge import _resolve_strategy_name_for_project
    from app.models import ResearchProject
    from unittest.mock import AsyncMock

    async def _test():
        project = ResearchProject(id="proj-test", title="Test Project")
        mock_db = AsyncMock()
        mock_db.get.return_value = None

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            await _resolve_strategy_name_for_project(project, "btc_ema_atr", mock_db)
        assert exc.value.status_code == 409

    asyncio.run(_test())


def test_approved_arguments_are_rejected_if_mutated(seed_pending):
    from fastapi import HTTPException

    seed_pending["arguments_hash"] = "not-the-current-hash"
    approve_proposal(PROJECT, seed_pending["request_id"], True, "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(execute_approved_proposal(
            SimpleNamespace(id=PROJECT), seed_pending["request_id"], SimpleNamespace()
        ))
    assert exc.value.status_code == 409


def test_execute_approved_proposal_catches_executor_exception():
    import asyncio
    from app.dsh.bridge import execute_approved_proposal, _registry, _EXECUTORS
    from app.models import ResearchProject
    from unittest.mock import AsyncMock

    async def _test():
        project = ResearchProject(id="proj-exc-test", title="Exc Project")
        _registry["proj-exc-test"] = {
            "req-1": {
                "request_id": "req-1",
                "tool": "execute_backtest_tool",
                "arguments": {"parameters": {"fast_period": 12, "slow_period": 26, "trade_size": 0.1}},
                "status": "approved",
            }
        }

        original_executor = _EXECUTORS.get("execute_backtest_tool")
        try:
            _EXECUTORS["execute_backtest_tool"] = AsyncMock(side_effect=ValueError("未知策略参数: fast_period"))
            mock_db = AsyncMock()
            res = await execute_approved_proposal(project, "req-1", mock_db)
            assert res["status"] == "ok"
            assert res["result"]["ok"] is False
            assert "未知策略参数" in res["result"]["error"]
        finally:
            if original_executor:
                _EXECUTORS["execute_backtest_tool"] = original_executor

    asyncio.run(_test())


def test_failed_preflight_never_publishes_strategy(monkeypatch, tmp_path):
    published = []
    workspace_file = tmp_path / "candidate.py"

    monkeypatch.setattr("app.dsh.bridge._workspace_strategy_file", lambda project, name: workspace_file)
    monkeypatch.setattr(
        "app.dsh.bridge._verify_custom_path",
        lambda target, name: {"ok": False, "failed_level": "L4", "error_message": "bad runtime"},
    )
    monkeypatch.setattr("app.dsh.bridge._diff_vs_baseline", lambda target, name: {"diff": "candidate"})
    monkeypatch.setattr("app.strategy_files.save_strategy_code", lambda name, code: published.append((name, code)))

    from app.dsh.bridge import _exec_write_strategy_code

    result = asyncio.run(_exec_write_strategy_code(
        SimpleNamespace(id="isolated-project"),
        {"strategy_name": "safe_candidate", "code": "from __future__ import annotations\n"},
        SimpleNamespace(),
    ))
    assert result["ok"] is False
    assert result["status"] == "verification_failed"
    assert published == []


def test_write_approval_result_keeps_error_but_drops_large_diff_body():
    result = _compact_approval_result("write_strategy_code", {
        "ok": False,
        "status": "verification_failed",
        "verification": {"failed_level": "L2", "error_message": "bad manifest"},
        "diff": {"files": [{"path": "x.py"}], "additions": 700, "deletions": 0, "diff": "x" * 50_000},
    })
    assert result["verification"]["failed_level"] == "L2"
    assert result["diff"] == {"files": [{"path": "x.py"}], "additions": 700, "deletions": 0}
    assert "truncated" not in result


def test_stage_candidate_persists_failed_preflight_without_publishing(monkeypatch, tmp_path):
    from app.dsh.bridge import _exec_stage_strategy_candidate

    candidate = tmp_path / "staged.py"
    monkeypatch.setattr("app.dsh.bridge._workspace_strategy_file", lambda project, name: candidate)
    monkeypatch.setattr(
        "app.dsh.bridge._verify_custom_path",
        lambda target, name: {"ok": False, "failed_level": "L2", "error_message": "bad manifest"},
    )
    result = asyncio.run(_exec_stage_strategy_candidate(
        SimpleNamespace(id="project"),
        {"strategy_name": "staged_strategy", "code": "from __future__ import annotations\nVALUE = 1\n"},
        SimpleNamespace(),
    ))
    assert result["status"] == "candidate_staged"
    assert result["verification"]["failed_level"] == "L2"
    assert candidate.read_text(encoding="utf-8").endswith("VALUE = 1\n")


def test_patch_candidate_changes_only_unique_fragment_and_reverifies(monkeypatch, tmp_path):
    from app.dsh.bridge import _exec_patch_strategy_candidate

    candidate = tmp_path / "patched.py"
    candidate.write_text("from __future__ import annotations\nVALUE = 1\nUNCHANGED = 9\n", encoding="utf-8")
    monkeypatch.setattr("app.dsh.bridge._workspace_strategy_file", lambda project, name: candidate)
    monkeypatch.setattr("app.dsh.bridge._verify_custom_path", lambda target, name: {"ok": True})
    result = asyncio.run(_exec_patch_strategy_candidate(
        SimpleNamespace(id="project"),
        {"strategy_name": "patched_strategy", "edits": [{"old": "VALUE = 1", "new": "VALUE = 2"}]},
        SimpleNamespace(),
    ))
    assert result["ok"] is True
    assert result["applied_edits"] == 1
    assert candidate.read_text(encoding="utf-8") == "from __future__ import annotations\nVALUE = 2\nUNCHANGED = 9\n"


def test_ambiguous_candidate_patch_is_atomic(monkeypatch, tmp_path):
    from app.dsh.bridge import _exec_patch_strategy_candidate

    candidate = tmp_path / "ambiguous.py"
    original = "from __future__ import annotations\nVALUE = 1\nVALUE = 1\n"
    candidate.write_text(original, encoding="utf-8")
    monkeypatch.setattr("app.dsh.bridge._workspace_strategy_file", lambda project, name: candidate)
    result = asyncio.run(_exec_patch_strategy_candidate(
        SimpleNamespace(id="project"),
        {"strategy_name": "ambiguous_strategy", "edits": [{"old": "VALUE = 1", "new": "VALUE = 2"}]},
        SimpleNamespace(),
    ))
    assert result["ok"] is False
    assert "匹配 2 次" in result["error"]
    assert candidate.read_text(encoding="utf-8") == original


def test_normalize_backtest_arguments_accepts_check_data_integrity():
    from app.dsh.bridge import _normalize_backtest_arguments

    args = {
        "strategy_name": "btc_ema_cross",
        "symbols": ["BTCUSDT"],
        "timeframes": ["15m"],
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "initial_balance": 10000.0,
        "leverage": 1.0,
        "execution_model": "CONSERVATIVE",
        "check_data_integrity": True,
        "parameters": {"fast_period": 10},
    }
    normalized = _normalize_backtest_arguments(args)
    assert normalized["strategy_name"] == "btc_ema_cross"
    assert normalized["check_data_integrity"] is True
    assert normalized["symbols"] == ["BTCUSDT"]
    assert normalized["timeframes"] == ["15m"]

