# ADR-001: Configuration-Driven Architecture

## Status

Accepted.

## Context

Markets, symbols, thresholds, intervals, weights, stages, costs, models, and destinations vary by user and plan.

## Decision

Use typed sparse LayerPatchSchema inputs, immutable ResolvedConfigurationSchema snapshots, and versioned canonical serialization. YAML is seed/test/import data; PostgreSQL snapshots are execution truth.

## Consequences

The core contains no business constants. Runs are reproducible by embedded merged payload and canonical hash. Patch validation and snapshot validation have separate contracts, which prevents incomplete overrides from masquerading as executable configuration.
