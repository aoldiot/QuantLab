from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app import research
from app.dsh import bridge, engine
from app.dsh.tools import dispatch_dsh_tool_call
from app.models import ResearchStatus
from app.research import (
    IMPLEMENTATION_PHASE_INSTRUCTIONS,
    _fast_intent_decision,
    _implementation_prompt,
    _instructions_for_phase,
)
from app.schemas import DshActionRequest, ResearchMessageCreate


def test_dsh_intent_response_parser_accepts_structured_json_only() -> None:
    parsed = engine._parse_intent_response(
        '```json\n{"intent":"start_implementation","confidence":0.93,'
        '"normalized_request":"按已确认方案实现","needs_clarification":false}\n```'
    )
    assert parsed["intent"] == "START_IMPLEMENTATION"
    assert parsed["confidence"] == 0.93
    assert parsed["normalized_request"] == "按已确认方案实现"


def test_dsh_intent_response_parser_handles_truncated_tokens() -> None:
    truncated = (
        '{"intent":"START_IMPLEMENTATION","confidence":0.84,'
        '"normalized_request":"确认方案并实现","needs_clarification":false,'
        '"clarification_question":"","pending_request_id":"","reason":"用户逐条确认了'
    )
    parsed = engine._parse_intent_response(truncated)
    assert parsed["intent"] == "START_IMPLEMENTATION"
    assert parsed["confidence"] == 0.84
    assert parsed["normalized_request"] == "确认方案并实现"
    assert parsed["needs_clarification"] is False


def test_dsh_intent_response_parser_rejects_unknown_intent() -> None:
    try:
        engine._parse_intent_response('{"intent":"GUESS_FROM_KEYWORDS"}')
    except ValueError as exc:
        assert "未知意图" in str(exc)
    else:
        raise AssertionError("unknown DSH intent must be rejected")


def test_dsh_intent_response_parser_rejects_non_json() -> None:
    try:
        engine._parse_intent_response("这是没有意图的纯文本回复")
    except ValueError as exc:
        assert "未返回 JSON 对象" in str(exc)
    else:
        raise AssertionError("non-json output must be rejected")


def test_exact_common_commands_use_fast_route_but_nuanced_text_does_not() -> None:
    fixed = _fast_intent_decision("执行回测", [])
    assert fixed is not None
    assert fixed["intent"] == "REQUEST_BACKTEST"
    assert fixed["confidence"] == 1.0
    assert _fast_intent_decision("先讨论一下是否适合执行回测", []) is None


def test_implementation_phase_uses_focused_instructions_and_complete_prompt() -> None:
    project = SimpleNamespace(original_idea="完整策略规格")
    prompt = _implementation_prompt(project, "同意，开始编码", "已确认研究方案")

    assert _instructions_for_phase("IMPLEMENTATION") == IMPLEMENTATION_PHASE_INSTRUCTIONS
    assert "不要重新输出研究方案" in IMPLEMENTATION_PHASE_INSTRUCTIONS
    assert "write_strategy_code" in IMPLEMENTATION_PHASE_INSTRUCTIONS
    assert "完整 QuantLab 项目文件系统和终端" in IMPLEMENTATION_PHASE_INSTRUCTIONS
    assert "最多三轮" in IMPLEMENTATION_PHASE_INSTRUCTIONS
    assert "完整策略规格" in prompt
    assert "已确认研究方案" in prompt
    assert "同意，开始编码" in prompt


def test_dsh_endpoint_routes_only_from_dsh_intent_result(monkeypatch) -> None:
    project = SimpleNamespace(
        id="implementation-routing",
        status=ResearchStatus.DISCUSSING,
        updated_at=None,
        research_phase="RESEARCH",
        original_idea="完整策略规格",
        strategy_id=None,
        latest_backtest_id=None,
    )
    started: dict[str, object] = {}

    class FakeDb:
        def add(self, value) -> None:
            pass

        async def commit(self) -> None:
            pass

    async def fake_project(project_id, db):
        return project

    async def fake_context(project_id, db):
        return [{"role": "assistant", "content": "已确认研究方案"}]

    async def fake_classify(project_arg, user_message, recent_messages, pending):
        assert user_message == "措辞完全不固定，但模型理解我要落地"
        return {
            "intent": "START_IMPLEMENTATION",
            "confidence": 0.95,
            "normalized_request": "根据已确认方案编写完整策略",
            "needs_clarification": False,
            "clarification_question": "",
            "pending_request_id": "",
            "reason": "用户希望落地方案",
        }

    async def fake_approve_specification(project_arg, approved_plan, db):
        return SimpleNamespace(id="specification-1")

    async def fake_create_task(*args, **kwargs):
        return SimpleNamespace(id="task-1")

    def fake_start(project_arg, content, **kwargs):
        started.update(project=project_arg, content=content, **kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(research, "_project", fake_project)
    monkeypatch.setattr(research, "_recent_intent_context", fake_context)
    monkeypatch.setattr(research, "_start_dsh_turn", fake_start)
    monkeypatch.setattr(research, "_approve_research_specification", fake_approve_specification)
    monkeypatch.setattr("app.workflow.task_service.create_task", fake_create_task)
    monkeypatch.setattr(bridge, "pending_approvals", lambda project_id: [])
    monkeypatch.setattr(engine, "classify_intent", fake_classify)

    result = asyncio.run(research.run_dsh_pipeline_endpoint(
        project.id,
        ResearchMessageCreate(content="措辞完全不固定，但模型理解我要落地"),
        FakeDb(),
    ))

    assert result["phase"] == "IMPLEMENTATION"
    assert project.research_phase == "IMPLEMENTATION"
    assert started["phase"] == "IMPLEMENTATION"
    assert "完整策略规格" in str(started["content"])
    assert "write_strategy_code" in str(started["content"])


def test_fixed_backtest_action_executes_directly_without_intent_turn(monkeypatch) -> None:
    project = SimpleNamespace(
        id="fixed-backtest",
        status=ResearchStatus.READY_FOR_BACKTEST,
        updated_at=None,
        research_phase="IMPLEMENTATION",
        strategy_id="strategy-id",
    )
    added = []
    executed = {}

    class FakeDb:
        def add(self, value) -> None:
            added.append(value)

        async def commit(self) -> None:
            pass

    async def fake_project(project_id, db):
        return project

    async def fake_create_task(*args, **kwargs):
        return SimpleNamespace(id="task-1", attempt=0, max_attempts=2)

    async def fake_start_task(db, task, session_id):
        return task

    async def fake_complete_task(db, task, output):
        return task

    async def fake_execute(project_arg, arguments, db):
        executed.update(arguments)
        return {"ok": True, "run_id": "run-1"}

    monkeypatch.setattr(research, "_project", fake_project)
    monkeypatch.setattr("app.workflow.task_service.create_task", fake_create_task)
    monkeypatch.setattr("app.workflow.task_service.start_task", fake_start_task)
    monkeypatch.setattr("app.workflow.task_service.complete_task", fake_complete_task)
    monkeypatch.setattr(bridge, "_exec_execute_backtest", fake_execute)
    monkeypatch.setattr(engine, "classify_intent", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("intent classifier must not run")))

    result = asyncio.run(research.run_dsh_action_endpoint(
        project.id,
        DshActionRequest(
            action="RUN_BACKTEST",
            arguments={
                "strategy_name": "test_strategy",
                "symbols": ["BTCUSDT"],
                "timeframes": ["1h"],
                "start_date": "2026-01-01",
                "end_date": "2026-02-01",
                "initial_balance": 10000,
                "leverage": 1,
                "parameters": {},
            },
        ),
        FakeDb(),
    ))

    assert result["kicked_off"] is False
    assert result["result"]["run_id"] == "run-1"
    assert executed["strategy_name"] == "test_strategy"
    assert any(getattr(item, "role", None) == "assistant" for item in added)


def test_intent_cordis_is_tool_free() -> None:
    text = (Path(__file__).parents[1] / "dsh_runtime" / "cordis-intent.yml").read_text(encoding="utf-8")
    assert "quantlab-tools" not in text
    assert "dsh-bash-local" not in text
    assert "dsh-fs-local" not in text


def test_research_cordis_has_no_terminal_or_filesystem() -> None:
    text = (Path(__file__).parents[1] / "dsh_runtime" / "cordis-research.yml").read_text(encoding="utf-8")
    assert "dsh-bash-local" not in text
    assert "dsh-fs-local" not in text
    assert "quantlab-tools" in text


def test_coding_cordis_has_no_raw_terminal() -> None:
    text = (Path(__file__).parents[1] / "dsh_runtime" / "cordis-coding.yml").read_text(encoding="utf-8")
    assert "dsh-bash-local" not in text
    assert "dsh-fs-local" not in text
    assert "coding-tools" in text
    assert "quantlab-tools" in text


def test_default_cordis_has_no_raw_terminal() -> None:
    text = (Path(__file__).parents[1] / "dsh_runtime" / "cordis.yml").read_text(encoding="utf-8")
    assert "dsh-bash-local" not in text
    assert "dsh-fs-local" not in text
    assert "coding-tools" in text
    assert "quantlab-tools" in text


def test_repair_phase_instructions_cover_manifest_and_contracts() -> None:
    instructions = _instructions_for_phase("REPAIR")
    assert "STRATEGY_MANIFEST" in instructions
    assert "StrategyManifest" in instructions
    assert "calculate_indicators" in instructions
    assert "完整项目文件系统" in instructions
    assert "不生成审批卡" in instructions


def test_auto_repair_prompt_contains_candidate_and_structured_error() -> None:
    prompt = research._build_auto_repair_prompt(
        "broken_strategy",
        "from app.strategy_contract import StrategyManifest\n",
        {"failed_level": "L2", "error_message": "ParameterSpec required"},
        1,
        2,
    )
    assert "第 1/2 次" in prompt
    assert "ParameterSpec required" in prompt
    assert "from app.strategy_contract import StrategyManifest" in prompt
    assert "不改变交易假设" in prompt
    assert "write_strategy_code" in prompt
    assert "read_strategy_candidate" in prompt
    assert "patch_strategy_candidate" in prompt


def test_cordis_path_routes_to_phase_specific_configs() -> None:
    assert engine._cordis_path("INTENT").name == "cordis-intent.yml"
    assert engine._cordis_path("RESEARCH").name == "cordis-research.yml"
    assert engine._cordis_path("REPAIR").name == "cordis-coding.yml"
    assert engine._cordis_path("FIX_ERROR").name == "cordis-coding.yml"
    assert engine._cordis_path("IMPLEMENTATION").name == "cordis-coding.yml"
    assert engine._cordis_path("BACKTEST").name == "cordis-backtest.yml"
    assert engine._cordis_path("RESULT_REVIEW").name == "cordis-analysis.yml"


def test_coding_worker_has_full_project_tools() -> None:
    runtime = Path(__file__).parents[1] / "dsh_runtime"
    config = (runtime / "cordis-coding.yml").read_text(encoding="utf-8")
    tools = (runtime / "src" / "coding-tools.mjs").read_text(encoding="utf-8")
    assert "coding-tools.mjs" in config
    for name in ("read_file", "search_code", "replace_in_file", "run_command"):
        assert f"name: '{name}'" in tools


def test_capabilities_tool_is_self_contained() -> None:
    result = asyncio.run(dispatch_dsh_tool_call("quant_get_capabilities", {}))
    assert result["ok"] is True
    assert "RESEARCH" in result["research_workflow"]
    assert "研究阶段不读取项目源码" in result["constraints"]


def test_research_tool_budget_is_enforced(monkeypatch) -> None:
    async def fake_dispatch(*args, **kwargs):
        return {"ok": True}

    monkeypatch.setattr(bridge, "dispatch_dsh_tool_call", fake_dispatch)
    project = SimpleNamespace(id="budget-project", research_phase="RESEARCH")
    bridge.reset_turn_budget(project.id)

    async def scenario():
        for _ in range(5):
            result = await bridge._exec_dispatch_tool(
                project,
                {"tool_name": "quant_get_capabilities", "arguments": {}},
                None,
            )
            assert result["ok"] is True
        denied = await bridge._exec_dispatch_tool(
            project,
            {"tool_name": "quant_get_capabilities", "arguments": {}},
            None,
        )
        assert denied["error_code"] == "RESEARCH_TOOL_BUDGET_EXCEEDED"

    asyncio.run(scenario())


def test_empty_final_response_gets_one_synthesis_retry(monkeypatch) -> None:
    calls: list[str] = []

    class Harness:
        def run(self, prompt, session_id, on_notification):
            calls.append(prompt)
            if len(calls) == 1:
                return SimpleNamespace(final_response="")
            on_notification(SimpleNamespace(
                method="assistant/message",
                payload={
                    "sessionId": session_id,
                    "event": {
                        "type": "assistant/message",
                        "data": {"message": {"content": [{"type": "text", "text": "最终研究结论"}]}},
                    },
                },
            ))
            return SimpleNamespace(final_response="最终研究结论")

        def close(self):
            return None

    async def skip_persist(*args, **kwargs):
        return None

    monkeypatch.setattr(engine, "_harness_for", lambda *args, **kwargs: Harness())
    monkeypatch.setattr(engine, "persist_mapped", skip_persist)

    result = asyncio.run(engine._execute_turn(SimpleNamespace(id="empty-final"), "研究", phase="RESEARCH"))
    assert result["ok"] is True
    assert result["final_response"] == "最终研究结论"
    assert result["metrics"]["recovered_empty_response"] is True
    assert len(calls) == 2


def test_write_strategy_timeout_is_960_seconds() -> None:
    assert engine.TASK_TIMEOUT_SECONDS["WRITE_STRATEGY"] == 960


def test_archive_session_directory_archives_and_cleans_folders(tmp_path, monkeypatch) -> None:
    fake_data_root = tmp_path / "data"
    sessions_root = fake_data_root / "dsh" / "sessions"
    target_session_dir = sessions_root / "test-workspace" / "dsh_project_test_123_coding"
    target_session_dir.mkdir(parents=True, exist_ok=True)
    (target_session_dir / "session.jsonl.zstd").write_text("corrupted", encoding="utf-8")

    monkeypatch.setattr(engine.settings, "data_root", fake_data_root)

    archived = engine._archive_session_directory("dsh_project_test_123_coding")
    assert len(archived) == 1
    assert not target_session_dir.exists()
    assert archived[0].exists()
    assert (archived[0] / "session.jsonl.zstd").read_text(encoding="utf-8") == "corrupted"
    assert "_archived_" in archived[0].name
