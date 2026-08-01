"""Framework-independent records for immutable resolved configuration evidence."""

from dataclasses import dataclass
from uuid import UUID

from app.application.configuration import ResolvedConfigurationSnapshot
from app.domain.values import Ownership


@dataclass(frozen=True, slots=True)
class PersistedConfigurationSnapshot:
    """A resolved payload plus its versioned target ownership recorded in storage."""

    snapshot_id: UUID
    resolved_snapshot: ResolvedConfigurationSnapshot
    config_version: int
    target_ownership: Ownership
    target_reference_id: UUID

    def __post_init__(self) -> None:
        if not isinstance(self.resolved_snapshot, ResolvedConfigurationSnapshot):
            raise TypeError("resolved_snapshot must be a ResolvedConfigurationSnapshot")
        if isinstance(self.config_version, bool) or self.config_version < 1:
            raise ValueError("config_version must be positive")
        if not isinstance(self.target_ownership, Ownership):
            raise TypeError("target_ownership must be an Ownership")
        if not isinstance(self.target_reference_id, UUID):
            raise TypeError("target_reference_id must be a UUID")
