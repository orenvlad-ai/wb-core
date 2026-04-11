---
title: "Модуль: spp_block"
doc_id: "WB-CORE-MODULE-05-SPP-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `spp_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для `spp`."
source_basis:
  - "migration/33_spp_block_contract.md"
  - "migration/36_spp_block_legacy_sample_source.md"
  - "artifacts/spp_block/evidence/initial__spp__evidence.md"
  - "apps/spp_block_smoke.py"
  - "apps/spp_block_http_smoke.py"
related_modules:
  - "packages/contracts/spp_block.py"
  - "packages/adapters/spp_block.py"
  - "packages/application/spp_block.py"
related_tables: []
related_endpoints:
  - "GET /api/v1/supplier/sales?dateFrom=..."
related_runners:
  - "apps/spp_block_smoke.py"
  - "apps/spp_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/33_spp_block_contract.md"
  - "migration/34_spp_block_parity_matrix.md"
  - "migration/35_spp_block_evidence_checklist.md"
  - "migration/36_spp_block_legacy_sample_source.md"
  - "artifacts/spp_block/evidence/initial__spp__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `spp_block`."
---

# 1. Идентификатор и статус

- `module_id`: `spp_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `GET /api/v1/supplier/sales?dateFrom=...` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - raw оставляет sales rows только за `snapshot_date`
  - `spp` нормализуется в долю: `>1 => /100`
  - apply пишет в `DATA` среднее `spp_avg` по sales rows

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `nm_id`, `spp`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded yesterday-style snapshot `spp` по requested `nmId`.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/spp_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/spp_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/spp_block/target/normal__template__target__fixture.json`
  - `artifacts/spp_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/spp_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/spp_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/spp_block/evidence/initial__spp__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/spp_block.py`
- adapters: `packages/adapters/spp_block.py`
- application: `packages/application/spp_block.py`
- artifact-backed smoke: `apps/spp_block_smoke.py`
- authoritative server-side smoke: `apps/spp_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/spp_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/spp_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Live smoke ограничен normal-case из-за upstream rate limit `1 request / minute`; empty-case честно доказывается artifact-backed path.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
