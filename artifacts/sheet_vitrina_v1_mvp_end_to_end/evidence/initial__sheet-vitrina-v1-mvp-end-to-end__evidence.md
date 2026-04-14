# Evidence

- `prepareRegistryUploadOperatorSheets` поднимает current operator seed `33 / 102 / 7`.
- `uploadRegistryUploadBundle` принимает bundle `33 / 102 / 7` через существующий HTTP upload entrypoint.
- `DATA_VITRINA` materialize-ит полный current displayed set `95` metric keys и `1631` rows по current truth.
- `loadSheetVitrinaTable` materialize-ит `DATA_VITRINA` и `STATUS` через live readback endpoint на том же lightweight HTTP server.
- `STATUS` явно фиксирует blocked sources `promo_by_price` и `cogs_by_group`; их rows остаются в `DATA_VITRINA`, но numeric values для них пустые до появления live HTTP adapters.
