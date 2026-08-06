# Milestone 2 Platform Capabilities Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tenancy-safe PostgreSQL/FastAPI platform around the existing M0/M1 recommendation core, so an authenticated user can manage the permitted records and request one authoritative, dry-run recommendation by rotation-plan ID.

**Architecture:** Keep the existing framework-free domain, existing canonical configuration v1, and the single `RunCoordinator` decision path.  M2 adds database composition, a server-derived authenticated-request envelope, revision-aware resource services, immutable snapshot binding, and a narrow coordinator phase-store adapter; it does not add a second strategy engine or duplicate coordinator logic in an HTTP router.

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, PostgreSQL, Alembic, psycopg, pytest, Ruff, mypy, deterministic mock market data and calendar.

---

## Authority, baseline, and non-goals

Implementation begins only after this plan is approved.  The implementation base is
`d872a6a6e5fd4c99abc95a4e3bcac892a46a1da3`, the approved M2 design head on
`feature/m2-platform-design`; the implementer records it immediately before the
first code change:

```powershell
$m2ImplementationBase = git rev-parse HEAD
git status --short
```

Every checkpoint below is one independently reviewable commit.  Do not squash,
rebase, or amend an approved checkpoint.  At each stop point, push the same Draft
PR, update its body with the new commit and verification result, wait for the four
existing CI jobs, and do not start the next checkpoint until review approval.

M2 is recommendation-only.  The following are prohibited in every checkpoint:

- any broker, order, broker credential, execution confirmation, paper accounting,
  or `Position` mutation path;
- a real market-data provider, outbound network market-data call, LLM, notification
  delivery, scheduler, worker, queue, or Redis;
- a client-supplied user, role, owner, plan graph, configuration snapshot, market
  request, correlation ID, evaluator result, or runtime override on run-once.

The existing `/admin/run-once` compatibility route remains dry-run-only.  In
database composition it is available only when a valid, server-configured legacy
administrator principal is configured; otherwise it returns controlled `503`.

## Planned file map

| Area | Files and responsibility |
| --- | --- |
| Composition and identity | `app/persistence/composition.py`, `app/persistence/unit_of_work.py`, `app/application/authentication.py`, `app/api/identity.py`, `app/api/dependencies.py`, `app/main.py` |
| M2 domain/application records | `app/application/platform_models.py`, `app/application/platform_resources.py`, `app/application/audit.py`, `app/application/configuration_binding.py`, `app/application/execution_manifest.py`, `app/application/manual_scan.py`, `app/application/run_once_service.py`, `app/application/coordinator_phases.py` |
| Repository ports | `app/repositories/users.py`, `portfolios.py`, `candidate_groups.py`, `instruments.py`, `configuration_bindings.py`, `audit_events.py`, `phase_store.py`; extend existing scoped ports only where the new method is required |
| Persistence adapters | `app/persistence/models.py`, `mappers.py`, `repositories.py`, `identity.py`, `leases.py`, `phase_store.py` |
| HTTP adapters | `app/api/errors.py`, `body_size.py`, `users.py`, `portfolios.py`, `positions.py`, `candidate_groups.py`, `rotation_plans.py`, `configuration.py`, `run_once.py`, `schemas.py` |
| Migrations | `migrations/versions/0006_m2_identity_and_revisions.py`, `0007_m2_configuration_bindings.py`, `0008_m2_run_once_phases.py` |
| Tests | New focused unit, repository-contract, PostgreSQL-contract, API-integration, and coordinator-phase suites listed at each checkpoint |
| Documentation | `README.md`, `.env.example`, `docs/testing-strategy.md`, `docs/tenancy-and-security.md`, `docs/idempotency.md`, and `docs/database-erd.md` only after the implementation contract is proven |

`app/domain/access.py` deliberately remains backward compatible: `AccessContext`
does not acquire a client-set principal field.  M2 introduces an immutable
`AuthenticatedRequest` application envelope that carries the existing
`AccessContext`, authenticated `principal_id`, and authorization revision.  Domain
and repository code continue to accept the contained `AccessContext`.

## Checkpoint summary

| Checkpoint | Commit | Demonstrable output | Stop condition |
| --- | --- | --- | --- |
| M2-C1 | `feat: add M2 database identity composition` | Lazy database composition, request UoW, principal/credential identity boundary, audited bootstrap, readiness. | Review identity wire format, upgrade backfill, and fail-closed deployment behaviour. |
| M2-C2 | `feat: add tenancy-safe M2 resource APIs` | Revision-protected user, portfolio, group, plan, and read-only position/instrument APIs. | Review tenancy matrix, anti-enumeration, limits, audit, and no position writes. |
| M2-C3 | `feat: bind immutable plan configuration snapshots` | Authorized selected-layer resolution, immutable snapshots, typed execution section, and active plan binding. | Review canonical-v1 preservation and snapshot/binding CAS. |
| M2-C4 | `feat: add authoritative PostgreSQL run-once` | Database graph loader, scan identity v2, committed phases A–F, PostgreSQL lease, shared legacy/v1 run-once service. | Review recovery/crash matrix, no-provider-on-conflict, and recommendation-only boundary. |
| M2-C5 | `docs: complete M2 rollout verification` | Upgrade/rollback rehearsal, operator documentation, complete test/quality evidence, and final scope audit. | Final M2 review; no M3+ capability may be added. |

## M2-C1 — Database composition, identity, and readiness

**Files:**

- Create: `app/application/authentication.py`, `app/application/audit.py`, `app/application/platform_models.py`.
- Create: `app/persistence/composition.py`, `app/persistence/unit_of_work.py`, `app/persistence/identity.py`.
- Create: `app/repositories/audit_events.py`, `app/repositories/identity.py`.
- Create: `app/api/errors.py`, `app/api/identity.py`.
- Modify: `app/api/body_size.py`, `app/api/dependencies.py`, `app/api/health.py`, `app/main.py`.
- Modify: `app/persistence/models.py`, `app/persistence/mappers.py`, `app/persistence/repositories.py`, `app/persistence/__init__.py`.
- Create: `migrations/versions/0006_m2_identity_and_revisions.py`.
- Create tests: `tests/unit/application/test_authentication.py`, `tests/unit/persistence/test_database_composition.py`, `tests/contract/test_m2_identity_repository.py`, `tests/integration/test_m2_identity_api.py`, `tests/integration/test_postgresql_m2_0006_upgrade.py`.

### Data and port contract

- [ ] Define frozen application records in `app/application/platform_models.py`:
  `PrincipalRole` (`USER`, `ADMINISTRATOR`), `PrincipalStatus` (`ACTIVE`,
  `SUSPENDED`), `UserStatus` (`ACTIVE`, `SUSPENDED`), `Principal`,
  `ApiCredential`, `AuditEvent`, and `AuthenticatedRequest`.
- [ ] Define `AuthenticatedRequest` exactly as a server-created envelope:

  ```python
  @dataclass(frozen=True, slots=True)
  class AuthenticatedRequest:
      access_context: AccessContext
      principal_id: UUID
      authorization_revision: int
  ```

  Reject a non-positive `authorization_revision`; do not accept this type from a
  Pydantic request model.
- [ ] Define `IdentityRepository` methods with exact credential lookup and no
  collection scan: `get_credential(credential_id)`, `get_active_principal`,
  `create_credential`, `revoke_credential`, `revoke_all_credentials`,
  `change_principal_role`, and `consume_bootstrap_once`.  Every mutation accepts
  `AuthenticatedRequest` and returns the persisted revision/audit facts.
- [ ] Define `AuditEventRepository.append(event)` as append-only.  Its metadata
  type is `Mapping[str, str | int | bool | UUID]`; reject credentials, hashes,
  headers, raw request bodies, raw market data, and nested arbitrary evidence.

### Test-first steps

- [ ] Write `tests/unit/application/test_authentication.py` first.  Cover the
  only accepted credential syntax, `grm1.<22-char-base64url UUID>.<43-char
  base64url secret>`, ASCII/512-byte rejection, exact HMAC preimage, raw digest
  length `32`, constant-time raw-byte comparison, and a missing digest key
  returning typed unavailable rather than false authentication.
- [ ] Add test cases proving a User has one and only one principal; role changes
  affect the next request without invalidating credentials; principal suspension
  disables all credentials; individual revoke disables one; revoke-all disables
  each active credential; unavailable or non-ASCII legacy configuration returns
  controlled `503`.
- [ ] Add identity API tests before implementation: malformed, expired, revoked,
  non-ASCII, duplicate authorization headers, wrong secret, and unknown
  credential ID all produce the same `401` and no service call.  A verified
  non-admin gets `403` only for administrator operations.  A body/query/path
  `user_id`, `role`, `principal_id`, `request_id`, or `is_administrator` cannot
  replace envelope claims.
- [ ] Add bootstrap race tests with two transactions.  Exactly one row consumes
  `identity_bootstrap_state`; the winner receives a credential exactly once;
  replay returns a stable consumed result without a credential; losers cannot
  create a second administrator.

### Migration and persistence steps

- [ ] Add `0006_m2_identity_and_revisions.py`, revising
  `0005_legacy_finalization_claims`.  It must be additive and create
  `principals`, `api_credentials`, `identity_bootstrap_state`, and `audit_events`.
  `principals.user_id` has a unique constraint.  `api_credentials.digest` is
  PostgreSQL `BYTEA` with `octet_length(digest) = 32`; it stores raw HMAC bytes,
  never hex text.  Add indexed `(credential_id)`, `(principal_id, revoked_at)`,
  key/format-version, and expiry predicates needed for bounded lookup.
- [ ] In the same migration add nullable profile/status fields and positive
  `revision` columns to the existing User, Portfolio, Position, Instrument,
  CandidateGroup, and RotationPlan tables.  Backfill every existing row to
  `revision = 1` without inventing name, e-mail, locale, market, or enabled
  values.  Add positive-revision checks after backfill.
- [ ] **Required legacy-user backfill:** select every existing `users.user_id`,
  generate one fresh principal UUID per row, insert exactly one
  `principals(user_id, role='USER', status='ACTIVE', authorization_revision=1)`
  row, and rely on the unique `principals.user_id` constraint to prevent a
  duplicate on retry.  Do not infer an administrator from prior data, an
  environment variable, a portfolio, or a historical strategy run.  The only
  administrator created by M2 is the separately configured, one-time bootstrap
  operation.
- [ ] Make migration upgrade idempotence explicit only through normal Alembic
  single application: test fresh installation and `0005 -> 0006`, and test that
  upgrade data contains exactly one non-administrator principal for each legacy
  User.  Downgrade tests run only on disposable databases; production rollback
  is route/application disablement, not evidence deletion.
- [ ] Add SQLAlchemy models/mappers/repository implementations.  Preserve UTC
  restoration, use `SELECT ... FOR UPDATE` for credential revocation, role
  change, bootstrap consumption, and authorization-revision increments.  Do not
  add a role to `AccessContext`; construct the pre-existing context from verified
  principal claims inside `DatabaseIdentityProvider`.

### Composition and HTTP steps

- [ ] Implement `DatabaseApplicationComposition` with validated environment
  settings: `DATABASE_URL`, database statement/lock timeout, identity digest-key
  versions, optional legacy token plus `LEGACY_ADMIN_PRINCIPAL_ID`, and bootstrap
  settings.  Construction must not open a connection; startup/lifespan creates
  the engine/session factory only after configuration validation.
- [ ] Implement `DatabaseUnitOfWork` as one short request transaction.  It owns
  a single SQLAlchemy session, exposes scoped repositories, commits accepted
  commands, rolls back errors, and closes the session.  Provider I/O is excluded;
  M2-C4 adds separate coordinator-phase transactions.
- [ ] Implement the FastAPI identity dependency to parse a single bounded
  `Authorization: Bearer` header, look up one credential, create
  `AuthenticatedRequest`, and attach no identity field to `request.state` that
  can be populated by client data.  `AccessContext.request_id` is a fresh server
  UUID and `authentication_method` is either `bearer-credential` or
  `legacy-admin-token`.
- [ ] Extend the body-limit middleware into route-specific streaming limits,
  retaining the existing `/admin/run-once` 16 KiB pre-parse limit unchanged.
  It must count bytes before JSON parsing and return one stable `413` payload.
- [ ] Change `create_application()` to preserve injected in-memory test
  composition and the import-safe default.  In database mode register a lifespan
  composition, identity/error dependencies, and a bounded `GET /ready` SQL
  `SELECT 1`.  Failed database configuration, unavailable database, unavailable
  digest key, or invalid legacy-principal binding must fail closed with `503`; no
  import of `app.main` opens a database connection.
- [ ] Add `/admin/bootstrap-identity` with only the 4 KiB strict profile body
  and 512-byte ASCII bootstrap header.  It is never available to Bearer callers.
  It atomically creates the preselected administrator User/principal/credential,
  writes an audit event, records consumption, returns the secret once with
  `Cache-Control: no-store`, and never logs it.

### Verification and commit

- [ ] Run focused suites:

  ```powershell
  python -m pytest tests/unit/application/test_authentication.py tests/unit/persistence/test_database_composition.py tests/contract/test_m2_identity_repository.py tests/integration/test_m2_identity_api.py tests/integration/test_postgresql_m2_0006_upgrade.py
  ```

- [ ] Run `python -m pytest`, `python -m mypy app tests`, `python -m ruff check .`,
  `python -m ruff format --check .`, and `git diff --check`.
- [ ] Commit only this checkpoint:

  ```powershell
  git add app migrations tests README.md .env.example
  git commit -m "feat: add M2 database identity composition"
  ```

  `README.md` and `.env.example` may change only to name non-secret settings and
  explain that values belong in Zeabur or ignored `.local.env`.
- [ ] Push, wait for Ubuntu, Windows, PostgreSQL persistence-contract, and
  runtime PostgreSQL-smoke jobs, then stop at **M2-C1 review**.

**Acceptance:** a database-backed request receives a verified envelope and a
fresh scoped context; no plaintext credential is stored; every pre-existing User
has exactly one default non-admin principal after `0005 -> 0006`; bootstrap is
single-use; the app remains import-safe and fails closed when database identity
configuration is unavailable.

## M2-C2 — Tenancy-safe resource APIs and revisions

**Files:**

- Create: `app/application/platform_resources.py`.
- Create: `app/repositories/users.py`, `app/repositories/portfolios.py`, `app/repositories/candidate_groups.py`, `app/repositories/instruments.py`.
- Create: `app/api/schemas.py`, `app/api/users.py`, `app/api/portfolios.py`, `app/api/positions.py`, `app/api/instruments.py`, `app/api/candidate_groups.py`, `app/api/rotation_plans.py`.
- Modify: `app/api/body_size.py`, `app/api/dependencies.py`, `app/api/errors.py`, `app/main.py`.
- Modify: `app/persistence/models.py`, `app/persistence/mappers.py`, `app/persistence/repositories.py`, `app/repositories/base.py`, `app/repositories/positions.py`, `app/repositories/rotation_plans.py`.
- Create tests: `tests/unit/application/test_platform_resources.py`, `tests/contract/test_m2_resource_repositories.py`, `tests/integration/test_m2_resource_api.py`, `tests/integration/test_postgresql_m2_resource_contracts.py`.

### Resource contract

- [ ] Add repository methods for owner-scoped create/list/get/patch operations,
  always with `AccessContext`, and add `expected_revision: int` to every mutable
  command.  A stale revision returns typed conflict; absence and non-ownership
  both remain `NotFoundForActor`/HTTP `404`.
- [ ] Implement these routes only:

  | Resource | Permitted M2 operations |
  | --- | --- |
  | User | self/admin read; self profile patch; administrator status patch; administrator credential/role/revoke-all operations |
  | Portfolio | owner/admin create, list, get, revision-protected patch |
  | Position | owner/admin list and get only |
  | Instrument | global read/list only; no ordinary-user mutation route |
  | Candidate group | owner/admin create, list, get, revision-protected patch, complete membership replacement |
  | Rotation plan | owner/admin create, list, get, revision-protected patch |

- [ ] Do not register any POST/PATCH/DELETE/import/close/confirmation endpoint for
  positions.  Do not expose repository `PositionRepository.add()` through HTTP.
- [ ] Enforce all design limits before service work: canonical UUID paths; page
  size `1..100`; cursor at most 512 UTF-8 bytes; user/portfolio/group/plan body
  64 KiB; name 128 bytes; description 4 KiB; display name 128 bytes; email 254;
  timezone 64; locale 32; at most 500 unique candidate instruments; at most 100
  source IDs, 100 protected IDs, and 32 candidate groups.  Strict Pydantic models
  use `extra="forbid"` and do not coerce booleans/numbers across types.

### Test-first steps

- [ ] Write `tests/unit/application/test_platform_resources.py` for every command
  with an exact expected revision.  Assert stale `If-Match` causes no partial
  change and produces `409`; missing/non-owned resource produces the same safe
  result; SYSTEM Instrument write is administrator-only at the repository
  boundary even though no M2 API route publishes it.
- [ ] Write cross-tenant API cases with two Users and two portfolios owned by the
  same User.  The same user must not use one portfolio's group/plan/position as
  another portfolio's plan input.  A foreign User must receive `404` for private
  user, portfolio, position, group, plan, binding, and snapshot records.
- [ ] Write resource-limit tests that exceed each byte/count limit by one and
  prove no service/repository mutation happens.  Test invalid UTF-8-equivalent
  identifiers, blank/control-only names, duplicate IDs, source/protected overlap,
  and an invalid `If-Match` before database mutation.
- [ ] Write position-read-only tests that enumerate every registered route and
  assert no route maps to a position-write service method.
- [ ] Write audit tests for accepted/rejected credential, role, user, portfolio,
  group, and plan commands.  Assert audit metadata contains
  only IDs/revisions/result codes and never a header, token, digest, raw body, or
  unauthorized ID.

### Implementation steps

- [ ] Add Pydantic request/response models in `app/api/schemas.py` for a single
  resource at a time.  Convert `If-Match` to one positive integer only; reject
  duplicate/weak/list ETags.  Responses expose revision, ownership-safe fields,
  and timestamps but never secrets/digests/audit internals.
- [ ] Implement `PlatformResourceService`.  It opens a UoW, receives only
  `AuthenticatedRequest`, passes its `access_context` to repositories, rechecks
  reference ownership in the same transaction, increments the affected revision
  once, appends bounded audit evidence, and commits atomically.
- [ ] Implement CandidateGroup membership as a set, not an ordered input list.
  Validate all global Instrument IDs in one bounded query; reject duplicates;
  normalize by canonical UUID ordering in repository results, API responses,
  execution inputs, and hashes.  A replacement equal to the stored normalized
  set is idempotent and does not increment `CandidateGroup.revision`; a changed
  set atomically replaces association rows and increments exactly once.
- [ ] Preserve order only where existing data has an explicit ordinal:
  `rotation_plan_candidate_groups`, `rotation_plan_source_positions`, and
  `rotation_plan_protected_positions`.  Replacing a plan validates every group
  owner and source/protected Position against the exact plan portfolio in the
  same transaction.
- [ ] Register routers only after database composition is active.  Route handlers
  call a single service method, map typed errors through `app/api/errors.py`, and
  never assemble a SQLAlchemy model, ownership, or `AccessContext` from request
  JSON.

### Verification and commit

- [ ] Run focused suites:

  ```powershell
  python -m pytest tests/unit/application/test_platform_resources.py tests/contract/test_m2_resource_repositories.py tests/integration/test_m2_resource_api.py tests/integration/test_postgresql_m2_resource_contracts.py
  ```

- [ ] Run the complete pytest/mypy/Ruff/format/diff gate from M2-C1.
- [ ] Commit:

  ```powershell
  git add app tests
  git commit -m "feat: add tenancy-safe M2 resource APIs"
  ```

- [ ] Push, wait for all four CI jobs, and stop at **M2-C2 review**.

**Acceptance:** all permitted resources are tenancy-safe, revision-protected and
bounded; candidate membership is UUID-sorted unordered-set evidence; positions
remain read-only over HTTP; private resources cannot be enumerated across users
or sibling portfolios.

## M2-C3 — Immutable configuration snapshots and plan binding

**Files:**

- Create: `app/application/configuration_binding.py`, `app/repositories/configuration_bindings.py`.
- Create: `app/api/configuration.py`.
- Modify: `app/application/configuration.py`, `app/application/configuration_snapshots.py`, `app/schemas/configuration.py`, `app/schemas/requests.py`, `app/schemas/responses.py`.
- Modify: `app/api/schemas.py`, `app/api/dependencies.py`, `app/api/errors.py`, `app/main.py`.
- Modify: `app/persistence/models.py`, `app/persistence/mappers.py`, `app/persistence/repositories.py`, `app/repositories/configuration_layers.py`, `app/repositories/configuration_snapshots.py`.
- Create: `migrations/versions/0007_m2_configuration_bindings.py`.
- Create tests: `tests/unit/application/test_configuration_binding.py`, `tests/contract/test_m2_configuration_binding_repository.py`, `tests/integration/test_m2_configuration_api.py`, `tests/integration/test_postgresql_m2_0007_upgrade.py`.

### Test-first steps

- [ ] Write a snapshot-request fixture that supplies at most one exact
  `{scope, reference_id, version, content_hash}` selection per scope.  Test
  duplicate scope, unavailable layer, other-user layer, sibling-portfolio layer,
  bad content hash, expired override, target mismatch, and an unbounded request
  body; each must fail before resolver output is persisted.
- [ ] Write tests proving the resolver remains canonical configuration v1:
  `LayerPatchSchema` is sparse, `ResolvedConfigurationSchema` is final, existing
  seven-layer precedence is unchanged, and the M2 execution subsection is a
  typed validated object rather than arbitrary `settings` or `extensions` keys.
- [ ] Test `PlanConfigurationBinding` optimistic concurrency: first activation
  succeeds; stale expected binding revision gets `409`; the active binding always
  names one immutable snapshot targeted to the exact plan; a snapshot created for
  another plan/portfolio cannot be activated.
- [ ] Test snapshot immutability/replay: equivalent selected parents reuse only
  the exact immutable snapshot evidence; later layer edits do not change it; the
  returned canonical hash is still the M0/M1 v1 hash.

### Implementation and migration steps

- [ ] Define an immutable `PlanConfigurationBinding` record with binding ID,
  rotation-plan ID, snapshot ID, positive binding revision, creator, and aware
  timestamps.  Define a scoped repository port: `get_active(plan_id, context)`,
  `activate(binding, expected_revision, context)`, and list/get operations.
- [ ] Add `0007_m2_configuration_bindings.py`, revising `0006`.  Create the
  plan-binding table with a unique active binding per plan, foreign keys to plan
  and snapshot, positive revision check, timestamp/creator fields, and indexes
  for plan lookup.  It must not rewrite old canonical JSON or content hashes;
  execution-manifest persistence is introduced only by M2-C4.
- [ ] Extend `ResolvedConfigurationSchema` with a typed `execution` field:
  one market, IANA timezone, scan/source-bar intervals, freshness ceiling,
  `daily_lookback_completed_sessions` constrained to `1..250`, validated session
  grid, and a registered deterministic evaluator key.  Reject live-provider,
  broker, LLM, notification, scheduler, or runtime-provider selection fields.
- [ ] Implement `ConfigurationSnapshotService.create_for_plan`.  Authorize the
  caller before loading each selected layer; use existing SQL scope predicates;
  resolve with the current canonical-v1 resolver; persist immutable merged
  evidence with server-controlled ID/version/creator/time; and write audit
  metadata containing only target/parent IDs and result facts.
- [ ] Implement binding activation in a separate revision-protected command.  It
  must verify plan ownership and exact snapshot target in the same transaction,
  update only when `expected_revision` matches, and return the new binding
  revision.  Run-once in M2-C4 reads this binding; it never receives a snapshot
  ID from JSON.
- [ ] Add routes for configuration-layer list/get/create, plan snapshot create,
  snapshot list/get, active-binding get/activate.  Use the configuration command
  192 KiB pre-parse limit and retain existing 16 KiB string, 128 KiB DSL/ruleset/
  input/evidence, and 256 KiB output limits.  API output refuses an oversized
  canonical payload rather than truncating it.

### Verification and commit

- [ ] Run focused suites:

  ```powershell
  python -m pytest tests/unit/application/test_configuration_binding.py tests/contract/test_m2_configuration_binding_repository.py tests/integration/test_m2_configuration_api.py tests/integration/test_postgresql_m2_0007_upgrade.py tests/unit/config
  ```

- [ ] Run the complete pytest/mypy/Ruff/format/diff gate.
- [ ] Commit:

  ```powershell
  git add app migrations tests
  git commit -m "feat: bind immutable plan configuration snapshots"
  ```

- [ ] Push, wait for all four CI jobs, and stop at **M2-C3 review**.

**Acceptance:** the server can create a reproducible M0/M1 canonical-v1 snapshot
from authorized exact layers and bind it atomically to one plan.  No caller can
select a latest/unowned snapshot or supply arbitrary execution configuration.

## M2-C4 — PostgreSQL-authoritative run-once and phase persistence

**Files:**

- Create: `app/application/execution_manifest.py`, `app/application/manual_scan.py`, `app/application/run_once_service.py`, `app/application/coordinator_phases.py`.
- Create: `app/repositories/phase_store.py`.
- Create: `app/persistence/leases.py`, `app/persistence/phase_store.py`.
- Create: `app/api/run_once.py`.
- Modify: `app/application/run_coordinator.py`, `app/application/idempotency.py`, `app/services/rotation_run.py` only where phase handles are required.
- Modify: `app/domain/entities.py`, `app/domain/values.py`, `app/repositories/locks.py`, `app/repositories/logical_scans.py`, `app/repositories/rotation_plans.py`, `app/repositories/positions.py`.
- Modify: `app/persistence/models.py`, `app/persistence/mappers.py`, `app/persistence/repositories.py`, `app/api/admin.py`, `app/api/dependencies.py`, `app/api/errors.py`, `app/main.py`.
- Create: `migrations/versions/0008_m2_run_once_phases.py`.
- Create tests: `tests/unit/application/test_execution_manifest.py`, `tests/unit/application/test_manual_scan.py`, `tests/unit/application/test_coordinator_phases.py`, `tests/contract/test_m2_phase_store.py`, `tests/integration/test_m2_run_once_api.py`, `tests/integration/test_m2_run_once_recovery.py`, `tests/integration/test_postgresql_m2_0008_upgrade.py`.

### Authoritative load and scan identity tests

- [ ] Write tests showing `/v1/rotation-plans/{rotation_plan_id}/run-once` accepts
  only literal `{"dry_run": true}` and no additional field.  The path ID is the
  only client execution input; a JSON plan, position, instrument, snapshot,
  candidate list, provider request, evaluator output, correlation ID, or runtime
  override fails schema validation before the command service.
- [ ] Build test graphs with a normal source, protected core, unrelated normal
  holding, foreign holding, and CLOSED historical holding.  The database loader
  must use repository-fetched authoritative Positions and call the existing sale
  invariants; only plan-listed, same-portfolio, OPEN, non-protected sources are
  ever supplied to coordinator/sizing/state transitions.
- [ ] Test the `ExecutionInputManifest` exact tuple: plan/binding/portfolio
  revisions and enabled status; snapshot ID/hash; ordered source/protected
  positions with revisions and quantity/status/role; candidate-group revision;
  candidate instrument normalized UUID order with revision/market/symbol/active
  fields.  Same set in a different request order has the same manifest hash;
  changed group membership/position/binding has a different hash.
- [ ] Test manual scan identity from a fixed injected UTC clock: holiday,
  pre-open, session break, post-close, incomplete first interval, and insufficient
  daily history fail with the exact typed `409` before lease/provider work.  Two
  active calls in one completed interval use equal v2 identity/request UUIDv5;
  adjacent intervals differ; `requested_at` equals normalized window end.

### Phase-store contract and implementation steps

- [ ] Define `ExecutionInputManifest` and `ScanIdentityV2` in application/domain
  records.  Keep existing configuration canonical v1 unchanged.  Hash a separate
  versioned manifest payload, and store raw v2 scan components, actual snapshot
  hash, manifest hash, and both format versions on lease/logical-scan records.
- [ ] Define the narrow `CoordinatorPhaseStore` Protocol so `RunCoordinator`
  owns one workflow while persistence owns transaction boundaries.  Its typed
  operations are: `begin_attempt(authoritative_command)`,
  `finalize_attempt(handle, outcome)`, `finalize_non_actionable(handle, code,
  detail, snapshot=None)`, `read_durable_finalization(handle)`, and
  `rescue_finalization_failure(handle)`.  No method evaluates rules, calls a
  provider, changes a Position, or creates a second action path.
- [ ] Implement `DatabaseRunOnceCommandService` Phase A: short UoW authoritative
  graph load, snapshot binding validation, typed execution parsing, manifest
  build, fixed clock scan derivation, deterministic mock `MarketDataRequest`, and
  database evaluator-key lookup.  If anything is missing/unavailable, return a
  bounded typed unavailable/non-action result before provider I/O.
- [ ] Implement Phase B in `PostgresLeaseLockProvider`: atomically insert or
  reclaim an expired v2 lease in a committed short transaction, then release
  exactly `(scan_key, lease_id)` only.  It must authorize plan portfolio scope
  and use deployment-configured positive lock/statement timeouts.
- [ ] Implement Phase C in `SqlAlchemyCoordinatorPhaseStore.begin_attempt`: in a
  fresh committed transaction, lock/reload the plan graph, compare the complete
  Phase-A manifest and active-binding revision, create/recover the logical scan,
  and append a RUNNING attempt before returning a provider handle.  Mismatch
  records/reports `EXECUTION_INPUT_CHANGED`, releases lease, and calls no provider.
- [ ] Modify `RunCoordinator` to call Phase C before mock provider work and to
  call Phase E finalization/terminal non-action operations in fresh transactions.
  Preserve its existing rule, scoring, sizing, state-machine, final key, and
  child-intent decisions.  The HTTP service and legacy route must invoke this
  same coordinator/service, not a forked workflow.
- [ ] Implement expected post-C failures in a new terminal transaction:
  provider/parser/evaluator/sizing/state failures become bounded `FAILED`; valid
  stale/incomplete evidence becomes `DEGRADED`; manifest/binding revalidation
  mismatch becomes `FAILED EXECUTION_INPUT_CHANGED`.  These outcomes create no
  ACTION, child notification intent, position change, or provider retry claim.
- [ ] **Required uncertain-Phase-E commit algorithm:** when `finalize_attempt`
  receives a commit error whose result may be unknown, it first closes the failed
  transaction and opens a new read transaction.  It loads the logical scan,
  attempt, final strategy run, and recommendation state by their exact IDs and
  verifies the expected manifest, final key, finalization disposition, and
  revision facts.  If a matching durable final outcome exists, return that result
  (do not rescue).  If durable state proves the attempt is still RUNNING and no
  matching final outcome exists, execute one compare-and-swap rescue transaction
  to set `FAILED FINALIZATION_PERSISTENCE_ERROR`.  If durable state cannot be
  read or is ambiguous, return controlled `503`, leave RUNNING evidence intact,
  and do not issue a rescue write.  A rescue transaction failure also returns
  `503` with durable recovery left for a later lease-expiry request.
- [ ] Implement Phase F as a `finally` exact lease release.  A release failure
  cannot re-run provider work.  On later expired-lease acquisition, read the
  durable logical scan before provider I/O; if Phase E already committed, return
  the existing final/duplicate result.

### Migration and recovery tests

- [ ] Add `0008_m2_run_once_phases.py`, revising `0007`.  Add active execution
  manifest storage, v2 raw identity/manifest fields and format versions to logical
  scans, exact lease table/index/expiry constraints, phase handle/finalization
  facts required for CAS, and additive foreign keys/check constraints.  Retain
  legacy v1 rows as readable/recoverable; M2 manual routes always create v2 rows.
- [ ] Write PostgreSQL concurrency tests for one lease winner, expired reclaim,
  exact lease release, duplicate finalization, and stale binding/manifest CAS.
- [ ] Write crash-boundary tests that simulate A→B, B→C, C→D, D→E, E→F, and F
  release failure.  The E→F test must commit a terminal outcome, simulate process
  loss before lease release, advance lease time, make a second request, and assert
  the provider call count remains one while the existing durable result returns.
- [ ] Write uncertain-commit tests with a phase-store test double that raises
  after durable commit and one that raises before commit.  The former must reload
  and return the durable result without rescue; the latter must prove RUNNING via
  read then write exactly one `FINALIZATION_PERSISTENCE_ERROR` CAS.  A double that
  cannot read after the error must return `503`, retain RUNNING, and make no
  speculative rescue write.

### HTTP and verification steps

- [ ] Register the new `/v1/rotation-plans/{id}/run-once` route under verified
  Bearer identity.  Adapt `/admin/run-once` to construct the configured active
  legacy administrator envelope and delegate to exactly the same
  `DatabaseRunOnceCommandService`; retain literal `dry_run` and 16 KiB body rules.
- [ ] Keep run-once responses bounded summaries only.  They may expose safe
  disposition, logical-scan ID, and opaque final identity, but never market bars,
  configuration payload, credential, stack trace, evaluator internals, or audit
  evidence.
- [ ] Run focused suites:

  ```powershell
  python -m pytest tests/unit/application/test_execution_manifest.py tests/unit/application/test_manual_scan.py tests/unit/application/test_coordinator_phases.py tests/contract/test_m2_phase_store.py tests/integration/test_m2_run_once_api.py tests/integration/test_m2_run_once_recovery.py tests/integration/test_postgresql_m2_0008_upgrade.py
  ```

- [ ] Run the complete pytest/mypy/Ruff/format/diff gate.
- [ ] Commit:

  ```powershell
  git add app migrations tests
  git commit -m "feat: add authoritative PostgreSQL run-once"
  ```

- [ ] Push, wait for all four CI jobs, and stop at **M2-C4 review**.

**Acceptance:** run-once reconstructs the entire execution authority graph from
PostgreSQL, commits RUNNING evidence before mock provider I/O, uses stable v2
manual scan identity, and safely recovers all A–F crash boundaries without a
second provider call after durable completion.

## M2-C5 — Migration rehearsal, operator documentation, and final audit

**Files:**

- Modify: `README.md`, `.env.example`, `docs/architecture.md`, `docs/tenancy-and-security.md`, `docs/configuration.md`, `docs/idempotency.md`, `docs/database-erd.md`, `docs/testing-strategy.md`.
- Modify: `.github/workflows/ci.yml`, only if a missing deterministic migration/rollback job is needed; do not add deployment or scheduler jobs.
- Create: `tests/integration/test_m2_migration_rehearsal.py`, `tests/integration/test_m2_scope_audit.py`.
- Modify: existing PostgreSQL contract fixtures only to create disposable `0005`, fresh-head, and downgrade/upgrade databases.

### Test-first and rollout steps

- [ ] Add a fresh-head migration rehearsal and a `0005 -> 0006 -> 0007 -> 0008`
  PostgreSQL rehearsal.  Populate a representative legacy tenant before upgrade;
  verify one default non-admin principal per User, preserved OPEN/CLOSED positions,
  preserved canonical snapshot v1 evidence, inactive routes before ready state,
  and no inferred administrator.
- [ ] Add disposable downgrade/upgrade tests.  They may run only on throwaway
  databases and must document that production rollback disables the M2 application
  and routes while preserving schema/audit/credential/binding/lease evidence.
- [ ] Add a static/route scope audit that fails if the M2 application constructs a
  live provider, broker, notification channel, LLM, scheduler, worker, queue, or
  any HTTP Position write.  Add a second audit that checks `.env.example`, README,
  migrations, source, and test fixtures contain no secret value, local absolute
  path, token, password, or credential fixture resembling a deployable secret.
- [ ] Update README with plain-language local PostgreSQL development instructions,
  `DATABASE_URL`/identity setting names only, Alembic upgrade command, database
  readiness meaning, one-time bootstrap/recovery runbook, credential rotation
  sequence, and the explicit statement that this system only recommends and never
  submits trades.
- [ ] Update architecture/tenancy/configuration/idempotency/ERD/testing documents
  to reflect the implemented names and migrations.  Do not document an endpoint
  or capability that is absent from the approved code.
- [ ] Ensure CI retains Ubuntu, Windows, PostgreSQL persistence contracts, and
  runtime PostgreSQL smoke.  If the new rehearsal requires a service matrix entry,
  add it as a bounded PostgreSQL test command rather than a background scheduler
  or external integration.

### Verification and commit

- [ ] Run migration/rehearsal and focused scope suites:

  ```powershell
  python -m pytest tests/integration/test_m2_migration_rehearsal.py tests/integration/test_m2_scope_audit.py tests/integration/test_postgresql_task_four_contracts.py
  ```

- [ ] Run final local gate:

  ```powershell
  python -m pytest
  python -m mypy app tests
  python -m ruff check .
  python -m ruff format --check .
  git diff --check "$m2ImplementationBase..HEAD"
  git status --short
  ```

- [ ] Confirm that the implementation range contains exactly the approved
  checkpoint commits and their explicit review remediation commits:

  ```powershell
  git log --oneline "$m2ImplementationBase..HEAD"
  git diff --stat "$m2ImplementationBase..HEAD"
  ```

- [ ] Commit:

  ```powershell
  git add README.md .env.example docs .github tests
  git commit -m "docs: complete M2 rollout verification"
  ```

- [ ] Push, wait for all four CI jobs, update the Draft PR with final migration
  and quality evidence, then stop at **M2-C5 final review**.  Do not merge or
  begin M3 work in this checkpoint.

**Acceptance:** a fresh database and a representative `0005` upgrade both pass;
rollback is rehearsed only on disposable databases; operating instructions are
secret-free and understandable; the final scope audit proves no trading,
position-mutation, real-provider, notification, LLM, scheduler, or worker path
was introduced.

## Required quality matrix at every checkpoint

| Layer | Required result |
| --- | --- |
| Focused tests | Every new unit/contract/API/PostgreSQL suite passes before its checkpoint commit. |
| Full tests | `python -m pytest` passes; skips remain explicitly named and never hide M2 coverage. |
| Types | `python -m mypy app tests` passes without lowering strictness, excluding tests, or adding broad ignores. |
| Lint/format | `python -m ruff check .` and `python -m ruff format --check .` pass. |
| Diff safety | `git diff --check` passes; status contains no `.env`, `.local.env`, credential, database dump, cache, virtual environment, or absolute-path artifact. |
| CI | Ubuntu, Windows, PostgreSQL persistence contracts, and runtime PostgreSQL smoke are green before approval. |

## M2 acceptance checklist

- [ ] The database-backed ASGI composition is lazy, request-scoped, and
  fail-closed; `app.main` remains import-safe.
- [ ] HTTP identity is an authenticated server-derived envelope, raw credentials
  are never stored, legacy Users backfill to exactly one non-admin principal, and
  bootstrap/role/revocation/key-rotation contracts are tested.
- [ ] All M2 resources obey exact ownership, anti-enumeration, revisions, byte/
  count limits, audit bounds, and candidate-set canonicalization; positions remain
  HTTP read-only.
- [ ] Configuration snapshots retain M0/M1 canonical v1 evidence and are bound by
  exact plan revision; unowned/latest snapshots are impossible execution inputs.
- [ ] Run-once accepts only plan ID plus JSON `true`, loads all authority from
  PostgreSQL, uses mock data only, writes RUNNING before provider I/O, and handles
  uncertain Phase E commit results by durable reread before any rescue CAS.
- [ ] All crash points A→B through E→F, including expiry after unreleased lease,
  are covered and cannot cause duplicate provider work after durable completion.
- [ ] Fresh/upgrade/rollback rehearsals and the full local/CI quality matrix pass.
- [ ] The final diff contains none of the prohibited M3+ capabilities.
