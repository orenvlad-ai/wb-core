---
title: "Модуль: sheet_vitrina_v1_registry_seed_v3_bootstrap_block"
doc_id: "WB-CORE-MODULE-25-SHEET-VITRINA-V1-REGISTRY-SEED-V3-BOOTSTRAP-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`."
scope: "Compact v3 bootstrap для `CONFIG / METRICS / FORMULAS`: materialized operator seed, full current sheet/upload metrics dictionary, канонический formulas subset и сохранение service/status блока без поломки upload trigger."
source_basis:
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/config_v3_seed__fixture.json"
  - "artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/metrics_v3_seed__fixture.json"
  - "artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/formulas_v3_seed__fixture.json"
  - "artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/evidence/initial__sheet-vitrina-v1-registry-seed-v3-bootstrap__evidence.md"
related_modules:
  - "gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs"
  - "gas/sheet_vitrina_v1/RegistryUploadTrigger.gs"
  - "apps/sheet_vitrina_v1_registry_upload_trigger_harness.js"
  - "apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
related_tables:
  - "CONFIG"
  - "METRICS"
  - "FORMULAS"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
related_runners:
  - "apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py"
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
related_docs:
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ для compact v3 bootstrap operator sheets в `sheet_vitrina_v1`."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`
- `family`: `sheet-side`
- `status_transfer`: compact v3 seed bootstrap перенесён в `wb-core`
- `status_verification`: prepare-to-upload smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `sheet_vitrina_v1_registry_upload_trigger_block`
  - `registry_upload_http_entrypoint_block`
  - `migration/91_sheet_vitrina_v1_registry_upload_trigger.md`
  - `migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md`
- Семантика блока: не менять upload/runtime contract и не строить новый UI, а поднять в таблице уже заполненные operator registries, совместимые с существующим upload path и current live readback contour.

# 3. Target contract и смысл результата

- Канонический operator output prepare-step:
  - лист `CONFIG` с compact v3 rows
  - лист `METRICS` с compact v3 rows
  - лист `FORMULAS` с compact v3 rows
- Канонический headers set:
  - `CONFIG`: `nm_id`, `enabled`, `display_name`, `group`, `display_order`
  - `METRICS`: `metric_key`, `enabled`, `scope`, `label_ru`, `calc_type`, `calc_ref`, `show_in_data`, `format`, `display_order`, `section`
  - `FORMULAS`: `formula_id`, `expression`, `description`

## 3.1 Service block bounded шага

- `CONFIG!H:I` остаётся служебной зоной.
- `CONFIG!I2:I7` сохраняет:
  - `endpoint_url`
  - `last_bundle_version`
  - `last_status`
  - `last_activated_at`
  - `last_http_status`
  - `last_validation_errors`
- Prepare/reprepare не должен очищать эту зону.

## 3.2 Current main-confirmed seed bounded шага

- В текущем `main` materialize-ятся:
  - `config_v2 = 33`
  - `metrics_v2 = 19`
  - `formulas_v2 = 2`
- `CONFIG` остаётся compact v3 operator seed.
- `METRICS` больше не режется до MVP-safe `7` rows и собирает current main-confirmed dictionary:
  - `12` current upload-authoritative rows;
  - `7` live readback rows, уже используемых `sheet_vitrina_v1_live_plan`.
- `FORMULAS` синхронизируются до канонического subset, который требуется current formula metrics в `metrics_v2`.
- Это осознанный bounded checkpoint: full legacy dump не переносится, но sheet-side upload уже не теряет существующие `metrics_v2` rows.

# 4. Артефакты и wiring по модулю

- input artifacts:
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/config_v3_seed__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/metrics_v3_seed__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/input/formulas_v3_seed__fixture.json`
- target artifacts:
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/target/prepare_result__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/target/preserved_control_block__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/target/bundle_from_seed_v3__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/target/upload_response__accepted__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/target/status_block__after_upload__fixture.json`
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/target/current_state__fixture.json`
- parity:
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/parity/input-vs-sheets__comparison.md`
- evidence:
  - `artifacts/sheet_vitrina_v1_registry_seed_v3_bootstrap/evidence/initial__sheet-vitrina-v1-registry-seed-v3-bootstrap__evidence.md`

# 5. Кодовые части

- bound Apps Script:
  - `gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs`
  - `gas/sheet_vitrina_v1/RegistryUploadTrigger.gs`
- local harness:
  - `apps/sheet_vitrina_v1_registry_upload_trigger_harness.js`
- smoke:
  - `apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py`
- reused upload/runtime path:
  - `packages/application/registry_upload_http_entrypoint.py`
  - `packages/application/registry_upload_db_backed_runtime.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный prepare-to-upload smoke через `apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py`.
- Smoke проверяет:
  - что prepare materialize-ит `CONFIG / METRICS / FORMULAS` с compact v3 headers и current seed rows `33 / 19 / 2`;
  - что повторный prepare не теряет service/status block;
  - что bundle, собранный из seeded sheets, содержит полный expected `metrics_v2` set и уходит в существующий HTTP entrypoint;
  - что accepted response возвращается в канонической форме;
  - что current truth обновляется через уже существующий runtime DB.

# 7. Что уже доказано по модулю

- В `sheet_vitrina_v1` больше не поднимаются пустые operator sheets для registry upload.
- Оператор после `Подготовить листы CONFIG / METRICS / FORMULAS` получает уже заполненный seed, где `METRICS` содержит полный current main-confirmed dictionary, а не урезанный subset.
- Служебная зона `CONFIG!H:I` сохраняется и не ломает live endpoint wiring.
- Новый bootstrap не дублирует server-side validation/runtime и не меняет bundle/result contract.

# 8. Что пока не является частью финальной production-сборки

- reverse-load server-side truth обратно в таблицу;
- кнопка `обновить витрину`;
- deploy/auth-hardening;
- полный legacy dump и future expansion beyond current `19` metric rows;
- большой UI/UX redesign операторской таблицы.
