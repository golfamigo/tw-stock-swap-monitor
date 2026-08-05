"""Regression contracts for the committed generic configuration seeds."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
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
MAX_TEMPLATE_SAFETY_DEPTH = 16
MAX_TEMPLATE_SAFETY_NODES = 256
MAX_TEMPLATE_SAFETY_TEXT_LENGTH = 512
AUTOMATIC_EXECUTION_FLAG_KEYS = frozenset(
    {
        "automatic_order_submission_enabled",
        "automatic_orders_enabled",
        "auto_trading_enabled",
        "autotrading_enabled",
        "broker_mutation_enabled",
        "order_submission_enabled",
    }
)


@pytest.mark.parametrize(
    ("unsafe_value", "expected_reason"),
    (
        ("AAPL", "real asset symbol"),
        ("personal holding", "holding"),
        ("recipient@example.test", "recipient"),
        ("test-secret-value", "secret"),
        ("live_provider_api_key=placeholder", "live provider credential"),
        ("automatic order submission enabled", "automatic execution"),
    ),
)
@pytest.mark.parametrize("container_key", ("settings", "extensions"))
def test_template_safety_rejects_unsafe_values_below_neutral_keys(
    unsafe_value: str, expected_reason: str, container_key: str
) -> None:
    """Unsafe scalar data cannot hide under generic configuration map entries."""
    payload: dict[str, object] = {"settings": {}, "extensions": {}}
    container = payload[container_key]
    assert isinstance(container, dict)
    container["generic_note"] = [unsafe_value]

    findings = tuple(_template_safety_findings(payload))

    assert any(expected_reason in finding for finding in findings)


@pytest.mark.parametrize(
    "flag_key",
    (
        "automatic_order_submission_enabled",
        "broker_mutation_enabled",
        "auto_trading_enabled",
    ),
)
def test_template_safety_rejects_enabled_automatic_execution_flags(flag_key: str) -> None:
    payload = {"settings": {flag_key: True}, "extensions": {}}

    findings = tuple(_template_safety_findings(payload))

    assert any("automatic execution" in finding for finding in findings)


def test_template_safety_recursively_rejects_unsafe_keys_and_oversized_text() -> None:
    payload = {
        "settings": {},
        "extensions": {
            "nested": [
                {"live_provider_credential": "placeholder"},
                {"generic_note": "x" * 513},
            ]
        },
    }

    findings = tuple(_template_safety_findings(payload))

    assert any("live provider credential" in finding for finding in findings)
    assert any("maximum length" in finding for finding in findings)


def _load_template(relative_path: str) -> dict[str, object]:
    template_path = PROJECT_ROOT / relative_path
    assert template_path.is_file(), f"missing configuration template: {relative_path}"

    import yaml  # type: ignore[import-untyped]

    loaded = yaml.safe_load(template_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict), f"{relative_path} must contain a YAML mapping"
    assert all(isinstance(key, str) for key in loaded)
    return loaded


def _template_safety_findings(value: object) -> Iterator[str]:
    """Yield bounded, non-sensitive findings for forbidden generic seed content."""
    visited_nodes = 0
    limit_reached = False

    def visit(current: object, path: tuple[str | int, ...], depth: int) -> Iterator[str]:
        nonlocal limit_reached, visited_nodes
        if limit_reached:
            return
        if depth > MAX_TEMPLATE_SAFETY_DEPTH:
            yield f"{_path_label(path)} exceeds the maximum nesting depth"
            return

        visited_nodes += 1
        if visited_nodes > MAX_TEMPLATE_SAFETY_NODES:
            limit_reached = True
            yield "template safety traversal exceeds the maximum node count"
            return

        if isinstance(current, Mapping):
            for key, nested_value in current.items():
                if limit_reached:
                    return
                if not isinstance(key, str):
                    yield f"{_path_label(path)} contains a non-string mapping key"
                    next_path = path + ("<non-string-key>",)
                elif len(key) > MAX_TEMPLATE_SAFETY_TEXT_LENGTH:
                    yield f"{_path_label(path)} contains a key exceeding the maximum length"
                    next_path = path + ("<oversized-key>",)
                else:
                    next_path = path + (key,)
                    yield from _key_safety_findings(key, next_path)
                yield from visit(nested_value, next_path, depth + 1)
            return

        if isinstance(current, list):
            for index, item in enumerate(current):
                if limit_reached:
                    return
                yield from visit(item, path + (index,), depth + 1)
            return

        if isinstance(current, str):
            yield from _scalar_safety_findings(current, path)
        elif current is True and _is_automatic_execution_path(path):
            yield f"{_path_label(path)} enables automatic execution"

    yield from visit(value, (), 0)


def _key_safety_findings(key: str, path: tuple[str | int, ...]) -> Iterator[str]:
    normalized_key = key.casefold().replace("-", "_")
    key_parts = frozenset(part for part in normalized_key.split("_") if part)
    location = _path_label(path)
    if {"live", "provider"} <= key_parts and key_parts & {
        "credential",
        "key",
        "secret",
        "token",
    }:
        yield f"{location} is a live provider credential field"
    elif {"production", "provider"} <= key_parts and key_parts & {
        "credential",
        "key",
        "secret",
        "token",
    }:
        yield f"{location} is a production provider credential field"
    elif key_parts & {"symbol", "ticker"}:
        yield f"{location} is an asset symbol field"
    elif key_parts & {"holding", "holdings"}:
        yield f"{location} is a holding field"
    elif "recipient" in key_parts:
        yield f"{location} is a notification recipient field"
    elif key_parts & {"secret", "credential", "password", "token"}:
        yield f"{location} is a secret or credential field"
    elif {"api", "key"} <= key_parts or {"access", "key"} <= key_parts:
        yield f"{location} is a credential field"


def _scalar_safety_findings(value: str, path: tuple[str | int, ...]) -> Iterator[str]:
    location = _path_label(path)
    if len(value) > MAX_TEMPLATE_SAFETY_TEXT_LENGTH:
        yield f"{location} exceeds the maximum length"
        return

    normalized_value = value.casefold()
    if normalized_value in {"aapl", "2330", "2330.tw"}:
        yield f"{location} contains a real asset symbol"
    elif "holding" in normalized_value.split() or "holdings" in normalized_value.split():
        yield f"{location} contains a holding"
    elif "@" in normalized_value and "." in normalized_value.rsplit("@", maxsplit=1)[-1]:
        yield f"{location} contains a notification recipient"
    elif "live_provider_" in normalized_value and any(
        marker in normalized_value for marker in ("api_key", "credential", "secret", "token")
    ):
        yield f"{location} contains a live provider credential"
    elif "production_provider_" in normalized_value and any(
        marker in normalized_value for marker in ("api_key", "credential", "secret", "token")
    ):
        yield f"{location} contains a production provider credential"
    elif "secret" in normalized_value:
        yield f"{location} contains a secret"
    elif "automatic order submission enabled" in normalized_value:
        yield f"{location} enables automatic execution"


def _is_automatic_execution_path(path: tuple[str | int, ...]) -> bool:
    return bool(path) and isinstance(path[-1], str) and path[-1] in AUTOMATIC_EXECUTION_FLAG_KEYS


def _path_label(path: tuple[str | int, ...]) -> str:
    if not path:
        return "template root"
    return ".".join(str(part) for part in path)


def _settings(patch: LayerPatchSchema) -> dict[str, object]:
    assert patch.settings is not None
    return patch.settings


def test_generic_templates_parse_validate_and_remain_safe_seeds() -> None:
    templates = {relative_path: _load_template(relative_path) for relative_path in TEMPLATE_SCOPES}

    patches = {}
    for relative_path, scope in TEMPLATE_SCOPES.items():
        patch = LayerPatchSchema.for_scope(scope, templates[relative_path])
        patches[relative_path] = patch
        findings = tuple(_template_safety_findings(templates[relative_path]))
        assert not findings, f"{relative_path} contains unsafe seed content: {findings}"

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


def test_committed_templates_declare_explicit_safe_generic_modes() -> None:
    market = _load_template("config_templates/market/default.yaml")["settings"]
    strategy = _load_template("config_templates/strategy/default.yaml")["settings"]
    scoring = _load_template("config_templates/scoring/default.yaml")["settings"]
    sizing = _load_template("config_templates/sizing/default.yaml")["settings"]

    assert isinstance(market, dict)
    assert isinstance(strategy, dict)
    assert isinstance(scoring, dict)
    assert isinstance(sizing, dict)
    assert market["market_data_mode"] == "mock_only"
    assert market["live_market_data_enabled"] is False
    assert strategy["recommendation_mode"] == "recommendation_only"
    assert strategy["automatic_order_submission_enabled"] is False
    assert strategy["broker_mutation_enabled"] is False
    assert strategy["position_mutation_enabled"] is False
    assert scoring["scoring_mode"] == "disabled_until_configured"
    assert scoring["deterministic_ranking_only"] is True
    assert sizing["sizing_mode"] == "disabled_until_configured"
    assert sizing["recommendation_only"] is True
