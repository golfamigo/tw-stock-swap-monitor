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
        "auto_trading_enabled",
        "broker_mutation_enabled",
        "position_mutation_enabled",
    }
)
ASSET_IDENTIFIER_KEY_NAMES = frozenset({"asset_symbol", "symbol", "ticker"})
HOLDING_KEY_NAMES = frozenset({"holding", "holdings"})
CREDENTIAL_KEY_NAMES = frozenset(
    {
        "access_key",
        "access_token",
        "admin_api_token",
        "api_key",
        "bearer_token",
        "client_secret",
        "credential",
        "password",
        "private_key",
        "provider_api_key",
        "provider_credential",
        "refresh_token",
        "secret",
        "token",
    }
)
NOTIFICATION_RECIPIENT_KEY_NAMES = frozenset(
    {
        "email_recipient",
        "notification_destination",
        "notification_destinations",
        "notification_recipient",
        "notification_recipients",
        "recipient",
        "recipients",
        "webhook_destination",
    }
)
NOTIFICATION_DESTINATION_KEY_NAMES = frozenset(
    {
        "notification_destination",
        "notification_destinations",
        "webhook_destination",
    }
)
_CREDENTIAL_SUFFIXES = frozenset(
    {
        ("api", "key"),
        ("credential",),
        ("private", "key"),
        ("secret",),
        ("token",),
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


@pytest.mark.parametrize("container_key", ("settings", "extensions"))
def test_template_safety_rejects_enabled_hyphenated_automatic_execution_flags(
    container_key: str,
) -> None:
    payload: dict[str, object] = {"settings": {}, "extensions": {}}
    container = payload[container_key]
    assert isinstance(container, dict)
    container["auto-trading-enabled"] = True

    findings = tuple(_template_safety_findings(payload))

    assert any("automatic execution" in finding for finding in findings)


def test_template_safety_rejects_enabled_position_mutation() -> None:
    payload = {"settings": {"position_mutation_enabled": True}, "extensions": {}}

    findings = tuple(_template_safety_findings(payload))

    assert any("automatic execution" in finding for finding in findings)


@pytest.mark.parametrize(
    ("key", "expected_reason"),
    (
        ("api_key", "credential"),
        ("private_key", "credential"),
        ("notification_recipient", "notification recipient"),
        ("notification_destination", "notification destination"),
    ),
)
def test_template_safety_rejects_precise_sensitive_key_names(
    key: str, expected_reason: str
) -> None:
    payload = {"settings": {}, "extensions": {"nested": [{key: "placeholder"}]}}

    findings = tuple(_template_safety_findings(payload))

    assert any(expected_reason in finding for finding in findings)


def test_template_safety_allows_generic_token_and_recipient_count_keys() -> None:
    payload = {
        "settings": {"token_bucket_size": 10, "recipient_count": 0},
        "extensions": {"nested": [{"token_bucket_size": 10, "recipient_count": 0}]},
    }

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


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
                    normalized_key = _normalized_key(key)
                    next_path = path + (normalized_key,)
                    yield from _key_safety_findings(normalized_key, next_path)
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


def _normalized_key(key: str) -> str:
    """Return the only key normalization used by the bounded traversal."""
    return key.casefold().replace("-", "_")


def _key_safety_findings(normalized_key: str, path: tuple[str | int, ...]) -> Iterator[str]:
    location = _path_label(path)
    if _provider_credential_environment(normalized_key) == "live":
        yield f"{location} is a live provider credential field"
    elif _provider_credential_environment(normalized_key) == "production":
        yield f"{location} is a production provider credential field"
    elif normalized_key in ASSET_IDENTIFIER_KEY_NAMES:
        yield f"{location} is an asset symbol field"
    elif normalized_key in HOLDING_KEY_NAMES:
        yield f"{location} is a holding field"
    elif normalized_key in NOTIFICATION_DESTINATION_KEY_NAMES:
        yield f"{location} is a notification destination field"
    elif normalized_key in NOTIFICATION_RECIPIENT_KEY_NAMES:
        yield f"{location} is a notification recipient field"
    elif normalized_key in CREDENTIAL_KEY_NAMES:
        yield f"{location} is a secret or credential field"


def _provider_credential_environment(normalized_key: str) -> str | None:
    parts = tuple(part for part in normalized_key.split("_") if part)
    if len(parts) < 3 or parts[:2] not in {("live", "provider"), ("production", "provider")}:
        return None
    suffix_parts = parts[2:]
    if not any(suffix_parts[: len(suffix)] == suffix for suffix in _CREDENTIAL_SUFFIXES):
        return None
    return parts[0]


def _scalar_safety_findings(value: str, path: tuple[str | int, ...]) -> Iterator[str]:
    location = _path_label(path)
    if len(value) > MAX_TEMPLATE_SAFETY_TEXT_LENGTH:
        yield f"{location} exceeds the maximum length"
        return

    normalized_value = value.casefold()
    value_parts = _normalized_value_parts(normalized_value)
    if normalized_value in {"aapl", "2330", "2330.tw"}:
        yield f"{location} contains a real asset symbol"
    elif any(part in HOLDING_KEY_NAMES for part in value_parts):
        yield f"{location} contains a holding"
    elif "@" in normalized_value and "." in normalized_value.rsplit("@", maxsplit=1)[-1]:
        yield f"{location} contains a notification recipient"
    elif _provider_credential_environment("_".join(value_parts)) == "live":
        yield f"{location} contains a live provider credential"
    elif _provider_credential_environment("_".join(value_parts)) == "production":
        yield f"{location} contains a production provider credential"
    elif any(part in {"secret", "credential", "password"} for part in value_parts):
        yield f"{location} contains a secret"
    elif _has_value_phrase(value_parts, ("api", "key")) or _has_value_phrase(
        value_parts, ("private", "key")
    ):
        yield f"{location} contains a credential"
    elif _has_value_phrase(value_parts, ("notification", "recipient")):
        yield f"{location} contains a notification recipient"
    elif _has_value_phrase(value_parts, ("notification", "destination")):
        yield f"{location} contains a notification destination"
    elif _has_value_phrase(value_parts, ("automatic", "order", "submission", "enabled")):
        yield f"{location} enables automatic execution"


def _normalized_value_parts(value: str) -> tuple[str, ...]:
    normalized = value
    for separator in ("-", "_", "=", ":", "/", "."):
        normalized = normalized.replace(separator, " ")
    return tuple(part for part in normalized.split() if part)


def _has_value_phrase(value_parts: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    return any(
        value_parts[index : index + len(phrase)] == phrase for index in range(len(value_parts))
    )


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
