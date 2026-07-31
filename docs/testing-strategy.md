# Testing Strategy

## Test layers

Unit tests cover pure domain invariants, configuration merge rules, bar aggregation, VWAP, same-time volume ratio, typed DSL validation/evaluation, candidate scoring, Decimal sizing, state transitions, and idempotency key construction. They require no FastAPI, database, network, or wall-clock time.

Contract tests exercise every Protocol with its M0/M1 adapter: MarketDataProvider, TradingCalendarProvider, Indicator, RuleEngine, ScoringEngine, SizingEngine, LLMProvider stub, NotificationChannel stub, repositories, and LockProvider. Each contract describes success, validation failure, and repeat-call behavior.

Integration tests use FastAPI TestClient and in-memory adapters to exercise the protected run-once request, shared coordinator, authorization behavior, and an API/scheduler concurrency race. SQLAlchemy and PostgreSQL integration is planned after the initial migration is available; it is not allowed to replace pure unit tests.

## Deterministic fixtures

Fixtures use a fixed IANA timezone, a fixed trading date, a fixed session calendar including a break and holiday, fixed UUIDs, and a fixed mock-provider seed. They expose only generic identifiers such as source-a and candidate-a; they never embed a real symbol, holder, or strategy threshold. A same-seed assertion compares complete serialized bar output byte-for-byte.

## Required cases

1. 3-minute and 15-minute bars use left-closed/right-open boundaries, respect a session break, holiday, and timezone conversion.
2. VWAP resets at configured session boundaries; same-time volume ratio compares the configured lookback and rejects incomplete history.
3. Source weakening requires its configured evidence and does not hard-code an issuer or threshold.
4. Scoring produces stable ranking and refuses missing/invalid factor data.
5. Three configured stages, fees, slippage, lot constraints, reserve, and allocation weights produce valid actual quantities with Decimal.
6. No sizing or state path may sell a protected position.
7. Seven-level configuration precedence, recursive maps, replacement lists, explicit null, delete directive, conflicts, and expired runtime overrides have explicit expected outcomes.
8. DSL rejects unknown operators, unknown paths, excessive depth/nodes, invalid types, and missing data without silently coercing.
9. A duplicate run key and concurrent API/scheduler request yield one strategy outcome, no duplicate LLM intent, and no duplicate notification intent.
10. Cross-tenant access fails closed; global instruments remain shareable but immutable to regular users.
11. Missing or stale market fields move to DATA_DEGRADED and cannot create ACTION.
12. A notification does not change execution status or position quantity.

## Quality gates

The implementation PR runs pytest, ruff check, ruff format check, and mypy. Tests include UTC and market-timezone cases. Any test requiring real data, a real secret, a live LLM, a broker, or a network call is excluded from M0/M1 and replaced with a contract fixture.
