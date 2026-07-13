---
title: "Модуль: sku_management_block"
doc_id: "WB-CORE-MODULE-46-SKU-MANAGEMENT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический MVP операторского раздела `Управление SKU`: объяснимый прогноз дефицита, коммерческий read model, guarded price/bid writes, stabilization warnings и единая история воздействий."
scope: "Одна строка на active nmID, server-owned настройки и table preferences, calculation-only depletion forecast, inline seller-price и exact campaign/placement bid changes через существующие WB adapters, confirmed-readback event history и три метрики web-vitrina."
source_basis:
  - "packages/application/sku_management.py"
  - "packages/application/sheet_vitrina_v1_sku_actions.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "registry/pilot_bundle/metric_runtime_registry.json"
related_modules:
  - "07_MODULE__STOCKS_BLOCK.md"
  - "08_MODULE__SALES_FUNNEL_HISTORY_BLOCK.md"
  - "31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "35_MODULE__SPP_PROXY_BLOCK.md"
  - "36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "37_MODULE__SHEET_VITRINA_V1_ADS_OPERATOR_BLOCK.md"
  - "41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
related_tables:
  - "registry_upload_config_v2"
  - "sheet_vitrina_v1_user_configs"
  - "sheet_vitrina_v1_sku_action_events"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/sku-management"
  - "GET|POST /v1/sheet-vitrina-v1/sku-management/settings"
  - "POST /v1/sheet-vitrina-v1/sku-management/price/preview"
  - "POST /v1/sheet-vitrina-v1/sku-management/price/commit"
  - "POST /v1/sheet-vitrina-v1/sku-management/bid/preview"
  - "POST /v1/sheet-vitrina-v1/sku-management/bid/commit"
  - "GET /v1/sheet-vitrina-v1/sku-management/history"
related_runners:
  - "apps/sku_management_smoke.py"
  - "apps/sku_management_browser_smoke.py"
  - "apps/sku_management_metrics_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Production closure: stage-aware calculation-only forecast without optimistic regional/FF/WB fallbacks, exact price tuple and delayed bid readback, commit-time quarantine/promo/min-bid rechecks, persisted readback/override audit and observed buyer-price freshness."
---

# 1. Identity, authorization and truth

- `module_id`: `sku_management_block`.
- Unified tab: `Управление SKU` inside `/sheet-vitrina-v1/vitrina`.
- Authorization section: `sku_management`; it uses the existing `allowed_sections` model and WebCore session. There is no parallel user system.
- Row universe: enabled rows of canonical `registry_upload_config_v2`; nomenclature only enriches display identity.
- Forecast and table are read/calculation projections. They do not create orders, supplies, stock operations or accepted business truth.

Settings and table preferences use `sheet_vitrina_v1_user_configs` under the `sku_management` config key with optimistic revision checks. `localStorage` is not a source of truth. Server defaults are 14 sales days, 90 forecast days, 30-day future order cadence, 30-day production, 30-day factory-to-FF, 7-day FF-to-WB, 14 safety-stock days and three-day price/bid stabilization. Sales period is one of `7/14/30/60`; all other bounds are validated server-side. Zero stabilization disables that warning.

# 2. Forecast semantics

The forecast consumes existing contours only: active registry mapping, `stocks_block`, `ff_stock_ledger`, availability-adjusted sales history, registered supplier shipments/factory-order evidence, registered WB supplies and existing regional-demand/allocation results.

For each SKU the engine forms a dated inbound stream, deduplicated by `source + source_id + date + district`; repeated goods lines inside one WB supply are aggregated by nmID before that identity is emitted. Current WB stock is saleable immediately. Current FF stock becomes a WB inbound only after the configured FF-to-WB lead; quantities reserved by a registered WB supply are removed from that generic FF transfer. A WB supply forecasts only `planned goods composition − max(factual ready/accepted/added quantity)`, because the progressed part is already covered by current WB stock. A supply without an FF-ledger debit reserves its full planned composition in the initial FF pool while adding only that remaining quantity; a supply with an idempotent full ledger debit returns only the remaining quantity as a dated WB inbound. Invalid and overdue transfers are excluded before they can reserve FF. This prevents accepted partial units from existing in WB stock, FF stock and future inbound simultaneously. Supplier registry rows use only `production`/`in_transit`, prefer actual shipment date to planned shipment date and add factory-to-FF plus FF-to-WB leads. Accepted-FF rows are excluded because they are already in the FF ledger. Manual factory-order evidence is reused only when it is not the supplier-registry projection, preventing the same goods from entering two contours. Missing dates, overdue plans, partial quantities, duplicate identity and insufficient FF reservations remain explicit warnings instead of optimistic arrivals. Each day adds that day's valid inbound, consumes the selected availability-adjusted daily demand and measures stock against `daily demand × safety-stock days`.

After the last registered inbound plan, the model may add synthetic future factory orders at the configured cadence. Arrival is order date plus production, factory-to-FF and FF-to-WB lead times. Quantity uses the existing replenishment principle—demand over replenishment cadence plus safety requirement, rounded to a configured batch. Synthetic rows exist only in the response timeline; they are never persisted as factory orders, supplier shipments or WB supplies.

Output contains risk (`low/medium/high/unknown`), first deficit date, minimum projected stock including negative values, deficit units, norm coverage percent, first risky district when fresh district stock and demand are both available, compact reason and explicit quality/warnings. Authoritative zero demand remains zero rather than `unknown`. Missing WB or FF stock/demand makes the overall forecast `unknown`; absent or stale regional evidence produces district `unknown`. Missing district fields are never converted to zero. Default sort is highest risk, then nearest deficit.

# 3. Commercial table and presentation state

The table reuses current server projections for seller/buyer prices, SPP proxy, promo, campaigns/placements, ad performance, funnel, orders/sales, profit and margin. Promo participation/filtering uses canonical per-SKU `promo_participation`; the SKU count uses `promo_count_by_price`, while the Prices label retains its documented `eligible / total current promos` global-denominator context. A global current-promo count is never interpreted as participation of every SKU. Campaign identity/current bids are loaded through one current reverse placement index, without per-SKU minimum/recommendation request fanout; exact minimum is fetched at preview and again at commit. Buyer price chooses the freshest factual public-card observation among the confirmed event readback, the current captured `spp_proxy` temporal snapshot and web-vitrina projection; exact capture time lets a later same-day refresh supersede an older event, while the immediate event remains fresher than a not-yet-refreshed projection. Quality/freshness are exposed. It supports three-state header sort, SKU/risk/promo and numeric filters, visible columns, order and width. Applicable presentation state is persisted with settings through the server-owned user-config contour.

Loading, empty, partial evidence, stale, validation, preview-ready, commit-running, readback-pending, success and controlled upstream-error states are explicit. After confirmed response the table and an open history block are fetched again without a full page refresh.

# 4. Seller-price write flow

The operator edits desired seller price. Preview uses the canonical Prices Management adapter and deterministically searches an integer original-price/discount pair whose WB seller price equals the target. It returns current/new original price, discount, seller price, current factual public buyer price when available, promo/quarantine/stale and stabilization warnings. It never substitutes an estimated buyer price for absent public evidence.

Commit requires one unexpired preview id, explicit confirmation, exact nmID and explicit warning override for stabilization, active-promo or unavailable-promo evidence. Quarantine evidence is fail-closed and a current quarantine blocks preview and commit. The backend rechecks quarantine, promo evidence and the current WB original-price/discount/seller-price tuple before upload; a promo change after preview requires a new preview. It submits one canonical Prices API upload task, polls task state and performs fresh goods reads. Success exists only when original price, integer discount and seller price all match the requested tuple. A seller-price-only match with another tuple is a controlled failed event, never optimistic success. After confirmed tuple readback the public-card contour is queried separately; missing public evidence remains missing and is never replaced by a calculated buyer price.

# 5. Advertising-bid write flow

Bid identity is always `nm_id + advert_id + placement`. A single placement can be edited directly; multiple campaigns/placements require exact selection. Preview delegates campaign membership, current/min bid, freshness and safety validation to Ads Operator and adds stabilization warnings.

Commit accepts one preview once, rechecks current bid, fetches the current WB minimum again, reapplies absolute/relative safety thresholds, submits one WB Promotion API operation and performs delayed bounded cache-bypassing control reads. Success is stored only when the selected placement returns the requested bid. Unavailable or increased minimum fails before PATCH. Aggregate/max bid is never used as mutation identity.

# 6. Write gates and safety

The dedicated `sku_management` price and ad blocks are enabled in normal runtime construction and do not depend on disabled-by-default `WB_PRICES_WRITE_ENABLED` or `SHEET_VITRINA_ADS_WRITE_ENABLED`; those legacy flags still govern their original standalone surfaces. This section remains guarded by section authorization, one exact target, server validation and short-lived preview, explicit confirmation, stale/current/min/quarantine checks, backend-only WB calls, a single-use preview claim, sanitized audit and post-write readback. No frontend call targets a WB host or issues a WB write directly.

# 7. Stabilization and event history

The authoritative window starts at confirmed WB readback time. Same-parameter changes report elapsed and remaining days. With cross warnings enabled, a price change warns a bid edit and vice versa. Warnings are advisory: the operator may cancel or explicitly override; override requires no text reason but is stored automatically.

`sheet_vitrina_v1_sku_action_events` is the single server-owned event/audit contour. It stores nmID, parameter, old/requested/confirmed value, delta, exact timestamps, username/source, price or `advert_id/campaign/placement` identity, preview/correlation id, commit/readback status/evidence, warnings/override and sanitized error diagnostics. Funnel, stock and supply facts are not copied into events. The collapsed `История изменений` reads this table with filters and pagination. Failed/mismatched readbacks remain visible but do not update last-confirmed timestamps or metrics.

# 8. Metric contracts

- `seller_price_change_rub` / `Изменение нашей цены, ₽`: sum of all confirmed seller-price deltas per nmID and Asia/Yekaterinburg business date; no event means `null`, not zero.
- `advertising_bid_change_rub` / `Изменение рекламной ставки, ₽`: sum of all confirmed exact-placement bid deltas per nmID/day. The daily scalar is additive across placements; full detail remains in events. No event means `null`.
- `buyer_price_rub` / `Цена для покупателя, ₽`: factually observed public-card price from `spp_proxy`, preserving that source's measured-at/freshness/quality evidence. Missing observation stays `null`.

The two action metrics are available in the existing selector but default to collapsed. Runtime registry may contain operational metrics beyond the pilot display bundle; validation requires all display metrics to have runtime semantics but permits the documented runtime superset.

# 9. Out of scope

No AI/ML, recommendations, causal-effect estimation, automatic/bulk price or bid actions, real synthetic-order creation, promo participation changes, production backfill or automatic management are part of this MVP. The event shape permits later observation windows/control groups without rewriting accumulated events.
