"""Add interactive strategy research workspace."""
import sqlalchemy as sa
from alembic import op

from app import models  # noqa: F401
from app.db import Base

revision = "20260814_0003"
down_revision = "20260814_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    inspector = sa.inspect(bind)
    agent_columns = {column["name"] for column in inspector.get_columns("agent_sessions")}
    if "research_project_id" not in agent_columns:
        op.add_column("agent_sessions", sa.Column("research_project_id", sa.String(36), nullable=True))
        op.create_index("ix_agent_sessions_research_project_id", "agent_sessions", ["research_project_id"])
        op.create_foreign_key("fk_agent_session_research", "agent_sessions", "research_projects", ["research_project_id"], ["id"])
    if "specification_id" not in agent_columns:
        op.add_column("agent_sessions", sa.Column("specification_id", sa.String(36), nullable=True))
        op.create_foreign_key("fk_agent_session_spec", "agent_sessions", "strategy_specifications", ["specification_id"], ["id"])
    backtest_columns = {column["name"] for column in inspector.get_columns("backtest_runs")}
    if "research_project_id" not in backtest_columns:
        op.add_column("backtest_runs", sa.Column("research_project_id", sa.String(36), nullable=True))
        op.create_index("ix_backtest_runs_research_project_id", "backtest_runs", ["research_project_id"])
        op.create_foreign_key("fk_backtest_research", "backtest_runs", "research_projects", ["research_project_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_backtest_research", "backtest_runs", type_="foreignkey")
    op.drop_index("ix_backtest_runs_research_project_id", table_name="backtest_runs")
    op.drop_column("backtest_runs", "research_project_id")
    op.drop_constraint("fk_agent_session_spec", "agent_sessions", type_="foreignkey")
    op.drop_constraint("fk_agent_session_research", "agent_sessions", type_="foreignkey")
    op.drop_index("ix_agent_sessions_research_project_id", table_name="agent_sessions")
    op.drop_column("agent_sessions", "specification_id")
    op.drop_column("agent_sessions", "research_project_id")
    op.drop_table("research_messages")
    op.drop_table("strategy_specifications")
    op.drop_table("research_projects")
