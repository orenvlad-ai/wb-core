# Evidence: wide_data_matrix_delivery_bundle_v1

- Источник wide matrix: `artifacts/wide_data_matrix_v1/target/*.json`
- Источник status layer: `artifacts/table_projection_bundle_block/target/*.json`
- Cross-check источников SKU и metric keys: `artifacts/sku_display_bundle_block/target/*.json`, `registry/pilot_bundle/*.json`
- Подтверждающий smoke: `python3 apps/wide_data_matrix_delivery_bundle_v1_smoke.py`
- Цель checkpoint: зафиксировать первый sheet-ready delivery artifact без Google Sheet и без Apps Script.
