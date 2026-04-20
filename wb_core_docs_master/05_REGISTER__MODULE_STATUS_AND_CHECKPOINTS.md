---
title: "Register: module status and checkpoints"
doc_id: "WB-CORE-PROJECT-05-MODULE-STATUS"
doc_type: "register"
status: "active"
purpose: "Дать compact register смёрженных модулей и current checkpoints без чтения всех module docs подряд."
scope: "Семейства модулей, диапазоны `01–30`, текущий статус `main`, главный current checkpoint и открытые хвосты."
source_basis:
  - "docs/modules/00_INDEX__MODULES.md"
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
source_of_truth_level: "secondary_project_pack"
related_docs:
  - "docs/modules/00_INDEX__MODULES.md"
  - "README.md"
  - "docs/architecture/00_migration_charter.md"
  - "docs/architecture/01_target_architecture.md"
update_triggers:
  - "merge нового модуля"
  - "изменение main-confirmed checkpoint"
  - "смена статуса family/gap"
built_from_commit: "ae486b1ff53136a633fc34389f1c5b025a3d180c"
---

# Summary

На текущем `main` main-confirmed module set уже доходит до `31`.

Практически это значит:
- source/data foundation уже materialized;
- registry upload line уже замкнута до HTTP entrypoint;
- sheet-side line уже дошла до bounded MVP `prepare -> upload -> refresh -> load`.
- web-vitrina line уже имеет stable route/contract seam, отдельный library-agnostic `view_model`, первый concrete grid adapter layer и real live page composition на sibling route.

# Current norm

| Range | Family | Current status |
| --- | --- | --- |
| `01–10` | `web-source` + `official-api` | смёржены в `main`, bounded source blocks подтверждены |
| `11–12` | `rule-based` | смёржены в `main` |
| `13–19` | `table-facing` / `projection` / `wide-matrix` / `sheet-side scaffold` | смёржены в `main` |
| `20–23` | `registry upload line` | смёржены в `main` до live HTTP entrypoint |
| `24–26` | `sheet-side operator line` | смёржены в `main` до первого bounded MVP |
| `27` | `browser-capture collector` | смёржен в `main` как bounded local promo XLSX collector contour |
| `28` | `browser-capture live wiring` | смёржен в `main` как promo live source seam inside refresh/runtime/read-side |
| `29–31` | `web-vitrina seams` | смёржены в `main` как stable read/view-model/adapter ladder plus real sibling page composition |

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

## Operator-facing checkpoint

Current main-confirmed operator flow:
- `Подготовить листы CONFIG / METRICS / FORMULAS`
- `Отправить реестры на сервер`
- `POST /v1/sheet-vitrina-v1/refresh`
- `Загрузить таблицу`

Current sibling operator input flow:
- `Подготовить лист COST_PRICE`
- `Отправить себестоимости`
- flow обновляет separate `COST_PRICE` authoritative dataset, а existing refresh/read contour затем использует его server-side в current `DATA_VITRINA` / `STATUS`

Current sibling local promo collector precursor flow:
- `python3 apps/promo_xlsx_collector_live.py`
- flow делает bounded seller-portal capture только вне repo tree, reuse-ит unchanged archived campaign artifacts и materialize-ит `metadata.json` для каждого promo plus `workbook.xlsx` для downloaded/reused current promo
- contour remains the thin browser-capture precursor under the live-wired promo source seam

Current live promo source flow:
- `POST /v1/sheet-vitrina-v1/refresh`
- contour now invokes repo-owned archive-first promo collector server-side for `promo_by_price[today_current]`
- `promo_by_price[yesterday_closed]` still reads only accepted/runtime-cached exact-date promo truth
- cache miss on `promo_by_price[yesterday_closed]` may be filled server-side by interval replay from archived campaign artifacts when authoritative coverage exists
- `STATUS` and ready snapshot now expose truthful promo source facts instead of a permanent blocked gap

Current repo-owned operator refresh surface:
- `GET /sheet-vitrina-v1/operator`
- page uses `POST /v1/sheet-vitrina-v1/refresh`, `POST /v1/sheet-vitrina-v1/load`, `GET /v1/sheet-vitrina-v1/daily-report`, `GET /v1/sheet-vitrina-v1/stock-report`, `GET /v1/sheet-vitrina-v1/status` and `GET /v1/sheet-vitrina-v1/job`
- current `/sheet-vitrina-v1/operator` remains orchestration-first control surface and is intentionally not reused as `/sheet-vitrina-v1/operator/vitrina`
- web-vitrina is fixed as sibling routes `GET /sheet-vitrina-v1/vitrina` + `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina` stays server-owned and library-agnostic on the default path: current v1 shape is `meta + status_summary + schema + rows + capabilities`, built only from existing ready snapshot/current truth and optional `as_of_date`
- phase-2 web-vitrina additionally materializes repo-owned `web_vitrina_view_model` over that stable contract: current schema = `columns + rows + groups + sections + formatters + filters + sorts + state_model`
- phase-3 web-vitrina additionally materializes repo-owned `web_vitrina_gravity_table_adapter` over that `view_model`: current Gravity-specific surface = `columns + rows + renderers + groupings + filters + sorts + use_table_options + table_props + state_surface`
- phase-4 web-vitrina additionally materializes repo-owned `web_vitrina_page_composition` on the same read route via optional `surface=page_composition`, while `/sheet-vitrina-v1/vitrina` becomes a real read-only page with summary, filters, table container and truthful loading/empty/error states
- `web_vitrina_view_model` remains canonical and library-agnostic, the concrete Gravity-specific adapter stays isolated repo-side, and the page layer stays a page-only consumer instead of a second truth owner
- current phase-1/2/3/4 scope remains narrow: route fixation, stable read contract, library-agnostic presentation seam, concrete grid adapter and minimal live page composition only; export layer, Google Sheets cutover and broad feature parity stay later
- page stays intentionally narrow: top-level sections `Обновление данных` / `Расчёт поставок` / `Отчёты`, compact manual block `Ручная загрузка данных` with embedded buttons `Загрузить данные` / `Отправить данные` and only two persisted manual-success fields `Последняя удачная загрузка` / `Последняя удачная отправка`, one compact reports subsection-switch `Ежедневные отчёты` / `Отчёт по остаткам` inside `Отчёты`, separate compact auto block `Автообновления` and one fixed-height scrollable `Лог` block with `Скачать лог`
- daily-report block compares only the two latest closed business days in `Asia/Yekaterinburg`: `yesterday_closed(default_business_as_of_date(now))` vs `yesterday_closed(default_business_as_of_date(now)-1 day)`, never `today_current`
- daily-report ranked totals stay on the current canonical pool only (`total_view_count`, `total_views_current`, `avg_ctr_current`, `avg_addToCartConversion`, `avg_cartToOrderConversion`, `avg_spp`, `avg_ads_bid_search`, `total_ads_views`, `total_ads_sum`, `avg_localizationPercent`)
- daily-report SKU block truthfully shows only `display_name + nmId`; common factors use only deterministic sign-safe signals (`views/search views/search CTR/conversions`, `ads_sum`, `price_seller_discounted`, `Нет остатков`, district low-stock `< 20` except `stock_ru_far_siberia`)
- negative/positive factor sections render the full valid factor set, sorted by matched SKU count and then aggregate strength, not a hard top-5 cap
- factor rows now show label + restrained direction arrow + matched SKU count + type-aware aggregate summary (`медиана ±X.X%`, `медиана ±N ₽ (±X.X%)`, `медиана остатка N шт.` depending on factor type)
- daily-report JSON also carries `metric_ranking_diagnostics`, so the observed short decline list is now explicitly explained in the payload itself; the current repo-owned diagnostic smoke keeps `raw=10`, `present=9`, `negative=3`, `positive=6`, with `avg_ads_bid_search` excluded because both closed-day values are missing
- stock-report block now defaults to previous closed business day stocks from persisted ready snapshot `DATA_VITRINA[yesterday_closed]`, keeps threshold `<50`, accepts optional explicit `as_of_date` override on the same read path, uses five district keys (`stock_ru_central`, `stock_ru_northwest`, `stock_ru_volga`, `stock_ru_ural`, `stock_ru_south_caucasus`) and deliberately excludes merged bucket `stock_ru_far_siberia` / `ДВ и Сибирь`
- stock-report subsection now also exposes a compact SKU selector sourced from full active `config_v2` truth, not from breached rows only; selector defaults to all active SKU selected, uses `display_name + nmId` labels, applies the filter only after `Рассчитать`, rejects zero selected SKU with `Выберите хотя бы один SKU` and shows an honest empty result when the selected subset has no breaches
- operator page now also persists current top-level tab, active `Отчёты` / `Расчёт поставок` subsection and the stock-report SKU selector in namespaced browser `localStorage` only; reload restores the last valid UI state, while broken storage or obsolete `nmId` values truthfully fall back to the current default/current active SKU set without any server-side user profile state
- status/refresh responses drive the blocks through `server_context` + `manual_context`, so timezone/scheduler wording and manual-success timestamps are not hardcoded in UI; `Автоцепочка` now truthfully describes the full daily chain `Ежедневно в 11:00, 20:00 Asia/Yekaterinburg: загрузка данных + отправка данных в таблицу`
- the same block shows backend-driven `Последний автозапуск`, `Статус последнего автозапуска` and `Последнее успешное автообновление`
- `refresh` и `load` не смешиваются: refresh materialize-ит ready snapshot only, load пишет only already prepared snapshot в live sheet
- job/log surface is detailed and machine-useful: source/module/adapter/endpoint steps, source result kinds/counts, metric batch summaries and bridge/write results stay server-driven and can be exported per completed run through `GET /v1/sheet-vitrina-v1/job?job_id=...&format=text&download=1`
- server-side business timezone = `Asia/Yekaterinburg` for default `as_of_date`, `today_current` and operator-facing freshness dates
- live daily auto-refresh = repo-owned artifacts `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.{service,timer}` -> installed on host as `wb-core-sheet-vitrina-refresh.timer` -> existing `POST /v1/sheet-vitrina-v1/refresh` at `11:00, 20:00 Asia/Yekaterinburg` (`06:00 UTC` and `15:00 UTC` on current host) with `auto_load=true`, so the daily path now finishes as `refresh + load to live sheet`
- source matrix is now explicit: group A bot/web-source historical, group B WB API historical/date-period capable, group C WB API current-snapshot-only, group D other/manual/browser-collector overlays
- historical/date-period families (`seller_funnel_snapshot`, `web_source_snapshot`, `sales_funnel_history`, `sf_period`, `spp`, `stocks`, `ads_compact`, `fin_report_daily`) now use persisted accepted closed-day semantics for `yesterday_closed`
- current-snapshot-only families (`prices_snapshot`, `ads_bids`) capture upstream truth only as current snapshot, but an already accepted snapshot for business day D must materialize as `yesterday_closed=D` on D+1; later invalid auto/manual attempts must not blank prior-day accepted truth or already accepted same-day truth
- `stocks` is now fully in the date/period-capable group inside `sheet_vitrina_v1`: both `yesterday_closed` and `today_current` read authoritative exact-date Seller Analytics CSV snapshots from `temporal_source_snapshots[source_key=stocks]`
- manual operator refresh keeps short retries inside the run but does not create persisted long-retry tails and does not overwrite accepted truth on invalid candidates
- promo source follows the same accepted-truth norm:
  - invalid current attempt must not overwrite already accepted same-day promo truth
  - `yesterday_closed` must read only accepted/runtime-cached promo truth
  - low-confidence cross-year labels keep null exact dates instead of invented dates
- live retry completion is bounded by repo-owned runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py` plus repo-owned artifacts `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.{service,timer}` installed on host as `wb-core-sheet-vitrina-closure-retry.timer`; the runner covers due `yesterday_closed` for the full historical/date-period matrix and same-day current-only capture retries only within the current business day

Current additional operator supply flow on the same page:
- top-level tab `Расчёт поставок` keeps the existing page pattern and now materializes two bounded sibling blocks: `Заказ на фабрике` and `Поставка на Wildberries`
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
- regional block adds server-side district allocation with truthful `deficit = full_recommendation - allocated_qty`, a compact per-district summary table and one separate XLSX file per canonical district key
- current bounded regional methodology uses total SKU `orderCount` plus current district stocks and then applies legacy box allocation against shared `stock_ff`; inbound `ФФ -> WB` is intentionally not materialized for this block in the current checkpoint

Current main-confirmed counts для этого flow:
- prepare/upload package = `33 / 102 / 7`
- current truth / ready snapshot displayed metrics = `95`
- refresh materialize-ит date-aware ready snapshot `yesterday_closed + today_current`
- operator-facing `DATA_VITRINA` = server-driven two-day `date_matrix` `1698` rendered rows / `95` metric keys (`1631` source rows, `34` blocks)
- operator-facing `STATUS` = per-source/per-slot freshness surface; current-snapshot-only sources (`prices_snapshot`, `ads_bids`) now expose `accepted_current_rollover` semantics for `yesterday_closed` and preserve accepted truth across later invalid attempts, `stocks[yesterday_closed]` и `stocks[today_current]` resolve through historical exact-date runtime snapshots, а failed later attempts preserve the last accepted truth instead of blank/zero overwrite
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
- production hardening around runtime/deploy/auth;
- generic orchestration platform beyond current bounded auto + retry timers;
- actual deploy rights/publish wiring for hosted contour;
- unresolved long-tail compatibility around `AI_EXPORT`.

# Not in scope

- Полные module doc narratives.
- Artifact/evidence details по каждому модулю.
- Hidden operational deploy knowledge вместо repo-owned contract.
