# 96. WB regional planning business warehouse view

`Поставки -> Расчёты -> Подобрать склады WB` changes the operator planning response from raw date rows to grouped warehouse options.

## Contract changes

- `options[]` remains the visible planning list but each item is now `option_kind=warehouse_group`.
- A warehouse option keeps backward-compatible top-level fields (`warehouse_id`, `warehouse_name`, `date`, `coefficient`, `allow_unload`, `operator_handoff`) and adds:
  - `dates[]`, `date_count`, `good_date_count`, `free_date_count`;
  - `barcode_coverage`, `accepts_all_barcodes`;
  - `is_sgt`, `is_major_expected`;
  - detailed `box_tariff`, `best_transit_route`, `transit_routes`, `transit_route_count`.
- `summary` adds raw/grouped counters, accepts-all count and СГТ/non-СГТ counts.
- `major_warehouse_diagnostics[]` explains whether key warehouses were returned by WB acceptance/options, found in catalog/tariffs/coefficients/offices, mapped to a district, visible in the main list, hidden by cap/filter, or absent from the general API response.

## Ranking and safety

- Cap is applied after grouping by warehouse, so repeated СГТ/date rows cannot consume the first 300 visible slots before main warehouses.
- `coefficient=-1` is unavailable/problematic and ranked below `0` and `1`.
- Barcode coverage is factual per warehouse: accepted barcode count, total count and partial/all status come from WB acceptance/options evidence.
- Box/transit tariffs remain raw upstream evidence only. The module does not calculate or promise exact final supply cost.
- No WB mutations, FBW/FBS supply creation, Seller Portal automation, Google Sheets/GAS, or selected-option persistence are introduced.
