"""Persist terminal coordinator outcomes for fenced final strategy candidates.

Revision ID: 0004_finalization_outcome
Revises: 0003_finalization_fence
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_finalization_outcome"
down_revision: str | None = "0003_finalization_fence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable outcome evidence without rewriting legacy final attempts."""

    with op.batch_alter_table("scan_attempts") as batch:
        batch.add_column(sa.Column("finalization_disposition", sa.String(length=16), nullable=True))
        batch.create_check_constraint(
            "ck_scan_attempt_finalization_disposition",
            "finalization_disposition IS NULL OR "
            "finalization_disposition IN ('APPLIED', 'SUPERSEDED')",
        )
        batch.create_check_constraint(
            "ck_scan_attempt_finalization_evidence",
            "finalization_disposition IS NULL OR "
            "(status = 'SUCCEEDED' AND final_strategy_run_id IS NOT NULL "
            "AND final_strategy_key IS NOT NULL)",
        )


def downgrade() -> None:
    """Remove only the additive finalization outcome evidence."""

    with op.batch_alter_table("scan_attempts") as batch:
        batch.drop_constraint("ck_scan_attempt_finalization_evidence", type_="check")
        batch.drop_constraint("ck_scan_attempt_finalization_disposition", type_="check")
        batch.drop_column("finalization_disposition")
