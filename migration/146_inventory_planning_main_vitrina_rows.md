# Migration 146 — inventory planning rows in the main Web Vitrina

> Superseded presentation note (Migration 147, 2026-08-17): the incident-aware
> rows listed below remain part of the internal `inventory_planning_v1` read
> model and persisted audit/history, but are hidden from ordinary Web Vitrina
> rows/catalog/settings/picker. The active public subset is now only WB total,
> FBS total, dynamic active FBS facility and raw combined total.

## Correction boundary

Migration 145 introduced the query-only `inventory_planning_v1` endpoint and
warehouse card. This correction exposes the same truth as normal metric rows
in the main Web Vitrina table. It adds no formula, accounting operand,
warehouse stage, publication writer or business-data migration.

The current table contains one SKU/TOTAL pair for each label:

- `Остаток WB: всего`;
- `Остаток WB без инц.: всего`;
- `Остаток FBS: всего`;
- dynamic `Остаток FBS: <active facility>`;
- `Остаток без инц.: всего`;
- `Остаток: всего`.

The two familiar combined rows retain `wb_stock_effective_qty` and
`stock_total` identities as read-time presentation aliases. Their current cell
uses `inventory_planning_v1`; persisted ready snapshots, exact historical
values and factory/supply/regional consumers keep their prior semantics. An
exact-date request that does not contain the current WB snapshot date receives
no planning overlay.

## Values and quality

Per-SKU WB values come only from the current official aggregate snapshot.
Per-SKU and facility FBS values come only from exact facility × FBS physical
minus reserved ledger rows and remain signed. A missing physical row is N/A,
not zero. Official seller-warehouse stock remains reconciliation-only.

Missing exact current incident quantity evidence does not remove a metric row.
Both effective rows remain visible and every affected SKU/TOTAL cell renders
`Недоступно` with the canonical persisted-evidence reason. Aggregate-only WB
continues to leave current district rows unavailable and never substitutes an
old granular snapshot.

## Presentation migration

Existing version-4 metric settings record the planning SKU keys already seen.
Newly seen keys are appended once to saved presets and narrowed manual SKU
selection, so existing users see the rows without clearing localStorage or a
server profile. A later explicit hide/removal stays respected. A future active
facility is a newly seen key and therefore appears automatically; deactivation
removes only its current row. Every logical SKU/TOTAL pair has one label and
one independent shown/collapsed/hidden control.

The main page still loads table rows through its deferred, paginated/read-only
composition request; initial HTML receives no embedded inventory payload.

## Verification

- `apps/inventory_planning_read_model_smoke.py` covers exact per-SKU operands,
  signed negative available, missing physical evidence and seller
  reconciliation isolation.
- `apps/sheet_vitrina_v1_inventory_planning_smoke.py` covers main-contract
  labels, per-SKU/TOTAL formulas, no double count, missing incident N/A,
  activation/deactivation and exact-date immutability.
- `apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py` opens the main
  table and covers rendered values/N/A reason, existing-profile migration,
  independent hide/show, mobile dark layout and console health.
