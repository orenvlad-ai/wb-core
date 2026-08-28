---
title: "Модуль: wb_prices_management_block"
doc_id: "WB-CORE-MODULE-41-WB-PRICES-MANAGEMENT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический reference по operator-разделу `Цены` для чтения, guarded изменения цен/скидок и bounded `Проверка СПП` через WB Prices and Discounts API."
scope: "MVP раздела `Цены` в unified `/sheet-vitrina-v1/vitrina`: подтабы `Текущие цены` и `Проверка СПП`; current goods price/discount table and guarded edits; server-owned manual SPP tester for one nmID and exact ordered list of 1–6 prices with authenticated buyer readback, progressive compact results, history/log, execution lock and mandatory seller-tuple restore. Adaptive range/plan/refinement/threshold and scheduled SPP runs are removed."
source_basis:
  - "packages/contracts/wb_prices_management.py"
  - "packages/contracts/wb_price_quarantine.py"
  - "packages/contracts/wb_spp_tester.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/application/wb_prices_management.py"
  - "packages/application/wb_spp_tester.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "Official WB API docs: Prices and Discounts"
  - "Official WB Seller instruction `Карантин цен`, updated 2026-05-18: https://seller.wildberries.ru/instructions/ru/tj/material/price-quarantine?recommended=true"
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
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/start"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/status"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/restore"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/history"
related_runners:
  - "apps/wb_prices_management_smoke.py"
  - "apps/wb_prices_management_browser_smoke.py"
  - "apps/wb_spp_tester_smoke.py"
  - "apps/wb_spp_tester_browser_smoke.py"
  - "apps/change_registry_internal_writers_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/modules/03_MODULE__PRICES_SNAPSHOT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Manual SPP and ordinary prices preview share the conservative 1.5x/33.3% inclusive seller-discounted-price quarantine contract. Selected SKU current discounted price is visible before input; risky sequences are blocked before upload and rechecked immediately before each measurement write."
---

## Change-registry lifecycle

Standalone upload and the SKU-owned instance use the same module-58 seam with
different `source_surface`. Preview and stale/invalid confirmation create no
rows. Immediately before the only upload-task request, one transaction stores
the full old/requested price tuple for every nmID. Final upload success is not
confirmation by itself: exact current WB tuples must match before facts are
created; failure/mismatch remains explicit failed/ambiguous state.

# 1. Идентификатор и статус

- `module_id`: `wb_prices_management_block`
- `family`: `sheet_vitrina_v1/operator/official-api/prices`
- `status_main`: active implementation target
- `status_write_path`: guarded backend-only; ordinary price commit disabled unless `WB_PRICES_WRITE_ENABLED=true`; SPP tester live run/restore disabled unless both `WB_SPP_TEST_ENABLED=true` and `WB_PRICES_WRITE_ENABLED=true`

# 2. Product Semantics

Раздел `Цены` приближен по смыслу к WB Partners `Товары и цены -> Цена и скидка`, but it is not a pixel-perfect clone.

Раздел имеет два подтаба:
- `Текущие цены` — текущая таблица цен/скидок и ручной guarded upload-task workflow.
- `Проверка СПП` — manual live tool for one SKU/nmID and an exact ordered list of 1–6 operator-entered prices. It proves the authenticated-buyer-price capability, measures only those prices and restores the exact seller tuple.

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

For `Проверка СПП`, browser state is only the SKU, price-count and price-field draft. Current/last job, results, compact history, audit-derived ten-event log and restore proof are server-owned under `sheet_vitrina_v1_prices/spp_tests/`. Existing `jobs/*.json` remain compatible history truth.

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
- evaluates the shared conservative quarantine rule on exact kopecks: `new_discounted * 1.5 <= previous_discounted` (33.3% decrease or more, inclusive), and flags the concrete transition/drop percentage without changing ordinary upload semantics;
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
- accepts one ordered `prices` list of length `1..6` and never generates, sorts or deduplicates points;
- rejects `editableSizePrice=true` and existing quarantine at baseline;
- requires `WB_SPP_TEST_ENABLED=true`, `WB_PRICES_WRITE_ENABLED=true`, explicit live-change confirmation and `restore_baseline=true`;
- performs exact buyer-capability preflight on Start before any seller write and repeats it before every measurement write;
- retries only a generic transient `probe_error`/`session_probe_error` once after a short bounded pause; explicit logout/expiry, wrong account, login redirect, security challenge, recovery/automation lock and other blocking states are never retried; only an allowlisted navigation/HTTP/Chromium diagnostic category is exposed;
- validates fresh baseline discounted price → first requested point and every requested point → the next point using the shared 1.5x inclusive contract before creating a runnable job; risk returns controlled `422` with zero seller uploads;
- changes only integer seller `price` while preserving current discount during measurements;
- derives the integer seller `price` first, calculates the kopeck-exact expected `discountedPrice` after that rounding, and uses that expected value for quarantine comparisons;
- immediately before each measurement upload reads the fresh seller tuple and quarantine again, requires the expected previous tuple, and blocks on drift, unavailable proof, current quarantine or a newly risky transition;
- uses WB readback `discountedPrice` as actual seller price truth;
- uses only stable authenticated buyer price for SPP and has no anonymous fallback;
- stops remaining prices on the first error and proceeds to restore;
- writes sanitized upload/readback/buyer/quarantine events to JSONL audit;
- allows only one active/unrestored SPP test job at a time through runtime `current_job.json` pointer/heartbeat and removes that pointer after fresh exact seller baseline proof;
- shares one OS-level execution lock across manual jobs and emergency restore;
- reconciles an orphan only through fresh exact WB seller tuple + quarantine evidence; buyer evidence is not part of restore and TTL expiry alone never unlocks it.

Restore:
- is always required at the end of an MVP run;
- uses direct restore only for small moves;
- splits large downward discounted restore moves through bridge steps;
- evaluates every planned and fresh restore bridge against the same conservative rule; bounded bridge decreases remain strictly below the 1.5x threshold;
- requires upload success, WB readback and quarantine absence for bridge/final steps;
- final proof requires WB price/discount/discountedPrice equal baseline and quarantine absent; buyer availability cannot block seller restore;
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

`Проверка СПП` renders a minimal monitoring/testing surface:
- an exact authenticated buyer-price capability check; invalid auth/capability points to centralized `Настройки → Источники и сессии` and never starts recovery or exposes noVNC/launcher controls locally;
- SKU/nmID selector sourced from current price rows / active registry;
- the selected SKU's already-loaded current seller `discountedPrice`, with supporting original seller price and discount when available; selecting a SKU creates no additional upstream request;
- dropdown `Сколько цен проверить` with values 1–6 and exactly that many numbered money inputs;
- a concrete quarantine warning naming the risky transition and drop percentage; Start stays disabled for baseline→first, within-list and exact inclusive-boundary risks;
- one `Старт проверки` button;
- progressive five-column results: target, actual seller discounted, buyer, SPP, status;
- compact newest-first history without raw detail expansion;
- exactly the latest ten sanitized technical events at the bottom.

# 9. Verification

Targeted local smokes:
- `python3 apps/wb_prices_management_smoke.py`
- `python3 apps/wb_prices_management_browser_smoke.py`
- `python3 apps/wb_spp_tester_smoke.py`
- `python3 apps/wb_spp_tester_browser_smoke.py`

The targeted smokes prove selected-SKU current-price rendering, safe-sequence availability, baseline→first and within-list warnings, the exact inclusive 1.5x boundary, controlled `422` with zero upload, fresh per-write drift rejection, one transient buyer retry, explicit no-retry blockers, sanitized diagnostics and conservative restore bridges.

Regression smokes:
- `python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_browser_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`

All prices management and SPP tester smokes use fake upstreams and must not call live `POST /api/v2/upload/task`.

Ordinary prices public verification may open the page, read goods, run preview and inspect commit enabled/disabled state, but must not click ordinary live commit. Post-deploy SPP acceptance may perform only the separately authorized single bounded safe run defined by module 42, with exact baseline capture, mandatory restore and fresh seller tuple/quarantine/lock proof.

# 10. Out Of Scope

- Excel import/export of prices.
- Automatic or scheduled SPP checks.
- WB Club discount writes.
- B2B wholesale discount writes.
- Size-level price editing through `/api/v2/upload/task/size`.
- Automatic mass price changes.
- Google Sheets/GAS integration.
- Price index and auto-promo minimum price semantics without a separate official API research pass.

## `Управление SKU` reuse

The `Управление SKU` section reuses this module's official Prices API adapter, quarantine/current-value reads and upload-task contour. It deterministically derives an original-price/integer-discount pair for a desired seller price, blocks unavailable/current quarantine evidence, joins canonical per-SKU `promo_participation`/`promo_count_by_price` freshness (never the global `promoCurrentCount` denominator as SKU participation), records active or unavailable/stale promo evidence as an explicit override warning, and rechecks quarantine, promo evidence and the current tuple immediately before upload. SKU preview supplies one exact current-goods payload to both guarded validation and local table enrichment, so it performs no duplicate current-price fetch. Commit reuses one current tuple for promo freshness and runs upload-status plus exact tuple readback in one bounded early-cadence/deadline loop. It records success only when final upload status is successful and original price, integer discount and seller price all match; equal seller price with another tuple is a controlled mismatch. Optional public buyer-price enrichment is deferred instead of delaying confirmed seller-price success. Its dedicated application block is enabled by normal runtime construction and does not inherit the standalone `Цены` tab's legacy `WB_PRICES_WRITE_ENABLED` switch; section auth, preview, confirmation, stale/quarantine/promo validation, single-use action, audit and readback remain mandatory. Existing `Цены` and SPP-tester flag contracts are unchanged.
