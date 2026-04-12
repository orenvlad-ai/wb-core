---
title: "Модуль: wide_data_matrix_v1_fixture_block"
doc_id: "WB-CORE-MODULE-15-WIDE-DATA-MATRIX-V1-FIXTURE-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `wide_data_matrix_v1_fixture_block`."
scope: "Wide-by-date input/target fixtures, кодовый минимум, подтверждённый artifact-backed smoke и границы первого рабочего checkpoint."
source_basis:
  - "migration/79_wide_data_matrix_contract.md"
  - "migration/80_wide_data_matrix_v1_fixture.md"
  - "artifacts/wide_data_matrix_v1/evidence/initial__wide-data-matrix-v1__evidence.md"
  - "apps/wide_data_matrix_v1_smoke.py"
related_modules:
  - "packages/contracts/wide_data_matrix_v1.py"
  - "packages/application/wide_data_matrix_v1.py"
related_tables: []
related_endpoints: []
related_runners:
  - "apps/wide_data_matrix_v1_smoke.py"
related_docs:
  - "migration/79_wide_data_matrix_contract.md"
  - "migration/80_wide_data_matrix_v1_fixture.md"
  - "artifacts/wide_data_matrix_v1/evidence/initial__wide-data-matrix-v1__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для первого artifact-backed implementation шага wide-by-date витрины."
---

# 1. Идентификатор и статус

- `module_id`: `wide_data_matrix_v1_fixture_block`
- `family`: `wide-matrix`
- `status_transfer`: модульный fixture-шаг перенесён в `wb-core`
- `status_verification`: fixture и smoke подтверждены
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: ожидает merge в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как:
  - `sku_display_bundle_block`
  - `table_projection_bundle_block`
  - `registry/pilot_bundle`
  - `migration/79_wide_data_matrix_contract.md`
- Семантика блока: не создавать новую таблицу и не писать Apps Script, а зафиксировать первый artifact-backed wide-by-date fixture для новой витрины.
- Для `TOTAL` и `GROUP` используется только safe aggregate subset; главным рабочим блоком V1 остаётся `SKU`.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `columns[]`
  - `dates[]`
  - `blocks[]`
  - `rows[]`
- Empty shape:
  - `kind = "empty"`
  - `columns[]`
  - `dates[]`
  - `blocks[]`
  - `rows = []`
  - `detail`
- Каноническая wide форма:
  - `A = label`
  - `B = key`
  - `C.. = dates`
- Key-patterns:
  - `TOTAL|<metric_key>`
  - `GROUP:<group>|<metric_key>`
  - `SKU:<nm_id>|<metric_key>`

# 4. Артефакты по модулю

- input bundle:
  - `artifacts/wide_data_matrix_v1/input_bundle/normal__template__input-bundle__fixture.json`
  - `artifacts/wide_data_matrix_v1/input_bundle/minimal__template__input-bundle__fixture.json`
- target:
  - `artifacts/wide_data_matrix_v1/target/normal__template__target__fixture.json`
  - `artifacts/wide_data_matrix_v1/target/minimal__template__target__fixture.json`
- parity:
  - `artifacts/wide_data_matrix_v1/parity/normal__template__input-vs-target__comparison.md`
  - `artifacts/wide_data_matrix_v1/parity/minimal__template__input-vs-target__comparison.md`
- evidence:
  - `artifacts/wide_data_matrix_v1/evidence/initial__wide-data-matrix-v1__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/wide_data_matrix_v1.py`
- application: `packages/application/wide_data_matrix_v1.py`
- artifact-backed smoke: `apps/wide_data_matrix_v1_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён artifact-backed smoke через `apps/wide_data_matrix_v1_smoke.py`.
- Smoke проверяет:
  - форму wide matrix;
  - block layout `TOTAL / GROUP / SKU`;
  - row ordering по `display_order`;
  - корректность key-patterns;
  - кросс-ссылки между input bundle, projection и registry.

# 7. Что уже доказано по модулю

- Wide matrix больше не является только схемой: есть живой input/target fixture.
- `SKU`-блок уже материализуется в canonical wide-by-date форме.
- `TOTAL` и `GROUP` можно честно собрать как bounded safe subset без Google Sheet и Apps Script.
- Formula и ratio-строки резолвятся через runtime registry, а не через табличную вычислительную логику.

# 8. Что пока не является частью финальной production-сборки

- live Google Sheet;
- Apps Script wiring;
- полный `TOTAL` parity;
- полный `GROUP` parity;
- полный legacy `DATA` parity;
- orchestration и deploy.
