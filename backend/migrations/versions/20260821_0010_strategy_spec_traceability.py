"""Link immutable strategy versions to approved research specifications.

Revision ID: 20260821_0010
Revises: 20260820_0009
"""

import sqlalchemy as sa
from alembic import op


revision = "20260821_0010"
down_revision = "20260820_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategy_versions", sa.Column("specification_id", sa.String(length=36), nullable=True))
    op.create_index("ix_strategy_versions_specification_id", "strategy_versions", ["specification_id"])
    op.create_foreign_key(
        "fk_strategy_versions_specification_id",
        "strategy_versions",
        "strategy_specifications",
        ["specification_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_strategy_versions_specification_id", "strategy_versions", type_="foreignkey")
    op.drop_index("ix_strategy_versions_specification_id", table_name="strategy_versions")
    op.drop_column("strategy_versions", "specification_id")
