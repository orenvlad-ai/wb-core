# Migration 159: applicability-gated dense FBS physical truth

## Scope and invariant

For current state, every active FF facility × active, non-hidden, positive-`nmId`
nomenclature identity is applicable to the system-owned `FBS` pool by default.
An applicable pair has one canonical physical row even before its first business
operation.  The initial row is an explicit zero:

- `quantity=0`;
- `capital_rub=0`;
- `wac_rub=NULL`;
- immutable `pool_inventory` absolute-target evidence.

The only override is a dated, append-only `inapplicable` event with non-empty
reason, actor and provenance.  A later dated `applicable` event reinstates the
default only when the retained physical row still exists.  Archive/reactivation
never deletes balance or document history and never resets a balance. Both
nomenclature retirement paths (`delete` and active-to-inactive/hidden/changed
`nmId` save) hold the shared warehouse lock and fail before publication while
the prior `nmId` has any non-canonical-zero FBS row, active reservation, open
reconciliation/identity dependency, unfinished FBS order/lifecycle, or a
missing applicable row at any active facility. A fully
covered canonical-zero SKU may be archived and later reactivated against its
retained row and immutable documents.
An active facility can become inactive only while every FBS row has canonical
zero shape and it has no pending pool request, active FBS reservation, open
reconciliation, unresolved mapped identity or unfinished mapped FBS order. The
existing quantity guard for other pools is retained without extending these new
FBS dependency rules into FBO.
FBO, WB, aggregate FF, reservations, orders and historical captures are outside
the zero-initialization effect.

## Staged activation and reused physical contour

Facility activation and stock-managed SKU activation use a durable state machine:

`staged -> materializing -> [resumable] -> materialized -> active`.

The immutable intent pins the full facility/SKU roster, applicability decisions,
writer epoch, every materialized balance before-image, activation CAS, effective
date and plan fingerprint. Facility activation materializes its full applicable
SKU roster. SKU activation materializes only the staged SKU across all active
facilities, after the full pre-existing roster is proven complete by a compact
streamed coverage fingerprint; it persists neither default-applicability events
nor the already-covered facility × SKU cross-product and never repairs a legacy
gap belonging to another SKU. Materialization deliberately reuses the existing
`pool_inventory` request/manifest/document/document-line/post/readback path.  A
missing row receives target zero; an existing row receives its exact current
quantity.  Therefore dense initialization can insert an explicit zero or retain
an existing row, but can never create a movement, quantity delta, capital delta,
WAC, receipt or writeoff.  The existing document fingerprint and absolute-target
line are the coverage receipt; no second physical contour or pair-coverage ledger
is introduced.

Registry and pool-document tables cannot safely publish in one SQLite transaction.
The only additive non-physical schema is consequently:

- immutable dated applicability events;
- immutable dense activation intents;
- append-only intent lifecycle/receipt events.

The registry subject remains inactive while canonical documents are posted. A
final transaction repeats compact existing-coverage proof, materialized-pair
readback and subject CAS before publishing active. Exact identity/semantic/CAS
drift is terminally blocked. A transport exception whose canonical request is
durably `accepted`, `processing`, `ready`, `posted` or `replay` appends a
`resumable` receipt instead. Retrying the same orchestration identity reads back
already completed documents, resets only the exact interrupted `processing`
request, and advances only unfinished canonical submits; an unknown outcome is
never blindly retried.
Every phase uses the shared warehouse writer lock.  Balance/roster/subject drift
fails closed.

## Writer and reader boundary

Receipts, writeoffs, transfers, reservations and FBS order lifecycle processing
must find an existing applicable physical row.  They cannot materialize one as a
side effect.  Missing is `missing`, an explicit exception is `inapplicable`, a
proven non-zero row is `exact`, and a proven zero row is `exact_zero`; each state
includes reason and provenance.  Missing is never coerced to zero globally.  FBO
keeps its existing writer behavior and WB is never written.

Current planning totals use active facilities only.  Any applicable missing
SKU component makes the relevant facility/SKU aggregate unavailable instead of
adding a known-subset total.  Inactive facilities and inactive SKU pairs retain
history but are not current operands.  Future history captures persist the same
typed state/reason/provenance.  Existing historical captures are immutable.

Current applicability/read publication obtains its date from the shared
`packages.business_time` EKT helper. The UTC/EKT boundary is deterministic:
`2026-08-25T18:59:59Z` is business date 25 August and
`2026-08-25T19:00:00Z` is 26 August. It never depends on process-local
`date.today()`.

An explicit zero becomes valid only at its proven future dense cutover `T0`.
The immutable document line and dense request manifest remain the T0 receipt even
after later movements advance the current balance watermark.  A business event
dated before T0 is rejected into explicit reconciliation/forward recovery; the
current zero is never copied backward.

## Future Orenburg repair boundary

`apps/ff_pool_dense_fbs.py` exposes query-only dry-run planning through the same
general service for facility `fff_2579bb2741ed4ab23b11bb4c4183`, pool `FBS` and
the exact 12 reviewed `nmId` values. `--target-file` and `--runtime-dir` are
mandatory. The target must be the exact active primary hosted target and the
operational database must resolve through an explicit (non-implicit)
StoreRegistry generation opened `query_only`; the legacy implicit monolith path
is rejected.

The plan proves the one active mapping seller warehouse `854205` + official
office `12223` → the exact facility with pinned mapping/official evidence and
mapping-extension receipt whose 21 positive-WAC allocation identities exactly
equal the current non-target Orenburg FBS identities. It proves the exact active
stock-managed roster of 33 identities,
partitioned into the approved 12 absent targets and the exact 21
current Orenburg FBS balance identities. All 12 targets must remain applicable
at the canonical EKT business date. The targets must be absent across every
balance epoch and from target FBS movement, document, lifecycle, reservation,
identity-mapped order and official-order evidence. The latest finalized
2026-08-24 capture must carry the exact 12 as `exact_zero` with
`fbs_mapping_extension_allocation` provenance; 25-August evidence is only
fingerprinted and no current value is retrocopied or rewritten.

Moscow, WB, FBO, aggregate/cost/WAC, reservations, orders, documents, movements
and the two scoped history dates receive target/non-target fingerprints for a
later reconciliation. Queries are facility/roster/date/warehouse scoped and
streamed in 512-row chunks; unrelated multi-GB operational tables are never
copied or fully hashed. Optional output is admitted by the existing root-storage
policy and written `0600`, otherwise the command remains stdout-only. No apply
entrypoint exists. Deployment, tests and this dry-run do not apply the repair.
A later production apply requires a separate exact owner gate,
fresh deployed-SHA/store/roster/balance/non-target CAS, one canonical
`pool_inventory` submit under the shared lock, query-only readback and
reconciliation.

## Verification

`apps/ff_pool_dense_fbs_smoke.py` covers new facility, new SKU,
non-zero/archive save/delete guards, zero archive/reactivation retention, active
reservation, missing-coverage and unfinished-order retirement guards, default applicability plus
dated exception, missing-writer failure, FBS/FBO/WB boundaries, shared-lock
concurrency, balance CAS drift, exact-id pre-commit transport resume,
after-commit ambiguous readback, EKT date boundary, historical T0 guard and
query-only Orenburg planning over 4,000 unrelated noise rows. Its
production-shaped case first materializes 1,000 pairs (250 SKU × 4 facilities),
then activates one new SKU with only four persisted pair rows and a non-persisted
compact proof over the prior 1,000 pairs. It reports dense intent, manifest and
document bytes, emits zero movement lines/default-applicability events and makes
no database copy. Existing pool
document, lifecycle, facility surface, inventory-planning and inventory-history
smokes remain compatibility coverage.
