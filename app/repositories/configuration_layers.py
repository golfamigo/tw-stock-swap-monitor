"""Port for loading explicit versioned configuration evidence for one rotation plan."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.configuration import ConfigurationLayer
from app.domain.access import AccessContext
from app.domain.entities import RotationPlan
from app.schemas.common import ConfigurationLayerScope

_HIERARCHY_ORDER = {
    ConfigurationLayerScope.SYSTEM: 0,
    ConfigurationLayerScope.MARKET: 1,
    ConfigurationLayerScope.STRATEGY: 2,
    ConfigurationLayerScope.USER: 3,
    ConfigurationLayerScope.PORTFOLIO: 4,
    ConfigurationLayerScope.ROTATION_PLAN: 5,
    ConfigurationLayerScope.RUNTIME_OVERRIDE: 6,
}


@dataclass(frozen=True, slots=True)
class ConfigurationLayerSelectionEntry:
    """One exact immutable layer identity selected for a deterministic strategy run."""

    scope: ConfigurationLayerScope
    reference_id: UUID
    version: int
    content_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ConfigurationLayerScope):
            raise TypeError("scope must be a ConfigurationLayerScope")
        if isinstance(self.version, bool) or self.version < 1:
            raise ValueError("version must be positive")
        normalized_hash = self.content_hash.lower()
        if len(normalized_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_hash
        ):
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest")
        object.__setattr__(self, "content_hash", normalized_hash)


@dataclass(frozen=True, slots=True)
class ConfigurationLayerSelection:
    """Exact, hierarchy-ordered layer identities for one resolution attempt."""

    entries: tuple[ConfigurationLayerSelectionEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "entries", tuple(self.entries))
        scopes = [entry.scope for entry in self.entries]
        if len(set(scopes)) != len(scopes):
            raise ValueError("duplicate configuration selection scope")

    @property
    def ordered_entries(self) -> tuple[ConfigurationLayerSelectionEntry, ...]:
        """Return entries in architecture-defined configuration precedence order."""

        return tuple(sorted(self.entries, key=lambda entry: _HIERARCHY_ORDER[entry.scope]))

    def validate_for_plan(self, plan: RotationPlan) -> None:
        """Reject plan-bound selection entries before any persistence query runs."""

        for entry in self.entries:
            if (
                entry.scope
                in {
                    ConfigurationLayerScope.ROTATION_PLAN,
                    ConfigurationLayerScope.RUNTIME_OVERRIDE,
                }
                and entry.reference_id != plan.rotation_plan_id
            ):
                raise ValueError("plan-bound configuration selection reference does not match plan")


class ConfigurationLayerRepository(Protocol):
    """Load configuration evidence without allowing sibling portfolio leakage."""

    def add(self, layer: ConfigurationLayer, *, access_context: AccessContext) -> None:
        """Persist one validated layer after mutation authorization."""

    def load_for_plan(
        self,
        *,
        plan: RotationPlan,
        selection: ConfigurationLayerSelection,
        access_context: AccessContext,
    ) -> Sequence[ConfigurationLayer]:
        """Return only the exact selected layers for this exact plan portfolio."""
