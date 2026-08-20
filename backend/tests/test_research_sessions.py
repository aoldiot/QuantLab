import uuid

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    BacktestRun,
    ResearchMessage,
    ResearchProject,
    ResearchStatus,
    RunStatus,
    Strategy,
    StrategyVersion,
)
from app.research import create_project, list_project_backtests
from app.schemas import ResearchProjectCreate


@pytest.mark.anyio
async def test_continue_strategy_creates_independent_session_with_handoff():
    suffix = uuid.uuid4().hex[:10]
    async with SessionLocal() as db:
        strategy = Strategy(
            name=f"会话续接测试 {suffix}",
            slug=f"session_handoff_{suffix}",
            description="测试同一策略的多轮独立会话",
            category="test",
        )
        db.add(strategy)
        await db.flush()
        version = StrategyVersion(
            strategy_id=strategy.id,
            version="1.0.0",
            entrypoint=f"app.strategies.session_handoff_{suffix}:Strategy",
            code="# test strategy",
            parameter_schema={},
            data_requirements={},
        )
        db.add(version)
        await db.flush()
        source = ResearchProject(
            client_id="session-test-client",
            title="第一轮策略设计",
            original_idea="使用趋势过滤和波动率止损",
            status=ResearchStatus.DISCUSSING,
            research_phase="RESULT_REVIEW",
            conversation_id=f"source-{suffix}",
            strategy_id=strategy.id,
            conclusion_summary="趋势过滤有效，但震荡期假突破偏多",
            conclusion_next_step="在新会话中优化震荡过滤",
        )
        db.add(source)
        await db.flush()
        run = BacktestRun(
            name="第一轮回测",
            strategy_version_id=version.id,
            status=RunStatus.COMPLETED,
            stage="已完成",
            progress=100,
            config={"strategy_name": strategy.slug, "symbols": ["BTCUSDT"]},
            metrics={"total_return": 12.5, "sharpe_ratio": 1.4},
            research_project_id=source.id,
        )
        db.add(run)
        await db.flush()
        source.latest_backtest_id = run.id
        db.add(ResearchMessage(
            project_id=source.id,
            role="assistant",
            content="建议下一轮重点降低震荡行情中的错误入场。",
            message_type="message",
        ))
        await db.commit()

        created_data = await create_project(
            ResearchProjectCreate(
                client_id=source.client_id,
                title="第二轮震荡过滤优化",
                original_idea="",
                source_project_id=source.id,
            ),
            db,
        )
        created = await db.get(ResearchProject, created_data["id"])
        assert created is not None
        assert created.strategy_id == strategy.id
        assert created.latest_backtest_id == run.id
        assert created.research_phase == "RESULT_REVIEW"
        assert created.conversation_id != source.conversation_id

        handoff = await db.scalar(
            select(ResearchMessage).where(
                ResearchMessage.project_id == created.id,
                ResearchMessage.role == "assistant",
            )
        )
        assert handoff is not None
        assert handoff.metadata_json["event_type"] == "session_handoff"
        assert handoff.metadata_json["source_project_id"] == source.id
        assert "不会复制完整历史消息" in handoff.content
        assert "震荡期假突破偏多" in handoff.content
        assert run.id in handoff.content

        inherited_runs = await list_project_backtests(created.id, db)
        assert [item["id"] for item in inherited_runs] == [run.id]

        created.latest_backtest_id = None
        source.latest_backtest_id = None
        await db.flush()
        await db.delete(handoff)
        source_messages = (
            await db.scalars(select(ResearchMessage).where(ResearchMessage.project_id == source.id))
        ).all()
        for message in source_messages:
            await db.delete(message)
        await db.delete(created)
        await db.delete(source)
        await db.delete(run)
        await db.delete(version)
        await db.delete(strategy)
        await db.commit()
