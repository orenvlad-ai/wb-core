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
never deletes balance or document history and never resets a non-zero balance.
FBO, WB, aggregate FF, reservations, orders and historical captures are outside
the zero-initialization effect.

## Staged activation and reused physical contour

Facility activation and stock-managed SKU activation use a durable state machine:

`staged -> materializing -> materialized -> active`.

The immutable intent pins the full facility/SKU roster, applicability decisions,
writer epoch, every relevant balance before-image, activation CAS, effective
date and plan fingerprint. Facility activation materializes its full applicable
SKU roster. SKU activation materializes only the staged SKU across all active
facilities, after the full pre-existing roster is proven complete; it never
repairs a legacy gap belonging to another SKU. Materialization deliberately reuses the existing
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

The registry subject remains inactive while canonical documents are posted.  A
final transaction repeats coverage readback and subject CAS before publishing
active.  A crash or ambiguous transport leaves a durable resumable intent;
resume reads canonical request/document state and never blindly submits again.
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

An explicit zero becomes valid only at its proven future dense cutover `T0`.
The immutable document line and dense request manifest remain the T0 receipt even
after later movements advance the current balance watermark.  A business event
dated before T0 is rejected into explicit reconciliation/forward recovery; the
current zero is never copied backward.

## Future Orenburg repair boundary

`apps/ff_pool_dense_fbs.py` exposes query-only dry-run planning through the same
general service for facility `fff_2579bb2741ed4ab23b11bb4c4183`, pool `FBS` and
the exact 12 reviewed `nmId` values.  The plan expects 21 existing non-target
Orenburg FBS rows, pins target balance CAS and non-target digests, proposes only
12 absent explicit zeros, uses no whole-database copy and exposes no apply
entrypoint.  Moscow, WB, FBO, aggregate/cost/WAC, reservations, orders, documents,
movements and history are non-targets.  Deployment, tests and this dry-run do not
apply the repair.  A later production apply requires a separate exact owner gate,
fresh deployed-SHA/store/roster/balance/non-target CAS, one canonical
`pool_inventory` submit under the shared lock, query-only readback and
reconciliation.

## Verification

`apps/ff_pool_dense_fbs_smoke.py` covers new facility, new SKU,
archive/reactivation without reset, default applicability plus dated exception,
missing-writer failure, FBS/FBO/WB boundaries, shared-lock concurrency, balance
CAS drift, idempotent resume, ambiguous post transport, historical T0 guard and
query-only Orenburg planning.  Its production-shaped case materializes 1,000
pairs (250 SKU × 4 facilities), emits four inventory documents and zero movement
lines while keeping storage bounded and making no database copy.  Existing pool
document, lifecycle, facility surface, inventory-planning and inventory-history
smokes remain compatibility coverage.
