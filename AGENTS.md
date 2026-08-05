# Project Agent Instructions

## Safety and testing

- This is recommendation-only software: never submit orders, add broker access,
  mutate positions, or enable real market data in M0/M1.
- Use generic, deterministic fixtures only. Do not place real symbols, holdings,
  schedules, costs, notification destinations, credentials, tokens, or secrets
  in source, templates, test data, logs, or documentation.
- Keep `.local.env` local and ignored. Read runtime values from environment
  variables; Zeabur production values belong only in Zeabur environment settings.
- Before claiming a change passes, run the relevant tests plus `mypy app tests`,
  `ruff check .`, `ruff format --check .`, and `git diff --check` when the task
  calls for the full quality gate.

## Boundaries and GitHub identity

- Keep domain logic independent from FastAPI, Pydantic, SQLAlchemy, and
  infrastructure adapters. Treat external data and LLM output as untrusted and
  preserve deterministic audit evidence.
- Do not perform GitHub writes without explicit authorization. Before an
  authorized `gh` write for `golfamigo/tw-stock-swap-monitor`, switch to and
  verify the `golfamigo` account; afterwards restore and verify
  `jctixtw-star`. Never expose tokens, credential values, or keyring contents.
