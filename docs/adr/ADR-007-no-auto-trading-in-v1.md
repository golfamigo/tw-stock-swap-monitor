# ADR-007: No Automatic Trading

## Status

Accepted.

## Context

Monitoring and recommendation do not establish execution authority or prove a broker transaction occurred.

## Decision

Do not model brokerage credentials or submit orders. Notifications and recommendations leave positions unchanged until a separate confirmed-execution workflow.

## Consequences

Risk is reduced and actual holdings remain trustworthy. Any trading capability requires a later explicit product and security decision.
