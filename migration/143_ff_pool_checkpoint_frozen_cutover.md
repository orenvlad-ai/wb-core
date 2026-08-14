# Migration 143 — checkpoint-frozen Stage 7C cutover

## Goal

Migration 143 supersedes the moving-source owner gate in Migration 142.  It
does not apply the opening by deployment.  The dry-run remains query-only and
creates an immutable accounting boundary in its private reviewed manifest:

- local UTC `T` is the truthful collector observation boundary;
- compound `W` contains the independent order-observation, status-observation
  and status-transition sequences;
- complete bounded-row digests prove every row at or below each component of
  `W` without embedding the full append-only streams in the plan.

The official FBS status response has no source timestamp.  No upstream event
time is inferred.  Classification uses only durable local sequence and
`observed_at`; an already known order whose status changes above `W` is a
post-checkpoint event even when the order itself was created earlier.

## Stable owner gate

The exact deployed SHA, aggregate opening quantity/capital, facilities,
mappings, complete/sorted policy, frozen evidence, non-target invariants and
`excluded_pending_receipt` proof remain owner-gated.  Apply re-hashes those
same frozen rows under `BEGIN IMMEDIATE` and fails closed on any change.

Rows appended above `W` are outside the reviewed source fingerprint.  Ordinary
collector growth, a new order or a later status observation therefore does not
expire the gate.  A new gate is required only for a different deployed SHA,
frozen/business-critical fingerprint, handoff rule or pending-receipt
treatment.  Operational timing guidance is advisory and does not age the
frozen boundary.

## Atomic suffix drain

The five-minute collector remains enabled.  The opening transaction writes the
manifest, checkpoint, epoch and historical backfill, then drains every already
persisted status observation above `W` before the same commit. The finite
write-locked suffix is consumed in bounded pages (at most 100 × 100,000 rows);
exceeding that explicit safety bound rolls the whole attempt back without aging
the gate. The drain uses
the exact tuple `order_id + order revision + status digest + order/status
sequence` and persists its high-water progress atomically with reservation,
physical and reconciliation effects.

- new/eligible creates or refreshes a reservation;
- pre-handoff cancellation is a no-op or release;
- `supplierStatus=complete AND wbStatus=sorted` fulfills once with frozen WAC;
- later/reordered terminal or handoff evidence is an immutable no-op, never a
  second debit;
- post-handoff cancellation/return enters reconciliation;
- a first locally observed pre-`T` row that arrived above `W` is isolated in
  append-only late evidence and cannot block unrelated suffix rows.

SQLite single-writer serialization is the short collector latch.  A collector
write already committed before the opening transaction is drained atomically;
one waiting during it commits afterward and is consumed by the ordinary
epoch-gated collector processor.  There is no operator pause or orphan hold.

## Recovery and non-targets

Crash before commit rolls back opening, delta effects and drain progress.
Crash after commit resumes exact readback without replay.  Every later drain
batch advances progress in the same transaction as its effects, so retry is
idempotent.  The FBS collector, shipment `26GN527`, supplier acceptance and WB
remain untouched by deployment and dry-run.  `26GN527` stays
`excluded_pending_receipt` until guided `Принять на FF`.

Verification is owned by:

- `python3 apps/ff_pool_cutover_smoke.py`;
- `python3 apps/ff_pool_cutover_production_smoke.py`;
- `python3 apps/ff_pool_fbs_lifecycle_smoke.py`;
- `python3 apps/wb_fbs_shadow_polling_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`.
