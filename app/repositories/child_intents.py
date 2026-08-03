"""Typed repository port for deterministic, non-delivered child intents."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from app.domain.access import AccessContext

if TYPE_CHECKING:
    from app.application.child_intents import ChildIntent


class ChildIntentRepository(Protocol):
    """Persist one immutable child fingerprint per final strategy key and purpose."""

    def record_or_get(self, *, intent: ChildIntent, access_context: AccessContext) -> ChildIntent:
        """Return an exact existing intent rather than creating a duplicate side effect."""

    def get(self, *, intent_key: str, access_context: AccessContext) -> ChildIntent:
        """Load one authorized deterministic intent by its non-secret fingerprint."""

    def list_for_strategy_run(
        self, *, strategy_run_id: UUID, access_context: AccessContext
    ) -> Sequence[ChildIntent]:
        """Return immutable child-intent evidence for one final strategy run."""
