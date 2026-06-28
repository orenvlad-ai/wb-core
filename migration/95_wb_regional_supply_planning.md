# 95. WB regional supply planning assistant

`Поставки -> Расчёты` gains a bounded read-only assistant for choosing WB warehouses/dates/routes after a regional supply calculation.

## Scope

- Uses the latest persisted `wb_regional_supply` result.
- Plans one selected calculation district at a time.
- Resolves `nmId -> barcode` from server-owned nomenclature.
- Calls official WB FBW `POST /api/v1/acceptance/options` only as a read-only information request with the official JSON array body `[{barcode, quantity}]`; optional `warehouseID` is query-only.
- Normalizes official `result[]` as barcode-level evidence, flattens nested `result[].warehouses[]` into option candidates, deduplicates repeated warehouse candidates across successful barcode rows, and returns a bounded top-ranked visible set; per-barcode upstream errors are controlled warnings/blockers and mixed success/error can still produce partial options.
- Enriches returned options with read-only warehouses, Marketplace offices, acceptance coefficients, box tariff and transit tariff evidence.
- Ranks options deterministically and exposes copyable manual handoff JSON for the operator.

## Persistence

No new database table or schema migration is required for the MVP.

The endpoint is stateless and reads existing runtime truth:

- `sheet_vitrina_v1_wb_regional_supply_result_state`
- `sheet_vitrina_v1_nomenclature_items`
- existing WB supplies warehouse/district mapping evidence when available

The planning response includes `cache.enabled=false`; selected options are not persisted as fact.

## Operator state safety

- The fresh `POST .../wb-regional/calculate` response is the canonical browser state for the visible result; a following lagging status refresh must not overwrite it with an older `last_result`.
- Starting a new regional calculation clears the previous planning panel and resets old district planning in-flight state.
- Planning requests send the latest visible `calculation_id`. If the backend returns structured `calculation_id_mismatch`, the UI performs one status refresh and one retry with the backend-provided actual calculation id.
- `Подобрать склады WB` is disabled only while regional calculation/planning is in-flight or for a district with zero planned quantity. Zero-quantity rows expose the reason `Нет количества к поставке` in the disabled button state.

## Boundaries

- No FBW supply creation.
- No FBS supply creation.
- No Seller Portal automation.
- No WB mutations.
- No Google Sheets/GAS/localStorage truth.
- Missing barcode and upstream/token/rate-limit failures are controlled response states, not crashes.
- HTTP diagnostics include endpoint, request shape, product count, optional `warehouseID` and sanitized body prefix without tokens or full barcode lists.
