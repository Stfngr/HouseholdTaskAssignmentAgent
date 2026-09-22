# Architecture

## Module Boundaries

- `main.py`: CLI, process lifecycle, scheduling, orchestration, and daily transaction ordering.
- `household_agent/allocation.py`: pure weekly/monthly planner. Inputs are validated snapshots, state, date, and supplied `random.Random`; it performs no I/O and does not mutate inputs.
- `household_agent/config.py`: strict JSON configuration and environment loading.
- `household_agent/state.py`: state schema validation, migration, reconciliation, locking, and atomic persistence.
- `household_agent/telegram.py`: German message text, Telegram adapter, outbox delivery, and retry behavior.
- `household_agent/dashboard.py`: durable dashboard snapshot construction and delivery.

## Behavioral Invariants

- Preserve idempotent daily booking: one local day produces at most one committed allocation, including days with no tasks.
- Persist assignments, statistics, and queued daily Telegram message in one state transaction before delivery. Delivery failure must not roll back a committed allocation.
- Deliver pending monthly reports before allocating a new day in a later month.
- Preserve outbox ordering and retry state. Acknowledged message parts must not be resent normally.
- Keep state writes atomic and guarded by the process lock. Reject corrupt or unknown-schema state rather than replacing it.
- Current state schema is `2`; schema `1` migrates atomically. Do not add migrations without persisted-data need.
- Assignment history is immutable operational evidence. Do not rewrite prior assignments when configuration changes.

## Planning

- Weekly periods start Monday; monthly periods use local calendar months.
- Respect roles, fixed off days, and period cooldowns. Children never receive adult-only tasks.
- Minimize overflow before fairness and daily concentration; use supplied RNG only for equivalent tie breaks.
- Keep results deterministic for identical snapshot, state, date, and RNG seed.
- Preserve ability to repair placements across tasks, roles, and periods before accepting avoidable overflow.
