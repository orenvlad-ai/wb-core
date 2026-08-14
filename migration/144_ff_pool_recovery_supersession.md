# Migration 144 — exact Stage 7C recovery supersession

## Problem and boundary

A Stage 7C attempt can fail before its business transaction commits while the
central T2 checkpoint is already verified and the domain epoch has been safely
aborted. If a separately fingerprinted later Stage 7C attempt then applies,
passes exact readback and releases its epoch, the earlier
`failed_recoverable` row still truthfully records the old failure. Ordinary
warehouse publication must continue to fail closed while that row is
unresolved; silently ignoring or deleting it is forbidden.

This migration adds a narrow proof and mutation contract for that one case. It
does not repeat opening, historical FBS debit, facility seed, shipment receipt,
collector processing or WB writes. It does not edit functional or business
projection rows. Shipment `26GN527` remains `excluded_pending_receipt` until a
separate guided acceptance.

## Query-only proof

`apps/ff_pool_cutover_recovery_supersession.py dry-run` accepts one exact old
recovery id and opens the canonical database with `mode=ro` plus
`PRAGMA query_only=ON`. The plan is ready only when it proves:

1. exact Stage 7C T2 identity, failure next action and
   `mutation_running → failed_recoverable` transition;
2. both registered checkpoint artifacts are verified and byte-present, and a
   fresh bounded file read reproduces their registered size and SHA-256;
3. every old matching domain epoch is exactly `held → aborted`, and the old
   cutover has neither manifest nor recovery event;
4. one canonical later manifest has exact passing readback, aggregate/detail
   conservation, enabled reader and released barrier;
5. its persisted recovery events are exactly `applied → readback_passed`, name
   a later retained/released T2 recovery and share the manifest evidence;
6. the later recovery/manifest scope, after digest, non-target digest and
   released epoch chain all agree with the failed attempt.

The fingerprint covers immutable proof only. Continuous collector suffixes and
dynamic reservations are revalidated by canonical cutover readback but are not
copied into the owner gate. The plan records the deployed remediation runner
SHA, exact pre-change recovery/transition/artifact digests, expected record
counts and explicit non-targets.

## Owner-gated apply and recovery

Hosted `ff-pool-recovery-supersession-apply` requires an external reviewed
plan, its exact SHA-256 fingerprint, an owner authorization reference and an
actor. The wrapper verifies the active EU target and exact deployed runtime
SHA. Under the shared warehouse writer lock, apply rebuilds the query-only plan
and rejects any drift, then commits one transaction:

- insert one immutable append-only supersession relation with the complete
  proof and authorization reference;
- compare-and-set the target lifecycle from `failed_recoverable` to terminal
  `superseded`;
- append the matching lifecycle transition.

No prior row, transition, failure or artifact is deleted or retroactively
edited. The old rollback-availability metadata remains stored; terminal state
prevents blind rollback. Update/delete triggers protect the relation. The exact
repeat is idempotent. SQLite rollback handles a pre-commit failure; after an
ambiguous response, query-only readback verifies relation fingerprint,
transition, preserved artifacts and that the target no longer belongs to the
blocking T2 set. A later challenge requires a new reviewed append-only contract,
not reversal or row deletion.

## Publication and acceptance

After supersession readback, the normal hourly/manual warehouse mechanism owns
the next fresh functional and business publication. Acceptance must prove:

- the old operation is `superseded` with immutable relation to the later
  retained/released Stage 7C recovery and preserved checkpoint evidence;
- no unresolved recovery in this scope blocks the new T2 publication;
- public/business FF physical quantity equals the current Moscow/FBS
  facility-pool aggregate, while reservations/available come from that same
  version and are not added twice;
- aggregate/detail and quantity/capital conservation pass;
- Stage 7C remains applied/reconciled, its historical debit remains single,
  the collector/mappings and `26GN527` are unchanged, and WB writes are zero;
- the displayed error is cleared by successful publication or truthfully names
  a current error. A Recovery Policy blocker containing the diagnostic field
  `sqlite_busy_timeout_ms` must never render as a false upstream timeout.

Targeted regression is
`python3 apps/ff_pool_cutover_recovery_supersession_smoke.py`. It covers exact
success, query-only dry-run, immutable relation, idempotency, hard non-targets,
truthful error presentation and continued blocking for an unproven recovery.
