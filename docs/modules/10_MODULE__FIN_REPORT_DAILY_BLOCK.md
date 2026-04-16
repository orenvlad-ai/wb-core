---
title: "Модуль: fin_report_daily_block"
doc_id: "WB-CORE-MODULE-10-FIN-REPORT-DAILY-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `fin_report_daily_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для daily financial snapshot."
source_basis:
  - "migration/53_fin_report_daily_block_contract.md"
  - "migration/56_fin_report_daily_block_legacy_sample_source.md"
  - "artifacts/fin_report_daily_block/evidence/initial__fin-report-daily__evidence.md"
  - "apps/fin_report_daily_block_smoke.py"
  - "apps/fin_report_daily_block_http_smoke.py"
related_modules:
  - "packages/contracts/fin_report_daily_block.py"
  - "packages/adapters/fin_report_daily_block.py"
  - "packages/application/fin_report_daily_block.py"
related_tables: []
related_endpoints:
  - "GET /api/v5/supplier/reportDetailByPeriod?period=daily"
related_runners:
  - "apps/fin_report_daily_block_smoke.py"
  - "apps/fin_report_daily_block_http_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/53_fin_report_daily_block_contract.md"
  - "migration/54_fin_report_daily_block_parity_matrix.md"
  - "migration/55_fin_report_daily_block_evidence_checklist.md"
  - "migration/56_fin_report_daily_block_legacy_sample_source.md"
  - "artifacts/fin_report_daily_block/evidence/initial__fin-report-daily__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует merged official-api checkpoint блока `fin_report_daily_block`."
---

# 1. Идентификатор и статус

- `module_id`: `fin_report_daily_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `reportDetailByPeriod(period=daily)` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId` и special row `nmId = 0`.
- Ключевая semantics:
  - постраничный поток через `rrdid`
  - deadline/max-pages guardrail
  - sale/return normalization для `fin_buyout_rub` и `fin_commission_wb_portal`
  - отдельная total storage row `nmId = 0`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с десятью `fin_*` полями
  - `storage_total`
- Special row semantics:
  - `storage_total.nm_id = 0`
  - `storage_total.fin_storage_fee_total`
- Целевой смысл блока: bounded daily financial snapshot с сохранением pagination и total-row semantics.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/fin_report_daily_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/fin_report_daily_block/legacy/storage_total__template__legacy__fixture.json`
- target samples:
  - `artifacts/fin_report_daily_block/target/normal__template__target__fixture.json`
  - `artifacts/fin_report_daily_block/target/storage_total__template__target__fixture.json`
- parity:
  - `artifacts/fin_report_daily_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/fin_report_daily_block/parity/storage_total__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/fin_report_daily_block/evidence/initial__fin-report-daily__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/fin_report_daily_block.py`
- adapters: `packages/adapters/fin_report_daily_block.py`
- application: `packages/application/fin_report_daily_block.py`
- artifact-backed smoke: `apps/fin_report_daily_block_smoke.py`
- authoritative server-side smoke: `apps/fin_report_daily_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/fin_report_daily_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/fin_report_daily_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `storage-total`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`, `storage_total -> 0.0`.
- Прежний paused auth-blocker снят заменой server-side canonical WB token path.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
