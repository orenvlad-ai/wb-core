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
  - "53_MODULE__SKU_INVENTORY_BALANCE.md"
related_tables:
  - "registry_upload_config_v2"
  - "sheet_vitrina_v1_user_configs"
  - "sheet_vitrina_v1_sku_action_events"
  - "sheet_vitrina_v1_wb_incident_policy_revisions"
  - "sheet_vitrina_v1_wb_incident_projection_cache"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/sku-management"
  - "GET /v1/sheet-vitrina-v1/sku-management/sku/{nm_id}"
  - "GET|POST /v1/sheet-vitrina-v1/sku-management/settings"
  - "POST /v1/sheet-vitrina-v1/sku-management/price/preview"
  - "POST /v1/sheet-vitrina-v1/sku-management/price/commit"
  - "POST /v1/sheet-vitrina-v1/sku-management/bid/preview"
  - "POST /v1/sheet-vitrina-v1/sku-management/bid/commit"
  - "GET /v1/sheet-vitrina-v1/sku-management/history"
  - "GET|POST /v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-settings"
  - "GET /v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-options"
related_runners:
  - "apps/sku_management_smoke.py"
  - "apps/sku_management_browser_smoke.py"
  - "apps/wb_warehouse_exclusion_browser_smoke.py"
  - "apps/sku_management_metrics_smoke.py"
  - "apps/wb_incident_policy_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Existing surface is preserved as subtab `Общее`; sibling `Баланс запасов` is owned by module 53 and does not change this block's calculation/write semantics."
---

# 1. Identity, authorization and truth

- `module_id`: `sku_management_block`.
- Unified tab: `Управление SKU` inside `/sheet-vitrina-v1/vitrina`; this module owns its subtab `Общее`. Sibling `Баланс запасов` is specified by module 53.
- Authorization section: `sku_management`; it uses the existing `allowed_sections` model and WebCore session. There is no parallel user system.
- Row universe: enabled rows of canonical `registry_upload_config_v2`; nomenclature only enriches display identity.
- Forecast and table are read/calculation projections. They do not create orders, supplies, stock operations or accepted business truth.

Settings and table preferences use `sheet_vitrina_v1_user_configs` under the `sku_management` config key with optimistic revision checks. `localStorage` is not a source of truth. Server defaults are 14 sales days, 90 forecast days, 30-day future order cadence, 30-day production, 30-day factory-to-FF, 7-day FF-to-WB, 14 safety-stock days and three-day price/bid stabilization. Sales period is one of `7/14/30/60`; all other bounds are validated server-side. Zero stabilization disables that warning.

The warehouse incident policy is seller/account-level and edited only in `Остатки → Склад WB → Инциденты на складах WB`. Supply and SKU Management display the same policy read-only. Contract v2 owns an immutable `effective_from` per positive stable warehouse ID; a revision may mix old `2026-07-25` entries and a newly selected/current-date entry without rewriting history. Removal closes the old interval, later re-selection opens a new one, and ID `0` is never an operational destination. Legacy global-date/list rows are projected into the same canonical entry model until one explicit Apply; conflicting legacy sets fail closed. Browser/localStorage copies never own calculation truth. Explicit Apply invokes one bounded derived Web Vitrina rematerialization from the earliest changed per-warehouse date inside the available 14-day ready window and returns its exact status/count/fingerprint/readback; this side effect does not relax the SKU Management projection, change physical WB quantity/WAC/capital or refetch historical sources. Exact repeated policy content is T0.

# 2. Forecast semantics

The forecast consumes existing contours only: active registry mapping, `stocks_block`, `ff_stock_ledger`, availability-adjusted sales history, registered supplier shipments/factory-order evidence, registered WB supplies and existing regional-demand/allocation results.

The additive `build_inventory_balance_evidence` read model exposes explicit `stock_wb`/`stock_ff`, recalculates availability-adjusted demand for the Balance-owned 7/14/30/60-day setting, and emits only supplier-registry `production`/`in_transit` milestones with empirical completed-shipment ETA evidence. It does not change the existing `Общее` forecast or write behavior; module 53 owns the all-fronts opening and pacing semantics.

For each SKU the engine forms a dated inbound stream, deduplicated by `source + source_id + date + district`; repeated goods lines inside one WB supply are aggregated by nmID before that identity is emitted. Current WB stock is saleable immediately. Current FF stock becomes a WB inbound only after the configured FF-to-WB lead; quantities reserved by a registered WB supply are removed from that generic FF transfer. A WB supply forecasts only `planned goods composition − max(factual ready/accepted/added quantity)`, because the progressed part is already covered by current WB stock. A supply without an FF-ledger debit reserves its full planned composition in the initial FF pool while adding only that remaining quantity; a supply with an idempotent full ledger debit returns only the remaining quantity as a dated WB inbound. Invalid and overdue transfers are excluded before they can reserve FF. This prevents accepted partial units from existing in WB stock, FF stock and future inbound simultaneously. Supplier registry rows use only `production`/`in_transit`, positive product quantities and exact `matched_by_barcode` nmID evidence; legacy `matched`/`matched_by_compatibility` rows remain readable, while extras, manual overrides, unmatched/ambiguous and accepted-FF rows are excluded. Repeated eligible SKU lines are aggregated inside one invoice. The supplier arrival date prefers actual shipment date to planned shipment date and adds the same configured factory-to-FF plus FF-to-WB leads used by the forecast timeline. Manual factory-order evidence is reused only when it is not the supplier-registry projection, preventing the same goods from entering two contours. Missing dates, overdue plans, partial quantities, duplicate identity and insufficient FF reservations remain explicit warnings instead of optimistic arrivals. Each day adds that day's valid inbound, consumes the selected availability-adjusted daily demand and measures stock against `daily demand × safety-stock days`.

After the last registered inbound plan, the model may add synthetic future factory orders at the configured cadence. Arrival is order date plus production, factory-to-FF and FF-to-WB lead times. Quantity uses the existing replenishment principle—demand over replenishment cadence plus safety requirement, rounded to a configured batch. Synthetic rows exist only in the response timeline; they are never persisted as factory orders, supplier shipments or WB supplies.

Output contains risk (`low/medium/high/unknown`), first deficit date, minimum projected stock including negative values, deficit units, norm coverage percent, first risky district when fresh district stock and demand are both available, compact reason and explicit quality/warnings. Authoritative zero demand remains zero rather than `unknown`. Missing WB or FF stock/demand makes the overall forecast `unknown`; absent or stale regional evidence produces district `unknown`. Missing district fields are never converted to zero. Default sort is highest risk, then nearest deficit.

Before total-WB and district forecast fields are formed, the engine consumes `build_incident_stock_projection`. It does not perform a second subtraction. Only physical warehouse quantity is removed from the operational opening; un-attributed in-way fields are untouched. Risk, deficit date, coverage, deficit units and regional warning state therefore share the same policy revision and effective snapshot as Supply and Vitrina. Any active non-empty policy requires complete official warehouse evidence and digest; incomplete evidence fails closed to unknown stock/forecast.

# 3. Commercial table and presentation state

The read model classifies metrics by time semantics:

- cumulative/unstable-during-day facts use the exact Asia/Yekaterinburg business date `D-2`: funnel (`view_count`, `openCount`, `cartCount`, `addToCartConversion`, `cartToOrderConversion`), orders and sales (`orderCount`, `orderSum`), advertising performance (`ads_drr`, `ads_drr_attributed`, `ads_views`, `ads_clicks`, `ads_atbs`, `ads_orders`, `ads_sum`, `ads_sum_price`, `ads_cpc`, `ads_ctr`, `ads_cr`) and profit/margin (`proxy_profit_3_rub`, `proxy_profit_rub`, `proxy_margin_3_pct`, `proxy_margin_pct`). The runtime loads a ready snapshot that contains that exact column and reads only its exact index. A blank value or absent exact-date snapshot stays blank; no newer/older-day fallback and no cross-date mixing are allowed;
- snapshot/current facts—seller and factual buyer price, SPP, promotion participation/count, campaigns and exact-placement bids—use the newest successful non-empty observation of their own source. An empty/failed refresh cannot replace the prior confirmed fact. Every response field carries exact captured/update time when available, otherwise its exact observation date.

Promo participation uses canonical per-SKU `promo_participation`; the SKU count uses `promo_count_by_price`, while the Prices label retains its documented `eligible / total current promos` global-denominator context. A global current-promo count is never interpreted as participation of every SKU. Campaign identity/current bids are loaded through one current reverse placement index, without per-SKU minimum/recommendation request fanout; exact minimum is fetched at preview and again at commit. Buyer price chooses the freshest factual public-card observation among the confirmed event readback and accepted `spp_proxy` temporal snapshots; exact capture time lets a later same-day refresh supersede an older event, while the immediate event remains fresher than a not-yet-refreshed projection. Quality/freshness are exposed.

The only table filter is case-insensitive SKU/nmID/name search. Risk, promo, coverage and deficit controls are retired; schema-v2 preference normalization drops them so old server-owned state cannot reactivate invisible filters. Header sorting remains three-state. Column visibility, width and order remain independent server-owned preferences; the selector exposes checkbox visibility plus drag-and-drop order with keyboard arrow fallback, and optimistic revision conflicts reload current canonical preferences with a controlled message. `product` is mandatory and normalization always pins it first; its visibility, drag handle and arrow moves are disabled. Other saved preferences remain compatible. The presentation column `Проблемный округ` is removed from defaults, sorting, selector and normalized preferences, while regional evidence remains in the forecast response for diagnostics and other consumers.

`Ближ. поставка` is a compact projection of the exact supplier inbound evidence admitted to the forecast. It selects the earliest non-overdue calculated arrival for the SKU, then ties by case-folded invoice number and shipment id. The cell shows invoice number, calculated arrival date and the quantity of this SKU aggregated within the invoice; missing or non-deterministic evidence stays `—`. Sorting uses calculated arrival date with empty values always last.

The first header/body column is independently sticky on the left as well as under the sticky header, with opaque backgrounds, explicit z-index, separator/shadow and hover background. Its header is `Название / nmID`; the bold primary line is the product name, the muted secondary line is `nmID`, internal SKU is not rendered, and the full long name remains accessible through `title`. Cumulative headers include `за DD.MM`; snapshot cells include `обновлено DD.MM[ время]`. A read-only line shows `Учитывается политика инцидентов · Не участвуют: …` only when the revision is effective, otherwise operational stock equals fact. The rest of the table retains restrained row/column separators, numeric alignment, row hover, truncation and horizontal scrolling; history is visually separated from the grid.

Loading, empty, partial evidence, stale and validation states are explicit. Price and bid modals use the state machine `preview_loading → preview_ready → commit_running → readback_pending → success|controlled_error`. The common operator modal card is fully opaque on the dark theme and sits above sticky headers inside a translucent dimming backdrop; Ads, Prices and SKU flows share this styling without changing drawers/selectors. `success` is entered only for `status=success`, `readback_status=matching` and a confirmed value. Price success shows product name, nmID and old/requested/confirmed values. Bid success additionally shows the exact advert id, campaign and placement. It remains open until an explicit safe close; mismatches and upstream errors never render green success.

The main Vitrina SKU separator is clickable for users who have the same `sku_management` section. The browser immediately opens the modal with the already-rendered product name and nmID, then calls narrow `GET /v1/sheet-vitrina-v1/sku-management/sku/{nm_id}` for remote mutation facts. This route is a dedicated quick read model rather than a filtered full-table build: it performs one exact goods read, one current campaign/placement index read, local temporal price/promo/buyer projections and filtered audit history. It never invokes `build_table`, stocks, sales forecast, supplier/WB-supply collection, Ads fullstats, per-placement minimum/recommendation fanout or all-active-SKU price reads. Response `meta.diagnostics` records sanitized phase milliseconds, total milliseconds, bounded remote-call counts, read scope and intentionally skipped paths.

The opaque quick modal exposes seller price and an exact required `advert_id + placement` selector for bids, then delegates to the existing guarded preview/commit/readback state machine. TOTAL is never clickable. Input/select focus, fill and click cannot dismiss or reset it. Backdrop clicks never close this mutation dialog. Keyboard focus stays trapped even if a backdrop interaction temporarily moves the active element, and an explicit safe close returns focus to the opener. `Закрыть`/`Отмена` and Escape work only outside commit/readback; while a commit is running they cannot cancel the in-flight operation. A matching success patches only confirmed readback state; it never rewrites historical Vitrina cells.

After closing confirmed success, the browser patches only the target row cells from the backend readback: seller price, confirmed price timestamp and factual observed buyer price when supplied, or the exact `advert_id + placement` bid and its timestamp. Search, sort, column configuration and table scroll remain intact; non-target rows are not rebuilt and the SKU table endpoint is not refetched. An already-open history block may refresh independently. Controlled errors restore the unchanged target display and do not apply requested values.

# 4. Seller-price write flow

The operator edits desired seller price. Preview uses the canonical Prices Management adapter and deterministically searches an integer original-price/discount pair whose WB seller price equals the target. One exact current-goods payload is reused by target-combination validation, guarded Prices preview and local promo enrichment; duplicate current-price reads are forbidden by the regression contract. It returns product name/nmID, current/new original price, discount, seller price, the latest accepted temporal factual buyer price when available, promo/quarantine/stale and stabilization warnings. It never starts a public-card network fetch and never substitutes an estimated buyer price for absent public evidence. Sanitized phase timings and remote-call counts are returned with the preview.

Commit requires one unexpired preview id, explicit confirmation, exact nmID and explicit warning override for stabilization, active-promo or unavailable-promo evidence. Quarantine evidence is fail-closed and a current quarantine blocks preview and commit. The backend rechecks quarantine, promo evidence and the current WB original-price/discount/seller-price tuple before upload; the one current tuple payload is reused for the promo recheck, and a promo change after preview requires a new preview. It submits one canonical Prices API upload task and interleaves upload-status and exact-tuple reads with early bounded cadence, maximum attempts and a wall-clock deadline instead of two sequential fixed-sleep loops. Success exists only when final upload status is successful and original price, integer discount and seller price all match the requested tuple. A seller-price-only match with another tuple is a controlled failed event, never optimistic success. Optional public buyer-price enrichment is explicitly deferred and never delays an already confirmed seller-price success; later normal temporal refresh may provide that fact, and missing evidence is never replaced by a calculation.

# 5. Advertising-bid write flow

Bid identity is always `nm_id + advert_id + placement`. A single placement can be edited directly; multiple campaigns/placements require exact selection. Preview delegates campaign membership, current/min bid, freshness and safety validation to Ads Operator and adds stabilization warnings.

Commit accepts one preview once, rechecks current bid, fetches the current WB minimum again, reapplies absolute/relative safety thresholds and submits one WB Promotion API operation. Readback uses bounded early-cadence/deadline polling of only the exact `nm_id + advert_id + placement` tuple through the advert-detail source; it does not rebuild expanded SKU Ads detail, fullstats, minimums or recommendations. Success is stored only when the selected placement returns the requested bid and includes product name/nmID plus exact advert/campaign/placement identity. Unavailable or increased minimum fails before PATCH. Aggregate/max bid is never used as mutation identity.

# 6. Write gates and safety

The dedicated `sku_management` price and ad blocks are enabled in normal runtime construction and do not depend on disabled-by-default `WB_PRICES_WRITE_ENABLED` or `SHEET_VITRINA_ADS_WRITE_ENABLED`; those legacy flags still govern their original standalone surfaces. This section remains guarded by section authorization, one exact target, server validation and short-lived preview, explicit confirmation, stale/current/min/quarantine checks, backend-only WB calls, a single-use preview claim, sanitized audit and post-write readback. No frontend call targets a WB host or issues a WB write directly.

# 7. Stabilization and event history

The authoritative window starts at confirmed WB readback time. Same-parameter changes report elapsed and remaining days. With cross warnings enabled, a price change warns a bid edit and vice versa. Warnings are advisory: the operator may cancel or explicitly override; override requires no text reason but is stored automatically.

`sheet_vitrina_v1_sku_action_events` is the single server-owned event/audit contour. It stores nmID, parameter, old/requested/confirmed value, delta, exact timestamps, username/source, price or `advert_id/campaign/placement` identity, preview/correlation id, commit/readback status/evidence, warnings/override and sanitized error diagnostics. Timings contain phase durations/call counts and no credentials, tokens or request bodies. Funnel, stock and supply facts are not copied into events. The collapsed `История изменений` reads this table with filters and pagination. Failed/mismatched readbacks remain visible but do not update last-confirmed timestamps or metrics.

# 8. Metric contracts

The `GET /sku-management` response additionally exposes `meta.metric_policy` with business timezone/date, `cumulative_exact_date`, no-fallback flag, complete cumulative field classification and latest-successful snapshot field classification. This metadata is the UI's authoritative source for `за DD.MM` headers and is covered by exact-date/no-fallback tests.

- `seller_price_change_rub` / `Изменение нашей цены, ₽`: sum of all confirmed seller-price deltas per nmID and Asia/Yekaterinburg business date; no event means `null`, not zero.
- `advertising_bid_change_rub` / `Изменение рекламной ставки, ₽`: sum of all confirmed exact-placement bid deltas per nmID/day. The daily scalar is additive across placements; full detail remains in events. No event means `null`.
- `buyer_price_rub` / `Цена для покупателя, ₽`: factually observed public-card price from `spp_proxy`, preserving that source's measured-at/freshness/quality evidence. Missing observation stays `null`.

The two action metrics are available in the existing selector but default to collapsed. Runtime registry may contain operational metrics beyond the pilot display bundle; validation requires all display metrics to have runtime semantics but permits the documented runtime superset.
The web-vitrina read contract extends its metric metadata through the same SKU-action registry extension used by live materialization, so Russian labels, sections and formats remain authoritative after snapshot normalization instead of falling back to raw metric keys.

# 9. Out of scope

No AI/ML, recommendations, causal-effect estimation, automatic/bulk price or bid actions, real synthetic-order creation, promo participation changes, production backfill or automatic management are part of this MVP. The event shape permits later observation windows/control groups without rewriting accumulated events.
