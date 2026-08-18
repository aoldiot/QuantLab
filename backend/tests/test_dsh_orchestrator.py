from unittest.mock import patch

import pytest

from app.db import engine
from app.dsh.orchestrator import DSHOrchestrator
from app.dsh.prompts import (
    DEVELOPER_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
)
from app.dsh.runtime import dsh_runtime


@pytest.mark.anyio
async def test_dsh_orchestrator_star_topology_execution():
    await engine.dispose()
    orchestrator = DSHOrchestrator(session_id="test-session-dsh-001")

    async def mock_call_llm(messages, system_prompt="", tools=None, db_config=None, temperature=0.2):
        if system_prompt == RESEARCHER_SYSTEM_PROMPT:
            return '{"strategy_name": "btc_ema_atr_trend", "hypothesis": "EMA trend following", "timeframe": "1h"}', [], "思考因子IC显著性..."
        elif system_prompt == REVIEWER_SYSTEM_PROMPT:
            return "### 审查结论: APPROVED\n- 逻辑一致性: 通过\n- 未来函数排查: 无未来函数\n- 契约规范: 通过", [], "审查代码逻辑中..."
        elif system_prompt == DEVELOPER_SYSTEM_PROMPT:
            return "策略代码实现完成", [], "开发中..."
        else:
            return "【Quant Lead 综合报告】\n本次量化策略研发已圆满完成。策略代码已通过 4 级 Pre-Flight 沙盒与 Reviewer 独立审查。", [], "汇总各子Agent成果..."

    with patch.object(dsh_runtime, "call_llm", side_effect=mock_call_llm):
        res = await orchestrator.execute_task(
            user_prompt="构建一个基于 BTC 1h 的 EMA 趋势策略并完成全流程回测验证",
            project_id="proj-dsh-test",
        )

        assert res["ok"] is True
        assert res["strategy_name"] == "btc_ema_atr_trend"
        assert "candidate" in res
        assert "verification" in res
        assert "review" in res
        assert "backtest" in res
        assert "robustness" in res
        assert "final_summary" in res
        assert "Quant Lead" in res["final_summary"] or "完成" in res["final_summary"]

        events = dsh_runtime.get_session_events("test-session-dsh-001")
        roles = {e.agent_role for e in events}
        assert "lead" in roles
        assert "researcher" in roles
        assert "developer" in roles
        assert "reviewer" in roles
