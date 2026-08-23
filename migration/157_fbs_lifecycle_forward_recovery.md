# Migration 157: FBS lifecycle forward cutover and bounded backlog recovery

## Scope

This migration adds one versioned repo-owned production runner for separating
continuous FBS ingress from a pinned historical lifecycle suffix. It extends
the existing Stage 7C lifecycle event/debit implementation; it does not add a
second ledger, WAC allocator, facility/SKU resolver or Finance cost source.
Deployment only installs schema/code. It does not create a boundary or change
business data. Until the separately gated generation exists, a known-facility
missing-SKU row preserves the legacy fail-closed rollback: deployment cannot
quietly consume the historical suffix before T0 review. The schema initializer
commits only the inert tables, so the subsequent T0 planner stays query-only.

## Query-only T0 manifest

`apps/ff_pool_fbs_forward_recovery.py` defaults to `dry-run`. The production
source is opened with SQLite `mode=ro` and `PRAGMA query_only=ON`; planning may
apply the canonical lifecycle function only to a disposable in-memory copy.
The resulting machine-readable manifest binds:

- exact deployed SHA, active storage generation/schema and active Stage 7C
  cutover identity;
- old lifecycle cursor and source status maximum `C` observed at T0;
- every stable order/status/identity business row in `(old cursor, C]`, its
  exact before state and stable digest;
- exact current target-location WAC evidence, predicted event/quarantine and
  quantity/capital after-images;
- pinned pre-existing handoff/debit identities and frozen WAC digest;
- private mode-`0600` before-image, atomic rollback, query-only ambiguous-
  transport readback, idempotency and non-target invariants.

`generated_at`, collector/publisher freshness, cursor `updated_at`, volatile
poll timestamps and the current global maximum above `C` are not fingerprint
material. Stable business dates/revisions/status digests and exact pinned
identity evidence are fingerprint material. Thus append-only observations
`C+1..N` do not change the reviewed manifest, while a changed target row,
mapping/cutover identity, target WAC or storage generation fails closed.

## Gated apply and two lanes

Apply is a separate explicit command and requires the exact post-deploy owner
gate, manifest fingerprint, actor and approval reference. Under the canonical
warehouse writer lock and one `BEGIN IMMEDIATE` transaction it:

1. repeats target-scoped CAS without comparing the moving global source max;
2. appends one immutable generation at `C`, initializes a separate forward
   cursor to `C` so ordinary processing begins at `C+1`;
3. processes only the manifest status identities `<= C` through the existing
   lifecycle reservation/debit/quarantine implementation without updating or
   rewinding either live cursor;
4. appends exact recovery target/result evidence and commits the generation,
   recovery and lifecycle effects atomically.

The ordinary processor then reads only new statuses above the forward cursor.
Historical unresolved identities remain in the existing pending/quarantine
lane and cannot consume the forward retry budget. A missing known-facility SKU
therefore creates no debit, capital or WAC and cannot block later valid rows.
Post-cutoff observations never enter recovery target rows.

The recovery is retry-safe through manifest and per-status event identities.
After the one authorized apply, `verify-noop` compares the reviewed manifest
with the completed durable readback on a query-only connection and proves
`repeat_submit_performed=false` / `would_write=false`; it does not issue a
second apply. If the client loses the response after commit, the only supported
next action is query-only `readback` and then `verify-noop`; no second submit is
inferred from transport state.
Past fulfilled events/frozen WAC remain append-only. Apply reconciliation
proves exact target quantity/capital deltas and that rows outside the target
were unchanged inside the serialized transaction.

## Production boundary

No dry-run or apply is performed by repository tests or release deployment.
After exact-SHA deploy, query-only dry-run may prepare the manifest. Production
apply still requires the separate exact fingerprinted human gate. A different
inventory-history backfill, timer change, manual refresh, mapping alias or
historical Finance/Partner rewrite remains outside this runner.

## Verification

`apps/ff_pool_fbs_forward_recovery_smoke.py` uses synthetic data only and
proves continuous `C+1..N` ingress does not invalidate the reviewed `<= C`
target; forward processing while an old SKU stays quarantined; no duplicate
debit/capital across lanes; post-cutoff exclusion; freshness-insensitive stable
digests; inert pre-gate rollback; target-WAC CAS failure; query-only repeat
no-op proof without a second submit; ambiguous-transport readback;
past fulfilled immutability; and exact quantity/capital/non-target invariants.
