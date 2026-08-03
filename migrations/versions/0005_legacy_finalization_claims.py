"""Fence legacy 0002 finalization recovery to one verified strategy run.

Revision ID: 0005_legacy_finalization_claims
Revises: 0004_finalization_outcome
Create Date: 2026-08-03
"""

from collections.abc import Mapping, Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op

revision: str = "0005_legacy_finalization_claims"
down_revision: str | None = "0004_finalization_outcome"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLAIMED = "CLAIMED"
_NO_CLAIM = "NO_CLAIM"
_AMBIGUOUS = "AMBIGUOUS"
_NOT_APPLICABLE = "NOT_APPLICABLE"


def _uuid() -> sa.Uuid:
    """Keep the claim reference compatible with the existing UUID migrations."""

    return sa.Uuid(as_uuid=True)


def upgrade() -> None:
    """Backfill only uniquely verifiable pre-fence 0002 finalization candidates."""

    with op.batch_alter_table("recommendation_states") as batch:
        batch.add_column(
            sa.Column(
                "legacy_finalization_claim_status",
                sa.String(length=16),
                nullable=False,
                server_default=_NO_CLAIM,
            )
        )
        batch.add_column(sa.Column("legacy_finalization_strategy_run_id", _uuid(), nullable=True))
        batch.add_column(
            sa.Column("legacy_finalization_strategy_key", sa.String(length=64), nullable=True)
        )
        batch.create_foreign_key(
            "fk_recommendation_state_legacy_finalization_strategy_run",
            "strategy_runs",
            ["legacy_finalization_strategy_run_id"],
            ["strategy_run_id"],
        )
        batch.create_unique_constraint(
            "uq_recommendation_state_legacy_finalization_strategy_run",
            ["legacy_finalization_strategy_run_id"],
        )
        batch.create_check_constraint(
            "ck_recommendation_state_legacy_claim_status",
            "legacy_finalization_claim_status IN "
            "('NOT_APPLICABLE', 'CLAIMED', 'NO_CLAIM', 'AMBIGUOUS')",
        )
        batch.create_check_constraint(
            "ck_recommendation_state_legacy_claim_evidence",
            "(legacy_finalization_claim_status = 'CLAIMED' "
            "AND legacy_finalization_strategy_run_id IS NOT NULL "
            "AND legacy_finalization_strategy_key IS NOT NULL) OR "
            "(legacy_finalization_claim_status != 'CLAIMED' "
            "AND legacy_finalization_strategy_run_id IS NULL "
            "AND legacy_finalization_strategy_key IS NULL)",
        )
    _backfill_legacy_claims()


def downgrade() -> None:
    """Remove only the additive provenance marker."""

    with op.batch_alter_table("recommendation_states") as batch:
        batch.drop_constraint("ck_recommendation_state_legacy_claim_evidence", type_="check")
        batch.drop_constraint("ck_recommendation_state_legacy_claim_status", type_="check")
        batch.drop_constraint(
            "fk_recommendation_state_legacy_finalization_strategy_run",
            type_="foreignkey",
        )
        batch.drop_constraint(
            "uq_recommendation_state_legacy_finalization_strategy_run",
            type_="unique",
        )
        batch.drop_column("legacy_finalization_strategy_key")
        batch.drop_column("legacy_finalization_strategy_run_id")
        batch.drop_column("legacy_finalization_claim_status")


def _backfill_legacy_claims() -> None:
    """Classify each existing state without guessing which concurrent run changed it."""

    bind = op.get_bind()
    metadata = sa.MetaData()
    states = sa.Table("recommendation_states", metadata, autoload_with=bind)
    scans = sa.Table("logical_scan_runs", metadata, autoload_with=bind)
    runs = sa.Table("strategy_runs", metadata, autoload_with=bind)
    attempts = sa.Table("scan_attempts", metadata, autoload_with=bind)
    intents = sa.Table("child_intents", metadata, autoload_with=bind)

    for state in bind.execute(sa.select(states)).mappings():
        if (
            state["finalization_strategy_run_id"] is not None
            or state["finalization_strategy_key"] is not None
        ):
            _write_claim(bind, states, state, status=_NOT_APPLICABLE)
            continue
        candidates = _legacy_candidates(
            bind=bind,
            state=state,
            scans=scans,
            runs=runs,
            attempts=attempts,
            intents=intents,
        )
        if len(candidates) == 1:
            run_id, final_key = candidates[0]
            _write_claim(
                bind,
                states,
                state,
                status=_CLAIMED,
                strategy_run_id=run_id,
                final_strategy_key=final_key,
            )
        elif len(candidates) > 1:
            _write_claim(bind, states, state, status=_AMBIGUOUS)
        else:
            _write_claim(bind, states, state, status=_NO_CLAIM)


def _legacy_candidates(
    *,
    bind: sa.Connection,
    state: Mapping[str, object],
    scans: sa.Table,
    runs: sa.Table,
    attempts: sa.Table,
    intents: sa.Table,
) -> list[tuple[object, str]]:
    """Return only candidates whose immutable evidence proves a single legacy owner."""

    rows = bind.execute(
        sa.select(
            runs.c.strategy_run_id.label("strategy_run_id"),
            runs.c.portfolio_id.label("run_portfolio_id"),
            runs.c.configuration_content_hash.label("configuration_content_hash"),
            runs.c.outputs.label("outputs"),
            runs.c.state_transition.label("state_transition"),
            scans.c.logical_scan_run_id.label("logical_scan_run_id"),
            scans.c.configuration_snapshot_hash.label("scan_configuration_hash"),
            scans.c.scan_lock_key.label("scan_lock_key"),
        )
        .select_from(runs.join(scans, runs.c.logical_scan_run_id == scans.c.logical_scan_run_id))
        .where(
            runs.c.rotation_plan_id == state["rotation_plan_id"],
            runs.c.portfolio_id == state["portfolio_id"],
            scans.c.rotation_plan_id == state["rotation_plan_id"],
            scans.c.status == "RUNNING",
        )
    ).mappings()
    candidates: list[tuple[object, str]] = []
    for row in rows:
        if row["configuration_content_hash"] != row["scan_configuration_hash"]:
            continue
        candidate = _candidate_from_outputs(state=state, row=row)
        if candidate is None:
            continue
        strategy_run_id, final_strategy_key = candidate
        final_success = bind.scalar(
            sa.select(attempts.c.scan_attempt_id)
            .where(
                attempts.c.logical_scan_run_id == row["logical_scan_run_id"],
                attempts.c.status == "SUCCEEDED",
                attempts.c.final_strategy_run_id == strategy_run_id,
            )
            .limit(1)
        )
        if final_success is not None:
            continue
        notification = bind.scalar(
            sa.select(intents.c.child_intent_id)
            .where(
                intents.c.rotation_plan_id == state["rotation_plan_id"],
                intents.c.portfolio_id == state["portfolio_id"],
                intents.c.strategy_run_id == strategy_run_id,
                intents.c.final_strategy_key == final_strategy_key,
                intents.c.purpose == "NOTIFICATION",
            )
            .limit(1)
        )
        if notification is None:
            continue
        candidates.append((strategy_run_id, final_strategy_key))
    return candidates


def _candidate_from_outputs(
    *, state: Mapping[str, object], row: Mapping[str, object]
) -> tuple[object, str] | None:
    """Validate the immutable 0002 run evidence without decoding application models."""

    outputs = row["outputs"]
    if not isinstance(outputs, Mapping):
        return None
    recommendation = outputs.get("recommendation")
    idempotency = outputs.get("idempotency")
    if not isinstance(recommendation, Mapping) or not isinstance(idempotency, Mapping):
        return None
    previous_state = recommendation.get("previous_state")
    next_state = recommendation.get("next_state")
    previous_halted = recommendation.get("previous_remaining_stages_halted")
    remaining_halted = recommendation.get("remaining_stages_halted")
    notification_recorded = recommendation.get("notification_intent_recorded")
    final_strategy_key = idempotency.get("final_strategy_key")
    scan_lock_key = idempotency.get("scan_lock_key")
    if (
        not isinstance(previous_state, str)
        or not isinstance(next_state, str)
        or type(previous_halted) is not bool
        or type(remaining_halted) is not bool
        or notification_recorded is not True
        or not _sha256(final_strategy_key)
        or scan_lock_key != row["scan_lock_key"]
        or next_state != state["state"]
        or remaining_halted != state["remaining_stages_halted"]
        or previous_state == next_state
        and previous_halted == remaining_halted
        or row["state_transition"] != f"{previous_state}->{next_state}"
    ):
        return None
    strategy_run_id = row["strategy_run_id"]
    if _as_uuid(strategy_run_id) is None:
        return None
    return strategy_run_id, final_strategy_key


def _write_claim(
    bind: sa.Connection,
    states: sa.Table,
    state: Mapping[str, object],
    *,
    status: str,
    strategy_run_id: object | None = None,
    final_strategy_key: str | None = None,
) -> None:
    """Write a complete claim tuple, never a partially inferred marker."""

    bind.execute(
        states.update()
        .where(states.c.rotation_plan_id == state["rotation_plan_id"])
        .values(
            legacy_finalization_claim_status=status,
            legacy_finalization_strategy_run_id=strategy_run_id,
            legacy_finalization_strategy_key=final_strategy_key,
        )
    )


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.lower())
    )


def _as_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None
