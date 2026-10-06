# Shared heavy admission: first Finance backup integration

This change integrates **only full post-cutover Finance backup**. Legacy API,
warehouse, Finance source/cost, FBS and standalone history producers are not yet
integrated. It does not establish global serialization or activate a cycle,
timer, schedule profile or new cadence.

## Lease API

`business_data_heavy_admission.HeavyAdmissionLease(runtime_dir, *, operation,
independent=False)` acquires an independent maintenance SH lease, then the
private `.business-data-heavy-admission.lock` with EX/nonblocking. Writer
admission initializes private lock infrastructure when missing; read/status does
not provision it. Busy acquisition closes its SH immediately. No waiting,
polling or retry happens inside this helper.

`lease.entered()` establishes trusted live ownership in the sole worker thread;
`lease.close()` belongs in that worker's finally. Creation in the parent and
transfer to one worker are supported. Concurrent/different thread entry is
rejected. Same-thread nested `heavy_admitted(runtime_dir, operation=...)` borrows
the root owner without acquiring/unlocking another flock. Forked, closed and
copied-to-another-thread context cannot provide authority. Closing a fork's
duplicate never explicitly unlocks the parent's shared file description.

After uncertain `Thread.start()`, use `lease.close_if_unstarted(thread)`: it closes
only with the existing CPython no-child proof. A live or uncertain child retains
the lease until its finally. Never close a possibly accepted worker's lease just
because the start call raised. Client job IDs, env values and names do not grant
ownership.

`current_heavy_owner` and `require_heavy_owner` validate actual live in-process,
current-thread ownership. `heavy_admission_status` is a bounded read-only
nonblocking probe. Missing/unsafe infrastructure is unproven, not idle.

Lock order is maintenance SH → heavy EX NB → domain locks → short SQLite writer
transaction. Do not call subprocess writers while holding parent ownership and
then wait for them to reacquire. The heavy lease is not a replacement for
warehouse/Finance domain locks or SQLite CAS.

## Covered canonical backup boundaries

`FinanceStorageBackupRotation.build_plan`, `apply` and full `readback` acquire or
reenter the same owner. A busy direct method raises `FinanceBackupDeferred` with
a bounded result receipt; it never returns a fake reviewed plan. The direct
post-cutover `finance_storage_split` snapshot-retention paths therefore share
the boundary. CLI busy prints the receipt without overwriting `--output`.

`scheduled_rotation` holds one lease across exact pending producer recovery or
fresh build-plan → apply → selection → verification → GC/terminal result.
The existing Finance snapshot-retention EX lock remains nested. Capture and
verify are not split into independent windows. Existing approval, canonical
split, mount, retention/copy/capacity checks and before-write checkpoints remain.
Six-day minimum, seven-day age/RPO, four-hour **recovery** RTO, count and byte caps
are unchanged. Exact nonterminal reviewed plan/deployed SHA recovery remains;
another SHA is rejected. Unknown outcomes are read/reconciled from that same
transaction, not submitted as a new replacement.

## Cheap priority and durable deferred request

`finance_backup_admission.backup_admission_priority(runtime_dir, *, now=None,
duration_budget_seconds=None, next_actor_budget_seconds=0)` reads private policy,
current selector, selected manifest, existing transaction metadata and source
stat/sidecar identities. It opens no SQLite connection and hashes no database or
backup bytes. Metadata is bounded to 2 MiB/file, 128 transaction entries and
16 MiB total transaction metadata; unknown/unsafe/excessive inventory returns
`ready=False, priority=True`. These bounds do not permit deletion or repair.

Multiple pending records remain unproven for another heavy producer and strictly
blocked for scheduled resume. Manual canonical build/apply/readback instead read
only the valid private intent request ID before their detailed existing guards.
They can prove and terminalize multiple exact zero-mutation `started` records;
unsafe phases, copy/deletion evidence or changed transaction identity remain
blocked by canonical recovery rules. The cheap priority hint is not a permission
gate for manual proof or safe supersession. Invalid private intent still fails
closed, and acknowledgement still matches only the observed request ID.

The result separates a cheap source-change hint from authoritative due. After
lease acquisition the existing actual plan is recomputed. Waiting request or
nonterminal transaction takes priority over a future cycle. Future integrated
cycle admission must recheck this result under its heavy lease before effects;
reading a hint outside the lease is not reservation or scheduling.

A busy due approved backup persists one private
`.finance-backup-admission.json` intent, serialized by a short nonblocking
metadata lock. The same current/policy identity retains one request ID across
contenders and restart. Success or fresh authoritative not_due resolves only
the request observed before that operation. No new universal queue, manual
operation approval or backup transaction is created by deferral. An inert/not-due
policy returns deferred without a due intent. Failure to prove/save metadata is
an explicit error, not silently successful deferral.

The deadline uses the data's captured_at, not selected_at/verified_at.
An independent explicit duration budget permits a latest-safe-start calculation;
unknown budget means `latest_safe_start_at=None, timing_proven=False`. Four-hour
RTO and a historical backup duration are not a duration SLA. A caller may supply
the next actor's bounded duration to reserve a slot before it crosses the latest
safe start. No timer/cycle consumer is activated in this PR, so the metadata alone
does not prove production starvation prevention.

Existing unchanged-source policy becomes due at hard age, not automatically at
latest-safe-start. Reservation does not secretly force an earlier replacement.
If an unchanged source is still not_due in a reserved window, terminal RPO timing
is not proven by this admission layer. A guarantee of a newly verified copy before
the hard age needs an explicitly reviewed due-policy change and independent
duration bound; this PR preserves the existing replacement predicate.

## Coherence during parallel short commits

The existing `_guard` proves selected canonical split/generation, inactive
shadow, zero raw/outbox/consumer/live-tail lag and no actionable dead letters.
`_assert_plan_cas` repeats manifest/generation/shadow, source **path/device/inode**
and backup mount/device identity. It does **not** compare size, mtime or sidecars:
those are included in due/capture evidence, not this CAS.

The existing copy path keeps read-only/query-only SQLite capture, integrity/FK/
logical proofs, resumed-copy comparison, post-copy guard, manifest byte identity,
isolated restore registry/logical and watermark proofs, protected non-target
identity and current-copy-safe selection/GC. None is weakened or made stricter.
An ordinary short operational commit does not necessarily fail this CAS. The
helper does not prove one global point-in-time snapshot of every operational
write and both stores, or a global capture drain. Preserve any required existing
capture boundary; do not claim all backup time is SQLite locked or all other
writers excluded. Independent stores/short guarded commits retain their own
guards; later shared-admission integrations must be reviewed separately.

## Offline validation

`apps/business_data_heavy_admission_smoke.py` uses temporary fixtures and real
processes: conflict, same-thread reentry, one-worker lifetime, uncertain start,
copied context, fork no-authority/no-parent-unlock, SH release on busy, bounded
due/deadline/unknown-budget read, deferred request restart/deduplication,
canonical plan/apply/readback/scheduled collision and exact partial recovery
without another copy submit. Two safe started transactions exercise manual
canonical proof/apply/readback, strict scheduled ambiguity and unsafe-phase
refusal; invalid private intent remains blocked before the manual body.
Existing Finance backup rotation and Finance split
retention smokes verify preservation of original policy/recovery/caps behavior.
No production, WB request or real backup is required by these tests.
