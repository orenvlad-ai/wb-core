---
title: "Модуль: sheet_vitrina_v1_mvp_end_to_end_block"
doc_id: "WB-CORE-MODULE-26-SHEET-VITRINA-V1-MVP-END-TO-END-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_mvp_end_to_end_block`."
scope: "Первый bounded end-to-end alignment для `sheet_vitrina_v1`: uploaded compact bootstrap `CONFIG / METRICS / FORMULAS`, sibling `COST_PRICE` upload contour, сохранённый upload trigger, explicit refresh в repo-owned date-aware ready snapshot, separate load этого snapshot в live sheet, server-side cost overlay в operator-facing rows, cheap read этого snapshot в `DATA_VITRINA`, compact daily-report read model for two latest closed business days и narrow server-side operator page без возврата heavy logic в Google Sheets, дополненная bounded factory-order supply tab без переноса расчётной логики в Apps Script."
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
  - "packages/contracts/factory_order_supply.py"
  - "packages/application/cost_price_upload.py"
  - "packages/application/factory_order_supply.py"
  - "packages/application/simple_xlsx.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/sheet_vitrina_v1.py"
  - "packages/application/sheet_vitrina_v1_load_bridge.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/web_source_current_sync.py"
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
  - "POST /v1/sheet-vitrina-v1/load"
  - "GET /v1/sheet-vitrina-v1/daily-report"
  - "GET /v1/sheet-vitrina-v1/plan"
  - "GET /v1/sheet-vitrina-v1/status"
  - "GET /v1/sheet-vitrina-v1/job"
  - "GET /sheet-vitrina-v1/operator"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/status"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/calculate"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
related_runners:
  - "apps/cost_price_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_upload_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_read_side_smoke.py"
  - "apps/sheet_vitrina_v1_business_time_smoke.py"
  - "apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_read_split_smoke.py"
  - "apps/sheet_vitrina_v1_web_source_current_sync_smoke.py"
  - "apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py"
  - "apps/sheet_vitrina_v1_operator_load_smoke.py"
  - "apps/factory_order_supply_smoke.py"
  - "apps/sheet_vitrina_v1_factory_order_http_smoke.py"
  - "apps/web_source_temporal_adapter_smoke.py"
  - "apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py"
  - "apps/sheet_vitrina_v1_daily_report_smoke.py"
  - "apps/sheet_vitrina_v1_daily_report_http_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
  - "apps/registry_upload_http_entrypoint_live.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py"
related_docs:
  - "migration/90_registry_upload_http_entrypoint.md"
  - "migration/91_sheet_vitrina_v1_registry_upload_trigger.md"
  - "migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md"
  - "migration/93_sheet_vitrina_v1_mvp_end_to_end.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/24_MODULE__SHEET_VITRINA_V1_REGISTRY_UPLOAD_TRIGGER_BLOCK.md"
  - "docs/modules/25_MODULE__SHEET_VITRINA_V1_REGISTRY_SEED_V3_BOOTSTRAP_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под final temporal classifier и execution modes: `sheet_vitrina_v1` теперь явно разделяет group A bot/web-source historical, group B WB API date/period-capable, group C WB API current-snapshot-only и group D other/manual overlays; `stocks` закреплены как date/period-capable source c exact-date runtime cache и `yesterday_closed + today_current`, current-only group живёт по non-destructive same-day accepted-state contract, manual refresh больше не создаёт persisted long-retry tails, а daily auto chain truthfully описан как `11:00, 20:00 Asia/Yekaterinburg`."
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
  - separate server-side current state updates `COST_PRICE` dataset
  - existing refresh/read contour затем подключает этот dataset server-side в `DATA_VITRINA` и `STATUS`
- Канонический operator-facing refresh surface:
  - `GET /sheet-vitrina-v1/operator`
  - top-level tabs = `Обновление данных`, `Расчёт поставок`, `Отчёты`
  - две explicit actions `Загрузить данные` и `Отправить данные`
  - `Загрузить данные` вызывает existing `POST /v1/sheet-vitrina-v1/refresh` и materialize-ит ready snapshot only
  - `Отправить данные` вызывает `POST /v1/sheet-vitrina-v1/load` и пишет в live sheet только already prepared snapshot
  - page additionally читает `GET /v1/sheet-vitrina-v1/daily-report` для compact блока `Ежедневные отчёты` внутри отдельного top-level tab `Отчёты`
  - page additionally читает `GET /v1/sheet-vitrina-v1/stock-report` для compact блока `Отчёт по остаткам` внутри того же top-level tab `Отчёты`
  - page читает `GET /v1/sheet-vitrina-v1/status` для compact manual/auto status surface
  - page читает `GET /v1/sheet-vitrina-v1/job` для detailed построчного operator log без отдельного audit subsystem
  - тот же `job` route поддерживает text-export конкретного completed run через `format=text&download=1`
  - `Отчёты` uses the same sibling subsection selector pattern as the supply tab: default section = `Ежедневные отчёты`, second section = `Отчёт по остаткам`, only one report body is visible at a time
  - daily-report block остаётся read-only и server-owned:
    - compare target = два последних closed business day в `Asia/Yekaterinburg`
    - current rule = `yesterday_closed` из ready snapshot `as_of_date=default_business_as_of_date(now)` versus `yesterday_closed` из ready snapshot `as_of_date=default_business_as_of_date(now)-1 day`
    - `today_current` не используется как comparison baseline
    - block читает только persisted ready snapshots и current registry labels, без новых upstream fetch и без browser-side ranking logic
    - ranked total metric pool intentionally остаётся узким и canonical: `total_view_count`, `total_views_current`, `avg_ctr_current`, `avg_addToCartConversion`, `avg_cartToOrderConversion`, `avg_spp`, `avg_ads_bid_search`, `total_ads_views`, `total_ads_sum`, `avg_localizationPercent`
    - seller-funnel `ctr` и `open_card_count` intentionally исключены из daily-report current pool, so the block keeps only one transparent CTR = `CTR в поиске`
    - SKU identity в этом block truthfully остаётся `display_name + nmId`
    - ranked explanation factors используют только deterministic sign-safe signals (`views/search views/search CTR/conversions`, `ads_sum`, `price_seller_discounted`, `Нет остатков`, district low-stock `< 20` except `stock_ru_far_siberia`)
    - negative/positive factor sections are no longer capped at top-5; they render the full valid factor set
    - factor rows stay compact but now include factor label, restrained direction arrow, matched SKU count and a type-aware aggregate summary
    - aggregate summary stays truthful per factor type:
      - directional continuous/ratio factors = median percent change across matched SKU
      - price factor = median rub delta and, when available, median percent delta
      - stock/distribution flags = median stock context in pieces
    - route now surfaces `metric_ranking_diagnostics` so operator/debug tooling can explain why a ranked metric list contains fewer than five items
    - `SPP`, `ads_bid_search` и `localizationPercent` не входят в ranked explanation factors, потому что current repo norm не фиксирует для них однозначный good/bad sign
  - stock-report block остаётся read-only и server-owned:
    - source seam = persisted ready snapshot `as_of_date=default_business_as_of_date(now)` -> `DATA_VITRINA` -> slot `today_current`
    - include rule = only SKU with at least one district stock `< 50`
    - sort = min breached district stock ascending, then breached district breadth descending, then total stock ascending
    - compact district labels remain truthful to current repo buckets: `Центральный ФО`, `Северо-Западный ФО`, `Приволжский ФО`, `Уральский ФО`, `Юг и СКФО`
    - merged bucket `stock_ru_far_siberia` / `ДВ и Сибирь` stays fully excluded from stock-report filter/display because current truth does not split Far East from Siberia
  - page дополнительно показывает compact manual block `Ручная загрузка данных` с embedded actions `Загрузить данные` / `Отправить данные` и только двумя persisted manual-success fields `Последняя удачная загрузка` / `Последняя удачная отправка`
  - эти два manual fields заполняются только из `manual_context`: successful manual `refresh` обновляет только `Последняя удачная загрузка`, successful manual `load` обновляет только `Последняя удачная отправка`, auto path их не трогает
  - reload/page-open state этого manual block truthfully показывает только persisted manual-success facts и не является самостоятельным доказательством успешной последней manual `Отправить данные` без completed job/log
  - page дополнительно показывает compact block `Автообновления`, который заполняется только из server-driven `server_context`
  - `Автоцепочка` в этом block должна описывать полный daily auto cycle, а не только schedule time: current truthful wording = `Ежедневно в 11:00, 20:00 Asia/Yekaterinburg: загрузка данных + отправка данных в таблицу`
  - тот же auto block additionally показывает `Последний автозапуск`, `Статус последнего автозапуска`, `Последнее успешное автообновление` из backend/status surface
  - log block остаётся fixed-height scrollable viewport с title `Лог` и одной bounded action `Скачать лог`
- Канонический operator-facing supply surface в том же repo-owned page:
  - top-level tab `Расчёт поставок`
  - shared block `Остатки ФФ` reused by both supply calculations
  - bounded subsection `Заказ на фабрике`
  - bounded subsection `Поставка на Wildberries`
  - explicit actions `Скачать шаблон остатков ФФ`, `Скачать шаблон товаров в пути от фабрики`, `Скачать шаблон товаров в пути от ФФ на Wildberries`, `Рассчитать заказ на фабрике`, `Скачать рекомендацию`, `Рассчитать поставку на Wildberries`
  - uploads for all operator XLSX files start automatically right after file selection; current uploaded file download/delete lifecycle stays visible in the same block
  - server-side settings validation for `prod_lead_time_days`, `lead_time_factory_to_ff_days`, `lead_time_ff_to_wb_days`, `safety_days_mp`, `safety_days_ff`, `cycle_order_days`, `order_batch_qty`, `report_date_override`, `sales_avg_period_days`
  - server-side settings validation for regional block `sales_avg_period_days`, `cycle_supply_days`, `lead_time_to_region_days`, `safety_days`, `order_batch_qty`, `report_date_override`
  - operator-facing label for `order_batch_qty` = `Кратность штук в коробке`
  - operator-facing cycle vocabulary is unified: factory uses `Цикл заказов`, WB block uses `Цикл поставок`
  - page-load defaults are server/operator-owned contract: factory `30/30/15/15/15/14/250/14`, regional `14/7/15/15/250`, manual dates empty
  - upper `sheet_vitrina_v1` label is a clickable link to the current live spreadsheet target resolved from the bound Apps Script target config
  - authoritative `orderCount` history for this contour lives only server-side in `temporal_source_snapshots[source_key=sales_funnel_history]`
  - UI accepts any positive `sales_avg_period_days`; backend calculates any fully covered lookback window and returns an exact coverage blocker only when requested history reaches outside the persisted authoritative window
  - live `DATA_VITRINA` may seed a one-time bounded historical reconcile window `2026-03-01..2026-04-18`, but this is migration input only; ongoing source of truth stays server-side and future exact-date days continue through existing refresh/runtime flow
  - operator XLSX templates stay compact and Russian-headed; backend keeps stable internal mapping
  - generated XLSX files must stay readable without repair prompt in standard XLSX readers/Excel
  - `Остатки ФФ` require one row per active SKU and reject duplicate `nmId`
  - the same exact uploaded `Остатки ФФ` dataset/state is reused by the regional block; there is no second `stock_ff` upload contract/entity
  - inbound templates allow duplicate `nmId`; one row = one separate planned delivery
  - inbound datasets are optional for calculation; when a file is absent or deleted, its coverage term is treated as `0`
  - each upload block exposes the current uploaded file as a downloadable link and a bounded delete action for the stored dataset
  - factory-order coverage includes `stock_total`, uploaded `stock_ff`, inbound from factory to FF inside horizon and the parity-critical uploaded inbound `ФФ -> Wildberries`
  - result surface gives both downloadable XLSX recommendation and the same `Общее количество` / `Расчётный вес` / `Расчётный объём` summary directly in UI
  - regional block does not materialize inbound `ФФ -> Wildberries`; this input stays outside the current bounded scope
  - regional result surface gives server-driven summary, a compact district deficit table and separate district XLSX files keyed by the six canonical federal districts
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
  - dataset хранится отдельно от current registry bundle и подключается в existing refresh/load truth path только server-side
- Канонический load path:
  - `GET /v1/sheet-vitrina-v1/plan`
  - response body = date-aware `SheetVitrinaV1Envelope`-совместимый ready snapshot для `DATA_VITRINA` и `STATUS`
- Канонический refresh path:
  - `POST /v1/sheet-vitrina-v1/refresh`
  - response body = `SheetVitrinaV1RefreshResult` со snapshot metadata, `date_columns`, `temporal_slots`, `source_temporal_policies` и row counts
- Канонический operator load path:
  - `POST /v1/sheet-vitrina-v1/load`
  - response body = snapshot metadata + thin bridge result для existing bound Apps Script write path
  - route не триггерит refresh автоматически и truthfully падает при missing/invalid ready snapshot
- Канонический operator status path:
  - `GET /v1/sheet-vitrina-v1/status`
  - response body = latest persisted `SheetVitrinaV1RefreshResult`-compatible metadata для current bundle / requested `as_of_date`
  - same response additionally carries `server_context` with business timezone/current time and daily refresh trigger metadata
  - when ready snapshot is still missing, route stays truthful `422`, but error payload still carries `server_context` for the operator page empty state
- Канонический operator daily-report path:
  - `GET /v1/sheet-vitrina-v1/daily-report`
  - response body = compact JSON summary для operator block `Ежедневные отчёты`
  - route keeps `200` even when report is not yet comparable and then returns truthful `status=unavailable` + exact `reason`
  - route does not build a new ready snapshot, does not fetch upstream data and does not read `today_current` as the comparison baseline
- Канонический operator live-log path:
  - `GET /v1/sheet-vitrina-v1/job`
  - default response body = current async action status + detailed postрочный live log для `refresh` или `load`
  - `GET /v1/sheet-vitrina-v1/job?job_id=...&format=text&download=1` = plain `.txt` export ровно этого run log

## 3.1 Date-aware ready snapshot semantics

- Текущий bounded root cause был в single-date surrogate model: server materialize-ил один ready snapshot на `as_of_date` refresh/run и не хранил достаточно явно фактическую temporal nature source values.
- Current checkpoint заменяет это на two-slot read model:
  - `yesterday_closed` = requested `as_of_date`
  - `today_current` = фактическая current business date materialization run в `Asia/Yekaterinburg`
- Canonical business timezone для default-date semantics = `Asia/Yekaterinburg`:
  - default `as_of_date` = previous business day in `Asia/Yekaterinburg`;
  - `today_current` / current-only freshness = current business day in `Asia/Yekaterinburg`;
  - contour не использует host-local timezone как implicit source of truth.
- Persisted ready snapshot теперь обязан хранить и отдавать:
  - `date_columns`
  - `temporal_slots`
  - `source_temporal_policies`
  - per-source/per-slot `STATUS` rows
- В bounded live contour используется следующая source-classification и temporal policy matrix:
  - group A `bot/web-source historical / closed-day-capable`: `seller_funnel_snapshot`, `web_source_snapshot`; allowed slots = `yesterday_closed + today_current`
  - group B `WB API historical/date-period capable`: `sales_funnel_history`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`; allowed slots = `yesterday_closed + today_current`
  - group C `WB API current-snapshot-only`: `prices_snapshot`, `ads_bids`; accepted truth is captured only as current snapshot, but the accepted snapshot for closed business day D must materialize as `yesterday_closed=D` on D+1 without historical refetch
  - group D `other/non-WB/manual/browser-collector`: `cost_price`, `promo_by_price`; `cost_price` resolves `yesterday_closed + today_current` by `effective_from <= slot_date`, `promo_by_price` now reads bounded live/current truth from repo-owned promo collector sidecar + workbook seam
  - `dual_day_capable`: `seller_funnel_snapshot`, `sales_funnel_history`, `web_source_snapshot`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`, `cost_price`
  - `accepted_current_rollover`: `prices_snapshot`, `ads_bids`
  - `dual_day_capable`: `seller_funnel_snapshot`, `sales_funnel_history`, `web_source_snapshot`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`, `cost_price`, `promo_by_price`
- Для bot/web-source family (`seller_funnel_snapshot`, `web_source_snapshot`) current server-side read rule теперь bounded и truthful:
  - сначала source adapter пробует explicit requested date/window;
  - при `404` source adapter пробует latest payload без query params;
  - latest payload принимается только если его factual date совпадает с requested slot date;
  - если source latest уже уехал дальше requested slot date, STATUS surface остаётся truthful `not_found` с `resolution_rule=explicit_or_latest_date_match`.
- Для `today_current` тот же refresh contour теперь может bounded-materialize-ить missing web-source snapshot перед read-side fetch:
  - refresh сначала проверяет local `wb-ai` exact-date availability;
  - при miss он вызывает server-local owner path `/opt/wb-web-bot` same-day runners и затем `/opt/wb-ai/run_web_source_handoff.py`;
  - после successful handoff refresh читает уже materialized exact-date local snapshot;
  - если sync path падает, `STATUS.web_source_snapshot[today_current].note` / `STATUS.seller_funnel_snapshot[today_current].note` получают `current_day_web_source_sync_failed=...`, а values остаются truthful blank вместо invented fill.
- Для тех же bot/web-source sources current checkpoint теперь запрещает silent provisional inheritance в closed slot:
  - `today_current` хранится как `provisional_current_snapshot`;
  - explicit closure attempt для завершённого дня может временно сохранить `closed_day_candidate_snapshot`;
  - `yesterday_closed` читает только `accepted_closed_day_snapshot`;
  - invalid closed-day candidate не может silently оставить прошлое provisional same-day значение как будто это final truth.
- Persisted closure state materialize-ится server-side и surface-ится narrow status semantics:
  - `closure_pending`
  - `closure_retrying`
  - `closure_rate_limited`
  - `closure_exhausted`
  - `success`
- Для accepted-state policy current checkpoint применяет source-aware invalid signatures:
  - `seller_funnel_snapshot`: zero-filled payload или `source_fetched_at < next business day start in Asia/Yekaterinburg`
  - `web_source_snapshot`: zero-filled payload или `search_analytics_raw.fetched_at < next business day start in Asia/Yekaterinburg`
  - `prices_snapshot` и `ads_bids` остаются current-snapshot-only, но accepted snapshot предыдущего business day обязан truthfully materialize-иться в `yesterday_closed`, а later invalid/blank/zero attempt не может затереть ни accepted yesterday truth, ни already accepted same-day current truth;
  - `stocks` больше не current-only: `yesterday_closed` и `today_current` читают authoritative exact-date historical payload/runtime cache.
- Current-snapshot-only rollover contract is non-destructive:
  - day D valid snapshot is accepted only as current snapshot for D;
  - on D+1 the already accepted snapshot for D materializes into `yesterday_closed=D` via persisted accepted-current seam, without destructive historical refetch;
  - `today_current=D+1` remains a separate current slot and does not overwrite `yesterday_closed=D`;
  - manual invalid run does not blank accepted yesterday/current truth and does not create persisted due retry states.
- Для `stocks` current checkpoint теперь обязан:
  - materialize-ить `stocks[yesterday_closed]` из Seller Analytics CSV path `STOCK_HISTORY_DAILY_CSV`;
  - materialize-ить `stocks[today_current]` из того же exact-date historical CSV/runtime path;
  - сохранять exact-date success payload server-side в `temporal_source_snapshots[source_key=stocks]`;
  - использовать current `wb-warehouses` endpoint только как bounded metadata bridge `OfficeName -> regionName`, а не как active current stocks truth внутри витрины;
  - не терять quantity вне configured district map молча: она остаётся внутри `stock_total` и surface-ится в `STATUS.stocks[yesterday_closed].note`;
  - later invalid attempt не может destructively очистить already accepted exact-date snapshot ни для `yesterday_closed`, ни для `today_current`.
- Execution modes теперь разделены явно:
  - `auto_daily` = `11:00, 20:00 Asia/Yekaterinburg`, short retries inside run, persisted long-retry allowed where policy permits
  - `manual_operator` = short retries yes, persisted long-retry no, invalid candidate never overwrites accepted truth
  - `persisted_retry` = дожимает due `yesterday_closed` for groups A/B and same-day `today_current` only for group C within the current business day
- Для `cost_price[*]` server truth обязан:
  - брать только authoritative dataset из separate `POST /v1/cost-price/upload`;
  - match по `group`;
  - выбирать latest `effective_from <= slot_date`;
  - не рисовать fake values при empty/missing/unmatched dataset и честно surface-ить coverage в `STATUS.cost_price[*]`.
- Таблица остаётся thin shell: ни `load`, ни bound Apps Script не пытаются локально угадывать, какая дата у source values.
- Новый factory-order contour тоже остаётся thin shell:
  - operator page only orchestrates download/upload/calculate/download actions;
  - daily-report block only renders a ready-made JSON summary and does not compute ranking logic in browser JS;
  - XLSX files carry only operator-facing Russian columns, not hidden technical truth;
  - all validation, active-SKU expansion, demand averaging and recommendation math live server-side.
- `POST /v1/sheet-vitrina-v1/load` тоже остаётся thin bridge:
  - сначала server contour читает уже persisted ready snapshot;
  - затем передаёт его в existing bound Apps Script bridge;
  - same-day `date_matrix` merge treats an explicit blank incoming cell as authoritative clear, so stale live-sheet values and stale zeros are overwritten instead of being silently preserved;
  - route не rebuild-ит truth и не подмешивает implicit refresh.

## 3.1.1 Cost overlay и новые operator-facing metrics

- Current canonical read-side keys для cost overlay:
  - `cost_price_rub` = SKU-level resolved себестоимость по authoritative `COST_PRICE`
  - `avg_cost_price_rub` = weighted average по enabled SKU rows
  - `total_proxy_profit_rub` = canonical TOTAL key для operator-facing строки `Прибыль прокси всего, ₽`
  - `proxy_margin_pct_total` = canonical TOTAL key для operator-facing строки `Прокси маржинальность всего, %`
- `total_proxy_profit_rub` не invent-ится как новый surface key: используется уже существующий canonical uploaded metric key из current bundle.
- `Прибыль прокси всего` из operator wording фиксируется на canonical row `total_proxy_profit_rub` с текущим repo label `Прибыль прокси всего, ₽`.

## 3.1.2 Daily live refresh scheduling

- Daily auto-refresh materialize-ится поверх existing heavy route, а не через новый scheduler contour:
  - timer target = `POST /v1/sheet-vitrina-v1/refresh` with payload flag `auto_load=true`
  - schedule = `11:00, 20:00 Asia/Yekaterinburg`
  - current live host keeps `Etc/UTC`, поэтому systemd timer stores `OnCalendar=*-*-* 06:00:00 UTC; *-*-* 15:00:00 UTC`
- Schedule storage is repo-owned and deploys into live systemd units:
  - source artifacts = `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.service`
  - source artifacts = `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.timer`
  - live install path = `/etc/systemd/system/wb-core-sheet-vitrina-refresh.service`
  - live install path = `/etc/systemd/system/wb-core-sheet-vitrina-refresh.timer`
- Persisted retry completion for historical/date-period families plus same-day current-only captures materialize-ится отдельным bounded repo-owned timer/service pair:
  - source artifacts = `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.service`
  - source artifacts = `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.timer`
  - live install path = `/etc/systemd/system/wb-core-sheet-vitrina-closure-retry.service`
  - live install path = `/etc/systemd/system/wb-core-sheet-vitrina-closure-retry.timer`
  - service runs repo-owned runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py`
  - actual retry cadence remains runtime-owned via `next_retry_at`; timer may poll more frequently without turning into a tight loop.
- Canonical hosted deploy runner `apps/registry_upload_http_entrypoint_hosted_runtime.py` now owns the bounded install path for these unit artifacts:
  - rsync current clean worktree to `/opt/wb-core-runtime/app`
  - install checked-in unit files into `/etc/systemd/system`
  - `systemctl daemon-reload`
  - restart `wb-core-registry-http.service`
  - enable/restart the managed timers so host runtime and `server_context` stay aligned on the same schedule truth
- Repo-owned truth при этом остаётся в current code:
  - default `as_of_date` / `today_current` semantics live in `packages/business_time.py`
  - heavy refresh logic stays in existing `POST /v1/sheet-vitrina-v1/refresh`
  - auto path сначала делает refresh/persist ready snapshot, затем в том же server-owned cycle вызывает existing load bridge и доводит обновление до live sheet
  - refresh/load cycle защищён bounded mutual exclusion lock и не должен destructively смешивать parallel auto/manual/retry writes
  - runtime/status surface хранит last auto run status / timestamps separately from manual operator jobs, чтобы block `Автообновления` truthfully показывал именно результат daily auto chain
  - Apps Script remains thin shell and does not own scheduling or date math
- `Прокси маржинальность всего, %` фиксируется на canonical row `proxy_margin_pct_total`.
- Расчёт остаётся server-side:
  - SKU `proxy_profit_rub` / `profit_proxy_rub` uses existing canonical formula `{orderSum}*0,5096-{orderCount}*0,91*{cost_price_rub}-{ads_sum}`;
  - TOTAL `total_proxy_profit_rub` = sum of SKU `proxy_profit_rub`;
  - TOTAL `proxy_margin_pct_total` = `total_proxy_profit_rub / total_orderSum`, если denominator допустим.
- Пустой или неполный `COST_PRICE` dataset не валит refresh/load:
  - cost-based rows остаются blank;
  - `STATUS.cost_price[*]` объясняет missing/incomplete coverage;
  - current truth не подменяет blanks выдуманными значениями.

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
- Для current-snapshot-only sources bounded contour читает `yesterday_closed` из already accepted current snapshot предыдущего business day и не делает destructive historical refetch или blank overwrite accepted truth.
- Для `stocks` bounded contour теперь применяет task-local classifier norm: both `yesterday_closed` и `today_current` обязаны приходить из authoritative exact-date historical snapshot/runtime cache, а не из intraday surrogate.

## 3.4 Явный live blocker

- `promo_by_price` больше не является blocked source в текущем contour:
  - `today_current` materialize-ится через repo-owned promo collector run;
  - `yesterday_closed` читается только из accepted/runtime-cached promo truth;
  - low-confidence cross-year labels не invent-ят exact dates и остаются truthful `promo_start_at/end_at = null`.
- `stocks[yesterday_closed]` больше не является declared gap: official historical Seller Analytics CSV path materialized и authoritative runtime cache `temporal_source_snapshots[source_key=stocks]` now owns the closed-day truth for this source family.
- Legacy `cogs_by_group` rule module не используется как live fallback для `sheet_vitrina_v1`: текущий contour опирается только на authoritative `COST_PRICE` dataset.
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
- Для hosted runtime/publish closure canonical repo-owned path теперь фиксирован:
  - `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy`
  - `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe`
  - `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe`
- Этот runner применим и к current branch/PR without merge-before-verify, потому что деплоит current checked-out worktree, а не требует сначала merge в `main`.
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
- timezone helper:
  - `packages/business_time.py`
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
- `apps/sheet_vitrina_v1_business_time_smoke.py`
- `apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py`
- `apps/sheet_vitrina_v1_auto_update_smoke.py`
- `apps/sheet_vitrina_v1_current_snapshot_acceptance_smoke.py`
- `apps/sheet_vitrina_v1_refresh_read_split_smoke.py`
- `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`
- `apps/web_source_temporal_adapter_smoke.py`
- `apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py`
- `apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py`

# 6. Какой smoke подтверждён

- Подтверждён локальный end-to-end smoke через `apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py`.
- Подтверждён targeted business-time smoke через `apps/sheet_vitrina_v1_business_time_smoke.py`.
- Подтверждён targeted runtime smoke через `apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py`.
- Подтверждён split refresh/read smoke через `apps/sheet_vitrina_v1_refresh_read_split_smoke.py`.
- Подтверждён operator async refresh/load smoke через `apps/sheet_vitrina_v1_operator_load_smoke.py`.
- Подтверждён targeted current-day web-source sync smoke через `apps/sheet_vitrina_v1_web_source_current_sync_smoke.py`.
- Подтверждён targeted closed-day source freshness smoke через `apps/web_source_current_sync_closed_day_freshness_smoke.py`.
- Подтверждён targeted temporal closure retry smoke через `apps/sheet_vitrina_v1_temporal_closure_retry_smoke.py`.
- Подтверждён targeted current-snapshot acceptance smoke через `apps/sheet_vitrina_v1_current_snapshot_acceptance_smoke.py`.
- Подтверждён targeted auto scheduler/status smoke через `apps/sheet_vitrina_v1_auto_update_smoke.py`.
- Подтверждён integration smoke для retry/acceptance cycle через `apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py`.
- Подтверждён targeted server-driven smoke через `apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py`, включая same-day blank overwrite, который обязан затирать stale sheet cell вместо сохранения старого значения.
- Smoke проверяет:
  - что `prepare` поднимает operator seed `33 / 102 / 7`;
  - что upload из sheet-side trigger сохраняет current truth в existing runtime без усечения `metrics_v2`;
  - что operator page `GET /sheet-vitrina-v1/operator` отдается тем же server contour и публикует refresh/load/status/job paths;
  - что operator page показывает compact `Ручная загрузка данных` + `Автообновления`, отдельный `Лог`, fixed-height scroll viewport и `Скачать лог`;
  - что `POST /v1/sheet-vitrina-v1/refresh` вызывает heavy source blocks и обновляет persisted date-aware ready snapshot;
  - что `POST /v1/sheet-vitrina-v1/load` пишет в live shell только already prepared snapshot и не триггерит heavy refresh заново;
  - что `GET /v1/sheet-vitrina-v1/status` возвращает последний persisted refresh result без live fetch и с `date_columns` / `temporal_slots` plus `server_context`;
  - что `GET /v1/sheet-vitrina-v1/status` до первого refresh остаётся truthful `422`, но всё равно несёт `server_context`;
  - что `GET /v1/sheet-vitrina-v1/job` показывает построчные start / key steps / finish / error для operator actions;
  - что `GET /v1/sheet-vitrina-v1/plan` и sheet-side `load` читают только ready snapshot и не делают live fetch;
  - что authoritative `COST_PRICE` current state резолвится server-side по `group + latest effective_from <= slot_date`;
  - что `total_proxy_profit_rub` и `proxy_margin_pct_total` materialize-ятся в `DATA_VITRINA` только при applicable `COST_PRICE` coverage;
  - что empty/missing `COST_PRICE` state оставляет cost-based rows blank и surface-ит truthful `STATUS.cost_price[*]`;
  - что при отсутствии ready snapshot load path возвращает явную ошибку `ready snapshot missing`;
  - что `DATA_VITRINA` materialize-ит полный server-driven metric set как `date_matrix`, не режется до `7` metric keys и сразу грузит `yesterday_closed + today_current`;
  - что current-snapshot-only sources materialize-ят `yesterday_closed` через accepted-current rollover seam и не blank-ят already accepted previous-day truth;
  - что later invalid auto/manual current-only attempt не перетирает already accepted same-day snapshot;
  - что manual refresh не создаёт persisted long-retry tail;
  - что `STATUS` фиксирует live sources per temporal slot, `cost_price[*]` coverage и current/closed promo source facts `promo_by_price[*]` with collector trace/debug note;
  - что service/status block `CONFIG!H:I` сохраняется и не перезаписывается при load.

# 7. Что уже доказано по модулю

- В `wb-core` появился первый bounded end-to-end MVP для `VB-Core Витрина V1`.
- Sheet-side upload registry больше не обрезает `METRICS` до subset: current truth хранит полный uploaded compact dictionary `102` rows.
- Таблица больше не заканчивается на upload-only flow: появился explicit refresh/build action и cheap read path из repo-owned ready snapshot обратно в `DATA_VITRINA`.
- У explicit refresh появился отдельный repo-owned operator page, поэтому нормальный operator path больше не зависит от ручного `curl`.
- Read path больше не строит live plan on-demand: heavy fetch живёт только в explicit refresh action, а `load` читает persisted date-aware snapshot из current runtime contour.
- При missing current-day bot/web-source snapshot refresh больше не ограничен pure read-side fallback: он может bounded-trigger'ить same-day capture/handoff на server host и затем materialize-ить truthful `today_current` values в том же operator flow.
- Persisted retry semantics больше не ограничены только bot/web-source family: due `yesterday_closed` теперь дожимаются для всей historical/date-period matrix, а due current-only captures дожимаются только в пределах того же business day.
- Single-date surrogate semantics убраны: current-day values больше не маскируются под `as_of_date`, а `DATA_VITRINA` materialize-ит `yesterday_closed + today_current` как server-owned `date_matrix`.
- `DATA_VITRINA` materialize-ит полный incoming current-truth row set `95` metric keys / `1631` source rows как operator-facing `date_matrix` (`34` blocks / `1698` rendered rows на двух date columns) и не теряет `show_in_data` metrics на sheet-side bridge.
- Existing upload contour не ломается: bundle/result contracts и control block сохраняются.

# 8. Что пока не является частью финальной production-сборки

- full legacy parity 1:1 по всем metric sections и registry rows;
- numeric live fill для promo-backed metrics и других bounded long-tail rows beyond current `COST_PRICE` overlay;
- full operator-facing legacy parity beyond current server-driven date-matrix scaffold;
- official-api-backed coverage всех historical metrics beyond current uploaded package;
- отдельный bounded fix по любому оставшемуся non-district / foreign stocks residual, если он потребует отдельной operator-facing semantics beyond current truthful `STATUS` note;
- stable hosted runtime URL и production-bound operator runtime;
- deploy/auth-hardening;
- generic orchestration platform beyond current bounded auto + retry timers;
- кабинет/панель администрирования;
- большой UI/UX redesign таблицы.
