"""Regression contracts for the CI PostgreSQL migration target."""

from pathlib import Path


def test_alembic_uses_the_ci_postgresql_url_override_when_supplied() -> None:
    environment = Path("migrations/env.py").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "POSTGRES_TEST_DATABASE_URL" in environment
    assert 'config.set_main_option("sqlalchemy.url"' in environment
    assert "POSTGRES_TEST_DATABASE_URL" in workflow
    assert "python -m alembic upgrade head" in workflow
    assert "python -m alembic downgrade base" in workflow


def test_runtime_database_url_is_the_production_default_and_ci_uses_a_nondev_smoke() -> None:
    environment = Path("migrations/env.py").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert 'os.environ.get("DATABASE_URL")' in environment
    assert "POSTGRES_TEST_DATABASE_URL" in environment
    assert "runtime-postgres-smoke" in workflow
    assert "python -m pip install ." in workflow
    assert "DATABASE_URL" in workflow
