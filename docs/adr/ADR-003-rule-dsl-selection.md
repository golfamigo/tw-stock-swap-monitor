# ADR-003: Restricted JSON Logic Rule DSL

## Status

Accepted.

## Context

Rules must be configurable without giving configuration authors code execution.

## Decision

Adopt a small JSON-Logic subset with JSON Schema transport validation, typed AST semantic validation, allowlisted operators and paths, resource caps, and evidence output.

## Consequences

No eval or arbitrary Python occurs. Some advanced expressions require future operator additions, which are intentional reviewed code changes.
