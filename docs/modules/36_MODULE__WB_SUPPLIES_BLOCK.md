---
title: "Модуль: wb_supplies_block"
doc_id: "WB-CORE-MODULE-36-WB-SUPPLIES-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для read-only блока `Поставки -> Wildberries`: WB API / FBW Supplies registry, runtime cache/history, protected API and operator UI."
scope: "Official WB FBW Supplies read-only contour under current WebCore runtime: adapter over `supplies-api.wildberries.ru`, SQLite cache/state/warehouse tables, protected list/sync/detail API routes, embedded operator UI filters/table/pagination and targeted smokes."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "Official Wildberries Developer Portal: Supplies API / FBW Supplies"
related_modules:
  - "packages/adapters/wb_supplies.py"
  - "packages/application/wb_supplies.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
related_tables:
  - "sheet_vitrina_v1_wb_supplies"
  - "sheet_vitrina_v1_wb_supplies_sync_state"
  - "sheet_vitrina_v1_wb_supplies_sync_runs"
  - "sheet_vitrina_v1_wb_supplies_warehouses"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}"
related_runners:
  - "apps/wb_supply_overlay_smoke.py"
  - "apps/wb_supplies_api_adapter_smoke.py"
  - "apps/wb_supplies_backfill_live.py"
  - "apps/wb_supplies_backfill_smoke.py"
  - "apps/wb_supplies_accepted_parity_diagnostics.py"
  - "apps/wb_supplies_first20_parity_smoke.py"
  - "apps/wb_supplies_goods_composition_diagnostics.py"
  - "apps/wb_supplies_goods_composition_smoke.py"
  - "apps/wb_supplies_incremental_sync_smoke.py"
  - "apps/wb_supplies_live_diagnostics.py"
  - "apps/wb_supplies_normalization_smoke.py"
  - "apps/wb_supplies_renormalize_cache.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Read-only WB/FBW supplies registry separates quick incremental/latest-window refresh from resumable full history backfill, preserves enriched raw evidence, exposes normalized goods composition, maps the planned/target WB warehouse name to the six repo-owned calculation districts through Marketplace offices primary evidence, tariffs/box fallback and bounded known-warehouse fallback, exposes district presets inside the `Склад` dropdown in `Все поставки`, and publishes a read-only calculation-overlay options route for `Поставки -> Расчёты`. Actual/transit warehouses stay route/display evidence and do not define the calculation district. Overlay selector options include only calculation-eligible statuses 2/3/4/6; statuses 1/5 stay out of the selector and are revalidated/skipped server-side if posted manually. It adds no WB mutations, no FBS process, no Google Sheets/GAS writes and no ЕБД metric truth writes."
---

# 1. Contract

- Operator surface: `Поставки` has sibling inner sections `Расчёты`, `Wildberries`, `От поставщика`.
- `Расчёты` keeps the existing factory-order and WB regional calculators.
- `От поставщика` remains the supplier invoice registry and is not redefined by this module.
- `Wildberries` renders one screen:
  - inner section label: `Wildberries`;
  - title: `Все поставки`;
  - source note: `WB API / FBW Supplies · read-only`;
  - lead: `Read-only список поставок WB API / FBW Supplies`.
- The UI is read-only. It does not create, update, delete or draft WB supplies.
- The module does not implement WB Seller Portal browser automation and does not use FBS APIs.

# 2. Official API Boundary

- Default upstream base URL: `https://supplies-api.wildberries.ru`.
- Required token env: `WB_API_TOKEN`.
- Optional base override: `WB_SUPPLIES_API_BASE_URL`.
- Timeout follows shared official API helper conventions.
- Implemented read methods:
  - `POST /api/v1/supplies`;
  - `GET /api/v1/supplies/{ID}`;
  - `GET /api/v1/supplies/{ID}/goods`;
  - `GET /api/v1/supplies/{ID}/package` exists in adapter as optional evidence and is not fatal for MVP table;
  - `GET /api/v1/transit-tariffs` exists in adapter/diagnostics as read-only tariff evidence; the UI does not calculate transit cabinet cost from it without a proven formula;
  - `GET /api/v1/warehouses`.
- Additional read-only district mapping evidence:
  - district source is the planned/target supply warehouse (`warehouseName`, exposed as `planned_warehouse_name` / `target_warehouse_name` / `district_source_warehouse_name`);
  - `actualWarehouseName` and `transitWarehouseName` remain route/display/evidence only and must not decide the calculation district;
  - Marketplace `GET /api/v3/offices` (`WB_MARKETPLACE_API_BASE_URL` override) is the primary source; match is by normalized planned/target warehouse/offices name and raw `federalDistrict`;
  - tariffs `GET /api/v1/tariffs/box` (`WB_TARIFFS_API_BASE_URL` override) is fallback; match is by normalized planned/target `warehouseName` and raw `geoName`;
  - bounded manual known-warehouse fallback covers live/cache warehouses missing from external references and publishes `source/confidence/evidence` as `manual_known_wb_warehouse`;
  - Supplies `warehouse_id` is not treated as Marketplace office id.
- `POST /api/v1/acceptance/options`, transit create/update methods and all WB mutations stay outside scope.
- Adapter errors are sanitized:
  - missing `WB_API_TOKEN` returns controlled app-level error;
  - upstream `401/403` maps to `WB API token has no Supplies permission or is invalid`;
  - non-JSON and transport failures map to controlled transport errors;
  - token values are not printed.

# 3. Runtime Persistence

Runtime truth is server-owned SQLite under `RegistryUploadDbBackedRuntime`.

Tables:
- `sheet_vitrina_v1_wb_supplies`: primary cached rows keyed by legacy-compatible normalized `supply_id`, plus explicit stable `cache_key` (`supply:<supplyID>` / `preorder:<preorderID>`), normalized row JSON, sanitized raw list/detail/goods/package evidence, `wb_supply_id`, `preorder_id`, `warehouse_id`, `status_id`, `quantity_for_size_filter`, source dates, raw evidence hashes, `last_list_synced_at`, `last_enriched_at`, `enrichment_status` and `enrichment_error`.
- `sheet_vitrina_v1_wb_supplies_sync_state`: last sync fields plus `backfill_complete`, `backfill_started_at`, `backfill_completed_at`, `highest_synced_offset`, `last_successful_offset`, `last_mode`, latest-window counters, `may_have_more` and sanitized `last_error`.
- `sheet_vitrina_v1_wb_supplies_sync_runs`: per-run progress for `incremental_refresh`, `full_backfill` and explicit missing-critical enrichment requests: status/phase, offset/limit, pages/raw/upserted/new/changed/unchanged/enriched/failed counters, `may_have_more`, last error and compact sanitized logs.
- `sheet_vitrina_v1_wb_supplies_warehouses`: cached warehouse dictionary/options.

The cache is an operator registry/cache only:
- it is not accepted ЕБД metric truth;
- it is not written into `web-vitrina` ready snapshots;
- old rows are not deleted just because they are absent from the latest fetch;
- sync upserts rows and preserves cached data after failed upstream attempts.
- list-only sync/backfill must not downgrade enriched rows: if new list evidence arrives but cached `raw_detail`, `raw_goods` or `raw_package` already exists, normalization is rebuilt from the new list plus existing enriched evidence.
- lazy detail/goods enrichment uses row-only persistence and does not rewrite global sync-state.

# 4. API Routes

`GET /v1/sheet-vitrina-v1/supply/wb-supplies`

Returns cached rows only. It does not fetch upstream.

Query params:
- `search`;
- `warehouse_id` or `warehouse`;
- `district_keys` / `district_key` as comma-separated or repeated six-key calculation district filter;
- `status_ids` as comma-separated list or repeated query params;
- `status_id` as backward-compatible single status filter;
- `size_filter = main_250 | all | small_lt_250`;
- `limit = 20 | 50 | 100`;
- `offset`;
- `sort_key = supply_date`;
- `sort_dir = asc | desc`.

Response shape:
- `contract_name = sheet_vitrina_v1_wb_supplies`;
- `contract_version`;
- `meta`;
- `filters`;
- `summary`;
- `pagination`;
- `schema.columns`;
- `rows`.
- `sync_state`;
- `active_run` when a backfill/latest run is still queued/running.

`POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync`

Performs ordinary incremental/latest-window refresh. It must not full-scan history.

Body:
- `mode`, default `incremental_refresh`;
- `limit`, default `1000`, max `1000`;
- `enrich`, default `changed_only`; `missing_critical` explicitly retries unchanged rows with missing critical fields; `none` skips enrichment.
- optional `status_ids` / legacy `status_id` when an explicit status-limited sync is requested;
- optional `list_params` so the sync response can return the caller's current filtered/sorted list without resetting UI filters.

Algorithm:
- fetch latest page/window from official WB API at `offset=0`;
- if the caller did not request an explicit status-limited sync, also fetch a bounded active-status page with `statusIDs=[1,2,3,4]` so active supplies are discoverable and authoritative for current active state;
- merge/dedupe default and targeted raw rows by stable supply/preorder key before upsert;
- calculate stable `raw_list_hash`;
- upsert and enrich only new rows and rows whose `updatedDate`/raw hash changed;
- refresh supply-backed active rows (`2/3/4`) through detail/goods on ordinary refresh so date/status/route/quantity/goods changes are reflected even when list evidence is otherwise unchanged;
- when the active-status slice completes without upstream error and is not capped by `limit`, hard-delete local rows still in statuses `1..4` that are absent from both default latest and the active-status slice;
- never hard-delete accepted/historical statuses `5/6` just because they are absent from a latest refresh window;
- old unchanged historical rows with missing critical fields are not retried by ordinary refresh; request `enrich=missing_critical` to run that bounded enrichment lane explicitly;
- unchanged historical rows are counted as `unchanged` and do not call detail/goods again.

`POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill`

Starts background full history backfill and returns `202` with `run_id`.

Body:
- `limit`, default/max `1000`;
- `start_offset`, default `0`;
- `resume`, default `true`;
- `enrich`, default `true`;
- optional `max_pages` for diagnostic bounded runs.

Full backfill walks `POST /api/v1/supplies?limit=<limit>&offset=<offset>` until a short/empty upstream page proves the end of available API history. It saves list rows and offset progress before the optional detail/goods enrichment pass for that page, uses idempotent upsert, never deletes old rows just because a page omits them, and can resume from `highest_synced_offset` after 429/timeout/non-JSON/upstream failures.

`GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status?run_id=...`

Returns the requested run or active run plus sync state and cached row count.

`GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}`

Returns cached normalized row plus raw list/detail/goods/package evidence for diagnostics and normalized goods composition for future/current supply detail UI.

Response additions:
- `goods`: normalized composition rows with `nm_id`, `barcode`, `vendor_code`, `supplier_article`, `tech_size`, `color`, `quantity`, `accepted_quantity`, `unloading_quantity`, `ready_for_sale_quantity`, `depersonalized_quantity`, `package_code`, `raw_index`, `evidence_source`;
- `goods_summary`: total added/accepted/unloading/ready/depersonalized quantities, row count, unique `nm_id` count and unique barcode count;
- `package.summary`: package count, package quantity total and barcode quantity total when package evidence is available;
- `composition_status = available | partial | missing | error`;
- `composition_last_enriched_at`;
- `composition_error`;
- `raw_diagnostics` with sanitized raw key lists and hashes.

If cached goods/detail evidence is absent, the detail route performs bounded lazy fetch for that one supply only, stores the result via row-only upsert, and returns cached data plus a controlled warning when upstream returns 429/non-JSON/transport errors. Missing `WB_API_TOKEN` during lazy fetch is a warning when cached data can still be rendered.

`GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options`

Returns cached WB supplies as read-only selector options for `Поставки -> Расчёты -> Учесть WB-поставки`. It does not fetch upstream, mutate WB, write Google Sheets, or write ready/web-vitrina metrics.

Contract:
- `eligible_status_ids = [2, 3, 4, 6]`;
- statuses `1` (`Не запланировано`) and `5` (`Принято`) are excluded from selector options and are not rendered even as disabled rows;
- calculate routes still revalidate posted `selected_wb_supply_ids`, so manually posted status `1`/`5` supplies are skipped with diagnostics and never counted;
- a future unknown-id status may be eligible only when it clearly means shipped and not accepted;
- option is disabled when no operational supply date exists, goods composition is absent, or usable active SKU quantity is zero;
- quantity source is only goods composition `nmId -> quantity`; accepted/ready/partial reception fields are not used for overlay quantity;
- unknown active SKU, missing `nmId`, missing/non-positive quantity and non-active `nmId` goods rows are skipped with diagnostics;
- response exposes status/date evidence, date source field, warehouse/district mapping evidence, usable SKU count/quantity, skipped goods and disabled reasons.

# 4.1 Warehouse District Mapping

The calculation districts are exactly the six keys from `wb_regional_supply`, not the ordinary eight Russian federal districts:
- `central`;
- `northwest`;
- `volga`;
- `ural`;
- `south_caucasus`;
- `far_siberia`.

Raw district names collapse into these six keys: Central -> `central`, Northwestern -> `northwest`, Volga -> `volga`, Ural -> `ural`, Southern + North Caucasus -> `south_caucasus`, Siberian + Far Eastern -> `far_siberia`.

Unmatched planned/target warehouse names remain `unmapped` and emit warnings. They remain visible in the WB supplies list, are not selected by district presets, and are not added to regional overlay quantities. The global `/api/v1/warehouses` catalog is used as name evidence/options but is not itself a warning target; warning counts are based on warehouses that occur in supply rows/options being mapped.

Operator UI must not render the full unmapped warehouse warning list inline in the calculation block. The default view shows a compact count/summary; full warehouse warning details are available only under a collapsed details/spoiler control.

# 5. Field Normalization

Normalization keeps separate evidence sources instead of flattening them with lossy overwrite:
- detail fields may be primary for status/date fields;
- warehouse, route, quantity and cost fields use first non-empty evidence from detail/list/goods/package/warehouse dictionary as appropriate;
- `planned_warehouse_name` / `target_warehouse_name` / `district_source_warehouse_name` identify the warehouse used for district mapping;
- `actual_warehouse_name` / `transit_warehouse_name` / `warehouse_to_name` identify fact/transit route evidence and can appear in `warehouse_display`;
- `None` and empty strings from detail do not erase non-empty list or dictionary evidence;
- normalized rows expose evidence markers: `warehouse_evidence`, `route_evidence`, `quantity_evidence`, `packed_quantity_evidence`, `cost_evidence`.

Warehouse/route fields:
- `warehouse_from_name`;
- `warehouse_to_name`;
- `warehouse_actual_name`;
- `warehouse_display`;
- `warehouse_fact_line`;
- `route_evidence`.

For transit supplies, official detail evidence observed in live diagnostics maps user-visible route as:
- from = `warehouseName`;
- to = `transitWarehouseName` or, if missing, `actualWarehouseName`.

Therefore `warehouse_display` for a transit supply is `warehouseName → transitWarehouseName`. The UI does not show `Факт: ...` for ordinary transit rows where `actualWarehouseName` is the same destination as `transitWarehouseName`, because that duplicates and can invert the cabinet route.

Quantity fields:
- `quantity_added` priority: detail `quantity`, then `sum(goods.quantity)`, then `sum(package.quantity)`;
- `packed_quantity` priority: explicit packed/package field if present, then `sum(goods.quantity)`, then `sum(goods.supplierBoxAmount)`, then `sum(package.quantity)`, then accepted-supply fallback to `quantity_added`;
- `accepted_quantity` priority: detail `acceptedQuantity`, then `sum(goods.acceptedQuantity)`;
- `quantity_for_size_filter` follows `quantity_added` before accepted/unloading fallbacks.

Cost fields:
- `acceptance_cost` preserves raw `acceptanceCost`;
- `transit_cost` preserves explicit transit cost fields if the upstream ever returns them;
- `cost_total` is the user-visible amount only when raw evidence provides a total, explicit transit cost, or a non-transit `acceptanceCost`;
- for transit rows with `acceptanceCost = 0` and no explicit total/transit cost, `cost_total = null`, `cost_display = —`, and `has_transit_cost_marker = true`;
- the UI must not render `0 ₽` for unknown transit cost.
- for non-transit accepted rows where official detail has `paidAcceptanceCoefficient = 0` and no explicit `acceptanceCost`, `cost_total = 0` with evidence `paidAcceptanceCoefficient.free_accepted_non_transit`; the UI renders `0 ₽` and coefficient `Бесплатно`.

Type labels:
- known `boxTypeID=1` renders as `Короб`;
- `boxTypeID=0` is never rendered as technical `Тип 0`; live evidence for accepted доприёмка rows has `virtualTypeID=5`, which maps to `Допринято`;
- technical `boxTypeID 1` is not user-facing when a mapping exists;
- unknown box types render as bounded `Тип <id>` and keep raw diagnostics in detail payload.

# 6. Quantity And Size Filter

The default size filter is `Основные от 250 шт` (`main_250`).

Server-normalized field:
- `quantity_for_size_filter`.

Evidence priority:
1. planned/added quantity from supply details when available;
2. goods total quantity when details quantity is missing but goods are loaded;
3. accepted/unloading quantity only as fallback evidence;
4. unknown remains `null` and is not invented.

Filter semantics:
- `main_250`: rows with numeric `quantity_for_size_filter >= 250`;
- `small_lt_250`: rows with numeric `quantity_for_size_filter < 250`;
- `all`: all cached rows, including unknown quantity.
- status does not override the size filter; planned rows with quantity `1` are visible in `all` and `small_lt_250`, planned rows with quantity `300` are visible in `all` and `main_250`, and unknown quantity is visible only in `all`.

Summary exposes:
- `hidden_by_size_filter_count`;
- `unknown_quantity_count`;
- threshold `250`.
- cache completeness label; if the last upstream page was full, the UI reports that history may still be incomplete.
- explicit states:
  - `История: latest window only`;
  - `История: частично загружена до offset N`;
  - `История: полная загрузка завершена`.

# 7. UI

The table is compact and keeps the current dark/violet operator identity.

Columns:
1. `Номер и тип`;
2. `Дата поставки`;
3. `Склад`;
4. `Статус`;
5. `Добавлено, шт / Упаковано → Принято`;
6. `Коэф. приёмки`;
7. `Стоимость`.

Filters:
- search placeholder `Номер поставки`;
- warehouse dropdown summary `Склады: все` / `ФО: ...` / `Склад: ...`;
- federal district presets `Все · ЦФО · СЗФО · ПФО · УрФО · Юг+СК · Сиб+ДВ` live inside the `Склад` dropdown; one or many district checkboxes filter by mapped warehouse district while unmapped warehouses remain available only through the concrete warehouse list;
- choosing a district preset clears a concrete warehouse filter, choosing a concrete warehouse clears district presets, and `Все` clears both to avoid stale false-empty combinations;
- status checkbox popup with summary `Статусы: все` or `Статусы: N`;
- status quick actions `Все`, `Активные` and `Сброс`; `Активные` selects all official statuses except `Не запланировано`;
- size select label `Размер поставки`;
- page size select `20 / 50 / 100`.

Known status labels:
- `1` = `Не запланировано`;
- `2` = `Запланировано`;
- `3` = `Отгрузка разрешена`;
- `4` = `Идёт приёмка`;
- `5` = `Принято`;
- `6` = `Отгружено на воротах`;
- unknown status = `Статус <id>`.

The status selector always exposes the official status set `1..6`, even before rows with every status are present in cache. `Виртуальная` is not shown unless upstream evidence adds a specific marker.

Filter state is browser-owned and persisted for search, warehouse, selected district presets, selected statuses, size filter, page size and date sort. `Обновить поставки` must preserve those filters, reapply them after the new payload arrives, and ignore stale in-flight responses if the operator changes filters while a request is running.

First open behavior:
- GET reads cache;
- if cache is empty, the authenticated UI starts bounded incremental latest-window `POST .../sync` with `limit=1000`;
- if token/API is unavailable, the UI shows a controlled error instead of a silent empty table.

Buttons:
- `Обновить поставки` = incremental latest-window refresh only; it does not scan all offsets and does not re-enrich unchanged rows or old unknown-quantity rows.
- `Загрузить всю историю` = one-time full backfill job; UI polls `sync-status` and shows offset/pages/fetched/upserted/enriched counters and last error.

Sorting:
- `Дата поставки` is clickable and toggles `asc/desc`.
- Sort is server-side over all filtered rows before pagination.
- Date sort key priority is `supply_date`, then `fact_date`, then `updated_date`, then `source_created_at`, then stable id.
- Rows without `supply_date` and `fact_date` stay at the bottom for both `asc` and `desc`; no-date `Не запланировано` rows are last among no-date rows.
- Date display is year-aware: current business-year dates render as `15 мая`; non-current-year single dates render as `15 мая 2025`; ranges include the year on every side that is not in the current year or when range years differ.

Error diagnostics:
- WB adapter checks status/content-type/body before JSON parsing.
- Upstream HTML/empty/non-JSON responses become controlled `WbSuppliesTransportError`/`WbSuppliesHttpStatusError` with sanitized status, content-type and body prefix.
- WebCore routes return JSON errors for controlled failures.
- UI non-JSON fallback displays route/status/content-type/body prefix; login HTML is shown as a session-expired/auth hint.

Operational full backfill command:

```bash
WB_API_TOKEN=... REGISTRY_UPLOAD_RUNTIME_DIR=/opt/wb-core-runtime/state \
  python3 apps/wb_supplies_backfill_live.py --limit 1000
```

The runner prints compact progress/result JSON without secrets and exits non-zero if full history cannot be marked complete.

Supply composition UI:
- clicking a WB supply row opens a read-only composition panel;
- the panel calls `GET .../wb-supplies/{supply_id}`;
- it shows supply header, composition status, totals and a goods table with `nmID`, barcode, vendorCode, size/color, added/accepted/unloading/ready quantities;
- no mutations or Seller Portal actions are available from this panel.

# 8. Diagnostics And Smokes

Live diagnostics:
- `python3 apps/wb_supplies_live_diagnostics.py` uses `WB_API_TOKEN`, scans configured target supply IDs through `POST /api/v1/supplies`, fetches detail/goods/package where available, samples `transit-tariffs`, and prints sanitized keys, field evidence and normalized deltas without token, headers, cookies or raw phone values.
- Target diagnostic IDs are the screenshot-backed supplies: `39265492`, `39265540`, `39265590`, `39265519`, `39265571`, `39238882`, `38535188`, `38350231`, `38978468`, `38978549`, `38978323`.
- `python3 apps/wb_supplies_accepted_parity_diagnostics.py --output-json` reports first-20 accepted row parity against expected screenshot/cabinet fields, cached normalized rows, raw evidence availability and optional official API detail/goods evidence with `--live`.
- `python3 apps/wb_supplies_renormalize_cache.py` safely rebuilds normalized rows from existing raw evidence without upstream calls by default. `--enrich-missing-critical` and `--enrich-missing-goods` use official API detail/goods/package for selected rows or missing-critical rows, preserve cached evidence on partial failures and print sanitized progress.
- `python3 apps/wb_supplies_goods_composition_diagnostics.py --output-json` reports composition status/totals/top goods for target supplies; `--live-fetch` allows the detail route to perform one-supply lazy enrichment.

Transit cost limitation as of this module revision:
- official detail/goods/package evidence for target transit rows exposes route, quantities, `acceptanceCost=0`, `paidAcceptanceCoefficient=0`, `storageCoef` and `deliveryCoef`, but no ready cabinet transit total;
- `/api/v1/transit-tariffs` exposes route tariffs (`boxTariff`, `palletTariff`, `activeFrom`), but tested formulas such as `boxTariff * quantity`, `boxTariff * acceptedQuantity`, pallet multiples and VAT/no-VAT variants did not stably match cabinet amounts for `39265519`, `39265492`, `39265590`, `39265571`;
- official Reports / Acceptance Expenses was checked with Analytics/Reports permission through `GET /api/v1/acceptance_report`, task status polling and task download for bounded windows around the target rows; the task completed, but download returned zero rows and did not expose the target values `15523.72`, `11543.52`, `14062.54`, `10726.11` or a usable `incomeId`/supply join key;
- production must keep transit amount as `—` with `с транзитом` until a ready source or proven formula is available;
- read-only Seller Portal network diagnostics are allowed, but current live storage state may be expired; no cookies/tokens/storage-state content may be logged.

Targeted smokes:
- `python3 apps/wb_supply_overlay_smoke.py`;
- `python3 apps/wb_supplies_api_adapter_smoke.py`;
- `python3 apps/wb_supplies_normalization_smoke.py`;
- `python3 apps/wb_supplies_first20_parity_smoke.py`;
- `python3 apps/wb_supplies_goods_composition_smoke.py`;
- `python3 apps/wb_supplies_backfill_smoke.py`;
- `python3 apps/wb_supplies_incremental_sync_smoke.py`;
- `python3 apps/wb_supplies_filter_sort_date_smoke.py`;
- `python3 apps/wb_supplies_acceptance_expenses_report_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_supplies_http_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py`.

Regression/protection smokes include:
- `python3 apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py`;
- `python3 apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py`;
- `python3 apps/sheet_vitrina_v1_operator_ui_persistence_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_smoke.py`;
- `git diff --check`.

# 9. Explicit Non-Scope

This module does not implement:
- WB supply creation/edit/delete;
- drafts;
- supply plan;
- warehouse restrictions screen;
- transit directions screen;
- unproven reverse-engineering of WB cabinet transit cost formula;
- FBS orders/supplies;
- Seller Portal browser automation;
- Google Sheets/GAS writes;
- accepted metric truth in web-vitrina ready snapshots;
- AI logic.
