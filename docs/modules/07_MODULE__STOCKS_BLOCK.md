---
title: "Модуль: stocks_block"
doc_id: "WB-CORE-MODULE-07-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `stocks_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для `stocks`, включая current inventory adapter и historical closed-day semantics в `sheet_vitrina_v1`."
source_basis:
  - "artifacts/stocks_block/evidence/initial__stocks__evidence.md"
  - "apps/stocks_block_smoke.py"
  - "apps/stocks_block_http_smoke.py"
  - "apps/stocks_adapter_contract_smoke.py"
  - "apps/stocks_resilience_smoke.py"
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

## 2.1 Current inventory resilience and total deadline

`HttpBackedStocksSource.fetch` starts one monotonic deadline before runtime
configuration, cache lookup and single-flight admission. The default is 180s;
`WB_STOCKS_FETCH_DEADLINE_SECONDS` overrides it with a finite value in `(0,1800]`.
The constructor offers the same budget for fixtures. The separate per-send
`OFFICIAL_API_TIMEOUT_SECONDS` remains capped by the remaining total budget.
The deadline includes every nmID batch/page, rate-limit/backoff wait, HTTP
connect/DNS/TLS/body, JSON decoding, validation, normalization/hashing and result
transfer. A budget that cannot accommodate a rate-limit wait fails immediately.
Only complete pagination may reach the shared cache or a successful waiter.

The 180s default was selected on 2026-11-06 using the read-only production journal
sample of the last 30 `official_complete_wb_stocks` stages (2026-10-31–2026-11-06):
29 successes took 12–41s; one HTTP500 failure took 21s. Recent successes were
31,34,34,33,33,33,40,32,29s. These are whole-stage durations, an upper-bound proxy
for fetch duration, **not** measured fetch p50/p95 or page counts. 180s exceeds
four times the observed successful maximum and leaves room for two retries and
20s seller spacing; the 1800s configuration ceiling remains below the existing
three-hour cycle cadence. This is a bounded failure policy, not a proven latency
SLA. Production fetch/page distributions still need observation after release.
An identical local HTTP fixture (one page, 1000 rows, 157914 response bytes), five
uncached runs per version on 2026-11-06, measured baseline median/max
0.0056/0.0093s and isolated candidate 0.1027/0.1062s. This measures spawn/IPC
overhead for that fixture only; it is not a representative production percentile.

Production urllib work runs in one isolated, spawn-created process per leader
fetch, rather than one process per page. The parent owns the seller limiter,
cache and single-flight markers; the child performs only the readonly stocks
request and computation. The parent mediates each send's atomic rate-slot
reservation. Spawn avoids inheriting locked mutexes from the threaded runtime.
On expiry/error/interrupt the child is terminated and joined (with bounded
terminate/kill cleanup); the leader always removes its marker and wakes waiters
with an explicit success/error. A follower has its own bounded wait and cannot
cancel its leader. Its result must match the seller/date/nmID key and carry
`pagination_complete=true`; zero cache TTL also supports successful followers.

A response is read in bounded chunks, with explicit local safety ceilings of
128MiB for a successful page and 1MiB for an error body; the entire fetch is also
limited to 128MiB of received bodies across all pages/retries and 499000 source
rows. Exceeding a ceiling is
an incomplete fetch, never a truncated success. The endpoint's existing contract
caps page rows at 250000 but supplies no byte/string-length guarantee; these byte
ceilings are local resource policy, not an assertion about an upstream maximum.
The process deadline cancels stalled reads and slowly arriving 200/500 bodies,
as well as CPU parsing/hashing, instead of treating a socket timeout as a total
HTTP timeout. Result transfer uses Unix datagrams: atomic frames are <=1024
bytes, an assembled JSON record is <=16KiB, and the aggregate result is capped at
256MiB / 1000000 row-or-offset records. Per-frame acknowledgement keeps one frame
outstanding and avoids macOS `ENOBUFS` queue overflow. The parent reads using
nonblocking `select`/`recv`, incrementally reconstructs the known payload schema,
and verifies row/page/batch counts on the terminal record. A partial/oversized
record or an absent terminal record never becomes a complete result. No receiver
thread is created and result transfer never invokes pickle deserialization.

Each parent decode has a maximum input of 16KiB and checks the deadline both
before and after decoding. Two local bounded-record fixtures (16047-byte string
record and 11806-byte/1000-field record), 2000 decodes each on 2026-11-06, observed
maximum native decode times of 0.0175ms and 0.101ms. These observations are not an
OS scheduling guarantee. One bounded native decode and bounded process cleanup
are indivisible; an artificially delayed decode can return after the deadline,
but is rejected before success/cache, with no decoder thread surviving the
fetch. The regression injects both the formerly problematic slow
`ForkingPickler.loads` (no longer called) and a late bounded JSON decode, and
checks thread/child cleanup. Explicit injected opener or fake-clock/sleep
dependencies remain cooperative fixture paths; production uses the default
isolated transport.

Each page has at most **three sends total** across all reasons. Retry admission
is restricted to HTTP500/502/503/504, existing bounded429 handling and classified
timeout/reset/aborted/broken-pipe transport failures. TLS/certificate, configuration,
authentication, payload and validation errors are terminal. Jittered exponential
backoff shares the same deadline and seller limiter. Every send reserves a slot,
including failed sends; 429 respects the maximum of `X-Ratelimit-Retry`,
`X-Ratelimit-Reset`, `Retry-After` and the configured seller interval. Even an
exhausted429 updates the shared cooldown. Retries preserve `nmIds`, `chrtIds`,
`limit` and the current `offset`; there is no date in this current-inventory call.
The historical CSV create/poll/download contract is unchanged.

`python3 apps/stocks_resilience_smoke.py` covers deterministic budget, retry,
pagination, validation and cache boundaries, plus actual spawn/urllib slow-body
cancellation, parsing cancellation, child cleanup and follower/leader isolation.
The existing `apps/sheet_vitrina_v1_stocks_refresh_smoke.py` fixture currently
fails with `stock_nomenclature_scope_incomplete` / `stock_catalog_scope=unknown`;
the same failure was reproduced with the pre-change stocks adapter. This is a
separate fixture/catalog-scope issue, not a passing integration claim for this
change.

## 2.2 Structured failure evidence

Current-inventory failures carry bounded `stocks_failure` evidence through the
spawned worker, parent exception, warehouse phase journal and cycle receipt.
The shared contract permits only fixed reason/stage/type enums and bounded
numeric HTTP status, errno, TLS verification code and timing fields. Raw messages,
URLs, tokens, headers and provider bodies are not copied into this evidence.

`attempt` counts actual sends for the failing page (1–3); zero means failure
before its first send, and null means that the parent cannot observe the count.
Attempt timing covers that send and response processing; total timing starts
at fetch entry, including waits and worker startup. Unknown values remain null.
If a worker cannot report before cancellation, parent evidence identifies the
worker-wait boundary without inventing its remote state. Diagnostics do not
change retry eligibility, deadlines, publication or cache decisions.

`python3 apps/stocks_failure_diagnostics_smoke.py` exercises real spawned workers
with local HTTP/fault fixtures and reloads both the SQLite phase journal and
cycle receipt. It checks failure classification and absence of test secrets.
It does not call WB or establish the cause of any historical production failure.

# 3. Target contract и смысл результата

Для нового сбора складских данных область запроса задаёт общая номенклатура
`stock_catalog_scope` (основные и удерживаемые скрытые SKU), а не список
отображаемых товаров `config_v2`. Прочие источники витрины продолжают использовать
собственные области запросов. Новый SKU попадает в следующий новый сбор без
изменения старого списка.

Расширение каталога не переписывает ранее принятый закрытый снимок. При чтении
такого снимка недостающие SKU явно перечисляются в статусе неполного охвата;
существующие значения сохраняются. Неполный ответ по новому дню может дать
витрине только полученные значения с явным статусом `incomplete`, но не становится
принятым полным снимком. Пропуски исторического CSV не превращаются в нули.

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

Migration 145 adds a separate current planning/read contract around this
policy. `Остаток WB: всего` remains the official seller/account aggregate.
`Остаток WB без инц.: всего` may subtract an active incident only from exact
append-only evidence bound to the current WB snapshot, policy revision, date
and full SKU-scope digest. Its per-SKU lines must cover that scope exactly and
the canonical manifest digest must match those quantities. Missing or partial
evidence is `unavailable`, not zero; no stale
warehouse snapshot is reconstructed and no synthetic cap is applied. The exact
aggregate sentinel continues to prove only WB total, so current district and
regional incident values remain unavailable while historical values are kept.

Canonical stock snapshots remain immutable. `wb_incident_policy` resolves one append-only seller/account revision for the exact snapshot date, then strict `build_incident_stock_projection` publishes three projections per nmID and canonical region: fact, physical quantity on incident warehouses, and operational/effective quantity. Supply and SKU Management `Общее` consume this strict server projection and continue to require complete pagination plus source digest; browsers and page calculators never subtract warehouses independently. Module 53 `Баланс запасов` has one narrower total-only fallback: after its own exact coverage/date/digest/numeric validation it may use the official aggregate per-SKU `stock_total` only as WB opening, with explicit aggregate provenance and coefficient. This exception never calls or relaxes incident projection and never produces a warehouse, region or destination.

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
- deadline/retry/cancellation smoke: `apps/stocks_resilience_smoke.py`
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
