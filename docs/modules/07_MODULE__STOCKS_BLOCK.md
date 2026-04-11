---
title: "Модуль: stocks_block"
doc_id: "WB-CORE-MODULE-07-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `stocks_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для `stocks`."
source_basis:
  - "migration/41_stocks_block_contract.md"
  - "migration/44_stocks_block_legacy_sample_source.md"
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
  - "apps/stocks_block_smoke.py"
  - "apps/stocks_block_http_smoke.py"
related_modules:
  - "packages/contracts/stocks_block.py"
  - "packages/adapters/stocks_block.py"
  - "packages/application/stocks_block.py"
related_tables: []
related_endpoints:
  - "POST /api/v2/stocks-report/products/sizes"
related_runners:
  - "apps/stocks_block_smoke.py"
  - "apps/stocks_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/41_stocks_block_contract.md"
  - "migration/42_stocks_block_parity_matrix.md"
  - "migration/43_stocks_block_evidence_checklist.md"
  - "migration/44_stocks_block_legacy_sample_source.md"
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `stocks_block`."
---

# 1. Идентификатор и статус

- `module_id`: `stocks_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `POST /api/v2/stocks-report/products/sizes` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - latest `snapshot_ts` per `(date,nmId)` считается authoritative
  - `stock_total` суммирует `stockCount` по всем offices
  - региональные `stock_*` строятся по текущему RU region mapping
  - publish guard не допускает success при неполном coverage requested `nmId`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `stock_total` и региональными `stock_*`
- Incomplete shape:
  - `kind = "incomplete"`
  - `requested_count`
  - `covered_count`
  - `missing_nm_ids`
- Целевой смысл блока: bounded stocks snapshot с сохранением coverage guard без буквального переноса Apps Script cursor/staging.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/stocks_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/stocks_block/legacy/partial__template__legacy__fixture.json`
- target samples:
  - `artifacts/stocks_block/target/normal__template__target__fixture.json`
  - `artifacts/stocks_block/target/partial__template__target__fixture.json`
- parity:
  - `artifacts/stocks_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/stocks_block/parity/partial__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/stocks_block/evidence/initial__stocks__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/stocks_block.py`
- adapters: `packages/adapters/stocks_block.py`
- application: `packages/application/stocks_block.py`
- artifact-backed smoke: `apps/stocks_block_smoke.py`
- authoritative server-side smoke: `apps/stocks_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/stocks_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/stocks_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `partial-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Coverage guard сохранён в bounded форме через `incomplete` result.

# 8. Что пока не является частью финальной production-сборки

- буквальный перенос Apps Script cursor/staging;
- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
