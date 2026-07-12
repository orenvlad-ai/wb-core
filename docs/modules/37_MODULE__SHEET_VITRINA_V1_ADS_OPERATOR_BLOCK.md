---
title: "Модуль: sheet_vitrina_v1_ads_operator_block"
doc_id: "WB-CORE-MODULE-37-SHEET-VITRINA-V1-ADS-OPERATOR-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический reference по operator-разделу `Реклама` для SKU-first чтения WB Promotion API и guarded изменения ставок."
scope: "SKU-first ads MVP in `/sheet-vitrina-v1/vitrina`: top-level tab `Реклама`, first-level active SKU/nm_id table from `registry_upload_config_v2`, enrichment from `sheet_vitrina_v1_nomenclature_items`, drawer with campaign/placement rows from WB Promotion API, read-only metrics/min/recommended bids where available, and guarded one-row bid update workflow `preview -> explicit commit -> audit -> delayed refresh`."
source_basis:
  - "packages/application/sheet_vitrina_v1_ads.py"
  - "packages/adapters/wb_promotion.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_modules:
  - "packages/application/sheet_vitrina_v1_ads.py"
  - "packages/adapters/wb_promotion.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_tables:
  - "registry_upload_config_v2"
  - "sheet_vitrina_v1_nomenclature_items"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/ads/skus"
  - "GET /v1/sheet-vitrina-v1/ads/sku/{nm_id}"
  - "POST /v1/sheet-vitrina-v1/ads/bid-change/preview"
  - "POST /v1/sheet-vitrina-v1/ads/bid-change/commit"
related_runners:
  - "apps/sheet_vitrina_v1_ads_smoke.py"
  - "apps/sheet_vitrina_v1_ads_browser_smoke.py"
related_docs:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
source_of_truth_level: "module_canonical"
update_note: "Initial SKU-first ads operator MVP with read routes, guarded bid preview/commit, persistent audit and no bulk/auto-bidding."
---

# 1. Идентификатор и статус

- `module_id`: `sheet_vitrina_v1_ads_operator_block`
- `family`: `sheet_vitrina_v1/operator/wb_promotion`
- `status_main`: active implementation target
- `status_write_path`: guarded backend-only; disabled unless `SHEET_VITRINA_ADS_WRITE_ENABLED=1`

# 2. Product Semantics

Раздел `Реклама` является SKU-first, not campaign-first.

Top-level UI shows active SKU/nm_id rows from `registry_upload_config_v2`. `our_sku`, barcode and readable name are enrichment only from `sheet_vitrina_v1_nomenclature_items`; WB Promotion API identity remains `nm_id`.

Clicking a SKU opens a drawer. The drawer preserves campaign-level and placement-level detail:
- `advert_id`
- campaign name
- status
- `payment_type`
- `bid_type`
- placement
- current bid
- min bid when WB endpoint returns it
- recommended bid for CPM when WB endpoint returns it
- stats fields from `/adv/v3/fullstats`

Stats scope is explicit: `/adv/v3/fullstats` is treated as campaign/SKU aggregate. The UI must not invent placement-level stats when WB does not return placement-level metrics.

# 3. WB Promotion API Boundary

The adapter `packages/adapters/wb_promotion.py` is a detail-preserving WB Promotion API boundary over the canonical `WB_API_TOKEN` runtime helper.

Read endpoints:
- `GET /adv/v1/promotion/count` discovers campaign ids.
- `GET /api/advert/v2/adverts` loads campaign details and `nm_settings`.
- `POST /api/advert/v1/bids/min` loads min bid per nm/placement when available.
- `GET /api/advert/v0/bids/recommendations` loads recommended bids for CPM; CPC renders as `not_available`.
- `GET /adv/v3/fullstats` loads campaign/SKU aggregate metrics.

Write endpoint:
- `PATCH /api/advert/v1/bids` is called only from `POST /v1/sheet-vitrina-v1/ads/bid-change/commit`.

Frontend must never call WB Promotion API directly and must never issue an ads `PATCH`.

# 4. Reverse Mapping Design

The application block builds a reverse index:

`WB campaigns -> nm_settings/nm_id -> internal SKU row -> campaigns[] -> placements[]`

Rules:
- primary key is `nm_id`;
- internal `our_sku` is display/enrichment and not API identity;
- SKU without campaigns remains in the top-level table with status `no_campaigns`;
- WB campaign nm_id missing from `registry_upload_config_v2` is surfaced as `missing_in_registry`, so campaign evidence is visible instead of silently dropped;
- placement names are normalized to `combined`, `search`, `recommendations`, with WB min-bid placement `recommendation` mapped back to UI `recommendations`;
- campaign rows are not collapsed to max bid and do not reuse the older ads-bids max-only path as drawer source.

# 5. Safety Workflow

Bid changes are one-row only:

`nm_id + advert_id + placement -> requested_bid_rub`

Preview route:
- re-fetches current campaign details;
- proves the nm_id belongs to advert_id;
- validates campaign status in `4/9/11`;
- validates `payment_type`, `bid_type` and placement;
- resolves current bid;
- fetches min bid when available;
- validates requested bid against min bid and safety thresholds;
- converts rubles to kopecks with `Decimal` and at most two decimal places;
- stores a short-lived preview payload;
- performs no WB mutation.

Commit route:
- requires `SHEET_VITRINA_ADS_WRITE_ENABLED=1`;
- accepts only one preview id / one bid operation;
- rejects stale preview;
- re-fetches current bid and blocks if it differs from preview old bid;
- sends one `bids[0].nm_bids[0]` PATCH request to WB;
- writes a JSONL audit event;
- returns `pending_refresh` with delayed refresh guidance because WB bid sync has lag.

No bulk changes, no auto-bidding and no direct frontend PATCH are in scope.

# 6. Safety Thresholds

Runtime env/config:
- `SHEET_VITRINA_ADS_WRITE_ENABLED`
- `SHEET_VITRINA_ADS_MAX_BID_RUB`
- `SHEET_VITRINA_ADS_MAX_PERCENT_INCREASE`
- `SHEET_VITRINA_ADS_MAX_ABSOLUTE_INCREASE_RUB`
- `SHEET_VITRINA_ADS_PREVIEW_TTL_SECONDS`

The backend blocks:
- below-min bids when min bid is available;
- unsupported status;
- nm_id/advert_id mismatch;
- unknown placement;
- bid above absolute max;
- increase above percent or absolute threshold;
- stale preview/current-bid mismatch.

# 7. Audit

Audit is persistent JSONL under runtime:

`sheet_vitrina_v1_ads/bid_audit.jsonl`

Each event stores:
- actor key when available;
- timestamp;
- operation id;
- nm_id;
- advert_id;
- placement;
- payment_type;
- bid_type;
- old/new bid in rubles and kopecks;
- delta;
- preview facts;
- sanitized WB request shape without token/secret;
- WB response/result.

# 8. UI

The `Реклама` tab is a sibling section in the unified operator shell. It renders:
- dense SKU table;
- loading/empty/error states;
- last refreshed timestamp;
- drawer on SKU row click;
- campaign/placement table in drawer;
- per-row bid input and preview button;
- confirmation modal with SKU/nm_id, advert_id, campaign name, placement, old/new/delta/min bid and live-spend warning;
- commit button that calls only the guarded backend commit route.

# 9. Verification

Targeted local smokes:
- `python3 apps/sheet_vitrina_v1_ads_smoke.py`
- `python3 apps/sheet_vitrina_v1_ads_browser_smoke.py`

These use fake Promotion API sources and do not call live WB write methods. They cover reverse mapping, placement normalization, min-bid/preflight validation, mocked PATCH shape, audit event, read routes, preview/commit routes, UI tab/table/drawer/modal, and negative safety cases.

# 10. Out Of Scope

- WB Media.
- normquery/search cluster bid editing.
- auto-bidding.
- bulk bid changes.
- campaign-first top-level UI.
- Google Sheets/GAS write/load revival.

## `Управление SKU` reuse

The `Управление SKU` section reuses this exact WB Promotion adapter, campaign membership/min-bid/current-bid validation and single-target PATCH contract. Its dedicated application block performs bounded delayed control reads and records success only after the exact `advert_id + placement` returns the requested bid. That section is enabled by normal runtime construction and does not inherit the standalone tab's legacy `SHEET_VITRINA_ADS_WRITE_ENABLED` switch; section auth, preview, explicit confirmation, stale/safety validation, single-use action, audit and readback are its sufficient gates. The original `Реклама` tab gate is unchanged.
