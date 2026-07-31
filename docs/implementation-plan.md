# Milestone 0 and 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Build a testable, configuration-driven, multi-tenant rotation-core foundation with deterministic mock data and no automatic trading.

**Architecture:** Keep pure domain logic independent from frameworks; use Pydantic at boundaries, Protocols at ports, in-memory adapters for M0/M1, and SQLAlchemy only in persistence. A single RunCoordinator resolves a frozen configuration snapshot, enforces idempotency, then invokes data, indicator, rule, scoring, sizing, and state services.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, pandas, SQLAlchemy 2, Alembic, pytest, ruff, mypy, Docker.

---

## Target file map

~~~text
app/domain/                  Pure aggregates, values, enums, invariants
app/schemas/                 Pydantic inputs, outputs, configuration
app/repositories/            Scoped repository and lock Protocols
app/persistence/             SQLAlchemy models, mappings, repositories
app/data_sources/            MarketDataProvider and deterministic mock
app/calendar/                calendar Protocol and deterministic mock
app/indicators/              aggregation and registered indicators
app/rules/                   constrained DSL validator and evaluator
app/scoring/ app/sizing/     deterministic engines
app/state_machine/           transition table and guards
app/application/             configuration resolver and RunCoordinator
app/api/                     health and guarded dry-run route
config_templates/            generic YAML templates only
migrations/                  Alembic environment and initial revision
tests/                       unit, contract, integration, fixtures
~~~

## Task 1: Bootstrap project tooling

**Files:** Create pyproject.toml, app/__init__.py, app/main.py, tests/__init__.py, .gitignore, .env.example, Dockerfile, docker-compose.yml, README.md, AGENTS.md.

**Depends on:** none.

- [ ] Add pinned compatible runtime and development dependencies for FastAPI, Pydantic v2, pandas, SQLAlchemy, Alembic, pytest, ruff, and mypy.
- [ ] Configure pytest, ruff, and mypy with strict package-local checking.
- [ ] Define only secret names in .env.example; do not insert values.
- [ ] Add Docker build instructions without embedding credentials or invoking a worker by default.
- [ ] Write a failing smoke test that imports app.main; run pytest for that test and observe failure.
- [ ] Add the minimal importable application package and an empty application factory in app/main.py; rerun the smoke test and expect pass. Task 10 extends this file rather than creating it.
- [ ] Commit with message chore: bootstrap foundation tooling.

**Acceptance:** New checkout can create an environment, run pytest, ruff check, ruff format check, and mypy without external services. **Tests:** tests/unit/test_imports.py.

## Task 2: Pure domain primitives and ownership

**Files:** Create app/domain/enums.py, values.py, access.py, entities.py, errors.py, invariants.py; tests/unit/domain/test_ownership.py and test_invariants.py.

**Depends on:** Task 1.

- [ ] Write failing tests for Scope SYSTEM, USER, PORTFOLIO; AccessContext authorization; timezone-aware timestamps; Decimal-only money/quantity; and protected position rejection.
- [ ] Implement frozen value objects and explicit domain errors without framework imports.
- [ ] Add aggregates for User, Portfolio, Position, Instrument, CandidateGroup, RotationPlan, ConfigurationSnapshotRef, and StrategyRun evidence.
- [ ] Implement invariant functions equivalent to ensure_position_is_sellable and ensure_plan_reference_is_authorized.
- [ ] Rerun focused tests, then the domain suite; commit feat: add pure ownership domain.

**Acceptance:** Domain tests run with neither FastAPI nor SQLAlchemy imported. **Tests:** protected source is rejected even if a caller supplies a sale quantity.

## Task 3: Boundary schemas and configuration merge

**Files:** Create app/schemas/common.py, configuration.py, requests.py, responses.py; app/application/configuration.py; tests/unit/config/test_resolution.py.

**Depends on:** Task 2.

- [ ] Write failing parameterized tests for LayerPatchSchema versus ResolvedConfigurationSchema, all seven precedence layers, map recursion, default list replacement, keyed-list schema merge, explicit null, delete directive, incompatible types, and expired runtime override.
- [ ] Define Pydantic v2 LayerPatchSchema models for permitted sparse layer fields and operations, plus separate ResolvedConfigurationSchema models for required executable fields and list merge strategies.
- [ ] Implement ConfigurationResolver.resolve to validate patches, merge them, validate the final object, and return its payload, ordered parent versions, creator, creation time, runtime expiry, canonical format version, and SHA-256 hash.
- [ ] Add canonicalization tests for Decimal scale and negative zero, UUID, enum, timezone-equivalent datetime, Unicode NFC, sorted keys, and cross-process output; assert floats, non-finite numbers, naive datetimes, and duplicate normalized keys fail.
- [ ] Add a test that serializes a resolved snapshot, changes a lower layer, and proves the stored result remains unchanged.
- [ ] Run configuration tests and commit feat: add versioned configuration resolution.

**Acceptance:** every run can store one complete reproducible snapshot. **Tests:** a non-null type conflict raises ConfigurationMergeError before any provider call, while final required fields are enforced only by ResolvedConfigurationSchema.

## Task 4: Repository ports and persistence projection

**Files:** Create app/repositories/base.py, positions.py, rotation_plans.py, strategy_runs.py, locks.py; app/persistence/models.py, mappers.py, repositories.py; alembic.ini, migrations/env.py, migrations/versions/0001_foundation.py; tests/contract/test_repositories.py.

**Depends on:** Tasks 2 and 3.

- [ ] Write contract tests that fetch by bare identifier fails without authorized AccessContext, global instruments are readable but not user-mutable, and duplicate strategy keys return one run.
- [ ] Define Protocol methods accepting typed owner/access inputs rather than unscoped IDs.
- [ ] Implement in-memory repositories and lock provider for M0/M1.
- [ ] Add separate SQLAlchemy 2 tables, association tables, scope constraints, configuration snapshot columns, logical scan-run and scan-attempt tables, final strategy-run key constraints, and a partial unique position index applying only when status is OPEN.
- [ ] Generate an Alembic initial revision from the persistence metadata and test migration metadata against declared tables.
- [ ] Run contract tests against in-memory adapters; commit feat: add scoped persistence foundation.

**Acceptance:** no domain module imports SQLAlchemy and repository callers cannot cross tenant boundaries. **Tests:** concurrent acquisition of one scan-lock key permits one winner and CLOSED position history remains insertable.

## Task 5: Market calendar and deterministic mock provider

**Files:** Create app/data_sources/base.py, providers/mock.py, models.py; app/calendar/base.py, mock.py; tests/contract/test_market_data_provider.py, tests/unit/data_sources/test_mock_reproducibility.py.

**Depends on:** Tasks 2 and 3.

- [ ] Write failing tests for same seed/full request yielding identical bars, different seed changing output, fixed market timezone, and awareness of holidays and session breaks.
- [ ] Define MarketDataProvider and TradingCalendarProvider Protocols using domain types.
- [ ] Implement mock bars generated from seed, requested interval, fixed trading day, and configured calendar; attach snapshot ID and data-quality metadata.
- [ ] Reject invalid intervals, naive datetimes, missing OHLCV fields, and stale data.
- [ ] Run provider contracts and commit feat: add deterministic market data adapters.

**Acceptance:** no network access occurs in the test suite. **Tests:** a stale or incomplete snapshot cannot be marked actionable.

## Task 6: Bar aggregation and indicators

**Files:** Create app/indicators/base.py, registry.py, bars.py, vwap.py, volume_ratio.py; tests/unit/indicators/test_bars.py, test_vwap.py, test_volume_ratio.py.

**Depends on:** Task 5.

- [ ] Write failing tests defining left-closed/right-open 3-minute and 15-minute windows, session reset, break exclusion, VWAP, and same-time volume lookback.
- [ ] Implement pandas aggregation with timezone-preserving index validation.
- [ ] Define Indicator Protocol and registry; implement session VWAP and same-time volume ratio.
- [ ] Convert output to finite Decimal metric values at the strategy boundary and retain missing-data status.
- [ ] Run indicator tests and commit feat: add bar and indicator engine.

**Acceptance:** bar boundary behavior is explicit and market-calendar driven. **Tests:** K bars do not bridge a break or holiday.

## Task 7: Constrained rule DSL

**Files:** Create app/rules/schema.py, parser.py, engine.py, evidence.py; tests/unit/rules/test_validation.py, test_engine.py.

**Depends on:** Tasks 2, 3, and 6.

- [ ] Write failing tests for rejected eval-like input, unapproved operators, unknown paths, deep/large expressions, mismatched types, division by zero, and missing evidence.
- [ ] Implement JSON Schema transport validation plus semantic AST validation against the registered evidence schema.
- [ ] Implement typed evaluation using only the documented operators and bounded resource budget.
- [ ] Return matched, failed, and missing evidence without coercing missing values.
- [ ] Run DSL tests and commit feat: add safe rule evaluation.

**Acceptance:** arbitrary Python cannot be evaluated through a rule. **Tests:** source weakening is expressed only through a generic configured ruleset.

## Task 8: Scoring and sizing engines

**Files:** Create app/scoring/base.py, engine.py; app/sizing/base.py, engine.py, costs.py; tests/unit/scoring/test_engine.py, tests/unit/sizing/test_engine.py.

**Depends on:** Tasks 2, 3, and 6.

- [ ] Write failing scoring tests for configured weights, directions, normalizations, ties, missing metrics, and candidate authorization.
- [ ] Implement ScoringEngine returning auditable per-factor contributions.
- [ ] Write failing sizing tests for three configured stages, fee and tax profiles, slippage, reserve, weights, minimum quantity, odd-lot policy, insufficient cash, and protected positions.
- [ ] Implement Decimal cost/sizing calculations with the funding inequality enforced after rounding.
- [ ] Run focused suites and commit feat: add deterministic scoring and sizing.

**Acceptance:** stages and market rules are injected configuration, never literals. **Tests:** all target values plus costs and reserve fit available cash plus net proceeds.

## Task 9: State, idempotency, and run coordinator

**Files:** Create app/state_machine/states.py, machine.py; app/application/idempotency.py, run_coordinator.py; app/services/rotation_run.py; tests/unit/state_machine/test_transitions.py, tests/integration/test_run_concurrency.py.

**Depends on:** Tasks 3 through 8.

- [ ] Write failing transition-table tests for pending/notified invalidation, degraded data from every active state, partial-stage halt/recovery, and duplicate scan-lock tests for API and scheduler callers.
- [ ] Implement a code-defined transition graph whose guards consume validated rules and sizing results.
- [ ] Implement ScanLockKeyBuilder from plan, normalized session date/window/interval, and configuration hash; after provider fetch implement StrategyRunKeyBuilder from scan lock, market-data snapshot ID, and market-data content hash.
- [ ] Implement RunCoordinator that acquires the scan lock before I/O, records incomplete attempts, associates only one completed final run with a logical scan, blocks bad data, and does not mutate positions.
- [ ] Run state and concurrency tests and commit feat: add idempotent rotation coordinator.

**Acceptance:** one scan lock produces one completed evaluation; a changed provider snapshot on a failed retry is auditable and receives a distinct final identity; notification does not imply execution. **Tests:** protected source is denied by rule input, sizing, and transition guard.

## Task 10: Minimal FastAPI adapters

**Files:** Create app/main.py, app/api/dependencies.py, health.py, admin.py; tests/integration/test_api.py.

**Depends on:** Task 9.

- [ ] Write failing tests for health, readiness, status, absent/invalid admin token, missing plan ID, non-dry-run request, and shared coordinator outcome.
- [ ] Implement application factory and protected POST admin/run-once endpoint requiring plan identifier and dry_run true.
- [ ] Wire the same RunCoordinator used by a scheduler adapter; create audit intent without changing positions.
- [ ] Return typed responses without secret fields.
- [ ] Run API tests and commit feat: add guarded dry-run API.

**Acceptance:** anonymous production run-once is impossible and no endpoint submits an order. **Tests:** scheduler/API duplicate key gets one run.

## Task 11: Templates, documentation, and final quality

**Files:** Create config_templates/market/default.yaml, strategy/default.yaml, scoring/default.yaml, sizing/default.yaml; update README.md, AGENTS.md, Dockerfile, docker-compose.yml; complete tests and docs.

**Depends on:** Tasks 1 through 10.

- [ ] Add generic validated templates with placeholders and no real asset symbols, holdings, schedules, costs, or recipients.
- [ ] Document local test, lint, type-check, container, and no-auto-trading operation.
- [ ] Add every required fixture and regression test listed in testing-strategy.md.
- [ ] Run pytest, ruff check, ruff format check, and mypy; fix failures only by preserving the approved design.
- [ ] Commit docs: complete M0/M1 foundation documentation and open an implementation PR.

**Acceptance:** all quality commands pass and documentation lists deferred work. **Tests:** full test suite plus static checks.

## Not implemented in this phase

Live provider connections, live PostgreSQL operation, authentication/login, CRUD portfolio APIs, scheduler process, distributed locking adapter, notifications, LLM adapters and calls, paper-trading accounting, execution confirmation, backtesting, Zeabur deployment, and all broker integration remain future milestones. Automatic trading is permanently out of scope for this system.
