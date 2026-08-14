# Migration 141 — dedicated FBS shadow and physical-handoff evidence

## Goal and live boundary

Stage 7B moves the already enabled official FBS observation collector out of
the long hourly warehouse-functional job into the dedicated
`wb-core-fbs-shadow-collector.service/.timer`.  The timer runs every five
minutes (bounded scheduler jitter included) and targets freshness no worse
than ten minutes in normal state.  This is polling, not real time.

The only upstream calls remain `GET /api/v3/orders` and
`POST /api/v3/orders/status`; the POST is the documented official read
semantic.  The deployment and timer activation may append privacy-minimized
shadow observations and diagnostics.  They never reserve/debit stock, create a
facility/pool document, operation, balance or movement, accept a China
shipment, choose `T`, open/cut over an epoch, or write to WB.

## Bounded polling contract

- one non-blocking cross-process lock prevents overlapping collector cycles;
- the official FBS method family shares a crash-safe file-backed request
  budget at a conservative 220 ms minimum interval;
- one cycle covers the trailing seven days, at most 10 pages × 1000 orders,
  and persists each page before advancing its cursor;
- a crash or bounded-partial cycle resumes the exact pinned window/cursor;
- 429/5xx/transport retries use bounded exponential backoff, server hints and
  jitter; request, retry, rate/error and wait counters are append-only;
- duplicate/conflicting status rows fail closed, missing rows and unknown
  status vocabulary remain explicit diagnostic evidence.

Ordinary FBW/API sync and the hourly warehouse/cost writer no longer invoke
the FBS collector.  No parallel accounting writer is introduced.

## Exact transition evidence

The schema adds mutable current-episode state only as an index and immutable
append-only transition rows.  A transition binds the same official order ID,
previous/current order revision and status digest, exact previous/current
`supplierStatus + wbStatus`, episode sequence, local first/last-seen times and
deduplication digest.  The official status response has no source timestamp,
so `source_observed_at` remains explicitly empty and
`source_timestamp_available=false`; local observation time is never presented
as upstream event time.  Reappearance/reorder is counted without imposing an
invented lifecycle order.  Customer address/comment, raw response, token,
headers and other unknown fields are not stored.

## Query-only readiness and decision rule

`python3 apps/wb_fbs_shadow.py --runtime-dir <runtime> readiness` opens the
operational store with `mode=ro` and `PRAGMA query_only=ON`.  It reports
cadence/lag, recent error/backpressure, transition pairs, mappings and
unmatched/deferred evidence, aggregate FF and unopened pool-zero state, and
shipments awaiting factual FF acceptance.  Portal lanes are labelled only as
inference; the seller portal is not scraped and a static UI count is not API
trigger evidence.

The 2026-08-14 read-only control screenshot showed the business lanes
`Новые / На сборке / В доставке / Завершённые / Отменённые` and visible static
counts `397 / 0 / 1225 / not shown / 38`.  These values are not persisted,
reconciled as API truth or used to choose a transition; only the lane model
informs the explicitly labelled inference diagnostics.

`supplierStatus=complete` remains forbidden as a debit trigger.
`wbStatus=sorted` remains only a candidate.  Even three distinct exact
`complete/waiting → complete/sorted` transitions make the report eligible only
for an owner-gated design review; they do not select or activate a trigger.
Until that repeatable evidence and official semantics exist, readiness is
`NO_GO` and the safe next action is to keep this read-only collector running.

## Verification

- `python3 apps/wb_fbs_orders_collector_smoke.py`;
- `python3 apps/wb_fbs_orders_http_smoke.py`;
- `python3 apps/wb_fbs_shadow_polling_smoke.py`;
- `python3 apps/ff_stage_7a_production_smoke.py`;
- exact-SHA Release Train deploy, installed/enabled timer readback, several
  successful production cycles and query-only production readiness.
