# Migration 134: FF facility × pool business documents

## Goal and rollout boundary

This Stage 2 change adds the durable backend contract for immutable business
documents inside the Stage 1 facility × `FBS|FBO` detail. Deployment creates
only additive empty schema. The service is default-off and is not imported by
public HTTP routes, operator UI or any current aggregate FF producer/consumer.
No facility seed, feature epoch, opening, backfill or production business-data
apply is part of this migration.

The existing six warehouse stages remain unchanged. Facility and pool are
dimensions inside `ff`; their quantity/capital is explanatory detail and is
never added to stage or TOTAL values a second time. Stage 1 operations and
movement lines remain the only dimensional movement ledger. Warehouse-domain
T2 checkpoints include the facility registry together with the pool tables;
workflow events remain operational audit rather than restored business state.

## Durable documents and workflow

`packages/application/ff_pool_documents.py` adds one application/domain
service and the following operational objects:

- `sheet_vitrina_v1_ff_pool_document_requests` and request aliases persist
  exact source identity/revision/idempotency epoch, actor, UTC audit time,
  Asia/Yekaterinburg business date, source/file SHA, accepted source bytes,
  parsed manifest, blockers and immutable posted manifest;
- `sheet_vitrina_v1_ff_pool_documents`, document lines and positive immutable
  RUB expense lines are append-only roots/evidence above Stage 1 movements;
- `sheet_vitrina_v1_ff_pool_document_relations` supports forward-only typed
  shipment, receipt, loss, discrepancy, cancellation, inventory child,
  correction, storno and late-expense edges with allowlists, uniqueness,
  chronology and recursive cycle protection;
- existing `sheet_vitrina_v1_ff_workflow_events` records
  `accepted/processing/blocked/ready/posted/replay/complete/error` transitions.
- guided China acceptance additionally records one immutable replay row with
  accepted quantities, the materialized supplier-cost layer and the exact
  money-normalization manifest. Its append-only recovery table binds at most
  one compensating storno to that replay; neither table is a second movement
  ledger.

Legacy documents are not related or backfilled. Correction, storno and late
expense edges are also mirrored into the narrower Stage 1 operation relation
table. All other child types use the new document graph so Stage 1 schema is
not rewritten.

Exact semantic repeat is T0. A new posting uses recovery policy
`ff_pool_document_posting` at T1 with bounded request/balance before-images and
an undo manifest that also enumerates every inserted immutable operation,
movement, document, line, expense and relation. The append-only guards make a
generic row-deletion rollback fail atomically before any balance/request
before-image can be restored; business reversal is a related storno/correction,
never a partial physical delete. It never takes a full-store backup or
integrity scan. A
process restart returns an interrupted `processing` request to `accepted`, and
`posted/replay` resumes by immutable readback. Exact retry, concurrent duplicate
and response loss cannot create a second operation or movement; one runtime-
local posting lock serializes recovery preparation and the short SQLite writer
closure. Request acceptance also serializes the client alias and binds each
external source revision/epoch to one semantic manifest across all document
kinds; an audit filename is not part of that business identity.

## Accounting contracts

All quantities are SQLite `INTEGER`. Capital and WAC remain minor-unit integer
or canonical Decimal TEXT; no new accounting field uses `REAL`.

- A future opening decomposes an externally supplied aggregate FF snapshot
  into facility/pool lines only when exact per-SKU quantity/capital parity
  holds and the whole detail contour is empty. It does not reuse
  `warehouse_opening_v1` or change aggregate FF.
- A China acceptance allocation selects one facility and exact FBS/FBO line
  quantities. Common expenses are distributed deterministically across the
  whole accepted quantity, with exact nmId/barcode identity and no fuzzy match.
  Supplier capital may contain fractional kopecks. The document rounds the
  exact aggregate header once with `ROUND_HALF_UP`, floors every per-SKU share
  to kopecks and assigns the remaining kopecks by largest fractional remainder,
  then `nmId`. It persists the exact header, canonical header, total/per-SKU
  residual and residual owners. Independent per-SKU rounding is forbidden.
  The resulting per-SKU kopecks are split between FBO/FBS by accepted quantity;
  positive quantity may not become synthetic zero. Aggregate, SKU detail and
  pool totals therefore conserve the same canonical header exactly. An
  accepted inbound SKU may be absent from the current aggregate `ff` snapshot;
  that absence is frozen as `row_present=false` semantic zero, not rejected as
  `aggregate_sku_missing`. Confirm materializes its first positive aggregate
  row with the same quantity/capital and full cost coverage. Existing rows add
  the accepted quantity to `cost_covered_quantity` together with physical
  quantity and capital. A guided preview is ready only when the complete active
  aggregate `ff` revision exactly equals all current facility/pool detail, not
  merely the request's affected SKUs. Its durable posting proof pins both
  fingerprints and confirm reproduces the same full plan before T1 and under
  the immediate apply lock.
- One inter-facility transfer root owns immutable shipment, receipt, loss,
  discrepancy, cancellation, correction, storno and late-expense children.
  Shipment freezes source WAC/capital. Receipt posts only actual accepted
  quantity. Open in-flight quantity/capital is derived from the root and
  children, not stored in a transit warehouse or reservation. Loss retains its
  proportional frozen source capital and expense share. Mis-sort returns an
  expected-not-sent line at frozen source capital and moves an unexpected SKU
  only at its positive current source WAC; the displaced transfer-expense share
  is redistributed over the actual unexpected receipt. Insufficient stock/cost
  blocks that discrepancy child without duplicating independent accepted
  lines. Generic signed corrections cannot bypass the transfer state machine:
  transfer changes use typed outcomes or storno followed by a replacement.
  A receipt with active capitalized late expense must storno that late-expense
  child before the receipt itself can be reversed.
- FBO ↔ FBS reallocation stays inside one facility and preserves physical
  facility quantity. Pool WAC remains independent; optional expense increases
  destination capital with deterministic exact allocation.
- Inventory has one facility and selected FBS, FBO or both pools. Selected
  values are absolute targets; an unselected pool is unchanged. Positive
  surplus and shortage are separate linked children with a positive same-SKU
  cost basis and no zero/synthetic fallback.
- Scoped overhead allocates one positive RUB amount across FBS, FBO or both,
  using only positive selected physical quantities and stable largest-remainder
  kopeck rounding. Reversal and late expense are append-only linked documents.

A guided-acceptance storno is stricter than generic movement reversal. The
original posted manifest freezes supplier factual status/date, affected pool
balances and active aggregate rows. Recovery is allowed only while all those
affected states and the exact current supplier cost layer still equal
`before + original effect`; later reservations/debits/cost/source drift block
it. One transaction appends the negative legacy receipt, reverses the typed
pool movements, restores the supplier factual status/date, restores or
deactivates the cost layer and returns aggregate FF to the frozen before-state.
It then records immutable recovery replay evidence and queues only the affected
SKU projection. A row that was present is restored field-for-field, including
WAC, cost coverage, quality/certification, WB counters and provenance. A SKU
that was absent before confirm remains as an audited canonical zero row after
storno; quantity, capital and cost coverage are zero and WAC is null, so it is
semantically equal to the frozen absent state without deleting history. No
delete, ad-hoc SQL or blind store restore is part of this business compensation.

One or more immutable positive RUB expense lines may carry a safe free-text
basis and optional source-file digest. Their cents are allocated over the whole
shipped/reallocated quantity in stable-line order; partial loss retains the
same deterministic proportional share.

## XLSX boundary

`packages/application/ff_pool_documents_xlsx.py` generates and parses server-
side China acceptance and inventory workbooks using openpyxl data validation.
An empty active facility registry returns `no_active_facilities`; templates do
not create seeds. Barcodes are text. Accepted sheet names, headers, contract,
template/source fingerprints and complete selected-scope coverage are exact.

Before openpyxl parsing, reusable limits reject excessive request/file bytes,
ZIP entry count, total/per-entry uncompressed bytes, compression ratio, rows,
columns/cells and shared-string size. Only `.xlsx` and the allowlisted MIME are
accepted. Macros, formulas, external links, unsafe/duplicate ZIP members,
embedded active content, malformed OOXML, numeric/scientific/fractional
barcodes, duplicate resolved SKU and unknown/ambiguous/conflicting identity
fail with machine-readable errors. XML parts must be UTF-8, self-closing row/
cell/formula tags count toward the same bounds, and generated server/catalog
text is forced to string cells so formula-like labels/SKUs cannot execute in a
downloaded template. A blocked preview can never be posted. The only bounded
upgrade exception is an identical immutable China workbook previously blocked
with `money_minor_unit_required`: after deployment it is revalidated by the new
aggregate boundary and reopened in place, preserving the canonical request,
source revision, workbook digest and audit history. No other blocked code is
normally reopened. One further bounded compatibility case covers the initial
posting-plan readiness release: an identical guided request blocked with
`supplier_source_revision_changed` is reprocessed only when its stored request
revision exactly recomputes as `hash(raw supplier revision + workbook SHA-256)`
and the retry reproduces that same immutable identity. Processing then rechecks
the current raw supplier revision against the raw revision in the workbook
manifest, so genuine composition/cost drift remains fail closed. `ready` guided
previews additionally persist a query-only full
posting-plan proof after writer epoch, opening, current source revision,
aggregate/pool before-state and all recovery guards are constructible. Guided
`confirm_allowed` is false without that proof. An identical older immutable
`ready` request may be reprocessed in place only to add this stronger proof;
it cannot create another request or business row.
An identical guided request blocked by
`guided_acceptance_parity_failed`, `guided_acceptance_parity_not_current` or
`guided_acceptance_posting_plan_drift` may be reopened in place only after the
complete current aggregate/detail proof passes. Processing then rebuilds and
persists a fresh exact posting proof; the compatibility path never approximates
the old plan, bypasses source checks or posts a business row.
Stage 3 must additionally enforce the HTTP request read limit before buffering.

## Verification and later stages

`python3 apps/ff_pool_documents_smoke.py` covers empty/default-off bootstrap,
XLSX generation and attack/identity limits, lifecycle recovery/idempotency,
transfer conservation and partial outcomes, mis-sort fail-closed behavior,
reallocation, inventory, overhead/reversal, relations, bounded access plans and
T1 evidence. It also pins the production-shaped 26GN527 composition (21 SKU,
66,000 accepted, FBS/FBO 39,250/26,750, zero expenses and fractional-kopeck
capital) through filled-workbook parse, exact plan conservation, idempotent
repeat and stale-source rejection. It also pins the three production-shaped
SKUs whose aggregate FF row is initially absent. The same 21-SKU fixture applies
all 39 canonical-kopek movements over the exact fractional-kopeck pool capitals
observed after production cutover and proves the inverse movement restores every
prior Decimal exactly; existing balance capital is never silently normalized to
minor units. The FBS lifecycle smoke proves
legacy blocked/ready request revalidation, first aggregate-row materialization,
global aggregate/detail readiness, blocked-request reopening after exact parity,
stored/live posting-plan drift rejection before business writes, cost-coverage
drift rejection, immutable cost-layer replay and exact append-only guided
recovery to audited semantic zero. Existing Stage 1, FF
ledger/reservation/inventory/overhead/
documents, functional warehouse, capital and recovery smokes remain required.

Stage 3 public API/UI/document registry and later facility CRUD/seeds, opening,
shadow writer/reader activation, supplier-trigger switch, FBS lifecycle,
cutover and live business-data apply remain outside this migration.
