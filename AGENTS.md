# Project Agent Instructions

## Non-negotiable safety

- The system must not submit brokerage orders or store broker credentials.
- Never hard-code asset symbols, holdings, thresholds, schedules, allocation stages, costs, model names, or notification destinations. Use validated configuration.
- Keep domain logic independent from FastAPI, Pydantic, SQLAlchemy, and infrastructure adapters.
- Treat all external data and LLM output as untrusted; preserve audit evidence and enforce deterministic invariants.

## GitHub identity procedure

The GitHub CLI normally uses the local account jctixtw-star. Before any GitHub write operation for golfamigo/tw-stock-swap-monitor, switch explicitly to the golfamigo account and verify the active account. Immediately after the operation, switch back to jctixtw-star and verify the restoration. Never expose tokens, credential values, or keyring contents in output.
