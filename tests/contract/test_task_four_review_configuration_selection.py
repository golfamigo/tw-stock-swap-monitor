"""Regression contracts for exact configuration-layer selection at the repository boundary."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from app.application.configuration import ConfigurationLayer, canonical_content_hash
from app.domain.access import AccessContext
from app.domain.entities import Portfolio, RotationPlan
from app.domain.enums import Scope
from app.domain.values import Ownership
from app.persistence.mappers import configuration_layer_to_model
from app.persistence.models import (
    Base,
    ConfigurationLayerModel,
    PortfolioModel,
    UserModel,
)
from app.persistence.repositories import SqlAlchemyConfigurationLayerRepository
from app.repositories.configuration_layers import (
    ConfigurationLayerSelection,
    ConfigurationLayerSelectionEntry,
)
from app.schemas.common import ConfigurationLayerScope
from app.schemas.configuration import LayerPatchSchema
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)


def _context(user_id: UUID, *, administrator: bool = False) -> AccessContext:
    return AccessContext(
        actor_user_id=user_id,
        is_administrator=administrator,
        request_id=uuid4(),
        authentication_method="contract-test",
    )


def _portfolio(user_id: UUID) -> Portfolio:
    return Portfolio(portfolio_id=uuid4(), user_id=user_id, created_at=NOW)


def _plan(portfolio: Portfolio) -> RotationPlan:
    return RotationPlan(
        rotation_plan_id=uuid4(),
        portfolio_id=portfolio.portfolio_id,
        candidate_group_ids=(uuid4(),),
        source_position_ids=(),
        protected_position_ids=(),
        created_at=NOW,
    )


def _layer(
    *,
    scope: ConfigurationLayerScope,
    reference_id: UUID,
    version: int,
    owner_id: UUID | None,
    portfolio_id: UUID | None,
    portfolio_owner_id: UUID | None,
    content_hash: str | None = None,
) -> ConfigurationLayer:
    patch = LayerPatchSchema.for_scope(scope, {"settings": {}})
    return ConfigurationLayer(
        scope=scope,
        patch=patch,
        reference_id=reference_id,
        version=version,
        content_hash=content_hash
        or canonical_content_hash(
            patch.model_dump(mode="python", by_alias=True, exclude_unset=True)
        ),
        ownership=(
            Ownership(Scope.SYSTEM, None)
            if scope
            in {
                ConfigurationLayerScope.SYSTEM,
                ConfigurationLayerScope.MARKET,
                ConfigurationLayerScope.STRATEGY,
            }
            else Ownership(Scope.USER, owner_id)
            if scope is ConfigurationLayerScope.USER
            else Ownership(Scope.PORTFOLIO, portfolio_id)
        ),
        portfolio_owner_id=portfolio_owner_id,
    )


def _selection(*layers: ConfigurationLayer) -> ConfigurationLayerSelection:
    """Build the future port value object without hiding a missing implementation."""

    return ConfigurationLayerSelection(
        entries=tuple(
            _selection_entry(
                scope=layer.scope,
                reference_id=layer.reference_id,
                version=layer.version,
                content_hash=layer.content_hash,
            )
            for layer in layers
        )
    )


def _selection_entry(
    *,
    scope: ConfigurationLayerScope,
    reference_id: UUID,
    version: int,
    content_hash: str,
) -> ConfigurationLayerSelectionEntry:
    return ConfigurationLayerSelectionEntry(
        scope=scope,
        reference_id=reference_id,
        version=version,
        content_hash=content_hash,
    )


def _selection_from_entries(
    *entries: ConfigurationLayerSelectionEntry,
) -> ConfigurationLayerSelection:
    return ConfigurationLayerSelection(entries=entries)


def test_in_memory_selection_loads_only_exact_versions_in_hierarchy_order() -> None:
    from app.persistence.in_memory import InMemoryConfigurationLayerRepository

    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    system = _layer(
        scope=ConfigurationLayerScope.SYSTEM,
        reference_id=uuid4(),
        version=1,
        owner_id=None,
        portfolio_id=None,
        portfolio_owner_id=None,
    )
    selected_user = _layer(
        scope=ConfigurationLayerScope.USER,
        reference_id=uuid4(),
        version=2,
        owner_id=owner_id,
        portfolio_id=None,
        portfolio_owner_id=None,
    )
    stale_user = _layer(
        scope=ConfigurationLayerScope.USER,
        reference_id=selected_user.reference_id,
        version=1,
        owner_id=owner_id,
        portfolio_id=None,
        portfolio_owner_id=None,
    )
    unrelated_system = _layer(
        scope=ConfigurationLayerScope.SYSTEM,
        reference_id=uuid4(),
        version=1,
        owner_id=None,
        portfolio_id=None,
        portfolio_owner_id=None,
    )
    repository = InMemoryConfigurationLayerRepository({portfolio.portfolio_id: owner_id})
    for layer, context in (
        (system, _context(owner_id, administrator=True)),
        (selected_user, _context(owner_id)),
        (stale_user, _context(owner_id)),
        (unrelated_system, _context(owner_id, administrator=True)),
    ):
        repository.add(layer, access_context=context)

    result = repository.load_for_plan(
        plan=plan,
        selection=_selection(selected_user, system),
        access_context=_context(owner_id),
    )

    assert result == (system, selected_user)


def test_selection_rejects_a_sibling_portfolio_layer_for_the_same_user() -> None:
    from app.persistence.in_memory import InMemoryConfigurationLayerRepository

    owner_id = uuid4()
    authorized_portfolio = _portfolio(owner_id)
    sibling_portfolio = _portfolio(owner_id)
    plan = _plan(authorized_portfolio)
    sibling_layer = _layer(
        scope=ConfigurationLayerScope.PORTFOLIO,
        reference_id=uuid4(),
        version=1,
        owner_id=None,
        portfolio_id=sibling_portfolio.portfolio_id,
        portfolio_owner_id=owner_id,
    )
    repository = InMemoryConfigurationLayerRepository(
        {
            authorized_portfolio.portfolio_id: owner_id,
            sibling_portfolio.portfolio_id: owner_id,
        }
    )
    repository.add(sibling_layer, access_context=_context(owner_id))

    with pytest.raises(ValueError, match="portfolio"):
        repository.load_for_plan(
            plan=plan,
            selection=_selection(sibling_layer),
            access_context=_context(owner_id),
        )


def test_selection_rejects_missing_or_duplicate_expected_identity_deterministically() -> None:
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    layer = _layer(
        scope=ConfigurationLayerScope.PORTFOLIO,
        reference_id=uuid4(),
        version=1,
        owner_id=None,
        portfolio_id=portfolio.portfolio_id,
        portfolio_owner_id=owner_id,
    )

    with pytest.raises(ValueError, match="duplicate"):
        _selection(layer, layer)


def test_sql_selection_filters_before_decoding_unrelated_malformed_rows() -> None:
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    selected = _layer(
        scope=ConfigurationLayerScope.PORTFOLIO,
        reference_id=uuid4(),
        version=3,
        owner_id=None,
        portfolio_id=portfolio.portfolio_id,
        portfolio_owner_id=owner_id,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                UserModel(user_id=owner_id, created_at=NOW),
                PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                configuration_layer_to_model(selected),
                ConfigurationLayerModel(
                    layer_id=uuid4(),
                    scope="SYSTEM",
                    reference_id=uuid4(),
                    version=1,
                    patch={"not": "a validated patch"},
                    content_hash="not-a-hash",
                    content_hash_format_version="v1",
                    ownership_scope="SYSTEM",
                    owner_id=None,
                    portfolio_owner_id=None,
                    runtime_expires_at=None,
                ),
            )
        )
        session.commit()

        result = SqlAlchemyConfigurationLayerRepository(session).load_for_plan(
            plan=plan,
            selection=_selection(selected),
            access_context=_context(owner_id),
        )

    assert result == (selected,)


def test_sql_selection_rejects_a_missing_exact_hash_without_falling_back_to_another_version() -> (
    None
):
    owner_id = uuid4()
    portfolio = _portfolio(owner_id)
    plan = _plan(portfolio)
    stored = _layer(
        scope=ConfigurationLayerScope.PORTFOLIO,
        reference_id=uuid4(),
        version=1,
        owner_id=None,
        portfolio_id=portfolio.portfolio_id,
        portfolio_owner_id=owner_id,
    )
    expected = _selection_from_entries(
        _selection_entry(
            scope=stored.scope,
            reference_id=stored.reference_id,
            version=stored.version,
            content_hash="b" * 64,
        )
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                UserModel(user_id=owner_id, created_at=NOW),
                PortfolioModel(
                    portfolio_id=portfolio.portfolio_id, user_id=owner_id, created_at=NOW
                ),
                configuration_layer_to_model(stored),
            )
        )
        session.commit()
        with pytest.raises(ValueError, match="missing"):
            SqlAlchemyConfigurationLayerRepository(session).load_for_plan(
                plan=plan,
                selection=expected,
                access_context=_context(owner_id),
            )
