# Normal Case Parity

- `wide_data_matrix_v1` normal fixture разворачивается в sheet-ready `data_vitrina.header + rows` без изменения порядка строк.
- `table_projection_bundle_block` normal fixture разворачивается в `status.header + rows` через `source_statuses`.
- `snapshot_id` и `as_of_date` собираются на основе последней даты wide matrix и фиксированного `delivery_contract_version`.
- `sku_display_bundle_block` и `registry/pilot_bundle` используются как cross-check источников SKU и metric keys, а не как отдельные выходные листы.
