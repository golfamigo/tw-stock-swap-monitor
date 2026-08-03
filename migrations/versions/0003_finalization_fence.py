"""Fence coordinator terminal transitions to immutable final strategy evidence.

Revision ID: 0003_finalization_fence
Revises: 0002_coordinator_safety_evidence
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_finalization_fence"
down_revision: str | None = "0002_coordinator_safety_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Uuid:
    """Use the same UUID representation as the existing evidence migrations."""

    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    """Retain the exact final run and key that won a durable terminal transition."""

    with op.batch_alter_table("recommendation_states") as batch:
        batch.add_column(sa.Column("finalization_strategy_run_id", _uuid(), nullable=True))
        batch.add_column(sa.Column("finalization_strategy_key", sa.String(length=64), nullable=True))
        batch.create_foreign_key(
            "fk_recommendation_state_finalization_strategy_run",
            "strategy_runs",
            ["finalization_strategy_run_id"],
            ["strategy_run_id"],
        )
        batch.create_check_constraint(
            "ck_recommendation_state_finalization_fence",
            "(finalization_strategy_run_id IS NULL AND finalization_strategy_key IS NULL) OR "
            "(finalization_strategy_run_id IS NOT NULL AND finalization_strategy_key IS NOT NULL)",
        )


def downgrade() -> None:
    """Remove only the additive finalization fence evidence."""

    with op.batch_alter_table("recommendation_states") as batch:
        batch.drop_constraint("ck_recommendation_state_finalization_fence", type_="check")
        batch.drop_constraint(
            "fk_recommendation_state_finalization_strategy_run", type_="foreignkey"
        )
        batch.drop_column("finalization_strategy_key")
        batch.drop_column("finalization_strategy_run_id")
