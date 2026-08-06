# Milestone 2 Platform Design

## Status and scope

**Status: proposed for design review.** This design is based on main merge commit `09e32df8ffa274424f871a2a5dca5cab80686a3f`. It is a design specification only, not an implementation plan and not an authorization to change application code.

Milestone 2 adds a database-backed platform around the merged M0/M1 core: FastAPI composition backed by PostgreSQL; a trustworthy HTTP identity-to-`AccessContext` boundary; tenancy-safe APIs for users, portfolios, read-only positions, candidate groups, rotation plans, configuration layers/snapshots, and manual run-once; an authoritative `rotation_plan_id` command flow; and migrations, auditing, input limits, rollback controls, and tests.

M2 remains recommendation-only. It must not add automatic orders, broker access, real market data, notifications, LLM calls, a scheduler or worker, queues, execution confirmation, paper accounting, or **any position mutation**. A recommendation cannot alter a holding. The M2 positions API is read-only.

## Database application composition

M2 retains the existing dependency direction and framework-free domain.

```text
HTTP router and identity dependency
        |
application command/query services
        |
domain and existing repository/lock Protocols
        |
SQLAlchemy repositories, PostgreSQL lease adapter, deterministic mock adapters
```

Pydantic remains at HTTP and persisted-configuration boundaries. `RunCoordinator`, the configuration resolver, state machine, deterministic engines, and repository Protocols remain the single core workflow. M2 adds adapters and application services; it does not rebuild those foundations.

| Proposed object | Responsibility | Not responsible for |
| --- | --- | --- |
| `DatabaseApplicationComposition` | Lazily owns the engine/session factory, validated runtime settings, mock provider factory, clock, and service factories. | Opening a database connection during `app.main` import, retaining a global session, or making a strategy decision. |
| `DatabaseUnitOfWork` | Creates a short request transaction and SQLAlchemy repositories; commits accepted commands and rolls back failures. | Creating identity, returning sessions, or invoking a broker. |
| `DatabaseIdentityProvider` | Parses/verifies a credential and resolves an active principal. | Accepting user, owner, role, or admin values from JSON/query/path input. |
| `AccessContextFactory` | Creates immutable context from verified claims and server-generated request ID. | Trusting unverified forwarded identity headers. |
| `PlatformResourceService` | Authorized resource queries/commands, optimistic revision checks, and audit writes. | Evaluation or position mutation. |
| `DatabaseRunOnceCommandService` | Produces a complete authoritative command from route plan ID, then calls current coordinator. | Accepting plan, snapshot, positions, instruments, or evaluator output from JSON. |
| `PostgresLeaseLockProvider` | Implements current `LockProvider` before provider I/O. | Scheduler, Redis, or a queue. |

`create_application()` continues to allow injected test composition. A deployed database composition is selected only during ASGI lifespan/startup after validated `DATABASE_URL` settings. If unavailable, importing the app and the existing safe in-memory default remain valid; database command routes fail closed and readiness reports unavailable. Every request gets a fresh SQLAlchemy session, which is closed before a response is returned. Database statement and lock timeouts are positive deployment-configured safety limits.

`GET /health` remains process-only. Under database composition, `GET /ready` executes a bounded `SELECT 1` and returns an opaque unavailable result when it fails. It does not probe a provider, LLM, broker, notification service, or scheduler.

## Trusted HTTP identity to `AccessContext`

M2 uses a database-backed opaque Bearer credential, not password login, cookie sessions, client-supplied identity headers, or unauthenticated proxy headers. The credential is ASCII-only, versioned, contains at least 256 bits of server-generated random secret material, and is accepted only as `Authorization: Bearer <opaque-credential>`.

PostgreSQL stores principal/user references and current roles in the principal table, while a credential row stores its principal reference, creation/revocation/expiry times, and a versioned keyed digest. It never stores the plaintext credential. The digest key is a Zeabur environment secret such as `HTTP_IDENTITY_DIGEST_KEY`; it is absent from source, logs, responses, audit metadata, examples, and database error output. Parsing and comparison are constant-time over ASCII bytes.

M2 includes no public registration or password recovery. Initial administrator bootstrap is a one-time deployment operation using a separate Zeabur-only bootstrap secret and a preselected administrator UUID. It records consumption and disables itself. Administrators provision/revoke opaque credentials for known users; a newly generated plaintext credential is returned once in an uncached HTTPS response and is never retrievable again. A later runbook documents the operation without recording a secret.

```text
raw HTTP request
  -> bounded ASCII Bearer parsing
  -> active-principal lookup plus constant-time keyed-digest check
  -> verified {principal_id, user_id, roles, authentication_method}
  -> server-generated request_id
  -> AccessContext(actor_user_id, is_administrator, request_id, method)
```

This dependency is the sole HTTP source of `AccessContext`. Request body and query data cannot construct, replace, or augment it. `is_administrator` is derived from verified principal role. The server generates request correlation UUIDs rather than copying unbounded client correlation headers into evidence.

Missing, malformed, non-ASCII, oversized, expired, revoked, or invalid credentials receive the same `401`. A valid caller without access to a private record receives the established non-enumerating `404`. `403` is reserved for verified administrator-only system operations. The legacy `X-Admin-Token` route remains compatible but internally creates the same administrator context and delegates to the same service; it never bypasses repository checks.

### Credential, principal, legacy-admin, and bootstrap contract

The exact M2 credential wire form is `grm1.<credential-id>.<secret>`. `credential-id` is a UUID encoded as unpadded base64url (22 ASCII characters); `secret` is 32 random bytes encoded as unpadded base64url (43 ASCII characters). The complete value therefore matches `^grm1\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$`. The identity provider rejects all other forms, non-ASCII data, and values longer than 512 bytes before database work.

The decoded identifier selects exactly one `api_credentials.credential_id` row. The provider never scans all stored digests. It calculates lowercase hexadecimal `HMAC-SHA-256` using the credential row's `digest_key_version` and this exact preimage: UTF-8 `tw-stock-swap-monitor/http-credential/v1`, one zero byte, 16 UUID bytes, one zero byte, then the ASCII secret bytes. It compares the computed and stored 32-byte digest with a constant-time byte comparison. The row records the derivation format version separately from the digest-key version so a future format is opt-in rather than silently reinterpreting a stored value.

The active identity predicate is evaluated on every request in one bounded lookup: credential is not revoked and not expired; referenced principal is `ACTIVE`; referenced user is `ACTIVE`; and the configured digest-key version is available. Roles live on the principal, not copied into credentials or cached in a token. A role change therefore takes effect on the next request; suspending a principal disables all of its credentials, while revoking a credential disables only that credential. Each role/status/revocation update increments the principal authorization revision and creates an audit event. An unavailable configured digest key is deployment misconfiguration and returns controlled `503`, never a false `401` or a bypass.

Digest-key rotation is explicit: deploy the new key version while retaining the old key; issue replacement credentials under the new active version; revoke every old-version credential; verify no active old-version row remains; then remove the old key. The system cannot re-hash an opaque secret it no longer possesses. Credential creation and verification always state their derivation/key versions, so historical audit evidence remains interpretable.

Database composition enables the legacy `POST /admin/run-once` only if both `ADMIN_API_TOKEN` and `LEGACY_ADMIN_PRINCIPAL_ID` are configured. The latter must resolve to an active database principal whose user is active and whose current role is administrator. On each accepted legacy-token request, the server builds `AccessContext` from that persisted principal and uses `authentication_method="legacy-admin-token"`; it uses no generated stand-in user ID. A missing, invalid, suspended, or non-administrator binding disables the endpoint with `503`. In-memory test composition keeps its existing isolated contract.

Bootstrap is one atomic transaction. `POST /admin/bootstrap-identity` is available only while the separate configured bootstrap secret exists and one `identity_bootstrap_state` row has `consumed_at IS NULL`. It accepts only ASCII `X-Identity-Bootstrap-Token` (maximum 512 bytes) plus a strict, maximum-4-KiB profile body; the administrator UUID comes from `IDENTITY_BOOTSTRAP_ADMIN_USER_ID` and the role is fixed by the server, never supplied by the caller. After constant-time secret verification, the command locks that state row, creates the preconfigured administrator User/principal/credential, appends audit evidence, sets `consumed_at`, and commits. A concurrent winner is the only request that receives the generated credential. Every replay after commit returns a stable consumed result and never returns that secret again. If the network response is lost after commit, replay remains forbidden; an operator must use the documented out-of-band recovery/credential-provisioning procedure rather than reuse the bootstrap secret.

## Data model and tenancy controls

Existing users, portfolios, positions, instruments, candidate groups and their associations, rotation plans and their associations, configuration layers/snapshots, logical scans/attempts, recommendation states, strategy runs, and child intents remain authoritative. M2 extends rather than replaces them.

| Resource | M2 persisted addition | Access rule |
| --- | --- | --- |
| User | Display profile, status, positive revision; no password column. | Self may read/update safe fields; admin controls status. |
| Principal/credential | Role, keyed digest, revoked/expired data, bootstrap marker. | Internal lookup; provision/revoke is administrator-only. |
| Portfolio | Name, base currency, market scope, enabled flag, revision. | One owning user; resolve owner before every access. |
| Position | Positive row revision despite no M2 HTTP write; optional display join to Instrument. | Portfolio-scoped, historical, and read-only over HTTP. |
| Instrument | Optional display/active SYSTEM metadata plus positive revision. | Globally readable when otherwise authorized; ordinary users cannot mutate it. |
| Candidate group | Name, description, enabled flag, positive revision; membership replacement increments the same revision. | USER-owned and must match the exact plan portfolio owner. |
| Rotation plan | Name, enabled flag, positive revision. Ordered associations stay authoritative. | PORTFOLIO-owned; no cross-portfolio reference. |
| Plan configuration binding | One active immutable snapshot ID plus positive binding revision. | Snapshot targets exactly the bound plan. |
| Scan lease | Versioned scan key, manifest hash, lease ID, plan/portfolio IDs, acquired/expiry timestamps. | Internal adapter reauthorizes plan portfolio. |
| API audit event | Actor/principal/request IDs, verb, resource, outcome, bounded metadata. | Immutable, owner/admin readable only. |

Database constraints duplicate material invariants: foreign keys, association uniqueness/order, positive revision, nonblank bounded text, enum checks, active snapshot target matching, credential digest uniqueness, and lease expiry ordering. They complement, not replace, domain invariants.

The authority rules are: SYSTEM records are reference data and admin-mutable only; USER records require exact owner match; PORTFOLIO access first resolves portfolio-to-user (sibling portfolios of one user remain separate); candidate group owner must equal plan portfolio owner; sale-capable sources must be exact plan-listed, same-portfolio, OPEN, and non-protected; and a run uses only the immutable snapshot actively bound to that exact plan—never a “latest snapshot” lookup.

The execution manifest compares exact ordered values, not only IDs: plan ID/revision and enabled state; portfolio ID/revision/enabled state; active binding ID/revision/snapshot ID; immutable snapshot ID plus canonical hash; each source/protected Position ID/revision/instrument/role/status/quantity; each candidate-group ID/revision/enabled state and ordered member Instrument ID/revision/market/symbol/active state. Snapshot ID plus hash is sufficient because snapshots are immutable. A manifest is canonicalized with its own `execution-input-manifest-v1` format and SHA-256; it does not alter configuration canonical v1.

## API surface

All new routes are `/v1` JSON routes with strict Pydantic models and `extra="forbid"`. Server-generated IDs, ownership, timestamps, revisions, hashes, creators, audit values, and credential digests are never client input. Lists are stable cursor-paginated instead of unbounded offsets.

| Route | Permission and contract |
| --- | --- |
| `GET /v1/me` | Verified caller profile and principal summary. |
| `POST /v1/users` | Administrator-only; server creates ID and role is never client-selected. |
| `GET/PATCH /v1/users/{user_id}` | Self or admin; PATCH uses `If-Match`, and only admin changes status. |
| `POST /v1/users/{user_id}/credentials` | Admin-only; returns a new secret once, with no secret audit/log field. |
| `POST /v1/credentials/{credential_id}/revoke` | Admin-only, revision/audit protected. |
| `POST /admin/bootstrap-identity` | One-time deployment-only bootstrap route; unavailable after atomic consumption and never available to ordinary Bearer credentials. |
| `POST/GET /v1/portfolios`, `GET/PATCH /v1/portfolios/{id}` | Owner/admin; create derives owner from context and PATCH requires `If-Match`. |
| `GET /v1/portfolios/{id}/positions`, `GET /v1/positions/{id}` | Owner/admin read only. No M2 POST/PATCH/DELETE/import/close/confirmation position route exists. |
| `POST/GET/PATCH /v1/candidate-groups`, `GET /v1/candidate-groups/{id}` | Owner/admin; mutation requires revision. |
| `PUT /v1/candidate-groups/{id}/instruments` | Owner/admin bounded complete membership replacement after SYSTEM Instrument validation; requires `If-Match`. |
| `POST/GET/PATCH /v1/rotation-plans`, `GET /v1/rotation-plans/{id}` | Owner/admin; source/protected/group IDs verified transactionally against exact portfolio/user. |
| `POST/GET /v1/configuration-layers` | Server assigns version/ownership. SYSTEM/market/strategy writes are admin-only; USER and portfolio/plan writes require exact ownership. Runtime overrides are not persisted through M2 API. |
| `POST /v1/rotation-plans/{id}/configuration-snapshots` | Owner/admin supplies exact bounded layer selection. Server authorizes, resolves, canonicalizes, versions, and stores snapshot. Merged payload/hash/creator/time are not input. |
| `GET /v1/rotation-plans/{id}/configuration-snapshots`, `GET /v1/configuration-snapshots/{id}` | Authorized immutable snapshot read. |
| `PUT /v1/rotation-plans/{id}/active-configuration-snapshot` | Owner/admin revision-protected target-plan exact match only. |
| `POST /v1/rotation-plans/{id}/run-once` | Owner/admin manual dry-run. Body contains literal `dry_run: true` only; server loads all execution data. |
| `GET /v1/rotation-plans/{id}/strategy-runs`, `GET /v1/strategy-runs/{id}` | Owner/admin gets bounded, redacted immutable evidence. |

M2 does not hard-delete tenant records: users can be suspended, portfolios/plans/groups disabled, credentials revoked, and snapshots/audit records retained. Mutable records have server-managed positive revisions; stale `If-Match` returns `409` with no partial update.

## Configuration snapshot lifecycle

M2 retains seven-layer precedence, merge semantics, `LayerPatchSchema`, `ResolvedConfigurationSchema`, canonical format v1, and hash evidence. It does not create another configuration format.

For a plan snapshot request, route authorization precedes layer lookup. The request supplies at most one exact `{scope, reference_id, version, content_hash}` selection per scope. The repository uses the existing scoped SQL predicates; the resolver validates/merges/canonicalizes/freezes; the server sets creator/time/next target version; then a separate revision-protected plan binding activates the exact snapshot. Run-once reads that binding, never a client snapshot ID or “latest” row.

M2 introduces a typed execution subsection in the resolved configuration schema. It contains only mock-market request and scan identity data: market, IANA timezone, bar windows/intervals, freshness ceiling, and registered deterministic evaluator key. It is validated as one object during resolution. It cannot select a live provider, broker, notification channel, LLM, or scheduler.

The current `RotationEvaluator` is injectable but has no production default. If the active snapshot lacks complete typed execution settings or no registered deterministic evaluator is available, run-once fails closed before provider I/O. It cannot invent defaults, evaluator output, or ACTION.

M2 introduces `scan-identity-v2`. It preserves every existing v1 component—rotation plan ID, market session date, normalized scan window start, interval, market timezone, and the actual configuration snapshot hash—and adds the `execution_input_manifest_hash` plus its manifest format version. The database stores those raw components alongside the v2 key in the logical-scan and lease records. It never substitutes a synthetic manifest hash for the existing configuration snapshot hash. Existing v1 rows remain readable/recoverable under their recorded format; newly created M2 manual scans use v2. This ensures that an input-graph change in the same wall-clock window does not reuse a completed scan for a different plan state.

## PostgreSQL-authoritative run-once

`POST /v1/rotation-plans/{id}/run-once` accepts exactly `{"dry_run": true}`. Only literal Boolean `true` is legal; numbers, strings, false, null, omission, and unknown fields fail before command service entry. The path ID identifies a resource but grants no access. M2 accepts no runtime override.

Before provider I/O, `DatabaseRunOnceCommandService` performs this complete authority load:

1. Load the plan using `RotationPlanRepository.get(plan_id, access_context)`.
2. Load every ordered source/protected Position from PostgreSQL and enforce exact plan/portfolio references, including all three sale boundaries.
3. Load ordered candidate groups, verify exact user ownership, and load their deduplicated global Instrument members. Client symbols/instrument IDs are never route inputs.
4. Load the exact active plan snapshot, validate target/owner/hash/canonical evidence, and parse typed execution settings.
5. Construct immutable `ExecutionInputManifest` containing plan revision; snapshot ID/hash; source/protected IDs and revisions; group/membership revisions; and selected instrument IDs. Store a bounded copy in strategy-run evidence so later rows are not needed to interpret a run.
6. Internally build full `RunCoordinatorRequest` with server time, verified context, `TriggerSource.API`, and server correlation ID.
7. After scan lease acquisition, re-read binding and manifest before provider I/O. A concurrent edit returns typed conflict and calls no provider.
8. Invoke existing coordinator with SQLAlchemy repositories, PostgreSQL lease, deterministic mock provider/calendar, and registered deterministic evaluator only.

### Manual scan identity derivation

The typed execution subsection defines one market, its IANA timezone, positive `scan_interval`, source-bar interval, `daily_lookback_completed_sessions` in the bounded range 1–250, and maximum data delay. M2 manual run-once supports one market session grid per plan; a mixed-market plan is rejected during configuration/activation rather than silently choosing one timezone. Configuration validation requires every configured regular/special session used by that plan to be an exact multiple of `scan_interval`, and requires the source-bar interval to align with that grid. This guarantees that a manual scan never names a partial closing window.

The server injects one UTC clock value and converts it to the execution market timezone. It asks the mock trading calendar for that local date's sessions and selects only a session containing the instant with left-closed/right-open semantics: `opens_at <= now_market < closes_at`. If there is no such session, no lock or provider call occurs: holiday has code `MARKET_CLOSED`; before open has `MARKET_NOT_OPEN`; a break between sessions has `SESSION_BREAK`; and post-close has `MARKET_CLOSED`. Each is a typed manual-command `409`, not an ACTION or a fabricated old window.

For an active session, let `completed = floor((now_market - opens_at) / scan_interval)`. If `completed < 1`, the code is `SCAN_WINDOW_NOT_COMPLETE` and provider I/O does not occur. Otherwise the normalized scan window is `[opens_at + (completed - 1) * scan_interval, opens_at + completed * scan_interval)`. `market_session_date` is the start date in market timezone, and `scan_window_start` is the normalized left boundary. Intraday request data starts at this current session's `opens_at` and ends at normalized window end, so indicators receive only complete current-session bars and never bridge a break. `daily_end` is that market session date (exclusive, thereby excluding the still-open current day); `daily_start` is the earliest of exactly `daily_lookback_completed_sessions` preceding open-market dates, found by a bounded reverse calendar walk. A configuration whose calendar cannot supply that history produces typed `INSUFFICIENT_DAILY_HISTORY` before provider I/O.

The complete `MarketDataRequest` is deterministic: instruments come from the manifest in canonical order; `requested_at` is normalized window end; and `request_id` is UUIDv5 over the `scan-identity-v2` key with a fixed request-format namespace. Therefore two manual requests with identical manifest/configuration during one completed window construct the same request and scan key, while adjacent windows construct different values. Per-request wall-clock `now`, random UUIDs, and client values never enter the scan identity.

### Transaction topology and coordinator phase boundary

M2 must not hold a database transaction over provider I/O, and it must not allow a RUNNING scan attempt to roll back together with a process crash. It introduces a narrow `CoordinatorPhaseStore` port used by `RunCoordinator` itself. The port owns only committed persistence phases; it does not evaluate rules, choose a recommendation, call a provider, or reproduce coordinator decision logic in an HTTP wrapper.

| Phase | Transaction boundary and required result |
| --- | --- |
| A. Authoritative load | Short read transaction loads plan, active snapshot, execution configuration, and input graph; it builds manifest and v2 identity, then commits/closes. |
| B. Lease acquire | Separate short PostgreSQL transaction atomically acquires/reclaims the exact v2 scan lease and commits it before any provider work. Failure returns duplicate/conflict with no provider call. |
| C. Begin attempt | `CoordinatorPhaseStore.begin_attempt` starts a fresh transaction, locks/reloads the execution graph, compares it exactly with Phase A manifest, creates/recover logical scan, persists RUNNING `ScanAttempt`, and commits. A mismatch returns `409 EXECUTION_INPUT_CHANGED`, releases lease, and performs no provider I/O. |
| D. Provider work | The existing coordinator invokes deterministic mock provider outside a database transaction, using the committed attempt handle. |
| E. Finalize | `CoordinatorPhaseStore.finalize_attempt` starts a fresh transaction, locks the logical scan/attempt/recommendation-state rows, rechecks manifest/binding, records terminal attempt, strategy run, recommendation transition, child intent, and final scan attachment with compare-and-swap checks, then commits one durable outcome. |
| F. Lease release | A short transaction deletes only the exact `(scan_key, lease_id)` in a `finally` path. |

If Phase D succeeds but a process dies before Phase E, Phase C's committed RUNNING evidence remains available to the existing recovery rules. If manifest/binding changed before or during Phase E, Phase E records a non-actionable terminal `EXECUTION_INPUT_CHANGED` outcome and never creates an ACTION recommendation. Finalization compares expected logical-scan RUNNING state, attempt RUNNING state, manifest hash/version, active-binding revision, and recommendation-state revision. A failed compare-and-swap returns the existing duplicate/superseded result; it never applies a second transition. This preserves one coordinator decision path while making its durable phases explicit.

The route never deserializes a coordinator request, template, position, snapshot, candidate list, market request, or evaluator output from caller JSON. Its response is the existing safe coordinator summary only.

Existing logical scans/strategy runs remain durable idempotency records. M2 adds `scan_leases` only for the pre-provider `LockProvider`: acquire/reclaim an unexpired v2 key atomically in a committed short transaction; record key, identity/manifest format versions, raw identity components, manifest hash, lease, plan/portfolio, acquired/expiry; release only exact `(key, lease_id)`; reclaim expired lease then use existing logical-scan recovery; failed acquire returns duplicate/conflict with no provider I/O. This is manual serialization, not a scheduler or distributed job system.

## Input and resource limits

A streaming ASGI policy rejects oversized body bytes before JSON parsing. Strict schemas enforce types, authorization precedes expensive graph loads, and database statements have bounded timeouts.

| Boundary | M2 contract |
| --- | --- |
| Authorization | One ASCII Bearer credential, maximum 512 bytes; duplicate authorization headers rejected. |
| Identifiers | Canonical UUID only; malformed IDs fail before repository access. |
| Request ID | Server-generated; client correlation headers are not audit identity. |
| Pagination | Positive page size up to 100; opaque validated cursor up to 512 bytes. |
| User text | Email 254 UTF-8 bytes, display name 128, IANA timezone 64, locale 32. |
| Portfolio/group/plan text | Name 128 UTF-8 bytes, description 4 KiB, no blank/control-only name. |
| Candidate membership | At most 500 unique Instrument UUIDs per replacement. |
| Plan references | At most 100 sources, 100 protected, 32 groups; unique per role and source/protected disjoint. |
| Resource command body | User/portfolio/group/plan up to 64 KiB. |
| Configuration command body | Layer/snapshot selection up to 192 KiB plus existing 16 KiB string, 128 KiB DSL/ruleset/input/evidence, and 256 KiB output boundaries. |
| Run-once body | Existing pre-parse 16 KiB ceiling; legal body only literal true. |
| Bootstrap body | Deployment-only strict profile body up to 4 KiB; bootstrap header follows authorization-header ASCII/512-byte limit. |
| Evidence response | Snapshot/API evidence at most 256 KiB canonical payload, never truncated. |
| Database work | Positive configured statement/lock limits; bounded joins/pages only. |

All relevant text and JSON limits are UTF-8 bytes. Unknown fields, coercions, non-finite values, naive datetimes, duplicate references, invalid unicode/identifiers, inaccessible rows, and limit excess fail closed. Errors contain stable codes and safe boundary information, never raw huge payloads, credentials, or unauthorized IDs.

## Audit, migrations, rollback

Accepted and rejected sensitive commands write immutable bounded audit events in the outcome transaction where possible: `event_id`, `occurred_at`, `actor_user_id`, `principal_id`, `request_id`, `operation`, `resource_type`, `resource_id`, `result_code`, and bounded metadata. Metadata contains IDs/revisions/error classes only; never Bearer tokens, digests, headers, secrets, raw market data, full personal data, or position updates. Strategy-run evidence remains the sole coordinator evidence record.

Migrations start after `0005_legacy_finalization_claims` and are additive: (1) principal/credential/bootstrap-state/audit tables plus nullable profile and positive revision metadata for User, Portfolio, Position, Instrument, CandidateGroup, and RotationPlan; (2) explicit legacy backfill (`revision=1`) plus incomplete-profile marker without inventing name/e-mail/market values; (3) exact active plan-snapshot binding with its own revision plus canonical execution-manifest storage; (4) v2 logical-scan raw identity/manifest columns and PostgreSQL scan-lease table with expiry/index constraints; (5) route activation only after migration and database-readiness checks.

Fresh PostgreSQL and upgrade-from-`0005` databases must both pass. The last M0/M1 application remains compatible with expanded but inactive schema. Production rollback is application/route disablement while retaining schema and immutable evidence. Alembic downgrade is tested only before activation or on disposable databases; a production downgrade that drops credential, audit, binding, or lease evidence requires backup and explicit operator approval.

## Review checkpoints

These are review gates, not an implementation task list. A separately approved implementation plan will enumerate files and commits.

| Checkpoint | Demonstrable result |
| --- | --- |
| M2-D0 | Gap analysis and design approved; no code before approval. |
| M2-C1 | DB composition, request UoW, verified identity/context, DB readiness; unavailable config fails closed. |
| M2-C2 | Tenancy-safe user/portfolio/read-only-position/group/plan APIs, revisions, limits, audit. |
| M2-C3 | Scoped layers, immutable snapshot API, exact active plan binding. |
| M2-C4 | Authoritative loader, PostgreSQL lease, shared legacy/new run-once, manifest, fail-closed evaluator binding. |
| M2-C5 | Upgrade/rollback rehearsal, documentation, full quality/CI review; no M3+ scope. |

## Test matrix and acceptance

| Test layer | Required evidence |
| --- | --- |
| Unit/application | Wire-format parser, direct credential-row lookup, HMAC preimage/version, key rotation, active predicate, role/status invalidation, legacy principal binding, atomic bootstrap replay, context conversion, revisions, snapshot binding, and fail-closed evaluator. |
| Repository contract | Every operation takes context; absent/non-owned same outcome; SYSTEM Instrument read-only; sibling portfolios isolated; position/group/plan/binding/instrument revision and manifest equality exact. |
| PostgreSQL | Fresh and upgrade-from-0005 migrations; disposable downgrade/upgrade; UUID/JSON/timezone, partial OPEN index, revisions, binding, v2 identity raw fields, lease acquire/reclaim/release, bootstrap unique winner, and concurrent CAS finalization. |
| API | Invalid/expired/revoked/non-ASCII credentials; identity spoofing; 401/404/403/503; legacy token with invalid principal; body/cursor limits; no secret response. |
| Run-once | Only literal true; authority graph from DB; client inputs forbidden; session/break/holiday/pre-open/unfinished-window responses; same-window equality and adjacent-window difference; committed RUNNING attempt survives simulated crash; stale manifest/lock conflict no provider; invalid data cannot ACTION. |
| Safety regression | Protected/NORMAL/foreign/CLOSED position cannot be sale source; no API calls position-write ports; no live provider/broker/notification/LLM/scheduler is constructed. |
| Quality | Focused tests, full `pytest`, `mypy app tests`, Ruff check/format, diff check, Ubuntu/Windows/PostgreSQL-contract/runtime-PostgreSQL-smoke CI. |

M2 is accepted only when database composition preserves safe import behaviour; credential lookup, rotation, bootstrap, and legacy admin identity are deterministic and auditable; all API identity is server-derived; tenant records and snapshots cannot leak; positions remain HTTP read-only; run-once rebuilds inputs from PostgreSQL, commits RUNNING evidence before mock provider I/O, uses stable v2 manual scan identity, and reaches existing idempotency; all inputs are bounded; migrations/rollback are rehearsed; and the final diff contains no prohibited M3+ capability.
