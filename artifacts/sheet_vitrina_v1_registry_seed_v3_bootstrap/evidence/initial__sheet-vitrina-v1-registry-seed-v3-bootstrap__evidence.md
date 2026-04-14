# Evidence

- `prepareRegistryUploadOperatorSheets` поднимает current main-confirmed seed `33 / 102 / 7`.
- Seed собирается в upload bundle без ручных правок.
- HTTP upload path принимает seeded bundle целиком, включая все `102` rows `metrics_v2`, и materialize-ит current state.
