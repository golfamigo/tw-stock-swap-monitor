"""Regression contracts for the committed generic configuration seeds."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from app.rules.parser import parse_rules
from app.schemas.common import ConfigurationLayerScope
from app.schemas.configuration import LayerPatchSchema, ResolvedConfigurationSchema

PROJECT_ROOT = Path(__file__).resolve().parents[3]
TEMPLATE_SCOPES = {
    "config_templates/market/default.yaml": ConfigurationLayerScope.MARKET,
    "config_templates/strategy/default.yaml": ConfigurationLayerScope.STRATEGY,
    "config_templates/scoring/default.yaml": ConfigurationLayerScope.STRATEGY,
    "config_templates/sizing/default.yaml": ConfigurationLayerScope.STRATEGY,
}
FORBIDDEN_TEMPLATE_KEY_FRAGMENTS = frozenset(
    {
        "symbol",
        "holding",
        "schedule",
        "cost",
        "recipient",
        "secret",
        "credential",
        "token",
    }
)


def _load_template(relative_path: str) -> dict[str, object]:
    template_path = PROJECT_ROOT / relative_path
    assert template_path.is_file(), f"missing configuration template: {relative_path}"

    import yaml  # type: ignore[import-untyped]

    loaded = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{relative_path} must contain a YAML mapping"
    assert all(isinstance(key, str) for key in loaded)
    return loaded


def _mapping_keys(value: object) -> Iterator[str]:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            if isinstance(key, str):
                yield key
            yield from _mapping_keys(nested_value)
    elif isinstance(value, list):
        for item in value:
            yield from _mapping_keys(item)


def _settings(patch: LayerPatchSchema) -> dict[str, object]:
    assert patch.settings is not None
    return patch.settings


def test_generic_templates_parse_validate_and_remain_safe_seeds() -> None:
    templates = {relative_path: _load_template(relative_path) for relative_path in TEMPLATE_SCOPES}

    patches = {}
    for relative_path, scope in TEMPLATE_SCOPES.items():
        patch = LayerPatchSchema.for_scope(scope, templates[relative_path])
        patches[relative_path] = patch
        forbidden_keys = {
            key
            for key in _mapping_keys(templates[relative_path])
            if any(fragment in key.casefold() for fragment in FORBIDDEN_TEMPLATE_KEY_FRAGMENTS)
        }
        assert (
            not forbidden_keys
        ), f"{relative_path} contains deployment-specific keys: {forbidden_keys}"

    strategy_template = templates["config_templates/strategy/default.yaml"]
    parse_rules(strategy_template["rules"])
    strategy_patch = patches["config_templates/strategy/default.yaml"]
    assert strategy_patch.keyed_items is not None
    assert strategy_patch.extensions is not None

    resolved_payload = {
        "configuration_name": "generic-recommendation-only",
        "settings": {
            **_settings(patches["config_templates/market/default.yaml"]),
            **_settings(strategy_patch),
            **_settings(patches["config_templates/scoring/default.yaml"]),
            **_settings(patches["config_templates/sizing/default.yaml"]),
        },
        "rules": strategy_template["rules"],
        "keyed_items": strategy_patch.keyed_items,
        "extensions": strategy_patch.extensions,
        "nullable_note": strategy_patch.nullable_note,
    }
    ResolvedConfigurationSchema.model_validate(resolved_payload)
