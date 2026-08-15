"""Complete strategy research lifecycle fields."""

import sqlalchemy as sa
from alembic import op

revision = "20260815_0004"
down_revision = "20260814_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE researchstatus ADD VALUE IF NOT EXISTS 'READY_FOR_ANALYSIS'")
    with op.batch_alter_table("research_projects") as batch:
        batch.add_column(sa.Column("conclusion_verdict", sa.String(30), nullable=True))
        batch.add_column(sa.Column("conclusion_summary", sa.Text(), nullable=True))
        batch.add_column(sa.Column("conclusion_next_step", sa.Text(), nullable=True))
        batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("research_projects") as batch:
        batch.drop_column("archived_at")
        batch.drop_column("conclusion_next_step")
        batch.drop_column("conclusion_summary")
        batch.drop_column("conclusion_verdict")
