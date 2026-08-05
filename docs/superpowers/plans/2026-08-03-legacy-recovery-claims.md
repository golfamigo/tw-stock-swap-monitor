# Legacy Recovery Claims Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permit `0002` finalization recovery only for a durable claim tied to one exact strategy run and final key.

**Architecture:** Add immutable migration-era claim evidence to recommendation state. Revision `0005` deterministically classifies every unfenced pre-existing state as one exact claim, ambiguous, or no-claim. Coordinator recovery accepts only an exact claim and independently validates the stored scan/run/intent evidence.

**Tech Stack:** Python, SQLAlchemy/Alembic, frozen domain records, in-memory and SQL repositories, pytest, Ruff, mypy.

---

### Task 1: Define claim evidence

**Files:**
- Modify: `app/domain/enums.py`
- Modify: `app/state_machine/states.py`
- Modify: `app/persistence/models.py`
- Modify: `app/persistence/mappers.py`
- Modify: `app/persistence/in_memory.py`
- Modify: `app/persistence/repositories.py`
- Test: `tests/integration/test_coordinator_safety_hardening.py`

- [ ] Write a failing marked-claim recovery test, then run it and confirm the current heuristic gate fails.
- [ ] Add `NOT_APPLICABLE`, `CLAIMED`, `NO_CLAIM`, and `AMBIGUOUS` claim statuses plus an optional claimed run/key pair. Only `CLAIMED` permits the pair; every other status requires both values null.
- [ ] Map the claim through the domain, SQL model, SQL/in-memory state repositories, and preserve it on ordinary state transitions.

### Task 2: Backfill migration provenance

**Files:**
- Create: `migrations/versions/0005_legacy_finalization_claims.py`
- Test: `tests/contract/test_task_four_contracts.py`

- [ ] Write an upgrade fixture that proves a unique matching running scan/run is claimed, while zero and multiple candidates are explicitly classified without any run/key.
- [ ] Add the additive claim columns and constraints. Enumerate only a state’s running scans with an exact stored strategy run, matching scan key/configuration and recommendation state evidence, and no final success attempt. Write `CLAIMED` only for one candidate, otherwise `NO_CLAIM` or `AMBIGUOUS`.
- [ ] Keep downgrade additive: drop only the claim columns and constraint.

### Task 3: Gate coordinator recovery

**Files:**
- Modify: `app/application/run_coordinator.py`
- Test: `tests/integration/test_coordinator_safety_hardening.py`

- [ ] Add RED regressions for a uniquely claimed legacy recovery, a concurrent non-noop stored candidate targeting the same state, and unmarked/ambiguous recovery.
- [ ] Change `_is_legacy_pending_finalization` to require `CLAIMED` and an exact stored run/key match before checking the existing scan, no-final-success, state-transition, and child-intent evidence. Never create claims during recovery.
- [ ] Confirm the concurrent candidate is terminally superseded without an intent and that unmarked/ambiguous state cannot be claimed.

### Task 4: Verify and commit

**Files:**
- Modify only files above

- [ ] Run: `python -m pytest tests/integration/test_coordinator_safety_hardening.py tests/contract/test_task_four_contracts.py tests/contract/test_task_four_review_scan_attempt_reference_integrity.py tests/contract/test_task_four_review_scan_attempts.py -q`
- [ ] Run: `python -m ruff check app tests migrations`, `python -m ruff format --check app tests migrations`, `python -m mypy app`, `python -m pytest -q`, and `git diff --check`.
- [ ] Commit the intentional schema, migration, coordinator, and regression changes with `fix: fence legacy recovery claims`.
