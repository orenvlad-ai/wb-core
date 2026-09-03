# WBC0027 split-generation hot-journal recovery

## Scope

This contour exists only for the reviewed WBC0027 S047 incident: the canonical
storage manifest is in exact `cutover/split` state, business maintenance is in
the durable partial-abort epoch 2, the external write barrier is still
`acquiring`, and the selected operational database has one validated hot DELETE
rollback journal. It is not a general SQLite repair facility.

## Deploy continuity

The hosted deploy reconciler reads the external barrier and durable maintenance
state before any managed-unit enable/restart. With no active barrier it issues
the legacy enable and restart inventories unchanged. For the exact incident
state it proves all six captured pause-owned timers disabled/inactive and their
services terminal, then omits only those six from the enable/restart calls.
Missing, malformed, held, differently bound, or non-quiescent state fails before
any `systemctl` mutation.

## Recovery contract

`sqlite-hot-journal-recovery-dry-run` is the default and is query-only. It binds
the runtime SHA, barrier/window/fingerprint, durable abort epoch, generation
manifest, DB/journal identities and hashes, exact 169-record journal overlay,
Finance raw store, operation counters, writer/timer/lock/process boundaries,
`zstd` binary, compressed allocation, and Finance reserve. The expected
post-recovery DB SHA is streamed from the reviewed journal overlay.

Apply is available only through the existing server-owned
`wb-core-storage-recovery-sanitation@.service` detached worker and a caller-known
64-hex job id. Submit binds the reviewed plan bytes, plan fingerprint, deployed
SHA, and immutable approval reference. A reused job id cannot be rebound and an
ambiguous submit is reconciled by status only, never resubmitted blindly.

Before SQLite is opened read-write, the worker creates a bit-exact `zstd -T1 -1`
capsule of both database and journal on the distinct backup filesystem, fsyncs
it, and verifies each decompressed size and SHA against the sources. Capacity
must retain the exact Finance reserve plus evidence envelope. SQLite then owns
rollback through its pager; no DML, DDL, checkpoint, or ad-hoc SQL is issued.

Success requires journal absence, the exact overlay-derived physical DB SHA,
`integrity_check=ok`, zero foreign-key violations, unchanged Finance raw and
generation-manifest files, and zero WBC0027 operation counters. Only that result
writes the recovery marker accepted by the partial-abort continuation. Normal
maintenance Apply/restore semantics and all collector/order logic are unchanged.

## SQLite-owned implicit rollback reconciliation

If the journal has already been consumed by SQLite before the reviewed worker
submitted, the normal recovery contour remains closed and is never retried.
The incident-only `sqlite-hot-journal-reconcile-existing-*` mode instead binds
the sealed pre-rollback DB/journal identity, header, all 169 checksums and page
list, plus the reviewed overlay SHA. It requires the same generation, device,
inode and size; two stable current physical hashes; query-only integrity/FK;
unchanged Finance raw/manifest files; the exact acquiring barrier, abort epoch,
paused writers, empty business-writer timeline and zero operation counters.
The full `integrity_check` and `foreign_key_check` run only against an
operation-bounded O_EXCL/0600 byte copy on the dedicated `recovery_scratch`
filesystem. One nonblocking allocation lock admits at most one copy. The copy
is opened by SQLite through its private staging name, then immediately unlinked
before either check; every failure path performs cleanup and requires zero
leftover. The source/copy
size and SHA-256 must match the stable current database identity, the copy is
byte-, time-, cache- and throughput-bounded, and scratch admission requires
available bytes greater than or equal to the current source size plus exactly
8 GiB. The Finance reserve on the backup filesystem remains exact and covers
only the durable evidence envelope, not the qualification peak. The unlinked
copy has no durable path during checks and is closed after the
checks; corruption, FK evidence, timeout, capacity, copy identity or source
drift fails closed. The live database still receives the typed immutable
query-only semantic read and final physical CAS, but never the ad-hoc full
integrity scan.

## WBC0035 recovery-scratch bootstrap dependency

The active target contract binds the separate provider disk by stable parent
and partition by-id, serial `vde`, exact size `53687091200`, current major:minor
`8:48`, one GPT partition and the reviewed ext4 UUID. Release may land while the
disk is proven blank: deploy status accepts only that exact `bootstrap-pending`
state and performs no disk write. Initialization is a distinct repo-owned
dry-run, one-submit Apply and query-only readback. After completion, missing or
wrong device, UUID, mount options, mountpoint, fstab line or separation from the
root/backup/generation devices fails closed.

This filesystem is temporary emergency/recovery verification space only, with
zero retention. It must never hold Finance data, durable evidence, recovery
markers, business databases or any other business data. The reconciliation
plan/result/marker remain on canonical backup; only the anonymous qualification
copy uses scratch, and successful or failed qualification leaves it empty.

Before a reviewed plan exists, the deployed
`sqlite-hot-journal-reconcile-existing-rehearsal` command is the only admitted
consolidated diagnostic. It resolves the exact active barrier server-side and
returns all eight execution-protocol phases. It creates no private plan,
operation directory, detached job, recovery marker or submit, and verifies the
barrier fingerprint and timer states are unchanged across the readback.

The only admitted post-rollback operational writes are identity- and
request-digest-bound Change Registry jobs from the trusted release activation
actor or the natural systemd schedule actor, their checkpoint/source-manifest
rows, and the seller-session source-health UPSERT. Natural observer jobs retain
the exact accepted/running/complete, terminal fact-count zero contract. The
typed incident exception manifest binds exactly two immutable scheduled jobs
to the historical observer/deployed contract and to full job/event/checkpoint/
manifest/fact/link row-set digests: the 10:00Z job is
accepted/running/partial with a partial checkpoint, `ads=partial`,
`prices=complete` and zero facts; the 12:00Z job is
accepted/running/complete with exactly the two checkpoint-linked bid and
campaign facts. There is no third exception. Any identity or digest drift,
extra/missing/reordered event, changed checkpoint/manifest, fact/link drift,
unknown state/source/actor, or fact on any generic job fails closed.

The reviewed plan records an exact `(requested_at, job_id)` observer cutoff,
the cutoff job's exact terminal-event timestamp, and a canonical digest of
every observer-owned row through that terminal boundary. This includes
identity incidents emitted after the job request but before its terminal
event. A later
apply or marker readback requires that prefix unchanged, while allowing only a
strictly later tail that independently passes the generic scheduled
complete/fact-zero validator. Thus the continuous observer may append truthful
jobs without freezing a global row count, but cannot rewrite history or widen
the exception. All remaining SQLite tables are streamed in canonical sorted
table/column/row order using typed SQLite scalar encoding and bound by one
SHA-256 digest. Journal reappearance or an ambiguous timeline remains closed.

The detached one-submit reconcile worker opens the database only as
`mode=ro&immutable=1` with `query_only=ON`. Its sole writes are durable private
evidence, result and recovery marker files. The partial-abort continuation
accepts `mode=reconciled_existing` only after independently re-reading the same
non-operational digest, the exact cutoff prefix and a newly validated generic
tail. It issues no SQLite recovery, DML, DDL or checkpoint; the ordinary
hot-journal result shape and normal maintenance Apply/restore behavior remain
unchanged.

## Operational boundary

The release may preserve the active barrier, but it must not invoke the recovery
submit. After exact runtime reconciliation, create and seal one fresh dry-run
plan and stop at the production-data recovery gate. Following a separately
accepted exact manifest, submit once, read back the detached job, continue the
same outer-baseline abort restore, release the barrier, and establish a fresh
maintenance window. Any identity, journal, capacity, writer, digest, or counter
drift fails closed.
