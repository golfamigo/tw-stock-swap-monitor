"""Explicit errors raised when deterministic domain rules are violated."""


class DomainError(ValueError):
    """Base class for expected domain validation and invariant failures."""


class AuthorizationDenied(DomainError):
    """The actor may identify a resource but cannot perform the requested mutation."""


class NotFoundForActor(DomainError):
    """A resource is absent or intentionally hidden from the requesting actor."""


class NaiveDatetimeError(DomainError):
    """A domain timestamp omitted timezone information."""


class NonDecimalValueError(DomainError):
    """A money-like value was not supplied as a Decimal instance."""


class NonFiniteDecimalError(DomainError):
    """A money-like Decimal was NaN or infinite."""


class NegativeQuantityError(DomainError):
    """A quantity was negative."""


class ProtectedPositionSaleError(DomainError):
    """A protected core position was offered as a sale source."""


class PositionNotOpenError(DomainError):
    """An action requiring an open position was attempted on a closed position."""


class SaleQuantityExceedsPositionError(DomainError):
    """A proposed sale would exceed the currently held quantity."""


class PlanReferenceUnauthorizedError(DomainError):
    """A plan attempted to use a resource outside its ownership boundary."""


class CandidateInstrumentUnauthorizedError(DomainError):
    """An instrument was not in the candidate group selected by a plan."""


class PositionAlreadyOpenError(DomainError):
    """A portfolio would have more than one open position for one instrument."""


class PositionHistoryError(DomainError):
    """Open and closed timestamps do not represent a valid position history."""


class InvalidStrategyRunEvidenceError(DomainError):
    """Strategy run evidence contained an unsupported mutable or invalid value."""


class LogicalScanAlreadyCompletedError(DomainError):
    """A logical scan already has its single permitted final strategy result."""


class IdempotencyConflictError(DomainError):
    """One idempotency key was reused with a materially different strategy run."""
