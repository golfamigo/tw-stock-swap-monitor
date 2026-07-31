# ADR-001: Configuration-Driven Architecture

## Status

Accepted.

## Context

Markets, symbols, thresholds, intervals, weights, stages, costs, models, and destinations vary by user and plan.

## Decision

Use validated scoped configuration layers and immutable resolved snapshots. YAML is seed/test/import data; PostgreSQL snapshots are execution truth.

## Consequences

The core contains no business constants. Runs are reproducible by embedded merged payload and hash. Configuration validation and snapshot storage add implementation work but prevent hidden behavior.
