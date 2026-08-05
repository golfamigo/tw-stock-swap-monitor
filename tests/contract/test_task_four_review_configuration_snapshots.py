"""Regression contracts for immutable, scoped configuration snapshot repositories."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.domain.access import AccessContext
from app.domain.enums import Scope
from app.domain.errors import IdempotencyConflictError, NotFoundForActor
from app.domain.values import Ownership
from app.persistence.models import Base, PortfolioModel, UserModel
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


def _snapshot(
    *,
    snapshot_id: UUID | None = None,
    owner: Ownership,
    created_by: UUID,
    config_version: int = 1,
    target_reference_id: UUID | None = None,
) -> Any:
    records = importlib.import_module("app.persistence.records")
    payload: dict[str, object] = {
        "configuration_name": "snapshot-repository-contract",
        "settings": {"source": "validated"},
        "rules": [],
        "keyed_items": [],
        "extensions": {},
        "nullable_note": None,
    }
    resolved = ResolvedConfigurationSnapshot(
        payload=payload,
        parent_versions=(),
        created_by=created_by,
        created_at=NOW,
        runtime_expires_at=None,
        canonical_format_version=CANONICAL_FORMAT_VERSION,
        content_hash=canonical_content_hash(payload),
        canonical_json=canonical_json(payload),
    )
    return records.PersistedConfigurationSnapshot(
        snapshot_id=snapshot_id or uuid4(),
        resolved_snapshot=resolved,
        config_version=config_version,
        target_ownership=owner,
        target_reference_id=target_reference_id or uuid4(),
    )


def _repository_module() -> Any:
    module_name = "app.repositories.configuration_snapshots"
    assert importlib.util.find_spec(module_name) is not None, f"{module_name} must exist"
    return importlib.import_module(module_name)


def test_configuration_snapshot_port_is_framework_independent() -> None:
    module = _repository_module()
    source = importlib.util.find_spec(module.__name__)
    assert source is not None and source.origin is not None
    contents = Path(source.origin).read_text(encoding="utf-8")

    assert "class ConfigurationSnapshotRepository" in contents
    assert "sqlalchemy" not in contents.lower()
    assert "fastapi" not in contents.lower()


def test_in_memory_snapshot_repository_is_exact_idempotent_and_scoped() -> None:
    memory = importlib.import_module("app.persistence.in_memory")
    owner_id, other_id = uuid4(), uuid4()
    snapshot = _snapshot(owner=Ownership(Scope.USER, owner_id), created_by=owner_id)
    repository = memory.InMemoryConfigurationSnapshotRepository({})

    first = repository.record_or_get(snapshot=snapshot, access_context=_context(owner_id))
    assert repository.record_or_get(snapshot=snapshot, access_context=_context(owner_id)) == first
    assert repository.get(snapshot.snapshot_id, access_context=_context(owner_id)) == first
    with pytest.raises(NotFoundForActor):
        repository.get(snapshot.snapshot_id, access_context=_context(other_id))
    with pytest.raises(IdempotencyConflictError):
        repository.record_or_get(
            snapshot=replace(snapshot, config_version=2), access_context=_context(owner_id)
        )


def test_in_memory_snapshot_repository_requires_the_exact_portfolio_owner() -> None:
    memory = importlib.import_module("app.persistence.in_memory")
    owner_id, other_id, portfolio_id = uuid4(), uuid4(), uuid4()
    snapshot = _snapshot(
        owner=Ownership(Scope.PORTFOLIO, portfolio_id),
        created_by=owner_id,
    )
    repository = memory.InMemoryConfigurationSnapshotRepository({portfolio_id: owner_id})

    repository.record_or_get(snapshot=snapshot, access_context=_context(owner_id))
    with pytest.raises(NotFoundForActor):
        repository.get(snapshot.snapshot_id, access_context=_context(other_id))


def test_sql_snapshot_repository_round_trips_canonical_evidence_and_enforces_tenant_access() -> (
    None
):
    persistence = importlib.import_module("app.persistence.repositories")
    owner_id, other_id, portfolio_id = uuid4(), uuid4(), uuid4()
    snapshot = _snapshot(
        owner=Ownership(Scope.PORTFOLIO, portfolio_id),
        created_by=owner_id,
    )
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            (
                UserModel(user_id=owner_id, created_at=NOW),
                UserModel(user_id=other_id, created_at=NOW),
                PortfolioModel(portfolio_id=portfolio_id, user_id=owner_id, created_at=NOW),
            )
        )
        session.commit()
        repository = persistence.SqlAlchemyConfigurationSnapshotRepository(session)

        first = repository.record_or_get(snapshot=snapshot, access_context=_context(owner_id))
        session.commit()
        assert repository.get(snapshot.snapshot_id, access_context=_context(owner_id)) == first
        with pytest.raises(NotFoundForActor):
            repository.get(snapshot.snapshot_id, access_context=_context(other_id))
        with pytest.raises(IdempotencyConflictError):
            repository.record_or_get(
                snapshot=replace(snapshot, config_version=2), access_context=_context(owner_id)
            )


def test_sql_snapshot_repository_revalidates_corrupted_canonical_evidence_on_read() -> None:
    persistence = importlib.import_module("app.persistence.repositories")
    owner_id = uuid4()
    snapshot = _snapshot(owner=Ownership(Scope.USER, owner_id), created_by=owner_id)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(UserModel(user_id=owner_id, created_at=NOW))
        session.commit()
        repository = persistence.SqlAlchemyConfigurationSnapshotRepository(session)
        repository.record_or_get(snapshot=snapshot, access_context=_context(owner_id))
        session.commit()
        stored = session.get(
            importlib.import_module("app.persistence.models").ConfigurationSnapshotModel,
            snapshot.snapshot_id,
        )
        assert stored is not None
        stored.canonical_json = "{}"
        session.commit()

        with pytest.raises(ValueError, match="canonical_json"):
            repository.get(snapshot.snapshot_id, access_context=_context(owner_id))
