---
title: "Модуль: backend-health Веб Витрины"
doc_id: "WB-CORE-MODULE-59-WEB-VITRINA-HEALTH"
doc_type: "module"
status: "active"
purpose: "Разделить закрытие вчерашнего дня, текущую актуальность и BOT/session/collector health на server-owned evidence."
scope: "Expectation matrix, append-only telemetry, 30-day bot gap detection, compact operator indicators/details, exact idempotent recovery preview/start/readback and permanent 06:30/07:30 Asia/Yekaterinburg contour."
source_basis:
  - "packages/application/sheet_vitrina_v1_health.py"
  - "packages/application/sheet_vitrina_v1_source_groups.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "apps/sheet_vitrina_v1_health_tick.py"
  - "apps/sheet_vitrina_v1_health_browser_smoke.py"
related_runners:
  - "apps/sheet_vitrina_v1_health_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Server-owned health projection plus compact operator UI and exact recovery seam; closed-history repair evidence is overlaid after any daytime publication and has no ordinary refresh hook."
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

The evaluator also reads the current ready snapshot's typed
`functional_economics_historical_repair_required` registry. Any closed-date
numeric-to-missing/coverage regression or exact warehouse/cost evidence mismatch
adds an independent failing `warehouse_functional_history` expectation. This
overlay is evaluated on every health read, so a daytime ordinary publication
cannot remain green merely because the 06:30/07:30 pair predates the regression.
The registry retains exact affected dates/SKU/families/reasons while the Vitrina
continues to show preserved last-good cells.

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

Closed warehouse economics is deliberately not assigned a group-refresh hook.
Its recovery preview is `hook=none`: only a separately reviewed version-bound
historical reconciliation may change the closed target cells.

Recovery submissions append an idempotency receipt to
`sheet_vitrina_v1_health_recovery_receipts`. The action fingerprint is bound to
the exact observation, plan fingerprint, business date, source group and
existing `refresh_group` job. A repeat returns that same job; a changed/stale
plan, mismatched identity, missing transient status after restart or active
Vitrina writer fails closed with `409` and never creates a second submit. The
job reuses the existing group-refresh writer and job polling. On terminal it
persists a new `phase=recovery` observation; technical job success does not
promote a still-incomplete semantic signal.

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
- Both services pass an explicit 1500-second application polling deadline under
  a distinct 1800-second systemd hard timeout. The runner flushes a launch
  receipt as soon as the canonical job identity is known and always emits a
  terminal receipt before its outer service deadline. A poll deadline, failed
  job or active single-flight is persisted as an append-only non-green
  candidate/confirmation observation with its own fingerprint; it is never
  promoted from a last-good snapshot.
- Confirmation does not spend another polling window on the candidate's still
  active single-flight and does not start a second refresh/recovery job. It
  persists the truthful incomplete confirmation instead, so the same-day pair,
  transitions and operator details remain observable without duplicate work.
- The live promo collector never reads a response body inside Playwright's
  synchronous response callback. The callback records only the timeline
  manifest URL; after hydration, one authenticated read-only replay with a
  10-second request timeout materializes the same manifest. This bounds the
  proven callback deadlock while preserving the current collector semantics.

The existing runtime-managed `10:00, 13:00, 16:00, 19:00, 22:00
Asia/Yekaterinburg` interval slots and the 15-minute temporal closure state
machine remain unchanged. Manual/full/auto/group refresh, Finance
`dual_day_intraday_tolerant`, stocks yesterday-only, inventory history, FBS,
formulas, metric values, load controls, filters and user configuration keep
their existing contracts.

# 4. Operator surface

Protected `GET /v1/sheet-vitrina-v1/web-vitrina/health` is the only
operator-facing projection. The same object is embedded into page composition;
the browser never recalculates source health. The compact table-header cluster
contains independent `Вчера`, `Сегодня` and `BOT` indicators. Before the first
real candidate plus confirmation pair for the business date, all three remain
neutral `Наблюдаем`. After the pair, `Вчера` and `BOT` follow their independent
server signals. `Сегодня` stays neutral until the server-owned first daytime
expectation boundary at `10:00 Asia/Yekaterinburg`, then follows the persisted
current-day signal.

Click or keyboard focus opens one accessible compact dialog with business
dates, latest observation/phase, server-provided source groups/keys,
`complete/missing/partial/inapplicable/no_events/accepted_fallback` explanations,
recent transitions and the next 06:30/07:30 actions. Raw JSON, trace text,
credentials and storage-state contents are not rendered.

Protected `POST /v1/sheet-vitrina-v1/web-vitrina/health/recovery/start` accepts
only the exact current observation/plan/action/group/date identity shown by the
read surface. A runnable button exists only for `hook=group_refresh` with both
`apply_allowed=true` and `operator_apply_allowed=true`, and always opens a
confirmation first. `hook=none`, including historical `spp`/`spp_proxy` gaps,
is preview-only and explains that automatic historical recovery is unavailable
and a new current observation is required. Login/relogin remains exclusively in
`Настройки → Источники и сессии`; no credential/session control is added to the
Vitrina header. After a terminal job, the UI rereads both health and page
composition and displays the new semantic result.

Runner failures appear as the synthetic server-owned `Утренний контур` source
group with a safe reason and cycle execution metadata (`phase`, failure code,
job identity/status and single-flight flag). They do not create a runnable
recovery action and cannot render `Вчера` or `BOT` green.

Closed warehouse-economics drift appears as the separate
`История складских метрик` group. Its safe explanation lists the affected date
range and preserves an explicit non-green `Вчера` signal at any time of day;
the operator surface never offers full/group refresh as repair.
