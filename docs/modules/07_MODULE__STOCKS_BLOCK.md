---
title: "Модуль: stocks_block"
doc_id: "WB-CORE-MODULE-07-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `stocks_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для `stocks`, включая current inventory adapter и historical closed-day semantics в `sheet_vitrina_v1`."
source_basis:
  - "migration/41_stocks_block_contract.md"
  - "migration/44_stocks_block_legacy_sample_source.md"
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
  - "apps/stocks_block_smoke.py"
  - "apps/stocks_block_http_smoke.py"
  - "apps/stocks_adapter_contract_smoke.py"
related_modules:
  - "packages/contracts/stocks_block.py"
  - "packages/adapters/stocks_block.py"
  - "packages/adapters/seller_analytics_csv_report.py"
  - "packages/application/stocks_block.py"
  - "packages/application/warehouse_stocks.py"
  - "packages/application/wb_incident_policy.py"
related_tables: []
related_endpoints:
  - "POST /api/analytics/v1/stocks-report/wb-warehouses"
  - "POST /api/v2/nm-report/downloads [reportType=STOCK_HISTORY_DAILY_CSV]"
  - "GET /api/v2/nm-report/downloads"
  - "GET /api/v2/nm-report/downloads/file/{downloadId}"
related_runners:
  - "apps/stocks_block_smoke.py"
  - "apps/stocks_block_region_mapping_smoke.py"
  - "apps/stocks_block_batching_smoke.py"
  - "apps/stocks_block_http_smoke.py"
  - "apps/stocks_adapter_contract_smoke.py"
  - "apps/stocks_historical_csv_smoke.py"
  - "apps/sheet_vitrina_v1_stocks_refresh_smoke.py"
  - "apps/sheet_vitrina_v1_stocks_historical_backfill.py"
  - "apps/wb_incident_policy_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/41_stocks_block_contract.md"
  - "migration/42_stocks_block_parity_matrix.md"
  - "migration/43_stocks_block_evidence_checklist.md"
  - "migration/44_stocks_block_legacy_sample_source.md"
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Общий Seller Analytics CSV transport сохраняет `STOCK_HISTORY_DAILY_CSV` exact-date closed-day semantics. Exact typed aggregate sentinel WB не переписывает raw evidence/digest: exact SKU/TOTAL сохраняются, а недоказанные warehouse/region и incident значения остаются blanks."
---

# 1. Идентификатор и статус

- `module_id`: `stocks_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Current checkpoint и bounded semantics

- Исторический repo-checkpoint до этого fix использовал `POST /api/v2/stocks-report/products/sizes` per `nmId`; на bundle с десятками enabled SKU такой fan-out мог приходить в `429`.
- Current main-confirmed official paths:
  - `POST /api/analytics/v1/stocks-report/wb-warehouses` c batched `nmIds`, `limit/offset` pagination и analytics-capable token;
  - Seller Analytics CSV chain `POST /api/v2/nm-report/downloads` + `GET /api/v2/nm-report/downloads` + `GET /api/v2/nm-report/downloads/file/{downloadId}` with `reportType=STOCK_HISTORY_DAILY_CSV`.
- Current canonical runtime secret path для official stocks adapter: `WB_API_TOKEN`.
- Общая transport-граница `packages/adapters/seller_analytics_csv_report.py` владеет create/list/poll/download/ZIP/CSV decode и bounded `429` retry; `stocks_block.py` передаёт в неё только `STOCK_HISTORY_DAILY_CSV` params и сохраняет прежнюю stocks-specific нормализацию.
- Current `wb-warehouses` endpoint остаётся live inventory source для factory/WB supply flows и bounded metadata bridge `OfficeName -> regionName` при historical CSV normalization.
- The same current adapter is the only `Склад WB` opening-snapshot source. Its raw payload now includes exact UTC `data.fetched_at`; the warehouse cutover stores that timestamp, requested/covered nmID counts, payload digest and per-warehouse raw rows. This reuse does not switch `sheet_vitrina_v1` back from the historical closed-day semantics described below.
- В bounded `sheet_vitrina_v1` contour `stocks` теперь классифицируется как WB API date/period-capable source:
  - `stocks[yesterday_closed]` materialize-ит authoritative exact-date snapshot из `STOCK_HISTORY_DAILY_CSV`;
  - success payload для exact-date closed day сохраняется в `temporal_source_snapshots[source_key=stocks]` и читается runtime-first;
  - `stocks[today_current]` в current `sheet_vitrina_v1` contour больше не считается required same-day success condition и stays truthful `not_available`/blank instead of invented intraday stocks;
  - source-level and aggregate semantic status must stay green when `stocks[yesterday_closed]` is confirmed and only non-required `stocks[today_current]` is blank.
- Ключевая semantics:
  - historical CSV day column считается authoritative stocks truth на закрытые сутки;
  - exact-date `snapshot_date` в success обязан совпадать с requested closed day;
  - latest fetched `snapshot_ts` per `nmId` внутри exact-date payload считается authoritative;
  - `stock_total` суммирует `quantity` по всем WB warehouses / chart variants, которые вернул endpoint;
  - региональные `stock_*` строятся по текущему RU region mapping с нормализацией legacy/current alias-ов `Южный +/и Северо-Кавказский` и `Дальневосточный +/и Сибирский`;
  - supply planning additionally retains `warehouseId`, `warehouseName`, `regionName` and quantity as `warehouse_rows[]`. Exact Central registry IDs aggregate into `stock_ru_central_north/east/south` without changing canonical `stock_ru_central`;
  - historical CSV uses `OfficeName`; when warehouseID is absent, Central planning classification allows only exact canonical names/explicit aliases. Электросталь and Котовск remain East history, while SC/specialised and unknown rows remain excluded/unmapped rather than guessed;
  - `planning_reconciliation` proves `legacy_central_total = three zone total + central_unmapped_total + central_excluded_total + difference`; expected difference is zero;
  - quantity из raw regions/warehouses вне configured district map не invent-ится в district rows: она остаётся внутри `stock_total` и surface-ится в `StocksSuccess.detail` / `STATUS.stocks[yesterday_closed].note`;
  - publish guard не допускает success при неполном coverage requested `nmId`;
  - current `wb-warehouses` adapter по-прежнему уважает `X-Ratelimit-Retry` / `X-Ratelimit-Reset`, использует per-seller limiter и после bounded retry budget не превращается в fake-success внутри source.
  - production evidence фиксирует специальный official bucket `warehouseId=0`, `warehouseName=Остальные`: он может нести `inWayToClient`/`inWayFromClient` при нулевом physical quantity и потому сохраняется в WB contour/raw audit; для дедупликации его identity включает warehouse name + region. Любой другой zero/negative/invalid `warehouseId` остаётся fail-closed, а bounded error evidence фиксирует только allowlisted context и digest строки.
  - official current response additionally accepts only the exact typed WB aggregate sentinel `warehouseId=-999999`, `warehouseName=Склад WB`, `regionName=Склад WB`. The original source rows and their digest remain unchanged evidence; a separate normalization envelope maps the sentinel to the service bucket only for SKU-total calculation and marks `warehouse_granularity_complete=false`. Float, bool, string, partial-name and every other negative-ID variant remain invalid.
  - historical `OfficeName=Склад WB` has the same aggregate-only meaning. A date containing that row is incomplete even when concrete offices are mixed into the same CSV; the aggregate quantity remains in exact SKU/TOTAL `stock_total` and is never allocated through a persisted `OfficeName -> regionName` fallback. Regional stock and incident actual/excluded/effective cells stay explicit blanks, not zeros.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `stock_total`, canonical regional `stock_*` and additive Central planning-zone stock fields
  - `warehouse_rows[]` with exact identity/classification evidence and `planning_reconciliation`
  - `warehouse_granularity_complete`, which is false when the source proves only an aggregate WB bucket rather than warehouse-level allocation
  - `detail` для honest note по unmapped raw regions, если часть quantity не попала ни в один configured district bucket
- Incomplete shape:
  - `kind = "incomplete"`
  - `requested_count`
  - `covered_count`
  - `missing_nm_ids`
- Целевой смысл блока: bounded stocks snapshot с сохранением coverage guard без буквального переноса Apps Script cursor/staging.
- Для two-day sheet read model блок обязан оставаться честным: required `yesterday_closed` читается только из authoritative exact-date historical path/runtime cache, while `today_current` stays blank/`not_available` and is not filled through surrogate current values.

## 3.1 Seller-level incident policy and shared stock projection

Canonical stock snapshots remain immutable. `wb_incident_policy` resolves one append-only seller/account revision for the exact snapshot date, then strict `build_incident_stock_projection` publishes three projections per nmID and canonical region: fact, physical quantity on incident warehouses, and operational/effective quantity. Supply and SKU Management consume this strict server projection and continue to require complete pagination plus source digest; browsers and page calculators never subtract warehouses independently.

Web Vitrina alone calls the separately named `build_vitrina_incident_stock_projection` information adapter. For an already accepted incomplete/digestless historical payload it projects only received rows, leaves unprovable triples blank, validates every published SKU/region/TOTAL triple and emits provisional quality/evidence. Its deterministic accepted-payload digest is cache identity only, not upstream completeness evidence. This adapter is intentionally absent from Supply/SKU business-action imports. Confirmed projections remain keyed by seller/date/source digest/policy revision; provisional Vitrina projections use seller/date/accepted-payload digest/policy revision under a separate cache namespace.

The low-level `build_wb_warehouse_exclusion` arithmetic accepts stable numeric warehouse IDs. It subtracts only `StocksWarehouseRow.quantity`; `in_way_to_client` and `in_way_from_client` remain factual WB-contour evidence and are never excluded without a separate exact attribution contract. Regional subtraction uses only the row's canonical `region_name`, while Supply planning-zone fields use only its canonical `planning_zone_key`; unmapped quantity can affect fact total but never an invented region or zone. An active non-empty policy requires complete pagination and a non-empty snapshot digest. Current rows use exact numeric IDs. Historical `OfficeName` rows can be mapped only through the exact, unambiguous name/ID identities captured in the applied policy revision; missing identity evidence fails closed.

Policy revisions carry `active`, warehouse IDs and identities, reason, `effective_from`, optional `effective_to`, status, actor and audit timestamp. A revision is applied only inside its interval. Before the first revision, incident-aware metric rows remain unmaterialized; after an inactive/resolved revision or an ended interval, current/future operational quantity equals fact while already published exact-date incident history remains auditable. Existing per-user `wb_warehouse_exclusions` values are read as a non-mutating migration input; one consistent legacy set remains current-compatible until explicit Apply, while conflicting sets are preserved and disabled fail-closed.

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
- shared Seller Analytics CSV transport: `packages/adapters/seller_analytics_csv_report.py`
- application: `packages/application/stocks_block.py`
- official token boundary: `packages/adapters/official_api_runtime.py` with canonical env key `WB_API_TOKEN`
- artifact-backed smoke: `apps/stocks_block_smoke.py`
- region normalization smoke: `apps/stocks_block_region_mapping_smoke.py`
- targeted batching/rate-limit smoke: `apps/stocks_block_batching_smoke.py`
- authoritative server-side smoke: `apps/stocks_block_http_smoke.py`
- historical CSV smoke: `apps/stocks_historical_csv_smoke.py`
- refresh integration smoke: `apps/sheet_vitrina_v1_stocks_refresh_smoke.py`
- one-off runtime backfill runner: `apps/sheet_vitrina_v1_stocks_historical_backfill.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/stocks_block_smoke.py`.
- Alias normalization + unmapped-note semantics подтверждены через `apps/stocks_block_region_mapping_smoke.py`.
- Batching + cache + `429` retry/exhaustion подтверждены через `apps/stocks_block_batching_smoke.py`.
- Historical CSV create/poll/download/parse path подтверждён через `apps/stocks_historical_csv_smoke.py`.
- Exact typed aggregate sentinel, raw-evidence digest, mixed historical granularity and 33-SKU/TOTAL temporal live-plan semantics подтверждены injected regression `apps/stocks_adapter_contract_smoke.py` без bind/listen.
- Refresh/runtime path c historical runtime cache для `sheet_vitrina_v1` подтверждён через `apps/sheet_vitrina_v1_stocks_refresh_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/stocks_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `partial-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Historical CSV path доказан как рабочий official closed-day stocks source для live enabled SKU set.
- Runtime-backed `sheet_vitrina_v1` contour теперь читает required `stocks[yesterday_closed]` как exact-date Seller Analytics CSV snapshot и не invent-ит surrogate current values for `today_current`.
- В date-aware refresh `stocks[yesterday_closed]` materialize-ится как closed-day truth, `stocks[today_current]` stays truthful `not_available`/blank, а later invalid attempt не имеет права разрушить already accepted closed-day snapshot.
- Live-shaped region aliases `Южный и Северо-Кавказский` и `Дальневосточный и Сибирский` больше не теряются на application normalization stage: district rows materialize-ятся в `stock_ru_south_caucasus` / `stock_ru_far_siberia`.
- Если raw payload содержит quantity вне configured district map, эта разница больше не теряется молча: она остаётся внутри `stock_total` и явно попадает в success detail / operator-facing `STATUS` note.
- Forced/external `429` у current inventory adapter больше не маскируется под заполненные stock values в тех contours, где этот adapter ещё используется.
- Coverage guard сохранён в bounded форме через `incomplete` result.

# 8. Что пока не является частью финальной production-сборки

- буквальный перенос Apps Script cursor/staging;
- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
