---
title: "Модуль: sales_funnel_history_block"
doc_id: "WB-CORE-MODULE-08-SALES-FUNNEL-HISTORY-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `sales_funnel_history_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для historical sales funnel, включая current exact-date runtime seam для server-owned factory-order history."
source_basis:
  - "migration/45_sales_funnel_history_block_contract.md"
  - "migration/48_sales_funnel_history_block_legacy_sample_source.md"
  - "artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md"
  - "apps/sales_funnel_history_block_smoke.py"
  - "apps/sales_funnel_history_block_http_smoke.py"
related_modules:
  - "packages/contracts/sales_funnel_history_block.py"
  - "packages/adapters/sales_funnel_history_block.py"
  - "packages/adapters/seller_analytics_csv_report.py"
  - "packages/application/sales_funnel_history_block.py"
  - "packages/application/factory_order_sales_history.py"
related_tables:
  - "temporal_source_snapshots"
related_endpoints:
  - "POST /api/analytics/v3/sales-funnel/products/history"
  - "POST /api/v2/nm-report/downloads [reportType=DETAIL_HISTORY_REPORT]"
  - "GET /api/v2/nm-report/downloads"
  - "GET /api/v2/nm-report/downloads/file/{downloadId}"
related_runners:
  - "apps/sales_funnel_history_block_smoke.py"
  - "apps/sales_funnel_history_block_http_smoke.py"
  - "apps/sales_funnel_history_block_batching_smoke.py"
  - "apps/sales_funnel_history_detail_csv_smoke.py"
  - "apps/factory_order_sales_history_smoke.py"
  - "apps/factory_order_sales_history_reconcile.py"
  - "apps/sheet_vitrina_v1_buyout_mature_backfill.py"
  - "apps/sheet_vitrina_v1_buyout_mature_backfill_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/45_sales_funnel_history_block_contract.md"
  - "migration/46_sales_funnel_history_block_parity_matrix.md"
  - "migration/47_sales_funnel_history_block_evidence_checklist.md"
  - "migration/48_sales_funnel_history_block_legacy_sample_source.md"
  - "artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под immutable Proxy V4: обычный D-6 capture остаётся дешёвым, а exact `2026-07-06..2026-07-12` DETAIL_HISTORY_REPORT reconcile закрывает единственный pre-provenance gap для первого as-of трёхнедельного окна без fallback."
---

# 1. Идентификатор и статус

- `module_id`: `sales_funnel_history_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `POST /api/analytics/v3/sales-funnel/products/history` + current RAW/APPLY semantics. Current historical official source для периода старше недельной глубины — Seller Analytics CSV `DETAIL_HISTORY_REPORT` через общую create/list/download chain.
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
- Current weekly HTTP adapter keeps the same external request/response contract and splits its supported periods into bounded date/SKU batches. Он не используется для старого reconcile за пределами недельной глубины.
- `DetailHistoryCsvBackedSalesFunnelHistorySource` создаёт один bounded `DETAIL_HISTORY_REPORT` с `nmIDs`, `startDate/endDate`, `timezone=Asia/Yekaterinburg`, `aggregationLevel=day`; общий transport `seller_analytics_csv_report.py` владеет POST create, GET list/poll, GET ZIP/download, CSV decode и `429` retry для stocks и funnel consumers.
- CSV parser требует документированные `nmID`, `dt`, `ordersCount`, `buyoutPercent`, ровно одну строку на каждую requested enabled-SKU/date пару, отвергает duplicate/out-of-scope/non-finite/invalid values и не синтезирует отсутствующие нули. `ordersCount` нормализуется в target metric `orderCount`, `buyoutPercent` — из процентов в fraction через существующий application transform.
- Success payload также пригоден для server-owned exact-date persistence:
  - current factory-order helper split-ит `success.items[]` по `item.date`;
  - дальше каждый exact-date slice может truthfully сохраняться в `temporal_source_snapshots[source_key=sales_funnel_history]` без изменения business contract самого official-api блока.
- Canonical `buyoutPercent` maturity is fixed in `Asia/Yekaterinburg`: `snapshot_date <= current_business_date - 6 calendar days`, inclusive. D0..D-5 is never public. Web Vitrina overwrites every persisted SKU/TOTAL value in that immature band with blank/`—`, including old zero and non-zero values. Mature SKU projection accepts only an exact-date official snapshot whose persisted `captured_at` proves capture on/after that date's D-6 boundary and whose payload covers every enabled SKU; polluted ready rows are not a fallback. Daily `TOTAL|buyoutPercent` remains `SUM(buyoutPercent * orderCount) / SUM(orderCount)` over enabled SKU with positive orders; no valid denominator is blank, `buyoutCount` is never a weight and legacy arithmetic `avg_buyoutPercent` remains disabled/nonpublic.
- The fixed threshold is grounded in the `2026-08-09` diagnostic: mature `2026-07-01..2026-07-21` weighted `96.22%` on `63,804` orders; live age-5 refetch `2026-08-04` weighted `96.50%` with zero anomalous SKU; age 4 weighted `92.51%` with seven anomalous SKU representing `14.5%` of order weight. The observed stabilization boundary is D-5, and the public D-6 rule deliberately retains one calendar day of safety margin instead of adding a dynamic stability estimator.
- Each business day the ordinary refresh owns one cheap mature reconcile: inspect only D-7..D-6, request D-6 when mature proof is absent and use D-7 only as bounded catch-up. Persisted exact-date payload + capture business date + enabled-SKU coverage is the idempotency proof, so repeated same-day manual refreshes do not repeat mature upstream load. An old D-6 candidate captured while immature is replaced by the fresh official payload; after successful mature capture it is not requested again. This seam does not introduce a scheduler, watcher or orchestration state machine.
- Settings row `Расчётный выкуп (подтверждённый)` uses the same `SUM(buyoutPercent * orderCount) / SUM(orderCount)` formula for each of exactly the latest three closed Monday-Sunday slots and for their combined value. A week is READY only when all seven dates are within the trusted cutoff and every enabled SKU-day has valid mature coverage. Immature/missing/invalid data blanks that week cell and excludes the whole week from combined; it is never treated as zero, partially counted or replaced by an older week outside the three slots. Combined is published from every READY week currently present in those slots (one to three), by direct SKU-day SUM/SUM with its exact positive `orderCount` weight; a proven zero buyout remains a valid zero. With zero READY weeks combined is blank. The current open week is always excluded. Coverage text lists the contributing and pending ranges. This reference never changes a Proxy formula, `buyout_rate` or saved calculation parameter.
- Guarded official reconciles have already replaced `2026-07-13..2026-08-03`; production readback therefore proves mature data from `2026-07-13` forward. Proxy V4 as-of initialization on fixed boundary `2026-08-01` independently requires the aligned buyout window `2026-07-06..2026-07-26`, so the remaining exact source gap is `2026-07-06..2026-07-12`. The current one-time runner in `apps/sheet_vitrina_v1_buyout_mature_backfill.py` is bounded to exactly those seven dates and must official-refetch all `33 × 7 = 231` enabled-SKU/date pairs instead of weakening D-6 or accepting next-day captures as fallback. Dry-run is the default and writes a private machine-readable reviewed manifest outside Git; explicit apply requires its SHA-256, exact deployed-runtime SHA marker, human approval reference, exact current targets/pre-change digest, coherent verified SQLite backup, atomic allowlisted replacement, non-target digest equality, idempotent desired-content proof and post-apply reconciliation evidence. Manifest v3 records the official CSV endpoint chain, report type, download/report provenance, CSV digest and exact coverage counts separately from the desired business-content digest. If any enabled-SKU/date pair is absent, the manifest is blocked.
- Bounded production capability proof on `2026-08-09` already confirmed `DETAIL_HISTORY_REPORT` entitlement and strict full coverage for later repaired windows. The same official transport must separately prove all `231/231` requested pairs for `2026-07-06..2026-07-12` before the new manifest can be approved or applied; no legacy sheet data, capture-timestamp rewrite, partial week or invented value may initialize V4.
- If a bounded historical window is migrated from live `DATA_VITRINA`, that sheet acts only as one-time migration input for exact-date replacement/reconcile; ongoing source of truth remains official API payload + server-owned runtime snapshots.
- If the upstream source rejects older start days relative to current business date, the server-owned consumer must surface that boundary truthfully instead of inventing backfill or approximate history.

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
- shared Seller Analytics CSV transport: `packages/adapters/seller_analytics_csv_report.py`
- application: `packages/application/sales_funnel_history_block.py`
- runtime-backed consumer helper: `packages/application/factory_order_sales_history.py`
- artifact-backed smoke: `apps/sales_funnel_history_block_smoke.py`
- authoritative server-side smoke: `apps/sales_funnel_history_block_http_smoke.py`
- batching/rate-limit smoke: `apps/sales_funnel_history_block_batching_smoke.py`
- DETAIL_HISTORY_REPORT transport/normalization/coverage smoke: `apps/sales_funnel_history_detail_csv_smoke.py`
- runtime/reconcile smoke: `apps/factory_order_sales_history_smoke.py`
- bounded reconcile runner: `apps/factory_order_sales_history_reconcile.py`
- guarded mature-buyout reconcile runner: `apps/sheet_vitrina_v1_buyout_mature_backfill.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/sales_funnel_history_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/sales_funnel_history_block_http_smoke.py`.
- Official `DETAIL_HISTORY_REPORT` request schema, percent/order normalization and strict incomplete-coverage failure подтверждены через `apps/sales_funnel_history_detail_csv_smoke.py`.
- Exact-date runtime split/reconcile smoke подтверждён через `apps/factory_order_sales_history_smoke.py`.
- D-6 maturity, overwrite/idempotency/catch-up, immutable read masking, complete-week partial-window aggregation and guarded historical apply are checked by `apps/sheet_vitrina_v1_buyout_percent_smoke.py` and `apps/sheet_vitrina_v1_buyout_mature_backfill_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 140`.
- Percent-normalization и latest-`fetched_at` semantics сохранены внутри bounded target contract.
- Weekly HTTP adapter режет запрос по `nmIds` и bounded day windows без изменения target shape; historical CSV adapter сохраняет тот же target shape для документированной глубины до года.
- Тот же target shape достаточно строг, чтобы server-owned consumers могли:
  - materialize-ить exact-date snapshots в runtime;
  - делать truthful window replacement/reconcile без merge с polluted rows;
  - затем считать покрытые averaging windows без fixed `<= 7` product rule.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
