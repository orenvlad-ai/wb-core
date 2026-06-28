# 95. WB regional supply planning assistant

`Поставки -> Расчёты` gains a bounded read-only assistant for choosing WB warehouses/dates/routes after a regional supply calculation.

## Scope

- Uses the latest persisted `wb_regional_supply` result.
- Plans one selected calculation district at a time.
- Resolves `nmId -> barcode` from server-owned nomenclature.
- Calls official WB FBW `POST /api/v1/acceptance/options` only as a read-only information request with `barcode + quantity`.
- Enriches returned options with read-only warehouses, Marketplace offices, acceptance coefficients, box tariff and transit tariff evidence.
- Ranks options deterministically and exposes copyable manual handoff JSON for the operator.

## Persistence

No new database table or schema migration is required for the MVP.

The endpoint is stateless and reads existing runtime truth:

- `sheet_vitrina_v1_wb_regional_supply_result_state`
- `sheet_vitrina_v1_nomenclature_items`
- existing WB supplies warehouse/district mapping evidence when available

The planning response includes `cache.enabled=false`; selected options are not persisted as fact.

## Boundaries

- No FBW supply creation.
- No FBS supply creation.
- No Seller Portal automation.
- No WB mutations.
- No Google Sheets/GAS/localStorage truth.
- Missing barcode and upstream/token/rate-limit failures are controlled response states, not crashes.
