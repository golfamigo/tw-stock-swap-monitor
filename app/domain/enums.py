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


class LogicalScanStatus(str, Enum):
    """The persisted lifecycle of one pre-market-data logical scan."""

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"


class ScanAttemptStatus(str, Enum):
    """The terminal or in-progress state of one market-data provider attempt."""

    RUNNING = "RUNNING"
    FAILED = "FAILED"
    DEGRADED = "DEGRADED"
    SUCCEEDED = "SUCCEEDED"


class FinalizationDisposition(str, Enum):
    """The durable outcome of a final strategy candidate's coordinator finish."""

    APPLIED = "APPLIED"
    SUPERSEDED = "SUPERSEDED"


class ScanAttemptRecoveryDecision(str, Enum):
    """The deterministic worker action allowed after observing an attempt."""

    RESUME = "RESUME"
    RETRY = "RETRY"
    FINALIZE = "FINALIZE"
