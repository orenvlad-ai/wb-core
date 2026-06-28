# 95. WB regional supply planning assistant

`Поставки -> Расчёты` gains a bounded read-only assistant for choosing WB warehouses/dates/routes after a regional supply calculation.

## Scope

- Uses the latest persisted `wb_regional_supply` result.
- Plans one selected calculation district at a time.
- Resolves `nmId -> barcode` from server-owned nomenclature.
- Calls official WB FBW `POST /api/v1/acceptance/options` only as a read-only information request with the official JSON array body `[{barcode, quantity}]`; optional `warehouseID` is query-only.
- Normalizes official `result[]` as barcode-level evidence, flattens nested `result[].warehouses[]`, then groups the visible business result by destination warehouse. Repeated barcode/date/coefficient rows become nested `dates[]` and barcode coverage evidence on the warehouse option; the UI cap is applied after grouping, not to raw date rows.
- Adds `major_warehouse_diagnostics` for important district warehouses. For the Central district it checks Коледино, Электросталь, Обухово, Подольск, Тула and key СГТ warehouses across acceptance/options, warehouse catalog, box tariffs, acceptance coefficients and Marketplace offices. If an expected warehouse is absent from the general acceptance/options response but exists in the catalog, bounded read-only `warehouseID` probes may be used as diagnostics.
- Enriches returned warehouse options with read-only warehouses, Marketplace offices, acceptance coefficients, detailed box tariff fields and transit tariff evidence.
- Ranks options deterministically and exposes copyable manual handoff JSON for the operator.

## Persistence

The planning endpoint is stateless and reads existing runtime truth:

- `sheet_vitrina_v1_wb_regional_supply_result_state`
- `sheet_vitrina_v1_nomenclature_items`
- existing WB supplies warehouse/district mapping evidence when available

Regional calculate also maintains `sheet_vitrina_v1_wb_regional_supply_calculation_audit` as a bounded metadata-only ring-buffer for operator diagnostics. This audit is not a planning source of truth and must not contain WB supply ids, barcodes or row-level recommendation payloads.

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
- `coefficient=-1` is treated as unavailable/problematic and ranked below normal `0/1` dates.
