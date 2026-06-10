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
  - "sheet_vitrina_v1_wb_supplies_warehouses"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}"
related_runners:
  - "apps/wb_supplies_api_adapter_smoke.py"
  - "apps/wb_supplies_live_diagnostics.py"
  - "apps/wb_supplies_normalization_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Read-only WB/FBW supplies registry now has field-level route/warehouse/quantity/cost evidence normalization, 1000-row latest-window sync and sanitized live diagnostics. It adds no WB mutations, no FBS process, no Google Sheets/GAS writes and no ЕБД metric truth writes."
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
- `POST /api/v1/acceptance/options`, transit create/update methods and all WB mutations stay outside scope.
- Adapter errors are sanitized:
  - missing `WB_API_TOKEN` returns controlled app-level error;
  - upstream `401/403` maps to `WB API token has no Supplies permission or is invalid`;
  - non-JSON and transport failures map to controlled transport errors;
  - token values are not printed.

# 3. Runtime Persistence

Runtime truth is server-owned SQLite under `RegistryUploadDbBackedRuntime`.

Tables:
- `sheet_vitrina_v1_wb_supplies`: primary cached rows keyed by normalized `supply_id`, normalized row JSON, raw list/detail/goods/package evidence, `warehouse_id`, `status_id`, `quantity_for_size_filter`, source dates and `synced_at`.
- `sheet_vitrina_v1_wb_supplies_sync_state`: `last_synced_at`, `last_successful_sync_at`, `last_error`, `last_limit`, `last_offset`, `latest_synced_count`.
- `sheet_vitrina_v1_wb_supplies_warehouses`: cached warehouse dictionary/options.

The cache is an operator registry/cache only:
- it is not accepted ЕБД metric truth;
- it is not written into `web-vitrina` ready snapshots;
- old rows are not deleted just because they are absent from the latest fetch;
- sync upserts rows and preserves cached data after failed upstream attempts.

# 4. API Routes

`GET /v1/sheet-vitrina-v1/supply/wb-supplies`

Returns cached rows only. It does not fetch upstream.

Query params:
- `search`;
- `warehouse_id` or `warehouse`;
- `status_id`;
- `size_filter = main_250 | all | small_lt_250`;
- `limit = 20 | 50 | 100`;
- `offset`.

Response shape:
- `contract_name = sheet_vitrina_v1_wb_supplies`;
- `contract_version`;
- `meta`;
- `filters`;
- `summary`;
- `pagination`;
- `schema.columns`;
- `rows`.

`POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync`

Performs a bounded upstream fetch and DB upsert.

Body:
- `limit`, default `1000`, max `1000`;
- `offset`, default `0`;
- `enrich_details`, default `true`.

`GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}`

Returns cached normalized row plus raw list/detail/goods/package evidence for diagnostics.

# 5. Field Normalization

Normalization keeps separate evidence sources instead of flattening them with lossy overwrite:
- detail fields may be primary for status/date fields;
- warehouse, route, quantity and cost fields use first non-empty evidence from detail/list/goods/package/warehouse dictionary as appropriate;
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

Type labels:
- known `boxTypeID=1` renders as `Короб`;
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

Summary exposes:
- `hidden_by_size_filter_count`;
- `unknown_quantity_count`;
- threshold `250`.
- cache completeness label; if the last upstream page was full, the UI reports that history may still be incomplete.

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
- warehouse select placeholder `Все склады`;
- status select placeholder `Все статусы`;
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

First open behavior:
- GET reads cache;
- if cache is empty, the authenticated UI starts bounded `POST .../sync` with `limit=1000`;
- if token/API is unavailable, the UI shows a controlled error instead of a silent empty table.

# 8. Diagnostics And Smokes

Live diagnostics:
- `python3 apps/wb_supplies_live_diagnostics.py` uses `WB_API_TOKEN`, scans configured target supply IDs through `POST /api/v1/supplies`, fetches detail/goods/package where available, samples `transit-tariffs`, and prints sanitized keys, field evidence and normalized deltas without token, headers, cookies or raw phone values.
- Target diagnostic IDs are the screenshot-backed supplies: `39265492`, `39265540`, `39265590`, `39265519`, `39265571`, `39238882`, `38535188`, `38350231`, `38978468`, `38978549`, `38978323`.

Targeted smokes:
- `python3 apps/wb_supplies_api_adapter_smoke.py`;
- `python3 apps/wb_supplies_normalization_smoke.py`;
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
