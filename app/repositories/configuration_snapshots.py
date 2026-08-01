"""Port for immutable, canonical configuration snapshot evidence."""

from typing import Protocol
from uuid import UUID

from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.domain.access import AccessContext


class ConfigurationSnapshotRepository(Protocol):
    """Persist and retrieve immutable configuration evidence with scoped authorization."""

    def record_or_get(
        self,
        *,
        snapshot: PersistedConfigurationSnapshot,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        """Return an existing snapshot only when the exact immutable record is retried."""

    def get(
        self,
        snapshot_id: UUID,
        *,
        access_context: AccessContext,
    ) -> PersistedConfigurationSnapshot:
        """Load one snapshot after enforcing access to its target ownership."""
