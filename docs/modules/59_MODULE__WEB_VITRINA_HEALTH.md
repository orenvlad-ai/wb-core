---
title: "Модуль: backend-health Веб Витрины"
doc_id: "WB-CORE-MODULE-59-WEB-VITRINA-HEALTH"
doc_type: "module"
status: "active"
purpose: "Разделить закрытие вчерашнего дня, текущую актуальность и BOT/session/collector health на server-owned evidence."
scope: "Expectation matrix, append-only shadow telemetry, 30-day bot gap detection, recovery preview/hooks and permanent 06:30/07:30 Asia/Yekaterinburg contour; no user-facing UI."
source_basis:
  - "packages/application/sheet_vitrina_v1_health.py"
  - "packages/application/sheet_vitrina_v1_source_groups.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "apps/sheet_vitrina_v1_health_tick.py"
related_runners:
  - "apps/sheet_vitrina_v1_health_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Backend-only health foundation; no metric/formula/history rewrite and no performance work."
---

# 1. Expectation and signal contract

Every active persisted source is evaluated for both `yesterday_closed` and
`today_current` using its canonical temporal policy and source group. A STATUS
row is evidence, not success by itself. The evaluator keeps `missing`,
`partial`, `failure`, `inapplicable`, `exact_zero`, `no_events`,
`accepted_fallback` and exact `complete` distinct. Complete requires exact
target-date identity and required coverage; partial is never promoted to full.

The three top-level signals are independent:

- `yesterday_closed` reports closed-day coverage only;
- `today_current` reports same-day currency only;
- `bot_health` becomes non-OK only for a confirmed session/route/collector or
  current data-arrival problem in `seller_portal_bot` / `wb_public_card_bot`.
  Generic auth state, WB Buyer challenge state and lawful empty event sources
  do not lower this signal.

`sku_action_events` is an event-scope source. When its lookup completes with no
confirmed price/bid change, the scope is covered and the semantic state is
`no_events`; a real lookup error remains an error.

# 2. Durable shadow truth and recovery seam

Every candidate, confirmation or explicit shadow evaluation appends one
date-bound fingerprinted row to
`sheet_vitrina_v1_health_observations`. Signal changes append independent rows
to `sheet_vitrina_v1_health_transitions`. Both tables reject UPDATE and DELETE;
repeating the same date/phase/fingerprint is an idempotent no-op.

The evaluator scans the latest 30 business dates of accepted temporal slot
observations for bot-backed source/date/role gaps. Completely missing evidence
is `skipped`; nonterminal/short coverage is `incomplete`. The deterministic
recovery preview groups exact gaps and exposes only existing single-flight
group-refresh hooks for historical-capable sources. Current-only rollover
sources remain preview-only for a historical gap; no value is invented and no
mass historical apply occurs.

# 3. Permanent morning contour

- `wb-core-sheet-vitrina-health-candidate.timer` runs at `06:30
  Asia/Yekaterinburg`. It starts one canonical full auto-update through the
  existing single-flight job and unified ready/history writer, but deliberately
  does not consume or mutate the runtime schedule ledger.
- `wb-core-sheet-vitrina-health-confirmation.timer` runs at `07:30
  Asia/Yekaterinburg`. It first evaluates closure and launches at most three
  deterministic source-group recovery hooks only when yesterday is not closed.
  It is never a second blind full refresh.
- Both timers are non-persistent, so deployment does not replay an old night
  experiment or stale absolute slot.

The existing runtime-managed `10:00, 13:00, 16:00, 19:00, 22:00
Asia/Yekaterinburg` interval slots and the 15-minute temporal closure state
machine remain unchanged. Manual/full/auto/group refresh, Finance
`dual_day_intraday_tolerant`, stocks yesterday-only, inventory history, FBS,
formulas, metric values and user-facing lamps/BOT badge/recovery controls keep
their existing contracts.
