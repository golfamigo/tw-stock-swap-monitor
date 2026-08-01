from pathlib import Path

from fastapi import FastAPI


def test_application_factory_returns_fastapi_application() -> None:
    """The bootstrap package exposes an importable ASGI application factory."""
    import app.main

    repository_root = Path(__file__).resolve().parents[2]
    assert Path(app.main.__file__).resolve().is_relative_to(repository_root)

    application = app.main.create_application()

    assert isinstance(application, FastAPI)
