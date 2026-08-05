"""Separate sparse patch and complete resolved configuration schemas."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from app.schemas.common import ConfigurationLayerScope, DeleteDirective

_ALLOWED_PATCH_FIELDS_BY_SCOPE: dict[ConfigurationLayerScope, frozenset[str]] = {
    ConfigurationLayerScope.SYSTEM: frozenset(
        {
            "configuration_name",
            "settings",
            "rules",
            "keyed_items",
            "extensions",
            "nullable_note",
        }
    ),
    ConfigurationLayerScope.MARKET: frozenset({"settings", "extensions", "nullable_note"}),
    ConfigurationLayerScope.STRATEGY: frozenset(
        {"settings", "rules", "keyed_items", "extensions", "nullable_note"}
    ),
    ConfigurationLayerScope.USER: frozenset({"settings", "extensions", "nullable_note"}),
    ConfigurationLayerScope.PORTFOLIO: frozenset({"settings", "extensions", "nullable_note"}),
    ConfigurationLayerScope.ROTATION_PLAN: frozenset(
        {"settings", "rules", "keyed_items", "extensions", "nullable_note"}
    ),
    ConfigurationLayerScope.RUNTIME_OVERRIDE: frozenset(
        {"settings", "extensions", "nullable_note"}
    ),
}


class ListMergeStrategy(str, Enum):
    """The deterministic strategy used to combine one declared list field."""

    REPLACE = "replace"
    KEYED = "keyed"


@dataclass(frozen=True, slots=True)
class ListMergePolicy:
    """Validated list merge behavior derived from a Pydantic field declaration."""

    strategy: ListMergeStrategy
    key: str | None = None

    def __post_init__(self) -> None:
        if self.strategy is ListMergeStrategy.KEYED:
            if self.key is None or not self.key.strip():
                raise ValueError("keyed list merge policies require a non-blank key")
        elif self.key is not None:
            raise ValueError("replace list merge policies cannot declare a key")


def list_merge_policies_for(schema: type[BaseModel]) -> Mapping[str, ListMergePolicy]:
    """Return immutable list merge policies declared by a resolved schema's metadata."""

    policies: dict[str, ListMergePolicy] = {}
    for field_name, field_info in schema.model_fields.items():
        extra = field_info.json_schema_extra
        if extra is None:
            continue
        if not isinstance(extra, Mapping):
            raise TypeError(f"{field_name} merge metadata must be a mapping")
        raw_policy = extra.get("merge_policy")
        if raw_policy is None:
            continue
        if not isinstance(raw_policy, Mapping):
            raise TypeError(f"{field_name} merge policy must be a mapping")
        if set(raw_policy) - {"strategy", "key"}:
            raise ValueError(f"{field_name} merge policy contains unsupported keys")
        raw_strategy = raw_policy.get("strategy")
        if not isinstance(raw_strategy, str):
            raise TypeError(f"{field_name} merge policy strategy must be a string")
        try:
            strategy = ListMergeStrategy(raw_strategy)
        except ValueError as error:
            raise ValueError(f"{field_name} has an unsupported list merge strategy") from error
        raw_key = raw_policy.get("key")
        if raw_key is not None and not isinstance(raw_key, str):
            raise TypeError(f"{field_name} merge policy key must be a string")
        policies[field_name] = ListMergePolicy(strategy=strategy, key=raw_key)
    return MappingProxyType(policies)


def _delete_operation(value: object) -> bool:
    return isinstance(value, Mapping) and "$delete" in value


def _reject_delete_operations(value: object) -> object:
    if isinstance(value, DeleteDirective) or _delete_operation(value):
        raise ValueError("delete directives are only valid as direct extensions entries")
    if isinstance(value, Mapping):
        for nested_value in value.values():
            _reject_delete_operations(nested_value)
    elif isinstance(value, list):
        for nested_value in value:
            _reject_delete_operations(nested_value)
    return value


def _require_unique_keyed_entries(
    value: list[dict[str, object]], *, field_name: str, key: str
) -> None:
    identifiers: set[str] = set()
    for item in value:
        identifier = item.get(key)
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError(f"each {field_name} entry requires a non-blank string {key}")
        if identifier in identifiers:
            raise ValueError(f"{field_name} {key} values must be unique")
        identifiers.add(identifier)


class LayerPatchSchema(BaseModel):
    """A sparse, typed configuration layer that cannot execute on its own."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    configuration_name: str | None = None
    settings: dict[str, object] | None = None
    rules: list[dict[str, object]] | None = Field(
        default=None, json_schema_extra={"merge_policy": {"strategy": "replace"}}
    )
    keyed_items: list[dict[str, object]] | None = Field(
        default=None, json_schema_extra={"merge_policy": {"strategy": "keyed", "key": "id"}}
    )
    extensions: dict[str, object] | None = None
    nullable_note: str | None = None

    @classmethod
    def for_scope(cls, scope: ConfigurationLayerScope, value: Mapping[str, object] | Self) -> Self:
        """Parse one sparse patch using the field allow-list for its target layer."""

        if not isinstance(scope, ConfigurationLayerScope):
            raise TypeError("scope must be a ConfigurationLayerScope")
        if isinstance(value, cls):
            value = value.model_dump(by_alias=True, mode="python", exclude_unset=True)
        return cls.model_validate(value, context={"configuration_layer_scope": scope})

    @model_validator(mode="after")
    def reject_unauthorized_nulls(self) -> Self:
        """Reserve literal null for fields whose final schema explicitly permits it."""

        for field_name in (
            "configuration_name",
            "settings",
            "rules",
            "keyed_items",
            "extensions",
        ):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} does not permit an explicit null patch")
        return self

    @model_validator(mode="after")
    def reject_scope_unauthorized_fields(self, info: ValidationInfo) -> Self:
        """Reject fields that the selected sparse layer is not authorized to supply."""

        scope = (info.context or {}).get("configuration_layer_scope")
        if scope is None:
            return self
        if not isinstance(scope, ConfigurationLayerScope):
            raise ValueError("configuration layer scope context is invalid")
        unauthorized_fields = self.model_fields_set - _ALLOWED_PATCH_FIELDS_BY_SCOPE[scope]
        if unauthorized_fields:
            fields = ", ".join(sorted(unauthorized_fields))
            raise ValueError(f"{scope.value} layer does not permit patch fields: {fields}")
        return self

    @field_validator("settings", "rules", "keyed_items")
    @classmethod
    def reject_delete_outside_extensions(cls, value: object) -> object:
        if value is not None:
            _reject_delete_operations(value)
        return value

    @model_validator(mode="after")
    def require_keyed_list_identifiers(self) -> Self:
        for field_name, policy in list_merge_policies_for(type(self)).items():
            if policy.strategy is not ListMergeStrategy.KEYED:
                continue
            value = getattr(self, field_name)
            if value is not None:
                assert policy.key is not None
                _require_unique_keyed_entries(value, field_name=field_name, key=policy.key)
        return self

    @field_validator("extensions", mode="before")
    @classmethod
    def parse_extension_deletes(cls, value: object) -> object:
        if value is None or not isinstance(value, Mapping):
            return value
        normalized: dict[object, object] = {}
        for key, item in value.items():
            if _delete_operation(item):
                normalized[key] = DeleteDirective.model_validate(item)
            else:
                _reject_delete_operations(item)
                normalized[key] = item
        return normalized


class ResolvedConfigurationSchema(BaseModel):
    """The complete final configuration accepted for an immutable run snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    configuration_name: str
    settings: dict[str, object]
    rules: list[dict[str, object]] = Field(
        json_schema_extra={"merge_policy": {"strategy": "replace"}}
    )
    keyed_items: list[dict[str, object]] = Field(
        json_schema_extra={"merge_policy": {"strategy": "keyed", "key": "id"}}
    )
    extensions: dict[str, object]
    nullable_note: str | None

    @field_validator("settings", "rules", "keyed_items", "extensions")
    @classmethod
    def reject_delete_in_resolved_payload(cls, value: object) -> object:
        _reject_delete_operations(value)
        return value

    @model_validator(mode="after")
    def require_resolved_keyed_list_identifiers(self) -> Self:
        for field_name, policy in list_merge_policies_for(type(self)).items():
            if policy.strategy is ListMergeStrategy.KEYED:
                value = getattr(self, field_name)
                assert policy.key is not None
                _require_unique_keyed_entries(value, field_name=field_name, key=policy.key)
        return self
