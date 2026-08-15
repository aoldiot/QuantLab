"""Complete strategy research lifecycle fields."""

import sqlalchemy as sa
from alembic import op

revision = "20260815_0004"
down_revision = "20260814_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE researchstatus ADD VALUE IF NOT EXISTS 'READY_FOR_ANALYSIS'")
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("research_projects")} if inspector.has_table("research_projects") else set()
    with op.batch_alter_table("research_projects") as batch:
        if "conclusion_verdict" not in columns:
            batch.add_column(sa.Column("conclusion_verdict", sa.String(30), nullable=True))
        if "conclusion_summary" not in columns:
            batch.add_column(sa.Column("conclusion_summary", sa.Text(), nullable=True))
        if "conclusion_next_step" not in columns:
            batch.add_column(sa.Column("conclusion_next_step", sa.Text(), nullable=True))
        if "archived_at" not in columns:
            batch.add_column(sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("research_projects")} if inspector.has_table("research_projects") else set()
    with op.batch_alter_table("research_projects") as batch:
        if "archived_at" in columns:
            batch.drop_column("archived_at")
        if "conclusion_next_step" in columns:
            batch.drop_column("conclusion_next_step")
        if "conclusion_summary" in columns:
            batch.drop_column("conclusion_summary")
        if "conclusion_verdict" in columns:
            batch.drop_column("conclusion_verdict")
