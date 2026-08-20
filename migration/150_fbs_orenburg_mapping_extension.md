# Migration 150 — exact Orenburg FBS mapping extension and backlog drain

Status: implemented, default dry-run; production apply requires the separate
post-merge owner apply gate.

Contour: `scope:production-mutation`.

## Purpose

Extend the already applied Stage 7C lifecycle for the exact official seller
warehouse `854205` (`FBS Оренбург`) and office `12223`
(`Оренбург Центральная`) to facility
`fff_2579bb2741ed4ab23b11bb4c4183` (`FF Оренбург`). The accepted transfer
receipt `ffpd_690c4f6ba705b75c27292020cfbd` under root
`ffpd_676073b7b74a73cbbfe04a087444` remains immutable input: 21 SKU, 26,750
units and 2,874,226.82 RUB. This migration never repeats the transfer or edits
its documents.

Warehouse `1988668` remains mapped only to Moscow facility
`fff_d67e8c823d5f81dd988d00dbfea6`.

## Additive data contract

The existing
`sheet_vitrina_v1_wb_supplies_fbs_warehouse_facility_mappings` table remains
the only warehouse routing source. Its new optional official-evidence columns
are additive and leave the historical Moscow row valid. The Orenburg row binds
exact seller warehouse ID, exact official office ID/name/city, exact facility
and an official evidence digest; there is no name/fuzzy match.

`sheet_vitrina_v1_ff_pool_fbs_mapping_extensions` is an immutable authorization
envelope, not a parallel routing source. It binds the mapping row to the applied
cutover, exact deployed SHA, reviewed plan, accepted receipt/root, compound
frozen accounting boundary, complete target-row digest and owner gate.
`...mapping_extension_allocations` freezes the 21 receipt-backed positive WAC
rows. Both tables reject update/delete.

The historical Stage 7C manifest and checkpoint are never rewritten.

## Frozen source and suffix

Dry-run freezes local UTC boundary plus the independent order-observation,
status-observation and status-transition sequences. Every row at or below each
watermark participates in the complete stream digest; every Orenburg status row
at or below `W` participates in the complete target-backlog digest. A fresh
apply rehashes that exact source before and under the writer lock.

Append-only observations/transitions above `W` are not owner-gate drift. They
enter the post-`W` ordinary lifecycle suffix exactly once. A new gate is needed
only for frozen/business-critical drift, official warehouse/office identity,
receipt/allocation, deployed SHA, mapping semantics or exact target change. A
new SKU identity above `W` is not inferred; it stays identity-pending until a
later separately evidenced exact mapping exists.

## Apply and recovery

Canonical hosted commands are:

- `ff-fbs-mapping-extension-production-dry-run`;
- `ff-fbs-mapping-extension-production-apply`;
- `ff-fbs-mapping-extension-production-readback`.

Dry-run and readback use official WB read endpoints and SQLite `mode=ro` with
`PRAGMA query_only=ON`. Apply requires exact trusted-main deployed SHA, external
reviewed manifest/fingerprint, owner apply-gate reference and actor. It holds
the shared warehouse writer lock, creates a central T2 warehouse-domain
checkpoint for mutation kind `fbs_mapping_backlog_publication`, writes a
private `0600` exact-target before-image and commits mapping, extension,
allocation, exact identity evidence and ordinary lifecycle effects in one
SQLite transaction. After a possibly committed transport failure, operators
must query-only reconcile before any retry.

## Lifecycle invariants

- `new|eligible` creates or refreshes one reservation;
- only `supplierStatus=complete AND wbStatus=sorted` creates one physical debit;
- `complete` alone never debits;
- late, reordered and later terminal evidence remains audited no-op or
  reconciliation under the existing contract;
- missing or ambiguous warehouse/office/SKU evidence stays pending;
- lifecycle event and warehouse operation identities remain idempotent;
- aggregate FF is updated in the same transaction as a physical pool debit;
- WB writes, FBO supply, fulfillment-order calculation, demand allocation and
  factory-order semantics are outside scope.

## Reconciliation and closure

Query-only reconciliation proves exactly one `854205 -> FF Оренбург` mapping,
unchanged Moscow mapping and transfer receipt, frozen backlog partition and
effects, no duplicate event/operation, Orenburg physical/reserved/available,
current pool/aggregate parity, collector drain through frozen `W`, healthy
post-`W` continuation and zero WB writes. Public order/API evidence remains
privacy-minimized and exposes neither raw payload nor PII.

The PR uses `task:standard + scope:production-mutation`. Release/deploy of the
runner does not authorize apply. Closure uses the repository-wide immutable
pre-merge release gate, distinct post-merge exact manifest apply gate,
reconciliation comment and `/wb-core production-mutation complete ...`
terminalization contract.
