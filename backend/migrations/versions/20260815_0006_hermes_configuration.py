"""Add hermes configuration to llm_configuration

Revision ID: 20260815_0006
Revises: 20260815_0005
"""
import sqlalchemy as sa
from alembic import op

revision = "20260815_0006"
down_revision = "20260815_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("llm_configuration")} if inspector.has_table("llm_configuration") else set()
    if "hermes_base_url" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_base_url", sa.String(length=500), nullable=True))
    if "hermes_api_key_encrypted" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_api_key_encrypted", sa.Text(), nullable=True))
    if "hermes_model" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_model", sa.String(length=200), nullable=True))
    if "hermes_timeout_seconds" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_timeout_seconds", sa.Integer(), nullable=True))
    if "hermes_last_test_ok" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_last_test_ok", sa.Boolean(), nullable=True))
    if "hermes_last_test_message" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_last_test_message", sa.Text(), nullable=True))
    if "hermes_last_tested_at" not in columns:
        op.add_column("llm_configuration", sa.Column("hermes_last_tested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("llm_configuration")} if inspector.has_table("llm_configuration") else set()
    for col in (
        "hermes_last_tested_at",
        "hermes_last_test_message",
        "hermes_last_test_ok",
        "hermes_timeout_seconds",
        "hermes_model",
        "hermes_api_key_encrypted",
        "hermes_base_url",
    ):
        if col in columns:
            op.drop_column("llm_configuration", col)
