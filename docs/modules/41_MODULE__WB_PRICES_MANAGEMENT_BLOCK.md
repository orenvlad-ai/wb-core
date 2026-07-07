---
title: "Модуль: wb_prices_management_block"
doc_id: "WB-CORE-MODULE-41-WB-PRICES-MANAGEMENT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический reference по operator-разделу `Цены` для чтения и guarded изменения цен/скидок через WB Prices and Discounts API."
scope: "MVP раздела `Цены` в unified `/sheet-vitrina-v1/vitrina`: current goods price/discount table, inline price/discount edits, backend preview with diff/quarantine risk, env-guarded explicit upload task commit, upload status/goods error readback and quarantine read-only surface. The module reuses canonical `WB_API_TOKEN`, keeps browser state transient and does not create a new business truth layer."
source_basis:
  - "packages/contracts/wb_prices_management.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/application/wb_prices_management.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "Official WB API docs: Prices and Discounts"
related_modules:
  - "packages/contracts/wb_prices_management.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/application/wb_prices_management.py"
  - "packages/contracts/prices_snapshot.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_tables:
  - "registry_upload_config_v2"
  - "sheet_vitrina_v1_nomenclature_items"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/prices/goods"
  - "POST /v1/sheet-vitrina-v1/prices/preview"
  - "POST /v1/sheet-vitrina-v1/prices/upload-task"
  - "GET /v1/sheet-vitrina-v1/prices/upload-task/{upload_id}"
  - "GET /v1/sheet-vitrina-v1/prices/upload-task/{upload_id}/goods"
  - "GET /v1/sheet-vitrina-v1/prices/quarantine"
related_runners:
  - "apps/wb_prices_management_smoke.py"
  - "apps/wb_prices_management_browser_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/modules/03_MODULE__PRICES_SNAPSHOT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Initial guarded WB Prices and Discounts management MVP: read table, preview diff, quarantine risk, upload-task commit guarded by `WB_PRICES_WRITE_ENABLED`, status/detail/quarantine readback and browser smoke over fake upstream only."
---

# 1. Идентификатор и статус

- `module_id`: `wb_prices_management_block`
- `family`: `sheet_vitrina_v1/operator/official-api/prices`
- `status_main`: active implementation target
- `status_write_path`: guarded backend-only; disabled unless `WB_PRICES_WRITE_ENABLED=true`

# 2. Product Semantics

Раздел `Цены` приближен по смыслу к WB Partners `Товары и цены -> Цена и скидка`, but it is not a pixel-perfect clone.

Top-level UI shows one row per active `nmID` where possible:
- photo/name/our SKU/vendorCode/nmID;
- seller price, seller discount and discounted price;
- WB Club discount and club discounted price as read-only fields;
- `editableSizePrice` badge;
- quarantine badge when read-only quarantine endpoint reports the nmID;
- last upload status and per-row WB error when available;
- inline draft price/discount controls.

Browser state is only transient editing/filter/modal state. Current price truth is read from WB via backend routes; preview and upload status are server-owned readback surfaces.

# 3. WB Prices API Boundary

The adapter `packages/adapters/wb_prices_management.py` uses the canonical official API runtime helper and the same `WB_API_TOKEN` boundary as existing official-API modules.

Default upstream base:
- `https://discounts-prices-api.wildberries.ru`

Optional local/test override:
- `WB_PRICES_API_BASE_URL`

Read endpoints:
- `GET /api/v2/list/goods/filter`
- `POST /api/v2/list/goods/filter`
- `GET /api/v2/history/tasks`
- `GET /api/v2/history/goods/task`
- `GET /api/v2/quarantine/goods`

Write endpoint:
- `POST /api/v2/upload/task`

Frontend must never call WB Prices and Discounts API directly.

# 4. Server View Model

The application normalizes WB payload into a server-owned view model:
- `nmID`
- `vendorCode`
- `sizes[]`
- min/current price
- discounted price
- club discounted price
- seller discount
- WB Club discount
- `currencyIsoCode4217`
- `editableSizePrice`
- `wholesaleDiscountThreshold`
- `isBadTurnover`

Nomenclature names/barcodes/our SKU are enrichment from current runtime reference data and are not WB price truth.

# 5. Safety Workflow

Preview route:
- accepts up to `1000` changes;
- rejects empty payload, duplicate `nmID` and rows where both `price` and `discount` are absent;
- pulls current prices from WB read endpoint;
- calculates old/new seller price, seller discount and discounted price;
- flags quarantine risk when new discounted price is at least 3x lower than old discounted price;
- blocks ordinary product price edits for rows with `editableSizePrice=true`;
- stores only a short-lived preview token under runtime state.

Commit route:
- requires `WB_PRICES_WRITE_ENABLED=true`;
- requires `confirm=true`, preview id and confirmation token from the preview response;
- rejects expired/tampered previews;
- uploads only valid rows through `POST /api/v2/upload/task`;
- returns `uploadID` and `alreadyExists` when WB returns them;
- treats upload response as task creation only, not final price application.

# 6. Status Readback

Status routes map WB upload task statuses to UI labels:
- `1` -> `processing`
- `3` -> `success`
- `4` -> `canceled`
- `5` -> `partial_error`
- `6` -> `all_error`

For partial/all error statuses the application reads `history/goods/task` and exposes per-row `errorText` so the table can show WB errors next to affected goods.

# 7. Audit And Persistence Boundary

The module does not create a new DB truth layer for prices.

The only runtime writes are:
- short-lived preview files needed to prove explicit confirmation;
- bounded JSONL upload audit events under `sheet_vitrina_v1_prices/upload_audit.jsonl`.

These files are operational evidence, not accepted business truth. Current price state must be read back from WB and upload status routes.

# 8. UI

The `Цены` tab is a sibling section in the unified operator shell. It renders:
- dense current prices table;
- search by nmID/vendorCode/name when available;
- filters for errors, size-price rows and quarantine rows;
- inline price/discount draft controls;
- batch preview modal with old/new price, discount, discounted price and warnings;
- disabled/guarded commit state when server write flag is off;
- uploadID/status polling after commit;
- row-level WB error overlay after status/detail readback.

# 9. Verification

Targeted local smokes:
- `python3 apps/wb_prices_management_smoke.py`
- `python3 apps/wb_prices_management_browser_smoke.py`

Regression smokes:
- `python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_browser_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`

All prices management smokes use fake upstreams and must not call live `POST /api/v2/upload/task`.

# 10. Out Of Scope

- Excel import/export of prices.
- WB Club discount writes.
- B2B wholesale discount writes.
- Size-level price editing through `/api/v2/upload/task/size`.
- Automatic mass price changes.
- Google Sheets/GAS integration.
- Price index and auto-promo minimum price semantics without a separate official API research pass.
