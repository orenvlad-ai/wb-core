# Evidence

- `prepareRegistryUploadOperatorSheets` поднимает current operator seed `33 / 19 / 2`.
- `uploadRegistryUploadBundle` принимает bundle `33 / 19 / 2` через существующий HTTP upload entrypoint.
- `DATA_VITRINA` остаётся bounded to `7` live readback metrics, хотя upload/runtime уже хранят полный current `19`-row metrics dictionary.
- `loadSheetVitrinaTable` materialize-ит `DATA_VITRINA` и `STATUS` через live readback endpoint на том же lightweight HTTP server.
