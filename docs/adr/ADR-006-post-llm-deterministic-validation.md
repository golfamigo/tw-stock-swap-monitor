# ADR-006: Post-LLM Deterministic Validation

## Status

Accepted.

## Context

An LLM may propose unauthorized assets, protected sales, stale decisions, or impossible quantities.

## Decision

Validate schema, whitelists, freshness, rules, stages, and recomputed sizing after any future LLM response. Program invariants override the response.

## Consequences

LLM suggestions can be adjusted or rejected with audit evidence; they never place orders.
