"""Collision-safe reversible JSON codec for validated configuration payloads."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import UUID

from app.application.configuration import ConfigurationCanonicalizationError
from app.domain.values import require_finite_decimal, require_timezone_aware

_TAG = "$configuration_payload"


def encode_configuration_payload(value: object) -> object:
    """Encode canonical configuration values into an unambiguous JSON tree.

    All mappings are enveloped, including ordinary mappings that contain the tag key,
    so persisted user configuration cannot collide with this codec's control syntax.
    """

    return _encode(value, active_container_ids=set())


def _encode(value: object, *, active_container_ids: set[int]) -> object:
    if isinstance(value, Enum):
        if not isinstance(value.value, str):
            raise ConfigurationCanonicalizationError(
                "configuration Enum values must have stable string values"
            )
        return _encode(value.value, active_container_ids=active_container_ids)
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float | bytes | bytearray | memoryview | set | frozenset):
        raise ConfigurationCanonicalizationError(
            f"unsupported configuration persistence type: {type(value).__name__}"
        )
    if isinstance(value, Decimal):
        require_finite_decimal(value, field_name="configuration Decimal")
        return {_TAG: "decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {_TAG: "uuid", "value": str(value)}
    if isinstance(value, datetime):
        require_timezone_aware(value, field_name="configuration datetime")
        return {_TAG: "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {_TAG: "date", "value": value.isoformat()}
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_container_ids:
            raise ConfigurationCanonicalizationError("configuration payload must not be cyclic")
        active_container_ids.add(container_id)
        try:
            entries: list[list[object]] = []
            for key, nested in value.items():
                if not isinstance(key, str):
                    raise ConfigurationCanonicalizationError(
                        "configuration mapping keys must be strings"
                    )
                entries.append([key, _encode(nested, active_container_ids=active_container_ids)])
            return {_TAG: "map", "entries": entries}
        finally:
            active_container_ids.remove(container_id)
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in active_container_ids:
            raise ConfigurationCanonicalizationError("configuration payload must not be cyclic")
        active_container_ids.add(container_id)
        try:
            return {
                _TAG: "list" if isinstance(value, list) else "tuple",
                "items": [
                    _encode(item, active_container_ids=active_container_ids) for item in value
                ],
            }
        finally:
            active_container_ids.remove(container_id)
    raise ConfigurationCanonicalizationError(
        f"unsupported configuration persistence type: {type(value).__name__}"
    )


def _require_exact_mapping(value: object, *, expected_keys: frozenset[str]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("stored configuration payload envelope must be a mapping")
    if set(value) != expected_keys or any(not isinstance(key, str) for key in value):
        raise ValueError("stored configuration payload envelope has an invalid shape")
    return value


def _decode_items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("stored configuration payload items must be a list")
    return [decode_configuration_payload(item) for item in value]


def decode_configuration_payload(value: object) -> object:
    """Decode one strict configuration-payload JSON tree from persistence."""

    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, list | tuple):
        raise ValueError("stored configuration containers must use an explicit envelope")
    if not isinstance(value, Mapping):
        raise ValueError("stored configuration payload contains an unsupported JSON value")
    tag = value.get(_TAG)
    if not isinstance(tag, str):
        raise ValueError("stored configuration payload envelope is missing its type tag")
    if tag == "decimal":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored configuration Decimal must be a string")
        try:
            decimal = Decimal(rendered)
        except InvalidOperation as error:
            raise ValueError("stored configuration Decimal is invalid") from error
        try:
            return require_finite_decimal(decimal, field_name="stored configuration Decimal")
        except ValueError as error:
            raise ValueError("stored configuration Decimal must be finite") from error
    if tag == "uuid":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored configuration UUID must be a string")
        try:
            return UUID(rendered)
        except ValueError as error:
            raise ValueError("stored configuration UUID is invalid") from error
    if tag == "datetime":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored configuration datetime must be a string")
        try:
            timestamp = datetime.fromisoformat(rendered)
        except ValueError as error:
            raise ValueError("stored configuration datetime is invalid") from error
        try:
            return require_timezone_aware(timestamp, field_name="stored configuration datetime")
        except ValueError as error:
            raise ValueError("stored configuration datetime must be timezone-aware") from error
    if tag == "date":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "value"}))
        rendered = envelope["value"]
        if not isinstance(rendered, str):
            raise ValueError("stored configuration date must be a string")
        try:
            return date.fromisoformat(rendered)
        except ValueError as error:
            raise ValueError("stored configuration date is invalid") from error
    if tag == "map":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "entries"}))
        raw_entries = envelope["entries"]
        if not isinstance(raw_entries, list):
            raise ValueError("stored configuration map entries must be a list")
        decoded: dict[str, object] = {}
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, list) or len(raw_entry) != 2:
                raise ValueError("stored configuration map entry must be a pair")
            key, nested = raw_entry
            if not isinstance(key, str) or key in decoded:
                raise ValueError("stored configuration map keys must be unique strings")
            decoded[key] = decode_configuration_payload(nested)
        return decoded
    if tag == "list":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "items"}))
        return _decode_items(envelope["items"])
    if tag == "tuple":
        envelope = _require_exact_mapping(value, expected_keys=frozenset({_TAG, "items"}))
        return tuple(_decode_items(envelope["items"]))
    raise ValueError("stored configuration payload envelope has an unsupported type tag")
