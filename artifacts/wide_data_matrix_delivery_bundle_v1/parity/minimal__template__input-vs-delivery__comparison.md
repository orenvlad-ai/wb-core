# Minimal Case Parity

- `wide_data_matrix_v1` minimal fixture даёт пустой `DATA_VITRINA`, но сохраняет правильный wide header.
- `table_projection_bundle_block` minimal fixture даёт один честный status row для `sku_display_bundle`.
- Delivery bundle остаётся handoff-ready даже без строк данных, потому что `data_vitrina` и `status` уже range-write совместимы.
