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
