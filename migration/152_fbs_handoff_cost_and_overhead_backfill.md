# Migration 152: FBS handoff cost and bounded overhead backfill

## Scope

This migration introduced the channel/location-aware realized-cost contract
for Finance weekly/per-SKU and Partner and initially projected it through the
then-current Vitrina/Proxy rows. Migration 153 supersedes only that latter
informational consumer choice with a forward-only WB+FF inventory blend; the
exact FBS/WB sale COGS contract here remains authoritative. It also provides the
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
`Заказы без себестоимости, шт.` and evidence-backed units. Warehouse UI keeps
that published Finance amount/order/unit/week scope separate from the current
FBS lifecycle unresolved-order scope. The latter follows the selected order
date/filter set, carries status/reason evidence and alone opens the exact
`cost_unresolved` list. Neither counter is used as the denominator or label for
the other, and each scope is green only for its own proven zero. Lifecycle
cursor/source sequence, lag and pending identity evidence are also published
separately from collector poll success. Identifiers exposed by this surface are
hashed and contain no customer PII.

Migration 157 additionally splits a separately gated historical lifecycle
suffix from continuous ingress. Its query-only manifest pins stable rows through
`C`; an explicit apply starts a durable forward generation at `C+1` and reuses
the exact lifecycle cost/debit implementation for only the reviewed `<=C`
identities. New rows above `C` cannot change that fingerprint, while target WAC
or business-evidence drift fails closed. This mechanism does not rewrite any
fulfilled sale or realized Finance/Partner row.

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
after-images from a query-only projection, normalizes the target image and uses
the short snapshot-handoff/target CAS specified by migrations 153 and 154;
unrelated SQLite commits during long planning are not source drift. Phase/lock timing and
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
fingerprint and a separate post-merge apply authorization. Planning classifies
every aggregate SKU as either `selected_capital_pending`, `already_current` or
ambiguous using exact `Decimal` arithmetic and the canonical `ROUND_HALF_UP`
kopeck parity boundary; textual scale and equal-kopeck tails are evidence, not a
second expense. Only `selected_capital_pending` rows may update current
aggregate FF capital/WAC/provenance. When facility detail is already reflected,
aggregate rewrite count and capital delta are exactly zero and only missing
canonical queue/status/economics/Finance identities are reconciled. Completed
identities are not recomputed. The runner never replays a business document or
changes historical fulfilled sales. A fresh dry-run/readback after completion
is an explicit zero-write no-op; drift never triggers a second submit.

## UI/parser cleanup

Ordinary planning no longer publishes the incident-aware cards/controls
`Остаток WB без инц.: всего` and `Остаток без инц.: всего`; audit evidence and
legacy disclosure remain. The warehouse presentation filters their two exact
metric identities after the query-only payload is received, so retained audit
values cannot reappear as a control or card. Manual overhead renders neutral
`ручной ввод` and no empty PDF parser block. VTB parser v2 safely removes
recognized right-side form controls before beneficiary extraction; ambiguity
remains `needs_review` and the regression fixture is wholly synthetic.

## Verification

Focused smokes cover exact before/after handoff WAC, equal-time deterministic
order, missing mapping/WAC, cross-facility and WB/FBO non-target state, past-sale
immutability, overhead quantity/capital conservation, queue dedupe/reversal/
recovery and Finance acknowledgement, partial profit coverage across all four
consumers, Vitrina/Partner metrics, warehouse warning/filter, removed incident
controls, neutral manual evidence and synthetic VTB inline controls. The
warehouse/Finance contention regression proves heavy work occurs outside the
common writer section, an unrelated interactive writer commits without
invalidating Finance, and actual canonical-cost drift still aborts before
target replacement.
