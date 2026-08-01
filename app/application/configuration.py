"""Deterministic, authorized configuration resolution and canonical snapshots."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import cast
from uuid import UUID

from app.domain.access import AccessContext
from app.domain.values import Ownership, require_timezone_aware
from app.schemas.common import (
    EXPECTED_OWNERSHIP_SCOPE_BY_LAYER,
    ConfigurationLayerScope,
    DeleteDirective,
    ParentVersion,
)
from app.schemas.configuration import (
    LayerPatchSchema,
    ListMergePolicy,
    ListMergeStrategy,
    ResolvedConfigurationSchema,
    list_merge_policies_for,
)

CANONICAL_FORMAT_VERSION = "1"
_PRECEDENCE = {scope: index for index, scope in enumerate(ConfigurationLayerScope)}


class ConfigurationError(ValueError):
    """Base error for configuration boundary failures."""


class ConfigurationMergeError(ConfigurationError):
    """A higher-priority patch is structurally incompatible with its parent."""


class ConfigurationCanonicalizationError(ConfigurationError):
    """A validated payload contains a value with no stable canonical representation."""


class RuntimeOverrideExpiryError(ConfigurationError):
    """A command-local runtime override is expired or exceeds its permitted lifetime."""


@dataclass(frozen=True, slots=True)
class ConfigurationLayer:
    """One validated layer and its access-controlled version identity."""

    scope: ConfigurationLayerScope
    patch: LayerPatchSchema
    reference_id: UUID
    version: int
    content_hash: str
    ownership: Ownership
    content_hash_format_version: str = CANONICAL_FORMAT_VERSION
    portfolio_owner_id: UUID | None = None
    runtime_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ConfigurationLayerScope):
            raise TypeError("scope must be a ConfigurationLayerScope")
        if not isinstance(self.patch, LayerPatchSchema):
            raise TypeError("patch must be a LayerPatchSchema")
        expected_ownership_scope = EXPECTED_OWNERSHIP_SCOPE_BY_LAYER[self.scope]
        if self.ownership.scope is not expected_ownership_scope:
            raise ValueError(
                f"{self.scope.value} layers require {expected_ownership_scope.value} ownership"
            )
        if expected_ownership_scope.value == "PORTFOLIO":
            if self.portfolio_owner_id is None:
                raise ValueError(f"{self.scope.value} layers require a portfolio owner")
        elif self.portfolio_owner_id is not None:
            raise ValueError(f"{self.scope.value} layers must not specify a portfolio owner")
        object.__setattr__(self, "patch", LayerPatchSchema.for_scope(self.scope, self.patch))
        if self.version < 1:
            raise ValueError("configuration layer version must be positive")
        if self.content_hash_format_version != CANONICAL_FORMAT_VERSION:
            raise ConfigurationCanonicalizationError(
                "unsupported layer content hash format version"
            )
        normalized_content_hash = self.content_hash.lower()
        if len(normalized_content_hash) != 64 or any(
            character not in "0123456789abcdef" for character in normalized_content_hash
        ):
            raise ValueError("content_hash must be a SHA-256 hexadecimal digest")
        expected_content_hash = canonical_content_hash(
            self.patch.model_dump(mode="python", by_alias=True, exclude_unset=True),
            format_version=self.content_hash_format_version,
        )
        if normalized_content_hash != expected_content_hash:
            raise ConfigurationCanonicalizationError(
                "content_hash does not match the canonical patch"
            )
        object.__setattr__(self, "content_hash", normalized_content_hash)
        if self.runtime_expires_at is not None:
            require_timezone_aware(self.runtime_expires_at, field_name="runtime_expires_at")


type FrozenConfigurationValue = object


@dataclass(frozen=True, slots=True)
class ResolvedConfigurationSnapshot:
    """An immutable resolved payload plus all evidence required to reproduce it."""

    payload: Mapping[str, FrozenConfigurationValue]
    parent_versions: tuple[ParentVersion, ...]
    created_by: UUID
    created_at: datetime
    runtime_expires_at: datetime | None
    canonical_format_version: str
    content_hash: str
    canonical_json: str


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ConfigurationCanonicalizationError("Decimal values must be finite")
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _canonical_datetime(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConfigurationCanonicalizationError("datetime values must be timezone-aware")
    utc_value = value.astimezone(UTC)
    return utc_value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc_value.microsecond:06d}Z"


def _canonical_value(value: object, active_containers: set[int]) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        if not isinstance(value.value, str):
            raise ConfigurationCanonicalizationError("Enum values must have stable string values")
        return _canonical_value(value.value, active_containers)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise ConfigurationCanonicalizationError("floating-point values are not canonicalizable")
    if isinstance(value, Decimal):
        return _canonical_decimal(value)
    if isinstance(value, UUID):
        return _canonical_value(str(value), active_containers)
    if isinstance(value, datetime):
        return _canonical_value(_canonical_datetime(value), active_containers)
    if isinstance(value, str):
        return json.dumps(
            unicodedata.normalize("NFC", value), ensure_ascii=False, separators=(",", ":")
        )
    if isinstance(value, bytes | bytearray | memoryview | set | frozenset):
        raise ConfigurationCanonicalizationError(
            f"unsupported canonical value type: {type(value).__name__}"
        )
    if isinstance(value, Mapping):
        container_id = id(value)
        if container_id in active_containers:
            raise ConfigurationCanonicalizationError("cyclic mappings are not canonicalizable")
        active_containers.add(container_id)
        try:
            normalized_entries: list[tuple[bytes, str, object]] = []
            normalized_keys: set[str] = set()
            for raw_key, nested_value in value.items():
                if not isinstance(raw_key, str):
                    raise ConfigurationCanonicalizationError("map keys must be strings")
                normalized_key = unicodedata.normalize("NFC", raw_key)
                if normalized_key in normalized_keys:
                    raise ConfigurationCanonicalizationError(
                        "map contains duplicate keys after NFC normalization"
                    )
                normalized_keys.add(normalized_key)
                normalized_entries.append(
                    (normalized_key.encode("utf-8"), normalized_key, nested_value)
                )
            normalized_entries.sort(key=lambda entry: entry[0])
            return (
                "{"
                + ",".join(
                    f"{_canonical_value(key, active_containers)}:"
                    f"{_canonical_value(nested, active_containers)}"
                    for _, key, nested in normalized_entries
                )
                + "}"
            )
        finally:
            active_containers.remove(container_id)
    if isinstance(value, list | tuple):
        container_id = id(value)
        if container_id in active_containers:
            raise ConfigurationCanonicalizationError("cyclic lists are not canonicalizable")
        active_containers.add(container_id)
        try:
            return "[" + ",".join(_canonical_value(item, active_containers) for item in value) + "]"
        finally:
            active_containers.remove(container_id)
    if isinstance(value, date):
        return _canonical_value(value.isoformat(), active_containers)
    raise ConfigurationCanonicalizationError(
        f"unsupported canonical value type: {type(value).__name__}"
    )


def canonical_json(value: object) -> str:
    """Encode a validated value as the version-independent canonical JSON payload."""

    return _canonical_value(value, set())


def canonical_content_hash(value: object, *, format_version: str = CANONICAL_FORMAT_VERSION) -> str:
    """Hash canonical format version, a zero-byte delimiter, and canonical UTF-8 JSON."""

    if not format_version:
        raise ConfigurationCanonicalizationError("canonical format version must not be blank")
    content = canonical_json(value).encode("utf-8")
    return hashlib.sha256(format_version.encode("utf-8") + b"\0" + content).hexdigest()


def _copy_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _copy_value(nested_value) for key, nested_value in value.items()}
    if isinstance(value, list):
        return [_copy_value(nested_value) for nested_value in value]
    if isinstance(value, tuple):
        return tuple(_copy_value(nested_value) for nested_value in value)
    return value


def _freeze_value(value: object) -> FrozenConfigurationValue:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(nested_value) for key, nested_value in value.items()}
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(nested_value) for nested_value in value)
    return value


def _kind(value: object) -> str:
    if isinstance(value, Mapping):
        return "map"
    if isinstance(value, list | tuple):
        return "list"
    return "scalar"


def _merge_keyed_list(
    lower: list[object], higher: list[object], path: str, *, key: str
) -> list[object]:
    result: list[object] = []
    indexes: dict[str, int] = {}
    for item in lower:
        if not isinstance(item, Mapping) or not isinstance(item.get(key), str):
            raise ConfigurationMergeError(
                f"{path} requires mapping entries with string {key} values"
            )
        identifier = item[key]
        assert isinstance(identifier, str)
        indexes[identifier] = len(result)
        result.append(_copy_value(item))
    for item in higher:
        if not isinstance(item, Mapping) or not isinstance(item.get(key), str):
            raise ConfigurationMergeError(
                f"{path} requires mapping entries with string {key} values"
            )
        identifier = item[key]
        assert isinstance(identifier, str)
        if identifier in indexes:
            lower_item = result[indexes[identifier]]
            result[indexes[identifier]] = _merge_value(lower_item, item, f"{path}[{identifier}]")
        else:
            indexes[identifier] = len(result)
            result.append(_copy_value(item))
    return result


def _merge_value(
    lower: object, higher: object, path: str, *, list_policy: ListMergePolicy | None = None
) -> object:
    if higher is None:
        return None
    if lower is None:
        return _copy_value(higher)
    lower_kind = _kind(lower)
    higher_kind = _kind(higher)
    if lower_kind != higher_kind:
        raise ConfigurationMergeError(
            f"incompatible configuration types at {path}: {lower_kind} cannot merge with "
            f"{higher_kind}"
        )
    if lower_kind == "map":
        assert isinstance(lower, Mapping) and isinstance(higher, Mapping)
        result = {key: _copy_value(value) for key, value in lower.items()}
        for key, value in higher.items():
            if key in result:
                result[key] = _merge_value(result[key], value, f"{path}.{key}")
            else:
                result[key] = _copy_value(value)
        return result
    if lower_kind == "list":
        assert isinstance(lower, list | tuple) and isinstance(higher, list | tuple)
        if list_policy is not None and list_policy.strategy is ListMergeStrategy.KEYED:
            assert list_policy.key is not None
            return _merge_keyed_list(list(lower), list(higher), path, key=list_policy.key)
        return _copy_value(list(higher))
    if type(lower) is not type(higher):
        raise ConfigurationMergeError(
            f"incompatible configuration types at {path}: {type(lower).__name__} and "
            f"{type(higher).__name__}"
        )
    return _copy_value(higher)


def _merge_extensions(lower: object | None, higher: Mapping[str, object]) -> dict[str, object]:
    if lower is None:
        inherited: dict[str, object] = {}
    elif isinstance(lower, Mapping):
        inherited = {key: _copy_value(value) for key, value in lower.items()}
    else:
        raise ConfigurationMergeError("incompatible configuration types at extensions")
    for key, value in higher.items():
        if isinstance(value, DeleteDirective):
            if key not in inherited:
                raise ConfigurationMergeError(f"extensions.{key} cannot delete a non-inherited key")
            del inherited[key]
        elif key in inherited:
            inherited[key] = _merge_value(inherited[key], value, f"extensions.{key}")
        else:
            inherited[key] = _copy_value(value)
    return inherited


class ConfigurationResolver:
    """Resolve the fixed seven-layer precedence chain into one immutable snapshot."""

    def __init__(
        self,
        *,
        maximum_runtime_ttl: timedelta,
        resolved_configuration_schema: type[ResolvedConfigurationSchema] = (
            ResolvedConfigurationSchema
        ),
    ) -> None:
        if maximum_runtime_ttl <= timedelta(0):
            raise ValueError("maximum_runtime_ttl must be positive")
        self._maximum_runtime_ttl = maximum_runtime_ttl
        self._resolved_configuration_schema = resolved_configuration_schema
        self._list_merge_policies = list_merge_policies_for(resolved_configuration_schema)

    def resolve(
        self,
        layers: Sequence[ConfigurationLayer],
        *,
        access_context: AccessContext,
        created_by: UUID,
        created_at: datetime,
        now: datetime,
        run_deadline: datetime,
    ) -> ResolvedConfigurationSnapshot:
        """Authorize, merge, validate, canonicalize, and freeze all contributing layers."""

        require_timezone_aware(created_at, field_name="created_at")
        require_timezone_aware(now, field_name="now")
        require_timezone_aware(run_deadline, field_name="run_deadline")
        if run_deadline <= now:
            raise RuntimeOverrideExpiryError("run_deadline must be later than now")

        ordered_layers = sorted(layers, key=lambda layer: _PRECEDENCE[layer.scope])
        seen_scopes: set[ConfigurationLayerScope] = set()
        merged: dict[str, object] = {}
        parent_versions: list[ParentVersion] = []
        runtime_expires_at: datetime | None = None

        for layer in ordered_layers:
            if layer.scope in seen_scopes:
                raise ConfigurationMergeError(f"duplicate {layer.scope.value} configuration layer")
            seen_scopes.add(layer.scope)
            access_context.require_read(
                layer.ownership, portfolio_owner_id=layer.portfolio_owner_id
            )
            if layer.scope is ConfigurationLayerScope.RUNTIME_OVERRIDE:
                self._validate_runtime_expiry(layer.runtime_expires_at, now, run_deadline)
                runtime_expires_at = layer.runtime_expires_at
            elif layer.runtime_expires_at is not None:
                raise RuntimeOverrideExpiryError(
                    "only a RUNTIME_OVERRIDE layer may specify runtime_expires_at"
                )

            for field_name in layer.patch.model_fields_set:
                higher_value = getattr(layer.patch, field_name)
                if field_name == "extensions":
                    assert isinstance(higher_value, Mapping)
                    merged[field_name] = _merge_extensions(merged.get(field_name), higher_value)
                elif field_name in merged:
                    merged[field_name] = _merge_value(
                        merged[field_name],
                        higher_value,
                        field_name,
                        list_policy=self._list_merge_policies.get(field_name),
                    )
                else:
                    merged[field_name] = _copy_value(higher_value)
            parent_versions.append(
                ParentVersion(
                    scope=layer.scope,
                    reference_id=layer.reference_id,
                    version=layer.version,
                    content_hash=layer.content_hash,
                    content_hash_format_version=layer.content_hash_format_version,
                )
            )

        resolved = self._resolved_configuration_schema.model_validate(merged)
        resolved_payload = resolved.model_dump(mode="python")
        serialized = canonical_json(resolved_payload)
        frozen_payload = _freeze_value(resolved_payload)
        if not isinstance(frozen_payload, Mapping):
            raise ConfigurationMergeError("resolved configuration payload must remain a mapping")
        return ResolvedConfigurationSnapshot(
            payload=cast(Mapping[str, FrozenConfigurationValue], frozen_payload),
            parent_versions=tuple(parent_versions),
            created_by=created_by,
            created_at=created_at,
            runtime_expires_at=runtime_expires_at,
            canonical_format_version=CANONICAL_FORMAT_VERSION,
            content_hash=canonical_content_hash(resolved_payload),
            canonical_json=serialized,
        )

    def _validate_runtime_expiry(
        self, expires_at: datetime | None, now: datetime, run_deadline: datetime
    ) -> None:
        if expires_at is None:
            raise RuntimeOverrideExpiryError("runtime overrides require an expiry")
        if expires_at <= now:
            raise RuntimeOverrideExpiryError("runtime override is expired")
        if expires_at > run_deadline:
            raise RuntimeOverrideExpiryError("runtime override expires after the run deadline")
        if expires_at - now > self._maximum_runtime_ttl:
            raise RuntimeOverrideExpiryError("runtime override exceeds the configured maximum TTL")
