# Migration 120 — Autoanswers operator limit controls

## Preserved data and safety boundaries

This release adds no database schema and performs no production-data rewrite.
The seven global limits remain in the singleton schema-v7 Autoanswers settings
row, so current values survive restart and deploy. Existing usage, cost events,
reservations, uncertainty holds, policy epoch, transition-run identity,
immutable membership and owner-confirmed run cap remain unchanged.

The operator-editable server contract is:

| Field | Valid range |
| --- | ---: |
| `hourly_cap_usd` | `$0.01..$10.00` |
| `daily_cap_usd` | `$0.01..$50.00` |
| `monthly_cap_usd` | `$0.01..$500.00` |
| `max_paid_reviews_per_hour` | `1..200` |
| `global_paid_review_concurrency` | `1..4` |
| `max_inflight_role_calls` | `1..8` |
| `max_materialized_processing_jobs` | `1..100` |

All values are finite and positive. The server additionally requires
`hourly_cap_usd <= daily_cap_usd <= monthly_cap_usd`,
`global_paid_review_concurrency <= max_materialized_processing_jobs` and
`max_inflight_role_calls <= max_materialized_processing_jobs`.

## Optimistic write and readback

Settings GET returns the server-owned bounds and a SHA-256 revision of the
complete persisted settings projection. A limit update is admin-only and must
present both the current `policy_epoch` and exact settings revision. The
repository validates the full resulting combination, commits it atomically,
then reads it again. The response returns the new revision and exact
`confirmed_limits`; the UI displays success only when every requested value
matches. A concurrent policy or settings change fails closed with a Russian
refresh-and-retry message.

The modal is the single edit surface. It is opened from the visible
`Настроить лимиты` action, the legacy technical link or the contextual
`Увеличить лимит` action for hourly/daily/monthly budget pauses. It displays
current usage and reservations, the current/new values and the active run cap.
The run cap is read-only: changing globals does not silently expand the
owner-confirmed run boundary.

## Runtime behavior and rollback

Worker claims already re-evaluate persisted global limits on every ordinary
tick. Increasing the sole exhausted hourly, daily or monthly global budget
therefore allows the same transition run to continue on the next tick without
a mode toggle or replacement run. Lowering a limit below accumulated usage
retains that usage and keeps the pause. Another gate, run cap,
`budget_state_unknown`, provider quota, hold or reservation remains effective.

Rollback is the previous application SHA. Because there is no schema or data
migration, the persisted settings remain readable by the prior release.
Rollback does not restore or guess a prior limit value and must not clear
usage, reservations, holds or stop evidence.
