# Saved-source derived consumers

The following existing consumers enter canonical nonblocking heavy admission
before queue scans, expensive preparation or warehouse domain locks:

- `supplier_preparation_intents.drain_supplier_preparation_intents`.
- `cny_preparation_intents.drain_cny_preparation_intents`, including public
  `CnyLedgerBlock.replay_ledger` before its derived document sync.
- `fulfillment_recalc_intents.drain_fulfillment_recalc_intents`.
- `nomenclature_activation_intents.drain_nomenclature_activation_intents`
  and direct `DenseFbsService.activate_staged_skus`.
- `RegistryUploadHttpEntrypoint._replay_ff_document_queue`, before queue read,
  targeted plan/apply and economics publication.

The existing source transaction and its intent/targeted queue are unchanged.
Busy returns pending/deferred (FF: queued/deferred), without claiming running
or mutating/acknowledging the intent. Supplier and CNY after-save paths return
saved status with the existing source revision identity. No source/payment
operation is retried. Domain errors in direct consumers retain their contract.

Nomenclature and supplier invoice revision commit source+intent (invoice:
source+intent+audit) under the existing writer, close that writer, then attempt
derived admission. Invoice receipts cannot attribute a newer coalesced
preparation revision to the older saved operation. Nomenclature RuntimeError
continuation after commit returns saved rows reread from the actual source
status, with a separate diagnostic and the attempted revision. It does not
invent pending if a newer revision cancelled the demand or an acknowledgment
already activated the source. DenseFbsError/business errors are unchanged.

`WbSuppliesBlock.reconcile_functional_ff_state` remains the existing automatic
consumer for all four intent families. An entered owned warehouse/cycle lease
reenters the same real current-thread heavy owner. No separate scheduler,
operator buffer, schema, universal queue or desired-profile activation is added.
Existing source fingerprint/revision CAS and atomic delivery/publication ack
remain responsible for concurrent newer source commits and restart recovery.

This is finite producer coverage, not complete global serialization. Before
schedule activation, separate reviewed ownership is still required for Dense
facility creation/update activation (`ff_pool_surfaces` ->
`DenseFbsService.activate_facility`), forward zero-repair (`apply_zero_repair_plan`
-> `_materialize`) and supplier factual-date targeted/recovery paths
(`SupplierShipmentFactualCorrectionBlock.run_job/apply` ->
`WarehouseTargetedSupplierReplay`). Their source acceptance, writer ordering,
checkpoint/recovery and unknown-outcome semantics must be retained; money or
physical recovery writes cannot silently become generic deferred work.

Offline `apps/heavy_derived_intents_smoke.py` exercises real-process heavy
conflict, atomic saved source/intent, fresh-process owned automatic drains,
exact latest revision delivery, exception restart, direct publisher exclusion,
source writer ordering, same-owner reentry and invoice historical receipt CAS.
Existing source-intent suites exercise process-exit checkpoints, concurrent
new revisions and final warehouse balances/acknowledgments. No production or WB
is touched by these fixtures.
