"""Persist durable recommendation state and deterministic coordinator safety evidence.

Revision ID: 0002_coordinator_safety_evidence
Revises: 0001_foundation
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_coordinator_safety_evidence"
down_revision: str | None = "0001_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Uuid:
    """Return the same cross-dialect UUID type used by the frozen foundation revision."""

    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    """Add recommendation-only durability and attempt/intent audit evidence."""

    op.create_table(
        "recommendation_states",
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            primary_key=True,
        ),
        sa.Column(
            "portfolio_id",
            _uuid(),
            sa.ForeignKey("portfolios.portfolio_id"),
            nullable=False,
        ),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("remaining_stages_halted", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="ck_recommendation_state_revision"),
        sa.CheckConstraint(
            "state IN ('IDLE', 'WATCHING', 'NEAR_TRIGGER', 'ACTION_PENDING', "
            "'ACTION_NOTIFIED', 'DATA_DEGRADED', 'INVALIDATED', 'PARTIALLY_EXECUTED', "
            "'WAITING_CONFIRMATION', 'STAGE_COMPLETED', 'ROTATION_COMPLETED', 'PAUSED')",
            name="ck_recommendation_state_value",
        ),
    )
    op.create_table(
        "child_intents",
        sa.Column("child_intent_id", _uuid(), primary_key=True),
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            nullable=False,
        ),
        sa.Column(
            "portfolio_id",
            _uuid(),
            sa.ForeignKey("portfolios.portfolio_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_run_id",
            _uuid(),
            sa.ForeignKey("strategy_runs.strategy_run_id"),
            nullable=False,
        ),
        sa.Column("final_strategy_key", sa.String(length=64), nullable=False),
        sa.Column("final_strategy_identity_format_version", sa.String(length=16), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("intent_key", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("intent_key", name="uq_child_intent_key"),
        sa.UniqueConstraint("strategy_run_id", "purpose", name="uq_child_intent_run_purpose"),
    )
    with op.batch_alter_table("scan_attempts") as batch:
        batch.add_column(sa.Column("configuration_snapshot_id", _uuid(), nullable=True))
        batch.add_column(
            sa.Column(
                "configuration_snapshot_created_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("final_strategy_key", sa.String(length=64), nullable=True))
        batch.add_column(
            sa.Column("final_strategy_identity_format_version", sa.String(length=16), nullable=True)
        )
        batch.create_foreign_key(
            "fk_scan_attempt_configuration_snapshot",
            "configuration_snapshots",
            ["configuration_snapshot_id"],
            ["snapshot_id"],
        )


def downgrade() -> None:
    """Remove only the additive coordinator-safety schema changes."""

    with op.batch_alter_table("scan_attempts") as batch:
        batch.drop_constraint("fk_scan_attempt_configuration_snapshot", type_="foreignkey")
        batch.drop_column("final_strategy_identity_format_version")
        batch.drop_column("final_strategy_key")
        batch.drop_column("configuration_snapshot_created_at")
        batch.drop_column("configuration_snapshot_id")
    op.drop_table("child_intents")
    op.drop_table("recommendation_states")
