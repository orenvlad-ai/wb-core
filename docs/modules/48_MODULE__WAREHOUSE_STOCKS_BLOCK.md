---
title: "Модуль: warehouse_stocks_block"
doc_id: "WB-CORE-MODULE-48-WAREHOUSE-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать единый read-only раздел `Остатки / Склады`, шесть виртуальных количественных складов и guarded cutover из шести системных документов `Ввод начальных остатков`."
scope: "Unified warehouse UI/API, immutable opening documents and lines with provenance, exact source watermarks, idempotent/atomic initialization and bounded rollback. Costs, future movements, sales writeoffs, economic projections and source-record mutation are out of scope."
source_basis:
  - "docs/modules/07_MODULE__STOCKS_BLOCK.md"
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
related_modules:
  - "packages/application/warehouse_stocks.py"
  - "packages/application/stocks_block.py"
  - "packages/adapters/stocks_block.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
  - "packages/adapters/templates/sheet_vitrina_v1_operator.html"
related_tables:
  - "sheet_vitrina_v1_warehouse_cutovers"
  - "sheet_vitrina_v1_warehouse_documents"
  - "sheet_vitrina_v1_warehouse_document_lines"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/warehouses"
  - "GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}"
related_runners:
  - "apps/warehouse_opening_snapshot.py"
  - "apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-opening-{dry-run,apply,readback,rollback} / warehouse-ui-flow"
  - "apps/warehouse_stocks_smoke.py"
  - "apps/warehouse_stocks_browser_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "The six opening balances are quantity-only immutable documents over primary source evidence. The FF top balance remains a live projection of the existing canonical FF ledger while its opening document stays frozen. Costs/capital are NULL, stored balances do not feed web-vitrina/Proxy 3/canonical cost engine, and apply is available only through an exact dry-run fingerprint plus coherent backup and one SQLite transaction."
---

# 1. Warehouse Contract

The module exposes exactly six warehouses, in stable order:

1. `production` — `На производстве`.
2. `china_to_ff` — `В пути: Китай → FF`.
3. `ff` — `Склад FF`.
4. `ff_to_wb` — `В пути: FF → WB`.
5. `wb` — `Склад WB`.
6. `wb_acceptance_discrepancy` — `Расхождения приёмки WB`.

`Китай → FF` is a virtual transit stage from the supplier factory to the single Moscow FF, not a separate Chinese fulfillment site.

The `Остатки / Склады` top-level tab uses one shared screen and one shared render path for every warehouse. The screen shows source/cutover time, unique non-zero SKU, quantity, nullable economics, per-SKU balances/provenance and a document registry. Costs and capital render as `—` and persist as SQL `NULL`; they are never coerced to zero or included in economic calculations.

Access is the existing `supply` section boundary: internal admin/operator/supply-operator users may read the API and UI; supplier-only users receive `403`. Payloads contain sanitized business provenance only, never secrets, runtime paths, source blobs or auth material.

# 2. Opening Source Rules

Every non-zero line has a canonical `nm_id`, display identity when available and `provenance.source_records[]`.

## 2.1. На производстве

Source is the supplier shipment/invoice header and full positive product-line composition. Every product line must carry an authoritative canonical match status from `MATCH_STATUSES_WITH_AUTHORITATIVE_NM_ID` (`matched`, `matched_by_compatibility`, `matched_by_barcode`); an unmatched/ambiguous line blocks the whole cutover even if a stale `internal_nm_id` is present. A shipment is included only when at least one CNY document of type `supplier_cny_payment` has status `posted`, both raw factual-date fields are empty, the existing canonical factual-date resolver derives `production`, persisted status is not inactive/cancelled and no historical accepted-without-date exception is active. Any set China-shipment date excludes the row from production; an invalid/future factual date that cannot form its claimed occurred boundary blocks the cutover instead of being silently reclassified. The amount of the first payment does not scale quantity: the entire invoice composition is included. Provenance carries shipment/invoice/line identity, derived-status source and the posted payment document identity, datetime, natural key and source-file hash.

## 2.2. В пути: Китай → FF

Source is the same supplier shipment identity and the same authoritative-match-only product-line composition. The existing canonical factual-date resolver must derive `in_transit` from an occurred valid `actual_shipment_date` while no occurred FF acceptance boundary exists. No new per-stage business identifier is created.

## 2.3. Склад FF

Source is the existing append-only FF ledger, exactly `SUM(quantity_delta)` by active, non-hidden nomenclature `nm_id`. The module reads the existing operation/line tables and does not create another FF calculation or modify FF operations. The legacy `GET .../supply/ff-stocks` route and manual receipt/writeoff operation UI remain compatible; the former user-facing current-balance screen transitions to the new `Склад FF` screen.

The FF opening document freezes the cutover composition like the other five documents, but the balance table and FF summary on every later read are projected again through `FfStockLedgerBlock.current_balance_rows()`. Sanitized contributing operation lines are attached as reconciled provenance; a concurrent mismatch fails the read instead of displaying a mixed snapshot. Later canonical FF operations therefore change the current balance without mutating the opening document. `negative_balance` and `Отрицательный остаток ФФ` remain visible on the unified screen.

## 2.4. В пути: FF → WB

Source is persisted official WB FBW Supplies goods composition. Ordinary, traceable supplies in status `3` (`Отгрузка разрешена`) and its proven later non-final physical stages `4` (`Идёт приёмка`) / `6` (`Отгружено на воротах`) contribute their positive `quantity`; this preserves stock after the shipment gate until final acceptance. Planned status `2`, final status `5` and `Допринято` do not. Each source record carries the WB supply/cache identity, exact status, raw goods row index/hash and source sync/enrichment times.

## 2.5. Склад WB

Source is a fresh call through the existing `StocksBlock` and official Seller Analytics warehouse-stock adapter for the enabled canonical `registry_upload_config_v2` nmIDs. The adapter records a UTC `fetched_at`; all current warehouse rows and their snapshot timestamp remain line provenance. Incomplete requested nmID coverage fails closed. Historical sales/movements are not reconstructed.

## 2.6. Расхождения приёмки WB

For every SKU, ordinary final status-`5` supplies contribute `quantity - acceptedQuantity`. Final `Допринято` rows reduce the aggregate buffer for the same SKU using positive `acceptedQuantity`; the existing canonical mechanism uses positive `quantity` when accepted quantity is missing or zero. The calculation does not require an invented original-supply link. A negative final result raises a diagnostic error containing sent/accepted/doprinato quantities; it is never silently clamped to zero.

# 3. Cutover And Persistence

The one supported cutover has stable id `warehouse_opening_v1`. It contains exactly six stable documents `whdoc_opening_v1_*`, numbered `ВНО-000001` through `ВНО-000006`, all with one `cutover_at`. A zero warehouse still gets its document with zero lines and zero quantity.

`sheet_vitrina_v1_warehouse_cutovers` stores the shared timestamp, exact source watermarks, stable plan fingerprint and sanitized apply/backup audit. `sheet_vitrina_v1_warehouse_documents` stores the posted quantity-only headers. `sheet_vitrina_v1_warehouse_document_lines` stores exact SKU composition and provenance. Each initial balance is the sum of its posted opening-document lines. There is no second mutable warehouse balance table; the later FF current-balance projection continues to use the pre-existing FF ledger.

The cutover plan contains:

- a coherent read-transaction digest over supplier shipments/lines, posted CNY payments, FF operations/lines, WB supply cache/sync state and nomenclature;
- exact max timestamps/row counts for local sources;
- the official WB snapshot date, requested/covered nmID counts, `fetched_at` and payload digest;
- all six document headers and lines;
- a canonical `sha256:` plan fingerprint.

Apply revalidates the exact fingerprint, re-reads and compares the local source digest, creates an integrity-checked coherent SQLite backup, then inserts the cutover, six headers and all lines under one `BEGIN IMMEDIATE` transaction. Any error rolls the transaction back. A repeated apply with the same stored fingerprint is zero-change idempotent; a different fingerprint fails closed. The rollback command requires the exact stored fingerprint, makes another coherent backup and deletes only this new cutover through FK cascade.

Source invoice/payment/shipment/FF/WB/nomenclature rows are read-only throughout this contour.

# 4. Canonical Production Runner

After the release commit is deployed, use only the hosted runner against the checked-in active EU target:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-opening-dry-run --output /absolute/local/warehouse-opening-plan.json

python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-opening-apply \
  --plan-file /absolute/local/warehouse-opening-plan.json \
  --fingerprint 'sha256:...'

python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-opening-readback

python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-ui-flow --evidence-dir /absolute/outside-repo/warehouse-ui-evidence
```

The wrapper pins the current active target, app dir, runtime dir and backup dir. It loads hosted secrets only into the remote process environment for the WB API call and never prints them. The UI command builds a short-lived signed owner cookie without logging it, opens a fresh local Playwright context and reconciles all six visible warehouse totals/rows with their protected detail API, every opening document with production readback and the current FF rows with the legacy canonical FF API. It verifies expanded provenance, NULL-cost dashes, the old FF transition and absence of unexpected `5xx`, `pageerror` and console errors; screenshots and its sanitized report must stay outside Git. Direct production SQL, arbitrary SSH mutation, server-only scripts and bypassing the fingerprint/backup gates are not valid paths.

Rollback is recovery-only:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  warehouse-opening-rollback --fingerprint 'sha256:...'
```

# 5. Read/API Invariants

`GET /v1/sheet-vitrina-v1/warehouses` returns six summaries even before initialization. `GET .../warehouses/{warehouse_key}` returns the one stored opening document and balance rows with line provenance. For five warehouses those balance rows equal the frozen opening lines in this stage. For `ff`, after initialization they are the current canonical ledger projection and can diverge from the immutable opening document after later FF operations. Unknown keys return controlled `404`; malformed keys return controlled `400`.

For every stored document:

- `sku_count == len(lines)`;
- `total_quantity == SUM(lines.quantity)` using exact decimal semantics;
- document/line cost and capital are `NULL`;
- status is `quantity_fixed_cost_unset` / `Количество зафиксировано, стоимость не задана`;
- document and every line share the same cutover through the document FK;
- document id/number and per-document `nm_id` are unique.

# 6. Explicit Non-Effects

These tables are not referenced by web-vitrina materialization, canonical cost engine, `our_wb_unit_cost_rub`, Proxy 3, Finance/P&L, SKU management, factory-order or regional recommendation calculations. The module does not implement future receipts/issues/transfers, WB sales depletion, returns/writeoffs, FF inventory, historical movement backfill, costs, fees, logistics/customs allocation or late-expense revaluation.

# 7. Verification

Targeted checks:

- `python3 apps/warehouse_stocks_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_supplier_auth_smoke.py`
- `python3 apps/warehouse_stocks_browser_smoke.py`

The first covers all six source rules, full-invoice activation by first payment, exclusions, WB status gates, per-SKU доприёмка, negative discrepancy rejection, FF cutover parity, post-cutover live FF projection with immutable opening lines and negative warning, NULL economics, zero-warehouse document, source-drift rejection, idempotency, injected partial failure rollback, readback and bounded rollback. The auth/API smoke covers authorized and forbidden projections plus old/new FF routes. The Playwright smoke opens every warehouse, expands opening lines, checks dashes and validates the old FF UI transition with no warehouse-page `pageerror` or console error.
