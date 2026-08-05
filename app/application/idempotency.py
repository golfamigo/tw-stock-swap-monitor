"""Canonical two-phase identities for pre-fetch scans and final strategy runs."""

import json
from dataclasses import dataclass
from hashlib import sha256

from app.data_sources.models import MarketDataSnapshot
from app.domain.values import IdempotencyKey
from app.repositories.locks import ScanLockRequest

FINAL_STRATEGY_IDENTITY_FORMAT_VERSION = "strategy-run:v1"


def build_scan_lock_key(request: ScanLockRequest) -> str:
    """Centralize the existing versioned scan identity without creating a competing format."""

    if not isinstance(request, ScanLockRequest):
        raise TypeError("request must be a ScanLockRequest")
    return request.key


@dataclass(frozen=True, slots=True)
class FinalStrategyIdentity:
    """Versioned reproducibility identity available only after snapshot evidence exists."""

    scan_lock_key: str
    market_data_snapshot_id: str
    market_data_content_hash: str
    format_version: str = FINAL_STRATEGY_IDENTITY_FORMAT_VERSION

    def __post_init__(self) -> None:
        _require_sha256(self.scan_lock_key, field_name="scan_lock_key")
        _require_sha256(self.market_data_content_hash, field_name="market_data_content_hash")
        if self.format_version != FINAL_STRATEGY_IDENTITY_FORMAT_VERSION:
            raise ValueError("final strategy identity format_version is not supported")
        _validate_snapshot_id(self.market_data_snapshot_id, self.market_data_content_hash)
        object.__setattr__(self, "scan_lock_key", self.scan_lock_key.lower())
        object.__setattr__(self, "market_data_content_hash", self.market_data_content_hash.lower())

    @property
    def canonical_payload(self) -> dict[str, str]:
        """Return the canonical JSON components retained in audit evidence."""

        return {
            "format_version": self.format_version,
            "market_data_content_hash": self.market_data_content_hash,
            "market_data_snapshot_id": self.market_data_snapshot_id,
            "scan_lock_key": self.scan_lock_key,
        }

    @property
    def key(self) -> str:
        """Return the SHA-256 final strategy-run key for one exact market snapshot."""

        canonical_json = json.dumps(
            self.canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return sha256(canonical_json.encode("utf-8")).hexdigest()

    def as_idempotency_key(self) -> IdempotencyKey:
        """Expose the final identity through the existing strategy-run repository value type."""

        return IdempotencyKey(self.key)

    @classmethod
    def from_snapshot(
        cls, scan_lock_key: str, snapshot: MarketDataSnapshot
    ) -> "FinalStrategyIdentity":
        """Create the final identity from immutable market-data evidence."""

        if not isinstance(snapshot, MarketDataSnapshot):
            raise TypeError("snapshot must be a MarketDataSnapshot")
        return cls(
            scan_lock_key=scan_lock_key,
            market_data_snapshot_id=snapshot.snapshot_id,
            market_data_content_hash=snapshot.content_hash,
        )


def build_final_strategy_key(
    *,
    scan_lock_key: str,
    market_data_snapshot_id: str,
    market_data_content_hash: str,
) -> str:
    """Build the final key explicitly for adapters that only persist scalar components."""

    return FinalStrategyIdentity(
        scan_lock_key=scan_lock_key,
        market_data_snapshot_id=market_data_snapshot_id,
        market_data_content_hash=market_data_content_hash,
    ).key


def child_intent_key(final_strategy_key: str, purpose: str) -> str:
    """Derive a deterministic child-intent key without invoking an LLM or notifier."""

    _require_sha256(final_strategy_key, field_name="final_strategy_key")
    if not purpose.strip():
        raise ValueError("purpose must not be blank")
    payload = json.dumps(
        {"final_strategy_key": final_strategy_key.lower(), "purpose": purpose.strip()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _require_sha256(value: str, *, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value.lower()):
        raise ValueError(f"{field_name} must be a SHA-256 hexadecimal digest")


def _validate_snapshot_id(snapshot_id: str, content_hash: str) -> None:
    parts = snapshot_id.split(":")
    if len(parts) != 3 or parts[0] != "market-data" or parts[1] != "v1":
        raise ValueError("market_data_snapshot_id must use the market-data:v1 format")
    if parts[2].lower() != content_hash.lower():
        raise ValueError("market_data_snapshot_id must reference market_data_content_hash")
