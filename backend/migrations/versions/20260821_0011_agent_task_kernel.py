"""durable specialist worker task kernel

Revision ID: 20260821_0011
Revises: 20260821_0010
"""

import sqlalchemy as sa
from alembic import op

revision = "20260821_0011"
down_revision = "20260821_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    worker_type = sa.Enum("RESEARCH", "CODING", "BACKTEST", "ANALYSIS", name="workertype")
    task_status = sa.Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", "WAITING_USER", "CANCELLED", name="agenttaskstatus")

    if not inspector.has_table("agent_tasks"):
        op.create_table(
            "agent_tasks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("worker_type", worker_type, nullable=False),
            sa.Column("task_type", sa.String(60), nullable=False),
            sa.Column("status", task_status, nullable=False),
            sa.Column("input_json", sa.JSON(), nullable=False),
            sa.Column("output_json", sa.JSON(), nullable=False),
            sa.Column("session_id", sa.String(200)),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("parent_task_id", sa.String(36), sa.ForeignKey("agent_tasks.id", ondelete="SET NULL")),
            sa.Column("error_code", sa.String(80)),
            sa.Column("error_message", sa.Text()),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        for column in ("project_id", "worker_type", "task_type", "status", "session_id"):
            op.create_index(f"ix_agent_tasks_{column}", "agent_tasks", [column])
    else:
        indexes_tasks = {idx["name"] for idx in inspector.get_indexes("agent_tasks")}
        for column in ("project_id", "worker_type", "task_type", "status", "session_id"):
            if f"ix_agent_tasks_{column}" not in indexes_tasks:
                op.create_index(f"ix_agent_tasks_{column}", "agent_tasks", [column])

    if not inspector.has_table("candidate_revisions"):
        op.create_table(
            "candidate_revisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.id", ondelete="SET NULL")),
            sa.Column("strategy_name", sa.String(64), nullable=False),
            sa.Column("parent_revision_id", sa.String(36), sa.ForeignKey("candidate_revisions.id", ondelete="SET NULL")),
            sa.Column("code_sha256", sa.String(64), nullable=False),
            sa.Column("code", sa.Text(), nullable=False),
            sa.Column("patch", sa.Text(), nullable=False, server_default=""),
            sa.Column("source", sa.String(30), nullable=False, server_default="AGENT"),
            sa.Column("verification_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        for column in ("project_id", "task_id", "strategy_name", "code_sha256"):
            op.create_index(f"ix_candidate_revisions_{column}", "candidate_revisions", [column])
    else:
        indexes_revs = {idx["name"] for idx in inspector.get_indexes("candidate_revisions")}
        for column in ("project_id", "task_id", "strategy_name", "code_sha256"):
            if f"ix_candidate_revisions_{column}" not in indexes_revs:
                op.create_index(f"ix_candidate_revisions_{column}", "candidate_revisions", [column])

    if not inspector.has_table("verification_runs"):
        op.create_table(
            "verification_runs",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("research_projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("task_id", sa.String(36), sa.ForeignKey("agent_tasks.id", ondelete="SET NULL")),
            sa.Column("candidate_revision_id", sa.String(36), sa.ForeignKey("candidate_revisions.id", ondelete="SET NULL")),
            sa.Column("code_sha256", sa.String(64), nullable=False),
            sa.Column("contract_version", sa.String(30), nullable=False, server_default="1"),
            sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("diagnostics", sa.JSON(), nullable=False),
            sa.Column("result_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        for column in ("project_id", "task_id", "code_sha256", "ok"):
            op.create_index(f"ix_verification_runs_{column}", "verification_runs", [column])
    else:
        indexes_vruns = {idx["name"] for idx in inspector.get_indexes("verification_runs")}
        for column in ("project_id", "task_id", "code_sha256", "ok"):
            if f"ix_verification_runs_{column}" not in indexes_vruns:
                op.create_index(f"ix_verification_runs_{column}", "verification_runs", [column])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("verification_runs"):
        op.drop_table("verification_runs")
    if inspector.has_table("candidate_revisions"):
        op.drop_table("candidate_revisions")
    if inspector.has_table("agent_tasks"):
        op.drop_table("agent_tasks")
    sa.Enum(name="agenttaskstatus").drop(bind, checkfirst=True)
    sa.Enum(name="workertype").drop(bind, checkfirst=True)
