---
title: "Модуль: строгий read-only source acquisition для реестра изменений"
doc_id: "WB-CORE-MODULE-55-CHANGE-REGISTRY-SOURCE-ACQUISITION"
doc_type: "module"
status: "active_internal_source"
purpose: "Зафиксировать детерминированное совместное чтение seller-controlled Prices и Promotion Ads для будущего baseline engine без persistence или activation."
scope: "Один seller/account; exhaustive Prices GET pagination; Promotion count manifest и detail batches; typed scalar/identity evidence; canonical sanitized manifests and digests."
source_basis:
  - "packages/application/change_registry_source_acquisition.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/adapters/wb_promotion.py"
  - "apps/change_registry_source_acquisition_smoke.py"
  - "Official WB Prices and Discounts API: https://dev.wildberries.ru/openapi/work-with-products"
  - "Official WB Promotion API: https://dev.wildberries.ru/openapi/promotion"
  - "Official WB API rate-limit guidance: https://dev.wildberries.ru/openapi/api-information"
related_modules:
  - "37_MODULE__SHEET_VITRINA_V1_ADS_OPERATOR_BLOCK.md"
  - "41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "46_MODULE__SKU_MANAGEMENT_BLOCK.md"
  - "53_MODULE__SKU_INVENTORY_BALANCE.md"
  - "54_MODULE__CHANGE_REGISTRY_FOUNDATION.md"
related_runners:
  - "apps/change_registry_source_acquisition_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Strict acquisition remains persistence-free and read-only; its exact campaign state/payment/placement evidence is also the canonical readback vocabulary used by the separate module-58 writer."
---

# 1. Scope and activation boundary

`ChangeRegistrySourceAcquirer` produces one joint deterministic result for one
explicit `seller_id + account_scope`. The result is `complete` only when both
the Prices half and the Ads half are independently complete. A complete empty
seller source remains distinct from a request failure or a partial source.

The module is internal and persistence-free. It has no scheduler, refresh
integration, HTTP/UI route, database repository, storage initializer or writer
hook of its own; the active scheduled consumer is module 57 through the
canonical module-56 baseline engine. It
does not call Prices upload/status/quarantine methods, Ads minimum/
recommendation/statistics/PATCH methods or any other WB write. It never inserts
registry operations/items/facts/checkpoints/observation values or identity
incidents. Existing Prices/Ads/SKU writer and audit behavior is unchanged.

# 2. Prices completeness and size semantics

Prices uses only exhaustive
`GET /api/v2/list/goods/filter?limit=1000&offset=...` for seller completeness.
Offsets advance by 1000 and acquisition continues until WB returns an explicit
empty `listGoods` array. A short non-empty page is not terminal evidence.
Targeted `POST /api/v2/list/goods/filter` remains available to existing
operator flows but is not used as completeness proof.

Every source size tuple is preserved with its `sizeID`, `techSizeName`,
original price, seller discounted price and WB Club price. Money becomes exact
integer minor units; discount becomes integer basis points. A SKU-level
`original_price_minor + discount_bps + seller_price_minor` tuple exists only
when every source size has the same tuple. Otherwise representation is
explicitly `size_level` and the SKU tuple is `inapplicable`; no minimum,
maximum or first-size collapse is allowed.

Missing field, explicit JSON `null`, integer zero, inapplicable and invalid/
error evidence retain different typed forms. Zero money/discount remains
`exact_zero`; truthiness fallback is forbidden.

# 3. Ads manifest, detail and legacy evidence

Ads first reads `GET /adv/v1/promotion/count` as the seller campaign manifest.
Each group preserves type/status/count, sorted advert IDs, `changeTime` when
present and a response digest. Group count, top-level `all`, unique IDs and
observed manifest cardinality must agree.

All current supported IDs are requested from
`GET /api/advert/v2/adverts` in sorted batches of at most 50. Each batch binds
the exact requested and observed ID sets and response digest. Missing, extra or
duplicate detail IDs make Ads partial; an empty or malformed detail response
never becomes a complete empty source.

Legacy count-only `type=6/status=7` IDs are not discarded and are not sent to
the current detail method. They retain explicit completed count evidence while
detail, payment, bid and nmID mapping are `inapplicable`, not `missing` and not
an optimistic supported target.

# 4. Campaign identity, payment and bid fields

Current detail preserves:

- count `changeTime` and detail `timestamps.created` where the sources permit;
- raw status and canonical campaign state;
- payment model `cpm|cpc` and unit
  `per_thousand_impressions|per_click`;
- each exact `nm_id + advert_id + placement` bid in integer minor units,
  including literal zero;
- source/detail/record/target evidence digests without raw payloads.

Every aware acquisition timestamp crosses one boundary before any response,
record, source or joint digest: it is rendered as the equivalent UTC instant
with a terminal `Z`. This applies to joint/source intervals, count
`changeTime`, detail `timestamps.created` and timestamp-bearing sanitized
evidence. Therefore `Z`, `+00:00` and any other explicit offset for the same
instant produce identical canonical bytes and identities. The default
`datetime.now(timezone.utc)` path also emits `Z`, never `+00:00`.

A campaign target is actionable only when one `advert_id` resolves to exactly
one unique `nmID`. Cardinality zero or many emits a deterministic immutable-
shaped `campaign_nm_mapping_cardinality` incident candidate with sorted unique
nmIDs, stable id/digest and `persistence_status=not_persisted`. The campaign
and affected bids remain non-actionable. Block A never writes that candidate
to `change_registry_identity_incidents`.

# 5. Rate limits, retries and sanitized evidence

The source contract applies the accepted conservative limits:

- Prices: 10 requests per 6 seconds, minimum 600 ms interval, burst 5;
- Promotion detail: 20 requests per minute, minimum 3 seconds interval,
  burst 5, maximum 50 IDs per request.

HTTP 429 honors bounded non-negative `Retry-After` and official
`X-Ratelimit-Retry`/compatible rate headers. The default budget is two retries
after the initial request. Exhaustion is typed partial evidence; it is never an
empty page, empty campaign set or successful completion. Other HTTP/transport
errors fail the affected half closed without retrying as rate limits.

Manifests contain only method, sanitized path, counts, offsets/IDs, interval,
completeness, bounded error class/status/retry evidence and SHA-256 digests.
The joint safety seam reports both zero registry persistence and zero WB
`POST`/`PATCH` calls; module 56 validates both before ingest.
They contain no token, Authorization header, raw request/response body, cookie
or credential-shaped value. Canonical JSON is sorted, compact UTF-8; all
manifest/record/incident digests exclude randomness.

# 6. Verification and exclusions

`python3 apps/change_registry_source_acquisition_smoke.py` covers exhaustive
Prices pagination, uniform/nonuniform size tuples, exact zero/null/missing,
Ads count/detail complete and partial results, batches of 50, legacy
inapplicable evidence, exact-one/zero/many mapping, deterministic incidents,
explicit payment units, `Retry-After`, bounded exhaustion, joint completeness,
zero writer calls, zero persistence counts, UTC-offset digest equivalence and a
production-shaped `92 Prices / 189 Ads manifest / 179 details / 10 legacy /
537 bids` fixture.

Excluded: baseline diff engine, historical import/backfill, registry rows,
checkpoints, facts, observer/scheduler/lease/job, public API/UI, writer
instrumentation, manual pending, Balance bridge, analytics/recommendations,
campaign/price/bid mutation and any production data write.

The active Balance state writer does not route through this acquirer and does
not relax its zero-POST/PATCH seam. It uses the same normalized official
campaign-state vocabulary only through module 37/53, then module 58 records the
separate exact writer/readback proof.

The separately bounded successor is
`56_MODULE__CHANGE_REGISTRY_BASELINE_ENGINE.md`. It consumes this module's
sanitized manifest only after an explicit internal call; acquisition itself
still performs zero persistence and does not activate that consumer.
