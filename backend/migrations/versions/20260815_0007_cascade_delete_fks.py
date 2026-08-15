"""Add ondelete SET NULL to research_projects and related foreign keys.

Revision ID: 20260815_0007
Revises: 20260815_0006
"""
import sqlalchemy as sa
from alembic import op

revision = "20260815_0007"
down_revision = "20260815_0006"
branch_labels = None
depends_on = None


def _recreate_foreign_key(
    inspector,
    table_name: str,
    constraint_name: str,
    target_table: str,
    local_cols: list[str],
    remote_cols: list[str],
    ondelete: str | None = None,
) -> None:
    if not inspector.has_table(table_name):
        return
    fks = inspector.get_foreign_keys(table_name)
    for fk in fks:
        if fk.get("constrained_columns") == local_cols:
            fk_name = fk.get("name")
            if fk_name:
                op.drop_constraint(fk_name, table_name, type_="foreignkey")
    op.create_foreign_key(
        constraint_name,
        table_name,
        target_table,
        local_cols,
        remote_cols,
        ondelete=ondelete,
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _recreate_foreign_key(
        inspector,
        "research_projects",
        "research_projects_latest_backtest_id_fkey",
        "backtest_runs",
        ["latest_backtest_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _recreate_foreign_key(
        inspector,
        "research_projects",
        "research_projects_strategy_id_fkey",
        "strategies",
        ["strategy_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _recreate_foreign_key(
        inspector,
        "research_projects",
        "research_projects_implementation_session_id_fkey",
        "agent_sessions",
        ["implementation_session_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # backtest_runs.research_project_id
    _recreate_foreign_key(
        inspector,
        "backtest_runs",
        "fk_backtest_research",
        "research_projects",
        ["research_project_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # agent_sessions foreign keys
    _recreate_foreign_key(
        inspector,
        "agent_sessions",
        "fk_agent_session_research",
        "research_projects",
        ["research_project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    _recreate_foreign_key(
        inspector,
        "agent_sessions",
        "fk_agent_session_spec",
        "strategy_specifications",
        ["specification_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    _recreate_foreign_key(
        inspector,
        "research_projects",
        "research_projects_latest_backtest_id_fkey",
        "backtest_runs",
        ["latest_backtest_id"],
        ["id"],
    )
    _recreate_foreign_key(
        inspector,
        "research_projects",
        "research_projects_strategy_id_fkey",
        "strategies",
        ["strategy_id"],
        ["id"],
    )
    _recreate_foreign_key(
        inspector,
        "research_projects",
        "research_projects_implementation_session_id_fkey",
        "agent_sessions",
        ["implementation_session_id"],
        ["id"],
    )

    _recreate_foreign_key(
        inspector,
        "backtest_runs",
        "fk_backtest_research",
        "research_projects",
        ["research_project_id"],
        ["id"],
    )

    _recreate_foreign_key(
        inspector,
        "agent_sessions",
        "fk_agent_session_research",
        "research_projects",
        ["research_project_id"],
        ["id"],
    )
    _recreate_foreign_key(
        inspector,
        "agent_sessions",
        "fk_agent_session_spec",
        "strategy_specifications",
        ["specification_id"],
        ["id"],
    )
