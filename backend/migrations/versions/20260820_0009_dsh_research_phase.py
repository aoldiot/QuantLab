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
    op.add_column("research_projects", sa.Column("research_phase", sa.String(length=40), server_default="RESEARCH", nullable=False))
    op.create_index("ix_research_projects_research_phase", "research_projects", ["research_phase"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_research_projects_research_phase", table_name="research_projects")
    op.drop_column("research_projects", "research_phase")
