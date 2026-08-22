"""add persistent DSH research phase

Revision ID: 20260820_0009
Revises: 20260815_0008
"""

from alembic import op
import sqlalchemy as sa

revision = "20260820_0009"
down_revision = "20260815_0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("research_projects"):
        columns = {c["name"] for c in inspector.get_columns("research_projects")}
        if "research_phase" not in columns:
            op.add_column("research_projects", sa.Column("research_phase", sa.String(length=40), server_default="RESEARCH", nullable=False))
        indexes = {idx["name"] for idx in inspector.get_indexes("research_projects")}
        if "ix_research_projects_research_phase" not in indexes:
            op.create_index("ix_research_projects_research_phase", "research_projects", ["research_phase"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("research_projects"):
        indexes = {idx["name"] for idx in inspector.get_indexes("research_projects")}
        if "ix_research_projects_research_phase" in indexes:
            op.drop_index("ix_research_projects_research_phase", table_name="research_projects")
        columns = {c["name"] for c in inspector.get_columns("research_projects")}
        if "research_phase" in columns:
            op.drop_column("research_projects", "research_phase")
