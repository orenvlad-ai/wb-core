---
title: "Модуль: ads_compact_block"
doc_id: "WB-CORE-MODULE-09-ADS-COMPACT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `ads_compact_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для compact ads snapshot."
source_basis:
  - "migration/49_ads_compact_block_contract.md"
  - "migration/52_ads_compact_block_legacy_sample_source.md"
  - "artifacts/ads_compact_block/evidence/initial__ads-compact__evidence.md"
  - "apps/ads_compact_block_smoke.py"
  - "apps/ads_compact_block_http_smoke.py"
related_modules:
  - "packages/contracts/ads_compact_block.py"
  - "packages/adapters/ads_compact_block.py"
  - "packages/application/ads_compact_block.py"
related_tables: []
related_endpoints:
  - "GET /adv/v1/promotion/count"
  - "GET /adv/v3/fullstats"
related_runners:
  - "apps/ads_compact_block_smoke.py"
  - "apps/ads_compact_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/49_ads_compact_block_contract.md"
  - "migration/50_ads_compact_block_parity_matrix.md"
  - "migration/51_ads_compact_block_evidence_checklist.md"
  - "migration/52_ads_compact_block_legacy_sample_source.md"
  - "artifacts/ads_compact_block/evidence/initial__ads-compact__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `ads_compact_block`."
---

# 1. Идентификатор и статус

- `module_id`: `ads_compact_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `promotion/count` + `fullstats` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - raw агрегирует nested `days -> apps -> nms`
  - базовые поля `ads_views`, `ads_clicks`, `ads_atbs`, `ads_orders`, `ads_sum`, `ads_sum_price` суммируются по `(snapshot_date, nmId)`
  - apply-level derivation сохраняет `ads_cpc`, `ads_ctr`, `ads_cr`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с базовыми ads-полями и derived `ads_cpc`, `ads_ctr`, `ads_cr`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded compact ads snapshot для requested `nmId` без переноса более широкого ad-runtime.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/ads_compact_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/ads_compact_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/ads_compact_block/target/normal__template__target__fixture.json`
  - `artifacts/ads_compact_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/ads_compact_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/ads_compact_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/ads_compact_block/evidence/initial__ads-compact__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/ads_compact_block.py`
- adapters: `packages/adapters/ads_compact_block.py`
- application: `packages/application/ads_compact_block.py`
- artifact-backed smoke: `apps/ads_compact_block_smoke.py`
- authoritative server-side smoke: `apps/ads_compact_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/ads_compact_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/ads_compact_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Прежнее paused-состояние снято без дополнительных правок кода после замены server-side `WB_TOKEN`.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
