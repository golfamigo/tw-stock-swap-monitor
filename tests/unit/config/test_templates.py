"""Regression contracts for the committed generic configuration seeds."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from app.rules.parser import parse_rules
from app.schemas.common import ConfigurationLayerScope
from app.schemas.configuration import LayerPatchSchema, ResolvedConfigurationSchema

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SYNTHETIC_TICKER_SHAPED_TOKEN = "".join(("A", "B", "C"))
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
SEMANTIC_ASSET_KEY_NAMES = frozenset(
    {
        "asset_symbol",
        "holding",
        "holding_position",
        "instrument",
        "position",
        "symbol",
        "ticker",
    }
)
GENERIC_PLACEHOLDER_ASSET_SUFFIXES = frozenset(
    {"asset", "holding", "instrument", "position", "symbol", "ticker"}
)
GENERIC_CODE_KEY_NAMES = frozenset({"currency", "format"})
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
        (SYNTHETIC_TICKER_SHAPED_TOKEN, "ticker-style asset identifier"),
        ("recipient@example.test", "recipient"),
        ("api_key=placeholder", "credential"),
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


@pytest.mark.parametrize("container_key", ("settings", "extensions"))
@pytest.mark.parametrize("flag_key", tuple(sorted(AUTOMATIC_EXECUTION_FLAG_KEYS)))
@pytest.mark.parametrize("unsafe_value", ("true", 1, 0, "enabled"))
def test_template_safety_rejects_any_non_false_automatic_execution_flag_value(
    container_key: str, flag_key: str, unsafe_value: object
) -> None:
    payload: dict[str, object] = {"settings": {}, "extensions": {}}
    container = payload[container_key]
    assert isinstance(container, dict)
    container[flag_key] = unsafe_value

    findings = tuple(_template_safety_findings(payload))

    assert any("automatic execution" in finding for finding in findings)


@pytest.mark.parametrize("container_key", ("settings", "extensions"))
def test_template_safety_allows_only_literal_false_automatic_execution_flag_values(
    container_key: str,
) -> None:
    payload: dict[str, object] = {"settings": {}, "extensions": {}}
    container = payload[container_key]
    assert isinstance(container, dict)
    for flag_key in AUTOMATIC_EXECUTION_FLAG_KEYS:
        container[flag_key] = False

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


@pytest.mark.parametrize(
    "asset_key", ("symbol", "ticker", "instrument", "holding", "holding_position", "position")
)
def test_template_safety_rejects_non_placeholder_values_for_semantic_asset_keys(
    asset_key: str,
) -> None:
    payload = {"settings": {asset_key: SYNTHETIC_TICKER_SHAPED_TOKEN}, "extensions": {}}

    findings = tuple(_template_safety_findings(payload))

    assert any("placeholder asset value" in finding for finding in findings)


def test_template_safety_allows_explicit_generic_placeholder_asset_values() -> None:
    payload = {
        "settings": {"symbol": "placeholder-asset"},
        "extensions": {"instrument": "placeholder-instrument"},
    }

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


@pytest.mark.parametrize("container_key", ("settings", "extensions"))
def test_template_safety_rejects_ticker_shaped_code_below_a_neutral_key(
    container_key: str,
) -> None:
    payload: dict[str, object] = {"settings": {}, "extensions": {}}
    container = payload[container_key]
    assert isinstance(container, dict)
    container["generic_code"] = SYNTHETIC_TICKER_SHAPED_TOKEN

    findings = tuple(_template_safety_findings(payload))

    assert any("ticker-style asset identifier" in finding for finding in findings)


@pytest.mark.parametrize(("code_key", "code_value"), (("currency", "USD"), ("format", "JSON")))
@pytest.mark.parametrize("container_key", ("settings", "extensions"))
def test_template_safety_allows_uppercase_generic_codes_in_named_code_contexts(
    code_key: str, code_value: str, container_key: str
) -> None:
    payload: dict[str, object] = {"settings": {}, "extensions": {}}
    container = payload[container_key]
    assert isinstance(container, dict)
    container[code_key] = code_value

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


def test_template_safety_allows_explanatory_generic_prose() -> None:
    payload = {
        "settings": {
            "generic_note": "A holding and credential policy is explanatory, not a value."
        },
        "extensions": {
            "explanation": "A secret boundary may be documented without embedding a secret."
        },
        "nullable_note": "This generic note does not identify a recipient or position.",
    }

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


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


def test_template_safety_allows_a_mapping_at_the_exact_depth_limit() -> None:
    payload: object = {}
    for index in range(MAX_TEMPLATE_SAFETY_DEPTH):
        payload = {f"nested_{index}": payload}

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


def test_template_safety_reports_a_mapping_beyond_the_depth_limit_deterministically() -> None:
    payload: object = {}
    for index in range(MAX_TEMPLATE_SAFETY_DEPTH + 1):
        payload = {f"nested_{index}": payload}

    first_findings = tuple(_template_safety_findings(payload))
    second_findings = tuple(_template_safety_findings(payload))

    assert first_findings == second_findings
    assert len(first_findings) == 1
    assert first_findings[0].endswith("exceeds the maximum nesting depth")


def test_template_safety_allows_a_list_at_the_exact_depth_limit() -> None:
    payload: object = []
    for _ in range(MAX_TEMPLATE_SAFETY_DEPTH):
        payload = [payload]

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


def test_template_safety_reports_a_list_beyond_the_depth_limit_deterministically() -> None:
    payload: object = []
    for _ in range(MAX_TEMPLATE_SAFETY_DEPTH + 1):
        payload = [payload]

    first_findings = tuple(_template_safety_findings(payload))
    second_findings = tuple(_template_safety_findings(payload))

    assert first_findings == second_findings
    assert len(first_findings) == 1
    assert first_findings[0].endswith("exceeds the maximum nesting depth")


@pytest.mark.parametrize("container_kind", ("mapping", "list"))
def test_template_safety_allows_wide_containers_at_the_exact_node_limit(
    container_kind: str,
) -> None:
    if container_kind == "mapping":
        payload: object = {
            f"generic_{index}": index for index in range(MAX_TEMPLATE_SAFETY_NODES - 1)
        }
    else:
        payload = list(range(MAX_TEMPLATE_SAFETY_NODES - 1))

    findings = tuple(_template_safety_findings(payload))

    assert findings == ()


@pytest.mark.parametrize("container_kind", ("mapping", "list"))
def test_template_safety_reports_wide_containers_beyond_the_node_limit(
    container_kind: str,
) -> None:
    if container_kind == "mapping":
        payload: object = {f"generic_{index}": index for index in range(MAX_TEMPLATE_SAFETY_NODES)}
    else:
        payload = list(range(MAX_TEMPLATE_SAFETY_NODES))

    first_findings = tuple(_template_safety_findings(payload))
    second_findings = tuple(_template_safety_findings(payload))

    assert (
        first_findings
        == second_findings
        == ("template safety traversal exceeds the maximum node count",)
    )


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
                    yield from _key_safety_findings(normalized_key, nested_value, next_path)
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

    yield from visit(value, (), 0)


def _normalized_key(key: str) -> str:
    """Return the only key normalization used by the bounded traversal."""
    return key.casefold().replace("-", "_")


def _key_safety_findings(
    normalized_key: str, value: object, path: tuple[str | int, ...]
) -> Iterator[str]:
    location = _path_label(path)
    if normalized_key in SEMANTIC_ASSET_KEY_NAMES:
        if not _is_generic_placeholder_asset_value(value):
            yield f"{location} requires an explicit generic placeholder asset value"
    elif _is_automatic_execution_path(path):
        if type(value) is not bool or value is not False:
            yield f"{location} enables automatic execution or mutation"
    elif _provider_credential_environment(normalized_key) == "live":
        yield f"{location} is a live provider credential field"
    elif _provider_credential_environment(normalized_key) == "production":
        yield f"{location} is a production provider credential field"
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


def _is_generic_placeholder_asset_value(value: object) -> bool:
    if type(value) is not str or not value.startswith("placeholder-"):
        return False
    return value.removeprefix("placeholder-") in GENERIC_PLACEHOLDER_ASSET_SUFFIXES


def _scalar_safety_findings(value: str, path: tuple[str | int, ...]) -> Iterator[str]:
    location = _path_label(path)
    if len(value) > MAX_TEMPLATE_SAFETY_TEXT_LENGTH:
        yield f"{location} exceeds the maximum length"
        return

    if _is_bare_ticker_style_scalar(value) and not _is_named_generic_code_path(path):
        yield f"{location} contains a ticker-style asset identifier"
        return

    normalized_value = value.casefold()
    value_parts = _normalized_value_parts(normalized_value)
    assignment_key = _explicit_assignment_key(value)
    if "@" in normalized_value and "." in normalized_value.rsplit("@", maxsplit=1)[-1]:
        yield f"{location} contains a notification recipient"
    elif assignment_key is not None and _provider_credential_environment(assignment_key) == "live":
        yield f"{location} contains a live provider credential"
    elif (
        assignment_key is not None
        and _provider_credential_environment(assignment_key) == "production"
    ):
        yield f"{location} contains a production provider credential"
    elif assignment_key in CREDENTIAL_KEY_NAMES:
        yield f"{location} contains a credential"
    elif assignment_key in NOTIFICATION_RECIPIENT_KEY_NAMES - NOTIFICATION_DESTINATION_KEY_NAMES:
        yield f"{location} contains a notification recipient"
    elif assignment_key in NOTIFICATION_DESTINATION_KEY_NAMES:
        yield f"{location} contains a notification destination"
    elif _has_value_phrase(value_parts, ("automatic", "order", "submission", "enabled")):
        yield f"{location} enables automatic execution"


def _is_bare_ticker_style_scalar(value: str) -> bool:
    return value.isascii() and value.isalpha() and value.isupper() and 1 <= len(value) <= 5


def _is_named_generic_code_path(path: tuple[str | int, ...]) -> bool:
    return bool(path) and isinstance(path[-1], str) and path[-1] in GENERIC_CODE_KEY_NAMES


def _explicit_assignment_key(value: str) -> str | None:
    for separator in ("=", ":"):
        raw_key, marker, raw_value = value.partition(separator)
        if marker and raw_key.strip() and raw_value.strip():
            return _normalized_key(raw_key.strip())
    return None


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
