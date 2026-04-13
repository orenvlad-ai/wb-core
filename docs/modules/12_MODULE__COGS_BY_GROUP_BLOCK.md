---
title: "Модуль: cogs_by_group_block"
doc_id: "WB-CORE-MODULE-12-COGS-BY-GROUP-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `cogs_by_group_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части, подтверждённый rule-based smoke и границы первого рабочего checkpoint."
source_basis:
  - "migration/61_cogs_by_group_block_contract.md"
  - "migration/64_cogs_by_group_block_legacy_sample_source.md"
  - "artifacts/cogs_by_group_block/evidence/initial__cogs-by-group__evidence.md"
  - "apps/cogs_by_group_block_smoke.py"
  - "apps/cogs_by_group_block_rule_smoke.py"
related_modules:
  - "packages/contracts/cogs_by_group_block.py"
  - "packages/adapters/cogs_by_group_block.py"
  - "packages/application/cogs_by_group_block.py"
related_tables:
  - "RAW_COGS_RULES"
  - "CONFIG"
related_endpoints: []
related_runners:
  - "apps/cogs_by_group_block_smoke.py"
  - "apps/cogs_by_group_block_rule_smoke.py"
related_docs:
  - "migration/61_cogs_by_group_block_contract.md"
  - "migration/62_cogs_by_group_block_parity_matrix.md"
  - "migration/63_cogs_by_group_block_evidence_checklist.md"
  - "migration/64_cogs_by_group_block_legacy_sample_source.md"
  - "artifacts/cogs_by_group_block/evidence/initial__cogs-by-group__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ в рамках первого bounded checkpoint для rule-based apply блока `cogs_by_group_block`."
---

# 1. Идентификатор и статус

- `module_id`: `cogs_by_group_block`
- `family`: `rule-based`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как связка:
  - `RAW_COGS_RULES`
  - current active SKU linkage из `CONFIG.group`
  - `77_plugins_cogs_by_group.js`
- Legacy смысл результата задаётся на уровне `date + nmId`.
- Ключевая semantics:
  - `cost_price_rub` выбирается как последнее правило `effective_from <= date`
  - historical cost fan-out идёт через `group` active SKU
  - strict validation текущего apply path сохраняется как bounded contract

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `date_from`
  - `date_to`
  - `count`
  - `items[]` c полями `date`, `nm_id`, `cost_price_rub`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded historical COGS apply snapshot для requested `nmId` без переноса spreadsheet runtime.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/cogs_by_group_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/cogs_by_group_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/cogs_by_group_block/target/normal__template__target__fixture.json`
  - `artifacts/cogs_by_group_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/cogs_by_group_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/cogs_by_group_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/cogs_by_group_block/evidence/initial__cogs-by-group__evidence.md`
- rule-source fixture:
  - `artifacts/cogs_by_group_block/rule_source/normal__template__rules__fixture.json`

# 5. Кодовые части

- contracts: `packages/contracts/cogs_by_group_block.py`
- adapters: `packages/adapters/cogs_by_group_block.py`
- application: `packages/application/cogs_by_group_block.py`
- artifact-backed smoke: `apps/cogs_by_group_block_smoke.py`
- fixture-backed rule-source smoke: `apps/cogs_by_group_block_rule_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/cogs_by_group_block_smoke.py`.
- Fixture-backed rule-source smoke подтверждён через `apps/cogs_by_group_block_rule_smoke.py`.
- Для этого типа модуля canonical checkpoint не требует server-side HTTP smoke.

# 7. Что уже доказано по модулю

- Legacy-source, `nmId` и group linkage зафиксированы без догадок.
- Parity подтверждена для `normal-case` и `empty/no-row`.
- Rule-source logic повторяет текущую apply semantics без переноса spreadsheet runtime.
- Блок даёт рабочий bounded checkpoint на bootstrap sample set.

# 8. Что пока не является частью финальной production-сборки

- live spreadsheet/runtime orchestration;
- перенос `CONFIG/METRICS/FORMULAS`;
- jobs/API/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
