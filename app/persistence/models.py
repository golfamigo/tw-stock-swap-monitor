"""SQLAlchemy 2 persistence projections; no domain logic lives in this module."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Declarative metadata used by Alembic migrations and SQLAlchemy repositories."""


candidate_group_instruments = Table(
    "candidate_group_instruments",
    Base.metadata,
    Column(
        "candidate_group_id",
        Uuid(as_uuid=True),
        ForeignKey("candidate_groups.candidate_group_id"),
        primary_key=True,
    ),
    Column(
        "instrument_id",
        Uuid(as_uuid=True),
        ForeignKey("instruments.instrument_id"),
        primary_key=True,
    ),
)

rotation_plan_candidate_groups = Table(
    "rotation_plan_candidate_groups",
    Base.metadata,
    Column(
        "rotation_plan_id",
        Uuid(as_uuid=True),
        ForeignKey("rotation_plans.rotation_plan_id"),
        primary_key=True,
    ),
    Column(
        "candidate_group_id",
        Uuid(as_uuid=True),
        ForeignKey("candidate_groups.candidate_group_id"),
        primary_key=True,
    ),
    Column("ordinal", Integer, nullable=False),
    UniqueConstraint(
        "rotation_plan_id",
        "ordinal",
        name="uq_rotation_plan_association_ordinal_candidate_groups",
    ),
    CheckConstraint("ordinal >= 0", name="ck_rotation_plan_association_ordinal_candidate_groups"),
)

rotation_plan_source_positions = Table(
    "rotation_plan_source_positions",
    Base.metadata,
    Column(
        "rotation_plan_id",
        Uuid(as_uuid=True),
        ForeignKey("rotation_plans.rotation_plan_id"),
        primary_key=True,
    ),
    Column(
        "position_id", Uuid(as_uuid=True), ForeignKey("positions.position_id"), primary_key=True
    ),
    Column("ordinal", Integer, nullable=False),
    UniqueConstraint(
        "rotation_plan_id",
        "ordinal",
        name="uq_rotation_plan_association_ordinal_source_positions",
    ),
    CheckConstraint("ordinal >= 0", name="ck_rotation_plan_association_ordinal_source_positions"),
)

rotation_plan_protected_positions = Table(
    "rotation_plan_protected_positions",
    Base.metadata,
    Column(
        "rotation_plan_id",
        Uuid(as_uuid=True),
        ForeignKey("rotation_plans.rotation_plan_id"),
        primary_key=True,
    ),
    Column(
        "position_id", Uuid(as_uuid=True), ForeignKey("positions.position_id"), primary_key=True
    ),
    Column("ordinal", Integer, nullable=False),
    UniqueConstraint(
        "rotation_plan_id",
        "ordinal",
        name="uq_rotation_plan_association_ordinal_protected_positions",
    ),
    CheckConstraint(
        "ordinal >= 0", name="ck_rotation_plan_association_ordinal_protected_positions"
    ),
)


class UserModel(Base):
    """User ownership projection."""

    __tablename__ = "users"

    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PortfolioModel(Base):
    """Portfolio ownership projection."""

    __tablename__ = "portfolios"

    portfolio_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InstrumentModel(Base):
    """Globally readable instrument definition."""

    __tablename__ = "instruments"
    __table_args__ = (UniqueConstraint("market", "symbol", name="uq_instruments_market_symbol"),)

    instrument_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    market: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PositionModel(Base):
    """Position history projection with a partial unique OPEN constraint."""

    __tablename__ = "positions"
    __table_args__ = (
        Index(
            "uq_positions_open_portfolio_instrument",
            "portfolio_id",
            "instrument_id",
            unique=True,
            postgresql_where=text("status = 'OPEN'"),
            sqlite_where=text("status = 'OPEN'"),
        ),
        CheckConstraint("status IN ('OPEN', 'CLOSED')", name="ck_positions_status"),
        CheckConstraint("quantity > 0", name="ck_positions_quantity_positive"),
        CheckConstraint(
            "(status = 'OPEN' AND closed_at IS NULL) OR "
            "(status = 'CLOSED' AND closed_at IS NOT NULL)",
            name="ck_positions_status_closed_at",
        ),
    )

    position_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.portfolio_id"), nullable=False
    )
    instrument_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("instruments.instrument_id"), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 8), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CandidateGroupModel(Base):
    """User-owned candidate group projection."""

    __tablename__ = "candidate_groups"

    candidate_group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RotationPlanModel(Base):
    """Portfolio plan projection; group membership is persisted by an association table."""

    __tablename__ = "rotation_plans"

    rotation_plan_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.portfolio_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConfigurationLayerModel(Base):
    """Versioned sparse configuration-layer projection."""

    __tablename__ = "configuration_layers"
    __table_args__ = (
        UniqueConstraint("scope", "reference_id", "version", name="uq_configuration_layer_version"),
        CheckConstraint(
            "ownership_scope IN ('SYSTEM', 'USER', 'PORTFOLIO')",
            name="ck_configuration_layer_ownership_scope",
        ),
        CheckConstraint(
            "(ownership_scope = 'SYSTEM' AND owner_id IS NULL) OR "
            "(ownership_scope IN ('USER', 'PORTFOLIO') AND owner_id IS NOT NULL)",
            name="ck_configuration_layer_owner_presence",
        ),
        CheckConstraint(
            "(scope IN ('SYSTEM', 'MARKET', 'STRATEGY') AND ownership_scope = 'SYSTEM') OR "
            "(scope = 'USER' AND ownership_scope = 'USER') OR "
            "(scope IN ('PORTFOLIO', 'ROTATION_PLAN', 'RUNTIME_OVERRIDE') "
            "AND ownership_scope = 'PORTFOLIO')",
            name="ck_configuration_layer_scope_ownership",
        ),
        CheckConstraint(
            "(ownership_scope = 'PORTFOLIO' AND portfolio_owner_id IS NOT NULL) OR "
            "(ownership_scope IN ('SYSTEM', 'USER') AND portfolio_owner_id IS NULL)",
            name="ck_configuration_layer_portfolio_owner_presence",
        ),
    )

    layer_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    reference_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    patch: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash_format_version: Mapped[str] = mapped_column(String(16), nullable=False)
    ownership_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    portfolio_owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    runtime_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ConfigurationSnapshotModel(Base):
    """Immutable full configuration evidence used by each strategy run."""

    __tablename__ = "configuration_snapshots"
    __table_args__ = (
        Index("ix_configuration_snapshots_content_hash", "content_hash"),
        Index(
            "uq_configuration_snapshot_target_version_system",
            "target_scope",
            "target_reference_id",
            "config_version",
            unique=True,
            postgresql_where=text("target_scope = 'SYSTEM' AND target_owner_id IS NULL"),
            sqlite_where=text("target_scope = 'SYSTEM' AND target_owner_id IS NULL"),
        ),
        Index(
            "uq_configuration_snapshot_target_version_owned",
            "target_scope",
            "target_owner_id",
            "target_reference_id",
            "config_version",
            unique=True,
            postgresql_where=text(
                "target_scope IN ('USER', 'PORTFOLIO') AND target_owner_id IS NOT NULL"
            ),
            sqlite_where=text(
                "target_scope IN ('USER', 'PORTFOLIO') AND target_owner_id IS NOT NULL"
            ),
        ),
        CheckConstraint("config_version > 0", name="ck_configuration_snapshot_version"),
        CheckConstraint(
            "(target_scope = 'SYSTEM' AND target_owner_id IS NULL) OR "
            "(target_scope IN ('USER', 'PORTFOLIO') AND target_owner_id IS NOT NULL)",
            name="ck_configuration_snapshot_target_owner",
        ),
    )

    snapshot_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_format_version: Mapped[str] = mapped_column(String(16), nullable=False)
    parent_versions: Mapped[list[dict[str, object]]] = mapped_column(JSON, nullable=False)
    merged_payload: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    canonical_json: Mapped[str] = mapped_column(Text, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    target_owner_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    target_reference_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    runtime_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )


class LogicalScanRunModel(Base):
    """Pre-market-data scan identity guarded by the distributed lock provider."""

    __tablename__ = "logical_scan_runs"
    __table_args__ = (
        CheckConstraint("status IN ('RUNNING', 'COMPLETED')", name="ck_logical_scan_run_status"),
        CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL) OR "
            "(status = 'COMPLETED' AND completed_at IS NOT NULL)",
            name="ck_logical_scan_run_completion",
        ),
    )

    logical_scan_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    rotation_plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rotation_plans.rotation_plan_id"), nullable=False
    )
    market_session_date: Mapped[str] = mapped_column(String(10), nullable=False)
    scan_window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scan_interval: Mapped[str] = mapped_column(String(32), nullable=False)
    market_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_identity_format_version: Mapped[str] = mapped_column(String(16), nullable=False)
    scan_lock_key: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecommendationStateModel(Base):
    """Durable recommendation lifecycle, explicitly separate from brokerage execution."""

    __tablename__ = "recommendation_states"
    __table_args__ = (
        CheckConstraint("revision >= 0", name="ck_recommendation_state_revision"),
        CheckConstraint(
            "(finalization_strategy_run_id IS NULL AND finalization_strategy_key IS NULL) OR "
            "(finalization_strategy_run_id IS NOT NULL AND finalization_strategy_key IS NOT NULL)",
            name="ck_recommendation_state_finalization_fence",
        ),
        CheckConstraint(
            "state IN ('IDLE', 'WATCHING', 'NEAR_TRIGGER', 'ACTION_PENDING', "
            "'ACTION_NOTIFIED', 'DATA_DEGRADED', 'INVALIDATED', 'PARTIALLY_EXECUTED', "
            "'WAITING_CONFIRMATION', 'STAGE_COMPLETED', 'ROTATION_COMPLETED', 'PAUSED')",
            name="ck_recommendation_state_value",
        ),
    )

    rotation_plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rotation_plans.rotation_plan_id"), primary_key=True
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.portfolio_id"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    remaining_stages_halted: Mapped[bool] = mapped_column(nullable=False)
    finalization_strategy_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategy_runs.strategy_run_id"), nullable=True
    )
    finalization_strategy_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ScanAttemptModel(Base):
    """Provider attempt evidence for one logical scan run."""

    __tablename__ = "scan_attempts"
    __table_args__ = (
        UniqueConstraint("logical_scan_run_id", "attempt_number", name="uq_scan_attempt_number"),
        UniqueConstraint(
            "logical_scan_run_id",
            "scan_attempt_id",
            name="uq_scan_attempt_same_scan_identity",
        ),
        ForeignKeyConstraint(
            ["logical_scan_run_id", "recovery_of_attempt_id"],
            ["scan_attempts.logical_scan_run_id", "scan_attempts.scan_attempt_id"],
            name="fk_scan_attempt_recovery_same_scan",
        ),
        ForeignKeyConstraint(
            ["logical_scan_run_id", "duplicate_of_attempt_id"],
            ["scan_attempts.logical_scan_run_id", "scan_attempts.scan_attempt_id"],
            name="fk_scan_attempt_duplicate_same_scan",
        ),
        ForeignKeyConstraint(
            ["logical_scan_run_id", "final_strategy_run_id"],
            ["strategy_runs.logical_scan_run_id", "strategy_runs.strategy_run_id"],
            name="fk_scan_attempt_final_strategy_same_scan",
        ),
        CheckConstraint("attempt_number > 0", name="ck_scan_attempt_number_positive"),
        CheckConstraint(
            "status IN ('RUNNING', 'FAILED', 'DEGRADED', 'SUCCEEDED')",
            name="ck_scan_attempt_status",
        ),
        CheckConstraint(
            "(status = 'RUNNING' AND completed_at IS NULL) OR "
            "(status IN ('FAILED', 'DEGRADED', 'SUCCEEDED') AND completed_at IS NOT NULL)",
            name="ck_scan_attempt_completion",
        ),
        CheckConstraint(
            "(status IN ('FAILED', 'DEGRADED') AND failure_code IS NOT NULL "
            "AND failure_detail IS NOT NULL) OR "
            "(status IN ('RUNNING', 'SUCCEEDED') AND failure_code IS NULL "
            "AND failure_detail IS NULL)",
            name="ck_scan_attempt_failure_evidence",
        ),
        CheckConstraint(
            "length(trigger_correlation_id) > 0 AND length(trigger_correlation_id) <= 256 "
            "AND length(actor_correlation_id) > 0 AND length(actor_correlation_id) <= 256 "
            "AND (failure_code IS NULL OR length(failure_code) <= 64) "
            "AND (failure_detail IS NULL OR length(failure_detail) <= 1024)",
            name="ck_scan_attempt_evidence_bounds",
        ),
    )

    scan_attempt_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    logical_scan_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("logical_scan_runs.logical_scan_run_id"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    configuration_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_snapshot_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("configuration_snapshots.snapshot_id"), nullable=True
    )
    configuration_snapshot_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    market_data_snapshot_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    market_data_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_strategy_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_strategy_identity_format_version: Mapped[str | None] = mapped_column(
        String(16), nullable=True
    )
    trigger_correlation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    actor_correlation_id: Mapped[str] = mapped_column(String(256), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    recovery_of_attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    duplicate_of_attempt_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    final_strategy_run_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)


class StrategyRunModel(Base):
    """Final strategy-result projection with a plan-local idempotency constraint."""

    __tablename__ = "strategy_runs"
    __table_args__ = (
        UniqueConstraint("rotation_plan_id", "idempotency_key", name="uq_strategy_run_key"),
        UniqueConstraint("logical_scan_run_id", name="uq_strategy_run_logical_scan"),
        UniqueConstraint(
            "logical_scan_run_id",
            "strategy_run_id",
            name="uq_strategy_run_logical_scan_identity",
        ),
    )

    strategy_run_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    rotation_plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rotation_plans.rotation_plan_id"), nullable=False
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.portfolio_id"), nullable=False
    )
    logical_scan_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("logical_scan_runs.logical_scan_run_id"), nullable=True
    )
    configuration_snapshot_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("configuration_snapshots.snapshot_id"), nullable=False
    )
    configuration_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_snapshot_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    market_data_snapshot_id: Mapped[str] = mapped_column(String(256), nullable=False)
    state_transition: Mapped[str] = mapped_column(String(128), nullable=False)
    outputs: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False)


class ChildIntentModel(Base):
    """Persisted notification fingerprints only; no destination or delivery fields exist."""

    __tablename__ = "child_intents"
    __table_args__ = (
        UniqueConstraint("intent_key", name="uq_child_intent_key"),
        UniqueConstraint("strategy_run_id", "purpose", name="uq_child_intent_run_purpose"),
    )

    child_intent_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    rotation_plan_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rotation_plans.rotation_plan_id"), nullable=False
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("portfolios.portfolio_id"), nullable=False
    )
    strategy_run_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("strategy_runs.strategy_run_id"), nullable=False
    )
    final_strategy_key: Mapped[str] = mapped_column(String(64), nullable=False)
    final_strategy_identity_format_version: Mapped[str] = mapped_column(String(16), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    intent_key: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
