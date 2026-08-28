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
  - "migration/164_finance_daily_supported_transport_and_recovery.md"
related_modules:
  - "packages/contracts/fin_report_daily_block.py"
  - "packages/adapters/fin_report_daily_block.py"
  - "packages/application/fin_report_daily_block.py"
related_tables: []
related_endpoints:
  - "POST /api/finance/v1/sales-reports/detailed"
related_runners:
  - "apps/fin_report_daily_block_smoke.py"
  - "apps/fin_report_daily_block_http_smoke.py"
  - "apps/fin_report_daily_finance_transport_smoke.py"
  - "apps/finance_daily_historical_recovery.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/53_fin_report_daily_block_contract.md"
  - "migration/54_fin_report_daily_block_parity_matrix.md"
  - "migration/55_fin_report_daily_block_evidence_checklist.md"
  - "migration/56_fin_report_daily_block_legacy_sample_source.md"
  - "artifacts/fin_report_daily_block/evidence/initial__fin-report-daily__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Daily Vitrina переведена на поддерживаемый Finance POST, общий server-owned rate gate и exact-date recovery."
---

# 1. Идентификатор и статус

- `module_id`: `fin_report_daily_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy semantics сохраняются, но transport source теперь только поддерживаемый
  `POST /api/finance/v1/sales-reports/detailed` с exact `dateFrom=dateTo`,
  `period=daily`, `limit<=100000` и cursor `rrdId`.
- Результат задаётся на уровне `snapshot_date + nmId` и special row `nmId = 0`.
- Ключевая semantics:
  - постраничный поток через `rrdid`
  - deadline/max-pages guardrail
  - sale/return normalization для `fin_buyout_rub` и `fin_commission_wb_portal`
  - отдельная total storage row `nmId = 0`

GET `reportDetailByPeriod` больше не является live source этого блока.
Успешная acquisition завершается только документированным `HTTP 204` после
последней `HTTP 200` страницы. `HTTP 200` с пустым массивом, stuck cursor,
deadline, transport failure и любое прерывание pagination — typed incomplete
result; частичные строки не передаются application/publication слою.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с десятью `fin_*` полями
  - `storage_total`
  - privacy-safe `diagnostics` с endpoint/period/pages/final cursor/terminal 204,
    source digest и `requested/covered` evidence
- Special row semantics:
  - `storage_total.nm_id = 0`
  - `storage_total.fin_storage_fee_total`
- Целевой смысл блока: bounded daily financial snapshot с сохранением pagination и total-row semantics.
- CamelCase Finance fields маппятся в прежний daily result contract. Sale/return
  sign rules для buyout и WB commission сохранены. `acquiringFee` складывается
  в delivered sign, включая return rows; weekly classifier sign rule сюда не
  импортируется. `paidStorage` TOTAL складывается по всем exact-date seller
  report rows, в том числе вне 33 target SKU.

# 4. Shared rate gate и failure semantics

`packages/adapters/wb_finance_api.py` — один shared POST client weekly/daily.
Один interprocess lease на canonical runtime удерживает single-flight всей
pagination session и резервирует минимум 60 секунд перед каждым следующим
request. `Retry-After`, `X-RateLimit-Retry` и `X-RateLimit-Reset` могут только
сдвинуть next-at позже. Это покрывает weekly timer, ordinary/manual/group
Vitrina refresh и closure retry; отдельного browser lock/scheduler нет.

`429` не означает empty report и не retry-ится немедленно. Typed
`rate_limited` сохраняет exact source date/range, period, cursor, completed
pages, allowlisted header hints и `next_retry_at`. Invalid/partial acquisition
не заменяет last-good accepted slot. Exhausted same-day closure остаётся
durably retry-eligible со следующего business day/window, а не забывается.

# 5. Артефакты по модулю

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

# 6. Кодовые части

- contracts: `packages/contracts/fin_report_daily_block.py`
- adapters: `packages/adapters/fin_report_daily_block.py`
- application: `packages/application/fin_report_daily_block.py`
- artifact-backed smoke: `apps/fin_report_daily_block_smoke.py`
- authoritative server-side smoke: `apps/fin_report_daily_block_http_smoke.py`
- shared Finance transport smoke: `apps/fin_report_daily_finance_transport_smoke.py`
- exact recovery runner/smoke: `apps/finance_daily_historical_recovery.py`,
  `apps/finance_daily_historical_recovery_smoke.py`

# 7. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/fin_report_daily_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/fin_report_daily_block_http_smoke.py`.

# 8. Historical recovery contract

Canonical hosted actions `finance-daily-parity`,
`finance-daily-recovery-plan|apply|readback` принимают только exact accepted
dates. Parity для 24–25.08 query-only сравнивает все 171 current ready values с
fresh terminal-204 source после той же canonical six-decimal нормализации,
которую применяет Vitrina при записи numeric cells. Recovery для 26–27.08
работает последовательно по одной date/operation и меняет только 165 SKU
Finance cells + 6 Finance TOTAL.

Private mode-0600 plan вне Git pins deployed SHA, bundle/as-of/snapshot,
33/33 normalized source, pages/final cursor/source digest, 171 before/after
states, plan CAS digests, non-target digest и explicit Proxy-gap exclusion.
Apply использует reviewed normalized aggregates, не refetch-ит upstream,
атомарно обновляет exact ready plan плюс существующие temporal accepted/closure
seams и пишет operation audit (не второй Finance ledger). Query-only readback
проверяет terminal 204, 33/33, 171/171, duplicates, TOTAL, closure, plan and
non-target digests. Overall day health заново выводится из полного STATUS и не
принудительно становится green, если другой source incomplete.

# 9. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `storage-total`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`, `storage_total -> 0.0`.
- Прежний paused auth-blocker снят заменой server-side canonical WB token path.

# 10. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
