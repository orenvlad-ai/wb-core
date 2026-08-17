# Migration 145 — versioned planning inventory and FBS order read surfaces

## Boundary

This migration adds a current, query-only planning/read model. It does not add
a warehouse stage, change the canonical six-stage quantity/capital `TOTAL`,
repeat FF/FBS quantity in accounting totals, or switch factory-order, FBS/WB
supply, regional recommendation or China routing consumers. It performs no WB
write and no manual inventory/accounting mutation.

The current formula epoch is pinned to the latest applied facility/pool cutover
business date and feature epoch. Earlier Vitrina/history rows remain historical
evidence and keep their prior semantics. The current labels and formulas are:

- `Остаток WB: всего` = current official seller/account WB aggregate;
- `Остаток WB без инц.: всего` = WB aggregate minus exact persisted active
  incident quantity by SKU;
- `Остаток FBS: всего` = sum of signed `physical − reserved` across active FBS
  facilities;
- one dynamic `Остаток FBS: <склад>` for every active FBS facility;
- `Остаток без инц.: всего` = effective WB plus FBS available;
- `Остаток: всего` = raw WB plus FBS available.

Facility deactivation removes it only from the current total. It does not edit
old balances, audit events, formula epochs or historical presentation.

## Evidence and quality

The exact PR #979 sentinel remains
`(-999999, "Склад WB", "Склад WB")`. It certifies a fresh WB aggregate only;
district/current regional and warehouse incident allocation remains
`Недоступно: WB временно не передаёт распределение`, never zero. Historical
district values are retained.

Two additive, append-only evidence families are created empty:

- `sheet_vitrina_v1_wb_incident_quantity_evidence[_lines]` binds an exact
  incident registry quantity to seller, policy revision, WB snapshot id/digest,
  business date and the full current SKU-scope digest. Every SKU in that scope
  must have one persisted quantity line (including explicit zero), and the
  canonical manifest digest must match the complete ordered line set. An active
  incident with no exact matching manifest makes the effective WB and combined
  effective metric unavailable. There is no stale granular reconstruction,
  implicit zero or synthetic cap.
- `sheet_vitrina_v1_fbs_seller_stock_readbacks[_lines]` stores timestamped
  official seller-warehouse reconciliation evidence. Readback lines reach a
  facility only through an exact active `sellerWarehouseId → facility_id`
  mapping. Multiple active target facilities fail closed as ambiguous. Names
  are never guessed. The value and delta are reconciliation
  diagnostics and never replace ledger physical quantity.

Production deployment only creates the empty contracts. Populating incident,
facility, mapping, cutover or stock-readback business rows remains outside this
migration and requires its own applicable gate.

## Read surfaces

`GET /v1/sheet-vitrina-v1/warehouses/planning-inventory` opens the canonical
SQLite store in `mode=ro` with `PRAGMA query_only=ON`. It composes the active WB
snapshot with the current facility×FBS ledger. FBS `available` is signed;
negative values are preserved, and a missing physical ledger row for any
active facility makes the current FBS totals unavailable rather than zero.
Response provenance includes the WB snapshot,
formula/cutover epoch, facility freshness, incident evidence state and seller
readback timestamp. It contains neither raw WB payload nor PII.

The ordinary warehouse publisher performs a query-only planning readback after
its canonical functional publication. That proves a fresh official WB
aggregate and current live facility×pool FF can be displayed together without
an ad-hoc projection write. Failure still retains the last good publication;
source-contract/invariant reasons take precedence over a diagnostic
`sqlite_busy_timeout_ms` suffix.

The existing FBS order endpoints are upgraded to a server-paginated cache UI:

- list filters: dates, status category, `supplierStatus`, `wbStatus`, SKU,
  facility and search;
- counters: total, active pre-handoff, exact `complete + sorted` handoff,
  sold/closed, canceled, reconciliation/unknown-return, unmatched, deferred and
  ambiguous;
- list/detail evidence: safe order identity, exact facility/SKU mapping,
  reservation, debit/close events, transition timestamps/digests and lifecycle
  reason.

Address, comment, order UID/RID, raw payload and token fields are neither
stored by this surface nor returned. GET remains query-only and exposes no WB
or accounting mutation control. Facility detail links into the same list with
an exact `facility_id` filter.

## Verification

- `apps/inventory_planning_read_model_smoke.py` covers formulas/no double
  count, missing/exact incident evidence, signed available, dynamic facilities,
  seller reconciliation, sentinel/district unavailability, formula boundary
  and unchanged six stages.
- `apps/wb_fbs_orders_http_smoke.py` covers protected HTTP filters/counters,
  pagination, mapping/status evidence, ETag, query-only invariants and no PII.
- `apps/ff_pool_surfaces_smoke.py` and
  `apps/ff_pool_surfaces_browser_smoke.py` cover facility/pool presentation,
  contextual order entry, mobile UI, lazy reads and browser/console health.
- `apps/ff_pool_cutover_recovery_supersession_smoke.py` covers primary error
  reason precedence over SQLite timeout diagnostics.
