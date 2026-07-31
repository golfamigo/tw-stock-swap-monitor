# Idempotency and Concurrency

## Key

For every strategy run, the canonical idempotency key is the SHA-256 digest of canonical JSON containing:

~~~text
rotation_plan_id
market_session_date
scan_window_start
scan_interval
config_snapshot_hash
market_data_snapshot_id
~~~

All timestamps are normalized to the plan market IANA timezone before serialization. The key changes when a new market-data snapshot or resolved configuration is used, but retries of the same work retain the key.

## Required behavior

The RunCoordinator calls LockProvider.acquire before fetching expensive downstream work. If the key is completed, StrategyRunRepository returns its stored outcome. If it is in progress, an API caller receives a conflict or an existing-result reference; a scheduler skips it. LLM invocation and notification intents use a child key derived from the strategy-run key plus their deterministic purpose, so retries cannot duplicate either action.

Run-once and scheduled execution invoke the same coordinator with the same key builder. They cannot have separate decision paths. Run-once requires an explicit plan ID, dry-run flag, access context, and audit event; it never changes positions.

## M0/M1 and production adapters

M0/M1 supplies InMemoryLockProvider for a single process and an in-memory strategy-run repository that atomically records acquired keys. Its contract tests simulate API and scheduler callers racing for the same key and assert one evaluation.

The persistence design reserves a unique constraint on strategy_runs.idempotency_key and uses an insert-or-fetch operation. A later distributed LockProvider may use Redis or PostgreSQL advisory locking; it must preserve the same acquire, complete, release, and get-result semantics. A database uniqueness violation is treated as a duplicate outcome, not a second run. Lock expiration is bounded by the validated run deadline and recovery always checks persisted completion first.

## Audit data

Every attempt records actor, trigger source, key, key components, configuration snapshot hash, market-data snapshot ID, started/finished timestamps, final disposition, and any duplicate-of run ID. Secrets and raw authentication tokens are excluded.
