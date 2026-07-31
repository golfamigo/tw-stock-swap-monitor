# Milestone 0 and 1 Foundation Design

## Decision

Adopt a layered modular architecture. The pure Python domain is independent of FastAPI, Pydantic, pandas, SQLAlchemy, and PostgreSQL. Boundary schemas, adapters, persistence projection, and application orchestration depend inward on that domain. Configuration is canonical only as a validated immutable database snapshot; YAML remains a seed and testing format.

## Boundaries

Ownership is scope-based: SYSTEM global reference data, USER private profiles and groups, and PORTFOLIO plans, positions, runs, and decisions. Repositories take AccessContext and fail closed. No table receives a mechanical user_id or portfolio_id when its data is global.

The M0/M1 application defines all required Protocols and supplies deterministic/in-memory implementations. It computes 3-minute and 15-minute bars, session VWAP, same-time volume ratio, safe DSL rules, candidate score, staged Decimal sizing, and an explicit recommendation state machine. It does not connect to live markets, place orders, notify users, invoke an LLM, or mutate actual positions from a recommendation.

## Safety decisions

Rules are a restricted, schema-validated JSON-Logic subset with bounded tree depth and evidence capture. Idempotency is based on plan, market session/window/interval, frozen configuration hash, and market-data snapshot. Run-once and scheduling share one coordinator and lock contract. Protected positions are rejected by domain validation, sizing, and transition guards.

## Deliverables for review

The accompanying architecture, domain, configuration, DSL, state, idempotency, tenancy, testing, ERD, ADR, and implementation-plan documents are the specification for the implementation PR. The implementation plan deliberately defers live integrations and product APIs to later milestones.

## Review outcome

This design is ready for review. Implementation begins only after the design PR is approved.
