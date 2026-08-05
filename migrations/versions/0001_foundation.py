"""Create the immutable Task 4 foundation schema.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Uuid:
    """Return the frozen cross-dialect UUID type used by this revision."""

    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    """Create the initial schema without consulting current application metadata."""

    op.create_table(
        "users",
        sa.Column("user_id", _uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "instruments",
        sa.Column("instrument_id", _uuid(), primary_key=True),
        sa.Column("market", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("market", "symbol", name="uq_instruments_market_symbol"),
    )
    op.create_table(
        "portfolios",
        sa.Column("portfolio_id", _uuid(), primary_key=True),
        sa.Column("user_id", _uuid(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "positions",
        sa.Column("position_id", _uuid(), primary_key=True),
        sa.Column(
            "portfolio_id", _uuid(), sa.ForeignKey("portfolios.portfolio_id"), nullable=False
        ),
        sa.Column(
            "instrument_id", _uuid(), sa.ForeignKey("instruments.instrument_id"), nullable=False
        ),
        sa.Column("quantity", sa.Numeric(precision=28, scale=8), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_positions_status"),
        sa.CheckConstraint("quantity > 0", name="ck_positions_quantity_positive"),
        sa.CheckConstraint(
            "(status = 'OPEN' AND closed_at IS NULL) OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL)",
            name="ck_positions_status_closed_at",
        ),
    )
    op.create_index(
        "uq_positions_open_portfolio_instrument",
        "positions",
        ["portfolio_id", "instrument_id"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
        sqlite_where=sa.text("status = 'OPEN'"),
    )
    op.create_table(
        "candidate_groups",
        sa.Column("candidate_group_id", _uuid(), primary_key=True),
        sa.Column("user_id", _uuid(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "candidate_group_instruments",
        sa.Column(
            "candidate_group_id",
            _uuid(),
            sa.ForeignKey("candidate_groups.candidate_group_id"),
            primary_key=True,
        ),
        sa.Column(
            "instrument_id",
            _uuid(),
            sa.ForeignKey("instruments.instrument_id"),
            primary_key=True,
        ),
    )
    op.create_table(
        "rotation_plans",
        sa.Column("rotation_plan_id", _uuid(), primary_key=True),
        sa.Column(
            "portfolio_id", _uuid(), sa.ForeignKey("portfolios.portfolio_id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "rotation_plan_candidate_groups",
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            primary_key=True,
        ),
        sa.Column(
            "candidate_group_id",
            _uuid(),
            sa.ForeignKey("candidate_groups.candidate_group_id"),
            primary_key=True,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "rotation_plan_id",
            "ordinal",
            name="uq_rotation_plan_association_ordinal_candidate_groups",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_rotation_plan_association_ordinal_candidate_groups"
        ),
    )
    op.create_table(
        "rotation_plan_source_positions",
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            primary_key=True,
        ),
        sa.Column("position_id", _uuid(), sa.ForeignKey("positions.position_id"), primary_key=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "rotation_plan_id",
            "ordinal",
            name="uq_rotation_plan_association_ordinal_source_positions",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_rotation_plan_association_ordinal_source_positions"
        ),
    )
    op.create_table(
        "rotation_plan_protected_positions",
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            primary_key=True,
        ),
        sa.Column("position_id", _uuid(), sa.ForeignKey("positions.position_id"), primary_key=True),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.UniqueConstraint(
            "rotation_plan_id",
            "ordinal",
            name="uq_rotation_plan_association_ordinal_protected_positions",
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name="ck_rotation_plan_association_ordinal_protected_positions"
        ),
    )
    op.create_table(
        "configuration_layers",
        sa.Column("layer_id", _uuid(), primary_key=True),
        sa.Column("scope", sa.String(length=32), nullable=False),
        sa.Column("reference_id", _uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("patch", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash_format_version", sa.String(length=16), nullable=False),
        sa.Column("ownership_scope", sa.String(length=16), nullable=False),
        sa.Column("owner_id", _uuid(), nullable=True),
        sa.Column("portfolio_owner_id", _uuid(), nullable=True),
        sa.Column("runtime_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "scope", "reference_id", "version", name="uq_configuration_layer_version"
        ),
        sa.CheckConstraint(
            "ownership_scope IN ('SYSTEM', 'USER', 'PORTFOLIO')",
            name="ck_configuration_layer_ownership_scope",
        ),
        sa.CheckConstraint(
            "(ownership_scope = 'SYSTEM' AND owner_id IS NULL) OR "
            "(ownership_scope IN ('USER', 'PORTFOLIO') AND owner_id IS NOT NULL)",
            name="ck_configuration_layer_owner_presence",
        ),
        sa.CheckConstraint(
            "(scope IN ('SYSTEM', 'MARKET', 'STRATEGY') AND ownership_scope = 'SYSTEM') OR "
            "(scope = 'USER' AND ownership_scope = 'USER') OR "
            "(scope IN ('PORTFOLIO', 'ROTATION_PLAN', 'RUNTIME_OVERRIDE') "
            "AND ownership_scope = 'PORTFOLIO')",
            name="ck_configuration_layer_scope_ownership",
        ),
        sa.CheckConstraint(
            "(ownership_scope = 'PORTFOLIO' AND portfolio_owner_id IS NOT NULL) OR "
            "(ownership_scope IN ('SYSTEM', 'USER') AND portfolio_owner_id IS NULL)",
            name="ck_configuration_layer_portfolio_owner_presence",
        ),
    )
    op.create_table(
        "configuration_snapshots",
        sa.Column("snapshot_id", _uuid(), primary_key=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("canonical_format_version", sa.String(length=16), nullable=False),
        sa.Column("parent_versions", sa.JSON(), nullable=False),
        sa.Column("merged_payload", sa.JSON(), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("target_owner_id", _uuid(), nullable=True),
        sa.Column("target_reference_id", _uuid(), nullable=False),
        sa.Column("runtime_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", _uuid(), sa.ForeignKey("users.user_id"), nullable=False),
        sa.CheckConstraint("config_version > 0", name="ck_configuration_snapshot_version"),
        sa.CheckConstraint(
            "(target_scope = 'SYSTEM' AND target_owner_id IS NULL) OR "
            "(target_scope IN ('USER', 'PORTFOLIO') AND target_owner_id IS NOT NULL)",
            name="ck_configuration_snapshot_target_owner",
        ),
    )
    op.create_index(
        "ix_configuration_snapshots_content_hash",
        "configuration_snapshots",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        "uq_configuration_snapshot_target_version_system",
        "configuration_snapshots",
        ["target_scope", "target_reference_id", "config_version"],
        unique=True,
        postgresql_where=sa.text("target_scope = 'SYSTEM' AND target_owner_id IS NULL"),
        sqlite_where=sa.text("target_scope = 'SYSTEM' AND target_owner_id IS NULL"),
    )
    op.create_index(
        "uq_configuration_snapshot_target_version_owned",
        "configuration_snapshots",
        ["target_scope", "target_owner_id", "target_reference_id", "config_version"],
        unique=True,
        postgresql_where=sa.text(
            "target_scope IN ('USER', 'PORTFOLIO') AND target_owner_id IS NOT NULL"
        ),
        sqlite_where=sa.text(
            "target_scope IN ('USER', 'PORTFOLIO') AND target_owner_id IS NOT NULL"
        ),
    )
    op.create_table(
        "logical_scan_runs",
        sa.Column("logical_scan_run_id", _uuid(), primary_key=True),
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            nullable=False,
        ),
        sa.Column("market_session_date", sa.String(length=10), nullable=False),
        sa.Column("scan_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scan_interval", sa.String(length=32), nullable=False),
        sa.Column("market_timezone", sa.String(length=64), nullable=False),
        sa.Column("configuration_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("scan_identity_format_version", sa.String(length=16), nullable=False),
        sa.Column("scan_lock_key", sa.String(length=256), nullable=False, unique=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING', 'COMPLETED')", name="ck_logical_scan_run_status"),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL) OR "
            "(status = 'COMPLETED' AND completed_at IS NOT NULL)",
            name="ck_logical_scan_run_completion",
        ),
    )
    op.create_table(
        "strategy_runs",
        sa.Column("strategy_run_id", _uuid(), primary_key=True),
        sa.Column(
            "rotation_plan_id",
            _uuid(),
            sa.ForeignKey("rotation_plans.rotation_plan_id"),
            nullable=False,
        ),
        sa.Column(
            "portfolio_id", _uuid(), sa.ForeignKey("portfolios.portfolio_id"), nullable=False
        ),
        sa.Column(
            "logical_scan_run_id",
            _uuid(),
            sa.ForeignKey("logical_scan_runs.logical_scan_run_id"),
            nullable=True,
        ),
        sa.Column(
            "configuration_snapshot_id",
            _uuid(),
            sa.ForeignKey("configuration_snapshots.snapshot_id"),
            nullable=False,
        ),
        sa.Column("configuration_content_hash", sa.String(length=64), nullable=False),
        sa.Column("configuration_snapshot_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_data_snapshot_id", sa.String(length=256), nullable=False),
        sa.Column("state_transition", sa.String(length=128), nullable=False),
        sa.Column("outputs", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=512), nullable=False),
        sa.UniqueConstraint("rotation_plan_id", "idempotency_key", name="uq_strategy_run_key"),
        sa.UniqueConstraint("logical_scan_run_id", name="uq_strategy_run_logical_scan"),
        sa.UniqueConstraint(
            "logical_scan_run_id",
            "strategy_run_id",
            name="uq_strategy_run_logical_scan_identity",
        ),
    )
    op.create_table(
        "scan_attempts",
        sa.Column("scan_attempt_id", _uuid(), primary_key=True),
        sa.Column(
            "logical_scan_run_id",
            _uuid(),
            sa.ForeignKey("logical_scan_runs.logical_scan_run_id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("configuration_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("market_data_snapshot_id", sa.String(length=256), nullable=True),
        sa.Column("market_data_content_hash", sa.String(length=64), nullable=True),
        sa.Column("trigger_correlation_id", sa.String(length=256), nullable=False),
        sa.Column("actor_correlation_id", sa.String(length=256), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("recovery_of_attempt_id", _uuid(), nullable=True),
        sa.Column("duplicate_of_attempt_id", _uuid(), nullable=True),
        sa.Column("final_strategy_run_id", _uuid(), nullable=True),
        sa.UniqueConstraint("logical_scan_run_id", "attempt_number", name="uq_scan_attempt_number"),
        sa.UniqueConstraint(
            "logical_scan_run_id",
            "scan_attempt_id",
            name="uq_scan_attempt_same_scan_identity",
        ),
        sa.ForeignKeyConstraint(
            ["logical_scan_run_id", "recovery_of_attempt_id"],
            ["scan_attempts.logical_scan_run_id", "scan_attempts.scan_attempt_id"],
            name="fk_scan_attempt_recovery_same_scan",
        ),
        sa.ForeignKeyConstraint(
            ["logical_scan_run_id", "duplicate_of_attempt_id"],
            ["scan_attempts.logical_scan_run_id", "scan_attempts.scan_attempt_id"],
            name="fk_scan_attempt_duplicate_same_scan",
        ),
        sa.ForeignKeyConstraint(
            ["logical_scan_run_id", "final_strategy_run_id"],
            ["strategy_runs.logical_scan_run_id", "strategy_runs.strategy_run_id"],
            name="fk_scan_attempt_final_strategy_same_scan",
        ),
        sa.CheckConstraint("attempt_number > 0", name="ck_scan_attempt_number_positive"),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'FAILED', 'DEGRADED', 'SUCCEEDED')",
            name="ck_scan_attempt_status",
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL) OR "
            "(status IN ('FAILED', 'DEGRADED', 'SUCCEEDED') AND completed_at IS NOT NULL)",
            name="ck_scan_attempt_completion",
        ),
        sa.CheckConstraint(
            "(status IN ('FAILED', 'DEGRADED') AND failure_code IS NOT NULL "
            "AND failure_detail IS NOT NULL) OR "
            "(status IN ('RUNNING', 'SUCCEEDED') AND failure_code IS NULL "
            "AND failure_detail IS NULL)",
            name="ck_scan_attempt_failure_evidence",
        ),
        sa.CheckConstraint(
            "length(trigger_correlation_id) > 0 AND length(trigger_correlation_id) <= 256 "
            "AND length(actor_correlation_id) > 0 AND length(actor_correlation_id) <= 256 "
            "AND (failure_code IS NULL OR length(failure_code) <= 64) "
            "AND (failure_detail IS NULL OR length(failure_detail) <= 1024)",
            name="ck_scan_attempt_evidence_bounds",
        ),
    )


def downgrade() -> None:
    """Drop the exact initial schema in reverse dependency order."""

    op.drop_table("scan_attempts")
    op.drop_table("strategy_runs")
    op.drop_table("logical_scan_runs")
    op.drop_index(
        "uq_configuration_snapshot_target_version_owned",
        table_name="configuration_snapshots",
    )
    op.drop_index(
        "uq_configuration_snapshot_target_version_system",
        table_name="configuration_snapshots",
    )
    op.drop_index("ix_configuration_snapshots_content_hash", table_name="configuration_snapshots")
    op.drop_table("configuration_snapshots")
    op.drop_table("configuration_layers")
    op.drop_table("rotation_plan_protected_positions")
    op.drop_table("rotation_plan_source_positions")
    op.drop_table("rotation_plan_candidate_groups")
    op.drop_table("rotation_plans")
    op.drop_table("candidate_group_instruments")
    op.drop_table("candidate_groups")
    op.drop_index("uq_positions_open_portfolio_instrument", table_name="positions")
    op.drop_table("positions")
    op.drop_table("portfolios")
    op.drop_table("instruments")
    op.drop_table("users")
