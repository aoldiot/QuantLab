"""Store strategy version source code in database and add git backup status fields.

Revision ID: 20260815_0008
Revises: 20260815_0007
"""
import sqlalchemy as sa
from alembic import op

revision = "20260815_0008"
down_revision = "20260815_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1. Add code and code_hash to strategy_versions
    if inspector.has_table("strategy_versions"):
        sv_cols = {c["name"] for c in inspector.get_columns("strategy_versions")}
        with op.batch_alter_table("strategy_versions") as batch:
            if "code" not in sv_cols:
                batch.add_column(sa.Column("code", sa.Text(), nullable=False, server_default=""))
            if "code_hash" not in sv_cols:
                batch.add_column(sa.Column("code_hash", sa.String(length=64), nullable=True))

    # 2. Add backup status columns to git_configuration
    if inspector.has_table("git_configuration"):
        gc_cols = {c["name"] for c in inspector.get_columns("git_configuration")}
        with op.batch_alter_table("git_configuration") as batch:
            if "last_backup_at" not in gc_cols:
                batch.add_column(sa.Column("last_backup_at", sa.DateTime(timezone=True), nullable=True))
            if "last_backup_ok" not in gc_cols:
                batch.add_column(sa.Column("last_backup_ok", sa.Boolean(), nullable=True))
            if "last_backup_message" not in gc_cols:
                batch.add_column(sa.Column("last_backup_message", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table("git_configuration"):
        gc_cols = {c["name"] for c in inspector.get_columns("git_configuration")}
        with op.batch_alter_table("git_configuration") as batch:
            if "last_backup_message" in gc_cols:
                batch.drop_column("last_backup_message")
            if "last_backup_ok" in gc_cols:
                batch.drop_column("last_backup_ok")
            if "last_backup_at" in gc_cols:
                batch.drop_column("last_backup_at")

    if inspector.has_table("strategy_versions"):
        sv_cols = {c["name"] for c in inspector.get_columns("strategy_versions")}
        with op.batch_alter_table("strategy_versions") as batch:
            if "code_hash" in sv_cols:
                batch.drop_column("code_hash")
            if "code" in sv_cols:
                batch.drop_column("code")
