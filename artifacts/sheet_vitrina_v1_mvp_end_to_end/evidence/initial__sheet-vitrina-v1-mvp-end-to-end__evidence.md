# Evidence

- `prepareRegistryUploadOperatorSheets` поднимает current operator seed `33 / 102 / 7`.
- `uploadRegistryUploadBundle` принимает bundle `33 / 102 / 7` через существующий HTTP upload entrypoint.
- Current truth и server-side plan материализуют `95` enabled+show_in_data metrics, но operator-facing `DATA_VITRINA` reshaped в bounded legacy-aligned matrix view c `34` blocks, `7` metric keys и `305` data rows.
- `loadSheetVitrinaTable` materialize-ит `DATA_VITRINA` и `STATUS` через live readback endpoint на том же lightweight HTTP server.
- `STATUS` явно фиксирует blocked sources `promo_by_price` и `cogs_by_group`; current truth/server plan сохраняют этот более широкий source-status surface без переноса heavy logic в Apps Script.
