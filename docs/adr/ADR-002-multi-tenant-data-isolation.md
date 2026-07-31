# ADR-002: Multi-Tenant Data Isolation

## Status

Accepted.

## Context

Private portfolio data must be isolated while instruments and calendars are shared.

## Decision

Use SYSTEM, USER, and PORTFOLIO ownership scopes, explicit owner IDs, AccessContext-bound repositories, and mapper-separated persistence.

## Consequences

Global tables avoid meaningless portfolio columns. Every query requires authorization, with tests that prove cross-tenant denial.
