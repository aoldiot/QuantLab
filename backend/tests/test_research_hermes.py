import json
from pathlib import Path
import pytest

from app.agent.tools import (
    TOOL_DEFINITIONS,
    _validate_strategy_file,
    get_available_data_tool,
    get_strategy_code_tool,
)
from app.research import _parse_hermes_response, TOOL_CALL_REGEX


def test_tool_definitions():
    assert len(TOOL_DEFINITIONS) >= 5
    tool_names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    assert "execute_backtest" in tool_names
    assert "get_strategy_code" in tool_names
    assert "get_available_data" in tool_names
    assert "propose_backtest_params" in tool_names
    assert "propose_code_approval" in tool_names


def test_parse_hermes_response_openai_format():
    payload = {
        "choices": [
            {
                "message": {
                    "reasoning_content": "正在思考突破阈值与止损距离...",
                    "content": "好的，我为你设计了策略并准备调用 Claude 编写代码。",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "write_strategy_with_claude",
                                "arguments": json.dumps(
                                    {
                                        "strategy_name": "btc_breakout",
                                        "instructions": "编写 15m EMA 双均线突破策略",
                                    }
                                ),
                            },
                        }
                    ],
                }
            }
        ]
    }
    text, tool_calls, reasoning = _parse_hermes_response(payload)
    assert "我为你设计了策略" in text
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "write_strategy_with_claude"
    assert tool_calls[0]["arguments"]["strategy_name"] == "btc_breakout"
    assert "正在思考突破阈值" in reasoning


def test_parse_hermes_response_think_tags():
    payload = {
        "choices": [
            {
                "message": {
                    "content": "<think>分析均线金叉逻辑与ATR止损倍数</think>策略设计方案如下，请审阅。",
                }
            }
        ]
    }
    text, tool_calls, reasoning = _parse_hermes_response(payload)
    assert text == "策略设计方案如下，请审阅。"
    assert len(tool_calls) == 0
    assert "分析均线金叉逻辑" in reasoning


def test_parse_hermes_response_responses_format():
    payload = {
        "output": [
            {
                "type": "thought",
                "content": [{"type": "text", "text": "思考模型上下文与回测标的"}],
            },
            {
                "type": "message",
                "content": [{"type": "text", "text": "这是 Hermes /responses 格式回复"}],
            },
            {
                "type": "function_call",
                "name": "execute_backtest",
                "arguments": {
                    "strategy_name": "btc_breakout",
                    "symbols": ["BTCUSDT"],
                    "start_date": "2024-01-01",
                    "end_date": "2024-06-30",
                },
            },
        ]
    }
    text, tool_calls, reasoning = _parse_hermes_response(payload)
    assert "这是 Hermes /responses 格式回复" in text
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "execute_backtest"
    assert tool_calls[0]["arguments"]["symbols"] == ["BTCUSDT"]
    assert "思考模型上下文与回测标的" in reasoning


def test_parse_hermes_response_fallback_regex():
    payload = {
        "output_text": """
我将为你执行回测任务：
```tool_call
{"name": "execute_backtest", "arguments": {"strategy_name": "eth_rsi", "symbols": ["ETHUSDT"], "start_date": "2024-01-01", "end_date": "2024-06-30"}}
```
"""
    }
    text, tool_calls, reasoning = _parse_hermes_response(payload)
    assert "我将为你执行回测任务" in text
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "execute_backtest"
    assert tool_calls[0]["arguments"]["strategy_name"] == "eth_rsi"


def test_strategy_file_validation(tmp_path):
    valid_py = tmp_path / "test_strat.py"
    valid_py.write_text(
        """
import pandas as pd
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
from app.strategy_contract import StrategyManifest, StrategyMode

class TestConfig(StrategyConfig):
    instrument_id: str
    bar_type: str

class TestStrategy(Strategy):
    def on_start(self): pass
    def on_bar(self, bar): pass
    def on_stop(self): pass

STRATEGY_MANIFEST = StrategyManifest(
    name="Test",
    slug="test",
    description="Test strat",
    category="TEST",
    version="1.0.0",
    strategy_path="app.strategies.test:TestStrategy",
    config_path="app.strategies.test:TestConfig",
    parameters={},
    timeframes=("15m",),
    primary_timeframe="15m",
    plot_config={"main_plot": {"close": {"type": "line"}}, "subplots": {}},
    mode=StrategyMode.SINGLE_INSTRUMENT,
)

def calculate_indicators(df, params):
    return df
""",
        encoding="utf-8",
    )
    ok, msg = _validate_strategy_file(valid_py)
    assert ok is True
    assert msg == "OK"

    invalid_py = tmp_path / "invalid_strat.py"
    invalid_py.write_text(
        """
def calculate_indicators(df, params):
    return df
""",
        encoding="utf-8",
    )
    ok, msg = _validate_strategy_file(invalid_py)
    assert ok is False
    assert "缺少 STRATEGY_MANIFEST" in msg


def test_available_data_tool():
    result = get_available_data_tool()
    assert result["ok"] is True
    assert "available_instruments" in result


@pytest.mark.anyio
async def test_project_out_is_busy():
    from app.models import ResearchProject, ResearchStatus
    from app.research import _project_out, ACTIVE_RESEARCH_TASKS
    import asyncio

    proj = ResearchProject(
        id="test-busy-proj",
        client_id="default_client",
        title="Test Busy",
        original_idea="Test idea",
        hermes_conversation="test-conv",
        status=ResearchStatus.DISCUSSING,
    )
    # When no task is active
    ACTIVE_RESEARCH_TASKS.pop(proj.id, None)
    out = _project_out(proj)
    assert out["is_busy"] is False

    # When a task is running
    dummy_task = asyncio.create_task(asyncio.sleep(10))
    ACTIVE_RESEARCH_TASKS[proj.id] = dummy_task
    try:
        out_busy = _project_out(proj)
        assert out_busy["is_busy"] is True
    finally:
        dummy_task.cancel()
        ACTIVE_RESEARCH_TASKS.pop(proj.id, None)


@pytest.mark.anyio
async def test_research_endpoints_and_immediate_persistence():
    from httpx import ASGITransport, AsyncClient
    from app.auth import create_access_token
    from app.db import engine
    from app.main import app

    await engine.dispose()
    token = create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        # 1. Create project
        create_res = await ac.post("/api/research", headers=headers, json={
            "title": "测试策略研讨",
            "original_idea": "",
            "client_id": "test_client",
        })
        assert create_res.status_code == 200
        proj_data = create_res.json()
        proj_id = proj_data["id"]
        assert proj_data["title"] == "测试策略研讨"
        assert proj_data["is_busy"] is False

        # 2. Check status endpoint
        status_res = await ac.get(f"/api/research/{proj_id}/status", headers=headers)
        assert status_res.status_code == 200
        assert status_res.json()["is_busy"] is False

        # 3. Send message - should immediately persist user message
        send_res = await ac.post(f"/api/research/{proj_id}/messages", headers=headers, json={
            "content": "生成策略",
        })
        assert send_res.status_code == 200
        msgs = send_res.json()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "生成策略"

        # 4. List messages from DB - user message must already be there
        list_res = await ac.get(f"/api/research/{proj_id}/messages", headers=headers)
        assert list_res.status_code == 200
        saved_msgs = list_res.json()
        assert len(saved_msgs) >= 1
        assert saved_msgs[0]["role"] == "user"
        assert saved_msgs[0]["content"] == "生成策略"

        # 5. Check strategy endpoint - must be ok: False since this is a new project without generated strategy
        strat_res = await ac.get(f"/api/research/{proj_id}/strategy", headers=headers)
        assert strat_res.status_code == 200
        strat_data = strat_res.json()
        assert strat_data["ok"] is False
        assert "尚未生成策略代码" in strat_data["message"]


@pytest.mark.anyio
async def test_propose_backtest_params_and_strategy_code_flow():
    from httpx import ASGITransport, AsyncClient
    from app.agent.tools import dispatch_tool_call
    from app.auth import create_access_token
    from app.db import engine, SessionLocal
    from app.main import app
    from app.models import ResearchMessage, ResearchProject, ResearchStatus
    from app.strategy_files import save_strategy_code

    await engine.dispose()
    token = create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Test propose_backtest_params tool dispatch
    params_payload = {
        "strategy_name": "trend_follow_demo",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
        "initial_balance": 20000.0,
        "leverage": 2.0,
        "parameters": {"fast": 10, "slow": 30},
    }
    result = await dispatch_tool_call("propose_backtest_params", params_payload)
    assert result["ok"] is True
    assert result["status"] == "PROPOSED"
    assert result["backtest_params"]["strategy_name"] == "trend_follow_demo"

    # 2. Test strategy code retrieval and auto-registration
    sample_code = """# Test demo strategy
from nautilus_trader.config import StrategyConfig
from nautilus_trader.trading.strategy import Strategy
import pandas as pd
from app.strategy_contract import StrategyManifest

class TrendFollowDemoConfig(StrategyConfig):
    pass

class TrendFollowDemoStrategy(Strategy):
    pass

def calculate_indicators(df: pd.DataFrame, parameters: dict) -> pd.DataFrame:
    return df

STRATEGY_MANIFEST = StrategyManifest(
    name="Trend Follow Demo",
    slug="trend_follow_demo",
    description="Demo for testing",
    category="TREND",
    version="1.0.0",
)
"""
    save_strategy_code("trend_follow_demo", sample_code)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        create_res = await ac.post("/api/research", headers=headers, json={
            "title": "趋势策略项目",
            "original_idea": "趋势策略",
            "client_id": "test_client",
        })
        proj_id = create_res.json()["id"]

        # Insert a message with tool_call referencing the strategy
        async with SessionLocal() as db:
            m = ResearchMessage(
                project_id=proj_id,
                role="tool",
                content="{}",
                message_type="backtest_params",
                metadata_json={"strategy_name": "trend_follow_demo"},
            )
            db.add(m)
            await db.commit()

        # Query /api/research/{proj_id}/strategy
        strat_res = await ac.get(f"/api/research/{proj_id}/strategy", headers=headers)
        assert strat_res.status_code == 200
        strat_data = strat_res.json()
        assert strat_data["ok"] is True
        assert strat_data["strategy_name"] == "trend_follow_demo"
        assert "STRATEGY_MANIFEST" in strat_data["code"]


@pytest.mark.anyio
async def test_execute_backtest_error_handling():
    from app.agent.tools import dispatch_tool_call
    # Call execute_backtest with non-existent strategy
    result = await dispatch_tool_call("execute_backtest", {
        "strategy_name": "non_existent_strat_xyz",
        "symbols": ["BTCUSDT"],
        "start_date": "2024-01-01",
        "end_date": "2024-06-30",
    })
    assert result["ok"] is False
    assert "error" in result
    assert "加载策略 Manifest 失败" in result["error"]


@pytest.mark.anyio
async def test_get_writing_log_endpoint():
    from httpx import ASGITransport, AsyncClient
    from app.auth import create_access_token
    from app.db import engine
    from app.main import app

    await engine.dispose()
    token = create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        create_res = await ac.post("/api/research", headers=headers, json={
            "title": "写码日志测试项目",
            "original_idea": "测试写码日志",
            "client_id": "test_client",
        })
        proj_id = create_res.json()["id"]

        log_res = await ac.get(f"/api/research/{proj_id}/writing-log", headers=headers)
        assert log_res.status_code == 200
        log_data = log_res.json()
        assert "status" in log_data
        assert "progress" in log_data
        assert "logs" in log_data


@pytest.mark.anyio
async def test_propose_code_approval_dispatch():
    from app.agent.tools import dispatch_tool_call
    result = await dispatch_tool_call("propose_code_approval", {
        "strategy_name": "btc_breakout_demo",
        "strategy_summary": "15m 双均线金叉入场，ATR 动态止损",
        "key_rules": [
            "15m 周期，EMA 12 上穿 EMA 26 开多",
            "价格跌破 2*ATR 动态止损出场",
        ],
        "parameter_specs": {"fast_period": 12, "slow_period": 26},
    })
    assert result["ok"] is True
    assert result["status"] == "PENDING_USER_APPROVAL"
    assert "approval_data" in result
    assert result["approval_data"]["strategy_name"] == "btc_breakout_demo"
    assert len(result["approval_data"]["key_rules"]) == 2


@pytest.mark.anyio
async def test_get_thinking_status_endpoint():
    from httpx import ASGITransport, AsyncClient
    from app.auth import create_access_token
    from app.db import engine
    from app.main import app

    await engine.dispose()
    token = create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        create_res = await ac.post("/api/research", headers=headers, json={
            "title": "思考状态测试项目",
            "original_idea": "测试思考状态",
            "client_id": "test_client",
        })
        proj_id = create_res.json()["id"]

        think_res = await ac.get(f"/api/research/{proj_id}/thinking-status", headers=headers)
        assert think_res.status_code == 200
        think_data = think_res.json()
        assert "status" in think_data
        assert "step" in think_data
        assert "thought" in think_data


@pytest.mark.anyio
async def test_run_hermes_agent_cycle_backtest_terminates_immediately(monkeypatch):
    import uuid
    from app.db import engine, SessionLocal
    from app.models import ResearchProject
    from app.research import run_hermes_agent_cycle
    import app.research as r_mod

    await engine.dispose()

    # Mock _call_hermes_stream to return execute_backtest on turn 1, and raise if called again
    calls_count = 0
    async def mock_call_hermes_stream(project, prompt, instructions=None, tools=None, db=None):
        nonlocal calls_count
        calls_count += 1
        if calls_count == 1:
            return "启动回测", [{"name": "execute_backtest", "arguments": {"strategy_name": "non_existent_strat_xyz", "symbols": ["BTCUSDT"], "start_date": "2024-01-01", "end_date": "2024-06-30"}}], "思考中"
        raise AssertionError("execute_backtest 后禁止自动调用下一轮 Hermes Agent！")

    monkeypatch.setattr(r_mod, "_call_hermes_stream", mock_call_hermes_stream)

    async with SessionLocal() as db:
        project = ResearchProject(
            title="回测终止测试",
            original_idea="",
            client_id="test",
            hermes_conversation=f"test-conv-{uuid.uuid4()}",
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        # Run cycle
        messages = await run_hermes_agent_cycle(project, "请执行回测", db=db, max_turns=6, record_user_prompt=True)
        assert calls_count == 1
        # Check messages: user message, assistant text message, tool_call message, tool_output message
        tool_outputs = [m for m in messages if m.message_type == "tool_output"]
        assert len(tool_outputs) == 1
        assert tool_outputs[0].metadata_json["tool_name"] == "execute_backtest"


@pytest.mark.anyio
async def test_analysis_mode_guardrails_blocks_backtest_and_code_writing(monkeypatch):
    import uuid
    from app.db import engine, SessionLocal
    from app.models import ResearchProject
    from app.research import run_hermes_agent_cycle
    import app.research as r_mod

    await engine.dispose()

    # If LLM attempts to emit execute_backtest or write_strategy_with_claude during analysis mode, they must be filtered
    async def mock_call_hermes_stream(project, prompt, instructions=None, tools=None, db=None):
        return "归因分析结论：胜率良好", [
            {"name": "execute_backtest", "arguments": {}},
            {"name": "write_strategy_with_claude", "arguments": {"strategy_name": "strat", "instructions": "fix"}},
        ], ""

    monkeypatch.setattr(r_mod, "_call_hermes_stream", mock_call_hermes_stream)

    async with SessionLocal() as db:
        project = ResearchProject(
            title="分析防护测试",
            original_idea="",
            client_id="test",
            hermes_conversation=f"test-conv-{uuid.uuid4()}",
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        messages = await run_hermes_agent_cycle(project, "请对本次回测结果进行1次深度归因分析（只分析原因，禁止修改代码和回测）", db=db, max_turns=2, record_user_prompt=True)
        # Verify no tool_call or tool_output was executed because they were intercepted
        tool_calls = [m for m in messages if m.message_type in ("tool_call", "tool_output")]
        assert len(tool_calls) == 0


@pytest.mark.anyio
async def test_repair_mode_guardrails_blocks_auto_backtest(monkeypatch):
    import uuid
    from app.db import engine, SessionLocal
    from app.models import ResearchProject
    from app.research import run_hermes_agent_cycle
    import app.research as r_mod

    await engine.dispose()

    async def mock_call_hermes_stream(project, prompt, instructions=None, tools=None, db=None):
        # Hermes tries to emit execute_backtest in repair mode
        return "策略报错修复", [{"name": "execute_backtest", "arguments": {}}], ""

    monkeypatch.setattr(r_mod, "_call_hermes_stream", mock_call_hermes_stream)

    async with SessionLocal() as db:
        project = ResearchProject(
            title="修复防护测试",
            original_idea="",
            client_id="test",
            hermes_conversation=f"test-conv-{uuid.uuid4()}",
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        messages = await run_hermes_agent_cycle(project, "针对策略回测报错，请进行1次策略代码修复（只修改代码，禁止自动回测）", db=db, max_turns=2, record_user_prompt=True)
        # Verify execute_backtest was blocked
        backtest_calls = [m for m in messages if m.message_type in ("tool_call", "tool_output") and m.metadata_json.get("tool_name") == "execute_backtest"]
        assert len(backtest_calls) == 0


@pytest.mark.anyio
async def test_code_approval_hermes_generates_and_syncs_strategy_code(monkeypatch):
    import uuid
    from sqlalchemy import select
    from app.db import engine, SessionLocal
    from app.models import ResearchProject, Strategy
    from app.research import run_hermes_agent_cycle
    import app.research as r_mod

    await engine.dispose()

    async def mock_call_hermes_stream(project, prompt, instructions=None, tools=None, db=None):
        return (
            "策略代码已编写完成：\n\n```python\nfrom nautilus_trader.config import StrategyConfig\nfrom app.strategy_contract import StrategyManifest\nSTRATEGY_MANIFEST = StrategyManifest(slug='btc_trend_test', name='BTC', description='', category='trend', strategy_path='', config_path='', parameters={}, timeframes=('1h',), primary_timeframe='1h')\n```",
            [],
            "思考写码逻辑",
        )

    monkeypatch.setattr(r_mod, "_call_hermes_stream", mock_call_hermes_stream)

    async with SessionLocal() as db:
        project = ResearchProject(
            title="写码测试项目",
            original_idea="",
            client_id="test",
            hermes_conversation=f"test-conv-{uuid.uuid4()}",
        )
        db.add(project)
        await db.commit()
        await db.refresh(project)

        messages = await run_hermes_agent_cycle(
            project,
            "已批准策略「btc_trend_test」的设计方案。请开始编写策略代码文件 backend/app/strategies/btc_trend_test.py 并严格遵循 NautilusTrader 开发规范。",
            db=db,
            max_turns=2,
            record_user_prompt=True,
        )

        assert len(messages) >= 2
        strat = await db.scalar(select(Strategy).where(Strategy.slug == "btc_trend_test"))
        assert strat is not None








