---
title: "Модуль: stocks_block"
doc_id: "WB-CORE-MODULE-07-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `stocks_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для `stocks`, включая current-only temporal semantics в `sheet_vitrina_v1`."
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
related_runners:
  - "apps/stocks_block_smoke.py"
  - "apps/stocks_block_region_mapping_smoke.py"
  - "apps/stocks_block_batching_smoke.py"
  - "apps/stocks_block_http_smoke.py"
  - "apps/sheet_vitrina_v1_stocks_refresh_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/41_stocks_block_contract.md"
  - "migration/42_stocks_block_parity_matrix.md"
  - "migration/43_stocks_block_evidence_checklist.md"
  - "migration/44_stocks_block_legacy_sample_source.md"
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под batched `wb-warehouses` checkpoint и date-aware read model: `stocks_block` больше не fan-out'ит per `nmId`, уважает live rate-limit headers и materialize-ит only `today_current` stocks без backfill в yesterday-column."
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
- Current main-confirmed official path: `POST /api/analytics/v1/stocks-report/wb-warehouses` c batched `nmIds`, `limit/offset` pagination и analytics-capable token.
- Current canonical runtime secret path для official stocks adapter: `WB_API_TOKEN`.
- Результат остаётся на уровне `nmId`, но `snapshot_date` в success теперь отражает фактический день получения current WB warehouses inventory; он может отличаться от requested sheet `as_of_date`, и именно это считается честным freshness signal.
- В bounded `sheet_vitrina_v1` contour `stocks` классифицируется как `today_current` source:
  - `stocks[today_current]` materialize-ит фактический current inventory snapshot;
  - `stocks[yesterday_closed]` не invent-ится и остаётся `not_available`, пока не появится отдельный безопасный historical/EOD path.
- Ключевая semantics:
  - latest fetched `snapshot_ts` per `nmId` считается authoritative;
  - `stock_total` суммирует `quantity` по всем WB warehouses / chart variants, которые вернул endpoint;
  - региональные `stock_*` строятся по текущему RU region mapping с нормализацией legacy/current alias-ов `Южный +/и Северо-Кавказский` и `Дальневосточный +/и Сибирский`;
  - quantity из raw regions вне configured district map не invent-ится в district rows: она остаётся внутри `stock_total` и surface-ится в `StocksSuccess.detail` / `STATUS.stocks[today_current].note`;
  - publish guard не допускает success при неполном coverage requested `nmId`;
  - `429` уважает `X-Ratelimit-Retry` / `X-Ratelimit-Reset`, использует per-seller limiter и после bounded retry budget не превращается в fake-success внутри source.

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
- Для two-day sheet read model блок обязан оставаться честным: current stocks не должны подменять yesterday EOD даже при отсутствии historical stocks path.

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
- refresh integration smoke: `apps/sheet_vitrina_v1_stocks_refresh_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/stocks_block_smoke.py`.
- Alias normalization + unmapped-note semantics подтверждены через `apps/stocks_block_region_mapping_smoke.py`.
- Batching + cache + `429` retry/exhaustion подтверждены через `apps/stocks_block_batching_smoke.py`.
- Refresh/runtime path c real `stocks` adapter подтверждён через `apps/sheet_vitrina_v1_stocks_refresh_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/stocks_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `partial-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Refresh path с live-like bundle больше не делает `stocks` fan-out per `nmId`: current bundle `33` enabled SKU обслуживаются одним batched stocks request в normal bounded scenario.
- В date-aware refresh `stocks[yesterday_closed]` честно materialize-ится как `not_available`, а `stocks[today_current]` заполняет только current-day column.
- Live-shaped region aliases `Южный и Северо-Кавказский` и `Дальневосточный и Сибирский` больше не теряются на application normalization stage: district rows materialize-ятся в `stock_ru_south_caucasus` / `stock_ru_far_siberia`.
- Если raw payload содержит quantity вне configured district map, эта разница больше не теряется молча: она остаётся внутри `stock_total` и явно попадает в success detail / operator-facing `STATUS` note.
- Forced/external `429` больше не маскируется под заполненные stock values: source-level `STATUS.stocks[today_current]=error`, freshness остаётся пустым, stock cells materialize как blank.
- Coverage guard сохранён в bounded форме через `incomplete` result.

# 8. Что пока не является частью финальной production-сборки

- буквальный перенос Apps Script cursor/staging;
- `CONFIG/METRICS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
