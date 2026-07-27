# Module 51 — Warehouse Recovery Policy

## Purpose

This module is the single recovery control plane for warehouse, supplier-cost,
FF-stock and dependent economics mutations. It replaces call-site-owned
full-database checkpoints with deterministic tiers proportional to the
mutation closure.

Implementation:

- `packages/application/warehouse_recovery_policy.py`
- `apps/warehouse_recovery_policy_canary.py`
- `GET /v1/sheet-vitrina-v1/warehouses/recovery`

The authoritative design and acceptance matrix are
`migration/123_warehouse_recovery_policy_design.md`. Stage 4 routing,
retention and legacy storage sanitation are authoritative in
`migration/125_storage_recovery_sanitation.md`.

## Tier contract

- T0 is a semantic no-op. It creates no recovery file, registry row,
  reservation, undo row or recovery read.
- T1 is a document/shipment/SKU/date-scoped before-image and undo journal.
  Finance raw, full-store backup, full-store SHA and integrity scans are
  forbidden. SQLite BLOB values use reversible byte encoding, so consumed
  source documents such as FF previews are restored byte-for-byte.
- T2 is a warehouse/cost domain checkpoint with source watermarks and schema
  revision. `wb_finance_weekly_raw_rows` is excluded even though derived
  `wb_finance_*` tables are in the recoverable domain. Tables, explicit and
  implicit indexes, triggers and their capacity pages are checkpointed; rollback
  recreates the checkpoint schema before replaying rows.
- T3 is a coherent store backup. Only `schema_migration` or `store_migration`
  with a code-reviewed identifier from `T3_MIGRATION_ALLOWLIST` can select it.

Unknown mutation kinds, invalid closure kinds, a free-form T3 identifier and
disabled legacy operations fail closed before recovery bytes are reserved.
The policy implementation contains the repository's only warehouse/cost
`runtime.backup_database(...)` call.

## Durable lifecycle and capacity

Operations advance by compare-and-set:

`planned → reserved → writing → verified → mutation_running → retained`

Recovery alternatives are `failed_recoverable`, `rolled_back`, `quarantined`
and retention-driven `released`. Every transition persists state version,
heartbeat and next action. An exact retry resumes its deterministic operation
identity or creates a later deterministic generation only after a terminal
rollback; it never silently duplicates the business mutation.

Capacity reservations bind filesystem identity, planned bytes, operational
reserve, expiry and owning operation. Pre-write and post-write watermarks are
both fail closed. Public status reports planned, actual and read bytes rather
than inferring work from filenames.

New T2 checkpoints are routed through the authoritative runtime root to
`state/backups/warehouse-recovery/domain-checkpoints`. Production mounts
`state/backups` on the dedicated backup filesystem; status reports both mount
identities so a missing/misdirected mount is visible. The two newest verified
rollback points are protected, while successful T2 retention is bounded by
three generations, 2 GiB and 24 hours. The plan also reports observed cadence,
24-hour/14-day no-GC projections, bounded 30-day growth and 8 GiB degraded /
4 GiB hard-stop watermarks.

The read-only orphan scanner classifies registered and unregistered raw SQLite,
WAL, SHM, journal, zstd, manifest, temp and undo families, registry-without-byte
and undo-without-registry states, stuck operations, corrupt registered
artifacts and foreign non-target files. Corrupt identity becomes quarantine
evidence; retention removes only lifecycle-authorized exact artifacts. Expired
`retained` evidence advances to `released`; expired `rolled_back` evidence
keeps its terminal audit state while its files and undo rows are released.
Failed and quarantined evidence is never removed by ordinary retention.
Retention planning has a stable exact fingerprint and apply owns a durable
registry audit. It runs before and after hourly/manual publication under the
same writer lock, and can also be invoked through deployed-SHA-pinned hosted
`warehouse-recovery-retention-dry-run|apply`. Restart resumes the same audit;
operation state-version, path/stat/SHA or non-target drift fails closed.
Files already present in the legacy `backups/` tree before the first durable
policy operation are reported separately as the pre-policy baseline. They do
not make a later canary look as though it created an orphan, but they remain
visible in the API/UI; a new file or any baseline file touched after the
activation timestamp is unclassified and fails closed. This classification is
read-only and never adopts, deletes or rewrites a legacy backup.

## Routed production contours

The central policy is used by exact supplier-cost queue replay (including
commission regression), supplier factual correction and targeted warehouse
replay, FF manual/targeted operation closure, calculation parameters and
targeted economics, warehouse archival estimate and supplier certification,
bounded and wide canonical-cost/weekly-Finance publication, warehouse opening,
hourly/manual publication, emergency rebuild and rollback, and the allowlisted
functional schema cutover.

Supplier-document and WB-supply source writes keep their existing transactional
run evidence. Every supplier/CNY/cost source revision enters the targeted
recalculation queue through a correlated T1 queue before-image/undo operation;
an identical queued/complete revision whose date and SKU set are already
satisfied is a true T0 and does not update `requested_at` or create recovery
state. This is the mandatory commission no-op regression. The dependent
mutation closure is then recovered by the targeted T1 replay or
encompassing T2 publication. Historical
`supplier_26gn390_recovery`, `supplier_cny_payment_10_recovery` and
`ff_reservations_transit_cost_recovery` apply entrypoints remain disabled and
diagnostic-only. The historical Proxy margin 3 one-off is likewise dry-run
evidence only; its apply mode fails closed before any backup or mutation.

`apps/warehouse_recovery_policy_static_smoke.py` walks current executable
bounded entrypoints and fails CI if one regains a full backup, coherent-size
probe, full-store integrity/SHA scan or a second T3 backup call.
`apps/storage_recovery_writer_inventory_static_smoke.py` separately classifies
every production SQLite backup primitive/caller and fails if a new
unclassified writer appears or a scheduled full-monolith writer is introduced.

Legacy bytes are not adopted by ordinary retention. The separate
`apps/storage_recovery_sanitation.py` runner inventories both canonical backup
roots, accepts only explicit family names, losslessly compresses raw immutable
SQLite evidence through the standard archive primitive, verifies decompressed
size/SHA, and only then removes raw bytes or superseded verified generations.
Every action is exact-fingerprint/deployed-SHA gated, audited, fsynced,
restart-safe and non-target-digest checked. Custom-manifest, foreign,
incomplete and corrupt families remain untouched.

## Operator and production acceptance

The warehouse update tab loads the protected recovery API and visibly renders
tier/operation, scope, planned/actual/read bytes, lifecycle, next action,
rollback expiry, artifact/error count, capacity, writer/timer and
orphan/quarantine state. Readback failures remain visible; there is no silent
fallback.

The status API and card are strictly read-only: they do not initialize the
registry, expire reservations or create a writer lock. Missing initialization
is reported explicitly; expired active reservations remain visible until the
next policy writer reconciles them by CAS.

The canonical production sequence is:

1. deploy through the GitHub Release Train;
2. run hosted canary dry-run against the exact deployed SHA;
3. apply the exact fingerprinted T0/T1/T2 canary;
4. run the isolated Playwright warehouse UI Flow with
   `warehouse_recovery_policy_20260726` and the exact deployed SHA;
5. accept only that deployed merge SHA with the evidence digest.

The canary's only temporary row is in the recovery-owned canary table and is
removed by the exact T1 rollback. T2 performs no business mutation. Success
requires identical warehouse-domain digests and zero unclassified raw,
sidecar, temp or orphan evidence created after the durable policy activation
boundary. The independently visible pre-policy baseline is not attributed to
the canary. UI acceptance pins the runtime marker and requires T1/T2 canary
operations carrying that same full deployed SHA to be terminal; historical
iterations cannot overwrite or satisfy the check. Before a new deployed-SHA
attempt, the canary
releases only older canary-scoped failures proven to have stopped before
`mutation_running`; their deterministic owned temp/checkpoint paths and
reservations are released, while any failed business mutation remains
fail-closed.
