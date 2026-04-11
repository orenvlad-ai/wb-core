---
title: "Модуль: ads_bids_block"
doc_id: "WB-CORE-MODULE-06-ADS-BIDS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `ads_bids_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для `ads_bids`."
source_basis:
  - "migration/37_ads_bids_block_contract.md"
  - "migration/40_ads_bids_block_legacy_sample_source.md"
  - "artifacts/ads_bids_block/evidence/initial__ads-bids__evidence.md"
  - "apps/ads_bids_block_smoke.py"
  - "apps/ads_bids_block_http_smoke.py"
related_modules:
  - "packages/contracts/ads_bids_block.py"
  - "packages/adapters/ads_bids_block.py"
  - "packages/application/ads_bids_block.py"
related_tables: []
related_endpoints:
  - "GET /adv/v1/promotion/count"
  - "GET /api/advert/v2/adverts?ids=...&statuses=9"
related_runners:
  - "apps/ads_bids_block_smoke.py"
  - "apps/ads_bids_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/37_ads_bids_block_contract.md"
  - "migration/38_ads_bids_block_parity_matrix.md"
  - "migration/39_ads_bids_block_evidence_checklist.md"
  - "migration/40_ads_bids_block_legacy_sample_source.md"
  - "artifacts/ads_bids_block/evidence/initial__ads-bids__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `ads_bids_block`."
---

# 1. Идентификатор и статус

- `module_id`: `ads_bids_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `promotion/count` + `adverts?statuses=9` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - apply берёт latest `fetched_at`
  - по `(date,nmId,placement)` сохраняется `max(bid_kopecks)`
  - target поля выражаются в рублях: `ads_bid_search`, `ads_bid_recommendations`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `nm_id`, `ads_bid_search`, `ads_bid_recommendations`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded bids snapshot для active campaigns, отфильтрованный по requested `nmId`.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/ads_bids_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/ads_bids_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/ads_bids_block/target/normal__template__target__fixture.json`
  - `artifacts/ads_bids_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/ads_bids_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/ads_bids_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/ads_bids_block/evidence/initial__ads-bids__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/ads_bids_block.py`
- adapters: `packages/adapters/ads_bids_block.py`
- application: `packages/application/ads_bids_block.py`
- artifact-backed smoke: `apps/ads_bids_block_smoke.py`
- authoritative server-side smoke: `apps/ads_bids_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/ads_bids_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/ads_bids_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Empty-case сохраняется как честный block-level empty после фильтрации active bid rows по `nmId`.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
