"""Request-boundary schemas for command-local configuration resolution."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.configuration import LayerPatchSchema


class RuntimeOverrideRequestSchema(BaseModel):
    """A non-persisted patch that is valid only until its explicit expiry."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    patch: LayerPatchSchema
    expires_at: datetime
