# Milestone 2 Gap Analysis — Platform Capabilities

## Baseline and decision

This analysis is based on the `main` merge commit
`09e32df8ffa274424f871a2a5dca5cab80686a3f`.  It compares the original
Milestone 2 statement in [PROJECT_SPEC.md](../PROJECT_SPEC.md) with the
merged M0/M1 implementation.  It is a design document only: it authorizes no
implementation and does not change an existing runtime contract.

The original M2 scope is deliberately narrow:

```text
FastAPI
PostgreSQL
Alembic
user / portfolio / position APIs
candidate groups
rotation plans
admin run-once
```

M2 must turn the existing foundations into a tenancy-safe, database-backed
platform.  It must not add a broker, automatic order submission, real market
data, notifications, LLM calls, a scheduler, execution confirmation, or any
path that changes a Position.

## What is already complete

| Capability | Evidence in `main` | M2 treatment |
| --- | --- | --- |
| Pure tenant and ownership boundary | `AccessContext`, `Ownership`, `Scope`, `NotFoundForActor`, and domain invariants are framework-independent. | Reuse without weakening it.  M2 only supplies a trusted HTTP origin for `AccessContext`. |
| Sale safety invariants | An authorized sale source must be plan-listed, in the same portfolio, OPEN, and non-protected; sizing and state transitions repeat that boundary. | Reuse unchanged.  M2 must load the Position from PostgreSQL rather than trust a client identifier. |
| Immutable configuration evidence | Sparse layers, deterministic merge rules, canonical v1 hashing, immutable snapshots, and scoped snapshot repositories exist. | Reuse canonical v1 and the existing resolver; do not create a second configuration format. |
| SQLAlchemy / Alembic foundation | PostgreSQL-compatible models, mappers, repositories, and revisions `0001` through `0005` exist.  Runtime dependency includes `psycopg[binary]`. | Extend from the current Alembic head with additive M2 revisions; do not replace models or the repository pattern. |
| Scoped persistence contracts | SQLAlchemy and in-memory repositories already enforce portfolio ownership, plan references, configuration-layer selection, immutable snapshots, idempotent runs, and strategy-run evidence. | Add missing resource repositories and list/update operations with the same `AccessContext` contract. |
| FastAPI foundation | Application factory, typed health routes, request-body limiting, typed errors, and an explicit dependency object exist. | Keep the factory and dependency pattern; replace only the in-memory runtime composition in a configured deployment. |
| Guarded admin dry-run contract | `POST /admin/run-once` requires a configured token and literal JSON `true` for `dry_run`; it has a streaming pre-parse 16 KiB body limit and never mutates positions. | Preserve this compatibility endpoint and route it through the same database-backed command service. |
| Core deterministic engines | Mock market data, calendar, aggregation, indicators, restricted DSL, scoring, sizing, state machine, two-stage idempotency, and `RunCoordinator` exist. | Reuse them as injected adapters.  M2 adds composition and authoritative loading, not new trading logic. |
| Baseline quality gate | At the specified merge commit, local verification completed with `517 passed, 5 skipped`. | M2 must retain the full quality gate and add PostgreSQL/API coverage. |

## What is partially complete

| Original M2 capability | Existing implementation | Missing platform outcome |
| --- | --- | --- |
| FastAPI | Health, readiness, status, and one admin dry-run route are typed and safe. | No authenticated user API, resource routers, request-scoped database unit of work, API exception mapping for tenant resources, or database readiness check. |
| PostgreSQL | Models, migrations, mappers, and several SQLAlchemy repositories are tested. | The ASGI application does not create an engine/session factory from `DATABASE_URL`; a real request still uses in-memory adapters by default. |
| Alembic | A tested baseline chain exists through `0005_legacy_finalization_claims`. | M2 resource metadata, API principals, active configuration binding, API audit evidence, and a PostgreSQL lease adapter have no schema yet. |
| User and portfolio support | Minimal `User` and `Portfolio` domain/model projections carry identifiers and creation timestamps. | There are no user/portfolio repositories, display metadata, lifecycle fields, optimistic revisions, API schemas, or tenant-safe endpoints. |
| Position support | Domain/model/repository support OPEN/CLOSED history, partial uniqueness, and scoped reads; repository `add` exists for controlled setup. | There is no HTTP API.  M2 must expose read-only position views only; no create, edit, close, import, or execution update is permitted. |
| Candidate groups | Domain/model association table exists; plan insertion checks the candidate group owner. | No candidate-group repository, metadata, membership API, list operation, revision control, or database-backed execution loader exists. |
| Rotation plans | Domain/model/repository preserve ordered source, protected, and candidate-group references and validate cross-owner references on insert. | No list/patch API, metadata/lifecycle, active configuration-snapshot binding, revision control, or request builder constructed from PostgreSQL exists. |
| Configuration snapshots | Immutable persistence and scoped reads exist. | No API to create a resolved snapshot from selected, authorized layer versions; no active snapshot pointer for a plan; no safe list/read response. |
| Admin run-once | The route uses a shared coordinator and rejects unsafe bodies and tokens. | Its request builder looks up an in-memory template.  It cannot derive plan, snapshot, candidate instruments, market request, and authoritative source/protected positions from PostgreSQL. |
| Tenancy enforcement | Repository calls take `AccessContext`, and SQL adapters apply owner checks. | HTTP has no authenticated principal, so a request cannot yet obtain a trustworthy `AccessContext`; there is no API-level anti-enumeration coverage. |
| Deterministic evaluation composition | `RunCoordinator` accepts a typed `RotationEvaluator` plus providers and repositories. | No deployment composition binds a complete persisted execution profile to a production evaluator.  M2 must fail closed if that binding is incomplete; it must never fabricate a recommendation. |

## Not yet present

The following items are absent and are intentional M2 design work, not defects
in M0/M1:

- a database-backed ASGI composition root and request-scoped SQLAlchemy unit of
  work;
- a verified HTTP principal and a one-way conversion to `AccessContext`;
- tenant-safe CRUD/read routers for the permitted resources;
- a database-backed lease implementation for the pre-provider scan lock;
- an exact active configuration-snapshot binding for a rotation plan;
- an authoritative execution-input manifest and run-once command builder;
- immutable API audit events and API resource limits beyond the existing admin
  route;
- migrations for the above platform data;
- database readiness and deployment rollback controls.

The following remain outside M2, even though the project specification
mentions them in later milestones:

- real market-data adapters or credentials;
- scheduler/worker process, Redis, or distributed job queue;
- notification delivery or notification destinations;
- LLM providers, prompts, or request delivery;
- broker connections, orders, trading credentials, execution confirmation,
  paper accounting, or position mutation;
- web administration UI, backtesting, deployment automation, and metrics
  expansion.

## Material design constraints discovered in the current code

1. `create_application()` deliberately falls back to a safe in-memory,
   fail-closed composition.  M2 must retain that safe behaviour whenever
   `DATABASE_URL` or a complete database composition is unavailable; it must
   not make an import of `app.main` open a database connection.
2. `RunCoordinatorRequest` requires a complete plan, immutable configuration
   snapshot reference, scan identity, market request, and `AccessContext`.
   The M2 HTTP body must therefore not accept any of those values except the
   route's `rotation_plan_id` and literal `dry_run: true`.
3. Current configuration schemas are generic M0/M1 transport schemas.  A
   database execution builder may only read a separately validated, typed M2
   execution section; it may not pick arbitrary keys out of `settings` or
   `extensions`.
4. `RotationEvaluator` is injectable but has no default production binding.
   A request with no registered, fully validated deterministic evaluator must
   return a controlled unavailable/non-actionable result before provider I/O.
5. A position's existing repository `add` method is a persistence setup port,
   not permission for a public M2 write endpoint.  The API must not expose it.
6. Existing configuration and plan repositories already hide unauthorized
   records with `NotFoundForActor`.  M2 list, get, update, and run-once paths
   must keep the same non-enumerating response behaviour.

## M2 outcome

At M2 acceptance, an authenticated principal can safely manage the permitted
tenant-owned platform records, select an immutable configuration snapshot for
a plan, and request one manual, dry-run recommendation by plan identifier.
The server—not the client—loads and validates every authoritative input from
PostgreSQL before the existing coordinator reaches mock market data.  A
recommendation remains evidence only: no M2 route or adapter changes a
holding, places an order, invokes an external provider, or starts recurring
work.
