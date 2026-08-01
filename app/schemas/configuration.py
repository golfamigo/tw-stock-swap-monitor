"""Separate sparse patch and complete resolved configuration schemas."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator

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


class LayerPatchSchema(BaseModel):
    """A sparse, typed configuration layer that cannot execute on its own."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    configuration_name: str | None = None
    settings: dict[str, object] | None = None
    rules: list[dict[str, object]] | None = None
    keyed_items: list[dict[str, object]] | None = None
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

    @field_validator("keyed_items")
    @classmethod
    def require_keyed_list_identifiers(cls, value: list[dict[str, object]] | None) -> object:
        if value is None:
            return value
        identifiers: set[str] = set()
        for item in value:
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("each keyed_items entry requires a non-blank string id")
            if identifier in identifiers:
                raise ValueError("keyed_items ids must be unique within a layer")
            identifiers.add(identifier)
        return value

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
    rules: list[dict[str, object]]
    keyed_items: list[dict[str, object]]
    extensions: dict[str, object]
    nullable_note: str | None

    @field_validator("settings", "rules", "keyed_items", "extensions")
    @classmethod
    def reject_delete_in_resolved_payload(cls, value: object) -> object:
        _reject_delete_operations(value)
        return value

    @field_validator("keyed_items")
    @classmethod
    def require_resolved_keyed_list_identifiers(cls, value: list[dict[str, object]]) -> object:
        identifiers: set[str] = set()
        for item in value:
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("each keyed_items entry requires a non-blank string id")
            if identifier in identifiers:
                raise ValueError("keyed_items ids must be unique in the resolved configuration")
            identifiers.add(identifier)
        return value
