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
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("strategy_versions"):
        columns = {c["name"] for c in inspector.get_columns("strategy_versions")}
        if "specification_id" not in columns:
            op.add_column("strategy_versions", sa.Column("specification_id", sa.String(length=36), nullable=True))
        indexes = {idx["name"] for idx in inspector.get_indexes("strategy_versions")}
        if "ix_strategy_versions_specification_id" not in indexes:
            op.create_index("ix_strategy_versions_specification_id", "strategy_versions", ["specification_id"])
        fks = {fk["name"] for fk in inspector.get_foreign_keys("strategy_versions")}
        if "fk_strategy_versions_specification_id" not in fks and inspector.has_table("strategy_specifications"):
            op.create_foreign_key(
                "fk_strategy_versions_specification_id",
                "strategy_versions",
                "strategy_specifications",
                ["specification_id"],
                ["id"],
                ondelete="SET NULL",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("strategy_versions"):
        fks = {fk["name"] for fk in inspector.get_foreign_keys("strategy_versions")}
        if "fk_strategy_versions_specification_id" in fks:
            op.drop_constraint("fk_strategy_versions_specification_id", "strategy_versions", type_="foreignkey")
        indexes = {idx["name"] for idx in inspector.get_indexes("strategy_versions")}
        if "ix_strategy_versions_specification_id" in indexes:
            op.drop_index("ix_strategy_versions_specification_id", table_name="strategy_versions")
        columns = {c["name"] for c in inspector.get_columns("strategy_versions")}
        if "specification_id" in columns:
            op.drop_column("strategy_versions", "specification_id")
