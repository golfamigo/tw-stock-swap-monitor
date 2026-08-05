# Database ERD

This is the persistence projection, not the domain model. Tables map through explicit mapper functions and do not leak SQLAlchemy objects into the domain layer.

~~~mermaid
erDiagram
  USERS ||--o{ PORTFOLIOS : owns
  PORTFOLIOS ||--o{ POSITIONS : contains
  PORTFOLIOS ||--o{ ROTATION_PLANS : owns
  USERS ||--o{ CANDIDATE_GROUPS : owns
  CANDIDATE_GROUPS }o--o{ INSTRUMENTS : contains
  ROTATION_PLANS }o--o{ POSITIONS : source_or_protects
  ROTATION_PLANS }o--o{ CANDIDATE_GROUPS : observes
  INSTRUMENTS }o--|| MARKETS : listed_in
  MARKETS ||--o{ TRADING_CALENDARS : uses
  CONFIGURATION_SNAPSHOTS ||--o{ STRATEGY_RUNS : frozen_for
  ROTATION_PLANS ||--o{ STRATEGY_RUNS : executes
  ROTATION_PLANS ||--o{ SCAN_RUNS : schedules
  SCAN_RUNS ||--o{ SCAN_ATTEMPTS : records
  SCAN_RUNS ||--o| STRATEGY_RUNS : completes_as
  MARKET_DATA_SNAPSHOTS ||--o{ STRATEGY_RUNS : inputs
  STRATEGY_RUNS ||--o{ RULE_EVALUATIONS : produces
  STRATEGY_RUNS ||--o{ CANDIDATE_SCORES : produces
  STRATEGY_RUNS ||--o| SIZING_RESULTS : produces
  ROTATION_PLANS ||--o{ ROTATION_STATES : has
  ROTATION_STATES ||--o{ STATE_TRANSITIONS : records
  STRATEGY_RUNS ||--o{ AUDIT_LOGS : audits
~~~

## Scope-bearing tables

Configuration profiles and templates include scope, nullable owner_id, version, content hash, and immutable payload. A CHECK constraint makes SYSTEM require a null owner, USER require a user owner, and PORTFOLIO require a portfolio owner. A unique scope/owner/name/version constraint prevents ambiguous profiles.

Users, portfolios, positions, candidate groups, plans, decisions, notifications, strategy runs, execution confirmations, and audit logs use their natural owning aggregate foreign key. Instruments, markets, provider definitions, calendars, and tick-size rules are SYSTEM reference data and do not receive fabricated portfolio ownership columns.

## Important constraints

- instruments has a unique market and symbol pair.
- positions permits closed-position history. It has a partial unique index on portfolio_id and instrument_id only where status is OPEN, so at most one current open representation exists while any number of CLOSED historical rows remain valid.
- rotation_plan_candidate_groups is an association table with a unique rotation_plan_id/candidate_group_id pair; it represents the required one-to-many plan attachment without placing a single candidate_group_id on rotation_plans.
- rotation-plan source and protected association tables have a unique plan/position pair and reject a pair appearing in both roles.
- scan_runs has a unique scan_lock_key and at most one completed strategy_run_id.
- scan_attempts records each failed, degraded, or retried provider attempt for a scan run.
- strategy_runs has a unique strategy_run_key, derived only after an exact market-data snapshot is available.
- configuration_snapshots has a unique scope, owner, target type, target ID, and config_version tuple, and content_hash is indexed.
- audit logs are append-only and carry actor, resource type/ID, request ID, and payload hash.

The first Alembic migration creates these foundations and their ownership/uniqueness constraints. High-volume bars remain out of scope for live persistence in M0/M1; only typed mock snapshot metadata is required.
