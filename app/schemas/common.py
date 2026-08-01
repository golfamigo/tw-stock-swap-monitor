"""Common configuration boundary types."""

from dataclasses import dataclass
from enum import Enum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import Scope


class ConfigurationLayerScope(str, Enum):
    """The fixed configuration precedence order."""

    SYSTEM = "SYSTEM"
    MARKET = "MARKET"
    STRATEGY = "STRATEGY"
    USER = "USER"
    PORTFOLIO = "PORTFOLIO"
    ROTATION_PLAN = "ROTATION_PLAN"
    RUNTIME_OVERRIDE = "RUNTIME_OVERRIDE"


EXPECTED_OWNERSHIP_SCOPE_BY_LAYER: Final[dict[ConfigurationLayerScope, Scope]] = {
    ConfigurationLayerScope.SYSTEM: Scope.SYSTEM,
    ConfigurationLayerScope.MARKET: Scope.SYSTEM,
    ConfigurationLayerScope.STRATEGY: Scope.SYSTEM,
    ConfigurationLayerScope.USER: Scope.USER,
    ConfigurationLayerScope.PORTFOLIO: Scope.PORTFOLIO,
    ConfigurationLayerScope.ROTATION_PLAN: Scope.PORTFOLIO,
    ConfigurationLayerScope.RUNTIME_OVERRIDE: Scope.PORTFOLIO,
}


class DeleteDirective(BaseModel):
    """An explicit deletion of an inherited optional map entry."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    delete: Literal[True] = Field(alias="$delete")


@dataclass(frozen=True, slots=True)
class ParentVersion:
    """An ordered immutable identity for one layer contributing to a snapshot."""

    scope: ConfigurationLayerScope
    reference_id: UUID
    version: int
    content_hash: str
    content_hash_format_version: str
