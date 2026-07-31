# Architecture

## Scope

Milestone 0 and 1 establish a configuration-driven, deterministic rotation core. The deliverable includes a minimal FastAPI composition root and a protected administrative dry-run entry point, but not CRUD APIs, database-backed execution, a scheduler, a real market-data connection, notifications, LLM calls, or broker order submission. No code may contain a market symbol, a user position, a trading threshold, a schedule, a staged allocation, a cost, a model name, or a notification target.

## Layer boundaries

~~~text
FastAPI / worker adapters
        |
application services  ---- repository and lock Protocols
        |
domain models, invariants, value objects, events
        |
adapters: mock data, in-memory repositories, SQLAlchemy persistence
        |
configuration snapshots and template files
~~~

The domain layer imports only Python standard-library modules. It contains entities, value objects, enums, invariants, and domain events; it never imports FastAPI, Pydantic, pandas, SQLAlchemy, or an infrastructure package. Pydantic schemas validate external boundaries. SQLAlchemy declarative models, Alembic metadata, and concrete repositories live below persistence. Application services translate schemas and stored data into domain objects and orchestrate the Protocol interfaces.

## Planned module layout

~~~text
app/
  api/                 HTTP adapters and admin-token dependency
  application/         run coordinator, configuration resolver, commands
  domain/              pure entities, value objects, enums, invariants
  schemas/             Pydantic v2 boundary/configuration schemas
  repositories/        repository and lock Protocols
  persistence/         SQLAlchemy tables, mapper functions, implementations
  data_sources/        MarketDataProvider and deterministic mock provider
  calendar/            TradingCalendarProvider Protocol and mock calendar
  indicators/          Indicator Protocol, bar aggregation, VWAP, volume ratio
  rules/               JSON-logic validator, evaluator, evidence types
  scoring/             scoring Protocol and weighted implementation
  sizing/              sizing Protocol and Decimal implementation
  state_machine/       state/event transition table and guard evaluation
  services/            shared scan/run-once workflow
  llm/ notifications/  Protocol definitions only in M0/M1
config_templates/      YAML seed/template examples only
migrations/            Alembic initial schema
tests/                 unit, integration, contract, fixtures
~~~

## Execution flow

1. An API dry-run or future scheduler supplies a rotation-plan identifier and a run context.
2. The application service authorizes the caller, loads the plan through a scoped repository, and resolves one immutable configuration snapshot.
3. Before provider access it builds a scan-lock key from the plan, normalized market session/window/interval, and configuration hash. LockProvider serializes this logical scan. A completed scan returns its recorded strategy-run outcome and does not fetch data again.
4. The lock owner fetches validated mock bars. The provider returns a snapshot identifier and canonical market-data content hash. Together with the scan-lock key these form the final strategy-run key for the exact reproducible evaluation. Missing, stale, or malformed data emits DATA_DEGRADED and cannot yield ACTION.
5. The indicator engine aggregates bars, computes indicators, and returns typed metrics. The rule engine evaluates validated DSL expressions and preserves evidence.
6. The scoring engine ranks only plan-authorized candidates. The sizing engine calculates staged sale and purchase quantities using Decimal, configured costs and lot rules, then re-checks protected-position and funding invariants.
7. The state machine records a system recommendation state. It can expire or invalidate a recommendation without changing executed holdings. No notification, decision, or state transition changes actual positions; only a future confirmed-execution workflow may do so.

## Time and numeric policy

All persisted and domain datetimes are timezone-aware. A market calendar supplies the IANA timezone and session boundaries. Intraday bars use left-closed, right-open intervals: a 15-minute bar stamped 09:00 covers 09:00:00 inclusive through 09:15:00 exclusive. Session breaks and holidays come from the calendar, not resampling assumptions. The mock provider uses a fixed seed, fixed session timezone, and fixed market date.

Cash, prices, quantities, costs, allocation percentages, and limits use Decimal. pandas may use floating point internally for technical-series operations, but results are converted to finite Decimal values at the strategy boundary before scoring or sizing.

## Composition and future replacement

The composition root selects adapters from validated configuration: M0/M1 wires MockMarketDataProvider, InMemory repositories, an in-memory LockProvider, the mock calendar, and no-op LLM/notification adapters. M2 replaces repositories with SQLAlchemy implementations without changing domain services. M3 and M4 add provider adapters behind the existing Protocols. The coordinator is shared by API run-once and scheduled execution so they cannot diverge.

## Explicit exclusions

Automatic trading is prohibited. Provider credentials, tokens, and personal destinations are only secret references and never enter templates, logs, or API responses. Full authentication, user CRUD, worker scheduling, distributed locks, PostgreSQL runtime wiring, real data providers, notification delivery, LLM calls, and execution confirmation are deferred beyond this foundation.
