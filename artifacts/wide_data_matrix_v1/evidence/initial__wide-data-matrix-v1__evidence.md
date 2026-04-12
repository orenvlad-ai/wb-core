# Evidence: wide_data_matrix_v1 fixture

## Scope

- bounded implementation step для `wide_data_matrix_v1_fixture_block`
- без Google Sheet
- без Apps Script
- без внешних API

## Source Basis

- `artifacts/sku_display_bundle_block/target/normal__template__target__fixture.json`
- `artifacts/table_projection_bundle_block/target/normal__template__target__fixture.json`
- `registry/pilot_bundle/config_v2.json`
- `registry/pilot_bundle/metrics_v2.json`
- `registry/pilot_bundle/formulas_v2.json`
- `registry/pilot_bundle/metric_runtime_registry.json`

## What Is Proven

- wide matrix больше не существует только как схема: есть bounded input fixture и target fixture
- подтверждена форма `A=label`, `B=key`, `C..=dates`
- подтверждены три блока:
  - `TOTAL`
  - `GROUP`
  - `SKU`
- `TOTAL` и `GROUP` материализуются только для safe aggregate subset
- `SKU`-строки строятся в canonical порядке по `display_order`
- formula и ratio строки резолвятся через runtime registry, а не через табличную runtime-semantics

## Checks

- `python3 apps/wide_data_matrix_v1_smoke.py`
- `python3 -m py_compile packages/contracts/wide_data_matrix_v1.py packages/application/wide_data_matrix_v1.py apps/wide_data_matrix_v1_smoke.py`
