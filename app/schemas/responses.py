"""Response-boundary schemas for immutable resolved configuration metadata."""

from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.common import ParentVersion


class ResolvedConfigurationResponseSchema(BaseModel):
    """The serializable metadata and complete payload of a resolved snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    payload: Mapping[str, object]
    parent_versions: tuple[ParentVersion, ...]
    created_by: UUID
    created_at: datetime
    runtime_expires_at: datetime | None
    canonical_format_version: str
    content_hash: str
