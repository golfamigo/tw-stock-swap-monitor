"""Lock port for coordinating one logical market scan at a time."""

import json
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.access import AccessContext
from app.domain.entities import RotationPlan
from app.domain.values import require_timezone_aware


@dataclass(frozen=True, slots=True)
class ScanIdentity:
    """Typed, canonical identity for pre-market-data scan coordination."""

    rotation_plan_id: UUID
    market_session_date: date
    scan_window_start: datetime
    scan_interval: str
    configuration_snapshot_hash: str
    market_timezone: str

    def __post_init__(self) -> None:
        require_timezone_aware(self.scan_window_start, field_name="scan_window_start")
        if not self.scan_interval.strip():
            raise ValueError("scan_interval must not be blank")
        if len(self.configuration_snapshot_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in self.configuration_snapshot_hash.lower()
        ):
            raise ValueError("configuration_snapshot_hash must be a SHA-256 hexadecimal digest")
        try:
            timezone = ZoneInfo(self.market_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("market_timezone must be a valid IANA timezone") from error
        object.__setattr__(self, "market_timezone", timezone.key)

    @property
    def key(self) -> str:
        """Return an opaque SHA-256 key from a delimiter-safe canonical JSON identity."""

        market_window_start = self.scan_window_start.astimezone(ZoneInfo(self.market_timezone))
        payload = {
            "configuration_snapshot_hash": self.configuration_snapshot_hash.lower(),
            "market_session_date": self.market_session_date.isoformat(),
            "market_timezone": self.market_timezone,
            "rotation_plan_id": str(self.rotation_plan_id),
            "scan_interval": self.scan_interval,
            "scan_window_start": market_window_start.isoformat(timespec="microseconds"),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ScanLockRequest:
    """The preliminary scan identity known before a market snapshot is fetched."""

    plan: RotationPlan
    market_session_date: str
    scan_window_start: datetime
    scan_interval: str
    market_timezone: str
    configuration_snapshot_hash: str

    def __post_init__(self) -> None:
        require_timezone_aware(self.scan_window_start, field_name="scan_window_start")
        if not self.market_session_date.strip() or not self.scan_interval.strip():
            raise ValueError("scan lock fields must not be blank")
        try:
            session_date = date.fromisoformat(self.market_session_date)
        except ValueError as error:
            raise ValueError("market_session_date must be an ISO date") from error
        identity = ScanIdentity(
            rotation_plan_id=self.plan.rotation_plan_id,
            market_session_date=session_date,
            scan_window_start=self.scan_window_start,
            scan_interval=self.scan_interval,
            configuration_snapshot_hash=self.configuration_snapshot_hash,
            market_timezone=self.market_timezone,
        )
        object.__setattr__(self, "market_session_date", identity.market_session_date.isoformat())
        object.__setattr__(self, "market_timezone", identity.market_timezone)

    @property
    def identity(self) -> ScanIdentity:
        """Expose the validated value object used by all scan coordination adapters."""

        return ScanIdentity(
            rotation_plan_id=self.plan.rotation_plan_id,
            market_session_date=date.fromisoformat(self.market_session_date),
            scan_window_start=self.scan_window_start,
            scan_interval=self.scan_interval,
            configuration_snapshot_hash=self.configuration_snapshot_hash,
            market_timezone=self.market_timezone,
        )

    @property
    def key(self) -> str:
        """Return the stable preliminary key used to guard provider work."""

        return self.identity.key


@dataclass(frozen=True, slots=True)
class LockLease:
    """Opaque acquired lease that only the creating provider may release."""

    lease_id: UUID
    key: str


class LockProvider(Protocol):
    """Acquire a preliminary scan lock using an authenticated portfolio plan."""

    def acquire_scan_lock(
        self, request: ScanLockRequest, *, access_context: AccessContext
    ) -> LockLease | None:
        """Return one lease or ``None`` when another worker already owns it."""

    def release(self, lease: LockLease, *, access_context: AccessContext) -> None:
        """Release an acquired lease after re-authorizing its portfolio scope."""
