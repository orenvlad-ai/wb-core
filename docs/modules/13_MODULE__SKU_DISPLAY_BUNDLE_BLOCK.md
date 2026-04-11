---
title: "Модуль: sku_display_bundle_block"
doc_id: "WB-CORE-MODULE-13-SKU-DISPLAY-BUNDLE-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sku_display_bundle_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части, подтверждённый table-facing smoke и границы первого рабочего checkpoint."
source_basis:
  - "migration/66_sku_display_bundle_block_contract.md"
  - "migration/69_sku_display_bundle_block_legacy_sample_source.md"
  - "artifacts/sku_display_bundle_block/evidence/initial__sku-display-bundle__evidence.md"
  - "apps/sku_display_bundle_block_smoke.py"
  - "apps/sku_display_bundle_block_config_smoke.py"
related_modules:
  - "packages/contracts/sku_display_bundle_block.py"
  - "packages/adapters/sku_display_bundle_block.py"
  - "packages/application/sku_display_bundle_block.py"
related_tables:
  - "CONFIG"
related_endpoints: []
related_runners:
  - "apps/sku_display_bundle_block_smoke.py"
  - "apps/sku_display_bundle_block_config_smoke.py"
related_docs:
  - "migration/66_sku_display_bundle_block_contract.md"
  - "migration/67_sku_display_bundle_block_parity_matrix.md"
  - "migration/68_sku_display_bundle_block_evidence_checklist.md"
  - "migration/69_sku_display_bundle_block_legacy_sample_source.md"
  - "artifacts/sku_display_bundle_block/evidence/initial__sku-display-bundle__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ в рамках первого bounded checkpoint для тонкого table-facing блока `sku_display_bundle_block`."
---

# 1. Идентификатор и статус

- `module_id`: `sku_display_bundle_block`
- `family`: `table-facing`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: ожидает merge в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как минимальный subset листа `CONFIG`:
  - `sku(nmId)`
  - `active`
  - `comment`
  - `group`
- Legacy смысл блока: дать первой новой витрине канонический список SKU для отображения без полного переноса `CONFIG`.
- Ключевая semantics:
  - `comment` выступает как current human-readable display source;
  - `active` сохраняется как boolean-like enable flag;
  - row order внутри `CONFIG` фиксируется как stable table-facing order.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `count`
  - `items[]` c полями `nm_id`, `display_name`, `group`, `enabled`, `display_order`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: тонкий display bundle для новой витрины без полного registry/config migration.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/sku_display_bundle_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/sku_display_bundle_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/sku_display_bundle_block/target/normal__template__target__fixture.json`
  - `artifacts/sku_display_bundle_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/sku_display_bundle_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/sku_display_bundle_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/sku_display_bundle_block/evidence/initial__sku-display-bundle__evidence.md`
- safe source fixture:
  - `artifacts/sku_display_bundle_block/config_source/normal__template__config__fixture.json`
  - `artifacts/sku_display_bundle_block/config_source/empty__template__config__fixture.json`

# 5. Кодовые части

- contracts: `packages/contracts/sku_display_bundle_block.py`
- adapters: `packages/adapters/sku_display_bundle_block.py`
- application: `packages/application/sku_display_bundle_block.py`
- artifact-backed smoke: `apps/sku_display_bundle_block_smoke.py`
- safe CONFIG-fixture smoke: `apps/sku_display_bundle_block_config_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/sku_display_bundle_block_smoke.py`.
- Safe CONFIG-fixture smoke подтверждён через `apps/sku_display_bundle_block_config_smoke.py`.
- Для этого типа модуля canonical checkpoint не требует server-side HTTP smoke.

# 7. Что уже доказано по модулю

- Legacy-source зафиксирован строго и без расширения до полного `CONFIG`.
- Для первой витрины подтверждён минимальный bundle: `nmId`, `display_name`, `group`, `enabled`, `display_order`.
- Parity подтверждена для `normal-case` и `empty-case`.
- Safe source path показывает, что bundle можно собрать без live spreadsheet/runtime и без нового registry-слоя.

# 8. Что пока не является частью финальной production-сборки

- полный перенос `CONFIG`;
- перенос `METRICS`, `FORMULAS`, `DAILY RUN`;
- jobs/API/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
