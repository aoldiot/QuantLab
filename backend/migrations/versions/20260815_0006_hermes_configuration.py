"""Add hermes configuration to llm_configuration

Revision ID: 20260815_0006
Revises: 20260815_0005
"""
from alembic import op
import sqlalchemy as sa

revision = "20260815_0006"
down_revision = "20260815_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("llm_configuration", sa.Column("hermes_base_url", sa.String(length=500), nullable=True))
    op.add_column("llm_configuration", sa.Column("hermes_api_key_encrypted", sa.Text(), nullable=True))
    op.add_column("llm_configuration", sa.Column("hermes_model", sa.String(length=200), nullable=True))
    op.add_column("llm_configuration", sa.Column("hermes_timeout_seconds", sa.Integer(), nullable=True))
    op.add_column("llm_configuration", sa.Column("hermes_last_test_ok", sa.Boolean(), nullable=True))
    op.add_column("llm_configuration", sa.Column("hermes_last_test_message", sa.Text(), nullable=True))
    op.add_column("llm_configuration", sa.Column("hermes_last_tested_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_configuration", "hermes_last_tested_at")
    op.drop_column("llm_configuration", "hermes_last_test_message")
    op.drop_column("llm_configuration", "hermes_last_test_ok")
    op.drop_column("llm_configuration", "hermes_timeout_seconds")
    op.drop_column("llm_configuration", "hermes_model")
    op.drop_column("llm_configuration", "hermes_api_key_encrypted")
    op.drop_column("llm_configuration", "hermes_base_url")
