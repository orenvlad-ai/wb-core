# Migration 137: official FBS orders read-only shadow

## Goal and rollout boundary

Stage 5 adds only a privacy-minimized observation cache for the official WB
FBS assembly-order feed. The sole upstream method is
`GET https://marketplace-api.wildberries.ru/api/v3/orders` with bounded
`limit`, advancing `next` cursor and an explicit period no wider than 30 days.
The collector never calls the status POST or any create, patch, put or delete
method. It remains disabled unless `WB_FBS_COLLECTOR_ENABLED` is explicitly
set; deployment does not set that owner-managed activation.

This stage does not assign an FBS order to an FF facility, infer an origin,
create a facility/feature epoch/document/operation/reservation/movement or
balance, seed/open/backfill/cut over inventory, switch a writer/reader or
change WB. Ordinary FBW sync remains successful independently if a later
enabled FBS collection attempt fails.

## Safe append-only evidence

Operational schema ensure creates only the empty append-only table
`sheet_vitrina_v1_wb_supplies_fbs_order_observations` and empty collector state
table. Each observation stores official order id, optional FBS supply id,
`deliveryType=fbs`, source creation time, warehouse/office/nmId/chrtId, bounded
SKU barcodes, cargo/cross-border/zero-order flags, a hash-only safe source
revision and exact collection window/cursor/time. Repeating the same
order/revision is T0; a changed safe revision appends history. Update/delete
triggers protect the observation journal.

The allowlist intentionally excludes customer address, comment, order UID,
RID, prices and raw JSON. Authorization, token, response body and unknown
fields are never persisted. The existing `sheet_vitrina_v1_wb_suppl*`
warehouse recovery prefix covers both Stage 5 tables; no new recovery tier or
full-store backup is needed.

## Process hook and protected reads

After a successful ordinary process-owned FBW incremental sync, the Stage 5
hook returns `collector_default_off` without opening the upstream or writing
rows unless the explicit environment gate is enabled. When enabled in a later
authorized stage, pages are fetched by GET only, cursor reuse fails closed,
work is capped at 50 × 1000 rows per attempt and incomplete work returns a
truthful next cursor. Collector failure is supplemental and cannot turn the
already successful FBW refresh into failure.

The existing protected supply-role warehouse prefix owns:

- `GET /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/fbs-orders` for a
  bounded current-order page and collector state;
- `GET .../fbs-orders/{order_id}` for current safe evidence and bounded
  append-only history.

Both routes open SQLite `mode=ro` with `query_only=ON`, use deterministic ETag
and never initialize schema, call WB or trigger collection. There is no Stage 5
POST route and no operator UI control.

## Verification and production acceptance

- `python3 apps/wb_fbs_orders_collector_smoke.py` proves the GET-only adapter,
  30-day/window/page/cursor bounds, default-off no-call/no-write behavior,
  privacy allowlist, T0/append-only history, immutable triggers/indexes and
  zero facility/epoch/assignment/document/operation/movement side effects;
- `python3 apps/wb_fbs_orders_http_smoke.py` proves cache-only protected route
  shape, pagination/filter/detail, conditional GET, absent mutation path and
  unchanged target/non-target counts.

Production closure is exact-SHA deploy plus query-only schema/count and
authenticated GET evidence. Expected observation and collector-state rows are
zero because deploy does not enable or invoke the collector. No production
form submit, non-GET request, upstream collection or data mutation is part of
acceptance. No UI changes means no Stage 5 screenshot is required.

## Explicit later scope

Collector activation/backfill, FBS order-origin assignment, shadow movement
writers/readers, facility seeds, historical opening/backfill/cutover and live
inventory activation remain separate stages and gates.
