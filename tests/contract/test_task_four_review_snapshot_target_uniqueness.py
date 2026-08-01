"""Regression contracts for one canonical snapshot per target and config version."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import get_type_hints
from uuid import UUID, uuid4

import pytest
from app.application.configuration import (
    CANONICAL_FORMAT_VERSION,
    ResolvedConfigurationSnapshot,
    canonical_content_hash,
    canonical_json,
)
from app.application.configuration_snapshots import PersistedConfigurationSnapshot
from app.domain.access import AccessContext
from app.domain.enums import Scope
from app.domain.errors import IdempotencyConflictError
from app.domain.values import Ownership
from app.persistence.mappers import configuration_snapshot_to_model
from app.persistence.models import Base, UserModel
from app.persistence.repositories import SqlAlchemyConfigurationSnapshotRepository
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
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
    created_by: UUID,
    target_ownership: Ownership,
    configuration_name: str,
    config_version: int = 1,
    target_reference_id: UUID | None = None,
) -> PersistedConfigurationSnapshot:
    payload: dict[str, object] = {
        "configuration_name": configuration_name,
        "settings": {"target": target_ownership.scope.value},
        "rules": [],
        "keyed_items": [],
        "extensions": {},
        "nullable_note": None,
    }
    return PersistedConfigurationSnapshot(
        snapshot_id=uuid4(),
        resolved_snapshot=ResolvedConfigurationSnapshot(
            payload=payload,
            parent_versions=(),
            created_by=created_by,
            created_at=NOW,
            runtime_expires_at=None,
            canonical_format_version=CANONICAL_FORMAT_VERSION,
            content_hash=canonical_content_hash(payload),
            canonical_json=canonical_json(payload),
        ),
        config_version=config_version,
        target_ownership=target_ownership,
        target_reference_id=target_reference_id or uuid4(),
    )


@pytest.mark.parametrize(
    ("target_ownership", "administrator"),
    [
        pytest.param(lambda owner_id: Ownership(Scope.USER, owner_id), False, id="user"),
        pytest.param(lambda _: Ownership(Scope.SYSTEM, None), True, id="system-null-owner"),
    ],
)
def test_in_memory_rejects_a_second_snapshot_for_the_same_target_version(
    target_ownership: Callable[[UUID], Ownership], administrator: bool
) -> None:
    from app.persistence.in_memory import InMemoryConfigurationSnapshotRepository

    owner_id = uuid4()
    ownership = target_ownership(owner_id)
    target_reference_id = uuid4()
    first = _snapshot(
        created_by=owner_id,
        target_ownership=ownership,
        configuration_name="first",
        target_reference_id=target_reference_id,
    )
    contender = _snapshot(
        created_by=owner_id,
        target_ownership=ownership,
        configuration_name="contender",
        target_reference_id=target_reference_id,
    )
    repository = InMemoryConfigurationSnapshotRepository({})

    stored = repository.record_or_get(
        snapshot=first, access_context=_context(owner_id, administrator=administrator)
    )
    assert (
        repository.record_or_get(
            snapshot=first, access_context=_context(owner_id, administrator=administrator)
        )
        == stored
    )
    with pytest.raises(IdempotencyConflictError, match="target.*version"):
        repository.record_or_get(
            snapshot=contender,
            access_context=_context(owner_id, administrator=administrator),
        )


def test_sql_repository_and_database_reject_same_system_null_owner_target_version() -> None:
    owner_id = uuid4()
    target_reference_id = uuid4()
    first = _snapshot(
        created_by=owner_id,
        target_ownership=Ownership(Scope.SYSTEM, None),
        configuration_name="first",
        target_reference_id=target_reference_id,
    )
    contender = _snapshot(
        created_by=owner_id,
        target_ownership=Ownership(Scope.SYSTEM, None),
        configuration_name="contender",
        target_reference_id=target_reference_id,
    )
    repository_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(repository_engine)

    with Session(repository_engine) as session:
        session.add(UserModel(user_id=owner_id, created_at=NOW))
        session.commit()
        repository = SqlAlchemyConfigurationSnapshotRepository(session)
        repository.record_or_get(
            snapshot=first, access_context=_context(owner_id, administrator=True)
        )
        with pytest.raises(IdempotencyConflictError, match="target.*version"):
            repository.record_or_get(
                snapshot=contender,
                access_context=_context(owner_id, administrator=True),
            )

    database_engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(database_engine)
    with Session(database_engine) as session:
        session.add(UserModel(user_id=owner_id, created_at=NOW))
        session.commit()
        session.add(configuration_snapshot_to_model(first))
        session.commit()
        session.add(configuration_snapshot_to_model(contender))
        with pytest.raises(IntegrityError):
            session.commit()


def test_frozen_migration_has_null_safe_target_version_uniqueness() -> None:
    migration = Path("migrations/versions/0001_foundation.py").read_text(encoding="utf-8")

    assert "uq_configuration_snapshot_target_version_system" in migration
    assert "uq_configuration_snapshot_target_version_owned" in migration


def test_snapshot_target_version_key_is_typed_as_the_full_identity_tuple() -> None:
    from app.persistence.in_memory import _snapshot_target_version_key

    assert (
        get_type_hints(_snapshot_target_version_key)["return"]
        == tuple[Scope, UUID | None, UUID, int]
    )
