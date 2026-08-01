"""ASGI application entry point."""

from fastapi import FastAPI


def create_application() -> FastAPI:
    """Create the application; routes are added in later milestones."""
    return FastAPI()


app = create_application()
