---
title: "Модуль: our_wb_cost_model"
doc_id: "WB-CORE-MODULE-40-OUR-WB-COST-MODEL-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для управленческой proxy-модели нашей себестоимости WB, SKU-level FF cost layers, transit classifier, opening baseline, rolling weighted average state and proxy profit 3."
scope: "Server-owned runtime contour inside wb-core. This is management proxy cost, not strict accounting FIFO/cost truth. It uses supplier shipments, supplier financial documents/CNY ledger fields, WB supplies, accepted Fulfillment service uploads and ready snapshots; it does not replace 1C/proxy profit 2 and does not use Google Sheets/GAS/browser/localStorage as truth."
source_basis:
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
related_modules:
  - "packages/application/our_wb_costs.py"
  - "packages/application/sheet_vitrina_v1_our_wb_costs.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_supplier.html"
related_tables:
  - "sheet_vitrina_v1_supplier_ff_cost_layers"
  - "sheet_vitrina_v1_supplier_ff_cost_layer_lines"
  - "sheet_vitrina_v1_wb_supply_cost_layers"
  - "sheet_vitrina_v1_wb_opening_baseline"
  - "sheet_vitrina_v1_wb_cost_daily_state"
related_endpoints:
  - "POST /v1/sheet-vitrina-v1/wb-cost/recalculate"
  - "GET /v1/sheet-vitrina-v1/wb-cost/status"
related_runners:
  - "apps/our_wb_costs_smoke.py"
related_docs:
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Introduces bounded management proxy WB cost model with explicit source/component statuses, direct-zero transit handling, opening baseline 2026-07-01, rolling weighted average state, and proxy profit 3 metrics. It is not strict accounting FIFO and does not replace proxy profit 2."
---

# 1. Contract

The contour materializes our management proxy cost in runtime SQLite. It is designed to stabilize vitrina economics and `proxy_profit_3_rub`; it is not legal/accounting cost truth, not FIFO, and not physical supplier-lot-to-WB-stock trace.

All rows must keep explicit status/evidence:
- supplier FF layer status: `confirmed`, `estimated`, `pending_expenses`, `needs_review`;
- transit status: `direct_zero_confirmed`, `transit_confirmed`, `transit_missing`, `unknown_route`;
- WB/opening/daily source status: `confirmed`, `estimated`, `fallback`, `pending`, `needs_review`, plus opening source names.

No missing component may be hidden as confirmed zero. The exception is confirmed direct WB transit: if route has no transit marker/evidence and official/detail cost is zero, transit cost is `0` with `direct_zero_confirmed`.

# 2. Supplier FF Cost Layers

`accepted_ff` is not manually selectable. It is set when `actual_ff_acceptance_date` (`Фактическая дата приёмки на ФФ`) is saved on the supplier shipment. `actual_shipment_date` never triggers `accepted_ff`.

On trigger, an idempotent current version is materialized:
- `sheet_vitrina_v1_supplier_ff_cost_layers`;
- `sheet_vitrina_v1_supplier_ff_cost_layer_lines`.

MVP formula:

```text
effective_cny_rate = cny_payment_currency_rub_cost / invoice_amount_total_cny
sku_purchase_cost_rub = invoice_unit_price_cny * effective_cny_rate
common_expense_pool_rub =
  invoice_extras_cny * effective_cny_rate
  + cny_bank_fee_rub
  + logistics_invoice_rub
  + customs_declaration_rub
allocated_common_expenses_per_unit_rub = common_expense_pool_rub / total_product_qty
sku_ff_unit_cost_rub = sku_purchase_cost_rub + allocated_common_expenses_per_unit_rub
```

Allocation method is `qty_based_common_pool`. This is acceptable for glass SKUs as a management proxy because items are physically/logistically close.

Reconciliation check:

```text
SUM(sku_ff_unit_cost_rub * qty) / SUM(qty)
```

must match the order-average landed FF cost within rounding. For the known opening shipment `sup_905...`, expected average is about `111.181389 ₽/шт`.

If a current layer exists, accidental clearing/changing `actual_ff_acceptance_date` is blocked in MVP. Safe correction/versioning can be added later through a controlled admin flow.

# 3. Transit Classifier

Transit classification is centralized in `packages/application/our_wb_costs.py`.

Rules:
- `direct_zero_confirmed`: no transit marker/warehouse evidence, route is known, and official/detail/effective cost evidence is zero. Cost per unit is `0`.
- `transit_confirmed`: transit marker/evidence exists and official or Seller Portal cost exists.
- `transit_missing`: transit marker/evidence exists but cost amount is missing.
- `unknown_route`: route cannot be safely classified.

Direct supplies such as `40431461` must be `direct_zero_confirmed`, not `transit_missing`. Code must not use `NULLIF(cost_total, 0)` in a way that converts confirmed zero into missing.

# 4. WB Supply Cost Layers

`sheet_vitrina_v1_wb_supply_cost_layers` stores versioned current cost rows by `wb_supply_id + nm_id`.

Formula:

```text
our_wb_unit_cost_rub =
  sku_ff_unit_cost_rub
  + transit_per_unit_rub
  + ff_services_per_unit_rub
  + ff_storage_per_unit_rub
```

Fulfillment service components come only from accepted non-deleted uploads. `STORAGE` enters only through allocated storage amount on ordinary WB supply rows. Failed/deleted/unmatched/duplicate uploads are excluded.

Rows with incomplete components stay `pending`/`estimated`/`needs_review`; they are not counted as confirmed inbound.

Quantity evidence rule:
- confirmed WB cost rows require final accepted quantity from `acceptedQuantity` / `accepted_quantity` and accepted supply status `5` (`Принято`);
- `quantity` / `qty` is planned evidence only and may materialize an `estimated` row, never `confirmed`;
- partial receiving/open statuses such as `4` (`Идёт приёмка`) remain `estimated` even if WB already reports a non-zero accepted quantity;
- outbound/gate status `6` (`Отгружено на воротах`) is not receiving-complete evidence and cannot create confirmed cost from planned quantity.

Every current confirmed row must satisfy `our_wb_unit_cost_rub >= sku_ff_unit_cost_rub`; direct-zero transit remains a confirmed zero component only when the direct-route classifier has explicit zero evidence.

Rolling daily state groups WB supply cost layers by normalized supply business date (`YYYY-MM-DD`). WB/operator evidence may store `supply_date` as an ISO timestamp such as `2026-07-03T00:00:00+03:00`; the rolling key keeps the local date part (`2026-07-03`) and does not timezone-shift it. Empty or invalid dates are skipped instead of crashing materialization. If accepted WB inbound evidence arrives on a day where stock is still zero, rolling may carry the inbound bucket inside the recalculation and apply it when stock appears later; persisted daily buckets remain capped to current stock so confirmed share cannot exceed 100%.

# 5. Opening Baseline And Rolling State

Opening date is `2026-07-01`.

Opening priority chain:
1. `opening_confirmed_supply`: SKU exists in confirmed accepted-FF opening supplier shipment around 2026-06-21..2026-06-24 and has SKU FF cost.
2. `near_future_proxy`: nearest future known-cost shipment may be used only as marked future-informed estimate.
3. `metric11_2026_07_01_fallback`: frozen `onec_WB_STOCK_unit_cost_rub` / `WB: себестоимость за ед., руб` from ready snapshot on `2026-07-01`.
4. `needs_review`: no known cost and no fallback.

`sheet_vitrina_v1_wb_opening_baseline` stores source priority, component JSON, opening stock qty and bucket quantities.

MVP washout is rolling weighted average, not FIFO:

```text
new_unit_cost =
  (previous_stock_qty * previous_unit_cost + inbound_qty * inbound_unit_cost)
  / (previous_stock_qty + inbound_qty)
```

Stock reductions keep unit cost stable and scale confirmed/estimated/fallback buckets proportionally. `sheet_vitrina_v1_wb_cost_daily_state` stores daily state by date/SKU.

# 6. Vitrina Metrics

Runtime-extended user-facing metrics:
- `our_wb_unit_cost_rub`, label `Себестоимость WB наша, ₽/шт`; TOTAL key `total_our_wb_unit_cost_rub` is weighted by stock qty.
- `our_wb_cost_confirmed_share_pct`, label `Доля подтверждённой себестоимости, %`; TOTAL key `total_our_wb_cost_confirmed_share_pct` is `SUM(confirmed_qty) / SUM(stock_qty)`.
- `proxy_profit_3_rub`, label `proxy прибыль 3`; TOTAL key `total_proxy_profit_3_rub` is sum of SKU rows.

Date boundary:
- before `2026-07-01`, `proxy_profit_3_rub = proxy_profit_2_rub`;
- from `2026-07-01`, formula is `orderSum * 0.5096 - orderCount * 0.91 * our_wb_unit_cost_rub - ads_sum`.

`proxy_profit_2_rub` remains visible and unchanged.

The same runtime metric extension is used by the DATA snapshot builder and the web-vitrina read contract. Operator/public UI must show Russian labels for SKU and TOTAL rows, and `Доля подтверждённой себестоимости, %` is formatted as a percent (for example `0.727918` renders as about `72,79%`).

# 7. Routes And Backfill

Protected routes:
- `POST /v1/sheet-vitrina-v1/wb-cost/recalculate`: idempotent rebuild of accepted-FF supplier layers, WB supply cost layers, opening baseline and daily state.
- `GET /v1/sheet-vitrina-v1/wb-cost/status`: read status counts and latest TOTAL diagnostics.

These routes do not sync/enrich WB data and do not upload/delete files. They only materialize/recompute the management cost contour from existing runtime truth.

The ordinary web-vitrina refresh path persists the freshly built ready snapshot, runs this idempotent recalculation from runtime truth, and rebuilds/saves the ready snapshot again only when recalculation changed supplier/WB/opening/daily cost state. This keeps cost metrics updated with normal refresh/auto-refresh without coupling them to WB sync/enrich/upload jobs and without recursive refresh loops.

Backfill is performed by the same safe routines: rebuild cost state from existing runtime truth, then rebuild ready snapshots for selected historical dates. `proxy_profit_3_rub` is expected for the whole available analysis period wherever `proxy_profit_2_rub` is available; `our_wb_unit_cost_rub` and confirmed share start from `2026-07-01` where closed-day stock/input state exists.

# 8. Explicit Non-Goals

Do not add in this contour:
- strict accounting FIFO;
- physical box/lot tracing;
- replacement of proxy profit 2;
- товарный капитал / капитал в остатках;
- Google Sheets/GAS or browser/localStorage truth;
- hidden fallback values presented as confirmed.
