---
title: "Модуль: единая инструментация внутренних seller writers"
doc_id: "WB-CORE-MODULE-58-CHANGE-REGISTRY-INTERNAL-WRITERS"
doc_type: "module"
status: "active"
purpose: "Гарантированно фиксировать будущие внутренние price/bid interventions до WB submit и завершать их только доказанным lifecycle."
scope: "Standalone Prices/Ads, SKU Management price/bid и каждый SPP measurement/restore transition; один canonical seller/account; immutable StoreRegistry operational storage."
source_basis:
  - "docs/modules/54_MODULE__CHANGE_REGISTRY_FOUNDATION.md"
  - "docs/modules/56_MODULE__CHANGE_REGISTRY_BASELINE_ENGINE.md"
  - "packages/application/change_registry_writer.py"
  - "packages/application/registry_upload_http_entrypoint.py"
related_runners:
  - "apps/change_registry_internal_writers_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Future-write activation only; no import, backfill, new write capability or deploy-time WB mutation."
---

# 1. Runtime binding and surfaces

`RegistryUploadHttpEntrypoint` creates one `InternalWriterRegistry` and passes
that same seam to five source surfaces: `prices_upload`,
`sku_management_price`, `spp_tester`, `ads_bid_change` and
`sku_management_bid`. The binding is the supported
`SELLER_PORTAL_CANONICAL_SUPPLIER_ID`; `account_scope` is the repo-owned fixed
`seller-portal-primary`. Production startup fails closed if this binding
is absent. No multi-account selector or UI is introduced.

# 2. Before-submit contract

Preview, read-only and guard rejection paths never call the seam and create no
registry rows. Immediately before the first and only WB submit, the seam uses
one `BEGIN IMMEDIATE` to append the immutable operation, all exact atomic items
and a `created` attempt for every item. Storage/configuration failure stops the
WB call.

Price operations always carry the full tuple
`original_price_minor + discount_bps + seller_price_minor` with exact before
and requested values. The provenance annotation separately records which
fields were explicitly supplied, preserving omitted-vs-explicit semantics.
Bid identity is seller/account + nmID + advert_id + placement + `bid_minor`;
integer zero is valid.

# 3. Submit, proof and ambiguity

A single returned WB submit appends `submitted` with only sanitized receipt
identity/digest. Exact matching WB readback then appends `confirmed` and one
`wb_readback` fact for each changed canonical field. Unchanged tuple members
remain atomic items but do not become transition facts.

An explicit WB rejection before acceptance terminates the created attempt as
`rejected`/`failed`. Transport uncertainty or nonmatching/unverifiable readback
is `ambiguous`; a later query-only exact readback resolves the same attempt and
fact. There is no blind submit retry. In particular SPP performs exactly one
submit per measurement or restore bridge/final stage, with stable job/stage
correlation and a separate operation for every transition.

Existing Prices/Ads JSONL, SPP job audit and SKU action history remain native
evidence. Registry rows store only sanitized actor/surface/native IDs and links,
never credentials, raw WB payloads or historical imports.

# 4. Observer reconciliation

Writer readback and checkpoint diff for the same exact target/field,
before/after transition reconcile to one fact when the writer transition start
belongs to exactly one checkpoint interval. The later proof adds its own link.
Zero or multiple candidates do not coalesce. Equal values in unrelated
intervals remain separate facts.

# 5. Excluded scope

Excluded: historical import/backfill, public registry API/UI, new WB write
routes, campaign writers, Balance instrumentation, automatic WB probes,
deployment-time writes and production-mutation manifests. Balance dry-run
continues to report `wb_patch_called=false` and creates zero registry rows.
