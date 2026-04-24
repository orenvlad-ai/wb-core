---
title: "Модуль: sheet_vitrina_v1_scaffold_block"
doc_id: "WB-CORE-MODULE-17-SHEET-VITRINA-V1-SCAFFOLD-BLOCK"
doc_type: "module"
status: "archived"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_scaffold_block`."
scope: "Archived sheet-side scaffold, layout artifacts and sheet-write plan for the former Google Sheets contour. This is migration evidence, not an active runtime/update/write/load/verify target."
source_basis:
  - "migration/81_wide_data_matrix_delivery_contract.md"
  - "migration/82_wide_data_matrix_delivery_bundle_v1.md"
  - "migration/83_sheet_vitrina_v1_scaffold.md"
  - "artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md"
  - "apps/sheet_vitrina_v1_smoke.py"
related_modules:
  - "packages/contracts/sheet_vitrina_v1.py"
  - "packages/application/sheet_vitrina_v1.py"
related_tables: []
related_endpoints: []
related_runners:
  - "apps/sheet_vitrina_v1_smoke.py"
related_docs:
  - "migration/81_wide_data_matrix_delivery_contract.md"
  - "migration/82_wide_data_matrix_delivery_bundle_v1.md"
  - "migration/83_sheet_vitrina_v1_scaffold.md"
  - "artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Архивирован: former sheet-side scaffold remains as migration evidence; active/current contour is website/operator/public web-vitrina."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_scaffold_block`
- `family`: `sheet-side archived`
- `status_transfer`: scaffold перенесён в `wb-core`
- `status_verification`: artifacts и smoke подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`
- `status_current`: `ARCHIVED / DO NOT USE`; не active runtime/update/write/load/verify target

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как:
  - `wide_data_matrix_delivery_bundle_v1_block`
  - `migration/81_wide_data_matrix_delivery_contract.md`
  - `migration/82_wide_data_matrix_delivery_bundle_v1.md`
- Семантика блока: не создавать реальный Google Sheet и не писать Apps Script, а зафиксировать первый sheet-side scaffold с layout и full-overwrite write plan.

# 3. Target contract и смысл результата

- Top-level shape:
  - `plan_version`
  - `snapshot_id`
  - `as_of_date`
  - `sheets[]`
- Для каждого sheet target:
  - `sheet_name`
  - `write_start_cell`
  - `write_rect`
  - `clear_range`
  - `write_mode`
  - `partial_update_allowed`
  - `header`
  - `rows`
  - `row_count`
  - `column_count`
- Канонические листы V1:
  - `DATA_VITRINA`
  - `STATUS`

# 4. Артефакты по модулю

- layout:
  - `artifacts/sheet_vitrina_v1/layout/data_vitrina_sheet_layout.json`
  - `artifacts/sheet_vitrina_v1/layout/status_sheet_layout.json`
- input:
  - `artifacts/sheet_vitrina_v1/input/normal__template__delivery-bundle__fixture.json`
- target:
  - `artifacts/sheet_vitrina_v1/target/normal__template__sheet-write-plan__fixture.json`
- parity:
  - `artifacts/sheet_vitrina_v1/parity/normal__template__delivery-vs-sheet-plan__comparison.md`
- evidence:
  - `artifacts/sheet_vitrina_v1/evidence/initial__sheet-vitrina-v1__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/sheet_vitrina_v1.py`
- application: `packages/application/sheet_vitrina_v1.py`
- artifact-backed smoke: `apps/sheet_vitrina_v1_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён artifact-backed smoke через `apps/sheet_vitrina_v1_smoke.py`.
- Smoke проверяет:
  - что scaffold содержит ровно 2 листа;
  - что для каждого листа есть `header + rows`;
  - что `DATA_VITRINA` и `STATUS` готовы к полному overwrite;
  - что `write_rect` и `clear_range` детерминированы;
  - что delivery bundle можно честно положить в Sheet без раннего Apps Script runtime.

# 7. Что уже доказано по модулю

- Historical Google Sheets contour больше не является active/current contour; scaffold сохраняется как archive/migration reference.
- `DATA_VITRINA` historical write plan сохранён только как migration boundary.
- `STATUS` historical write plan сохранён только как migration boundary.
- Partial update semantics сознательно не вводятся раньше времени.

# 8. Что пока не является частью финальной production-сборки

- live Google Sheet;
- Google API wiring;
- Apps Script importer;
- partial update logic;
- orchestration доставки;
- operator-level workflow.
