"""Collision-safe, reversible JSON representation for immutable run evidence."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from app.domain.errors import InvalidStrategyRunEvidenceError
from app.domain.values import require_finite_decimal, require_timezone_aware

_TAG = "$evidence"


def encode_evidence(value: object) -> object:
    """Encode supported domain evidence into a strict JSON-compatible value tree.

    Containers and non-JSON scalar types are always wrapped, so a user mapping whose
    keys resemble this codec's tag is never interpreted as a control envelope.
    """

    if isinstance(value, Enum):
        raise InvalidStrategyRunEvidenceError("evidence Enum values are not supported")
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float | bytes | bytearray | memoryview | set | frozenset):
        raise InvalidStrategyRunEvidenceError(
            f"unsupported evidence persistence type: {type(value).__name__}"
        )
    if isinstance(value, Decimal):
        require_finite_decimal(value, field_name="evidence Decimal")
        return {_TAG: "decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {_TAG: "uuid", "value": str(value)}
    if isinstance(value, datetime):
        require_timezone_aware(value, field_name="evidence datetime")
        return {_TAG: "datetime", "value": value.isoformat()}
    if isinstance(value, Mapping):
        entries: list[list[object]] = []
        for key, nested in value.items():
            if not isinstance(key, str):
                raise InvalidStrategyRunEvidenceError("evidence mapping keys must be strings")
            entries.append([key, encode_evidence(nested)])
        return {_TAG: "map", "entries": entries}
    if isinstance(value, list):
        return {_TAG: "list", "items": [encode_evidence(item) for item in value]}
    if isinstance(value, tuple):
        return {_TAG: "tuple", "items": [encode_evidence(item) for item in value]}
    raise InvalidStrategyRunEvidenceError(
        f"unsupported evidence persistence type: {type(value).__name__}"
    )


def _require_exact_mapping(value: object, *, expected_keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("stored evidence envelope must be a mapping")
    if set(value) != expected_keys or any(not isinstance(key, str) for key in value):
        raise ValueError("stored evidence envelope has an invalid shape")
    return value


def _decode_items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("stored evidence items must be a list")
    return [decode_evidence(item) for item in value]


def decode_evidence(value: object) -> object:
    """Decode evidence produced by :func:`encode_evidence`, rejecting malformed data."""

    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, list | tuple):
        raise ValueError("stored evidence containers must use an explicit envelope")
    if not isinstance(value, Mapping):
        raise ValueError("stored evidence contains an unsupported JSON value")
    tag = value.get(_TAG)
    if not isinstance(tag, str):
        raise ValueError("stored evidence envelope is missing its type tag")
    if tag == "decimal":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored decimal evidence must be a string")
        try:
            decimal = Decimal(rendered)
        except InvalidOperation as error:
            raise ValueError("stored decimal evidence is invalid") from error
        try:
            return require_finite_decimal(decimal, field_name="stored evidence Decimal")
        except ValueError as error:
            raise ValueError("stored decimal evidence must be finite") from error
    if tag == "uuid":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored UUID evidence must be a string")
        try:
            return UUID(rendered)
        except ValueError as error:
            raise ValueError("stored UUID evidence is invalid") from error
    if tag == "datetime":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored datetime evidence must be a string")
        try:
            timestamp = datetime.fromisoformat(rendered)
        except ValueError as error:
            raise ValueError("stored datetime evidence is invalid") from error
        try:
            return require_timezone_aware(timestamp, field_name="stored evidence datetime")
        except ValueError as error:
            raise ValueError("stored datetime evidence must be timezone-aware") from error
    if tag == "map":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "entries"}))
        raw_entries = envelope["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("stored evidence map entries must be a list")
        decoded: dict[str, object] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise ValueError("stored evidence map entry must be a pair")
            key, nested = raw_entry
            if not isinstance(key, str) or key in decoded:
                raise ValueError("stored evidence map keys must be unique strings")
            decoded[key] = decode_evidence(nested)
        return decoded
    if tag == "list":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "items"}))
        return _decode_items(envelope["items"])
    if tag == "tuple":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "items"}))
        return tuple(_decode_items(envelope["items"]))
    raise ValueError("stored evidence envelope has an unsupported type tag")
