---
title: "Модуль: wb_prices_management_block"
doc_id: "WB-CORE-MODULE-41-WB-PRICES-MANAGEMENT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический reference по operator-разделу `Цены` для чтения, guarded изменения цен/скидок и bounded `Проверка СПП` через WB Prices and Discounts API."
scope: "MVP раздела `Цены` в unified `/sheet-vitrina-v1/vitrina`: подтабы `Текущие цены` и `Проверка СПП`; compact current goods price/discount table, browser-local column visibility, read-only `SPP-прокси`/promo summary enrichment from existing server-owned read-side sources, inline price/discount edits, backend preview with diff/quarantine risk, env-guarded explicit upload task commit, upload status/goods error readback and quarantine read-only diagnostics; bounded server-owned SPP tester for one nmID with safe-slow plan/start/status/restore, stale lifecycle reconciliation, existing-job history, one persistent daily `Автопроверка` schedule and a shared manual/scheduled runtime lock. The module reuses canonical `WB_API_TOKEN`, keeps browser state transient and does not create a new business truth layer."
source_basis:
  - "packages/contracts/wb_prices_management.py"
  - "packages/contracts/wb_spp_tester.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/application/wb_prices_management.py"
  - "packages/application/wb_spp_tester.py"
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
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/baseline?nmID=..."
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/plan"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/start"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/status"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/restore"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/history"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/history/{job_id}"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/schedule"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/schedule"
related_runners:
  - "apps/wb_prices_management_smoke.py"
  - "apps/wb_prices_management_browser_smoke.py"
  - "apps/wb_spp_tester_smoke.py"
  - "apps/wb_spp_tester_browser_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/modules/03_MODULE__PRICES_SNAPSHOT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Prices table semantics are unchanged. `Цены -> Проверка СПП` now automatically checks/recovers the dedicated buyer session, resumes an existing recovery after reload, auto-clicks one saved WB account, opens noVNC only for real human steps, and applies the same no-write preflight to manual and scheduled starts."
---

# 1. Идентификатор и статус

- `module_id`: `wb_prices_management_block`
- `family`: `sheet_vitrina_v1/operator/official-api/prices`
- `status_main`: active implementation target
- `status_write_path`: guarded backend-only; ordinary price commit disabled unless `WB_PRICES_WRITE_ENABLED=true`; SPP tester live run/restore disabled unless both `WB_SPP_TEST_ENABLED=true` and `WB_PRICES_WRITE_ENABLED=true`

# 2. Product Semantics

Раздел `Цены` приближен по смыслу к WB Partners `Товары и цены -> Цена и скидка`, but it is not a pixel-perfect clone.

Раздел имеет два подтаба:
- `Текущие цены` — текущая таблица цен/скидок и ручной guarded upload-task workflow.
- `Проверка СПП` — bounded live tool for one SKU/nmID that first proves or automatically recovers the dedicated buyer session, temporarily changes seller discounted price across an operator-specified range, measures authenticated buyer price plus anonymous control, detects suspicious adjacent thresholds and restores baseline through staged proof.

Top-level UI shows one row per active `nmID` where possible:
- photo/name/our SKU/vendorCode/nmID;
- seller price, seller discount and discounted price;
- WB Club discount and club discounted price as read-only fields;
- `editableSizePrice` badge;
- read-only `СПП` from current server-owned `spp_proxy`/`SPP-прокси` data, rendered as `н/д` when unavailable rather than fake zero;
- read-only `Акции` as `eligible / total current promos` counts from existing promo current semantics (`promo_count_by_price` plus global `promo_by_price.current_promos`/diagnostic current counter), rendered as `н/д` when the source/denominator is absent;
- quarantine badge when read-only quarantine endpoint reports the nmID;
- last upload status and per-row WB error when available;
- inline draft price/discount controls.

Browser state is only transient editing/modal state plus presentation-only column visibility in localStorage. Current price truth is read from WB via backend routes; SPP/promo values are read from current server-owned runtime/read-side sources; preview and upload status are server-owned readback surfaces.

For `Проверка СПП`, browser state is only form draft and presentation. Baseline, plan, current/last job, measurements, thresholds, expandable history, schedule, audit and restore proof are server-owned runtime state under `sheet_vitrina_v1_prices/spp_tests/`. Existing `jobs/*.json` remain the history source; no second DB/journal is introduced.

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
- `sppProxy`
- `sppProxyLabel`
- `sppProxyReason`
- `promoEligibleCount`
- `promoCandidateCount`
- `promoCurrentCount`
- `promoLabel`
- `promoReason`

Nomenclature names/barcodes/our SKU are enrichment from current runtime reference data and are not WB price truth.

`sppProxy*` is derived from latest available `DATA_VITRINA` `spp_proxy` row for the `nmID`; missing current data stays `null`/`н/д` with a reason. `promoEligibleCount` is derived from latest available `promo_count_by_price`. `promoCurrentCount` is the visible denominator and is read from current server-owned `promo_by_price` global counters: first `current_promos`, then compatible diagnostic counters such as `current_promo_count` / `covering_campaigns`, with `materializable_campaigns` / `usable_campaigns` only as bounded compatibility fallback when the primary current counters are absent. `promoCandidateCount` remains optional per-SKU debug/tooltip context from `items[].promo_candidate_count` and is not the visible denominator. Existing promo metrics (`promo_participation`, `promo_count_by_price`, `promo_entry_price_best`) keep their original meaning.

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

SPP tester route:
- accepts only one `nmID` per job;
- rejects `editableSizePrice=true` and existing quarantine at baseline;
- requires `WB_SPP_TEST_ENABLED=true`, `WB_PRICES_WRITE_ENABLED=true`, explicit live-change confirmation and `restore_baseline=true`;
- changes only integer seller `price` while preserving current discount during measurements;
- uses WB readback `discountedPrice` as actual seller price truth;
- polls public anonymous buyer price slowly and excludes low-confidence/stale/429 points from threshold detection;
- writes upload/readback/public/quarantine events to JSONL audit;
- allows only one active/unrestored SPP test job at a time through runtime `current_job.json` pointer/heartbeat and removes that pointer after fresh exact seller baseline proof;
- shares one OS-level execution lock across manual jobs, scheduled jobs and emergency restore;
- reconciles an orphan only through fresh exact WB seller tuple + quarantine evidence; authenticated/anonymous buyer evidence is diagnostic only and TTL expiry alone never unlocks it;
- persists `trigger_source=manual|schedule` for new jobs while legacy source stays unknown;
- uses the same baseline/write/readback/restore path for daily scheduled jobs and never starts one merely because the schedule was saved.

Restore:
- is always required at the end of an MVP run;
- uses direct restore only for small moves;
- splits large downward discounted restore moves through bridge steps;
- requires upload success, WB readback and quarantine absence for bridge/final steps;
- final proof requires only WB price/discount/discountedPrice equal baseline and quarantine absent; authenticated/public buyer price and SPP capture is non-blocking diagnostic evidence;
- failed restore or quarantine yields `manual_restore_required` and keeps emergency restore visible.

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
- inner tabs `Текущие цены` / `Проверка СПП`;
- dense current prices table;
- search by nmID/vendorCode/name when available;
- compact `Колонки` menu with browser-local visibility for optional columns;
- no separate toolbar filters for errors, size-price rows or quarantine rows;
- row-level read-only `СПП`, `Акции`, upload status, WB error and quarantine diagnostics;
- inline price/discount draft controls;
- batch preview modal with old/new price, discount, discounted price and warnings;
- disabled/guarded commit state when server write flag is off;
- uploadID/status polling after commit;
- row-level WB error overlay after status/detail readback.

`Проверка СПП` renders a minimal operator surface:
- automatic buyer-session check/recovery lifecycle with reload-safe `run_id` attachment and human-only noVNC escalation;
- `Автопроверка` above manual inputs with one daily schedule, explicit future-live-change consent, `Asia/Yekaterinburg — Оренбург`, next run and last automatic status;
- SKU/nmID selector sourced from current price rows / active registry;
- baseline card with seller price, discount, discounted price, public buyer price, current `SPP-прокси`, quarantine and `editableSizePrice`;
- inputs for discounted price min/max, threshold precision, max measurements, safe-slow mode, live-change confirmation and restore confirmation;
- plan preview with route, estimated duration, request budget, restore route and live warning;
- current job status, compact timeline, measurements table and threshold table.
- `История проверок` below the current/last job with bounded cursor loading and lazy safe detail expansion.

# 9. Verification

Targeted local smokes:
- `python3 apps/wb_prices_management_smoke.py`
- `python3 apps/wb_prices_management_browser_smoke.py`
- `python3 apps/wb_spp_tester_smoke.py`
- `python3 apps/wb_spp_tester_browser_smoke.py`

Regression smokes:
- `python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_browser_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`

All prices management and SPP tester smokes use fake upstreams and must not call live `POST /api/v2/upload/task`.

Public/live verification may open the page, read goods, run preview and inspect commit enabled/disabled state, but must not click live commit.

# 10. Out Of Scope

- Excel import/export of prices.
- Multiple SPP schedules or non-daily cadence.
- WB Club discount writes.
- B2B wholesale discount writes.
- Size-level price editing through `/api/v2/upload/task/size`.
- Automatic mass price changes.
- Google Sheets/GAS integration.
- Price index and auto-promo minimum price semantics without a separate official API research pass.

## `Управление SKU` reuse

The `Управление SKU` section reuses this module's official Prices API adapter, quarantine/current-value reads and upload-task contour. It deterministically derives an original-price/integer-discount pair for a desired seller price, blocks unavailable/current quarantine evidence, joins canonical per-SKU `promo_participation`/`promo_count_by_price` freshness (never the global `promoCurrentCount` denominator as SKU participation), records active or unavailable/stale promo evidence as an explicit override warning, and rechecks quarantine, promo evidence and the current tuple immediately before upload. It submits one target and polls task state plus fresh goods reads. The section records success only when original price, integer discount and seller price all match; equal seller price with another tuple is a controlled mismatch. Its dedicated application block is enabled by normal runtime construction and does not inherit the standalone `Цены` tab's legacy `WB_PRICES_WRITE_ENABLED` switch; section auth, preview, confirmation, stale/quarantine/promo validation, single-use action, audit and readback remain mandatory. Existing `Цены` and SPP-tester flag contracts are unchanged.
