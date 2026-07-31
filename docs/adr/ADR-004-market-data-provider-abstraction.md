# ADR-004: Market Data Provider Abstraction

## Status

Accepted.

## Context

The system must work first with reproducible data and later replace providers without strategy rewrites.

## Decision

Define MarketDataProvider and TradingCalendarProvider Protocols using typed domain values. Implement deterministic mock adapters in M0/M1.

## Consequences

Live adapters can be added later. Data quality remains an explicit input and bad data cannot produce action.
