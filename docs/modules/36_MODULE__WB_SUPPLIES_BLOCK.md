---
title: "Модуль: wb_supplies_block"
doc_id: "WB-CORE-MODULE-36-WB-SUPPLIES-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать canonical contract для read-only блока `Поставки -> Wildberries`: WB API / FBW Supplies registry, runtime cache/history, protected API, separate Seller Portal transit-cost enrichment, approved Fulfillment service-expense overlay and operator UI."
scope: "Official WB FBW Supplies read-only contour under current WebCore runtime plus separate overlays: read-only Seller Portal browser/network-json enrichment for missing transit cabinet cost, and server-owned approved Fulfillment service uploads for operator expense display. Covers adapter over `supplies-api.wildberries.ru`, supplemental Seller Portal `supply/cost` source boundary, SQLite cache/state/warehouse/enrichment tables, Fulfillment upload/line overlay tables, protected list/sync/detail/enrichment/API routes, embedded operator UI filters/table/pagination and targeted smokes."
source_basis:
  - "docs/modules/23_MODULE__REGISTRY_UPLOAD_HTTP_ENTRYPOINT_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "Official Wildberries Developer Portal: Supplies API / FBW Supplies"
  - "Seller Portal network research: `seller-supply.wildberries.ru/.../api/v1/supply/cost` internal JSON for cabinet transit cost"
related_modules:
  - "packages/adapters/wb_supplies.py"
  - "packages/adapters/seller_portal_transit_costs.py"
  - "packages/application/fulfillment_services.py"
  - "packages/application/ff_stock_ledger.py"
  - "packages/application/ff_wb_supply_origins.py"
  - "packages/application/warehouse_stocks.py"
  - "packages/application/wb_supplies.py"
  - "packages/application/wb_fbs_orders.py"
  - "packages/application/wb_fbs_shadow_polling.py"
  - "packages/application/ff_stage_7a_production.py"
  - "packages/adapters/wb_fbs_orders.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
related_tables:
  - "sheet_vitrina_v1_wb_supplies"
  - "sheet_vitrina_v1_wb_supplies_sync_state"
  - "sheet_vitrina_v1_wb_supplies_sync_runs"
  - "sheet_vitrina_v1_wb_supplies_warehouses"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment"
  - "sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs"
  - "sheet_vitrina_v1_wb_supply_cost_layers"
  - "sheet_vitrina_v1_wb_supply_ff_origin_assignments"
  - "sheet_vitrina_v1_fulfillment_service_uploads"
  - "sheet_vitrina_v1_fulfillment_service_lines"
  - "sheet_vitrina_v1_ff_stock_operations"
  - "sheet_vitrina_v1_ff_stock_operation_lines"
  - "sheet_vitrina_v1_wb_supplies_fbs_order_observations"
  - "sheet_vitrina_v1_wb_supplies_fbs_status_observations"
  - "sheet_vitrina_v1_wb_supplies_fbs_collector_state"
  - "sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings"
  - "sheet_vitrina_v1_wb_supplies_fbs_identity_mappings"
  - "sheet_vitrina_v1_wb_supplies_fbs_identity_evidence"
  - "sheet_vitrina_v1_wb_supplies_fbs_status_current"
  - "sheet_vitrina_v1_wb_supplies_fbs_status_transitions"
  - "sheet_vitrina_v1_wb_supplies_fbs_poll_runs"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/enrich"
  - "POST /v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/check"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/status"
  - "POST /v1/sheet-vitrina-v1/wb-cost/recalculate"
  - "GET /v1/sheet-vitrina-v1/wb-cost/status"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status"
  - "GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-supply-origins/{supply_ref}"
  - "POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-supply-origins/{supply_ref}"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/template.xlsx"
  - "POST /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}"
  - "DELETE /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}"
  - "GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}/payment-validation.pdf"
related_runners:
  - "apps/warehouse_cost_unified_recovery.py"
  - "apps/ff_stock_targeted_reconciliation.py"
  - "apps/ff_stock_targeted_reconciliation_smoke.py"
  - "apps/ff_stock_targeted_reconciliation_runner_smoke.py"
  - "apps/wb_supply_overlay_smoke.py"
  - "apps/ff_stage_7a_production.py"
  - "apps/ff_stage_7a_production_smoke.py"
  - "apps/wb_fbs_shadow.py"
  - "apps/wb_fbs_shadow_polling_smoke.py"
  - "apps/wb_supplies_api_adapter_smoke.py"
  - "apps/wb_supplies_backfill_live.py"
  - "apps/wb_supplies_backfill_smoke.py"
  - "apps/wb_supplies_accepted_parity_diagnostics.py"
  - "apps/our_wb_costs_smoke.py"
  - "apps/wb_supplies_first20_parity_smoke.py"
  - "apps/wb_supplies_goods_composition_diagnostics.py"
  - "apps/wb_supplies_goods_composition_smoke.py"
  - "apps/wb_supplies_incremental_sync_smoke.py"
  - "apps/wb_supplies_live_diagnostics.py"
  - "apps/wb_supplies_normalization_smoke.py"
  - "apps/wb_supplies_renormalize_cache.py"
  - "apps/wb_supplies_status_accepted_refresh_smoke.py"
  - "apps/wb_supplies_transit_cost_enrichment_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_http_smoke.py"
  - "apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py"
  - "apps/wb_supply_box_correction_smoke.py"
  - "apps/sheet_vitrina_v1_fulfillment_services_smoke.py"
  - "apps/sheet_vitrina_v1_fulfillment_services_browser_smoke.py"
  - "apps/registry_upload_http_entrypoint_public_routes_smoke.py"
related_docs:
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Official FBW supplies sync remains read-only and independent from Seller Portal success, and keeps the bounded process-owned transit-cost collector. A dedicated five-minute single-flight FBS timer owns bounded official order/status polling, shared rate budget, crash-safe cursor and immutable transitions. Migration 142 consumes those local observations only after an owner-gated opening: complete+sorted reserves/fulfills with exact-once local accounting, while supplier complete alone remains forbidden and no code writes WB. Durable canonical transit amount + append-only attempt evidence preserve last success across errors; stale active runs reconcile, identical work is single-flight, failures use taxonomy/backoff, successful amounts enqueue canonical targeted recalculation. List/status UI exposes separate Seller auth, exact supply-cost route, collector, freshness and coverage truth; local `Проверить` is route-specific and `Повторить сбор` is global. Login/recovery live only in central settings."
---

> Functional boundary: bounded reconciliation `31 500 / 31 477` ниже сохраняется как immutable incident evidence. Она не задаёт текущий `FF → WB`: active quantity после functional cutover всегда `max(fresh packed − fresh accepted, 0)`, final difference идёт только в pooled positive discrepancy, а WB quantity приходит только из complete official stocks snapshot.

# 1. Contract

- Operator surface: `Поставки` has sibling inner sections `Расчёты`, `Wildberries`, `ФФ`, `От поставщика`. The `ФФ` section contains inner subsections `Услуги ФФ` and `Остатки ФФ`.
- `Расчёты` keeps the factory-order and WB regional calculators and adds their shared read-only immutable `Реестр расчётов`; the registry is a calculation-history surface, not a second WB supplies source.
- `Услуги ФФ` is a separate server-owned upload/payment-validation contour documented in `39_MODULE__FULFILLMENT_SERVICES_BLOCK.md`; this module only defines how active approved Fulfillment expense lines are rendered over WB supply rows.
- `От поставщика` remains the supplier invoice registry and is not redefined by this module.
- `Wildberries` renders one screen:
  - inner section label: `Wildberries`;
  - title: `Все поставки`;
  - source note: `WB API / FBW Supplies · read-only`;
  - lead: `Read-only список поставок WB API / FBW Supplies`.
- The UI is read-only. It does not create, update, delete or draft WB supplies.
- Official WB API remains canonical for supply list/status/route/quantity evidence.
- Seller Portal remains a supplemental read-only source and never changes whether official WB sync succeeded. After a successful ordinary official sync and inside hourly/manual warehouse sync, the backend runs the process-owned autonomous transit collector; it does not run merely because the page opened and it does not use FBS APIs. Official FBS order/status polling is now a separate timer-owned process and is never invoked by this FBW sync.
- Fulfillment uploads are not official WB evidence. They are operator-uploaded runtime truth for service expenses and PDF payment validation only; failed uploads, unmatched rows, duplicate rows and deleted uploads must not affect the WB supplies list overlay.
- Management proxy WB cost layers are not official WB evidence and not strict accounting FIFO. They classify supply transit as `direct_zero_confirmed`, `transit_confirmed`, `transit_missing` or `unknown_route`, then combine SKU-level ФФ cost, WB transit, accepted Fulfillment services and allocated storage into `our_wb_unit_cost_rub`. Direct supplies with no transit marker and official/detail zero acceptance cost are confirmed zero transit, not missing transit; this explicitly covers supply patterns like `40431461`.
- ФФ stock ledger writeoffs are internal runtime movements only. They do not mutate WB, do not promote WB supplies cache into ЕБД metric truth, and use goods composition quantity because ФФ sent the planned composition regardless of later WB accepted quantity.

# 2. Official API Boundary

- Default upstream base URL: `https://supplies-api.wildberries.ru`.
- Required token env: `WB_API_TOKEN`.
- Optional base override: `WB_SUPPLIES_API_BASE_URL`.
- Timeout follows shared official API helper conventions.
- Seller Portal `seller-supply.wildberries.ru/.../api/v1/supply/cost` is not an official WB Developer API endpoint and must not be added to `packages/adapters/wb_supplies.py`.
- Seller Portal transit-cost enrichment lives under `packages/adapters/seller_portal_transit_costs.py`, uses the shared Seller Portal browser/session contour, and stores only normalized supplemental facts with source/evidence provenance.
- Implemented read methods:
  - `POST /api/v1/supplies`;
  - `GET /api/v1/supplies/{ID}`;
  - `GET /api/v1/supplies/{ID}/goods`;
  - `GET /api/v1/supplies/{ID}/package` exists in adapter as optional evidence and is not fatal for MVP table;
  - `GET /api/v1/transit-tariffs` exists in adapter/diagnostics as read-only tariff evidence; the UI does not calculate transit cabinet cost from it without a proven formula;
  - `GET /api/v1/warehouses`.
- Additional read-only methods reused by `Поставки -> Расчёты -> Подобрать склады WB`:
  - `POST /api/v1/acceptance/options` is a FBW planning information request, not a mutation; it is called only with the official JSON array body `[{barcode, quantity}]` from the latest regional calculation and server-owned nomenclature barcodes, while optional `warehouseID` is sent as a query parameter;
  - acceptance/options normalization treats official `result[]` rows as barcode-level evidence and groups them by exact destination `warehouseID`. A manager option exists only when every required barcode/quantity row contains that ID and `canBox=true`; partial and `canBox=false` groups remain coded exclusion diagnostics rather than visible alternatives;
  - Central identity/classification comes from the typed exact-ID registry. Names are exact-only historical fallback when ID is absent; `СЦ`, `СГТ`, specialised Food/Fuel/Tires, inactive, blocked and unclassified warehouses fail closed. A warehouseID-specific probe of an expected ordinary warehouse is not suppressed by a same-named specialised/SC row;
  - `GET /api/tariffs/v1/acceptance/coefficients` is read from the Common/Tariffs API base (`WB_TARIFFS_API_BASE_URL` override). For `box`, official `boxTypeID=1/2` rows are retained; other package types are excluded. A unique calendar date is available only when `allowUnload=true` and coefficient is `0` or `1`; nearest available and nearest free dates are calculated separately and chronologically;
  - `GET /api/v1/tariffs/box` evidence is normalized into logistics/storage/base/liter fields; `GET /api/v1/transit-tariffs` is joined by destination warehouse and exposed as raw route evidence, not a full exact cost formula;
  - the regional planning response includes sanitized registry/probe/exclusion/ranking diagnostics. Тверь, Владимир/Рязань and Коледино/Тула/Воронеж may become visible only with complete current WB evidence; blocked historical Электросталь/Котовск are never manager options.
- Additional read-only district mapping evidence:
  - district source is the planned/target supply warehouse (`warehouseName`, exposed as `planned_warehouse_name` / `target_warehouse_name` / `district_source_warehouse_name`);
  - `actualWarehouseName` and `transitWarehouseName` remain route/display/evidence only and must not decide the calculation district;
  - Marketplace `GET /api/v3/offices` (`WB_MARKETPLACE_API_BASE_URL` override) is the primary source; match is by normalized planned/target warehouse/offices name and raw `federalDistrict`;
  - tariffs `GET /api/v1/tariffs/box` (`WB_TARIFFS_API_BASE_URL` override) is fallback; match is by normalized planned/target `warehouseName` and raw `geoName`;
  - bounded manual known-warehouse fallback covers live/cache warehouses missing from external references and publishes `source/confidence/evidence` as `manual_known_wb_warehouse`;
  - Supplies `warehouse_id` is not treated as Marketplace office id.
- The separate official FBS boundary uses Marketplace `GET /api/v3/orders`
  (`WB_FBS_API_BASE_URL` override) with `limit<=1000`, an advancing `next`
  cursor and an explicit period no wider than 30 days. Stage 7A may additionally
  call `POST /api/v3/orders/status` only as the documented read semantic for an
  exact bounded ID set. It never calls FBS supply management, metadata,
  sticker/pass or another upstream mutation.
- Migration 140 activates that official shadow only through the hosted
  `ff-stage-7a-production-dry-run/apply/readback` contour. Seller warehouse
  identity comes from `GET /api/v3/warehouses`; its exact `officeId` is joined
  to `GET /api/v3/offices`. Only an explicitly accepted exact official Moscow
  city maps to `FF Москва`; `FF Оренбург` remains unrouted. SKU mapping requires
  one active nomenclature owner for exact `nmId/chrtId/one barcode/article`.
  Ambiguous, unmatched and incomplete identities are counted and isolated.
  Catch-up begins at `2026-08-01`; Migration 141 now polls through the
  dedicated five-minute FBS shadow timer with bounded jitter and a 10-minute
  normal-state SLO. It is not a real-time stream.
- FBW/FBS supply creation, transit create/update methods and all WB mutations stay outside scope of this module and of the regional planning assistant.
- Adapter errors are sanitized:
  - missing `WB_API_TOKEN` returns controlled app-level error;
  - upstream `401/403` maps to `WB API token has no Supplies permission or is invalid`;
  - acceptance/options HTTP diagnostics include endpoint, request shape, product count, optional `warehouseID` and sanitized WB body prefix, without token values or full barcode lists;
  - non-JSON and transport failures map to controlled transport errors;
  - token values are not printed.

# 3. Runtime Persistence

Runtime truth is server-owned SQLite under `RegistryUploadDbBackedRuntime`.

Tables:
- `sheet_vitrina_v1_wb_supplies`: primary cached rows keyed by legacy-compatible normalized `supply_id`, plus explicit stable `cache_key` (`supply:<supplyID>` / `preorder:<preorderID>`), normalized row JSON, sanitized raw list/detail/goods/package evidence, `wb_supply_id`, `preorder_id`, `warehouse_id`, `status_id`, `quantity_for_size_filter`, source dates, raw evidence hashes, `last_list_synced_at`, `last_enriched_at`, `enrichment_status` and `enrichment_error`.
- `sheet_vitrina_v1_wb_supplies_sync_state`: last sync fields plus `backfill_complete`, `backfill_started_at`, `backfill_completed_at`, `highest_synced_offset`, `last_successful_offset`, `last_mode`, latest-window counters, `may_have_more` and sanitized `last_error`.
- `sheet_vitrina_v1_wb_supplies_sync_runs`: per-run progress for `incremental_refresh`, `full_backfill` and explicit missing-critical enrichment requests: status/phase, offset/limit, pages/raw/upserted/new/changed/unchanged/enriched/failed counters, `may_have_more`, last error and compact sanitized logs.
- `sheet_vitrina_v1_wb_supplies_warehouses`: cached warehouse dictionary/options.
- `sheet_vitrina_v1_wb_supply_transit_cost_enrichment`: supplemental Seller Portal facts keyed by `supply_id`, with `amount`, `currency`, `amount_label`, `is_transit`, `source=seller_portal_browser`, `evidence_type=network_json`, `confidence`, `fetched_at`, `status`, sanitized `error`, sanitized `source_endpoint_path`, `created_at` and `updated_at`.
- `sheet_vitrina_v1_wb_supply_transit_cost_enrichment_runs`: durable process/job state for autonomous and explicit runs, including auth-required, route-unavailable, collector-unavailable and lock-busy counters. Append-only attempts carry the exact finer-grained error taxonomy; stale `queued/running` rows older than two hours reconcile as `orphan_reconciled`, and a current active row is joined as single-flight instead of starting duplicate work.
- `sheet_vitrina_v1_wb_supply_cost_layers`: management proxy cost-by-supply/SKU rows keyed by `wb_supply_id + nm_id + version`, current-row partial unique index, explicit `transit_cost_status`, source ФФ layer ids, Fulfillment upload id, per-unit transit/services/storage components, `our_wb_unit_cost_rub`, `source_status`, component JSON, `inputs_hash` and supersession fields. This table is recomputable/idempotent and does not mutate WB official evidence.
- `sheet_vitrina_v1_wb_supplies_fbs_order_observations`: Stage 5 append-only privacy-minimized official FBS order observations. It stores only order/supply/nmId/chrtId/warehouse/office/SKU/cargo identity, safe hash revision and collection provenance; address, comment, order UID, RID, price and raw JSON are excluded.
- `sheet_vitrina_v1_wb_supplies_fbs_collector_state`: one bounded last-attempt/success/window/cursor/count state row, absent while the default-off collector has never run.
- `sheet_vitrina_v1_wb_supplies_fbs_status_observations`: append-only exact
  order-revision/status-digest evidence from the official status read semantic.
- `sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings`,
  `sheet_vitrina_v1_wb_supplies_fbs_identity_mappings` and
  `sheet_vitrina_v1_wb_supplies_fbs_identity_evidence`: append-only exact-ID
  warehouse/SKU mappings and per-order matched/unmatched/deferred evidence.
- `sheet_vitrina_v1_fulfillment_service_uploads` and `sheet_vitrina_v1_fulfillment_service_lines`: server-owned Fulfillment upload/line persistence. The WB supplies block reads only fully valid uploads through the approved overlay provider and never treats them as WB official raw evidence.
- `sheet_vitrina_v1_ff_stock_operations` and `sheet_vitrina_v1_ff_stock_operation_lines`: internal ФФ stock ledger writeoffs are created idempotently with source key `wb_supply_debit:<cache_key or supply_id>` for eligible statuses `3/4/5/6`; statuses `1/2` and `Допринято` are skipped.
- `sheet_vitrina_v1_ff_stock_wb_auto_writeoff_checkpoint`: current ФФ stock ledger WB auto-writeoff boundary. Sync/backfill/detail enrichment ensures it before debiting, captures baseline-known `cache_key`, `source_key` and `supply_id` values from the current cache, and prevents historical/backfilled/cache-known WB supplies from being debited retroactively.
- `sheet_vitrina_v1_ff_stock_wb_supply_lifecycle`: durable complete-snapshot/cancellation journal. One missing active slice is debounced; two distinct complete observations or a strictly parsed confirmed-cancelled signal make only the unaccepted remainder eligible for one exact-cost FF return. Ordinary final accepted/cancelled cache history (`statusID=5/6`) is not reclassified merely because it is absent from an active slice. Economic return identity is bound to the original debit and canonical supply-source revision, so changed observation ids or lost lifecycle pointers cannot create a second return. Reappearance is retained as conservation evidence and cannot create a second debit/return.
- The ordinary checkpoint and pre-activation rules above are unchanged. The separate repo-owned v2 CLI `apps/ff_stock_targeted_reconciliation.py` is hard-bounded to `supply_id=40561872`, reads its identity and composition only from this cache, and may bypass only the exact pair `wb_supply_before_auto_writeoff_checkpoint` + `wb_supply_before_ledger_activation` after fingerprinted dry-run, integrity-checked backup and atomic revalidation of cache/goods/status/checkpoint/exact activation evidence/active nomenclature/affected balances plus the exact whole-ledger `38 250 - 31 500 = 6 750` totals. It is not a WB sync/backfill/detail route, is not an operator UI action and does not update the checkpoint or activation operation.
- A later official refresh may refine accepted-line evidence without rewriting that debit. For the exact current `40561872` operation, canonical replay pins the complete sent/accepted evidence fingerprints and conserves `31 477` accepted units inside the same supply when two one-unit SKU surpluses coexist with larger shortages. This is not a general tolerance: missing/drifted evidence, missing canonical baseline cost, future supplies and cross-supply allocation fail closed.

Transit-cost enrichment persistence must not store cookies, headers, Authorization values, storage-state content, raw HTML, screenshots or full raw network payloads.

The cache is an operator registry/cache only:
- it is not accepted ЕБД metric truth;
- it is not written into `web-vitrina` ready snapshots;
- old rows are not deleted just because they are absent from the latest fetch;
- sync upserts rows and preserves cached data after failed upstream attempts.
- list-only sync/backfill must not downgrade enriched rows: if new list evidence arrives but cached `raw_detail`, `raw_goods` or `raw_package` already exists, normalization is rebuilt from the new list plus existing enriched evidence.
- lazy detail/goods enrichment uses row-only persistence and does not rewrite global sync-state.
- Archived WebCore Data MCP compatibility exposes this cache only for legacy auth-gated read-only calls through `get_wb_supplies_registry`, `get_wb_supply_full_details` and the allowlisted business-table catalog/schema/rows tools. It is not a normal acquisition path. Compatibility reads do not call WB sync/backfill/detail lazy fetch, do not mutate sync-state, and return cached business payloads only through explicit scrubbed/bounded fields.

# 4. API Routes

`GET /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/fbs-orders[/{order_id}]`

Migration 138 may pin Stage 5 observation identities and safe fingerprints in
a dry-run cutover manifest, but it neither activates the collector nor turns an
observation into a reservation or physical debit. Every order observed before
the external boundary must be explicitly classified as absorbed closed,
absorbed opening reservation or unmatched; post-boundary orders are deferred.
An observation created before `T` but arriving outside the manifest belongs to
the isolated `Поздний заказ до границы` lane and never silently debits stock or
blocks unrelated processing.

Returns only the Stage 5 safe observation cache and collector state through the
existing protected `supply` role. Root is bounded/filterable; detail exposes
current plus bounded append-only history. Both reads use SQLite `mode=ro`,
`query_only=ON` and ETag, never fetch WB or initialize schema, and have no
corresponding POST route or operator UI control.

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
- `transit_cost_enrichment` meta includes source/evidence boundary, latest/active run and global `coverage`: eligible/confirmed/pending/retry_due/waiting_backoff/errors, taxonomy, last success/attempt/error, plus separate `auth_status`, exact `route_status`, `collector_status`, `freshness_status` and strict `overall_status`.

Rows expose Seller Portal enrichment as supplemental fields:
- `seller_portal_transit_cost`;
- `seller_portal_transit_cost_display`;
- `seller_portal_transit_cost_source`;
- `seller_portal_transit_cost_evidence_type`;
- `seller_portal_transit_cost_fetched_at`;
- `seller_portal_transit_cost_status`;
- `seller_portal_transit_cost_confidence`;
- `effective_cost_total`;
- `effective_cost_display`;
- `effective_cost_source`.
- `transit_per_unit_denominator`;
- `transit_per_unit_denominator_source`;
- `transit_per_unit_amount`;
- `transit_per_unit_display`.

Rows may also expose approved Fulfillment overlay fields:
- `fulfillment_amount_without_vat_total`;
- `fulfillment_vat_total`;
- `fulfillment_amount_with_vat_total`;
- `fulfillment_upload_ids`;
- `fulfillment_payment_validation_ids`;
- `fulfillment_per_unit_denominator`;
- `fulfillment_per_unit_denominator_source`;
- `fulfillment_per_unit_amount`;
- `fulfillment_per_unit_display`.
- `fulfillment_service_amount_with_vat_without_storage_total`;
- `fulfillment_storage_allocated_amount_with_vat_total`;
- `fulfillment_storage_per_unit_denominator`;
- `fulfillment_storage_per_unit_amount`;
- `fulfillment_storage_per_unit_display`.

Display priority:
1. official `cost_total`, when official evidence provides it;
2. Seller Portal enrichment amount only when the row is transit, official `cost_total` is unknown and enrichment status is `success`;
3. `—`.

A status-`1` preorder without a real numeric `wb_supply_id` is never a
Seller-Portal supply-cost candidate, even when its visible/preorder number is
numeric. It is rendered as `awaiting_supply_creation` / `Ожидает создания
поставки`, counted separately in coverage and is not classified as
`response_missing` or an endpoint error. Confirmed numeric zero remains a
successful fact distinct from missing; a failed latest attempt preserves the
last successful amount and a later success triggers only the bounded cost
replay.

Seller Portal values must not overwrite `cost_total` or become official raw evidence. A successful positive enrichment is nevertheless a canonical downstream-cost input: after saving the normalized evidence the application idempotently rematerializes canonical WB supply/SKU cost layers and immediately invokes the existing FF reservation reconciliation; the run reports the exact successful target supply IDs. Physical FF debit and reservation fulfillment remain one atomic ledger transaction; a reconciliation failure is visible on the enrichment run and never discards or promotes the saved evidence silently. Missing/failed evidence leaves that supply waiting and does not block independent supplies. Repeating the same enrichment/reconciliation is a no-op.

Fulfillment overlay values must not overwrite WB API fields, Seller Portal enrichment fields, `cost_total`, `effective_cost_total`, ready snapshots, 1C cost rows or ЕБД metric rows. They are display/payment-validation overlay values only.

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
- also fetch a bounded recent historical page with `statusIDs=[5,6]`; these rows are never used for hard-delete, but they are current status/quantity evidence for supplies that just moved to accepted/gate-shipped or whose accepted quantity changed after initial acceptance;
- merge/dedupe default and targeted raw rows by stable supply/preorder key before upsert, preferring the row with fresher `updatedDate` or targeted status evidence on duplicate keys;
- calculate stable `raw_list_hash`;
- upsert and enrich only new rows and rows whose `updatedDate`/raw hash changed;
- refresh supply-backed active rows (`2/3/4`) through detail/goods on ordinary refresh only when evidence requires it: status changed, enrichment failed/missing, or raw `updatedDate` is newer than the last enrichment;
- refresh supply-backed recent historical rows (`5/6`) from the bounded status slice through detail/goods when evidence requires it: previous active/non-historical status, failed/missing enrichment, suspicious accepted-zero row, or raw `updatedDate` newer than the last enrichment; forced refresh is capped at 12 prioritized rows per ordinary sync with status transitions first, then newer raw evidence, then critical/failed rows. This covers status `3/4 -> 5`, `3 -> 6` and same-status accepted-quantity changes without forcing detail/goods for every historical row on every click;
- for overlapping fields, fresh list evidence wins over cached detail for status/dates/planned quantity; `acceptedQuantity`/`unloadingQuantity` use list fields when present, then fresh aggregate detail, then goods totals, and stale detail only as fallback;
- when the active-status slice completes without upstream error and is not capped by `limit`, hard-delete local rows still in statuses `1..4` that are absent from both default latest and the active-status slice;
- never hard-delete accepted/historical statuses `5/6` just because they are absent from a latest refresh window;
- old historical rows absent from the bounded recent `5/6` slice are not retried by ordinary refresh; request `enrich=missing_critical` to run that bounded enrichment lane explicitly.

Sync response diagnostics include `fetched_recent_historical_statuses`, `recent_historical_status_ids`, `forced_status_refresh_rows`, `refreshed_recent_historical_rows`, `accepted_qty_changed_rows`, `partial_status_slices`, bounded `enrichment_failures` with supply identity/status/sanitized warnings and the existing new/changed/unchanged/enriched/delete counters. Detail/goods enrichment retries transient transport, 429 and 5xx failures three times with bounded backoff; 401/403 and other persistent 4xx remain immediate failures. Functional cutover/hourly validation stays fail-closed after retry exhaustion and includes the bounded supply-specific evidence in its operator error.

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

`POST /v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/enrich`

Starts a separate read-only background Seller Portal browser/network-json enrichment job and returns `202`.

Body:
- optional `supply_ids`;
- optional `list_params`;
- optional `limit`, default `50`, max `250`;
- optional `force`, default `false`.

Candidate rules:
- cached WB supply rows only;
- transit rows only: `has_transit_cost_marker=true` or transit warehouse evidence is present;
- official `cost_total` must be unknown;
- existing fresh/success Seller Portal enrichment is skipped unless `force=true`; MVP freshness TTL is 24 hours from the latest successful attempt (or canonical `fetched_at` when a later attempt failed);
- a same-value successful revalidation remains a T0 canonical fact (no artificial business revision) but its successful attempt time refreshes collector freshness and prevents immediate duplicate collection;
- without explicit `supply_ids` or diagnostic `list_params`, candidate selection is global over every eligible cached supply and ignores visible filters, pagination and offsets. Autonomous sync always uses this global mode. A 24-hour fresh success is skipped; failures use status-specific backoff (`lock_busy`, auth/session, route/collector, response/search/payload and not-found) unless a route-specific manual check uses `force=true`.

The worker uses the shared Seller Portal storage-state path/lock contract, navigates to `/supplies-management/all-supplies`, searches by supply id, waits for `listSupplies` and `supply/cost` network JSON, joins by `data.{supplyID}`, and extracts `costInSupplierCurrency.amountWithVat` before falling back to `cost`.

Hosted `warehouse-functional hourly-sync|manual-sync` calls the synchronous process-owned collector after official supply refresh and before downstream cost materialization. It runs at most four global batches of 250 candidates per invocation, so shutdown cannot orphan a daemon-only worker; unfinished/backed-off items remain durable for the next scheduled run. An ordinary protected WB-supplies sync invokes the same global collector. `sync-apply` does not add a pre-recheck mutation because its reviewed-plan optimistic boundary must remain exact.

`apps/ff_reservations_transit_cost_recovery.py` remains a legacy read-only diagnostic; its apply entrypoint is disabled because it copied the monolithic database and incorrectly gated physical movement on positive transit evidence. The reviewed production path is `apps/warehouse_cost_unified_recovery.py`: its query-only dry-run pins the explicit supplies and exact compositions, projects physical availability independently from cost, and its exact-fingerprint apply performs idempotent physical debits plus one targeted cost publication under the shared lock. Missing transit stays an explicit cost-freshness state and never a physical reservation reason.

`GET /v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/status?run_id=...`

Returns latest/requested run status, sanitized lock/error evidence and the global layered coverage contract. Session expiration is a controlled classified status, not a crash.

`POST /v1/sheet-vitrina-v1/supply/wb-supplies/transit-cost/check`

Forces one exact `supply/cost` route candidate and returns the ordinary run contract. It is read-only, route-specific, and not a generic Seller login/session probe. The UI polls the status route only while this bounded check is active.

`POST .../wb-supplies/sync` keeps official WB success independent from the supplemental result but now includes `transit_cost_collection` after a successful official sync. Collector errors lower transit health and remain visible without converting the official sync itself to failure.

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
- `eligible_status_ids = [3, 4, 6]`;
- statuses `1` (`Не запланировано`), `2` (`Запланировано`) and `5` (`Принято`) are excluded from selector options and are not rendered even as disabled rows;
- status `2` is only a WB slot reservation in the operational process, so it is not calculation evidence;
- calculate routes still revalidate posted `selected_wb_supply_ids`, so manually posted status `1`/`2`/`5` supplies and `Допринято` supplies are skipped with diagnostics and never counted;
- option is disabled when no operational supply date exists, goods composition is absent, or usable active SKU quantity is zero;
- quantity source is only goods composition `nmId -> quantity`; accepted/ready/partial reception fields are not used for overlay quantity;
- unknown active SKU, missing `nmId`, missing/non-positive quantity and non-active `nmId` goods rows are skipped with diagnostics;
- response exposes status/date evidence, date source field, warehouse/district mapping evidence, usable SKU count/quantity, skipped goods and disabled reasons.
- the first successful options load of a fresh operator page visibly selects every option where the backend returns `eligible_for_overlay=true` and `disabled=false`; browser code does not duplicate status/date/composition/mapping eligibility rules;
- after that first default, manual uncheck/recheck is preserved across `Обновить список`, repeated option loads, rerenders and switching between factory/regional forms. Refresh may only drop an id that is no longer present/valid and never silently re-add a manually removed id. A new page open starts a new selection session and evaluates the current backend default again;
- exact visible selected ids are posted as `selected_wb_supply_ids` and stored with the calculation result/immutable registry evidence. Empty and partial manual selections remain valid;
- the operator UI `Обновить список` action is sync-first: it runs bounded official `POST .../wb-supplies/sync` with `enrich=changed_only`, reloads cached list/options, preserves the current manual selection and shows a controlled warning when stale/invalid selected ids are dropped after revalidation.

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
- list fields are primary for status/date/planned fields during ordinary sync, with detail only filling fields absent from list;
- warehouse, route, quantity and cost fields use first non-empty fresh evidence from list/detail/goods/package/warehouse dictionary as appropriate;
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
- `quantity_added` priority: list/planned `quantity`, then `sum(goods.quantity)`, then `sum(package.quantity)`;
- `packed_quantity` priority: explicit packed/package field if present, then `sum(goods.quantity)`, then `sum(goods.supplierBoxAmount)`, then `sum(package.quantity)`, then accepted-supply fallback to `quantity_added`;
- `accepted_quantity` priority: list `acceptedQuantity` when present, then fresh detail `acceptedQuantity`, then `sum(goods.acceptedQuantity)`, then stale detail fallback;
- `quantity_for_size_filter` follows `quantity_added` before accepted/unloading fallbacks.

Cost fields:
- `acceptance_cost` preserves raw `acceptanceCost`;
- `transit_cost` preserves explicit transit cost fields if the upstream ever returns them;
- `cost_total` is the user-visible amount only when raw evidence provides a total, explicit transit cost, or a non-transit `acceptanceCost`;
- for transit rows with `acceptanceCost = 0` and no explicit total/transit cost, `cost_total = null`, `cost_display = —`, and `has_transit_cost_marker = true`;
- the UI must not render `0 ₽` for unknown transit cost.
- for non-transit accepted rows where official detail has `paidAcceptanceCoefficient = 0` and no explicit `acceptanceCost`, `cost_total = 0` with evidence `paidAcceptanceCoefficient.free_accepted_non_transit`; the UI renders `0 ₽` and coefficient `Бесплатно`.
- Seller Portal transit-cost collection adds supplemental `seller_portal_transit_cost*` and `effective_cost*` fields automatically after due sync collection or an explicit retry/check; official `cost_total` stays unchanged. A failed later attempt preserves the canonical last-success amount and stores its own status/error/attempt time.
- `effective_cost_total` is official `cost_total` first, then successful Seller Portal `supply/cost` amount for missing transit cost, then `null`.
- `effective_cost_source` is `official_wb_api`, `seller_portal_browser` or `unknown`; UI/debug text must keep this provenance available.

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
7. `Транзит`;
8. `Услуги ФФ`.

`Транзит` renders the current `effective_cost_total` amount plus `₽/шт`. The second line is the per-unit calculation, not a service/source label such as `Seller Portal`. Provenance remains available in backend fields.

Transit source state is explicit and lossless: confirmed positive, confirmed zero, not requested, updating, not found, source error, session expired, awaiting recalculation, included or recalculation error. `NULL`, no response, `not found`, source error and session expiry never become `0 ₽`; confirmed zero requires a successful source response that proves zero. A partially successful enrichment persists every successful row even when sibling supplies fail.

`Услуги ФФ` renders active approved Fulfillment amount with allocated STORAGE included plus `₽/шт`. Rows without approved matched Fulfillment upload lines render `—`. When storage allocation exists, the cell adds `в т.ч. хранение: X ₽/шт`. Failed, unmatched, duplicate and deleted Fulfillment uploads do not enter this column.

Per-unit denominator priority for both `Транзит` and `Услуги ФФ`:
1. accepted quantity / accepted goods total when available;
2. `quantity_for_size_filter` / known supply quantity;
3. planned/added quantity with a preliminary marker;
4. missing or zero denominator -> display `₽/шт —`.

Filters:
- search placeholder `Номер поставки`;
- warehouse dropdown summary `Склады: все` / `ФО: ...` / `Склад: ...`;
- federal district presets `Все · ЦФО · СЗФО · ПФО · УФО · ЮФО/СКФО · ДВФО/СФО` live inside the `Склад` dropdown; one or many district checkboxes filter by mapped warehouse district while unmapped warehouses remain available only through the concrete warehouse list;
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

The status selector always exposes the official status set `1..6`, even before rows with every status are present in cache. Rows keep backward-compatible `status_tone` (`idle`/`warning`/`success`/`neutral`) and also expose distinct `status_visual_tone` / `status_class` values `status-1..status-6`: `1` muted/neutral, `2` blue-muted, `3` violet/blue, `4` amber/orange, `5` green and `6` teal. The UI uses the distinct visual tone for pills while old consumers can keep reading `status_tone`. `Виртуальная` is not shown unless upstream evidence adds a specific marker.

Filter state is browser-owned and persisted for search, warehouse, selected district presets, selected statuses, size filter, page size and date sort. `Обновить поставки` must preserve those filters, reapply them after the new payload arrives, and ignore stale in-flight responses if the operator changes filters while a request is running.

First open behavior:
- GET reads cache;
- if cache is empty, the authenticated UI starts bounded incremental latest-window `POST .../sync` with `limit=1000`;
- if token/API is unavailable, the UI shows a controlled error instead of a silent empty table.

Buttons:
- `Обновить поставки` runs incremental official WB API refresh and the backend then collects all due eligible transit costs globally before returning. The list filters remain presentation-only. `Повторить сбор` invokes the same global eligible scope; `Проверить` forces one exact supply-cost route candidate and is not a generic login probe.
- the UI reports official sync separately from transit collection status and renders Seller auth, exact supply-cost route, collector, freshness, coverage, last success/attempt and the latest classified error. Green requires every mandatory layer; auth valid alone is never transit success.
- official sync failure stops the flow and does not launch Seller Portal enrichment;
- Seller Portal `session_expired`, automation lock busy, partial failure or no-candidate states are rendered as transit-cost stage messages and do not turn the completed official sync into a failed refresh;
- page open / cache read does not trigger transit-cost enrichment;
- Seller Portal recovery UI lives only in `Настройки → Источники и сессии`, using canonical `/opt/wb-web-bot/storage_state.json` and the shared `seller_portal_automation.lock.json`; the supplies page links there and does not duplicate relogin or launcher controls.
- `Загрузить всю историю` = one-time full backfill job; UI polls `sync-status` and shows offset/pages/fetched/upserted/enriched counters and last error.
- A separate transit-cost refresh button is not part of the primary UI. The backend route remains available for diagnostics and smokes.

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
- Stage 4 adds a separate lazy `Источник FF` evidence block below that cached
  composition. Only a real `wb_supply_id` may be linked to one existing active
  FF facility; its commercial pool is fixed to `FBO`;
- origin assignment is append-only, idempotent and current-assignment-CAS
  guarded behind the absent-by-default FF writer epoch. It mutates neither WB
  nor this cache and creates no FF reservation, debit, document, movement or
  balance;
- no Seller Portal action is available from the panel.

Final acceptance keeps declared FF composition as plan and gross per-nmID evidence as fact. Factory box size is a positive per-SKU nomenclature field, not a global hardcode. Automatic correction is allowed only when whole-box deltas preserve total sent quantity, every corrected sent quantity is at least accepted quantity, the minimum replacement count has exactly one deterministic solution and final status/evidence are current. The applied row stores declared/accepted/corrected compositions, gross shortage/surplus, box deltas, source revision, fingerprint and exact rollback manifest. If an earlier FF debit exists, one append-only compensation returns the missing box SKU and debits the substituted box SKU; rollback appends the inverse. Ambiguous/non-final evidence remains `Требуется сопоставить пересорт`. `Допринято` reduces discrepancy only for the same nmID and surplus never becomes negative stock.

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
- official API alone must keep transit amount as `—` with `с транзитом` when no supplemental evidence exists;
- Seller Portal browser research found a read-only internal network JSON source `POST seller-supply.wildberries.ru/ns/seller-api/suppliers-portal-goods/api/v1/supply/cost`, with response `data.{supplyID}.costInSupplierCurrency.amountWithVat`, matching screenshot-backed target rows `40422317 -> 10164`, `40421940 -> 45980`, `40119116 -> 23724`, `40119056 -> 3043.69`;
- this source is stored and rendered only as `source=seller_portal_browser`, `evidence_type=network_json`; no cookies/tokens/storage-state content, raw HTML, screenshots, headers or full raw payloads may be logged or persisted.

Targeted smokes:
- `python3 apps/ff_stock_targeted_reconciliation_smoke.py`;
- `python3 apps/ff_stock_targeted_reconciliation_runner_smoke.py`;
- `python3 apps/wb_supply_overlay_smoke.py`;
- `python3 apps/wb_supplies_api_adapter_smoke.py`;
- `python3 apps/wb_supplies_normalization_smoke.py`;
- `python3 apps/wb_supplies_first20_parity_smoke.py`;
- `python3 apps/wb_supplies_goods_composition_smoke.py`;
- `python3 apps/wb_supplies_backfill_smoke.py`;
- `python3 apps/wb_supplies_incremental_sync_smoke.py`;
- `python3 apps/wb_supplies_status_accepted_refresh_smoke.py`;
- `python3 apps/wb_supplies_filter_sort_date_smoke.py`;
- `python3 apps/wb_supplies_acceptance_expenses_report_smoke.py`;
- `python3 apps/wb_supplies_transit_cost_enrichment_smoke.py`;
- `python3 apps/wb_transit_cost_replay_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_supplies_http_smoke.py`;
- `python3 apps/sheet_vitrina_v1_wb_supplies_browser_smoke.py`;
- `python3 apps/ff_wb_supply_origins_smoke.py`;
- `python3 apps/ff_wb_supply_origins_http_smoke.py`;
- `python3 apps/ff_wb_supply_origins_browser_smoke.py`;
- `python3 apps/wb_fbs_orders_collector_smoke.py`;
- `python3 apps/wb_fbs_orders_http_smoke.py`;
- `python3 apps/wb_fbs_shadow_polling_smoke.py`;
- `python3 apps/sheet_vitrina_v1_fulfillment_services_smoke.py`;
- `python3 apps/sheet_vitrina_v1_fulfillment_services_browser_smoke.py`.

Regression/protection smokes include:
- `python3 apps/sheet_vitrina_v1_supplier_shipments_http_smoke.py`;
- `python3 apps/sheet_vitrina_v1_supplier_shipments_browser_smoke.py`;
- `python3 apps/sheet_vitrina_v1_operator_ui_persistence_smoke.py` (external `--base-url` is blocked by default because this smoke clicks refresh/calculate flows; use only loopback/isolated runtime unless explicitly acknowledging `--allow-live-mutations`);
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
- FBS supply management, stickers/passes and every upstream mutation. Migration
  140 activates only the official read-only collector/backfill and exact shadow
  mappings; `POST /api/v3/orders/status` remains a read semantic and no
  observation/status becomes a physical trigger;
- FBS order-origin assignment or any FBS inventory/reservation/movement consumer;
- general Seller Portal browser automation outside the bounded read-only transit-cost enrichment worker;
- automatic Seller Portal scans on page open or inside the backend official WB sync route;
- DOM scraping as the primary transit-cost source;
- Google Sheets/GAS writes;
- accepted metric truth in web-vitrina ready snapshots;
- final товарная себестоимость;
- 1C cost truth changes;
- ЕБД metric truth changes;
- global cost truth switch;
- AI logic.

## Stage 7B dedicated official FBS shadow

Migration 141 removes FBS polling from ordinary WB-supplies refresh and from
the long hourly warehouse/cost writer.  Those responses now report
`dedicated_collector` without making an upstream FBS call.  The separate
`wb-core-fbs-shadow-collector.timer` invokes a single-flight five-minute
poller with a trailing seven-day window, bounded pages/status batches,
crash-safe per-page cursor and the shared official FBS request budget.

Its POST to `/api/v3/orders/status` is read-only.  Append-only transition and
poll-run evidence contains only official order/revision/status identities,
local observation times and safe counters; no portal credentials, raw payload
or customer data are retained.  Portal-lane comparisons remain explicit
inference.  `supplierStatus=complete` never debits, and `wbStatus=sorted`
remains a candidate pending repeatable evidence and separate official semantic
review.

Migration 142 records that review in an owner-gated manifest. The only approved
handoff proposal is the official conjunction `supplierStatus=complete AND
wbStatus=sorted`; observed transition counts are evidence and never automatic
approval. After exact opening, the same collector persists observations first
and invokes an epoch-gated local lifecycle consumer: eligible orders reserve,
pre-handoff cancellation releases, approved handoff debits frozen-WAC physical
stock exactly once, and later terminal status creates no second debit. A later
cancellation/return uses a separate reconciliation lane. Late pre-T evidence is
isolated and no code writes WB. A clean manifest-pinned pending China receipt is
not readiness-blocking and contributes zero opening/debit; ambiguous or partly
posted receipt state still blocks.

# 12. Own capital movement consumer

The warehouse opening consumer is separate from the cost-engine movement consumer. `warehouse_stocks_block` reads persisted `raw_goods` only for the current material FF→WB source: ordinary status `3` (`Отгрузка разрешена`) and its later proven non-final physical stages `4/6` form `В пути: FF → WB`; planned `2`, final `5` and `Допринято` are excluded. The separate acceptance-discrepancy opening is a management boundary fixed at zero with no SKU lines. Historical final/doprinato rows are not read, validated or fingerprinted by opening and remain available only to an optional bounded read-only diagnostic. The consumer stores a one-time immutable quantity snapshot with source row/hash/timestamps and never updates this module's cache, triggers WB sync, or creates FF/cost/capital operations.

Module 45 consumes normalized WB goods/accepted evidence without changing this module's read-only WB boundary. Unknown nmID may remain upstream/cache evidence but atomically blocks FF writeoff, cost allocation and capital movement until authoritative nomenclature exists.

For an ordinary supply, `FF → WB = max(packed - accepted, 0)` until final acceptance, but only after an actual canonical FF debit. An eligible supply created before its goods arrive at FF stays in the separate append-only reservation ledger: it neither reduces physical FF nor enters FF→WB. Once its exact whole composition is physically available, one transaction creates the physical debit and closes the reservation regardless of transit/services/storage/paid-acceptance cost freshness; missing cost stays null with a preliminary/unavailable reason. Identity/composition ambiguity remains a local physical blocker. Accepted quantity is never manually added to WB: official contour snapshot owns WB quantity. At final acceptance the transit layer closes and positive `packed - final accepted` enters the separate pooled `Расхождения приёмки WB` warehouse by SKU. `Допринято` never repeats the FF debit; it consumes only a positive discrepancy of the same SKU, while surplus becomes transitional unmatched audit. Planned quantity/date, upload date, ambiguous identity and fabricated zero are forbidden substitutes.

Since `2026-07-01`, the cost layer used by that movement is the immutable functional snapshot of the exact `ff_stock_ledger` debit, not the latest FF cost line by nmID. Accepted quantity contributes inbound capital only with accepted status, final accepted quantity and factual accepted date. Transit/accepted Fulfillment add-ons attach to the same supply/SKU component graph; paid WB acceptance applies only to accepted units and is excluded from discrepancy cost. Discrepancy matching after final acceptance is pooled by exact nmID and never requires impossible factory-lot identity after FF mixing.

At the `2026-07-01` cost-history boundary every legacy anomaly policy remains immutable audit-only. Full warehouse history starts only at `warehouse_functional_cutover_v1`; its coherent watermarks absorb all source state before the cutover timestamp, so pre-boundary events cannot be replayed into active balances.

The bounded `CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V1` is read-side cutover evidence only: 10 exact final-accepted supply rows / 11 units are matched by full persisted identity, date, nmID, route, quantity, status, raw row/line fingerprint and current baseline cost reference before direct/FIFO. A match means the evidence is already absorbed by official WB stock and therefore creates no second quantity/capital/confirmation event and does not close unrelated outstanding. Any future or drifted row remains fail-closed.

V1 remains immutable. The separate `CUTOVER_UNMATCHED_DOPRINATO_ABSORPTION_V2` covers only 9 additional exact `supply_id + nmID` rows / 12 units and also pins empty `original_supply_id`, raw supply-row, goods-line, combined and semantic fingerprints. It has identical audit-only official-WB-stock semantics and cannot enter direct/FIFO. The strict backfill verifies lot-level recognized/paid unit-cost continuity from each immutable FF debit snapshot; stage aggregate WACs may differ because their SKU/lot composition differs.

Canonical FF replay resolves a WB writeoff date from authoritative persisted evidence. A valid operation `diagnostics.source_timestamp` wins; the repo-owned legacy targeted runner's equivalent `diagnostics.supply_timestamp` remains a compatibility provenance field. If neither exists, the engine requires one exact `sheet_vitrina_v1_wb_supplies` row matching both operation source object and canonical source key, then uses factual acceptance/fact date first and planned supply date only when no factual date exists. `ff_stock_operation.created_at` is never a WB business-date fallback. Missing, ambiguous or conflicting identity/date fails closed and is surfaced by the canonical backfill audit.
## Canonical cutover boundary (2026-07-01)

WB supplies already present in the coherent functional-cutover source snapshot are absorbed by exact source watermarks/digests and are not replayed into opening WB/FF→WB/discrepancy quantity or capital. Post-cutover corrections use stable supply/SKU identities and effective dates; raw persisted source evidence remains unchanged.

## Unified recovery-policy boundary

Ordinary official-source refresh remains idempotent and keeps its durable
sync-run/cache revision evidence. Selected supply/SKU replay uses central T1;
the hourly/manual publication that encompasses supply refresh, downstream
layers, FF reconciliation and the six warehouse stages uses one central T2
warehouse/cost domain checkpoint. Neither path may open Finance raw or regain a
full-store checkpoint. Historical targeted supply runners either route through
T1 or remain disabled as documented in module 51.

## Transit last-success and late replay

Seller Portal transit enrichment stores every attempt append-only and keeps a
separate canonical last-success. `failed`, `timeout`, `not_found`,
`session_expired` and logged-out attempts update only attempt freshness/error;
they cannot erase an earlier confirmed amount, currency or evidence. The UI
therefore shows the preserved amount together with the latest attempt warning.
Unknown stays NULL/`—`; zero is numeric only after a successful zero fact.

Mixed runs commit each success independently. Retry selection includes only
missing/stale facts and successful facts whose durable targeted recalculation
is awaiting/error. An unchanged successful fact keeps its source and success
revision; a changed fact advances exactly one revision. After fact commit the
bounded `wb_transit_cost:<supply_id>` replay owns cost-layer/WAC consumers. A
failure leaves `recalculation_error` and never loses the successful fact or
creates a second physical WB/FF movement. The canonical hourly/manual pipeline
marks the fact `complete` only after its exact functional, economics and
Finance consumers finish; a partial pipeline failure leaves durable work for
the next bounded retry.

`wb_transit_cost:*` is classified as a cost-only functional revision, never as
a generic physical/source revision. Its queue retains the originating factual
date. If that date predates the active business-time projection by more than
the 366-day safety bound, the public projection is limited to the intersection
of the active materialized surface, functional cutover and final bounded
window; diagnostics retain the requested/applied boundaries and omitted-day
count. Quantity keys must remain byte-semantically unchanged, and the runtime
must not weaken the 366-day limit or fabricate pre-cutover warehouse history.
