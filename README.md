# Taiwan Stock Swap Monitor

This repository contains the foundation for a configuration-driven, multi-tenant
Taiwanese stock rotation analysis system. It does not submit brokerage orders,
store brokerage credentials, or start a worker by default.

## Local development

Use Python 3.12 to create an isolated environment and install the pinned project
dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the quality checks:

```powershell
pytest
ruff check .
ruff format --check .
mypy
```

## Container

Build and run the API container with:

```powershell
docker compose up --build
```

The container starts only the ASGI API on port 8000. No worker, scheduler, market
data provider, or trading integration runs by default.

## Environment variables

Copy `.env.example` only when later milestones require secret configuration.
The template lists secret names without values; never commit `.env` or credentials.
