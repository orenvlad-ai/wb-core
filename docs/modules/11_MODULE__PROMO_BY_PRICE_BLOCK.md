---
title: "Модуль: promo_by_price_block"
doc_id: "WB-CORE-MODULE-11-PROMO-BY-PRICE-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `promo_by_price_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части, подтверждённый rule-based smoke и границы первого рабочего checkpoint."
source_basis:
  - "migration/57_promo_by_price_block_contract.md"
  - "migration/60_promo_by_price_block_legacy_sample_source.md"
  - "artifacts/promo_by_price_block/evidence/initial__promo-by-price__evidence.md"
  - "apps/promo_by_price_block_smoke.py"
  - "apps/promo_by_price_block_rule_smoke.py"
related_modules:
  - "packages/contracts/promo_by_price_block.py"
  - "packages/adapters/promo_by_price_block.py"
  - "packages/application/promo_by_price_block.py"
related_tables:
  - "RAW_PROMO_RULES"
  - "DATA"
related_endpoints: []
related_runners:
  - "apps/promo_by_price_block_smoke.py"
  - "apps/promo_by_price_block_rule_smoke.py"
related_docs:
  - "migration/57_promo_by_price_block_contract.md"
  - "migration/58_promo_by_price_block_parity_matrix.md"
  - "migration/59_promo_by_price_block_evidence_checklist.md"
  - "migration/60_promo_by_price_block_legacy_sample_source.md"
  - "artifacts/promo_by_price_block/evidence/initial__promo-by-price__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Создан как канонический модульный документ в рамках первого bounded checkpoint для rule-based apply блока `promo_by_price_block`."
---

# 1. Идентификатор и статус

- `module_id`: `promo_by_price_block`
- `family`: `rule-based`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как связка:
  - `RAW_PROMO_RULES`
  - current `DATA.price_seller_discounted`
  - `77_plugins_promo_by_price.js`
- Legacy смысл результата задаётся на уровне `date + nmId`.
- Ключевая semantics:
  - active rule определяется как `start_date <= date <= end_date`
  - `promo_entry_price_best = max(plan_price)` по active rules
  - `promo_count_by_price = count(plan_price where price_seller_discounted < plan_price + 0.5)`
  - `promo_participation = 1`, если `promo_count_by_price > 0`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `date_from`
  - `date_to`
  - `count`
  - `items[]` c полями `date`, `nm_id`, `promo_count_by_price`, `promo_entry_price_best`, `promo_participation`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded historical promo apply snapshot для requested `nmId` без переноса spreadsheet runtime.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/promo_by_price_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/promo_by_price_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/promo_by_price_block/target/normal__template__target__fixture.json`
  - `artifacts/promo_by_price_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/promo_by_price_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/promo_by_price_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/promo_by_price_block/evidence/initial__promo-by-price__evidence.md`
- rule-source fixture:
  - `artifacts/promo_by_price_block/rule_source/normal__template__rules__fixture.json`

# 5. Кодовые части

- contracts: `packages/contracts/promo_by_price_block.py`
- adapters: `packages/adapters/promo_by_price_block.py`
- application: `packages/application/promo_by_price_block.py`
- artifact-backed smoke: `apps/promo_by_price_block_smoke.py`
- fixture-backed rule-source smoke: `apps/promo_by_price_block_rule_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/promo_by_price_block_smoke.py`.
- Fixture-backed rule-source smoke подтверждён через `apps/promo_by_price_block_rule_smoke.py`.
- Для этого типа модуля canonical checkpoint не требует server-side HTTP smoke.

# 7. Что уже доказано по модулю

- Legacy-source и источник `nmId` зафиксированы без догадок.
- Parity подтверждена для `normal-case` и `empty/no-rule`.
- Rule-source logic повторяет текущую apply semantics без переноса spreadsheet runtime.
- Блок даёт рабочий bounded checkpoint на bootstrap sample set.

# 8. Что пока не является частью финальной production-сборки

- live spreadsheet/runtime orchestration;
- перенос `CONFIG/METRICS/FORMULAS`;
- jobs/API/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
