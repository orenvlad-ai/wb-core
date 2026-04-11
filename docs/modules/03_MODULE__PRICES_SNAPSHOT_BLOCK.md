---
title: "Модуль: prices_snapshot_block"
doc_id: "WB-CORE-MODULE-03-PRICES-SNAPSHOT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `prices_snapshot_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для prices snapshot."
source_basis:
  - "migration/24_prices_snapshot_block_contract.md"
  - "migration/27_prices_snapshot_block_legacy_sample_source.md"
  - "artifacts/prices_snapshot_block/evidence/initial__prices-snapshot__evidence.md"
  - "apps/prices_snapshot_block_smoke.py"
  - "apps/prices_snapshot_block_http_smoke.py"
related_modules:
  - "packages/contracts/prices_snapshot_block.py"
  - "packages/adapters/prices_snapshot_block.py"
  - "packages/application/prices_snapshot_block.py"
related_tables: []
related_endpoints:
  - "POST /api/v2/list/goods/filter"
related_runners:
  - "apps/prices_snapshot_block_smoke.py"
  - "apps/prices_snapshot_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/24_prices_snapshot_block_contract.md"
  - "migration/25_prices_snapshot_block_parity_matrix.md"
  - "migration/26_prices_snapshot_block_evidence_checklist.md"
  - "migration/27_prices_snapshot_block_legacy_sample_source.md"
  - "artifacts/prices_snapshot_block/evidence/initial__prices-snapshot__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `prices_snapshot_block`."
---

# 1. Идентификатор и статус

- `module_id`: `prices_snapshot_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `POST /api/v2/list/goods/filter` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - `price_seller = min(price)` across sizes
  - `price_seller_discounted = min(discountedPrice)` across sizes
  - latest `snapshot_date` считается canonical snapshot identity

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `nm_id`, `price_seller`, `price_seller_discounted`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded prices snapshot по bootstrap/requested `nmId` без переноса table runtime.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/prices_snapshot_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/prices_snapshot_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/prices_snapshot_block/target/normal__template__target__fixture.json`
  - `artifacts/prices_snapshot_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/prices_snapshot_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/prices_snapshot_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/prices_snapshot_block/evidence/initial__prices-snapshot__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/prices_snapshot_block.py`
- adapters: `packages/adapters/prices_snapshot_block.py`
- application: `packages/application/prices_snapshot_block.py`
- artifact-backed smoke: `apps/prices_snapshot_block_smoke.py`
- authoritative server-side smoke: `apps/prices_snapshot_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/prices_snapshot_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/prices_snapshot_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `empty -> empty`.
- Local transport-problem к upstream признан средовым отличием, а не blocker’ом модуля.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
