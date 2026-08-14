# Migration 142 — exact FF pool opening and FBS lifecycle cutover

## Goal and owner gate

Stage 7C adds the production-shaped, dry-run-default path that opens the
existing aggregate `ff` state inside one exact facility × `FBS|FBO` epoch and
starts the FBS order lifecycle. Repository deployment is inert: it does not
choose `T`, enable the epoch or change a business row. Production apply remains
`scope:production-mutation` and requires the exact dry-run fingerprint plus an
owner approval reference.

The initial allocation is read from the current active aggregate at the live
boundary. Quantities are signed SQLite `INTEGER`; quantity, capital and WAC are
calculated with `Decimal` and stored as exact text. SQLite `REAL`, synthetic
zero cost and approximate accounting are forbidden. Operational comparison to
a partner's physical count may be approximate only as later inventory evidence.

## Pending China receipt

Shipment `26GN527` (`sup_adc29a3cba934403bca4842c2add8b7d`) is pinned in the
reviewed manifest as `excluded_pending_receipt`. It contributes zero opening
quantity/capital and zero historical FBS debit. This classification is valid
only while all of the following are exact and unchanged under the held writer
boundary:

- factual FF acceptance date is empty;
- shipment and product-line quantities are positive integral values and equal;
- no aggregate FF receipt source key exists;
- no supplier FF cost layer exists;
- the shipment is not archived or partially posted.

A missing, ambiguous or partly posted shipment still blocks. A clean pending
receipt does not. After cutover it remains in transit. Later `Принять на FF`
is the only factual acceptance path: it records actual received quantities once,
splits them between FBS/FBO, records discrepancies and expenses, updates the
aggregate and pool projections atomically, and materializes the exact accepted
cost layer. The legacy factual-date editor remains prohibited.

## Exact pre-T checkpoint

The cutover pins immutable official order and status observations at one
collector watermark. For each exact mapped order:

- pre-T `supplierStatus=complete AND wbStatus=sorted` creates one historical
  physical debit at the frozen opening WAC;
- active pre-handoff state creates one opening reservation and reduces
  available, not physical, stock;
- pre-handoff cancellation creates an immutable no-op;
- later cancellation/return after a physical debit enters a separate
  reconciliation lane and never silently returns stock;
- a late-arriving pre-T order is isolated in `late_pre_t` and cannot double
  debit or globally stop unrelated post-T orders.

Available quantity may be negative (`physical - reservations`); physical
quantity and capital remain exact. Every order/event identity is idempotent.

## Handoff semantics and post-T processor

Official Marketplace documentation describes `supplierStatus=complete` as the
seller-controlled delivery state and `wbStatus=sorted` as WB warehouse sorting;
the official sandbox explicitly uses `(complete, sorted)` for an order accepted
by WB. Therefore the reviewed proposal is the conjunction
`supplierStatus=complete AND wbStatus=sorted`. `supplierStatus=complete` alone
is permanently forbidden. Observed `complete/waiting → complete/sorted`
transitions are evidence only and cannot approve the policy automatically; the
same decision is included in the owner-gate manifest.

The post-T processor is hard-off until an applied manifest, approved policy and
writer epoch exist. The dedicated five-minute collector remains the observation
owner and never writes WB. After each successful poll the processor:

- reserves a new eligible order;
- releases a pre-handoff cancelled reservation;
- fulfills a reservation and debits physical/capital once on approved handoff;
- treats later sold/closed observations as no second debit;
- routes later cancellation/return evidence to reconciliation.

## Production runner and recovery

`apps/ff_pool_cutover_production.py` is dry-run by default. The canonical hosted
commands are:

```text
ff-pool-cutover-production-dry-run
ff-pool-cutover-production-apply
ff-pool-cutover-production-readback
```

Every command requires the canonical target and exact deployed SHA. Dry-run is
query-only and proposes a window without choosing `T`. Apply validates the
reviewed fingerprint/approval, acquires and confirms the durable HTTP barrier,
holds/drains business writers and the warehouse timer, proves the supplier
acceptance writer is held and the FBS timer is still enabled/active, writes a
central Recovery Policy T2 warehouse-domain checkpoint plus a mode-`0600`
exact-target before-image, and only then selects `T` and revalidates all source
evidence under `BEGIN IMMEDIATE`.

The generic business-data inventory explicitly classifies
`wb-core-fbs-shadow-collector.timer` as a continuous observation-only timer. It
is inventoried but never disabled, waited or restored by the quiet window; an
unclassified timer still fails before the first maintenance mutation. If a
failure occurs after HTTP barrier acquisition but before `hold_started`, the
only automatic abort path requires mode-`0600` maintenance state and audit to
both predate the exact barrier timestamp, repeats that filesystem proof around
a fresh control readback, and records its fingerprint. Any state/audit change,
unknown timer, cron or owner-policy drift keeps the barrier fail closed and
requires the ordinary exact-restore path.

The opening, historical events, checkpoint, manifest and feature epoch commit
atomically. Crash before commit rolls back; the same exact gate resumes its T2
checkpoint and uses a new auditable write-epoch attempt without overwriting the
mode-`0600` before-image. Ambiguity after commit retains all
barriers for exact readback/recovery. Successful exact aggregate/detail,
manifest, checkpoint, non-target and pending-receipt readback appends recovery
evidence, restores the exact prior controls and releases the HTTP barrier.
Blind delete/replay and ad-hoc SQL are forbidden.

## Verification

- `python3 apps/ff_pool_cutover_smoke.py`;
- `python3 apps/ff_pool_cutover_production_smoke.py`;
- `python3 apps/ff_pool_fbs_lifecycle_smoke.py`;
- `python3 apps/ff_pool_documents_smoke.py`;
- `python3 apps/wb_fbs_shadow_polling_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`.

The fixtures cover signed/fractional opening conservation, historical debit
once, historical and later post-handoff reconciliation, active reservations
and negative available, cancellations, order arrival
during the boundary, late pre-T isolation, concurrent supplier acceptance
failure, guided post-cutover receipt, no WB writes, pre-commit crash recovery,
retry/idempotency and exact readback after lifecycle events.
