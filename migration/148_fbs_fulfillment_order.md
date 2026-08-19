# Migration 148 — independent own-FBS fulfillment order

Date: 2026-08-19
Contour: Supply planning live/runtime; no production business-data mutation

## Decision

`Расчёт поставок` gains the default-open independent block `Заказ на
фулфилмент (FBS)`. It plans a factory order to one selected own FBS facility
without reading or using WB/FBO stock, WB warehouse distribution, FF→WB
inbound, selected WB supplies or any WB overlay. The UI has an immutable
`Остатки WB не учитываются` disclosure and no switch that can add those pools.

The old `Заказ на фабрике` remains a separate default-collapsed WB compatibility
scenario. Its latest result/history/export contracts are preserved. When the
current official WB snapshot has no warehouse detail or is aggregate-only, its
status is `stale`, shows the last calculation time and current source reason,
and does not present the saved result as currently ready.

## Selected-facility formula

Only active facilities are exposed. The selected facility reads the
authoritative facility × FBS physical/reservation read model:

```text
horizon_days = production_days
             + factory_to_target_ff_days
             + ff_safety_days
             + order_cycle_days
target = national_daily_demand * horizon_days
available = physical - signed_reservation
coverage = available + remaining_active_inbound_assigned_to_facility
recommendation = ceil_to_batch(max(target - coverage, 0))
```

A missing facility/SKU physical row is unknown and blocks that facility; it is
never replaced with zero. Global FBS readiness may remain unavailable because
another active facility is incomplete, but a complete selected facility stays
readable. The MVP consumes Russia-wide `orderCount` demand once: FF Москва is
the only executable target, while active FF Оренбург remains visible and
blocked both by an incomplete physical ledger and the no-double-national-demand
policy.

Only positive matched product remainder from server-derived `production` and
`in_transit` supplier orders contributes inbound coverage. `accepted_ff`,
cancelled/inactive and non-authoritative lines are excluded. Explicit
`target_facility_id` is authoritative. Historical `NULL` targets receive a
planning-only fallback to active FF Москва; no row is rewritten and an explicit
Оренбург target never enters Moscow coverage.

## Sales window

The demand source remains exact-date server-owned
`temporal_source_snapshots[source_key=sales_funnel_history]` and national
`orderCount`. Two mutually exclusive modes are accepted:

- `last_n_days`: positive `N`, default `14`, ending at the last closed day;
- `custom_period`: required `date_from` and `date_to`, both inclusive.

Both dates must be valid, `date_from <= date_to`, closed/non-future and fully
covered by authoritative history. The existing availability-adjusted median
threshold may exclude suspicious stockout/low days only inside the requested
window. It never borrows dates outside the window. No valid SKU days is a
fail-closed calculation error; partial valid days produce explicit warnings and
evidence.

## Persistence and compatibility

Supplier shipment headers add nullable `target_facility_id` and resolved
`target_facility_name`. New records must explicitly select an active facility.
Historical rows are preserved without mass backfill. Target changes after
actual FF acceptance are rejected.

Successful FBS calculations do not create supplier/factory orders. They update
only the compatible FBS latest-result slot and append one immutable complete
`fbs_fulfillment_order` registry record. Evidence freezes facility id/name,
national scope, `wb_stock_used=false`, signed stock operands, included inbound,
settings, requested/actual sales boundaries, calendar and included/excluded
trading days, demand basis/fingerprints and exact XLSX bytes.

Schema changes are additive:

- `sheet_vitrina_v1_supplier_shipments.target_facility_id` nullable;
- `sheet_vitrina_v1_supplier_shipments.target_facility_name` nullable;
- `sheet_vitrina_v1_fbs_fulfillment_order_result_state` single latest slot.

No historical supplier row, WB snapshot, FF ledger row, reservation, business
document or production order is backfilled or mutated by this migration.

## HTTP and UI

- `GET /v1/sheet-vitrina-v1/supply/fbs-fulfillment-order/status`;
- `POST /v1/sheet-vitrina-v1/supply/fbs-fulfillment-order/calculate`;
- `GET /v1/sheet-vitrina-v1/supply/fbs-fulfillment-order/recommendation.xlsx`.

Status shows selected FF readiness, physical/reserved/available, active inbound,
history coverage and blockers. Result shows selected mode and exact boundaries,
calendar/used trading-day counts and per-SKU coverage/recommendation. Supplier
operator and supplier-safe surfaces show the target selector and registry
column.

## Verification

- `apps/fbs_fulfillment_order_supply_smoke.py` covers Moscow happy path,
  signed `physical-reserved`, incomplete Orenburg isolation, no WB operands,
  legacy-null Moscow fallback, explicit Orenburg exclusion, active/accepted
  inbound statuses, validation, rounding, both history modes, inclusive bounds,
  no outside-window leakage and immutable evidence/export;
- `apps/sheet_vitrina_v1_fbs_fulfillment_order_http_smoke.py` covers protected
  endpoints, UI tokens/default section, validation/download and stale legacy
  readiness;
- `apps/sheet_vitrina_v1_fbs_fulfillment_order_browser_smoke.py` covers the
  default-open/collapsed surfaces, facility readiness switching, both history
  controls and an exact custom-period calculation in a real browser;
- existing factory-order, supplier shipment, calculation registry, inventory
  planning and operator UI smokes remain regression gates.

## Production boundary

This is `scope:live-runtime`. Release Train deploys and verifies the exact
merged SHA. There is no production-mutation runner, data backfill, order
creation, WB write or owner apply gate. Production verification is read-only
HTTP/authenticated UI and stored-schema/result inspection only.

Migration 149 is a later, separate `scope:production-mutation` contour. It does
not change this feature-release boundary: its exact owner-authorized runner may
materialize only the 41 missing Moscow/FBS confirmed-zero rows needed by the
already deployed selected-facility readiness contract, and never runs a
calculation or creates an order.
