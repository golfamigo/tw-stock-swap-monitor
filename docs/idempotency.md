# Idempotency and Concurrency

## Two-phase identity

A provider snapshot does not exist until after a fetch, so one identifier cannot both lock a scan before fetch and identify its exact input after fetch. The coordinator uses two related keys.

The scan-lock key is SHA-256 over canonical JSON containing:

~~~text
rotation_plan_id
market_session_date
scan_window_start
scan_interval
config_snapshot_hash
~~~

It is built before provider access. It serializes the logical scan and prevents a scheduler retry or admin dry-run from duplicating provider, LLM, notification-intent, or state-transition work.

The final strategy-run key is SHA-256 over canonical JSON containing:

~~~text
scan_lock_key
market_data_snapshot_id
market_data_content_hash
~~~

It identifies the exact reproducible evaluation. The provider content hash uses the same documented canonical format rules as configuration hashes. All time components are normalized to the plan market IANA timezone before serialization.

## Retry behavior

The lock owner records a RUNNING scan attempt before fetching. If it completes successfully, later callers with the same scan-lock key return the completed strategy run without fetching current data. If the provider fails or returns invalid data before a strategy run completes, the attempt is recorded as failed/degraded and a bounded retry while holding the same scan lock may fetch again.

A retry may receive a new market snapshot for the same logical scan. The incomplete prior attempt is retained for audit with its snapshot reference and failure disposition; the retry receives a new strategy-run key and may become the one completed evaluation for the scan-lock key. Once a completed run is associated with a scan-lock key, no later snapshot can create a second completed evaluation, LLM invocation, or notification intent for that logical scan. Recovery after a process crash first reads the persisted scan attempt and completed-run association before fetching.

## Required behavior

RunCoordinator acquires scan-lock key before I/O. If the key is completed, StrategyRunRepository returns its stored outcome. If it is in progress, an API caller receives a conflict or existing-result reference; a scheduler skips it. LLM and notification intents use child keys derived from the final strategy-run key plus their deterministic purpose, so retries cannot duplicate them.

Run-once and scheduled execution invoke the same coordinator and key builders. Run-once requires an explicit plan ID, dry-run flag, access context, and audit event; it never changes positions.

## M0/M1 and production adapters

M0/M1 supplies InMemoryLockProvider and an in-memory scan/strategy-run repository that atomically records scan-lock acquisition, attempt state, final strategy-run association, and child intents. Contract tests simulate API and scheduler callers racing for one scan-lock key and assert one provider fetch and one completed evaluation.

The persistence design reserves unique constraints on scan_runs.scan_lock_key, strategy_runs.strategy_run_key, and child-intent keys. A later distributed LockProvider may use Redis or PostgreSQL advisory locking; it must preserve acquire, complete, release, retry, and get-result semantics. A uniqueness violation is treated as a duplicate outcome, not a second run. Lock expiration is bounded by the validated run deadline and recovery checks persistence first.

## Audit data

Every attempt records actor, trigger source, scan-lock key, final strategy-run key when available, key components, configuration snapshot hash, market-data snapshot ID/content hash, started/finished timestamps, final disposition, and duplicate-of references. Secrets and raw authentication tokens are excluded.
