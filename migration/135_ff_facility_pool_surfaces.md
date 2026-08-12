# Migration 135: FF facility/pool read, API and operator surfaces

## Goal and rollout boundary

Stage 3 exposes the already deployed Stage 1/2 facility × `FBS|FBO`
contracts through protected, bounded read models and a compact operator modal.
It does not activate the facility/pool writer or reader, create a feature
epoch, seed a facility, post an opening, backfill history or replace the
current aggregate FF producer. Deploying this migration therefore leaves
facilities, epochs, documents and movements empty unless they were created by
a separately authorized later activation.

The six warehouse stages and TOTAL formula are unchanged. Facility and pool
remain explanatory dimensions inside `ff`; their balances are never a seventh
warehouse and are never added to FF/TOTAL a second time. The opening action is
not exposed by the public API capability list or operator UI.

## Additive schema and facility management

Normal operational schema ensure adds only the empty append-only table
`sheet_vitrina_v1_ff_facility_changes`, its facility/time index and immutable
update/delete triggers. Each audited change stores a stable change id, durable
client request id and semantic request fingerprint, server-owned facility id,
action, actor, before/after JSON and UTC timestamp. This is bounded
`CREATE ... IF NOT EXISTS` metadata work: it scans or rewrites no existing
business table and needs no new recovery tier or full-store backup.
The audit table is part of the domain-table recovery allowlist, so any later
bounded domain checkpoint includes it together with the facility registry.

Facility create/update is available only when the existing latest feature
epoch has `writer_enabled=1`. The server owns immutable `facility_id` and
`code`; physical delete and reuse remain impossible. Name, active flag and
IANA display timezone use explicit validation, idempotent request identity and
optimistic `expected_updated_at` readback. Deployment does not call these
mutations.

## Protected HTTP and read models

The existing protected warehouse prefix now owns
`/v1/sheet-vitrina-v1/warehouses/ff/facility-pools`:

- capabilities and paginated facilities, facility detail and paginated
  facility/pool SKU balances;
- paginated root document registry with server-side facility, pool, kind,
  lifecycle, inclusive business-date and bounded search filters; all
  non-opening document kinds are filterable, while inventory surplus/shortage
  remain explicitly derived rather than direct operator actions;
- root detail plus lazy paginated lines/expenses, typed relations, bounded
  graph and source-file download;
- durable request status and paginated preview collections;
- China acceptance and inventory XLSX template/download/preview plus generic
  typed preview and explicit confirmation routes.

Every GET opens SQLite `mode=ro` with `query_only=ON`; it never initializes a
schema, resumes a request, acquires a writer lock or calls an external source.
Money aggregation uses an exact Decimal text aggregate and never SQLite REAL.
Root pages group document/evidence counts and expenses in bounded queries
instead of N+1. Evidence quantity/capital are explicitly marked non-additive
because a root graph can contain compensating or repeated lifecycle lines. Lines,
expenses, relations and graph are lazy. JSON reads carry deterministic ETags,
private no-cache semantics and bounded page sizes/payloads.

Routes remain behind the server-owned `supply` section. Supplier-only users
receive `403`. Every mutation additionally requires
`X-WB-FF-Pool-CSRF: 1`, same-origin browser evidence and the existing business
write barrier. JSON/body, identifier, date, enum, timezone and Decimal
validation fail with stable codes. Multipart XLSX rejects an oversized outer
`Content-Length` before buffering, then reuses the Stage 2 MIME/name, file,
ZIP/OOXML, row/cell, identity and formula/macro/link guards.

The nginx contract needs no new public allowlist entry because the exact API
family is below the already managed protected `/warehouses/` prefix.

## Operator lifecycle

The FF warehouse page contains one compact `Документы фулфилмента` launcher.
Opening it lazily reads capabilities/facilities and performs no business
mutation. The accessible modal provides facility navigation and management,
pool detail, filtered document registry, lazy evidence, the complete allowed
document-action list, XLSX template/upload flows and the explicit
preview-confirm lifecycle in plain Russian. It stores only the latest request
id in local browser storage, restores that durable server status after reload,
and cannot create a duplicate from a status refresh or double navigation.

Default-off is presented as a normal read-only state. No placeholder facility,
opening action, inferred transfer destination, speculative reservation, FBS
order or WB write is introduced. Legacy aggregate FF inventory/overhead
controls remain available under their existing disclosure and keep their
existing contracts.

## Verification and production acceptance

- `python3 apps/ff_pool_surfaces_smoke.py` proves default-off/no-read-write,
  stable facility identity/audit/idempotency/CAS/no-delete, exact Decimal
  aggregation, pagination/ETag/payload bounds and durable document status;
- `python3 apps/ff_pool_surfaces_http_smoke.py` proves protected route shape,
  conditional GET, CSRF and cross-site rejection, feature-off mutation and
  pre-buffer request-size enforcement;
- `python3 apps/ff_pool_surfaces_browser_smoke.py` proves lazy modal lifecycle,
  facility navigation, preview/confirm/reload recovery, focus restoration,
  narrow viewport/no horizontal overflow and no facility-pool 4xx/5xx or fatal
  page errors;
- existing Stage 1/2, supplier-role, six-stage warehouse, product-capital,
  recovery and hosted-runtime smokes remain mandatory.

Production closure is deploy plus query-only schema/count/readback and an
authenticated Playwright render on the exact deployed SHA. It must observe
zero facilities/epochs/documents/movements unless separately authorized
business data already exists. No seed, opening, cutover or feature activation
is part of Stage 3.

## Explicit later scope

FBW/WB-supply origin assignment, the official read-only FBS collector, shadow
writers/readers, facility seeds, historical opening/cutover and live activation
remain separate later stages with their own worktrees, PRs and release gates.
