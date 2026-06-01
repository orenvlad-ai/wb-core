---
title: "Register: module status and checkpoints"
doc_id: "WB-CORE-PROJECT-05-MODULE-STATUS"
doc_type: "register"
status: "active"
purpose: "Дать compact register смёрженных модулей и current checkpoints без чтения всех module docs подряд."
scope: "Семейства модулей, диапазоны `01–34`, текущий статус `main`, главный current checkpoint и открытые хвосты."
source_basis:
  - "docs/modules/00_INDEX__MODULES.md"
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
source_of_truth_level: "derived_secondary_project_pack"
related_docs:
  - "docs/modules/00_INDEX__MODULES.md"
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
update_triggers:
  - "merge нового модуля"
  - "изменение main-confirmed checkpoint"
  - "смена статуса family/gap"
built_from_commit: "623dcc17ad637f04e601f67f71bcb627881cadaa"
---

# Summary

На текущем `main` main-confirmed module set уже доходит до `34`.

Практически это значит:
- source/data foundation уже materialized;
- registry upload line уже замкнута до HTTP entrypoint;
- sheet-side line reached bounded MVP historically, but Google Sheets/GAS load/write contour is now archived/do-not-use.
- web-vitrina line уже имеет stable route/contract seam, отдельный library-agnostic `view_model`, concrete grid adapter layer и real live page composition на sibling route, plus read-only feedbacks, research tab, active 1C product-capital source group, server-side metric presentation user-config and weighted TOTAL search CTR in the same unified shell.
- supplier shipments line materialized as module `34`: `Поставки -> От поставщика` registry, XLSX invoice parser, nomenclature/compatibility matching, runtime storage and protected operator/supplier surfaces.

# Current norm

| Range | Family | Current status |
| --- | --- | --- |
| `01–10` | `web-source` + `official-api` | смёржены в `main`, bounded source blocks подтверждены |
| `11–12` | `rule-based` | смёржены в `main` |
| `13–16` | `table-facing` / `projection` / `wide-matrix` | смёржены в `main` |
| `17–19` | `archived sheet-side scaffold/write/presentation` | Google Sheets contour archived / do not use; retained only as migration evidence |
| `20–23` | `registry upload line` | смёржены в `main` до live HTTP entrypoint plus hosted public-route allowlist/deploy publication boundary, app-level auth, auth-aware canonical probes, EU current-live target metadata, production HTTPS/domain/TLS invariant and rollback-only selleros write guard |
| `24–26` | `web/operator + archived sheet-side history` | current web/operator contour active; Google Sheets/GAS side archived |
| `27` | `browser-capture collector` | смёржен в `main` как bounded local promo XLSX collector contour |
| `28` | `browser-capture live wiring` | смёржен в `main` как promo live source seam inside refresh/runtime/read-side |
| `29–31` | `web-vitrina seams` | смёржены в `main` как stable read/view-model/adapter ladder plus real sibling page composition, включая read-only feedbacks tab and transient AI review flow |
| `32` | `web/operator/research` | смёржен в `main` как read-only SKU group comparison over accepted truth / ready snapshots |
| `33` | `external-1c-source` | active/current in repo: 1C/Soykasoft stocks+cost source, date-specific load, web-vitrina source group `onec_product_capital`, 1C товарный капитал rows and 1C profitability metrics |
| `34` | `supplier-shipments` | active/current in repo: supplier invoice order registry, XLSX parser, runtime SQLite/filesystem storage, protected API/UI, settings/nomenclature, exact/alias/compatibility matching and optional supplier-only role boundary |

## Current checkpoint ladder

1. `sku_display_bundle_block`
2. `table_projection_bundle_block`
3. `registry_pilot_bundle`
4. `wide_data_matrix_v1_fixture_block`
5. `wide_data_matrix_delivery_bundle_v1_block`
6. `sheet_vitrina_v1_scaffold_block`
7. `sheet_vitrina_v1_write_bridge_block`
8. `sheet_vitrina_v1_presentation_block`
9. `registry_upload_bundle_v1_block`
10. `registry_upload_file_backed_service_block`
11. `registry_upload_db_backed_runtime_block`
12. `registry_upload_http_entrypoint_block`
13. `sheet_vitrina_v1_registry_upload_trigger_block`
14. `sheet_vitrina_v1_registry_seed_v3_bootstrap_block`
15. `sheet_vitrina_v1_mvp_end_to_end_block`
16. `promo_xlsx_collector_block`
17. `promo_live_source_wiring_block`
18. `web_vitrina_view_model_block`
19. `web_vitrina_gravity_table_adapter_block`
20. `web_vitrina_page_composition_block`
21. `research_sku_group_comparison_block`
22. `onec_stocks_block`
23. `supplier_shipments_block`

## Operator-facing checkpoint

Current main-confirmed operator flow:
- `POST /v1/sheet-vitrina-v1/refresh`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/job`
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition&include_source_status=1`
- `GET/POST /v1/sheet-vitrina-v1/web-vitrina/user-config`
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh`
- `GET /v1/sheet-vitrina-v1/daily-report`
- `GET /v1/sheet-vitrina-v1/stock-report`
- `GET /v1/sheet-vitrina-v1/plan-report`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx`
- `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-status`
- `GET /v1/sheet-vitrina-v1/feedbacks`
- `POST /v1/sheet-vitrina-v1/feedbacks/export.xlsx`
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints`
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status`
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job`
- `GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options`
- `POST /v1/sheet-vitrina-v1/research/sku-group-comparison/calculate`
- `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
- `GET /login`, `POST /login`, `GET /logout`, `POST /logout`
- `GET /sheet-vitrina-v1/supplier`
- `GET /sheet-vitrina-v1/settings`
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/supply/supplier-shipments...`
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/nomenclature...`
- `GET /sheet-vitrina-v1/operator`
- `GET /sheet-vitrina-v1/vitrina`
- former Google Sheets `prepare/upload/load DATA_VITRINA` flow is archived and blocked by guards

Current sibling cost-price flow:
- server-side `POST /v1/cost-price/upload`
- flow обновляет separate authoritative dataset, а existing refresh/read contour затем использует его server-side in website/operator/web-vitrina

Current 1C/Soykasoft source flow:
- source module = `onec_stocks_block`
- source key = `onec_stocks`
- web-vitrina source group = `onec_product_capital` / `1С / товарный капитал`
- live path = `/hs/soykasoft/stocks_wb?account_id=<account_id>&date=<YYYY-MM-DD>&nmId=<nmId>`
- historical/current snapshots are date-specific; requested historical load is accepted only when `payload.meta.date` equals the requested date
- current stage buckets are `CHINA_TO_FF`, `FF_STOCK`, `FF_TO_WB`, `WB_STOCK`; aliases `CN_TO_RU_TRANSIT` and `FF_TO_WB_TRANSIT` fold into the current buckets
- current 1C metric rows include 1C stage qty/unit-cost/capital rows, SKU/TOTAL товарный капитал, `proxy_profit_2_rub`, `proxy_margin_2_pct` and `inventory_capital_return_pct` families
- a fresh successful exact-date 1C payload with full active SKU coverage may be validly empty for any canonical stage bucket; runtime materializes that bucket's qty/unit-cost/capital rows as `0` with zero-stock diagnostics, while source errors, date mismatch, unmapped stages and partial SKU coverage remain warning/error/blank and preserve accepted same-date truth only as fallback for invalid attempts

Current sibling local promo collector precursor flow:
- `python3 apps/promo_xlsx_collector_live.py`
- flow делает bounded seller-portal capture только вне repo tree, reuse-ит unchanged archived campaign artifacts и materialize-ит `metadata.json` для каждого promo plus `workbook.xlsx` для downloaded/reused current promo
- contour remains the thin browser-capture precursor under the live-wired promo source seam

Current live promo source flow:
- `POST /v1/sheet-vitrina-v1/refresh`
- contour now invokes repo-owned archive-first promo collector server-side for `promo_by_price[today_current]`
- `promo_by_price[yesterday_closed]` now attempts corrective interval replay first on every refresh
- accepted/runtime-cached exact-date promo truth stays only as bounded fallback when replay is unavailable or non-exact
- `STATUS` and ready snapshot now expose truthful promo source facts instead of a permanent blocked gap
- normalized campaign row archive (`campaign_rows.jsonl` + `campaign_rows_manifest.json`) preserves replay-critical campaign truth with workbook fingerprint/source metadata, so raw workbook/debug artifacts are no longer the only historical replay substrate
- refresh-integrated `promo_refresh_light_gc_v1` runs after normalized archive + ready snapshot persistence, deletes only guarded old/debug/duplicate candidates, protects current run and unknown/incomplete artifacts, and records a structured `promo_artifact_gc` summary

Current repo-owned unified web/operator surface:
- primary route = `GET /sheet-vitrina-v1/vitrina`; first/default tab = `Витрина`, sibling tabs = `Поставки`, `Отчёты`, `Отзывы` and `Исследования`
- compatibility route = `GET /sheet-vitrina-v1/operator`; it renders the same unified shell and is not a separate source-of-truth owner
- page uses current read/action routes: `POST /v1/sheet-vitrina-v1/refresh`, `GET /v1/sheet-vitrina-v1/status`, `GET /v1/sheet-vitrina-v1/job`, `GET /v1/sheet-vitrina-v1/daily-report`, `GET /v1/sheet-vitrina-v1/stock-report`, `GET /v1/sheet-vitrina-v1/plan-report`, `GET /v1/sheet-vitrina-v1/feedbacks`, `feedbacks/export.xlsx`, `feedbacks/ai-prompt`, `feedbacks/ai-analyze`, `feedbacks/complaints`, `feedbacks/complaints/sync-status`, `feedbacks/complaints/submit-selected`, `feedbacks/complaints/submit-job`, `feedbacks/automation/schedules`, `feedbacks/automation/run-now`, `feedbacks/automation/runs`, `feedbacks/automation/run`, `feedbacks/automation/tick`, research SKU-group comparison routes, seller-session/recovery routes, `GET/POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules` and `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh`, including group id `onec_product_capital`
- former Google Sheets `/load` stays archived/blocked and is not needed for current web-vitrina completion
- `GET /v1/sheet-vitrina-v1/web-vitrina` stays server-owned and library-agnostic on the default path: current v1 shape is `meta + status_summary + schema + rows + capabilities`, built only from existing ready snapshot/current truth and optional `as_of_date`
- phase-2 web-vitrina materializes repo-owned `web_vitrina_view_model` over that stable contract: current schema = `columns + rows + groups + sections + formatters + filters + sorts + state_model`
- phase-3 web-vitrina materializes repo-owned `web_vitrina_gravity_table_adapter` over that `view_model`: current Gravity-specific surface = `columns + rows + renderers + groupings + filters + sorts + use_table_options + table_props + state_surface`
- phase-4 web-vitrina materializes repo-owned `web_vitrina_page_composition` via optional `surface=page_composition`; the page shell renders compact table header/history controls, main table first, then `Автообновления`, server-configured `Метрики` presentation block and bottom `Действия и состояния`
- visual system is dark across `Витрина`, embedded `Поставки` / `Отчёты`, `Отзывы` and `Исследования`; primary/action accent is violet/indigo, while green is kept only for semantic success/status signals
- the same unified shell exposes `Отзывы` as a manual read-only WB feedbacks table with strict server-side date/star/answered filters, 62-day feedback date picker, chunked WB pagination, diagnostic meta, Excel export for the current table, bounded internal horizontal scroll for wide columns, resizable columns, official review tags/actionable resolver, optional transient AI review columns, nested `Жалобы` over runtime complaint journal/status sync plus protected selected-row submit jobs, and nested `Авто-жалобы` over runtime schedules/run-now/tick reports; prompt storage, AI output and auto-complaints run stats are operational/transient, not ЕБД/accepted truth
- the same unified shell exposes `Исследования` as read-only SKU group comparison; options/calculate use active SKU truth, selectable non-financial metrics and persisted ready snapshots only
- current live vitrina action/status semantics:
  - `Загрузить и обновить` = canonical full refresh: `POST /v1/sheet-vitrina-v1/refresh` + ready snapshot materialize + page reread, without Google Sheets write dependency; both manual and automatic triggers use `Asia/Yekaterinburg` `today_current` / `yesterday_closed` slots and keep expected stale/missing source groups visible in semantic status/logs
  - the old cheap top-panel `Обновить`, `JSON Connect` and permanent top status badge are not rendered
  - table header keeps browser-owned page refresh time separate from server-owned freshness and the load-adjacent latest-window status lamp; old selected-period errors outside the latest two-day window do not force the visible load state red
  - visible table controls are compact: period/section/group near the load action, no separate `Фильтры и настройки` card, and obsolete search/columns/reset/type/sort controls are ignored safely
  - `Метрики` presentation has separate `Итого` and `SKU` scopes, drag-and-drop ordering, transient selected-row batch mode and display states `Показано` / `Свернуто` / `Скрыто`; canonical settings persist through auth-protected server user-config, localStorage is cache/one-time migration only, collapsed rows reveal under the nearest shown metric through icon-only disclosure, hidden rows stay absent from the main table
  - `CTR в поиске средний` TOTAL uses weighted aggregation by SKU `views_current`, not arithmetic mean of SKU CTR ratios; valid zero CTR remains zero
  - `Загрузка данных` is lazy: initial page composition renders `not_loaded` plus `Загрузить`, then explicit `include_source_status=1` loads a grouped compact table over source truth (`WB API`, `1С / товарный капитал`, `Seller Portal / бот`, `Прочие источники`); every visible main-table metric belongs to exactly one group, with residual calculated/formula metrics assigned to `Прочие источники`
  - each group has one compact date control, `Обновить группу`, group-level last update timestamp, today/yesterday status columns, reason columns, Russian metric labels and secondary technical endpoint text
  - `Seller Portal / бот` additionally exposes session status and `Проверить сессию` / `Восстановить сессию` / `Скачать лаунчер`
  - `Лог` renders below the loading table and keeps existing job/log download contour
  - former sibling block `Обновление данных` is no longer an active page-composition activity block; persisted `STATUS` rows remain underlying read-side truth
  - raw STATUS/job note, JSON fragments, traceback text, request ids and similar diagnostics stay only in existing log/download surfaces
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` accepts `{async: true, source_group_id, as_of_date}` for one source group and one selected date; it must not clear, overwrite or timestamp unrelated groups/date cells; for `onec_product_capital`, fresh empty canonical 1C stage buckets are materialized as structural zeros, not blanks
- group-refresh/full-refresh job results may include `updated_cells`/latest-confirmed metadata for transient browser-session highlighting only; current UI uses green/amber text-color emphasis without legacy light cell backgrounds and no permanent styling truth is persisted
- the sibling page keeps bounded history controls: no-query default opens the backend-owned rolling `2 недели` period `D-13..D` ending on `today_current_date`, explicit `as_of_date`, and `date_from/date_to` period mode over existing ready snapshots plus truthful blank/partial cells for visible dates without ready snapshots; stale April/browser-local period state cannot override the default, and old always-expanded `История` / `Фильтры и настройки` blocks are replaced by compact in-header controls
- `web_vitrina_view_model` remains canonical and library-agnostic, the Gravity adapter stays isolated repo-side, and the page layer stays a page-only consumer instead of a second truth owner
- current phase-1/2/3/4 scope remains narrow: route fixation, stable read contract, library-agnostic presentation seam, concrete grid adapter and server-driven page composition only; export layer, legacy Google Sheets/export migration and broad feature parity stay later
- `Отчёты` uses one sibling subsection selector: `Ежедневные отчёты`, `Отчёт по остаткам`, `Выполнение плана`; only one report body is visible at a time
- daily-report compares only the two latest persisted ready snapshot dates `<= default_business_as_of_date(now)` in `Asia/Yekaterinburg`; it reads their `yesterday_closed` slots, never `today_current`, and does not require a ready snapshot for a not-yet-materialized default closed day during night-boundary reads
- daily-report ranked totals stay on the current canonical pool only (`total_view_count`, `total_views_current`, `avg_ctr_current`, `avg_addToCartConversion`, `avg_cartToOrderConversion`, `avg_spp`, `avg_ads_bid_search`, `total_ads_views`, `total_ads_sum`, `avg_localizationPercent`)
- stock-report defaults to the latest persisted ready snapshot `<= default_business_as_of_date(now)` and reads `DATA_VITRINA[yesterday_closed]`; explicit `as_of_date` remains strict exact-read without fallback/upstream fetch, threshold stays `<50`, five district keys are used, and merged bucket `stock_ru_far_siberia` / `ДВ и Сибирь` is deliberately excluded
- stock-report exposes a compact SKU selector sourced from full active `config_v2` truth, not breached rows only; selector applies only after `Рассчитать`, rejects zero selected SKU and handles empty selected-subset results truthfully
- plan-report `Выполнение плана` is read-only on `GET /v1/sheet-vitrina-v1/plan-report`; primary query uses `period`, `h1_buyout_plan_rub`, `h2_buyout_plan_rub`, planned DRR percent and optional `as_of_date` / contract-start params; legacy Q1-Q4 params are transitional fallback only
- plan-report response is per-block (`selected_period`, `month_to_date`, `quarter_to_date`, `year_to_date`): available selected period must render even when another block is partial/unavailable
- plan-report facts come from persisted accepted closed-day snapshots for `fin_report_daily.fin_buyout_rub` and `ads_compact.ads_sum` with independent per-source coverage; optional full-month `manual_monthly_plan_report_baseline` may fill complete months only inside this route, without replacing accepted daily snapshots or other reports
- plan-report baseline routes provide controlled XLSX template/upload/status; one-off ready-fact reconcile may insert missing accepted daily report slots from server-side ready snapshots but never overwrites existing diffs or fabricates blank zeros
- user-facing `ЕБД` / `единая база данных` means the shared server-side accepted truth/runtime layer consumed by web-vitrina and reports, not Google Sheets/GAS, browser UI/localStorage or report-private manual facts
- browser persistence is namespaced localStorage only for current top-level tab, supply/report subsections and stock-report SKU selector; broken storage or obsolete `nmId` values fall back to current defaults without server-side user profile state
- status/refresh responses drive blocks through `server_context` + `manual_context`, so timezone/scheduler wording, manual-success timestamps and latest semantic summaries are not hardcoded in UI
- `GET /v1/sheet-vitrina-v1/status` exposes semantic root status for the visible snapshot and keeps technical completion separate, so preserved/unchanged/stale/not_verified results stay yellow or red instead of false green
- job/log surface is detailed and machine-useful: source/module/adapter/endpoint steps, source result kinds/counts, metric batch summaries and write results stay server-driven and can be exported per completed run through `GET /v1/sheet-vitrina-v1/job?job_id=...&format=text&download=1`
- server-side business timezone = `Asia/Yekaterinburg` for default `as_of_date`, `today_current` and operator-facing freshness dates
- live daily web-vitrina auto-refresh = repo-owned due-check timer `wb-core-sheet-vitrina-refresh.timer` every 10 minutes -> `apps/sheet_vitrina_v1_auto_refresh_tick.py` -> runtime JSON schedule rows (default `11:00`, `20:00 Asia/Yekaterinburg`) -> protected `POST /v1/sheet-vitrina-v1/refresh` with session auth and `auto_refresh=true`; daily path builds server-side ready snapshot only, never loads Google Sheets, and page/API owns read/write/run-now schedule state
- active hosted target = `wb-core-eu-root` / `89.191.226.88` / `/opt/wb-core-runtime/state`; production public endpoint is `https://api.selleros.pro`; app-level auth protects the public/operator surface; canonical probes are auth-aware and fast by default, with heavy refresh only under explicit `--include-refresh`; current-live nginx must keep `server_name 89.191.226.88 api.selleros.pro;` plus `listen 443 ssl`; old `selleros-root` / `178.72.152.177` is rollback-only and routine writes are blocked before SSH/rsync/nginx/systemd
- source matrix is explicit: group A bot/web-source historical, group B WB API historical/date-period capable, group C WB API current-snapshot-only, group D other/manual/browser-collector overlays, group E 1C product-capital date-capable source
- `seller_funnel_snapshot` materialization can receive enabled/relevant `nm_ids`; strict validation is applied after relevant-row filtering, so invalid non-relevant rows are logged as `ignored_non_relevant_invalid_rows` instead of poisoning the snapshot
- bot-backed current-day sync probes `/opt/wb-web-bot/storage_state.json` before seller portal capture; invalidated browser state surfaces as `seller_portal_session_invalid` / human `сессия seller portal больше не действует; требуется повторный вход`
- all Seller Portal browser runners use shared single-flight lock `/opt/wb-core-runtime/state/seller_portal_automation.lock.json`; status sync, submit/batch, auto-complaints tick/run-now, scouts/probes/dry-run/confirmation/detail/relogin and parser/export must not run in parallel, and route launches return controlled `seller_portal_automation_busy` metadata when lock is held
- route-specific Seller Portal capabilities are checked per task (`feedbacks_list`, `complaints_pending`, `complaints_answered`, supplier context). `Мои жалобы` jobs warm up the base/UI context before direct status URLs and stop on auth redirects with precise blockers.
- seller-portal auth recovery on selleros uses repo-owned localhost-only noVNC/Xvfb path via `apps/seller_portal_relogin_session.py`; unified UI exposes session-check/start/status/stop/launcher controls with per-run `run_id`, safe stop and canonical supplier confirmation
- steady-state bot-backed source materialization on EU uses localhost owner runtime API `http://127.0.0.1:8000` owned by `wb-ai-api.service` after `/opt/wb-web-bot/bot` capture and `/opt/wb-ai/run_web_source_handoff.py`; env overrides for web-source/seller-funnel base URLs are exceptional owner-runtime relocation knobs, not public nginx routes
- real complaint submit for `Отзывы/Жалобы` and `Отзывы/Авто-жалобы` is intentionally guarded: protected selected-row operator jobs, auto-complaints jobs and support runners may submit only after exact feedback/AI-row match and hard caps, while confirmation/detail probes are read-only evidence for uncertain attempts; broad/unauthenticated/browser-side submit remains forbidden
- historical/date-period families (`seller_funnel_snapshot`, `web_source_snapshot`, `sales_funnel_history`, `sf_period`, `stocks`, `ads_compact`, `fin_report_daily`) use persisted accepted closed-day semantics for `yesterday_closed`
- current-snapshot/accepted-rollover families (`prices_snapshot`, `ads_bids`, `spp`) capture upstream truth as current-visible snapshots, but an already accepted snapshot for business day D must materialize as `yesterday_closed=D` on D+1; later invalid auto/manual attempts must not blank prior-day accepted truth or already accepted same-day truth
- semantic reduction is now source-aware instead of naive two-slot worst-case:
  - `stocks` uses `yesterday_closed_only`: required slot = `yesterday_closed`, `today_current` stays truthful `not_available`/blank and does not degrade source or aggregate semantic status by itself;
  - `fin_report_daily` uses `dual_day_intraday_tolerant`: intraday current-day non-yield is tolerated when `yesterday_closed` is confirmed;
  - `prices_snapshot`, `ads_bids` and `spp` remain OK for accepted-current rollover / latest-confirmed filled values; SPP visible source is Seller Portal `discountOnSite`, and legacy sales-row average is only an explicit fallback mode, not fresh current-visible truth;
  - `promo_by_price` remains OK for accepted/runtime-cached latest confirmed filled values and degrades only when a required visible value lacks fallback;
  - `seller_funnel_snapshot` and `web_source_snapshot` remain strict two-slot sources.
- manual operator refresh keeps short retries inside the run but does not create persisted long-retry tails and does not overwrite accepted truth on invalid candidates
- promo source follows the same accepted-truth norm:
  - invalid current attempt must not overwrite already accepted same-day promo truth
  - `yesterday_closed` must first try corrective interval replay and may fall back to accepted/runtime cache only when replay is unavailable or non-exact
  - low-confidence cross-year labels keep null exact dates instead of invented dates
- live retry completion is bounded by repo-owned runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py` plus repo-owned artifacts `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.{service,timer}` installed on host as `wb-core-sheet-vitrina-closure-retry.timer`; the runner covers due `yesterday_closed` for the full historical/date-period matrix and same-day current-only capture retries only within the current business day

Current additional operator supply flow on the same page:
- top-level tab `Поставки` keeps inner tabs `Расчёты` / `От поставщика`: `Расчёты` keeps the existing factory/regional calculators, while `От поставщика` materializes the supplier order registry.
- `От поставщика` renders `订单登记表 / Order registry / Реестр заказов`, accepts XLSX invoice uploads, stores original invoices under runtime dir, persists headers/lines/nomenclature in SQLite, requires only shipment date after parse, downloads original invoice, supports operator delete and optional supplier-only role isolation.
- Supplier order matching is deterministic through server-side nomenclature: exact `product_type|normalized_model`, aliases, then compatible iPhone model overlap; `nmId` and nomenclature are auto-filled only for deterministic matches, ambiguous/unmatched rows stay visible, and the order card hides editable Supplier/Customer and order SKU fields while the registry shows supplier fallback `HanShang Technology`.
- the `Расчёты` tab keeps the existing page pattern and materializes two bounded sibling blocks: `Заказ на фабрике` and `Поставка на Wildberries`
- operator vocabulary inside these sibling blocks is unified around `period average / lead times / safety / batch / cycle`; factory now materializes `cycle_order_days`, while WB regional keeps the same math under `cycle_supply_days`
- current operator UX uses auto-upload after file selection, subtle delete icons for current uploaded files and a clickable `sheet_vitrina_v1` link to the bound live spreadsheet
- `Остатки ФФ` is a shared server-owned dataset block for both calculations; the same uploaded workbook/state is reused, not duplicated
- all operator XLSX templates in this block use Russian headers; backend keeps the machine mapping server-side
- `Остатки ФФ` is prefilled from current active SKU truth and requires exactly one row per active SKU
- `Товары в пути от фабрики` and `Товары в пути от ФФ на Wildberries` are compact event-based templates: one row = one expected inbound, duplicate `nmId` is allowed there and summed only when the planned arrival date falls within the planning horizon
- current repo had no other authoritative source for legacy parity term `FF -> WB inbound`, so the bounded flow uses a separate operator upload contract instead of silently dropping that coverage component
- operator-facing label for batch size = `Кратность штук в коробке`
- inbound files are optional for calculation; when absent or deleted, both inbound coverage terms truthfully become `0`
- inbound rows with `Количество в пути = 0` are accepted and ignored by backend normalization; they do not count as validation errors and do not contribute to coverage
- if an inbound workbook contains only zero rows after filtering, backend still stores it as an accepted uploaded dataset with truthful `row_count = 0`
- each upload block now surfaces the current uploaded filename plus download/delete actions from backend state
- current UI no longer hard-caps `sales_avg_period_days`; authoritative `orderCount` history now lives server-side in `temporal_source_snapshots[source_key=sales_funnel_history]`, so any positive covered window is allowed and blocker appears only when requested range falls outside runtime coverage
- live `DATA_VITRINA` may be used only as one-time migration input for bounded historical reconcile window `2026-03-01..2026-04-18`; sheet does not become a permanent source of truth
- future exact-date sales history continues to materialize through existing refresh/runtime flow, so the historical bootstrap is bounded and not a recurring operator step
- XLSX generation is hardened so operator templates and recommendation files open as standard XLSX workbooks without a repair path
- calculation, result XLSX and `Общее количество / Расчётный вес / Расчётный объём` summary stay fully server-driven
- regional block adds server-side district allocation with truthful `deficit = full_recommendation - allocated_qty`, a compact per-district summary table with row-level XLSX actions and one separate XLSX file per canonical district key; each district XLSX includes `nmId / SKU / Количество к поставке / Дефицит`
- current bounded regional methodology uses total SKU `orderCount` plus current district stocks and then applies legacy box allocation against shared `stock_ff`; inbound `ФФ -> WB` is intentionally not materialized for this block in the current checkpoint

Current main-confirmed counts для этого flow:
- prepare/upload package = `33 / 102 / 7`
- uploaded registry displayed-metric baseline = `95`; current web-vitrina can be runtime-extended by active 1C product-capital metrics from `onec_stocks_block`
- refresh materialize-ит date-aware ready snapshot `yesterday_closed + today_current`
- historical/operator `DATA_VITRINA` uploaded baseline = server-driven two-day `date_matrix` `1698` rendered rows / `95` uploaded metric keys (`1631` source rows, `34` blocks); these counts are not a cap on runtime-extended 1C rows
- operator-facing `STATUS` = per-source/per-slot freshness surface; current-snapshot-only sources (`prices_snapshot`, `ads_bids`) now expose `accepted_current_rollover` semantics for `yesterday_closed` and preserve accepted truth across later invalid attempts, `stocks[yesterday_closed]` resolves through historical exact-date runtime snapshots, `stocks[today_current]` stays truthful `not_available`/blank, а failed later attempts preserve the last accepted truth instead of blank/zero overwrite
- bot/web-source family (`seller_funnel_snapshot`, `web_source_snapshot`) uses bounded `explicit-date -> latest-if-date-matches` reads for `today_current`; exact-date runtime cache may truthfully surface previous captured day as next `yesterday_closed`, with explicit `STATUS` note instead of slot-copying
- if exact-date `today_current` snapshot is still missing for bot/web-source family, refresh may bounded-trigger server-local same-day capture in `/opt/wb-web-bot` plus `/opt/wb-ai/run_web_source_handoff.py` before final read-side fetch
- promo browser-capture family now uses bounded sidecar/workbook mapping server-side: workbook alone is insufficient, and `STATUS.promo_by_price[*].note` exposes collector trace plus current/future/past/ambiguous counts
- sibling `COST_PRICE` contour = отдельный sheet/menu/upload path и separate runtime current-state seam вне compact bundle
- current operator-facing cost overlay = server-side `cost_price_rub`, `avg_cost_price_rub`, `total_proxy_profit_rub`, `proxy_margin_pct_total` с resolution `group + latest effective_from <= slot_date`

This is the first bounded MVP checkpoint, not final production parity.

# Known gaps

- full legacy parity beyond current main-confirmed sheet/upload dictionary;
- promo source seam itself больше не является gap; remaining tail = broader live numeric parity beyond the currently wired promo-backed metric subset and beyond current `COST_PRICE` overlay;
- отдельный bounded fix по любому remaining non-district / foreign stocks residual, если одной truthful `STATUS` note окажется недостаточно для operator flow;
- production hardening around runtime/deploy/auth and storage binding;
- generic orchestration platform beyond current bounded auto + retry timers;
- actual live deploy rights for hosted contour;
- unresolved long-tail compatibility around `AI_EXPORT`.

# Not in scope

- Полные module doc narratives.
- Artifact/evidence details по каждому модулю.
- Hidden operational deploy knowledge вместо repo-owned contract.
