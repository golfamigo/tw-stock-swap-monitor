# Taiwan Stock Swap Monitor

This is an M0/M1 foundation for configuration-driven, multi-tenant rotation
recommendations. It is recommendation-only: it never submits orders, connects
to a broker, mutates positions, calls real market-data services, or starts a
worker or scheduler by default.

## Local development

Docker is not required for local development. Use Python 3.12 and the pinned
development dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Run the complete quality gate from the repository root:

```powershell
pytest
mypy app tests
ruff check .
ruff format --check .
git diff --check
```

The test suite is deterministic and must not require network access, real
credentials, a broker, or a running database.

## Local environment variables

Copy only the variable-name template to your ignored local file:

```powershell
Copy-Item .env.example .local.env
```

`.local.env` is never committed and is not automatically loaded by the
application. Before starting a local process, set the values in its environment
without printing them; for example, set `ADMIN_API_TOKEN` in the current
PowerShell session. `DATABASE_URL` is reserved for optional local PostgreSQL
development and is not required for the in-memory API or test suite.

In Zeabur production, configure values only as Zeabur environment variables.
Do not copy a local environment file into an image, commit it, or use it as a
production configuration source.

## Guarded dry-run API

`POST /admin/run-once` accepts a UUID `rotation_plan_id` and requires
`dry_run: true`; every other value is rejected. The endpoint also requires the
`X-Admin-Token` header to match `ADMIN_API_TOKEN`; it is disabled when that
environment variable is absent or blank. The admin request body is limited to
16,384 bytes before JSON parsing.

Even an accepted request is a dry-run recommendation attempt. It creates no
order, changes no holding or position, and does not enable real market data or
broker access. The default application composition has no configured plan, so
an explicit deployment composition is still required for a usable dry run.

## Configuration templates

`config_templates/` contains generic YAML seed patches, not live operating
profiles. They contain no asset symbols, holdings, schedules, costs,
notification recipients, secrets, or API credentials. The templates validate
against the sparse Pydantic layer schema; the strategy seed also uses a
validated generic evidence rule. The scoring and sizing seeds intentionally
remain disabled until a future configuration-to-engine binding supplies a
reviewed policy.

Apply templates as configuration layers and validate the final merged payload
before creating an immutable snapshot. YAML is a seed/import format only; run
execution reads the validated snapshot rather than an arbitrary file.

## Container artifact

Docker remains an optional production/deployment artifact:

```powershell
docker compose up --build
```

The compose file starts only the HTTP API on port 8000. It does not inject a
local environment file, provision PostgreSQL, or start a worker, scheduler,
market-data integration, or trading process. Supply any required runtime
values through the deployment environment.

## Deferred scope

The following are intentionally deferred: live provider connections, live
PostgreSQL operation, login and portfolio CRUD APIs, scheduler and distributed
locking adapters, notifications, LLM calls, paper-trading accounting, execution
confirmation, backtesting, Zeabur deployment automation, and all broker
integration. Automatic trading is permanently out of scope.
