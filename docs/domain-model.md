# Domain Model

## Ownership model

Ownership is represented by Scope and an optional owner identifier, rather than copying user_id and portfolio_id to every table.

| Scope | owner_id | Examples | Mutation rule |
| --- | --- | --- | --- |
| SYSTEM | null | instruments, markets, calendars, tick rules, provider definitions, system templates | administrator only |
| USER | user UUID | private strategy profiles, candidate groups, notification profiles | owning user or administrator |
| PORTFOLIO | portfolio UUID | positions, rotation plans, plan overrides, decisions, strategy runs | portfolio owner or administrator |

Portfolio belongs to exactly one User. A repository is always called with an AccessContext containing actor user ID, optional administrator capability, and requested ownership boundary. It verifies ownership before returning an entity; an identifier by itself is never authority. Global references are readable to authorized users but mutable only by administrators. A portfolio-scoped resource may reference a SYSTEM resource or one owned by the same user, never another user's resource.

## Aggregates and value objects

User owns portfolios. Portfolio owns positions and rotation plans. A portfolio may retain any number of CLOSED historical positions for the same instrument, while at most one OPEN position representation exists for that portfolio/instrument pair. CandidateGroup is user-scoped and references global Instruments. RotationPlan references source positions, protected positions, candidate groups, and scoped or global profiles. StrategyRun is immutable evidence of one resolved configuration snapshot, market-data snapshot, state transition, and deterministic outputs.

Important value objects are InstrumentRef, Money, Quantity, Percentage, MarketSession, ConfigurationSnapshotRef, IdempotencyKey, AccessContext, and PriceZone. Money and Quantity use Decimal; MarketSession requires a timezone-aware open and close. Positions use a role enum: ROTATION_SOURCE, PROTECTED_CORE, NORMAL, or CASH_PROXY.

## Invariants

- A protected position is never eligible as a source sale, regardless of rule output, stage, or caller.
- Candidate instruments must be authorized by a candidate group attached to the same plan.
- A source position must belong to the plan portfolio and be open.
- A rotation plan never crosses ownership boundaries.
- A sizing result cannot sell more than the source quantity, buy a negative quantity, violate lot rules, or exceed available cash after configured costs and reserve.
- A decision notification is not an execution confirmation. Actual holdings only change through a later confirmed transaction aggregate.
- All domain timestamps are timezone-aware; all money-like values are Decimal and non-finite values are rejected.

## Domain and persistence mapping

Domain entities are frozen or mutation-controlled Python dataclasses. Persistence has separate SQLAlchemy tables with UUID primary keys and explicit foreign keys. Mapper functions in persistence convert between the two. SQLAlchemy relationships are not exposed to application services, which depend only on repository Protocols.

## Required Protocols

PositionRepository, RotationPlanRepository, StrategyRunRepository, and LockProvider live in app/repositories. MarketDataProvider, TradingCalendarProvider, Indicator, RuleEngine, ScoringEngine, SizingEngine, LLMProvider, and NotificationChannel live beside their domains. Each Protocol uses pure domain inputs and outputs; adapters own I/O and framework conversion.

## Run state separation

RotationRecommendationState represents the system recommendation lifecycle. ExecutionStatus represents whether the user confirmed, partially confirmed, rejected, or has not confirmed any execution. Position state represents actual persisted holdings. These values are deliberately independent: ACTION_NOTIFIED with UNCONFIRMED leaves actual holdings unchanged.
