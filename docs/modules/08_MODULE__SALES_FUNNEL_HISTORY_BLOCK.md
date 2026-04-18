---
title: "Модуль: sales_funnel_history_block"
doc_id: "WB-CORE-MODULE-08-SALES-FUNNEL-HISTORY-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `sales_funnel_history_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для historical sales funnel."
source_basis:
  - "migration/45_sales_funnel_history_block_contract.md"
  - "migration/48_sales_funnel_history_block_legacy_sample_source.md"
  - "artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md"
  - "apps/sales_funnel_history_block_smoke.py"
  - "apps/sales_funnel_history_block_http_smoke.py"
related_modules:
  - "packages/contracts/sales_funnel_history_block.py"
  - "packages/adapters/sales_funnel_history_block.py"
  - "packages/application/sales_funnel_history_block.py"
related_tables: []
related_endpoints:
  - "POST /api/analytics/v3/sales-funnel/products/history"
related_runners:
  - "apps/sales_funnel_history_block_smoke.py"
  - "apps/sales_funnel_history_block_http_smoke.py"
  - "apps/sales_funnel_history_block_batching_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/45_sales_funnel_history_block_contract.md"
  - "migration/46_sales_funnel_history_block_parity_matrix.md"
  - "migration/47_sales_funnel_history_block_evidence_checklist.md"
  - "migration/48_sales_funnel_history_block_legacy_sample_source.md"
  - "artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `sales_funnel_history_block`, включая bounded batching по `nmIds`, pacing/retry и date-window chunking для длинных historical periods."
---

# 1. Идентификатор и статус

- `module_id`: `sales_funnel_history_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `POST /api/analytics/v3/sales-funnel/products/history` + current RAW/APPLY semantics.
- Результат задаётся на уровне `date + nmId + metric`.
- Ключевая semantics:
  - apply берёт latest `fetched_at` per `(date,nmId,metric)`
  - percent metrics `addToCartConversion`, `cartToOrderConversion`, `buyoutPercent` нормализуются делением на `100`
  - empty-case определяется как item с пустым `history`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `date_from`
  - `date_to`
  - `count`
  - `items[]` с `date`, `nm_id`, `metric`, `value`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded historical sales funnel snapshot без переноса старой orchestration-логики.
- Current HTTP adapter keeps the same external request/response contract, but splits long periods into bounded date windows before calling official API so larger `date_from/date_to` ranges do not break the operator-facing server flow.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/sales_funnel_history_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/sales_funnel_history_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/sales_funnel_history_block/target/normal__template__target__fixture.json`
  - `artifacts/sales_funnel_history_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/sales_funnel_history_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/sales_funnel_history_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/sales_funnel_history_block.py`
- adapters: `packages/adapters/sales_funnel_history_block.py`
- application: `packages/application/sales_funnel_history_block.py`
- artifact-backed smoke: `apps/sales_funnel_history_block_smoke.py`
- authoritative server-side smoke: `apps/sales_funnel_history_block_http_smoke.py`
- batching/rate-limit smoke: `apps/sales_funnel_history_block_batching_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/sales_funnel_history_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/sales_funnel_history_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 140`.
- Percent-normalization и latest-`fetched_at` semantics сохранены внутри bounded target contract.
- HTTP adapter не только режет запрос по `nmIds`, но и bounded-chunk'ит длинный date range на несколько day windows с последующим merge без изменения target shape.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
