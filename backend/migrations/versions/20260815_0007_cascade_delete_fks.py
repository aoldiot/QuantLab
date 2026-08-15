"""Add ondelete SET NULL to research_projects and related foreign keys.

Revision ID: 20260815_0007
Revises: 20260815_0006
"""
from alembic import op
import sqlalchemy as sa

revision = "20260815_0007"
down_revision = "20260815_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop and recreate research_projects foreign keys with ON DELETE SET NULL
    op.drop_constraint("research_projects_latest_backtest_id_fkey", "research_projects", type_="foreignkey")
    op.drop_constraint("research_projects_strategy_id_fkey", "research_projects", type_="foreignkey")
    op.drop_constraint("research_projects_implementation_session_id_fkey", "research_projects", type_="foreignkey")

    op.create_foreign_key(
        "research_projects_latest_backtest_id_fkey",
        "research_projects",
        "backtest_runs",
        ["latest_backtest_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "research_projects_strategy_id_fkey",
        "research_projects",
        "strategies",
        ["strategy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "research_projects_implementation_session_id_fkey",
        "research_projects",
        "agent_sessions",
        ["implementation_session_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # backtest_runs.research_project_id
    op.drop_constraint("fk_backtest_research", "backtest_runs", type_="foreignkey")
    op.create_foreign_key(
        "fk_backtest_research",
        "backtest_runs",
        "research_projects",
        ["research_project_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # agent_sessions foreign keys
    op.drop_constraint("fk_agent_session_research", "agent_sessions", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_session_research",
        "agent_sessions",
        "research_projects",
        ["research_project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.drop_constraint("fk_agent_session_spec", "agent_sessions", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_session_spec",
        "agent_sessions",
        "strategy_specifications",
        ["specification_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("research_projects_latest_backtest_id_fkey", "research_projects", type_="foreignkey")
    op.drop_constraint("research_projects_strategy_id_fkey", "research_projects", type_="foreignkey")
    op.drop_constraint("research_projects_implementation_session_id_fkey", "research_projects", type_="foreignkey")

    op.create_foreign_key(
        "research_projects_latest_backtest_id_fkey",
        "research_projects",
        "backtest_runs",
        ["latest_backtest_id"],
        ["id"],
    )
    op.create_foreign_key(
        "research_projects_strategy_id_fkey",
        "research_projects",
        "strategies",
        ["strategy_id"],
        ["id"],
    )
    op.create_foreign_key(
        "research_projects_implementation_session_id_fkey",
        "research_projects",
        "agent_sessions",
        ["implementation_session_id"],
        ["id"],
    )

    op.drop_constraint("fk_backtest_research", "backtest_runs", type_="foreignkey")
    op.create_foreign_key(
        "fk_backtest_research",
        "backtest_runs",
        "research_projects",
        ["research_project_id"],
        ["id"],
    )

    op.drop_constraint("fk_agent_session_research", "agent_sessions", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_session_research",
        "agent_sessions",
        "research_projects",
        ["research_project_id"],
        ["id"],
    )
    op.drop_constraint("fk_agent_session_spec", "agent_sessions", type_="foreignkey")
    op.create_foreign_key(
        "fk_agent_session_spec",
        "agent_sessions",
        "strategy_specifications",
        ["specification_id"],
        ["id"],
    )
