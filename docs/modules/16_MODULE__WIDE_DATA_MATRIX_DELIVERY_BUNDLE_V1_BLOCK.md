---
title: "Модуль: wide_data_matrix_delivery_bundle_v1_block"
doc_id: "WB-CORE-MODULE-16-WIDE-DATA-MATRIX-DELIVERY-BUNDLE-V1-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `wide_data_matrix_delivery_bundle_v1_block`."
scope: "Sheet-ready delivery bundle, input/target fixtures, кодовый минимум, подтверждённый artifact-backed smoke и границы первого handoff-ready checkpoint."
source_basis:
  - "migration/81_wide_data_matrix_delivery_contract.md"
  - "migration/82_wide_data_matrix_delivery_bundle_v1.md"
  - "artifacts/wide_data_matrix_delivery_bundle_v1/evidence/initial__wide-data-matrix-delivery-bundle-v1__evidence.md"
  - "apps/wide_data_matrix_delivery_bundle_v1_smoke.py"
related_modules:
  - "packages/contracts/wide_data_matrix_delivery_bundle_v1.py"
  - "packages/application/wide_data_matrix_delivery_bundle_v1.py"
related_tables: []
related_endpoints: []
related_runners:
  - "apps/wide_data_matrix_delivery_bundle_v1_smoke.py"
related_docs:
  - "migration/81_wide_data_matrix_delivery_contract.md"
  - "migration/82_wide_data_matrix_delivery_bundle_v1.md"
  - "artifacts/wide_data_matrix_delivery_bundle_v1/evidence/initial__wide-data-matrix-delivery-bundle-v1__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого sheet-ready delivery bundle новой wide-by-date витрины."
---

# 1. Идентификатор и статус

- `module_id`: `wide_data_matrix_delivery_bundle_v1_block`
- `family`: `delivery`
- `status_transfer`: delivery bundle перенесён в `wb-core`
- `status_verification`: fixtures и smoke подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: ожидает merge в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как:
  - `wide_data_matrix_v1_fixture_block`
  - `table_projection_bundle_block`
  - `sku_display_bundle_block`
  - `registry/pilot_bundle`
  - `migration/81_wide_data_matrix_delivery_contract.md`
- Семантика блока: не делать Google Sheet и не писать Apps Script, а зафиксировать первый handoff-ready delivery artifact для будущих листов `DATA_VITRINA` и `STATUS`.

# 3. Target contract и смысл результата

- Top-level shape:
  - `delivery_contract_version`
  - `snapshot_id`
  - `as_of_date`
  - `data_vitrina`
  - `status`
- Для каждой секции:
  - `sheet_name`
  - `header`
  - `rows`
- `data_vitrina` наследует wide-by-date форму:
  - `A = label`
  - `B = key`
  - `C.. = dates`
- `status` материализует source freshness, coverage и missing indicators в sheet-ready rows.

# 4. Артефакты по модулю

- input bundle:
  - `artifacts/wide_data_matrix_delivery_bundle_v1/input_bundle/normal__template__input-bundle__fixture.json`
  - `artifacts/wide_data_matrix_delivery_bundle_v1/input_bundle/minimal__template__input-bundle__fixture.json`
- target:
  - `artifacts/wide_data_matrix_delivery_bundle_v1/target/normal__template__delivery-bundle__fixture.json`
  - `artifacts/wide_data_matrix_delivery_bundle_v1/target/minimal__template__delivery-bundle__fixture.json`
- parity:
  - `artifacts/wide_data_matrix_delivery_bundle_v1/parity/normal__template__input-vs-delivery__comparison.md`
  - `artifacts/wide_data_matrix_delivery_bundle_v1/parity/minimal__template__input-vs-delivery__comparison.md`
- evidence:
  - `artifacts/wide_data_matrix_delivery_bundle_v1/evidence/initial__wide-data-matrix-delivery-bundle-v1__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/wide_data_matrix_delivery_bundle_v1.py`
- application: `packages/application/wide_data_matrix_delivery_bundle_v1.py`
- artifact-backed smoke: `apps/wide_data_matrix_delivery_bundle_v1_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён artifact-backed smoke через `apps/wide_data_matrix_delivery_bundle_v1_smoke.py`.
- Smoke проверяет:
  - наличие top-level metadata;
  - наличие секций `data_vitrina` и `status`;
  - `sheet_name + header + rows` contract;
  - range-write readiness обеих секций;
  - cross-check между wide fixture, projection fixture, sku display bundle и registry pilot bundle.

# 7. Что уже доказано по модулю

- Wide matrix delivery больше не является только контрактом: есть живой handoff-ready artifact.
- `DATA_VITRINA` уже материализуется как sheet-ready `header + rows`.
- `STATUS` уже материализуется как отдельный технический sidecar.
- Snapshot identity, coverage и missing indicators уже зафиксированы в машинно-читаемом bundle.

# 8. Что пока не является частью финальной production-сборки

- live Google Sheet;
- Apps Script importer;
- live range write;
- orchestration доставки;
- production deploy.
