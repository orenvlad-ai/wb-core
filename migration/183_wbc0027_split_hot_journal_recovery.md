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

The reviewed plan records an exact `(requested_at, job_id)` observer cutoff and
a canonical digest of every observer-owned row through that cutoff. A later
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
