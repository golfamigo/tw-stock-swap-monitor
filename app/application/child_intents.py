"""Typed deterministic child intents; persistence only, never delivery."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from app.application.idempotency import FINAL_STRATEGY_IDENTITY_FORMAT_VERSION, child_intent_key
from app.domain.values import require_timezone_aware


class ChildIntentPurpose(StrEnum):
    """Deterministic downstream purposes that may be represented but never invoked here."""

    NOTIFICATION = "NOTIFICATION"


@dataclass(frozen=True, slots=True)
class ChildIntent:
    """An immutable, idempotent notification-intent record with no destination or payload."""

    child_intent_id: UUID
    rotation_plan_id: UUID
    portfolio_id: UUID
    strategy_run_id: UUID
    final_strategy_key: str
    final_strategy_identity_format_version: str
    purpose: ChildIntentPurpose
    intent_key: str
    created_at: datetime

    def __post_init__(self) -> None:
        _require_sha256(self.final_strategy_key, field_name="final_strategy_key")
        _require_sha256(self.intent_key, field_name="intent_key")
        if self.final_strategy_identity_format_version != FINAL_STRATEGY_IDENTITY_FORMAT_VERSION:
            raise ValueError("child intent uses an unsupported final strategy identity format")
        if not isinstance(self.purpose, ChildIntentPurpose):
            raise TypeError("purpose must be a ChildIntentPurpose")
        if self.intent_key != child_intent_key(self.final_strategy_key, self.purpose.value):
            raise ValueError("intent_key must match the final strategy key and purpose")
        require_timezone_aware(self.created_at, field_name="created_at")


def build_child_intent(
    *,
    child_intent_id: UUID,
    rotation_plan_id: UUID,
    portfolio_id: UUID,
    strategy_run_id: UUID,
    final_strategy_key: str,
    purpose: ChildIntentPurpose,
    created_at: datetime,
) -> ChildIntent:
    """Build a notification fingerprint without producing, routing, or delivering a notification."""

    return ChildIntent(
        child_intent_id=child_intent_id,
        rotation_plan_id=rotation_plan_id,
        portfolio_id=portfolio_id,
        strategy_run_id=strategy_run_id,
        final_strategy_key=final_strategy_key,
        final_strategy_identity_format_version=FINAL_STRATEGY_IDENTITY_FORMAT_VERSION,
        purpose=purpose,
        intent_key=child_intent_key(final_strategy_key, purpose.value),
        created_at=created_at,
    )


def _require_sha256(value: str, *, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError(f"{field_name} must be a SHA-256 hexadecimal digest")
