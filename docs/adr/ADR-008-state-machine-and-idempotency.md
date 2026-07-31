# ADR-008: State Machine and Idempotency

## Status

Accepted.

## Context

Workers can restart, API and schedules can overlap, and notifications are retried.

## Decision

Use a code-defined recommendation state graph and an idempotency key derived from plan, session/window/interval, configuration hash, and market-data snapshot. Both triggers share RunCoordinator and LockProvider.

## Consequences

Duplicate work reuses evidence instead of re-evaluating. Execution and actual positions remain separate from recommendation state.
