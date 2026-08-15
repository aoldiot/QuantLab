"""Store strategy version source code in database and add git backup status fields.

Revision ID: 20260815_0008
Revises: 20260815_0007
"""
from alembic import op
import sqlalchemy as sa

revision = "20260815_0008"
down_revision = "20260815_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add code and code_hash to strategy_versions
    with op.batch_alter_table("strategy_versions") as batch:
        batch.add_column(sa.Column("code", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("code_hash", sa.String(length=64), nullable=True))

    # 2. Add backup status columns to git_configuration
    with op.batch_alter_table("git_configuration") as batch:
        batch.add_column(sa.Column("last_backup_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("last_backup_ok", sa.Boolean(), nullable=True))
        batch.add_column(sa.Column("last_backup_message", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("git_configuration") as batch:
        batch.drop_column("last_backup_message")
        batch.drop_column("last_backup_ok")
        batch.drop_column("last_backup_at")

    with op.batch_alter_table("strategy_versions") as batch:
        batch.drop_column("code_hash")
        batch.drop_column("code")
