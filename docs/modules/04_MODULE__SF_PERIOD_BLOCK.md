---
title: "Модуль: sf_period_block"
doc_id: "WB-CORE-MODULE-04-SF-PERIOD-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `sf_period_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для snapshot `sf_period`."
source_basis:
  - "migration/29_sf_period_block_contract.md"
  - "migration/32_sf_period_block_legacy_sample_source.md"
  - "artifacts/sf_period_block/evidence/initial__sf-period__evidence.md"
  - "apps/sf_period_block_smoke.py"
  - "apps/sf_period_block_http_smoke.py"
related_modules:
  - "packages/contracts/sf_period_block.py"
  - "packages/adapters/sf_period_block.py"
  - "packages/application/sf_period_block.py"
related_tables: []
related_endpoints:
  - "POST /api/analytics/v3/sales-funnel/products"
related_runners:
  - "apps/sf_period_block_smoke.py"
  - "apps/sf_period_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/29_sf_period_block_contract.md"
  - "migration/30_sf_period_block_parity_matrix.md"
  - "migration/31_sf_period_block_evidence_checklist.md"
  - "migration/32_sf_period_block_legacy_sample_source.md"
  - "artifacts/sf_period_block/evidence/initial__sf-period__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `sf_period_block`."
---

# 1. Идентификатор и статус

- `module_id`: `sf_period_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `POST /api/analytics/v3/sales-funnel/products` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - `localization_percent` берётся из `statistic.selected.localizationPercent`
  - `feedback_rating` берётся из `product.feedbackRating`
  - apply выбирает latest `fetched_at` для пары `date|nmId`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `nm_id`, `localization_percent`, `feedback_rating`
- Честный domain-level empty/not-found в checkpoint отдельно не фиксируется.
- Целевой смысл блока: bounded period snapshot для downstream полей `localizationPercent` и `feedbackRating`.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/sf_period_block/legacy/normal__template__legacy__fixture.json`
- target samples:
  - `artifacts/sf_period_block/target/normal__template__target__fixture.json`
- parity:
  - `artifacts/sf_period_block/parity/normal__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/sf_period_block/evidence/initial__sf-period__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/sf_period_block.py`
- adapters: `packages/adapters/sf_period_block.py`
- application: `packages/application/sf_period_block.py`
- artifact-backed smoke: `apps/sf_period_block_smoke.py`
- authoritative server-side smoke: `apps/sf_period_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/sf_period_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/sf_period_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Normal-case parity подтверждена на bootstrap sample set.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `count -> 2`.
- Честный empty/not-found case остаётся вне checkpoint, потому что upstream не дал безопасный domain-level empty response.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
