"""add encrypted remote Git configuration

Revision ID: 20260814_0002
Revises: 20260813_0001
"""
from alembic import op
import sqlalchemy as sa

revision = "20260814_0002"
down_revision = "20260813_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("git_configuration"):
        return
    op.create_table(
        "git_configuration",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("remote_url", sa.String(length=1000), nullable=False),
        sa.Column("username", sa.String(length=300), nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=False),
        sa.Column("auto_push", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("git_configuration"):
        op.drop_table("git_configuration")
