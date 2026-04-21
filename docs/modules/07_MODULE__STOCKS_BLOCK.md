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
related_modules:
  - "packages/contracts/stocks_block.py"
  - "packages/adapters/stocks_block.py"
  - "packages/application/stocks_block.py"
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
  - "apps/stocks_historical_csv_smoke.py"
  - "apps/sheet_vitrina_v1_stocks_refresh_smoke.py"
  - "apps/sheet_vitrina_v1_stocks_historical_backfill.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/41_stocks_block_contract.md"
  - "migration/42_stocks_block_parity_matrix.md"
  - "migration/43_stocks_block_evidence_checklist.md"
  - "migration/44_stocks_block_legacy_sample_source.md"
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под final temporal classifier: current `wb-warehouses` path сохраняется для supply-контуров и metadata bridge, а `sheet_vitrina_v1` закрепляет `stocks` на Seller Analytics CSV path `STOCK_HISTORY_DAILY_CSV` как exact-date closed-day source with `yesterday_closed_only` reporting semantics: required slot = `yesterday_closed`, `today_current` stays truthful `not_available`/blank and no longer degrades semantic green by itself."
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
- Current `wb-warehouses` endpoint остаётся live inventory source для factory/WB supply flows и bounded metadata bridge `OfficeName -> regionName` при historical CSV normalization.
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
  - historical CSV использует `OfficeName`; live normalization map получает district alias из current `wb-warehouses` metadata и не превращает этот bridge в active current stocks truth;
  - quantity из raw regions/warehouses вне configured district map не invent-ится в district rows: она остаётся внутри `stock_total` и surface-ится в `StocksSuccess.detail` / `STATUS.stocks[yesterday_closed].note`;
  - publish guard не допускает success при неполном coverage requested `nmId`;
  - current `wb-warehouses` adapter по-прежнему уважает `X-Ratelimit-Retry` / `X-Ratelimit-Reset`, использует per-seller limiter и после bounded retry budget не превращается в fake-success внутри source.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с `stock_total` и региональными `stock_*`
  - `detail` для honest note по unmapped raw regions, если часть quantity не попала ни в один configured district bucket
- Incomplete shape:
  - `kind = "incomplete"`
  - `requested_count`
  - `covered_count`
  - `missing_nm_ids`
- Целевой смысл блока: bounded stocks snapshot с сохранением coverage guard без буквального переноса Apps Script cursor/staging.
- Для two-day sheet read model блок обязан оставаться честным: required `yesterday_closed` читается только из authoritative exact-date historical path/runtime cache, while `today_current` stays blank/`not_available` and is not filled through surrogate current values.

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
