"""Add research decision approval gate."""

import sqlalchemy as sa
from alembic import op

revision = "20260815_0005"
down_revision = "20260815_0004"
branch_labels = None
depends_on = None

DECISION_STATUS = ("PENDING", "RESOLVED", "DISMISSED")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("research_decisions"):
        # create_table emits CREATE TYPE for the enum on PostgreSQL; creating it
        # separately here as well would fail with DuplicateObjectError.
        status = sa.Enum(*DECISION_STATUS, name="decisionstatus")
        op.create_table(
            "research_decisions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), sa.ForeignKey("research_projects.id"), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("options", sa.JSON(), nullable=False),
            sa.Column("recommendation", sa.Text(), nullable=True),
            sa.Column("impact", sa.Text(), nullable=True),
            sa.Column("status", status, nullable=False, server_default="PENDING"),
            sa.Column("answer", sa.Text(), nullable=True),
            sa.Column("origin", sa.String(20), nullable=False, server_default="DISCUSSION"),
            sa.Column("source_message_id", sa.String(36), sa.ForeignKey("research_messages.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    indexes = {idx["name"] for idx in inspector.get_indexes("research_decisions")} if inspector.has_table("research_decisions") else set()
    if "ix_research_decisions_project_id" not in indexes:
        op.create_index("ix_research_decisions_project_id", "research_decisions", ["project_id"])
    if "ix_research_decisions_status" not in indexes:
        op.create_index("ix_research_decisions_status", "research_decisions", ["status"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("research_decisions"):
        indexes = {idx["name"] for idx in inspector.get_indexes("research_decisions")}
        if "ix_research_decisions_status" in indexes:
            op.drop_index("ix_research_decisions_status", table_name="research_decisions")
        if "ix_research_decisions_project_id" in indexes:
            op.drop_index("ix_research_decisions_project_id", table_name="research_decisions")
        op.drop_table("research_decisions")
        sa.Enum(*DECISION_STATUS, name="decisionstatus").drop(bind, checkfirst=True)
