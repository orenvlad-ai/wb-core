# Migration 152: FBS handoff cost and bounded overhead backfill

## Scope

This migration makes `Себестоимость наша` one channel/location-aware contract
for Vitrina, Finance weekly/per-SKU, Partner and Proxy. It also provides the
repo-owned dry-run/apply/readback runner for the five already-posted
facility/FBS overhead documents dated `2026-08-21`. It does not introduce a
second ledger, allocator, sales history or Finance raw source.

## Exact FBS event semantics

An FBS lifecycle debit freezes the positive current WAC of exact
`facility_id + FBS + nmId` inside its serialized transaction. Ordering uses the
durable operation row/revision, not a day bucket or an ambiguous equal
timestamp. A debit committed before overhead retains its old WAC; a debit after
the overhead commit receives the new WAC. Later overhead never rewrites a
fulfilled order. A reversal that would invalidate a later immutable debit fails
closed.

The resolver requires privacy-safe WB order identity, exact facility mapping,
matching lifecycle debit and positive `frozen_wac_rub`. Missing or ambiguous
evidence returns a reason code. It cannot use another facility/SKU, FBO/WB
daily cost, an average, opening/cutover allocation, legacy fallback or zero.
FBO/WB continue to use canonical daily WAC.

## Profit coverage

Full sales/revenue facts remain visible. Uncovered sales enter neither a profit
numerator nor the profitability-revenue denominator. Profit and margin are
marked partial and use only covered revenue plus covered signed COGS. Every
consumer carries reason/coverage evidence. Vitrina adds
`Продажи без себестоимости, ₽`; Partner adds that row plus
`Заказы без себестоимости, шт.` and evidence-backed units. Warehouse UI shows a
bright unresolved-cost amount/count warning and opens the exact
`cost_unresolved` list; green means a proven zero only. Identifiers exposed by
this surface are hashed and contain no customer PII.

## Overhead publication and contention

`pool_overhead` confirm atomically writes its document/ledger effects, updates
affected current aggregate capital/WAC without quantity change and inserts one
deterministic queue revision. HTTP returns the durable posted/queued readback;
Warehouse, economics and Finance complete asynchronously with independent
status/error fields and exact fingerprints. Completion proves current detail
and aggregate projections, quantity invariance, capital conservation,
idempotency and no duplicate submit.

Official hourly capture remains hourly. A separate job lock preserves
hourly/manual single-flight, while the common warehouse writer lock is held only
by short canonical apply/readback sections. Heavy capture, plan/digest,
economics and Finance recomputation run outside it. Finance builds exact target
after-images from a query-only projection, then uses a short
`PRAGMA data_version` CAS. Phase/lock timing and
contention regressions prove interactive FF document/status writes remain
available while heavy planning is paused.

## Production backfill contract

`ff-pool-overhead-backfill-dry-run|apply|readback` accepts no document IDs from
the prompt. Query-only planning resolves current server-owned identities and
requires exactly four Moscow/FBS documents totalling `115206.50 RUB` and one
Orenburg/FBS document of `60000.00 RUB`, all dated `2026-08-21`. Any document,
facility, pool, SKU, amount, status, reversal or current-state ambiguity blocks
the plan.

The external mode-`0600` manifest binds exact deployed SHA; documents,
operations, facilities, pools, SKUs and event revisions; current detail and
aggregate projections; pre-change digests; a coherent backup; affected and
non-target rows; quantity/capital/document/lifecycle invariants; deterministic
queue identities; idempotency and recovery. Apply requires the exact reviewed
fingerprint and a separate post-merge apply authorization. Its short mutation
updates only current aggregate FF capital/WAC/provenance and exact queue rows.
It never replays a business document or changes historical fulfilled sales.
Warehouse/economics/Finance publication and query-only reconciliation follow;
an exact repeat is a no-op and drift never triggers a second submit.

## UI/parser cleanup

Ordinary planning no longer publishes the incident-aware cards/controls
`Остаток WB без инц.: всего` and `Остаток без инц.: всего`; audit evidence and
legacy disclosure remain. Manual overhead renders neutral `ручной ввод` and no
empty PDF parser block. VTB parser v2 safely removes recognized right-side form
controls before beneficiary extraction; ambiguity remains `needs_review` and
the regression fixture is wholly synthetic.

## Verification

Focused smokes cover exact before/after handoff WAC, equal-time deterministic
order, missing mapping/WAC, cross-facility and WB/FBO non-target state, past-sale
immutability, overhead quantity/capital conservation, queue dedupe/reversal/
recovery and Finance acknowledgement, partial profit coverage across all four
consumers, Vitrina/Partner metrics, warehouse warning/filter, removed incident
controls, neutral manual evidence and synthetic VTB inline controls. The
warehouse/Finance contention regression proves heavy work occurs outside the
common writer section and a concurrent interactive writer commits before the
stale Finance CAS aborts.
