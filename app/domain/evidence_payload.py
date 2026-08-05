"""Bounded, reversible evidence encoding shared by domain and persistence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, DecimalTuple, InvalidOperation
from enum import Enum
from uuid import UUID

from app.domain.errors import InvalidStrategyRunEvidenceError

MAX_IDENTIFIER_UTF8_BYTES = 256
MAX_STRING_UTF8_BYTES = 16 * 1024
MAX_STRATEGY_RUN_OUTPUT_UTF8_BYTES = 256 * 1024
MAX_STRATEGY_RUN_OUTPUT_DEPTH = 32
MAX_STRATEGY_RUN_OUTPUT_NODES = 8_192
MAX_EVIDENCE_DECIMAL_COEFFICIENT_DIGITS = 256
MAX_EVIDENCE_DECIMAL_ABSOLUTE_EXPONENT = 256
MAX_EVIDENCE_DECIMAL_SERIALIZED_CHARACTERS = 512

_TAG = "$evidence"
_JSON_ENCODER = json.JSONEncoder(
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
)


class CanonicalJsonSizeLimitError(InvalidStrategyRunEvidenceError):
    """A bounded canonical JSON representation exceeded its safe byte budget."""

    def __init__(
        self,
        *,
        boundary: str,
        limit_bytes: int,
        observed_at_least_bytes: int,
    ) -> None:
        self.boundary = boundary
        self.limit_bytes = limit_bytes
        self.observed_at_least_bytes = observed_at_least_bytes
        super().__init__(
            "canonical JSON size limit exceeded: "
            f"boundary={boundary}, limit_bytes={limit_bytes}, "
            f"observed_at_least_bytes={observed_at_least_bytes}"
        )


def preflight_evidence_payload(value: object) -> None:
    """Reject unsafe evidence before recursive freezing or persistence encoding.

    The walk is iterative, counts both mappings and sequences toward depth, and only
    tracks active containers for cycle detection. Reusing a value in separate
    branches is allowed; recursive references are not.
    """

    stack: list[tuple[object, int, bool]] = [(value, 1, False)]
    active_container_ids: set[int] = set()
    node_count = 0
    while stack:
        current, depth, is_exit = stack.pop()
        if is_exit:
            active_container_ids.remove(id(current))
            continue
        if depth > MAX_STRATEGY_RUN_OUTPUT_DEPTH:
            raise InvalidStrategyRunEvidenceError(
                f"evidence depth exceeds {MAX_STRATEGY_RUN_OUTPUT_DEPTH}"
            )
        node_count += 1
        if node_count > MAX_STRATEGY_RUN_OUTPUT_NODES:
            raise InvalidStrategyRunEvidenceError(
                f"evidence node count exceeds {MAX_STRATEGY_RUN_OUTPUT_NODES}"
            )
        if isinstance(current, Enum):
            raise InvalidStrategyRunEvidenceError("evidence Enum values are not supported")
        if isinstance(current, Mapping):
            _enter_container(current, active_container_ids)
            stack.append((current, depth, True))
            items = tuple(current.items())
            for key, nested in reversed(items):
                if not isinstance(key, str):
                    raise InvalidStrategyRunEvidenceError("evidence mapping keys must be strings")
                _require_evidence_string(key, field_name="evidence mapping key")
                stack.append((nested, depth + 1, False))
            continue
        if isinstance(current, list | tuple):
            _enter_container(current, active_container_ids)
            stack.append((current, depth, True))
            for nested in reversed(current):
                stack.append((nested, depth + 1, False))
            continue
        if isinstance(current, set | frozenset):
            raise InvalidStrategyRunEvidenceError("evidence must not contain unordered set values")
        if isinstance(current, Decimal):
            require_bounded_evidence_decimal(current)
            continue
        if isinstance(current, datetime):
            _require_aware_datetime(current, field_name="evidence datetime")
            continue
        if isinstance(current, bytes | bytearray | memoryview | float):
            raise InvalidStrategyRunEvidenceError(
                f"unsupported evidence type: {type(current).__name__}"
            )
        if current is None or isinstance(current, bool | str | int | UUID):
            if isinstance(current, str):
                _require_evidence_string(current, field_name="evidence string")
            continue
        raise InvalidStrategyRunEvidenceError(
            f"unsupported mutable or non-deterministic evidence value: {type(current).__name__}"
        )


def measure_json_bytes(value: object, *, limit_bytes: int, boundary: str) -> int:
    """Incrementally count canonical JSON UTF-8 bytes without materializing it."""

    _validate_size_limit(limit_bytes=limit_bytes, boundary=boundary)
    total = 0
    for chunk in _JSON_ENCODER.iterencode(value):
        total += len(chunk.encode("utf-8"))
        if total > limit_bytes:
            raise CanonicalJsonSizeLimitError(
                boundary=boundary,
                limit_bytes=limit_bytes,
                observed_at_least_bytes=total,
            )
    return total


def measure_encoded_evidence_bytes(value: object, *, limit_bytes: int, boundary: str) -> int:
    """Measure the exact reversible envelope written by persistence adapters."""

    return measure_json_bytes(
        encode_evidence(value),
        limit_bytes=limit_bytes,
        boundary=boundary,
    )


def encode_evidence(value: object) -> object:
    """Encode supported domain evidence into a collision-safe JSON value tree."""

    preflight_evidence_payload(value)
    return _encode_evidence(value)


def _encode_evidence(value: object) -> object:
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, Decimal):
        return {_TAG: "decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {_TAG: "uuid", "value": str(value)}
    if isinstance(value, datetime):
        return {_TAG: "datetime", "value": value.isoformat()}
    if isinstance(value, Mapping):
        return {
            _TAG: "map",
            "entries": [[key, _encode_evidence(nested)] for key, nested in value.items()],
        }
    if isinstance(value, list):
        return {_TAG: "list", "items": [_encode_evidence(item) for item in value]}
    if isinstance(value, tuple):
        return {_TAG: "tuple", "items": [_encode_evidence(item) for item in value]}
    raise AssertionError("evidence must be preflighted before encoding")


def decode_evidence(value: object) -> object:
    """Decode persisted evidence envelopes, rejecting malformed stored JSON."""

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
            return require_bounded_evidence_decimal(decimal)
        except InvalidStrategyRunEvidenceError as error:
            raise ValueError("stored decimal evidence is outside supported bounds") from error
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
            return _require_aware_datetime(timestamp, field_name="stored evidence datetime")
        except InvalidStrategyRunEvidenceError as error:
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


def require_bounded_evidence_decimal(value: Decimal) -> Decimal:
    """Reject Decimal values that could exceed fixed evidence resource limits."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise InvalidStrategyRunEvidenceError("evidence Decimal must be finite")
    decimal_tuple = value.as_tuple()
    if not isinstance(decimal_tuple.exponent, int):
        raise InvalidStrategyRunEvidenceError("evidence Decimal must have an integer exponent")
    coefficient_digits = len(decimal_tuple.digits)
    if coefficient_digits > MAX_EVIDENCE_DECIMAL_COEFFICIENT_DIGITS:
        raise InvalidStrategyRunEvidenceError(
            "evidence Decimal coefficient exceeds the fixed limit"
        )
    if abs(decimal_tuple.exponent) > MAX_EVIDENCE_DECIMAL_ABSOLUTE_EXPONENT:
        raise InvalidStrategyRunEvidenceError("evidence Decimal exponent exceeds the fixed limit")
    if _decimal_serialized_characters(decimal_tuple) > MAX_EVIDENCE_DECIMAL_SERIALIZED_CHARACTERS:
        raise InvalidStrategyRunEvidenceError(
            "evidence Decimal canonical form exceeds the fixed limit"
        )
    return value


def _enter_container(value: object, active_container_ids: set[int]) -> None:
    container_id = id(value)
    if container_id in active_container_ids:
        raise InvalidStrategyRunEvidenceError("evidence must not contain active-container cycles")
    active_container_ids.add(container_id)


def _require_evidence_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidStrategyRunEvidenceError(f"{field_name} must be a string")
    byte_length = len(value.encode("utf-8"))
    if byte_length > MAX_STRING_UTF8_BYTES:
        raise InvalidStrategyRunEvidenceError(
            f"{field_name} exceeds the {MAX_STRING_UTF8_BYTES // 1024} KiB UTF-8 limit"
        )
    return value


def _require_aware_datetime(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidStrategyRunEvidenceError(f"{field_name} must be timezone-aware")
    return value


def _validate_size_limit(*, limit_bytes: int, boundary: str) -> None:
    if not isinstance(limit_bytes, int) or isinstance(limit_bytes, bool) or limit_bytes < 0:
        raise ValueError("limit_bytes must be a non-negative integer")
    if not isinstance(boundary, str) or not boundary:
        raise ValueError("boundary must be a non-blank string")


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


def _decimal_serialized_characters(decimal_tuple: DecimalTuple) -> int:
    coefficient_digits = len(decimal_tuple.digits)
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise AssertionError("finite Decimal must have an integer exponent")
    sign_characters = 1 if decimal_tuple.sign else 0
    if exponent >= 0:
        return sign_characters + coefficient_digits + exponent
    decimal_point = coefficient_digits + exponent
    if decimal_point > 0:
        return sign_characters + coefficient_digits + 1
    return sign_characters + 2 + (-decimal_point) + coefficient_digits
