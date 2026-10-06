# Remaining finite operator producers — shared heavy exclusion

This bounded change covers active facility activation, factual shipment correction
and approved manual zero repair. It does not activate schedules/profile/readiness,
change payment preview, add queue/schema/intent, or revive historical backup restore.

## Covered admission boundaries

- `FfPoolSurface.create_facility`: validate requested active (defaultTrue), then
  maintenanceSH→nonblocking heavyEX **before** outer warehouse writer and the first
  inactive source/request acceptance. Update with active=True follows the same
  boundary. Inactive create, metadata-only update and active=False remain short
  writers. Existing identity/CAS/deactivation guards/readback remain unchanged.
- `DenseFbsService.activate_facility`: canonical direct guard before domain writer/
  intent/materialization. The actual surface owner reenters; another thread cannot.
- Factual HTTP correction reserves an independent actual heavy lease before
  `create_job`. Zero-change/dedup exits release it. The real lease transfers to
  existing OperatorJobStore, not a request/env/PID token. It is held through run_job,
  targeted effects/readback, optional acceptance update and operator terminal.
- One internal factual-only no-start callback terminalizes the already-created
  correction under the still-held EX. Constructor/admission/start errors use the
  existing reviewed no-start proof, error/failed/completed outcome and rethrow.
  KeyboardInterrupt/SystemExit remain cancellation. A possible native child gets
  no failed receipt/ref-clear/lease release: queued/running identity remains visible
  through bootstrap and worker finally. No startup algorithm or pickup queue added.
- Canonical `SupplierShipmentFactualCorrectionBlock.run_job`/`apply` and
  `WarehouseTargetedSupplierReplay.apply` acquire before scans/job claim/domain.
  All reachable replay.apply callers are factual.apply (already owned). Its second
  legacy apply is below the unconditional live return; `_targeted_dry_run` only
  plans. They are not extra unguarded producer paths.
- A running factual cancellation uses the existing final `needs_review` /
  `requires_review` readback contract. This makes no commit/rollback claim; exact
  source/targeted receipts must determine the outcome. Normal error/success behavior
  remains. No automatic replay after uncertain/cancelled work is added.
- Active factual CLI acquires before runtime/block constructors. Active unified
  CLI acquires before constructor/audit/source effects and its canonical apply
  acquires before audit start/domain; nested factual/derived calls reenter.
  Dry-run paths retain existing planning semantics. Cold CLI roots provision only
  directories/admission infrastructure before heavy, no business source.
- `DenseFbsService.apply_zero_repair_plan`: canonical guard before readback/
  revalidation/intent/domain. Existing approval/fingerprint/exact active target/SHA/
  StoreRegistry generation/bounded zero scope/inactive barrier/CAS/unknown readback
  remain mandatory. Cached zero readback and planning are unchanged.
- Actual facility/factual-confirm/factual-PATCH HTTP boundaries return409 busy with
  accepted=false/source_effects_started=false before source/job acceptance. Existing
  maintenance/auth/CSRF/preview contracts remain. Successful shape unchanged.

The full-store restore callers in supplier_26gn390_recovery,
ff_reservations_transit_cost_recovery, supplier_shipment_publication_chain and the
factual module's historical `_restore_backup_in_place` remain unreachable/refused.
No new capability or activation blocker is built for those migration lines.

## Offline checks and residuals

`apps/business_data_heavy_operator_producers_smoke.py` proves real-process busy
before acceptance, actual short inactive/metadata writes, owned nesting vs another
thread, CLI preconstructor/audit, actual factual durable job and optional acceptance
lifetime, pause between create and child admission, constructor/start errors and
both cancellation classes, native-spawn uncertainty before bootstrap without a
second job, running cancellation readback, and actual local HTTP409/no acceptance.
Legacy Dense/surface/targeted/unified/confirmation smokes preserve CAS, ambiguous
transport reconciliation, source identity and idempotency. No real WB/payment or
production data are used.

The no-start callback uses the existing durable job table. If durable terminal
cleanup itself cannot write (storage failure), the original error/cancellation is
retained with a bounded cleanup-failure note and all leases are released; existing
queued state requires explicit storage/job readback. This change cannot claim a
successful terminal write in an unavailable store and adds no speculative queue.

Global producer/profile/dispatch integration remains separate. After authorized
release, read only cached facility/request/correction/operator IDs and query-only
source/targeted receipts. Prove admission rejection created no new source/job,
uncertain starts remain drain-visible, and completed/cancelled workers released.
Do not run live correction, activation or zero repair merely to verify admission.

## Persisted active correction without proven execution

A failed no-start terminal write can leave the same durable queued/running row.
Deduplicated confirmation now reads the actual in-process correction→job→Thread
association. Only the preserved actual live/starting reference proves a worker;
an ID or durable active status alone does not. A native-start uncertain reference
remains present before bootstrap, using the existing OperatorStore lifetime rule.

Missing/finished worker proof, including after process restart, returns
`needs_review`, `requires_review=true`, `reason=worker_execution_unproven`, the
original correction/status and any known local operator job. It does not claim
source rollback, change the durable status, spawn or apply another job, or consume
a confirmation token as successful acceptance. Subsequent repeats remain the same
readback, and no automatic recovery/resend is introduced. Actual source/correction
readback and an explicit operator decision are required to resolve the unknown
execution; a terminal storage failure is not fabricated into a safe retry.
