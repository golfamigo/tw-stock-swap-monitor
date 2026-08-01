"""Closed sets used by the pure domain model."""

from enum import Enum


class Scope(str, Enum):
    """The ownership boundary assigned to a domain resource."""

    SYSTEM = "SYSTEM"
    USER = "USER"
    PORTFOLIO = "PORTFOLIO"


class PositionRole(str, Enum):
    """A position's strategy role, independent of its actual holding state."""

    ROTATION_SOURCE = "ROTATION_SOURCE"
    PROTECTED_CORE = "PROTECTED_CORE"
    NORMAL = "NORMAL"
    CASH_PROXY = "CASH_PROXY"


class PositionStatus(str, Enum):
    """The persisted holding lifecycle state."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
