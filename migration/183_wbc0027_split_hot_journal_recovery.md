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

## Operational boundary

The release may preserve the active barrier, but it must not invoke the recovery
submit. After exact runtime reconciliation, create and seal one fresh dry-run
plan and stop at the production-data recovery gate. Following a separately
accepted exact manifest, submit once, read back the detached job, continue the
same outer-baseline abort restore, release the barrier, and establish a fresh
maintenance window. Any identity, journal, capacity, writer, digest, or counter
drift fails closed.
