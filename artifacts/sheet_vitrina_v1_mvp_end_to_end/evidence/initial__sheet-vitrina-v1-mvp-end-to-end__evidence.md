# Evidence

- `prepareRegistryUploadOperatorSheets` поднимает current operator seed `33 / 19 / 2`.
- `uploadRegistryUploadBundle` принимает bundle `33 / 19 / 2` через существующий HTTP upload entrypoint.
- `DATA_VITRINA` больше не режет displayed metric rows до `7`: load materialize-ит полный current authoritative `19`-key set из runtime truth.
- Current numeric live fill остаётся backed only для existing `7` public readback metrics; остальные authoritative rows не пропадают, а остаются blank.
- `loadSheetVitrinaTable` materialize-ит `DATA_VITRINA` и `STATUS` через live readback endpoint на том же lightweight HTTP server.
