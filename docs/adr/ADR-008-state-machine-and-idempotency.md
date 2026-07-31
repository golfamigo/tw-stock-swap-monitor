# ADR-008: State Machine and Idempotency

## Status

Accepted.

## Context

Workers can restart, API and schedules can overlap, and notifications are retried.

## Decision

Use a code-defined recommendation state graph, a pre-fetch scan-lock key, and a post-fetch final strategy-run key containing the market-data snapshot. Both triggers share RunCoordinator and LockProvider.

## Consequences

Duplicate logical scans reuse evidence instead of re-evaluating. A changed snapshot after an incomplete retry remains auditable but cannot create a second completed scan. Execution and actual positions remain separate from recommendation state.
