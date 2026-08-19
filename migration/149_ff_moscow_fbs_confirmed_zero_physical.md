# Migration 149 — FF Moscow FBS confirmed physical zero publication

Date: 2026-08-19  
Contour: separately owner-gated production-data mutation

## Decision and exact scope

The owner has confirmed that physical FBS quantity in `FF Москва` is exactly
zero for only these active SKU:

- `497413772`;
- `497415593`;
- `497416931`.

Before apply, a missing facility/pool/SKU row remains unknown and is not zero.
The mutation resolves the immutable active Moscow `facility_id` from canonical
production and publishes an explicit `quantity=0`, `capital_rub=0`,
`wac_rub=NULL` row for each named SKU in pool `FBS`. It never targets Orenburg,
FBO, another SKU, a reservation, supplier shipment, WB source, sales/cost/
capital source, calculation result or factory order.

## Authoritative accounting path

`apps/ff_pool_zero_physical_production.py` is the only production entrypoint,
through the canonical hosted commands:

- `ff-pool-zero-physical-production-dry-run`;
- `ff-pool-zero-physical-production-apply`;
- `ff-pool-zero-physical-production-readback`.

The production contract is `ff_pool_zero_physical_production_v1`. Dry-run is
query-only and pins the exact deployed SHA, resolved facility identity, current
feature epoch/cutover, three missing-or-explicit-zero target rows, active SKU
and nomenclature identities, signed reservations, physical/capital totals and
non-target digests. Any existing non-zero target row is a blocker; the runner
does not turn an adjustment into a zero-publication shortcut.

Apply requires the exact reviewed fingerprint, actor and immutable GitHub apply
gate reference. It uses the existing `pool_inventory` document service with
source contract `ff_pool_confirmed_zero_physical_v1`. Three immutable
`absolute_target=0` lines are the business evidence. A zero-to-zero delta is
not a physical movement, so the mutation creates no pool movement line and no
synthetic cost. The guarded document projection materializes only a previously
missing zero balance row, with the immutable inventory document as its source
watermark.

The document posting remains serialized by the shared warehouse writer lock.
Its central T1 recovery operation records the exact absent target rows and all
request/document before-images in a verified server-owned undo artifact, then
reaches `retained`. The external evidence JSON is private mode `0600` and binds
the reviewed manifest, document, T1 operation, pre-change digest, non-target
invariants and post-apply readback. Recovery is forward reconciliation or a
separately authorized T1 supersession; ad-hoc SQL, row deletion and blind
database restore remain prohibited.

If the immutable document completes but the caller loses the response before
the external evidence file is written, a retry does not post again. It accepts
only the same document source revision, actor and immutable apply-gate
reference, verifies the exact three lines, zero movements and retained T1
operation, then reconstructs the private evidence. Any independent non-target
advancement observed after the shared writer lock was released is named in the
evidence instead of being attributed to or hidden by this document.

## Reconciliation

Readback requires all three rows to be explicit zero and checks the selected
facility read model over the complete active SKU scope. Signed availability is
still `physical - reserved`; therefore a target SKU with a reservation may be
negative after the physical zero is published. Physical/capital totals remain
unchanged, reservations are unchanged, all non-target balance rows and movement
lines retain their exact digests, and supplier/aggregate/cutover/facility
invariants remain unchanged.

The FBS fulfillment-order status must no longer list these three SKU as missing
Moscow physical evidence. Any other Moscow or Orenburg blocker remains visible
and is not masked. Readback never starts a calculation or creates an order.

## Release boundary

The PR uses `task:standard + scope:production-mutation`. Deploying the default-
dry-run runner does not authorize business apply. The current two-gate Release
Train contract remains mandatory: an immutable pre-merge release gate, exact
merge/deploy, then a distinct immutable post-merge apply gate that binds the
deployed SHA and fresh manifest fingerprint. Apply/reconciliation evidence is
terminalized only by the exact current production-mutation completion command.

## Verification

- `apps/ff_pool_zero_physical_production_smoke.py` proves query-only dry-run,
  exact scope, zero-row publication, signed reservation/read-model behavior,
  no movement, unchanged non-target digests, retained T1 evidence, idempotency,
  lost-response evidence recovery and stale/non-zero fail-closed behavior;
- `apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py` proves exact
  SHA, reviewed-plan stdin, apply-gate and canonical evidence-directory binding
  plus mandatory post-apply readback;
- `apps/ff_pool_documents_smoke.py` remains the general document/recovery
  regression gate.
