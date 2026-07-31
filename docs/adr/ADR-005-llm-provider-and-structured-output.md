# ADR-005: LLM Provider and Structured Output Boundary

## Status

Accepted for interface only.

## Context

LLM output is untrusted and must not own arithmetic, holdings, or market facts.

## Decision

Define LLMProvider as a Protocol and structured request/response schemas now; defer provider implementation and calls.

## Consequences

Future models are replaceable and outputs must enter deterministic validation. No model name or prompt is embedded in code.
