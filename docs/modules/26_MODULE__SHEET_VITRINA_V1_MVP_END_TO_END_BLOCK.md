---
title: "Модуль: sheet_vitrina_v1_mvp_end_to_end_block"
doc_id: "WB-CORE-MODULE-26-SHEET-VITRINA-V1-MVP-END-TO-END-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по bounded checkpoint блока `sheet_vitrina_v1_mvp_end_to_end_block`."
scope: "Current website/operator `sheet_vitrina_v1` contour: server-side refresh/runtime ready snapshots, public web-vitrina JSON/page composition, operator refresh/status/report/supply flows, and archived former Google Sheets load/upload/write contour. Google Sheets is not an active runtime/update/write/load/verify target."
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
  - "packages/contracts/web_vitrina_contract.py"
  - "packages/contracts/web_vitrina_gravity_table_adapter.py"
  - "packages/contracts/web_vitrina_view_model.py"
  - "packages/application/cost_price_upload.py"
  - "packages/application/factory_order_supply.py"
  - "packages/application/simple_xlsx.py"
  - "packages/application/sheet_vitrina_v1_plan_report.py"
  - "packages/application/sheet_vitrina_v1_research.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py"
  - "packages/application/sheet_vitrina_v1.py"
  - "packages/application/sheet_vitrina_v1_load_bridge.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/web_vitrina_gravity_table_adapter.py"
  - "packages/application/web_vitrina_page_composition.py"
  - "packages/application/web_vitrina_view_model.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
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
  - "POST /v1/sheet-vitrina-v1/seller-portal-recovery/start"
  - "POST /v1/sheet-vitrina-v1/seller-portal-recovery/stop"
  - "GET /v1/sheet-vitrina-v1/seller-portal-session/check"
  - "GET /v1/sheet-vitrina-v1/daily-report"
  - "GET /v1/sheet-vitrina-v1/stock-report"
  - "GET /v1/sheet-vitrina-v1/plan-report"
  - "GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx"
  - "POST /v1/sheet-vitrina-v1/plan-report/baseline-upload"
  - "GET /v1/sheet-vitrina-v1/plan-report/baseline-status"
  - "GET /v1/sheet-vitrina-v1/plan"
  - "GET /v1/sheet-vitrina-v1/status"
  - "GET /v1/sheet-vitrina-v1/job"
  - "GET /v1/sheet-vitrina-v1/seller-portal-recovery/status"
  - "GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip"
  - "GET /sheet-vitrina-v1/operator"
  - "GET /sheet-vitrina-v1/vitrina"
  - "GET /v1/sheet-vitrina-v1/web-vitrina"
  - "GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options"
  - "POST /v1/sheet-vitrina-v1/research/sku-group-comparison/calculate"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/status"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec-check"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb"
  - "POST /v1/sheet-vitrina-v1/supply/factory-order/calculate"
  - "GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/wb-regional/status"
  - "POST /v1/sheet-vitrina-v1/supply/wb-regional/calculate"
  - "GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx"
  - "GET /v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip"
  - "GET /v1/sheet-vitrina-v1/supply/calculations"
  - "GET /v1/sheet-vitrina-v1/supply/calculations/{record_id}"
  - "GET /v1/sheet-vitrina-v1/supply/calculations/{record_id}/download"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options"
related_runners:
  - "apps/seller_portal_relogin_session.py"
  - "apps/seller_portal_relogin_session_smoke.py"
  - "apps/cost_price_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_upload_smoke.py"
  - "apps/sheet_vitrina_v1_cost_price_read_side_smoke.py"
  - "apps/sheet_vitrina_v1_business_time_smoke.py"
  - "apps/sheet_vitrina_v1_ready_snapshot_runtime_smoke.py"
  - "apps/sheet_vitrina_v1_refresh_read_split_smoke.py"
  - "apps/sheet_vitrina_v1_web_source_current_sync_smoke.py"
  - "apps/sheet_vitrina_v1_data_vitrina_matrix_smoke.py"
  - "apps/sheet_vitrina_v1_operator_load_smoke.py"
  - "apps/sheet_vitrina_v1_seller_portal_recovery_http_smoke.py"
  - "apps/sheet_vitrina_v1_seller_portal_recovery_ui_smoke.py"
  - "apps/sheet_vitrina_v1_settings_sources_sessions_smoke.py"
  - "apps/sheet_vitrina_v1_settings_sources_sessions_browser_smoke.py"
  - "apps/sheet_vitrina_v1_seller_portal_recovery_live_smoke.py"
  - "apps/factory_order_supply_smoke.py"
  - "apps/wb_supply_overlay_smoke.py"
  - "apps/sheet_vitrina_v1_factory_order_http_smoke.py"
  - "apps/wb_regional_supply_smoke.py"
  - "apps/sheet_vitrina_v1_wb_regional_supply_http_smoke.py"
  - "apps/wb_regional_demand_diagnostics.py"
  - "apps/web_source_temporal_adapter_smoke.py"
  - "apps/sheet_vitrina_v1_web_source_temporal_refresh_smoke.py"
  - "apps/sheet_vitrina_v1_daily_report_smoke.py"
  - "apps/sheet_vitrina_v1_daily_report_http_smoke.py"
  - "apps/sheet_vitrina_v1_plan_report_smoke.py"
  - "apps/sheet_vitrina_v1_plan_report_http_smoke.py"
  - "apps/sheet_vitrina_v1_reports_ui_smoke.py"
  - "apps/sheet_vitrina_v1_research_sku_group_comparison_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py"
  - "apps/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py"
  - "apps/sheet_vitrina_v1_proxy_margin_3_historical_backfill_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_http_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_page_composition_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_gravity_table_adapter_integration_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_view_model_integration_smoke.py"
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
  - "docs/modules/29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "docs/modules/30_MODULE__WEB_VITRINA_GRAVITY_TABLE_ADAPTER_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под Google Sheets decommission and current plan-report operator flow: active contour is website/operator/public web-vitrina; plan-report has per-block coverage, controlled server-side monthly baseline XLSX routes and contract-period strategic guardrails for the annual buyout plan, 2026 USN upper limit and contractual minimum DRR; former Apps Script load/upload/write path is archived/do-not-use."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_mvp_end_to_end_block`
- `family`: `sheet-side`
- `status_transfer`: первый bounded end-to-end MVP перенесён в `wb-core`
- `status_verification`: prepare-to-upload-to-refresh-to-load smoke подтверждён
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`
- `status_current`: active website/operator/web-vitrina; legacy Google Sheets contour = `ARCHIVED / DO NOT USE`

# 2. Upstream/source basis и semantics

- Upstream/source basis фиксируется как связка:
  - `registry_upload_http_entrypoint_block`
  - `sheet_vitrina_v1_registry_upload_trigger_block`
  - `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`
  - `migration/90_registry_upload_http_entrypoint.md`
  - `migration/91_sheet_vitrina_v1_registry_upload_trigger.md`
  - `migration/92_sheet_vitrina_v1_registry_seed_v3_bootstrap.md`
  - `migration/93_sheet_vitrina_v1_mvp_end_to_end.md`
- Historical semantics of this checkpoint: it proved a practical `prepare -> upload -> refresh -> load` scenario on the uploaded compact package, repo-owned ready snapshot and existing bounded server-side modules. Current active semantics are server-side refresh/runtime ready snapshots plus website/operator/public web-vitrina read surfaces; the Google Sheets leg is archived.

# 3. Target contract и смысл результата

- Канонический current operator flow:
  - `POST /v1/sheet-vitrina-v1/refresh`
  - `GET /v1/sheet-vitrina-v1/status`
  - `GET /v1/sheet-vitrina-v1/web-vitrina`
  - `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`
  - `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&include_source_status=1`
  - `GET /sheet-vitrina-v1/operator`
  - `GET /sheet-vitrina-v1/vitrina`
- Current refresh observability is persisted server-side with the ready snapshot:
  - `metadata.refresh_diagnostics` carries compact refresh/phase/source-slot timing and origin metadata for the most recently materialized snapshot
  - `promo_by_price` may additionally attach nested `source_slots[].promo_diagnostics` with internal promo phase timings, counters, observation-only fingerprints, fallback/invalid reason fields and dry-run-only skip markers
  - freshly completed refresh/job results may expose the same diagnostics for operator inspection
  - diagnostics are not business truth and must not change accepted source semantics, fallback/preservation behavior, temporal slot policy, retry behavior, Google Sheets/GAS archive boundary or browser/localStorage boundary
- Former Google Sheets operator flow `prepare/upload/refresh/load DATA_VITRINA` is archived:
  - GAS functions fail fast through `ArchiveGuard.gs`;
  - `POST /v1/sheet-vitrina-v1/load` returns archived/gone in the current default runtime;
  - `auto_load=true` is rejected; daily timer performs server-side refresh only.
- Канонический sibling cost-price flow is server-side `POST /v1/cost-price/upload`; the former `COST_PRICE` sheet/menu path is archived.
- Канонический user-facing UI surface:
  - `GET /sheet-vitrina-v1/vitrina` is the primary wide entrypoint; first/default tab = `Витрина`
  - top-level tabs = `Витрина`, `Поставки`, `Отчёты`, `Отзывы`, `Исследования`; no separate top-level `Обновление данных` tab is active
  - `GET /sheet-vitrina-v1/operator` remains a compatibility entry and renders the same unified shell; embedded operator panels stay available only for the unified supply/report tabs and internal compatibility probes
  - primary action on `Витрина` is `Загрузить и обновить`, calling existing `POST /v1/sheet-vitrina-v1/refresh` and materializing the ready snapshot only
  - former `Отправить данные`/`POST /v1/sheet-vitrina-v1/load` path is archived and must not write Google Sheets
  - `Отчёты` additionally reads `GET /v1/sheet-vitrina-v1/daily-report` for `Ежедневные отчёты`
  - `Отчёты` additionally reads `GET /v1/sheet-vitrina-v1/stock-report` for `Отчёт по остаткам`
  - `Отчёты` additionally reads `GET /v1/sheet-vitrina-v1/plan-report` for `Выполнение плана`
  - page читает `GET /v1/sheet-vitrina-v1/status` для compact manual/auto status surface; root `status` there is semantic snapshot truth, while technical completion stays separated in derived fields
  - page читает `GET /v1/sheet-vitrina-v1/job` для detailed построчного operator log без отдельного audit subsystem
  - тот же `job` route поддерживает text-export конкретного completed run через `format=text&download=1`
  - web-vitrina read surface:
    - chosen page route = `GET /sheet-vitrina-v1/vitrina`
    - chosen JSON read route = `GET /v1/sheet-vitrina-v1/web-vitrina`
    - `/sheet-vitrina-v1/operator` is no longer a separate narrow source-of-truth UI; it is a compatibility entry to the same unified shell
    - v1 response is a stable library-agnostic server contract over existing ready snapshot/current truth: `meta + status_summary + schema + rows + capabilities`
    - phase 2 now additionally materializes repo-owned `web_vitrina_view_model` as a separate presentation-domain seam over that contract: `columns + rows + groups + sections + formatters + filters + sorts + state_model`
    - phase 3 now additionally materializes repo-owned `web_vitrina_gravity_table_adapter` as the first concrete adapter over that `view_model`: isolated Gravity-specific `columns + rows + renderers + groupings + filters + sorts + use_table_options + table_props + state_surface`
    - phase 4 now additionally materializes repo-owned `web_vitrina_page_composition` over the same seams: `/sheet-vitrina-v1/vitrina` fetches optional `surface=page_composition` on the existing read route and renders a real summary/filter/table page without turning browser state into canonical truth
    - history/date-range UX is compact by default: no-query page composition opens the current backend-owned business week `D-6..D` inclusive, ending on `today_current_date` in `Asia/Yekaterinburg`; `today_current_date` remains visible/selectable even before a ready snapshot exists, with blank/partial cells and warning status instead of hidden dates; the operator sees a narrow `DD.MM.YYYY - DD.MM.YYYY` control in the toolbar above the table, and the existing calendar/presets/manual fields live inside a compact one-month popover with month navigation arrows and no user-facing technical mode/query explanations; period reads preserve older persisted ready snapshots across registry bundle/schema extension by stable row key, so new metrics do not blank old metric history and old snapshots do not receive fake backfilled values
    - unified shell now includes tab `Отзывы`, backed by read-only route `GET /v1/sheet-vitrina-v1/feedbacks`; it manually loads official WB feedbacks for a bounded selected date range, star filter and answered/unanswered mode, surfaces a normalized table/summary, and does not write accepted truth, ready snapshots, Google Sheets/GAS or browser-local truth
    - table controls are compact by default: the former separate `Фильтры и настройки` card is not rendered, while `Диапазон`, `Поиск`, `Секции`, `Группа`, `Метрики`, `Столбцы` and `Сброс` live in one toolbar above the table and continue to use only browser-local filter/search/internal stable order/column-visibility state over the already loaded server payload; `Тип строк`, the visible `Сортировка` selector and row-count `Итог` block are not rendered
    - after the toolbar, a collapsible browser-local `Метрики` presentation block configures only the metrics selected by the toolbar selector: per current scope/section group it can reorder metrics, choose one anchor and move selected metrics into hidden-under-anchor state. The table applies that presentation order/collapse state with `Показать ещё N` / `Скрыть` anchor toggles, but metric registry, formulas, ready snapshots and accepted truth remain server-owned.
    - activity/reporting inside that sibling page stays server-owned: `Загрузка данных` is lazy. Initial page-open renders only a `not_loaded` state and button `Загрузить`; source groups, group refresh controls and Seller Portal session controls appear only after an explicit read-only details request (`surface=page_composition&include_source_status=1`) succeeds. That details request uses the current page payload `snapshot_as_of_date`, not browser-local today and not the rightmost `today_current` column. The loaded table renders over existing source outcomes with today/yesterday server-business dates, OK/not-OK status cells, reason columns, Russian metric labels and secondary technical endpoints; empty/incomplete details payload is shown as explicit empty/error state rather than a normal table with fake group shells, and missing ready snapshots surface as `source_status_state=missing_snapshot` with the action to run `Загрузить и обновить`. `Лог` stays below it with the existing download contour; the former `Обновление данных` activity block is no longer active page surface, while raw technical note/traceback payload stays in the existing log path. Top table header is a compact `Таблица` block with freshness badge, `Обновлено`, `Свежесть данных`, top-right load action and load-adjacent status for the latest two-day window (`today_current` + `yesterday_closed` in `Asia/Yekaterinburg`); old errors outside that latest window do not make the visible load status `Ошибка`.
    - the `view_model` layer stays library-agnostic, the Gravity-specific seam stays repo-side, and page composition remains a page-only layer above them
    - export layer, cutover away from Google Sheets and broad feature parity remain later layers
  - `Отчёты` uses the same sibling subsection selector pattern as the supply tab: default section = `Ежедневные отчёты`, additional sections = `Отчёт по остаткам` and `Выполнение плана`, only one report body is visible at a time
  - daily-report block остаётся read-only и server-owned:
    - compare target = два последних closed business day в `Asia/Yekaterinburg`
    - current rule = `yesterday_closed` из двух последних persisted ready snapshots `<= default_business_as_of_date(now)`
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
    - default source seam = latest persisted ready snapshot `<= default_business_as_of_date(now)` -> `DATA_VITRINA` -> slot `yesterday_closed`
    - default report date = latest persisted closed business day not newer than the requested default in `Asia/Yekaterinburg`
    - optional explicit `as_of_date` keeps strict exact-read on the same persisted closed-day seam and does not fallback or trigger refresh/upstream fetch
    - row set = full active `config_v2` SKU table; legacy `<50` threshold remains diagnostic only and no longer filters rows
    - default order follows active `config_v2` display order, while the operator table provides browser-local sorting
    - immediately after `Акция`, the visible table columns are `на произв.`, `в пути Китай`, `ост. ФФ`, `поставки ВБ`, `ост. ВБ`
    - `на произв.` / `в пути Китай` are read-only aggregations from existing supplier shipment registry product lines by `internal_nm_id`, including positive `matched_by_barcode` quantities (plus readable legacy `matched` / `matched_by_compatibility`) in statuses `production` (`На производстве`) and `in_transit` (`В пути`) respectively
    - `ост. ФФ` reads current server-owned `ff_stock_ledger` balances by active SKU
    - `поставки ВБ` is read-only aggregation from existing WB supplies runtime cache `raw_goods` by `nmId`, excluding only status ids `1/2/5` (`Не запланировано` / `Запланировано` / `Принято`); all other WB supply statuses are included when goods composition has positive active-SKU quantity
    - `ост. ВБ` is the existing `stock_total` WB stock value from the persisted ready snapshot, exposed with alias `stock_wb` for the report table without changing the source semantics
    - the table has an `Итого` row before SKU rows; quantitative stock/supply columns are summed, total sales/day is summed from row demand, and days-left is calculated from aggregate stock / aggregate demand or burn rather than as a simple average
    - header sorting is browser-local and preserves the current `scrollLeft` of the horizontal table wrapper across the synchronous table rebuild for both ascending and descending clicks, including the far-left and far-right positions
    - compact district labels remain truthful to current repo buckets: `Центральный ФО`, `Северо-Западный ФО`, `Приволжский ФО`, `Уральский ФО`, `Юг и СКФО`
    - merged bucket `stock_ru_far_siberia` / `ДВ и Сибирь` stays fully excluded from stock-report filter/display because current truth does not split Far East from Siberia
  - plan-report block `Выполнение плана` остаётся read-only и server-owned:
    - route = `GET /v1/sheet-vitrina-v1/plan-report`
    - primary input params = half-year buyout plans `H1/H2`, planned DRR percent and selected period (`yesterday`, `last_7_days`, `last_30_days`, `current_month`, `current_quarter`, `current_year`, fixed quarters/halves); optional `annual_plan_evenly_distributed=true|false` switches plan calculation to an even annual `H1+H2` daily load; legacy complete `Q1..Q4` params may be accepted transitionally by summing Q1+Q2 into H1 and Q3+Q4 into H2
    - ordinary operator UI exposes period, H1, H2, DRR, optional checkbox `Равномерный годовой план` and `Рассчитать`; the former contract-start checkbox/date input are not visible operator controls
    - operator UI defaults are WB/VB-specific when persisted plan inputs are absent/invalid: `H1=155379879`, `H2=294620121`, `DRR=6`, canonical contract start `2026-02-01`, annual-even disabled by default; default period follows the current WB/VB target half-year (`first_half` through 2026-06-30, then `second_half`); `H2=294620121` is the annual remainder to `450000000`, while Q3+Q4 source figures total `294620120`, so the 1 rub discrepancy is explicit
    - daily facts use persisted accepted closed-day snapshots for `fin_report_daily.fin_buyout_rub` and `ads_compact.ads_sum` over current active `config_v2` SKU
    - optional manual monthly facts come only from the separate runtime source `manual_monthly_plan_report_baseline`, uploaded through the plan-report baseline XLSX routes and used only for full baseline months inside aggregate plan-report periods
    - if a baseline month has incomplete daily precision, the monthly aggregate covers that month and overlapping daily rows are excluded from the block to prevent double-count; a fully daily-covered month uses daily facts and skips baseline
    - default buyout plan is distributed by calendar day and crosses 30 Jun / 1 Jul by using the H1/H2 plan for each individual date; fixed target periods use full target-period plan clipped by contract start for the main `plan`/`completion_pct`, while facts and coverage use only closed dates through `as_of_date`
    - annual-even remains optional in ordinary UI as a strategic pace view: unchecked formal mode uses WB/VB H1/H2 denominator semantics, checked mode sends `annual_plan_evenly_distributed=true` and distributes `H1+H2` evenly across the canonical contract period; this checked mode is not claimed as official WB/VB execution logic
    - `completion_pct = fact / plan * 100` is the visible execution percentage for buyout and ads; legacy `delta_pct` remains diagnostic
    - DRR fact = `ads_sum / fin_buyout_rub * 100`; `plan_drr_pct` is the contractual minimum, so DRR at or above it is `ok`/positive margin and only DRR below it is a minimum violation; ads plan follows WB/VB semantics `max(buyout_plan, buyout_fact) * plan_drr_pct / 100`, with payload diagnostics showing whether the base was plan turnover or fact turnover due to overperformance
    - response always includes selected block plus MTD/QTD/YTD blocks when active SKU truth is available, and includes `contract_period_projection` with projected contract-end buyout/ads from elapsed facts: `elapsed_fact / elapsed_days * total_contract_days`, annual buyout plan `H1+H2`, annual ads plan `(H1+H2) * DRR / 100`, percent-of-plan fields and projected DRR
    - `contract_period_projection` exposes stable strategic fields `annual_buyout_plan_rub`, fixed `usn_upper_limit_rub=490500000`, `projected_buyout_rub`, `projected_buyout_pct_of_annual_plan`, `projected_buyout_pct_of_usn_upper_limit`, `projected_buyout_remaining_to_usn_upper_limit_rub`, `projected_buyout_exceeds_usn_upper_limit`, `drr_minimum_pct`, `drr_requirement_type=minimum`, `projected_drr_pct`, `projected_drr_margin_to_minimum_pp` and `projected_drr_minimum_met`; fixed limit/minimum fields remain present for `partial/unavailable`, while derivatives are `null` only when their required projection is unavailable
    - `usn_upper_limit_rub=490500000` is the 2026 management reference `450000000 × 1.090` based on the deflator coefficient established by Ministry of Economic Development of Russia order dated 06.11.2025 №734; it compares against the same `fin_buyout_rub` buyout forecast, may yield a negative remaining amount on exceedance, and does not replace the tax-accounting income register
    - projection UI shows `Годовой план выкупов`, `Верхний порог УСН` and `Минимальный DRR по договору` with compact accessible explanations; the USN reference is `490,5 млн ₽`, the contractual minimum is `6%`, and a projected DRR above `6%` is shown as positive margin rather than an error
    - each block carries its own `available / partial / unavailable` status, coverage details, reason, source mix and metrics; missing YTD must not hide an available selected period
    - incomplete temporal snapshot/baseline coverage is surfaced as `partial`/`unavailable` with missing/covered dates and never as fabricated zero fact
    - operator page exposes compact baseline controls: download template, upload filled XLSX and read current baseline status/totals/upload metadata
  - embedded compatibility panel additionally keeps compact manual block `Ручная загрузка данных` с active action `Загрузить данные`; former Google Sheets action `Отправить данные` is archived/disabled, не является active runtime/update/write/load/verify target, and appears only as archived/manual-context history
  - Seller session controls are absent from the unified `Витрина` loading table and embedded manual operator panel. The ambiguous top Seller badge is also absent. `Seller Portal / бот` is monitoring-only there and exposes route/source `Проверить` plus optional safe `Повторить сбор`:
    - `Настройки → Источники и сессии` is the only login/recovery UI and truthfully distinguishes `session_valid_canonical / session_valid_wrong_org / session_invalid / session_missing / session_probe_error`
    - centralized `Проверить` refreshes cached auth and the exact supply-cost route; `Восстановить` starts the repo-owned lifecycle and exposes the reusable launcher only when readiness is explicit
    - WB Buyer uses the same visual steps but its own persistent profile/adapter, while WB Card/SPP Proxy is anonymous and has no login UI
    - if the backend returns controlled `409` because the run is still starting or the launcher is not yet ready, the UI keeps a warning/retry log/status state inside the same one-action flow instead of exposing an additional launcher button
    - recovery run truthfully passes through `starting / awaiting_login / saving_session / validating_session / checking_canonical_supplier / triggering_refresh` and must end as one explicit final outcome: `completed / not_needed / stopped / timeout / error`
    - host-side VNC contour is additionally hardened with `x11vnc -noxdamage`, because user-facing truth here is the noVNC canvas rather than host-side local screenshots
  - эти два manual fields заполняются только из `manual_context`: successful manual `refresh` обновляет только `Последняя удачная загрузка`; current Google Sheets `load` archived, so `Последняя удачная отправка` is historical state and must not be used as completion proof
  - reload/page-open state этого manual block truthfully показывает только persisted manual-success facts и не является доказательством Google Sheets write
  - page не содержит дублирующий block `Автообновления`; единственный editor/status surface Витрины расположен в `Настройки → Автообновления`
  - schedule rows остаются в прежнем server runtime JSON и там же expose editable `HH:mm`, enabled flag, next run, last run, last success, status/error and run-now action; the systemd timer is only a due-check ticker
  - log block остаётся fixed-height scrollable viewport с title `Лог` и одной bounded action `Скачать лог`
- Канонический operator-facing supply surface в том же repo-owned page:
  - top-level tab `Расчёт поставок`
  - shared block `Остатки ФФ` reused by both supply calculations
  - bounded subsection `Заказ на фабрике`
  - bounded subsection `Поставка на Wildberries`
  - read-only subsection `Реестр расчётов` for exact factory-order and WB regional history
  - explicit actions `Скачать шаблон остатков ФФ`, `Скачать шаблон товаров в пути от фабрики`, `Скачать шаблон товаров в пути от ФФ на Wildberries`, `Рассчитать заказ на фабрике`, `Скачать рекомендацию`, `Рассчитать поставку на Wildberries`
  - uploads for all operator XLSX files start automatically right after file selection; current uploaded file download/delete lifecycle stays visible in the same block
  - server-side settings validation for `prod_lead_time_days`, `lead_time_factory_to_ff_days`, `lead_time_ff_to_wb_days`, `safety_days_mp`, `safety_days_ff`, `cycle_order_days`, `order_batch_qty`, `report_date_override`, `sales_avg_period_days`
  - server-side settings validation for regional block `sales_avg_period_days`, `cycle_supply_days`, legacy scalar `lead_time_to_region_days`, canonical `lead_time_to_region_days_by_district`, `safety_days`, `order_batch_qty`, `report_date_override`, `included_district_keys`
  - regional block renders the supply-planning contract `ЦФО Север / ЦФО Восток / ЦФО Юг / СЗФО / ПФО / УФО / ЮФО/СКФО / ДВФО/СФО`; `ЦФО Запад` is absent. Canonical reporting key `central` remains unchanged outside this block. A separate `Доставка, дней` input per zone means lag until goods are available on WB and defaults to `15`; inclusion, validation, result rows and downloads use the same eight-key server contract.
  - regional share methodology is a bounded ladder: full clean days stay preferred; if they are insufficient, backend uses valid `SKU + district + day` partial observations; missing cells are then filled from deterministic SKU group prior and global prior; seed floor is last resort only. `0 -> 0` is counted as no-signal, not demand evidence and not an early seed trigger; `positive -> 0` remains stockout risk, restock/upward correction remains invalid for that district/day.
  - regional result card keeps the main status compact and human-readable in Russian: normal ladder recovery is shown as `Расчёт выполнен по расширенной методологии`, technical method/reason codes are translated in visible diagnostics, and ladder recovery alone is not shown as a warning. Long affected `nmId` lists, share source counts, low-confidence details, seed details and reason counters are available only in bounded expandable diagnostics and must not widen the card. Seed wording uses `SKU / направлений SKU-округ`.
  - operator-facing label for `order_batch_qty` = `Кратность штук в коробке`
  - operator-facing cycle vocabulary is unified: factory uses `Цикл заказов`, WB block uses `Цикл поставок`
  - page-load defaults are server/operator-owned contract: factory `30/30/15/15/15/14/250/14`, regional `14/7/15-per-district/15/250`, manual dates empty
  - upper `sheet_vitrina_v1` label is a clickable link to the current live spreadsheet target resolved from the bound Apps Script target config
  - authoritative `orderCount` history for this contour lives only server-side in `temporal_source_snapshots[source_key=sales_funnel_history]`
  - UI accepts any positive `sales_avg_period_days`; backend calculates any fully covered lookback window and returns an exact coverage blocker only when requested history reaches outside the persisted authoritative window
  - live `DATA_VITRINA` may seed a one-time bounded historical reconcile window `2026-03-01..2026-04-18`, but this is migration input only; ongoing source of truth stays server-side and future exact-date days continue through existing refresh/runtime flow
  - operator XLSX templates stay compact and Russian-headed; backend keeps stable internal mapping
  - generated XLSX files must stay readable without repair prompt in standard XLSX readers/Excel
  - `Остатки ФФ` manual Excel source requires one row per active SKU and rejects duplicate `nmId`
  - the same shared `Остатки ФФ` source selector is reused by factory-order and regional blocks; manual uploaded dataset/state remains one entity, while `1С / Фулфилмент` reads existing materialized 1C `FF_STOCK` metric `onec_FF_STOCK_qty` and does not create a second `stock_ff` upload contract/entity
  - the shared `Остатки ФФ` block also exposes calculation-only selector `Учесть WB-поставки` backed by `GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options`; selector options include only statuses `3/4/6`, statuses `1/2/5` and `Допринято` are not rendered. On first successful option load after a fresh page open, every option with backend `eligible_for_overlay=true` and `disabled=false` is visibly checked. Manual uncheck/recheck remains authoritative for that open page across refresh, rerender and form switching; a new page load applies a new current eligible default. Selected ids are sent explicitly as `selected_wb_supply_ids` in both factory-order and WB regional calculate requests, while backend always revalidates status/date/composition/active SKU/mapping from server runtime cache; long unmapped-warehouse warnings are collapsed behind details in the operator UI
  - selected WB supplies are overlay evidence only: they do not replace manual Excel / 1C as the `stock_ff` source, do not become ЕБД metric truth, do not write ready/web-vitrina snapshots and do not mutate WB Supplies API
  - common ФФ overlay formula for manual Excel and `1С / Фулфилмент` is `effective_stock_ff = max(base_stock_ff - selected_wb_supply_qty, 0)` plus `over_reserved_qty = max(selected_wb_supply_qty - base_stock_ff, 0)`; over-reserved rows warn but do not fail the calculation. For source `Остатки ФФ` / `ff_stock_ledger`, selected WB supplies are not deducted from `stock_ff` again, but still add factory inbound and WB regional projection.
  - inbound templates allow duplicate `nmId`; one row = one separate planned delivery
  - inbound datasets are optional for calculation; when a file is absent or deleted, its coverage term is treated as `0`
  - each upload block exposes the current uploaded file as a downloadable link and a bounded delete action for the stored dataset
  - factory-order coverage includes `stock_total`, selected `stock_ff` source (manual Excel, 1C FF_STOCK or `ff_stock_ledger`), inbound from factory to ФФ inside horizon and the parity-critical uploaded inbound `ФФ -> Wildberries`; selected WB supplies add the same quantity to automatic `inbound_ff_to_wb` rows when their selected operational date is inside the existing inbound window. Manual Excel and `1С / Фулфилмент` also subtract selected quantity from free `stock_ff`; `ff_stock_ledger` keeps ledger `stock_ff` unchanged because WB writeoff movements are already reflected.
  - result surface gives both downloadable XLSX recommendation and the same `Общее количество` / `Расчётный вес` / `Расчётный объём` summary directly in UI
  - regional block does not materialize an upload contract for `Товары в пути от ФФ на Wildberries`; selected WB supplies are the bounded calculation overlay for this flow: mapped supply events add quantity only to their destination calculation district, unmapped warehouse events do not add regional quantity and warn
  - factory-order and WB regional results expose `wb_supply_overlay` diagnostics: selected supplies, selected date/evidence, planned/target `district_source_warehouse_*` evidence, route/display warehouses, warehouse/district mapping evidence, accounted/skipped SKU quantities and reasons, `base_stock_ff`, `selected_wb_supply_qty`, `effective_stock_ff`, `over_reserved_qty`, factory added `inbound_ff_to_wb` and regional added quantities by district
  - regional result surface gives server-driven summary and a compact planning-zone table immediately under the result totals. Central is shown as three peer rows; positive rows open the selected zone in the single full-width `Подбор складов WB` card below the two-column parameters/result area and scroll to it. The wide table has its own horizontal scroll and never widens the page.
  - planning calls WB `acceptance/options` only after complete server-owned barcode resolution. The ordinary manager table contains only exact-zone, direct, active, unblocked full-storage destinations that accept all required barcodes with `canBox=true`; СЦ/СГТ/specialised/partial/blocked/unclassified evidence is diagnostic-only. It renders role, warehouseID, barcode coverage, package support, unique chronological dates, separate first available/free dates, tariffs, direct-destination state and explicit reason codes. It remains manual/read-only and creates or books nothing in WB.
  - direct planning-zone XLSX files retain their stable ASCII route filenames (`wb_regional_central_north_fo.xlsx`, `wb_regional_central_east_fo.xlsx`, `wb_regional_central_south_fo.xlsx`, plus unchanged non-Central stems) and now include only `nmId / SKU / Количество к поставке`; `Дефицит` remains backend/UI calculation truth but is not exported to the operator workbook.
  - `GET /v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip` and button `Скачать все рекомендации` build one atomic archive `Рекомендации_поставок_<date>_<time>_<calculation_id>.zip`. Every included recommendation follows UI order and gets a unique safe `ordinal + calculation_id + destination` folder/prefix with exactly two files: operator recommendation and WB-upload XLSX copied from the checked-in canonical `Sheet1 / Баркод / Количество` template. Exact nomenclature barcodes are text, duplicates are summed, totals reconcile, and any missing/ambiguous barcode or invalid quantity returns one controlled error without a partial archive.
  - every successful calculation atomically updates the compatible latest-result slot and appends one immutable complete registry snapshot. The unified registry is server-owned, bounded to `200` complete rows, paginated `25` by default (`100` maximum), stably ordered by calculation time and identity, and filtered by type/date. Detail reopens the exact stored settings, selected WB supply ids, incident/source evidence, warnings, summary and full result rows. Historical download streams the exact XLSX/ZIP bytes saved with that record rather than regenerating from latest/current state. Legacy regional metadata-only audit rows remain visible with an explicit incomplete/non-reproducible marker and no fabricated payload or download.
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
  - response body = snapshot metadata + thin bridge result для existing bound Apps Script write path + separate semantic fields for `updated / unchanged / not_verified / error`
  - route не триггерит refresh автоматически и truthfully падает при missing/invalid ready snapshot
- Канонический operator status path:
  - `GET /v1/sheet-vitrina-v1/status`
  - response body = latest persisted `SheetVitrinaV1RefreshResult`-compatible metadata для current bundle / requested `as_of_date`, but root `status` is semantic result of the current snapshot rather than mere readiness
  - same response additionally carries `server_context` with business timezone/current time and daily refresh trigger metadata
  - when ready snapshot is still missing, route stays truthful `422`, but error payload still carries `server_context` for the operator page empty state
- Канонический operator daily-report path:
  - `GET /v1/sheet-vitrina-v1/daily-report`
  - response body = compact JSON summary для operator block `Ежедневные отчёты`
  - route keeps `200` even when report is not yet comparable and then returns truthful `status=unavailable` + exact `reason`
  - route does not build a new ready snapshot, does not fetch upstream data and does not read `today_current` as the comparison baseline
- Канонический operator plan-report path:
  - `GET /v1/sheet-vitrina-v1/plan-report`
  - response body = compact JSON summary для operator block `Выполнение плана`
  - valid query returns `200` with root `status=available/partial/unavailable`
  - response body always contains per-block `periods.selected_period`, `periods.month_to_date`, `periods.quarter_to_date`, `periods.year_to_date`
  - root status is aggregate; UI must render available/partial blocks even when another block is unavailable
  - primary required params = `period`, `h1_buyout_plan_rub`, `h2_buyout_plan_rub`, `plan_drr_pct`; ordinary UI always adds canonical `use_contract_start_date=true`, `contract_start_date=2026-02-01`, and sends `annual_plan_evenly_distributed=false|true` from the optional checkbox; backend still accepts contract-start query params for compatibility/tests, but ordinary UI does not expose contract-start controls; legacy complete `q1_buyout_plan_rub`..`q4_buyout_plan_rub` remains a transitional fallback only
  - fixed target period payload separates `target_date_from`/`target_date_to`/`target_day_count` from `fact_date_from`/`fact_date_to`/`fact_day_count`; main `plan` is target-period plan, and `to_date_plan` is diagnostic
  - buyout/ads metrics expose `completion_pct = fact / plan * 100`; ads metrics also expose `ads_plan_base_rub` and `ads_plan_base_mode`
  - `fin_report_daily.fin_buyout_rub` and `ads_compact.ads_sum` facts come from the shared accepted temporal source slot layer; metric-specific daily coverage is transparent, so a missing ads day does not erase an available buyout day, and the block stays `partial` with source-specific missing dates
  - sibling baseline routes:
    - `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx`
    - `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload`
    - `GET /v1/sheet-vitrina-v1/plan-report/baseline-status`
  - baseline upload validates `YYYY-MM` month, non-negative numeric `fin_buyout_rub`/`ads_sum`, non-empty workbook and duplicate month errors, then stores aggregates idempotently in runtime SQLite
  - route does not build a new ready snapshot, does not fetch upstream data and does not use Google Sheets/GAS
- Канонический operator feedbacks path:
  - route = `GET /v1/sheet-vitrina-v1/feedbacks`
  - response body = `sheet_vitrina_v1_feedbacks` JSON with `meta`, `summary`, `schema.columns` and normalized `rows`
  - query supports `date_from`, `date_to`, optional `stars=1,2,3,4,5` and `is_answered=true|false|all`
  - upstream source is official WB `GET /api/v1/feedbacks` through canonical `WB_API_TOKEN`; because upstream requires `isAnswered`, default `all` performs separate bounded `false`/`true` streams, merges and sorts by date desc
  - route is read-only and does not build/alter ready snapshots, accepted temporal slots, complaint statuses, Seller Portal bot state, Google Sheets/GAS or long-term ЕБД persistence
- One-off historical consistency repair between web-vitrina and reports is handled by `apps/sheet_vitrina_v1_ready_fact_reconcile.py`:
  - dry-run compares server-side ready snapshots against accepted temporal slots for bounded windows and reports insert/skip/diff actions;
  - apply inserts only missing `fin_report_daily` / `ads_compact` accepted slots from daily SKU values already present in server-side ready snapshots;
  - existing accepted snapshots are not overwritten, blank ready values are not fabricated as zero, and the path is not a recurring Google Sheets/GAS source.
- Legacy one-off `apps/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py`
  остаётся read-only migration evidence для прежнего двухрядного repair и не
  является active post-functional writer. Его `--apply` fail-closed отключён
  до любого backup/write; активная публикация идёт только через единый T1
  recovery-policy contour ниже.
- Active bounded publication с `2026-07-01` выполняет только `packages/application/warehouse_functional_economics_backfill.py` через repo-owned команды `warehouse-functional-economics-dry-run/apply`:
  - она читает frozen/canonical daily WB WAC и effective versioned calculation parameters, затем публикует ровно восемь public cost/coverage/Proxy 3 SKU+TOTAL metric families;
  - Proxy margin делится на expected buyout revenue, TOTAL строится как ratio aggregate profits к aggregate expected revenue, а missing operand/zero denominator остаётся blank;
  - exact plan включает digest полного ready-snapshot manifest и non-target digest; apply требует fresh fingerprint, backup `0600`, проверяет manifest под `BEGIN IMMEDIATE`, делает in-transaction readback и сохраняет все non-target rows/metadata;
  - repeated dry-run после apply возвращает zero changes; settings save запускает тот же targeted publisher без physical warehouse rebuild.
- User-facing term `ЕБД` / `единая база данных` names the shared server-side accepted truth/runtime layer for this contour: persisted accepted closed-day temporal source slots, ready snapshots and related runtime state produced by repo-owned refresh/group-refresh/reconcile paths. Web-vitrina, plan-report and future reports consume this server-side layer; Google Sheets/GAS, the HTML UI, browser `localStorage` and report-private manual tables are not the EBD.
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
  - group B `WB API historical/date-period capable`: `sales_funnel_history`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`; source family stays date/period-capable, but required-slot policy is source-aware
  - group C current-snapshot-only accepted rollover: `prices_snapshot`, `ads_bids`, `spp_proxy`; accepted truth is captured only as current snapshot, but the accepted snapshot for closed business day D must materialize as `yesterday_closed=D` on D+1 without historical refetch. `spp_proxy` uses anonymous public WB card buyer price and existing `prices_snapshot.price_seller_discounted`; it does not replace current `spp`.
  - group D `other/non-WB/manual/browser-collector`: `cost_price`, `promo_by_price`; `cost_price` resolves `yesterday_closed + today_current` by `effective_from <= slot_date`, `promo_by_price` now reads bounded live/current truth from repo-owned promo collector sidecar + workbook seam
  - `dual_day_capable`: `seller_funnel_snapshot`, `sales_funnel_history`, `web_source_snapshot`, `sf_period`, `ads_compact`, `cost_price`, `promo_by_price`
  - `dual_day_intraday_tolerant`: `spp`, `fin_report_daily`
  - `accepted_current_rollover`: `prices_snapshot`, `ads_bids`, `spp_proxy`
  - `yesterday_closed_only`: `stocks`
- Source-aware semantic reduction norm:
  - `seller_funnel_snapshot` и `web_source_snapshot` remain full two-slot sources; broken `today_current` stays warning/error and must keep top badge/cards degraded.
  - `stocks[yesterday_closed]` stays authoritative required closed-day truth from exact-date historical CSV/runtime snapshots, while `stocks[today_current]` is a truthful non-required `not_available` slot that no longer counts against source or aggregate semantic status.
  - `spp` и `fin_report_daily` still request `today_current`, but intraday current-day non-yield (`empty`, `zero-like`, `invalid_exact_snapshot`, `no-result`, bounded `429/timeout`, preserved/runtime-cache current fallback) is tolerated when `yesterday_closed` is confirmed success.
  - `prices_snapshot` и `ads_bids` remain current-snapshot-only: accepted-current rollover, same-day accepted preservation and latest confirmed filled values are OK; a required current slot without accepted fallback remains not OK.
  - `promo_by_price` accepted/runtime-cached latest confirmed values are OK when the visible cells are filled; invalid attempts without accepted fallback remain not OK.
  - `onec_stocks` may be semantically partial by stage bucket: current 1C rows for present buckets materialize, missing bucket rows are filled only from server-owned accepted same-date truth when available, and no-truth missing bucket cells remain blank with an `incomplete` source reason rather than fake zeros.
  - loading/action status cells must use this same source-aware reduction instead of treating every `not refreshed / unchanged / fallback` note as red.
- Для bot/web-source family (`seller_funnel_snapshot`, `web_source_snapshot`) current server-side read rule теперь bounded и truthful:
  - сначала source adapter пробует explicit requested date/window;
  - при `404` source adapter пробует latest payload без query params;
  - latest payload принимается только если его factual date совпадает с requested slot date;
  - если source latest уже уехал дальше requested slot date, STATUS surface остаётся truthful `not_found` с `resolution_rule=explicit_or_latest_date_match`.
- Для `today_current` тот же refresh contour теперь может bounded-materialize-ить missing web-source snapshot перед read-side fetch:
  - refresh сначала проверяет local `wb-ai` exact-date availability;
  - если local exact-date snapshot отсутствует, contour сначала проверяет текущий seller-portal browser state в `/opt/wb-web-bot/storage_state.json`; login redirect / auth `401` materialize-ится как `seller_portal_session_invalid` и останавливает bot run до `runner_day` / `runner_sales_funnel_day`;
  - repo-owned recovery path for this barrier remains `apps/seller_portal_relogin_session.py`, but current steady operator flow wraps it behind `start/status/stop/launcher` HTTP routes and the compact operator block; the tool must first materialize a visible headed Chromium window on Xvfb/noVNC, then confirm the canonical supplier/org, safe-switch to it when available, only then save refreshed `storage_state.json`, auto-trigger loopback refresh and cleanup the temporary contour;
  - при miss он вызывает server-local owner path `/opt/wb-web-bot` same-day runners и затем `/opt/wb-ai/run_web_source_handoff.py`;
  - после successful handoff refresh читает уже materialized exact-date local snapshot;
  - если sync path падает, `STATUS.web_source_snapshot[today_current].note` / `STATUS.seller_funnel_snapshot[today_current].note` получают `current_day_web_source_sync_failed=...`; invalidated seller session now surfaces there as explicit `seller_portal_session_invalid`, а values остаются truthful blank вместо invented fill.
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
- Web-vitrina session highlight metadata is action-scoped:
  - full `POST /v1/sheet-vitrina-v1/refresh` emits `updated_cells` across every refreshed temporal date column, normally `yesterday_closed + today_current`;
  - full refresh does not treat `date_from/date_to` period selection or the rightmost `today_current` table column as the ready-snapshot key; current business date input is normalized to the previous closed day before materialization;
  - `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` emits `updated_cells` only for the selected source group and selected `as_of_date`;
  - `updated` means the visible ready value changed, `latest_confirmed` means the cell was checked and filled from accepted/latest-confirmed fallback, and the browser must not persist this as styling truth.
- Для `stocks` current checkpoint теперь обязан:
  - materialize-ить `stocks[yesterday_closed]` из Seller Analytics CSV path `STOCK_HISTORY_DAILY_CSV`;
  - surface-ить `stocks[today_current]` как truthful `not_available`/blank non-required slot instead of inventing same-day stocks;
  - сохранять exact-date success payload server-side в `temporal_source_snapshots[source_key=stocks]`;
  - использовать current `wb-warehouses` endpoint только как bounded metadata bridge `OfficeName -> regionName`, а не как active current stocks truth внутри витрины;
  - не терять quantity вне configured district map молча: она остаётся внутри `stock_total` и surface-ится в `STATUS.stocks[yesterday_closed].note`;
  - later invalid attempt не может destructively очистить already accepted exact-date snapshot for the required closed slot.
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
  - route не rebuild-ит truth и не подмешивает implicit refresh;
  - operator/public wording distinguishes technical write completion from confirmed material update, unchanged/no-op and first-write `not_verified`.

## 3.1.1 Legacy COST_PRICE audit и active operator-facing economics

Legacy `COST_PRICE` хранится как server-owned group/effective-date audit dataset, но больше не является active public cost model. Central archived-metric boundary удаляет всю зависимую Proxy 1 closure из catalog, ready-plan rows, public read contract, filters/settings/picker, activity labels and source-group refresh. Persisted historical rows и upload/current-state records не удаляются.

| Stable key(s) | Source / formula | Direct consumers before retirement | Active decision |
| --- | --- | --- | --- |
| `wb_stock_fact_qty*`, `total_wb_stock_fact_qty*` | the same `StocksItem.stock_total` / `stock_ru_*` values as `stock_total` / `total_stock_total` and regional canonical rows | Vitrina incident-family presentation only | archived from public catalog/read/filter/UI; raw stock and old ready evidence retained |
| `wb_stock_incident_qty*`, `total_wb_stock_incident_qty*` | exact selected physical warehouse quantity | Vitrina incident presentation | remains active/public |
| `wb_stock_effective_qty*`, `total_wb_stock_effective_qty*` | `fact − incident` | Vitrina availability presentation | remains active/public |
| `cost_price_rub`, `avg_cost_price_rub` | legacy `COST_PRICE` group rule, `max(effective_from <= slot_date)`; TOTAL is enabled-SKU average | Proxy 1 and old operator rows | audit-only; public catalog/read/filter/UI/source status excluded |
| `proxy_profit_rub`, `profit_proxy_rub`, `total_proxy_profit_rub` | `orderSum × 0.5096 − orderCount × 0.91 × cost_price_rub − ads_sum`; TOTAL sum | legacy Proxy 1 profit/margin and old saved views | retired atomically with its cost dependency |
| `proxy_margin_pct`, `proxy_margin_pct_total` | Proxy 1 profit divided by order revenue; TOTAL ratio of aggregates | legacy operator rows | retired atomically with Proxy 1 |
| `our_wb_unit_cost_rub`, `proxy_profit_3_rub`, `proxy_margin_3_pct` and TOTAL keys | canonical daily WB WAC plus versioned calculation parameters | Web Vitrina, Finance, Partner, SKU Management | remains canonical active/public |
| `own_capital_*`, `own_total_*` canonical stage/product-capital rows | functional warehouse quantity/capital/WAC projection | Web Vitrina and capital consumers | remains canonical active/public |

Saved metric-presentation state is compatible by intersection with the current server catalog: unknown retired keys are dropped from order/display/expanded anchors, active keys are appended in canonical order, and the cleaned state is persisted on migration or the next explicit user change. No dead metric row or zero-count picker option is created.
- Current 1C-based profitability keys are runtime-extended from repo code, not guessed from legacy bootstrap:
  - `onec_WB_STOCK_unit_cost_rub` = SKU-level 1C WB unit cost source metric
  - `onec_total_cost_rub` / `total_onec_total_cost_rub` = SKU/TOTAL 1C товарный капитал source metrics
  - `proxy_profit_2_rub` / `total_proxy_profit_2_rub` = proxy-profit formula with only `cost_price_rub` replaced by `onec_WB_STOCK_unit_cost_rub`
  - `proxy_margin_2_pct` / `proxy_margin_2_pct_total` = SKU `proxy_profit_2_rub / orderSum`, TOTAL `SUM(proxy_profit_2_rub) / SUM(orderSum)`
  - `inventory_capital_return_pct` / `inventory_capital_return_pct_total` = SKU `proxy_profit_2_rub / onec_total_cost_rub`, TOTAL `SUM(proxy_profit_2_rub) / SUM(onec_total_cost_rub)`
- Current management proxy WB cost keys are runtime-extended from repo code and read one shared temporal functional WB WAC projection:
  - `our_wb_unit_cost_rub` / `total_our_wb_unit_cost_rub` = `Себестоимость WB наша, ₽/шт`; it is a direct projection of canonical WB WAC and TOTAL is `SUM(WB contour capital) / SUM(WB contour quantity)`;
  - `our_wb_cost_confirmed_share_pct` / `total_our_wb_cost_confirmed_share_pct` = `Доля подтверждённой себестоимости, %`; SKU value is bucket-based `confirmed_qty / stock_qty` and may be partial, blank is allowed only when stock is zero/missing, and TOTAL is quantity-weighted `SUM(confirmed_qty) / SUM(stock_qty)` rather than an average of SKU percentages;
  - `proxy_profit_3_rub` / `total_proxy_profit_3_rub` = true `proxy прибыль 3` on every active date: before `2026-07-01` exact same-`nmId` cost and effective settings from 01.07 are projected backwards while historical order/ads operands remain date-specific; on/after the boundary exact-date cost/effective settings are used. Formula is `orderSum × buyout_rate × retained_share − orderCount × buyout_rate × canonical_WB_WAC − ads_sum`; TOTAL is the sum of complete SKU rows.
  - `proxy_margin_3_pct` / `proxy_margin_3_pct_total` = `Прокси маржинальность 3, %` / `Прокси маржинальность 3 всего, %`; SKU denominator is expected buyout revenue `orderSum × buyout_rate`, TOTAL is `SUM(SKU profit) / SUM(SKU expected buyout revenue)`. Proxy 2 is never substituted and SKU margins are never averaged.
- Preliminary WB supply cost layers may exist for status `4/6` or planned quantity, but physical daily rolling admits only final `acceptedQuantity` / `accepted_quantity` on status `5` and groups it by normalized local `accepted_date` derived from the fact date. Planned `supply_date`, status `4/6`, and `quantity/qty` do not move physical buckets. Final accepted NULL-cost quantity enters explicit estimated/unknown, `confirmed + estimated + fallback` closes to stock, and zero-stock inbound carry remains internal to recalculation while persisted buckets stay capped to stock.
- Web-vitrina read contract uses the same runtime-extended metric catalog as the DATA snapshot builder, so SKU/TOTAL our-WB rows must expose Russian labels and format metadata; confirmed share rows use `format=percent`, not raw number rendering.
- Ordinary manual/auto vitrina refresh reads the already materialized functional warehouse/cost state and never runs WB supply sync, stock fetch or Seller Portal automation. The separate bounded hourly/manual WB pipeline owns external refresh and atomic functional publication.
- Frozen snapshots created before margin 3 entered the runtime catalog are completed only by the guarded margin-3 one-off runner described above. Ordinary historical refresh, replace-existing materialization and workbook/stock importers are prohibited for this repair because they can rewrite unrelated frozen cells.
- Management proxy WB cost rows are not strict accounting FIFO. Proxy 2 remains technical archive only and cannot substitute Proxy 3; source/component statuses must stay explicit when values are estimates or pending components.
- Existing Proxy 1 keys remain evaluable only for historical/audit reproducibility; they are not active surface keys and cannot be selected by a public source-group refresh.

## 3.1.2 Daily live refresh scheduling

- Daily auto-refresh materialize-ится поверх existing heavy route through runtime-managed schedules:
  - default business rows = `11:00, 20:00 Asia/Yekaterinburg`
  - editable schedule storage = runtime JSON under the hosted runtime dir via `GET/POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules`
  - run-now route = `POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules/run-now`
  - systemd timer target = repo-owned due-check runner `apps/sheet_vitrina_v1_auto_refresh_tick.py`
  - systemd cadence = `OnCalendar=*-*-* *:00,10,20,30,40,50:00`
  - systemd timer is non-persistent; missed business-time catch-up is evaluated by the runner/runtime schedule state, not by an immediate stale systemd fire during deploy restart
  - the runner authenticates with WebCore session cookie before calling `POST /v1/sheet-vitrina-v1/refresh` with `{"async": true, "auto_refresh": true}`
  - due slot accounting is accepted-attempt based: the tick runner must not mutate `last_due_at`, mark missed due slots, or report terminal schedule state until the refresh route returns a real `job_id` or another non-concurrency terminal attempt; active-job skips keep both selected and accumulated missed due slots retryable
  - stale active auto-update jobs are surfaced explicitly in the skip payload (`active_job_stale`, `active_job_age_seconds`) and make the tick service fail visibly while preserving the due slot, instead of turning a blocked refresh into a green completed no-op
- Schedule runner/systemd wiring is repo-owned and deploys into live systemd units:
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
  - auto path делает refresh/persist ready snapshot only; legacy Google Sheets/GAS load bridge is archived and not an active completion target
  - refresh/load cycle защищён bounded mutual exclusion lock и не должен destructively смешивать parallel auto/manual/retry writes
  - runtime/status surface хранит last auto run status / timestamps separately from manual operator jobs plus latest semantic auto result payload, чтобы block `Автообновления` truthfully показывал именно результат daily auto chain
  - Apps Script remains thin shell and does not own scheduling or date math
- Legacy Proxy 1 calculation remains server-side audit logic only:
  - SKU `proxy_profit_rub` / `profit_proxy_rub` uses `{orderSum}*0,5096-{orderCount}*0,91*{cost_price_rub}-{ads_sum}`;
  - TOTAL `total_proxy_profit_rub` is the sum of SKU `proxy_profit_rub`;
  - TOTAL `proxy_margin_pct_total` is `total_proxy_profit_rub / total_orderSum`, если denominator допустим;
  - none of these rows is published through the active public catalog/read/UI.
  - 1C `proxy_profit_2_rub` uses the same coefficients and dependencies as `proxy_profit_rub`, replacing only `cost_price_rub` with `onec_WB_STOCK_unit_cost_rub`;
  - `proxy_profit_3_rub` always uses the effective versioned `buyout_rate`, included expense rates and shared canonical `our_wb_unit_cost_rub` projection; before `2026-07-01` the resolver projects same-`nmId` cost/settings from 01.07 and never reads Proxy 2;
  - `proxy_margin_3_pct` divides by `orderSum × buyout_rate`; TOTAL divides summed complete SKU profits by summed expected buyout revenue. Missing operands stay blank and a zero denominator returns blank; TOTAL is not an average of SKU percentages;
  - 1C percent totals `proxy_margin_2_pct_total` and `inventory_capital_return_pct_total` are ratio-of-aggregates, not averages of SKU rows;
  - margin 3 SKU/TOTAL rows are Python runtime extensions placed immediately after profit 3 and assigned to the same web-vitrina source/group refresh set, so profit and margin update atomically in partial merges;
  - the initial effective version is `91%` buyout, `44%` included expenses and `56%` retained share; hardcoded `0.5096/0.91` are not active Proxy 3 formula inputs.
- Пустой или неполный `COST_PRICE` dataset не валит active refresh:
  - internal `STATUS.cost_price[*]` remains truthful audit evidence;
  - the audit-only source does not downgrade public refresh status and does not appear as an active source;
  - current public truth не подменяет canonical WB WAC / Proxy 3 legacy group values.

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
- Для `stocks` bounded contour теперь применяет final classifier norm: only `yesterday_closed` is required and authoritative for semantic green, while `today_current` stays truthful blank/not_available instead of an intraday surrogate.

## 3.4 Явный live blocker

- `promo_by_price` больше не является blocked source в текущем contour:
  - `today_current` materialize-ится через repo-owned promo collector run;
  - `yesterday_closed` читается только из accepted/runtime-cached promo truth;
  - low-confidence cross-year labels не invent-ят exact dates и остаются truthful `promo_start_at/end_at = null`.
- `stocks[yesterday_closed]` больше не является declared gap: official historical Seller Analytics CSV path materialized и authoritative runtime cache `temporal_source_snapshots[source_key=stocks]` now owns the closed-day truth for this source family.
- Legacy `cogs_by_group` / `COST_PRICE` contour не используется как live fallback для active Vitrina economics: он сохраняется только для audit reproducibility, while public cost/profit reads canonical WB WAC / Proxy 3.
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

- Канонический current product flow = server-side `refresh -> web-vitrina read`; former Google Sheets `prepare -> upload -> refresh -> load` is archived/migration-only.
- Для задач, которые меняют archived bound Apps Script guard, operator UI или другой live operator surface вокруг `sheet_vitrina_v1`, `repo-complete` и local smokes недостаточны.
- Default completion для таких задач включает:
  - `clasp push` только для archived bound GAS guard changes или equivalent publish step для другого live contour, если это безопасно и доступно;
  - минимальный live verify по затронутому surface;
  - явную фиксацию, достигнуты ли `live-complete` и guard-only `sheet-complete`, если GAS guard входил в scope.
- Если изменение затрагивает registry/upload/current bundle/readiness semantics, done criteria должны проверять local smokes плюс active website/operator/public web-vitrina surfaces; legacy Google Sheets `load` не является valid completion target.
- Если изменение затрагивает public operator route или runtime publish, done criteria должны включать и public route probe, а не только router code в repo.
- Для hosted runtime/publish closure canonical repo-owned path теперь фиксирован:
  - `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy`
  - `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe`
  - `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe`
- Этот runner применим и к current branch/PR without merge-before-verify, потому что деплоит current checked-out worktree, а не требует сначала merge в `main`.
- Promo current correctness checklist: additionally run `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` after changes touching `promo_by_price`, promo archive/artifact validation, promo collector diagnostics/status handling, expected `ended_without_download` campaign handling, refresh orchestration, promo temporal acceptance/fallback, promo source-status reduction, or web-vitrina read/page-composition paths that can affect promo metric row visibility. If local CA verification blocks the public read, use `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` only as the accepted local diagnostic fallback; route timeout or bad payload remains a blocker.
- This guard verifies public `status` / `web-vitrina` / `plan`, `promo_by_price[today_current]` diagnostics, coherent `requested_count / covered_count`, zero fatal/true artifact loss counters when exposed, diagnostic-only ended/no-download artifacts and non-blank current promo rows. It does not use `/load`, Google Sheets/GAS, Sheets or browser/localStorage truth.
- Feedbacks MVP checklist: after changes touching the `Отзывы` tab, `GET /v1/sheet-vitrina-v1/feedbacks`, official feedbacks adapter/token path or unified-shell feedbacks date/filter/table UI, run `python3 apps/sheet_vitrina_v1_feedbacks_http_smoke.py` and `python3 apps/sheet_vitrina_v1_feedbacks_browser_smoke.py`. Live/public closure additionally verifies `/sheet-vitrina-v1/vitrina` and one bounded feedbacks GET against the hosted runtime; no `/load`, Google Sheets/GAS, Seller Portal bot or feedback persistence step is involved.
- Seller Portal recovery checklist: after changes touching `seller-portal-session/check`, recovery `start/status/stop/launcher` routes or centralized `Настройки → Источники и сессии`, run `python3 apps/sheet_vitrina_v1_seller_portal_recovery_http_smoke.py`, `python3 apps/sheet_vitrina_v1_seller_portal_recovery_ui_smoke.py`, `python3 apps/sheet_vitrina_v1_settings_sources_sessions_smoke.py` and `python3 apps/sheet_vitrina_v1_settings_sources_sessions_browser_smoke.py`; live/public closure additionally runs the bounded recovery/live status probe against `https://api.selleros.pro` without reintroducing recovery controls on Vitrina.
- Если `clasp` credentials для archived guard publish, live runtime access или publish rights недоступны, final handoff обязан явно назвать blocker и не маркировать задачу как fully complete.

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
- `apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`

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
- Подтверждён live/public invariant smoke через `apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` для защиты current promo rows после изменений в `promo_by_price`, refresh orchestration или web-vitrina read surface.
- Подтверждены feedbacks MVP smokes: `apps/sheet_vitrina_v1_feedbacks_http_smoke.py` для route/contract/token-path-facing normalization и `apps/sheet_vitrina_v1_feedbacks_browser_smoke.py` для вкладки `Отзывы`, compact date-range picker, star filter and manual table rendering.
- Smoke проверяет:
  - что `prepare` поднимает operator seed `33 / 102 / 7`;
  - что upload из sheet-side trigger сохраняет current truth в existing runtime без усечения `metrics_v2`;
  - что operator compatibility page `GET /sheet-vitrina-v1/operator` отдается тем же server contour и не становится 404;
  - что `GET /sheet-vitrina-v1/vitrina` и `GET /v1/sheet-vitrina-v1/web-vitrina` поднимаются тем же contour, with unified top tabs and unchanged read contract;
  - что embedded compatibility panels expose compact `Ручная загрузка данных`, separate `Лог`, fixed-height scroll viewport and `Скачать лог`, но не содержат второй `Автообновления`;
  - что `POST /v1/sheet-vitrina-v1/refresh` вызывает heavy source blocks и обновляет persisted date-aware ready snapshot;
  - что `POST /v1/sheet-vitrina-v1/load` пишет в live shell только already prepared snapshot и не триггерит heavy refresh заново;
  - что `GET /v1/sheet-vitrina-v1/status` возвращает последний persisted refresh result без live fetch и с `date_columns` / `temporal_slots` plus `server_context`, но не называет snapshot existence ordinary green success;
  - что `GET /v1/sheet-vitrina-v1/status` до первого refresh остаётся truthful `422`, но всё равно несёт `server_context`;
  - что preserved/rollover/first-load/no-baseline paths surfacing warning/error больше не маскируются под plain success;
  - что `GET /v1/sheet-vitrina-v1/job` показывает построчные start / key steps / finish / error для operator actions;
  - что `GET /v1/sheet-vitrina-v1/web-vitrina` возвращает stable library-agnostic contract и honors optional `as_of_date` without refresh/upstream fetch;
  - что `GET /v1/sheet-vitrina-v1/plan` и sheet-side `load` читают только ready snapshot и не делают live fetch;
  - что legacy `COST_PRICE` current state and group/effective-date rows remain readable as audit evidence;
  - что `cost_price_rub`, `avg_cost_price_rub` and the complete Proxy 1 dependency family are absent from active plan/public read/filter/settings/picker and source-group refresh;
  - что `stock_*`, incident/effective stock, canonical WB WAC / Proxy 3 and product-capital rows remain active;
  - что при отсутствии ready snapshot load path возвращает явную ошибку `ready snapshot missing`;
  - что `DATA_VITRINA` materialize-ит полный server-driven metric set как `date_matrix`, не режется до `7` metric keys и сразу грузит `yesterday_closed + today_current`;
  - что current-snapshot-only sources materialize-ят `yesterday_closed` через accepted-current rollover seam и не blank-ят already accepted previous-day truth;
  - что later invalid auto/manual current-only attempt не перетирает already accepted same-day snapshot;
  - что ordinary refresh/auto-refresh только читает materialized functional warehouse/cost state, а отдельный hourly/manual WB pipeline обновляет sources и публикует coherent version;
  - что runtime-extended our-WB SKU/TOTAL rows in `GET /v1/sheet-vitrina-v1/web-vitrina` expose Russian labels and percent format metadata;
  - что `our_wb_unit_cost_rub` before `2026-07-01` uses the exact same-`nmId` 01.07 retrospective projection and `proxy_profit_3_rub` is true Proxy 3 rather than a Proxy 2 alias;
  - что `proxy_margin_3_pct` / `proxy_margin_3_pct_total` are percent-formatted rows immediately after profit 3, use expected-buyout-revenue denominator and ratio-of-aggregates for TOTAL, never average SKU margins or substitute margin 2, and refresh in the same source group as profit 3;
  - что planned/open status `4/6` never changes physical daily buckets, status `4 -> 5` enters once on final acceptance fact, accepted NULL-cost inbound closes into explicit estimated quantity, and unchanged rebuild is idempotent;
  - что manual refresh не создаёт persisted long-retry tail;
  - что `STATUS` фиксирует live sources per temporal slot, `cost_price[*]` coverage и current/closed promo source facts `promo_by_price[*]` with collector trace/debug note;
  - что public `promo_by_price[today_current]` diagnostics не превращают expected ended/no-download campaign в fatal missing artifact и что current promo metric rows не становятся all blank;
  - что service/status block `CONFIG!H:I` сохраняется и не перезаписывается при load.

# 7. Что уже доказано по модулю

- В `wb-core` появился первый bounded end-to-end MVP для `VB-Core Витрина V1`.
- Sheet-side upload registry больше не обрезает `METRICS` до subset: current truth хранит полный uploaded compact dictionary `102` rows.
- Historical table flow больше не является active contour: current path has explicit refresh/build action and cheap web-vitrina read path from repo-owned ready snapshot; reverse-load обратно в Google Sheets `DATA_VITRINA` archived.
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
