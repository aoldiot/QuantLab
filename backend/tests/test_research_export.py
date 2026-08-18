import uuid
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.auth import create_access_token
from app.db import SessionLocal
from app.models import ResearchProject, ResearchMessage, ResearchStatus


@pytest.mark.anyio
async def test_research_export_json_and_markdown():
    async with SessionLocal() as db:
        unique_id = str(uuid.uuid4())
        proj = ResearchProject(
            client_id="default_client",
            title="测试动量突破策略研究",
            original_idea="基于EMA与ATR的突破策略，突破近期高点伴随成交量放大具备正向收益期望",
            status=ResearchStatus.DISCUSSING,
            conversation_id=f"test_conv_{unique_id}",
        )
        db.add(proj)
        await db.commit()
        await db.refresh(proj)

        # Add sample messages with CoT and tool calls
        msg1 = ResearchMessage(
            project_id=proj.id,
            role="user",
            content="请设计一个 BTC 动量突破策略",
            message_type="message",
        )
        msg2 = ResearchMessage(
            project_id=proj.id,
            role="assistant",
            content="以下是量化策略设计方案：...",
            message_type="message",
            metadata_json={"reasoning": "分析 BTC 波动特征，决定采用双均线 + ATR 通道过滤"},
        )
        msg3 = ResearchMessage(
            project_id=proj.id,
            role="assistant",
            content="调用工具: quant_write_strategy_code",
            message_type="tool_call",
            metadata_json={
                "tool_name": "quant_write_strategy_code",
                "arguments": {"strategy_name": "btc_momentum_breakout"},
            },
        )
        msg4 = ResearchMessage(
            project_id=proj.id,
            role="tool",
            content="策略代码编写完成并通过 4 级沙盒",
            message_type="tool_result",
            metadata_json={
                "tool_name": "quant_write_strategy_code",
                "result": {"ok": True, "strategy_name": "btc_momentum_breakout"},
            },
        )
        db.add_all([msg1, msg2, msg3, msg4])
        await db.commit()

        token = create_access_token("admin")
        headers = {"Authorization": f"Bearer {token}"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 1. Test Markdown export via Authorization header
            res_md = await client.get(f"/api/research/{proj.id}/export?format=markdown", headers=headers)
            if res_md.status_code != 200:
                print("FAILED RES_MD:", res_md.status_code, res_md.text)
            assert res_md.status_code == 200
            assert "text/markdown" in res_md.headers.get("content-type", "")
            assert "Content-Disposition" in res_md.headers
            md_text = res_md.text
            assert "测试动量突破策略研究" in md_text
            assert "DeepSeek CoT 思考链" in md_text
            assert "quant_write_strategy_code" in md_text
            assert "DSH 首席量化架构师系统提示词" in md_text

            # 2. Test JSON export via token query param
            res_json = await client.get(f"/api/research/{proj.id}/export?format=json&token={token}")
            assert res_json.status_code == 200
            assert "application/json" in res_json.headers.get("content-type", "")
            data = res_json.json()
            assert data["project"]["title"] == "测试动量突破策略研究"
            assert len(data["messages"]) >= 4
            assert data["messages"][1]["metadata"]["reasoning"] == "分析 BTC 波动特征，决定采用双均线 + ATR 通道过滤"
            assert "system_prompt" in data
