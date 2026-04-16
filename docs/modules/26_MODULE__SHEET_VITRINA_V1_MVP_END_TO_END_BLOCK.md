---
title: "Модуль: sheet_vitrina_v1_mvp_end_to_end_block"
doc_id: "WB-CORE-MODULE-26-SHEET-VITRINA-V1-MVP-END-TO-END-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_mvp_end_to_end_block`."
scope: "Первый bounded end-to-end alignment для `sheet_vitrina_v1`: uploaded compact bootstrap `CONFIG / METRICS / FORMULAS`, sibling `COST_PRICE` upload contour, сохранённый upload trigger, explicit refresh в repo-owned date-aware ready snapshot, cheap read этого snapshot в `DATA_VITRINA` и narrow server-side operator page для explicit refresh без возврата heavy logic в Google Sheets."
source_basis:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "migration/93_sheet_vitrina_v1_mvp_end_to_end.md"
  - "artifacts/sheet_vitrina_v1_mvp_end_to_end/target/mvp_summary__fixture.json"
  - "artifacts/sheet_vitrina_v1_mvp_end_to_end/evidence/initial__sheet-vitrina-v1-mvp-end-to-end__evidence.md"
related_modules:
  - "gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs"
  - "gas/sheet_vitrina_v1/RegistryUploadTrigger.gs"
  - "gas/sheet_vitrina_v1/PresentationPass.gs"
  - "packages/contracts/cost_price_upload.py"
  - "packages/application/cost_price_upload.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/sheet_vitrina_v1.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/web_source_snapshot_block.py"
  - "packages/adapters/seller_funnel_snapshot_block.py"
related_tables:
  - "CONFIG"
  - "METRICS"
  - "FORMULAS"
  - "DATA_VITRINA"
  - "STATUS"
related_endpoints:
  - "POST /v1/registry-upload/bundle"
  - "POST /v1/cost-price/upload"
  - "POST /v1/sheet-vitrina-v1/refresh"
  - "GET /v1/sheet-vitrina-v1/plan"
  - "GET /v1/sheet-vitrina-v1/status"
  - "GET /sheet-vitrina-v1/operator"
related_runners:
  - "apps/cost_price_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_upload_smoke.py"
  - "apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_read_split_smoke.py"
  - "apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
  - "apps/registry_upload_http_entrypoint_live.py"
related_docs:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "migration/93_sheet_vitrina_v1_mvp_end_to_end.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под date-aware ready snapshot: heavy build остаётся в `POST /v1/sheet-vitrina-v1/refresh`, но persisted plan теперь materialize-ит `yesterday_closed + today_current`, `GET /v1/sheet-vitrina-v1/plan` и `loadSheetVitrinaTable` читают только этот persisted plan, а operator-facing page/status routes явно показывают temporal slots без backfill текущих значений в yesterday-column."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_mvp_end_to_end_block`
- `family`: `sheet-side`
- `status_transfer`: первый bounded end-to-end MVP перенесён в `wb-core`
- `status_verification`: prepare-to-upload-to-refresh-to-load smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_http_entrypoint_block`
  - `sheet_vitrina_v1_registry_upload_trigger_block`
  - `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`
  - `migration/90_registry_upload_http_entrypoint.md`
  - `migration/91_sheet_vitrina_v1_registry_upload_trigger.md`
  - `migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md`
  - `migration/93_sheet_vitrina_v1_mvp_end_to_end.md`
- Семантика блока: не строить новый parallel server contour и не возвращать full legacy 1:1, а замкнуть практический `prepare -> upload -> refresh -> load` сценарий на uploaded compact package, repo-owned ready snapshot и уже существующих bounded server-side модулях.

# 3. Target contract и смысл результата

- Канонический operator flow:
  - `Подготовить листы CONFIG / METRICS / FORMULAS`
  - `Отправить реестры на сервер`
  - `POST /v1/sheet-vitrina-v1/refresh`
  - `Загрузить таблицу`
- Канонический sibling operator input flow для себестоимостей:
  - `Подготовить лист COST_PRICE`
  - `Отправить себестоимости`
  - separate server-side current state updates only `COST_PRICE` dataset
  - current checkpoint намеренно не подключает эти значения в `DATA_VITRINA` и `STATUS`
- Канонический operator-facing refresh surface:
  - `GET /sheet-vitrina-v1/operator`
  - одна primary action `Загрузить данные`
  - page вызывает existing `POST /v1/sheet-vitrina-v1/refresh`
  - page читает `GET /v1/sheet-vitrina-v1/status` для minimal status/log без отдельного audit subsystem
- Канонический prepare output:
  - `CONFIG` с uploaded compact rows
  - `METRICS` с uploaded compact rows
  - `FORMULAS` с uploaded compact rows
- Канонический upload path:
  - `POST /v1/registry-upload/bundle`
  - request body = existing upload bundle V1
  - response body = canonical `RegistryUploadResult`
- Канонический sibling cost-price path:
  - `POST /v1/cost-price/upload`
  - request body = `dataset_version + uploaded_at + cost_price_rows`
  - response body = canonical `CostPriceUploadResult`
  - dataset хранится отдельно от current registry bundle и не меняет refresh/load truth path до отдельного read-side шага
- Канонический load path:
  - `GET /v1/sheet-vitrina-v1/plan`
  - response body = date-aware `SheetVitrinaV1Envelope`-совместимый ready snapshot для `DATA_VITRINA` и `STATUS`
- Канонический refresh path:
  - `POST /v1/sheet-vitrina-v1/refresh`
  - response body = `SheetVitrinaV1RefreshResult` со snapshot metadata, `date_columns`, `temporal_slots`, `source_temporal_policies` и row counts
- Канонический operator status path:
  - `GET /v1/sheet-vitrina-v1/status`
  - response body = latest persisted `SheetVitrinaV1RefreshResult`-compatible metadata для current bundle / requested `as_of_date`

## 3.1 Date-aware ready snapshot semantics

- Текущий bounded root cause был в single-date surrogate model: server materialize-ил один ready snapshot на `as_of_date` refresh/run и не хранил достаточно явно фактическую temporal nature source values.
- Current checkpoint заменяет это на two-slot read model:
  - `yesterday_closed` = requested `as_of_date`
  - `today_current` = фактическая current UTC date materialization run
- Persisted ready snapshot теперь обязан хранить и отдавать:
  - `date_columns`
  - `temporal_slots`
  - `source_temporal_policies`
  - per-source/per-slot `STATUS` rows
- В bounded live contour используется следующая temporal policy matrix:
  - `dual_day_capable`: `seller_funnel_snapshot`, `sales_funnel_history`, `web_source_snapshot`, `sf_period`, `spp`, `ads_compact`, `fin_report_daily`
  - `today_current`: `prices_snapshot`, `ads_bids`, `stocks`
  - `blocked`: `promo_by_price`, `cogs_by_group`
- Current-only sources не backfill-ятся в closed-day column:
  - `stocks[yesterday_closed]`, `prices_snapshot[yesterday_closed]`, `ads_bids[yesterday_closed]` materialize-ятся как `not_available`
  - `today_current` values остаются в своей фактической колонке и не маскируются под yesterday EOD
- Для `stocks[today_current]` server truth дополнительно обязан:
  - нормализовать live WB region aliases `Южный +/и Северо-Кавказский` и `Дальневосточный +/и Сибирский` в canonical district metrics;
  - не терять quantity вне configured district map молча: она остаётся внутри `stock_total` и surface-ится в `STATUS.stocks[today_current].note`
- Таблица остаётся thin shell: ни `load`, ни bound Apps Script не пытаются локально угадывать, какая дата у source values.

## 3.2 Expanded operator seed bounded шага

- `config_v2 = 33`
- `metrics_v2 = 102`
- `formulas_v2 = 7`
- `enabled + show_in_data = 95`
- server-side ready snapshot materialize-ит:
  - `95` enabled+show_in_data metric rows
  - `1631` flat data rows (`47 TOTAL` + `48 * 33 SKU`)
- operator-facing `DATA_VITRINA` materialize-ит:
  - тот же incoming current-truth row set как thin presentation-only `date_matrix`
  - `95` unique metric keys
  - `34` block headers (`1 TOTAL` + `33 SKU`)
  - `33` separator rows
  - `1698` rendered data rows при тех же metric rows, но уже на двух server-owned date columns
  - header `дата | key | <yesterday_closed> | <today_current>`

Bounded допущение:
- seed deliberately не равен full legacy dump;
- `METRICS` materialize-ит полный uploaded compact dictionary для sheet/upload/runtime;
- server-side current truth, ready snapshot и `STATUS` не режутся до legacy subset;
- `DATA_VITRINA` не режет incoming server plan и делает только presentation-side reshape в data-driven `date_matrix`;
- unsupported live-source tail продолжает фиксироваться в `STATUS`, а не переносится в Apps Script как local truth path.

## 3.3 Явно принятые решения bounded шага

- `openCount` и `open_card_count` сохраняются как разные метрики из разных live sources.
- Все uploaded `total_*` и `avg_*` rows сохраняются:
  - `total_*` = сумма по enabled SKU rows;
  - `avg_*` = arithmetic mean по доступным enabled SKU values.
- Uploaded `section` dictionary считается authoritative и не remap-ится локально.
- `CONFIG!H:I` service/status block сохраняется при `prepare`, `upload`, `load`.
- Для current-only sources bounded contour честно предпочитает `not_available` в `yesterday_closed`, а не backfill текущих значений в yesterday-column.

## 3.4 Явный live blocker

- `promo_by_price` и `cogs_by_group` не имеют live HTTP adapters в текущем contour.
- Отдельный historical/EOD path для `stocks[yesterday_closed]` в этом checkpoint не materialized, поэтому yesterday stocks остаются честным gap до отдельного bounded шага, а не подменяются current snapshot-ом.
- Поэтому full current truth / `STATUS` остаются шире чисто sheet-side presentation pass.
- Это сознательно лучше, чем тихо подменять server contour локальным fixture/rule path или возвращать heavy aggregation logic в Apps Script.

## 3.5 Service block bounded шага

- `CONFIG!H:I` остаётся служебной зоной.
- `CONFIG!I2:I7` сохраняет:
  - `endpoint_url`
  - `last_bundle_version`
  - `last_status`
  - `last_activated_at`
  - `last_http_status`
  - `last_validation_errors`
- Ни `prepare`, ни `load` не должны очищать этот блок.

## 3.6 Completion semantics для execution handoff

- Канонический product flow по-прежнему остаётся `prepare -> upload -> refresh -> load`.
- Для задач, которые меняют bound Apps Script, sheet-side live behavior, operator UI или другой live operator surface вокруг `sheet_vitrina_v1`, `repo-complete` и local smokes недостаточны.
- Default completion для таких задач включает:
  - `clasp push` для bound GAS/sheet changes или equivalent publish step для другого live contour, если это безопасно и доступно;
  - минимальный live verify по затронутому surface;
  - явную фиксацию, достигнуты ли `live-complete` и `sheet-complete`.
- Если изменение затрагивает registry/upload/current bundle/readiness semantics, done criteria должны проверять не только local smokes, но и связку `refresh -> load` для текущего bundle/date.
- Если изменение затрагивает public operator route или runtime publish, done criteria должны включать и public route probe, а не только router code в repo.
- Если `clasp` credentials, spreadsheet access, live runtime access или publish rights недоступны, final handoff обязан явно назвать blocker и не маркировать задачу как fully complete.

# 4. Артефакты и wiring по модулю

- target artifacts:
  - `artifacts/sheet_vitrina_v1_mvp_end_to_end/target/mvp_summary__fixture.json`
- parity:
  - `artifacts/sheet_vitrina_v1_mvp_end_to_end/parity/seed-and-runtime-vs-data-vitrina__comparison.md`
- evidence:
  - `artifacts/sheet_vitrina_v1_mvp_end_to_end/evidence/initial__sheet-vitrina-v1-mvp-end-to-end__evidence.md`

# 5. Кодовые части

- bound Apps Script:
  - `gas/sheet_vitrina_v1/RegistryUploadSeedV3.gs`
  - `gas/sheet_vitrina_v1/RegistryUploadTrigger.gs`
  - `gas/sheet_vitrina_v1/PresentationPass.gs`
- application:
  - `packages/application/sheet_vitrina_v1_live_plan.py`
  - `packages/application/sheet_vitrina_v1.py`
  - `packages/application/registry_upload_http_entrypoint.py`
  - `packages/application/registry_upload_db_backed_runtime.py`
- adapters:
  - `packages/adapters/registry_upload_http_entrypoint.py`
  - `packages/adapters/web_source_snapshot_block.py`
  - `packages/adapters/seller_funnel_snapshot_block.py`
- local harness:
  - `apps/sheet_vitrina_v1_registry_upload_trigger_harness.js`
- smoke:
  - `apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py`
  - `apps/sheet_vitrina_v1_refresh_read_split_smoke.py`
  - `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`
  - `apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный end-to-end smoke через `apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py`.
- Подтверждён targeted runtime smoke через `apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py`.
- Подтверждён split refresh/read smoke через `apps/sheet_vitrina_v1_refresh_read_split_smoke.py`.
- Подтверждён targeted server-driven smoke через `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`.
- Smoke проверяет:
  - что `prepare` поднимает operator seed `33 / 102 / 7`;
  - что upload из sheet-side trigger сохраняет current truth в existing runtime без усечения `metrics_v2`;
  - что operator page `GET /sheet-vitrina-v1/operator` отдается тем же server contour и публикует existing refresh/status paths;
  - что `POST /v1/sheet-vitrina-v1/refresh` вызывает heavy source blocks и обновляет persisted date-aware ready snapshot;
  - что `GET /v1/sheet-vitrina-v1/status` возвращает последний persisted refresh result без live fetch и с `date_columns` / `temporal_slots`;
  - что `GET /v1/sheet-vitrina-v1/plan` и sheet-side `load` читают только ready snapshot и не делают live fetch;
  - что при отсутствии ready snapshot load path возвращает явную ошибку `ready snapshot missing`;
  - что `DATA_VITRINA` materialize-ит полный server-driven metric set как `date_matrix`, не режется до `7` metric keys и сразу грузит `yesterday_closed + today_current`;
  - что current-only sources не попадают в yesterday-column и materialize-ятся как `not_available` в `STATUS`;
  - что `STATUS` фиксирует live sources per temporal slot и blocked sources `promo_by_price` / `cogs_by_group`;
  - что service/status block `CONFIG!H:I` сохраняется и не перезаписывается при load.

# 7. Что уже доказано по модулю

- В `wb-core` появился первый bounded end-to-end MVP для `VB-Core Витрина V1`.
- Sheet-side upload registry больше не обрезает `METRICS` до subset: current truth хранит полный uploaded compact dictionary `102` rows.
- Таблица больше не заканчивается на upload-only flow: появился explicit refresh/build action и cheap read path из repo-owned ready snapshot обратно в `DATA_VITRINA`.
- У explicit refresh появился отдельный repo-owned operator page, поэтому нормальный operator path больше не зависит от ручного `curl`.
- Read path больше не строит live plan on-demand: heavy fetch живёт только в explicit refresh action, а `load` читает persisted date-aware snapshot из current runtime contour.
- Single-date surrogate semantics убраны: current-day values больше не маскируются под `as_of_date`, а `DATA_VITRINA` materialize-ит `yesterday_closed + today_current` как server-owned `date_matrix`.
- `DATA_VITRINA` materialize-ит полный incoming current-truth row set `95` metric keys / `1631` source rows как operator-facing `date_matrix` (`34` blocks / `1698` rendered rows на двух date columns) и не теряет `show_in_data` metrics на sheet-side bridge.
- Existing upload contour не ломается: bundle/result contracts и control block сохраняются.

# 8. Что пока не является частью финальной production-сборки

- full legacy parity 1:1 по всем metric sections и registry rows;
- numeric live fill для promo/cogs-backed metrics до появления `promo_by_price` и `cogs_by_group` HTTP adapters;
- full operator-facing legacy parity beyond current server-driven date-matrix scaffold;
- official-api-backed coverage всех historical metrics beyond current uploaded package;
- отдельный bounded fix по любому оставшемуся non-district / foreign stocks residual, если он потребует отдельной operator-facing semantics beyond current truthful `STATUS` note;
- stable hosted runtime URL и production-bound operator runtime;
- deploy/auth-hardening;
- daily orchestration;
- кабинет/панель администрирования;
- большой UI/UX redesign таблицы.
