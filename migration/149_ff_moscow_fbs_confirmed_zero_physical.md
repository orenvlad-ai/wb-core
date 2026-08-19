# Migration 149 — FF Moscow FBS confirmed physical zero publication

Date: 2026-08-19
Contour: separately owner-authorized production-data mutation

## Decision and exact scope

The visible owner decision changes unknown to confirmed physical zero for the
exact active/non-hidden catalog cohort below in canonical facility
`fff_d67e8c823d5f81dd988d00dbfea6` (`FF Москва`) and pool `FBS`:

- planner SKU: `497413772`, `497415593`, `497416931`;
- iPhone 18: `1221231049`, `1221235702`, `1221244040`, `1221249681`;
- No Frame Clean: `1235346302`, `1235353505`, `1235356960`, `1235358879`,
  `1235360281`, `1235361692`, `1235365622`, `1235366828`, `1235368116`,
  `1235369738`;
- No Frame Anti-Spy: `1235373410`, `1235374572`, `1235375860`, `1235377899`,
  `1235379341`, `1235381785`, `1235384726`, `1235387930`, `1235392011`,
  `1235393709`, `1235398515`, `1235399866`;
- No Frame Matte: `1235404761`, `1235405720`, `1235406475`, `1235406984`,
  `1235407826`, `1235409896`, `1235411727`, `1235412880`, `1235413454`,
  `1235414081`, `1235419785`, `1235421650`.

The total is exactly 41 distinct `nmID`. Before apply, a missing
facility/pool/SKU row remains unknown and is not zero. First apply is eligible
only while all 41 target rows are still absent and every target has exactly one
active/non-hidden nomenclature identity. A matching completed immutable
document is accepted only as an idempotent retry of the same manifest, actor
and apply-gate reference.

The mutation publishes `quantity=0`, `capital_rub=0`, `wac_rub=NULL` only for
those 41 keys. It never updates an existing balance row and never targets FBO,
Orenburg or another facility/SKU, reservations, inbound, supplier shipments,
WB data, aggregate FF, sales, costs/capital, transfers, calculations or
supplier/factory orders.

## Authoritative accounting path

`apps/ff_pool_zero_physical_production.py` is the only production entrypoint,
through the canonical hosted commands:

- `ff-pool-zero-physical-production-dry-run`;
- `ff-pool-zero-physical-production-apply`;
- `ff-pool-zero-physical-production-readback`.

The machine contract is `ff_pool_zero_physical_production_v1`. Dry-run is
query-only and pins exact deployed SHA, immutable facility identity, current
feature epoch/cutover, the 41 missing targets, active/non-hidden nomenclature,
planner membership, signed reservations, physical/capital totals and
non-target digests. Any target-row presence or identity/source drift blocks the
first apply.

Apply requires the exact reviewed fingerprint, actor and immutable GitHub
apply-gate reference. It posts one audited `pool_inventory` document with 41
immutable `absolute_target=0` lines. The existing inventory document contract
materializes a previously missing FBS zero balance row even when delta is zero.
No pool movement line, quantity/capital delta or synthetic WAC is created.

The shared warehouse writer lock serializes posting. Central T1 recovery
captures the absent balance keys and request/document before-images in one
verified server-owned undo artifact and reaches `retained`. Private `0600`
evidence binds the reviewed manifest, document, T1 operation, pre-change
digest, non-target invariants and exact post-apply readback. Recovery is
forward reconciliation or separately authorized T1 supersession; ad-hoc SQL,
row deletion and blind database restore remain prohibited.

If the document completed but the caller lost the response before external
evidence was written, an exact retry verifies the same source revision, actor,
apply gate, 41 lines, zero movements and retained T1 operation, then recreates
the private evidence without posting again.

## Reconciliation

Readback requires all 41 balance rows to be explicit physical zero. Signed
availability remains `physical - reserved`, so a target with an existing
reservation may have negative available while reservation semantics and rows
stay unchanged. Facility physical/capital totals, every non-target balance row,
all movements, reservations, supplier/aggregate/cutover/facility state and
their recorded digests must remain unchanged.

Only three of the 41 targets are planner SKU. Their materialization must remove
the last Moscow `missing physical` blockers, yield
`calculation_enabled=true` for Moscow and leave Orenburg blockers visible.
Readback and apply never start a calculation and never create an order; a
separate read-only verification proves that the formula path still declares
`wb_stock_used=false`.

## Release boundary

The PR uses `task:standard + scope:production-mutation`. Deploying the
default-dry-run runner does not authorize business apply. The current two-gate
Release Train contract is mandatory: immutable pre-merge release gate, exact
merge/deploy, then a different immutable post-merge apply gate bound to exact
PR, deployed SHA and fresh manifest fingerprint. A qualified executor may
relay the verbatim visible owner authorization under the transport-neutral
contract; it may not broaden or synthesize that decision. Apply/reconciliation
is terminalized only by the exact current production-mutation completion
command.

## Verification

- `apps/ff_pool_zero_physical_production_smoke.py` proves the 41-row query-only
  plan, exact facility/catalog scope, zero-row publication, signed reservation
  behavior, Moscow readiness, no movement, invariant preservation, retained T1
  evidence, idempotency, lost-response recovery and stale/non-zero fail-closed
  behavior;
- `apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py` proves exact
  active target/runtime/SHA, reviewed-plan stdin, apply-gate and evidence-root
  bindings plus mandatory post-apply readback;
- `apps/ff_pool_documents_smoke.py` remains the general audited inventory/T1
  regression gate.
