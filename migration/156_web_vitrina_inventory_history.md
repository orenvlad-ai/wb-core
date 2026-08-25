# Migration 156 — Web Vitrina inventory history

## Purpose

Replace the main Web Vitrina current-only inventory overlay with compact,
server-owned, date-aware history for TOTAL and every evidenced SKU. Preserve
legacy historical `stock_total` as WB-only source evidence and derive the
unified total without rewriting ready snapshots or introducing a second
general-stock metric.

## Repository and deploy phase

The deploy is additive:

- four empty append-only inventory-history tables and integrity triggers;
- capture wiring inside the existing ready-snapshot transaction;
- bounded date-window read materialization;
- the existing `stock_total / total_stock_total` public identities relabelled
  and ordered as `Остатки общие`, `Остатки WB`, `Остатки FBS Москва`,
  `Остатки FBS Оренбург`, then later user-enabled FBS facilities in their
  persisted order; an existing leading `FF`/`FBS` source-name prefix is not
  repeated in the public label;
- a versioned dry-run-first historical runner.

Deployment may create empty schema and begin capturing new accepted refreshes.
It does not authorize or perform historical production population. It adds no
schedule, no separate button, no WB/FBS source mutation and no rendered-plan
snapshot store.

For an accepted ready snapshot that contains a later exact WB column for the
date being closed, the writer first appends a new same-date capture and points
the superseding finalization to it. The WB operand comes from that exact dated
column; every FBS operand is preserved only from an earlier immutable capture
of the same business date. Current FBS balances are never copied backward. An
identical ready source revision is idempotent independent of writer time, while
a distinct proven ready revision is recorded as append-only supersession with
its own provenance.

## Historical evidence rules

For each proven ready date, persisted `stock_total` is retained with provenance
as WB-only. FBS components are reconstructed only from exact facility opening
allocations and later immutable physical/reservation movements. A facility is
`inapplicable` before independently evidenced activity. Applicability before an
exact opening is `missing`, not zero. No current balance, sales, orders, FBO,
aggregate FF, transit or seller-stock readback may fill a gap.

The desired dry-run window starts at the earliest proven ready date and ends at
the last closed business day. Its actual dates and partitions come solely from
the deployed store. No Moscow/Orenburg date from an audit prompt is hardcoded.

## Dry-run gate

The deployed runner defaults to query-only dry-run. It pins the exact deployed
SHA marker, schema/generation, cutoff/finalization identity, all consumed
source watermarks and the current target-history digest. It produces a private
mode-`0600` JSON manifest outside the repository and proves the canonical DB
byte digest unchanged.

The canonical database is resolved through `StoreRegistry` and the current
validated `storage_generation_manifest.json`, never by assuming the retained
legacy monolith filename. The manifest CAS includes the selected operational
generation id/path revision/watermark, storage-manifest digest and the exact
required source/history schema digest. Missing deployed history tables fail
closed before a dry-run manifest can be published.

The source watermark contract is target-scoped. It hashes only selected ready
evidence for `date_from..date_to` and the relevant facility roster, warehouse
mappings, openings, allocations, operations, lifecycle events and observations
that the FBS reconstruction consumes through `date_to`. Global table counts,
post-cutoff ready snapshots and valid post-cutoff lifecycle/observation writes
are excluded, so an ordinary current-day tick cannot stale a closed-window
manifest. Any selected target-date ready revision or relevant at/before-cutoff
FBS/roster/mapping change still changes the CAS and blocks apply.

Selected ready evidence uses a dedicated inventory-slice identity: exact
persisted and embedded snapshot/revision/rank fields, the complete selected
`DATA_VITRINA` header/date-column schema, and a sorted typed set of only
`TOTAL/SKU stock_total` scopes and values. Its `inventory_evidence_digest` is
the WB component source digest and source-CAS input. The full
`observed_plan_digest` remains immutable audit provenance in the reviewed
manifest/capture source manifest and is explicitly not an apply-CAS input.
Consequently same-rank non-inventory metric drift does not stale a manifest,
while selected snapshot/rank/revision, date/header/key/scope, typed missing
state, or target stock value drift still fails closed.

New facilities, multiple/ambiguous openings, source/schema/formula drift or an
invalid target window make the manifest `blocked`. Expected evidence gaps stay
explicit `partial`/`unavailable`; they are not silently converted to blockers
or values.

The owner callback contains only a path-safe summary plus exact manifest hash,
PR/head/merge/deployed SHA, target counts, full/partial/unavailable/inapplicable
partitions, gaps, non-target invariants, recovery/readback contract and the
exact proposed effect. The current release/deploy/dry-run authorization cannot
be broadened into apply authorization.

## Separate apply gate and recovery

Apply is permitted once, only after a separate exact owner authorization bound
to the reviewed manifest and deployed SHA under the repository production-
mutation contract. The runner then requires:

- trusted-main exact deployed SHA;
- exact manifest SHA-256 and non-empty approval reference;
- unchanged schema/generation, source watermarks and target-history digest;
- canonical warehouse writer lock;
- coherent target-scoped private before-image with a forward-restoration plan;
- no full-store/T3 backup for this bounded append-only closure;
- one `BEGIN IMMEDIATE` transaction;
- exact inserted capture/component/finalization count reconciliation;
- append/supersede-only writes to the four inventory-history tables;
- query-only post-commit readback and source/non-target reconciliation.

The manifest hash is the durable idempotency key. If transport is ambiguous,
the applies row and finalization readback determine the outcome; the caller
must not replay blindly. Restore is only through the canonical maintenance and
write-barrier procedure using the verified before-image.

## Acceptance

Repository acceptance requires the inventory history, planning, browser and
backfill smokes plus the exact-head `pr-gate`. Production acceptance requires
the trusted-main Release Runner to deploy and read back the exact squash-merge
SHA, followed by isolated UI verification. Any historical dry-run/apply and
final reconciliation remain separate production-mutation scope and require
their own exact manifest and owner gate.
