"""Executable contract for deterministic configuration resolution."""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum, IntEnum
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.domain.access import AccessContext
from app.domain.enums import Scope
from app.domain.errors import NotFoundForActor
from app.domain.values import Ownership
from app.schemas.configuration import ResolvedConfigurationSchema
from pydantic import Field, ValidationError

NOW = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
RUN_DEADLINE = NOW + timedelta(minutes=30)
ACTOR_ID = UUID("00000000-0000-0000-0000-000000000001")


class StableValue(str, Enum):
    """A value whose stable wire representation differs from its member name."""

    SAMPLE = "stable-value"


class UnstableIntegerValue(IntEnum):
    """An enum without the stable string wire value required for canonical configuration."""

    SAMPLE = 1


def _load_module(module_name: str) -> Any:
    """Load a Task 3 module while keeping the initial red run an assertion failure."""

    try:
        spec = importlib.util.find_spec(module_name)
    except ModuleNotFoundError:
        spec = None
    assert spec is not None, f"{module_name} must be implemented for configuration resolution"
    return importlib.import_module(module_name)


def _api() -> dict[str, Any]:
    common = _load_module("app.schemas.common")
    schemas = _load_module("app.schemas.configuration")
    configuration = _load_module("app.application.configuration")
    return {
        "ConfigurationCanonicalizationError": configuration.ConfigurationCanonicalizationError,
        "ConfigurationLayer": configuration.ConfigurationLayer,
        "ConfigurationLayerScope": common.ConfigurationLayerScope,
        "ConfigurationMergeError": configuration.ConfigurationMergeError,
        "ConfigurationResolver": configuration.ConfigurationResolver,
        "LayerPatchSchema": schemas.LayerPatchSchema,
        "ListMergeStrategy": schemas.ListMergeStrategy,
        "ResolvedConfigurationSchema": schemas.ResolvedConfigurationSchema,
        "RuntimeOverrideExpiryError": configuration.RuntimeOverrideExpiryError,
        "canonical_content_hash": configuration.canonical_content_hash,
        "canonical_json": configuration.canonical_json,
        "list_merge_policies_for": schemas.list_merge_policies_for,
    }


def _payload() -> dict[str, object]:
    return {
        "configuration_name": "logical-configuration",
        "settings": {"nested": {"base": "system"}, "stable": "value"},
        "rules": [{"name": "system-rule"}],
        "keyed_items": [{"id": "first", "from_system": "present"}],
        "extensions": {"keep": "present", "remove": "obsolete"},
        "nullable_note": "set-by-system",
    }


def _context() -> AccessContext:
    return AccessContext(
        actor_user_id=ACTOR_ID,
        request_id=UUID("00000000-0000-0000-0000-000000000002"),
        authentication_method="test",
    )


def _ownership_for(scope: Any) -> tuple[Ownership, UUID | None]:
    if scope.value in {"SYSTEM", "MARKET", "STRATEGY"}:
        return Ownership(Scope.SYSTEM, None), None
    if scope.value == "USER":
        return Ownership(Scope.USER, ACTOR_ID), None
    if scope.value in {"PORTFOLIO", "ROTATION_PLAN", "RUNTIME_OVERRIDE"}:
        portfolio_id = UUID("00000000-0000-0000-0000-000000000003")
        return Ownership(Scope.PORTFOLIO, portfolio_id), ACTOR_ID
    raise AssertionError(f"unexpected configuration layer scope: {scope}")


def _layer(
    api: dict[str, Any],
    scope: Any,
    patch: dict[str, object],
    *,
    version: int,
    expires_at: datetime | None = None,
    ownership: Ownership | None = None,
    portfolio_owner_id: UUID | None = None,
    content_hash: str | None = None,
    content_hash_format_version: str | None = None,
) -> Any:
    layer_ownership, layer_portfolio_owner_id = _ownership_for(scope)
    normalized_patch = api["LayerPatchSchema"].for_scope(scope, patch)
    hash_format_version = content_hash_format_version or "1"
    layer_kwargs: dict[str, object] = {
        "scope": scope,
        "patch": normalized_patch,
        "reference_id": uuid4(),
        "version": version,
        "content_hash": content_hash
        if content_hash is not None
        else api["canonical_content_hash"](
            normalized_patch.model_dump(mode="python", by_alias=True, exclude_unset=True),
            format_version=hash_format_version,
        ),
        "ownership": ownership if ownership is not None else layer_ownership,
        "portfolio_owner_id": (
            portfolio_owner_id if portfolio_owner_id is not None else layer_portfolio_owner_id
        ),
        "runtime_expires_at": expires_at,
    }
    if content_hash_format_version is not None:
        layer_kwargs["content_hash_format_version"] = content_hash_format_version
    return api["ConfigurationLayer"](**layer_kwargs)


def _resolver(api: dict[str, Any]) -> Any:
    return api["ConfigurationResolver"](maximum_runtime_ttl=timedelta(hours=1))


def _resolve(api: dict[str, Any], layers: list[Any]) -> Any:
    return _resolver(api).resolve(
        layers,
        access_context=_context(),
        created_by=ACTOR_ID,
        created_at=NOW,
        now=NOW,
        run_deadline=RUN_DEADLINE,
    )


def test_task_three_modules_exist() -> None:
    """The planned boundary and application modules are explicit project artifacts."""

    for module_name in (
        "app.schemas.common",
        "app.schemas.configuration",
        "app.schemas.requests",
        "app.schemas.responses",
        "app.application.configuration",
    ):
        _load_module(module_name)


def test_patch_is_sparse_while_resolved_configuration_requires_execution_fields() -> None:
    api = _api()
    patch = api["LayerPatchSchema"].model_validate({"configuration_name": "logical"})

    assert patch.model_dump(exclude_unset=True) == {"configuration_name": "logical"}
    with pytest.raises(ValidationError):
        api["LayerPatchSchema"].model_validate({"unknown": "field"})
    with pytest.raises(ValidationError):
        api["ResolvedConfigurationSchema"].model_validate({"configuration_name": "logical"})


def test_layer_patch_rejects_fields_not_permitted_for_the_target_scope() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]

    with pytest.raises(ValidationError, match="PORTFOLIO"):
        api["LayerPatchSchema"].for_scope(
            scope_type.PORTFOLIO, {"configuration_name": "global-only-name"}
        )


@pytest.mark.parametrize(
    ("layer_scope", "ownership_scope", "requires_owner"),
    [
        pytest.param("SYSTEM", Scope.SYSTEM, False, id="system"),
        pytest.param("MARKET", Scope.SYSTEM, False, id="market"),
        pytest.param("STRATEGY", Scope.SYSTEM, False, id="strategy"),
        pytest.param("USER", Scope.USER, True, id="user"),
        pytest.param("PORTFOLIO", Scope.PORTFOLIO, True, id="portfolio"),
        pytest.param("ROTATION_PLAN", Scope.PORTFOLIO, True, id="rotation-plan"),
        pytest.param("RUNTIME_OVERRIDE", Scope.PORTFOLIO, True, id="runtime-override"),
    ],
)
def test_configuration_layer_scope_has_an_explicit_ownership_policy(
    layer_scope: str, ownership_scope: Scope, requires_owner: bool
) -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    scope = scope_type(layer_scope)
    ownership, portfolio_owner_id = _ownership_for(scope)

    layer = _layer(
        api,
        scope,
        {},
        version=1,
        ownership=ownership,
        portfolio_owner_id=portfolio_owner_id,
    )

    assert layer.ownership.scope is ownership_scope
    assert (layer.ownership.owner_id is not None) is requires_owner


def test_portfolio_layer_rejects_system_ownership_before_resolution() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]

    with pytest.raises(ValueError, match="PORTFOLIO.*PORTFOLIO"):
        _layer(
            api,
            scope_type.PORTFOLIO,
            {"settings": {"scope": "invalid"}},
            version=1,
            ownership=Ownership(Scope.SYSTEM, None),
        )


@pytest.mark.parametrize(
    "winning_scope",
    [
        "SYSTEM",
        "MARKET",
        "STRATEGY",
        "USER",
        "PORTFOLIO",
        "ROTATION_PLAN",
        "RUNTIME_OVERRIDE",
    ],
)
def test_each_of_the_seven_layers_has_the_documented_precedence(winning_scope: str) -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    ordered_scopes = tuple(scope_type)
    winner_index = [scope.value for scope in ordered_scopes].index(winning_scope)
    system_payload = _payload()
    system_payload["nullable_note"] = scope_type.SYSTEM.value
    layers = [
        _layer(api, scope_type.SYSTEM, system_payload, version=1),
        *[
            _layer(
                api,
                scope,
                {"nullable_note": scope.value},
                version=index + 2,
                expires_at=NOW + timedelta(minutes=10)
                if scope is scope_type.RUNTIME_OVERRIDE
                else None,
            )
            for index, scope in enumerate(ordered_scopes[1 : winner_index + 1])
        ],
    ]

    snapshot = _resolve(api, layers)

    assert snapshot.payload["nullable_note"] == winning_scope
    assert [parent.scope for parent in snapshot.parent_versions] == list(
        ordered_scopes[: winner_index + 1]
    )


def test_resolver_recursively_merges_maps_replaces_normal_lists_and_merges_keyed_lists() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    snapshot = _resolve(
        api,
        [
            _layer(api, scope_type.SYSTEM, _payload(), version=1),
            _layer(
                api,
                scope_type.STRATEGY,
                {
                    "settings": {"nested": {"strategy": "added"}},
                    "rules": [{"name": "strategy-rule"}],
                    "keyed_items": [
                        {"id": "first", "from_strategy": "present"},
                        {"id": "second", "from_strategy": "new"},
                    ],
                },
                version=2,
            ),
        ],
    )

    assert snapshot.payload["settings"] == {
        "nested": {"base": "system", "strategy": "added"},
        "stable": "value",
    }
    assert list(snapshot.payload["rules"]) == [{"name": "strategy-rule"}]
    assert list(snapshot.payload["keyed_items"]) == [
        {"id": "first", "from_system": "present", "from_strategy": "present"},
        {"id": "second", "from_strategy": "new"},
    ]


def test_list_merge_policies_are_declared_by_resolved_schema_metadata() -> None:
    api = _api()
    policies = api["list_merge_policies_for"](api["ResolvedConfigurationSchema"])

    assert policies["rules"].strategy is api["ListMergeStrategy"].REPLACE
    assert policies["rules"].key is None
    assert policies["keyed_items"].strategy is api["ListMergeStrategy"].KEYED
    assert policies["keyed_items"].key == "id"


def test_resolver_uses_injected_schema_metadata_for_keyed_list_merging() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]

    class RulesKeyedSchema(ResolvedConfigurationSchema):
        rules: list[dict[str, object]] = Field(
            json_schema_extra={"merge_policy": {"strategy": "keyed", "key": "id"}}
        )

    system_payload = _payload()
    system_payload["rules"] = [{"id": "first", "from_system": "present"}]
    resolver = api["ConfigurationResolver"](
        maximum_runtime_ttl=timedelta(hours=1), resolved_configuration_schema=RulesKeyedSchema
    )

    snapshot = resolver.resolve(
        [
            _layer(api, scope_type.SYSTEM, system_payload, version=1),
            _layer(
                api,
                scope_type.STRATEGY,
                {"rules": [{"id": "first", "from_strategy": "present"}]},
                version=2,
            ),
        ],
        access_context=_context(),
        created_by=ACTOR_ID,
        created_at=NOW,
        now=NOW,
        run_deadline=RUN_DEADLINE,
    )

    assert list(snapshot.payload["rules"]) == [
        {"id": "first", "from_system": "present", "from_strategy": "present"}
    ]


def test_nullable_fields_replace_with_null_and_only_extensions_allow_typed_delete() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    snapshot = _resolve(
        api,
        [
            _layer(api, scope_type.SYSTEM, _payload(), version=1),
            _layer(
                api,
                scope_type.USER,
                {"nullable_note": None, "extensions": {"remove": {"$delete": True}}},
                version=2,
            ),
        ],
    )

    assert snapshot.payload["nullable_note"] is None
    assert snapshot.payload["extensions"] == {"keep": "present"}
    with pytest.raises(ValidationError):
        api["LayerPatchSchema"].model_validate({"settings": None})
    with pytest.raises(ValidationError):
        api["LayerPatchSchema"].model_validate({"settings": {"old": {"$delete": True}}})


def test_non_null_incompatible_values_fail_before_final_schema_validation() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    layers = [
        _layer(api, scope_type.SYSTEM, _payload(), version=1),
        _layer(api, scope_type.STRATEGY, {"settings": {"nested": ["invalid"]}}, version=2),
    ]

    with pytest.raises(api["ConfigurationMergeError"], match="incompatible"):
        _resolve(api, layers)


@pytest.mark.parametrize(
    ("expires_at", "run_deadline"),
    [
        pytest.param(NOW, RUN_DEADLINE, id="already-expired"),
        pytest.param(RUN_DEADLINE + timedelta(microseconds=1), RUN_DEADLINE, id="after-deadline"),
        pytest.param(
            NOW + timedelta(hours=1, microseconds=1), NOW + timedelta(hours=2), id="over-ttl"
        ),
    ],
)
def test_runtime_overrides_require_a_valid_bounded_expiry(
    expires_at: datetime, run_deadline: datetime
) -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    resolver = _resolver(api)
    layers = [
        _layer(api, scope_type.SYSTEM, _payload(), version=1),
        _layer(
            api,
            scope_type.RUNTIME_OVERRIDE,
            {"nullable_note": "runtime"},
            version=2,
            expires_at=expires_at,
        ),
    ]

    with pytest.raises(api["RuntimeOverrideExpiryError"]):
        resolver.resolve(
            layers,
            access_context=_context(),
            created_by=ACTOR_ID,
            created_at=NOW,
            now=NOW,
            run_deadline=run_deadline,
        )


def test_resolver_hides_parent_configuration_not_owned_by_the_actor() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    layer = _layer(
        api,
        scope_type.USER,
        {"settings": {"scope": "user"}},
        version=1,
        ownership=Ownership(Scope.USER, uuid4()),
    )

    with pytest.raises(NotFoundForActor):
        _resolve(api, [layer])


def test_configuration_layer_rejects_a_content_hash_that_does_not_match_its_patch() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]

    with pytest.raises(api["ConfigurationCanonicalizationError"], match="content_hash"):
        _layer(
            api,
            scope_type.SYSTEM,
            {"settings": {"source": "system"}},
            version=1,
            content_hash="0" * 64,
        )


def test_configuration_layer_hashes_the_alias_preserving_sparse_delete_payload() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    layer = _layer(
        api,
        scope_type.SYSTEM,
        {"extensions": {"retired": {"$delete": True}}},
        version=1,
    )
    canonical_patch = layer.patch.model_dump(mode="python", by_alias=True, exclude_unset=True)

    assert api["canonical_json"](canonical_patch) == '{"extensions":{"retired":{"$delete":true}}}'
    assert layer.content_hash == api["canonical_content_hash"](canonical_patch)
    assert layer.content_hash == api["canonical_content_hash"](
        {"extensions": {"retired": {"$delete": True}}}
    )


def test_parent_versions_record_the_hash_format_used_by_each_layer() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    snapshot = _resolve(api, [_layer(api, scope_type.SYSTEM, _payload(), version=1)])

    assert snapshot.parent_versions[0].content_hash_format_version == "1"


def test_configuration_layer_rejects_unsupported_hash_format_versions() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]

    with pytest.raises(api["ConfigurationCanonicalizationError"], match="format version"):
        _layer(
            api,
            scope_type.SYSTEM,
            {"settings": {"source": "system"}},
            version=1,
            content_hash_format_version="2",
        )


def test_canonical_json_and_hash_follow_the_versioned_contract() -> None:
    api = _api()
    payload = {
        "z": Decimal("-0.00"),
        "a": {
            "when": datetime(2026, 8, 1, 17, 0, 1, 20, tzinfo=timezone(timedelta(hours=8))),
            "date": date(2026, 8, 1),
            "uuid": UUID("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"),
            "enum": StableValue.SAMPLE,
            "unicode": "e\u0301",
        },
    }

    canonical = api["canonical_json"](payload)

    assert canonical == (
        '{"a":{"date":"2026-08-01","enum":"stable-value","unicode":"é",'
        '"uuid":"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",'
        '"when":"2026-08-01T09:00:01.000020Z"},"z":0}'
    )
    assert api["canonical_content_hash"](payload) == api["canonical_content_hash"](
        {"z": Decimal("0.0"), "a": payload["a"]}
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(1.25, id="float"),
        pytest.param(Decimal("NaN"), id="decimal-nan"),
        pytest.param(Decimal("Infinity"), id="decimal-infinity"),
        pytest.param(UnstableIntegerValue.SAMPLE, id="integer-enum"),
        pytest.param(datetime(2026, 8, 1, 9), id="naive-datetime"),
        pytest.param({"é": "first", "e\u0301": "second"}, id="duplicate-normalized-key"),
        pytest.param({"unordered"}, id="set"),
        pytest.param(b"bytes", id="bytes"),
    ],
)
def test_canonicalization_rejects_non_reproducible_values(value: object) -> None:
    api = _api()

    with pytest.raises(api["ConfigurationCanonicalizationError"]):
        api["canonical_json"](value)


def test_canonical_output_and_hash_are_stable_across_processes() -> None:
    api = _api()
    payload = {"unicode": "e\u0301", "decimal": Decimal("1.00")}
    command = "\n".join(
        (
            "from decimal import Decimal",
            "import sys",
            "from app.application.configuration import canonical_content_hash, canonical_json",
            "sys.stdout.reconfigure(encoding='utf-8')",
            "payload = {'unicode': 'e\\u0301', 'decimal': Decimal('1.00')}",
            "print(canonical_json(payload))",
            "print(canonical_content_hash(payload))",
        )
    )

    process = subprocess.run(
        [sys.executable, "-c", command],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
    )

    assert process.returncode == 0, process.stderr
    assert process.stdout.splitlines() == [
        api["canonical_json"](payload),
        api["canonical_content_hash"](payload),
    ]


def test_resolved_snapshot_deeply_freezes_payload_and_keeps_its_serialized_content() -> None:
    api = _api()
    scope_type = api["ConfigurationLayerScope"]
    supplied = _payload()
    snapshot = _resolve(api, [_layer(api, scope_type.SYSTEM, supplied, version=1)])
    original_canonical_json = snapshot.canonical_json

    settings = supplied["settings"]
    assert isinstance(settings, dict)
    nested = settings["nested"]
    assert isinstance(nested, dict)
    nested["base"] = "changed-after-resolution"

    assert snapshot.payload["settings"]["nested"]["base"] == "system"
    assert snapshot.canonical_json == original_canonical_json
    with pytest.raises(TypeError):
        snapshot.payload["settings"]["new"] = "mutation"
