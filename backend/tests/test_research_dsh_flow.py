from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import create_access_token
from app.db import engine
from app.dsh.prompts import (
    DEVELOPER_SYSTEM_PROMPT,
    RESEARCHER_SYSTEM_PROMPT,
    REVIEWER_SYSTEM_PROMPT,
)
from app.dsh.runtime import dsh_runtime
from app.main import app


@pytest.mark.anyio
async def test_research_dsh_pipeline_endpoint():
    await engine.dispose()
    token = create_access_token("admin")
    headers = {"Authorization": f"Bearer {token}"}

    async def mock_call_llm(messages, system_prompt="", tools=None, db_config=None, temperature=0.2):
        if system_prompt == RESEARCHER_SYSTEM_PROMPT:
            return '{"strategy_name": "btc_ema_atr_trend", "hypothesis": "EMA trend following", "timeframe": "1h"}', [], "思考因子IC..."
        elif system_prompt == REVIEWER_SYSTEM_PROMPT:
            return "### 审查结论: APPROVED\n- 逻辑一致性: 通过\n- 未来函数排查: 无未来函数\n- 契约规范: 通过", [], "审查代码逻辑..."
        elif system_prompt == DEVELOPER_SYSTEM_PROMPT:
            return "策略代码编写完成", [], "开发中..."
        else:
            return "【Quant Lead 综合报告】\n本次量化策略研发与全套验证圆满完成。", [], "汇总报告..."

    with patch.object(dsh_runtime, "call_llm", side_effect=mock_call_llm):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            # 1. Create project
            create_res = await ac.post("/api/research", headers=headers, json={
                "title": "DSH多Agent协作研究项目",
                "original_idea": "BTC EMA 双均线突破趋势策略",
                "client_id": "test_client",
            })
            assert create_res.status_code == 200
            proj = create_res.json()
            project_id = proj["id"]

            # 2. Trigger DSH Star-Topology workflow
            run_res = await ac.post(
                f"/api/research/{project_id}/dsh/run",
                headers=headers,
                json={"content": "基于 BTC 1h 研发 EMA 趋势策略并完成验证"},
            )
            assert run_res.status_code == 200
            data = run_res.json()
            assert data["ok"] is True
            assert data["strategy_name"] == "btc_ema_atr_trend"
            assert "final_summary" in data

            # 3. Check project status update
            p_res = await ac.get(f"/api/research/{project_id}", headers=headers)
            assert p_res.status_code == 200
            p_data = p_res.json()
            assert p_data["strategy_id"] is not None

            # 4. Check that user and assistant messages were saved
            msg_res = await ac.get(f"/api/research/{project_id}/messages", headers=headers)
            assert msg_res.status_code == 200
            msgs = msg_res.json()
            assert len(msgs) >= 1
            assert any(m["role"] == "user" for m in msgs)
