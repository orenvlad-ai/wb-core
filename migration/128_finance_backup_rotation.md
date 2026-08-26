# Migration 128 — post-cutover Finance backup rotation

Status: **repository implementation complete; production capability remains
inert until this production-mutation PR is merged, deployed and its exact
reviewed cleanup plan receives the required human gate**. The original
monolith and every object below the dedicated Finance generation filesystem
remain protected non-targets. Production cleanup evidence is appended here
only after the deployed runner reaches terminal readback.

## Fresh production baseline

The 2026-07-31 query-only preflight, against the canonical active Europe
target and deployed SHA `dc605b6e1255cedb5a884aa9e35990a060e50368`, found:

| Filesystem / family | Current evidence |
|---|---:|
| root `/dev/sda1` | capacity `82,086,711,296`; available `5,510,754,304`; 94% used |
| backup `/dev/sdb1` (`wb-core-backups`) | capacity `105,087,164,416`; available `15,990,616,064`; 84% used |
| generations `/dev/sdc1` (`wb-finance-gen`) | capacity `105,087,164,416`; available `42,735,882,240`; 59% used |
| root Finance migration snapshots | 3 sets; allocated `38,018,617,344` bytes |
| backup Finance migration snapshots | 6 sets; allocated `75,053,555,712` bytes |
| selected split generation `c54072027f14f90b374b` | raw `16,603,471,872`; operational `2,011,443,200` bytes |
| original monolith | `12,659,240,960` bytes; no opener; protected |

The split was healthy: `2,479,529` raw rows, zero pending outbox, consumer lag,
cursor mismatch, shadow mismatch and actionable dead letter. The selected raw
and operational files are on `/dev/sdc1`; the registry HTTP MainPID owns the
expected handles. The released business barrier and restored writer/timer
state were independently read back.

Protected `/dev/sdc1` non-targets are the selected `c540…` generation, prior
`29c15…` generation and both rollback-monolith generations. This migration has
no deletion primitive whose resolved path can enter `generations/` or the
original `registry_upload_runtime.sqlite3`.

For the unrelated WBC0008 exact-six lifecycle, the selected Finance raw and
operational files are active mutable canonical stores resolved by
StoreRegistry. Their ordinary same-inode writes may change content/size/mtime,
but path, mount/device, inode/type, owner/classification, generation identity
and expected owning-service/open-handle relationship remain fail-closed. This
does not grant warm-archive authority over a Finance generation or retained
restore set; Finance lifecycle locks still serialize the operation.

## Root cause and exact accumulation delta

The nine full snapshots correspond to nine distinct Finance split recovery
deploys, PRs `#850`, `#852`, `#859`, `#862`, `#863`, `#867`, `#868`, `#870`
and `#882`. Every recovery deploy invalidated the earlier reviewed
SHA/fingerprint and correctly captured a new coherent monolith source. The
first six were moved from root to the backup mount by
`FinanceStorageSnapshotRetention`; the last three remained on root.

That outcome was not governed by migration 123/125 T2 limits. The old Finance
runner was a migration-only, archive-first contour: it selected only an
implicit canonical monolith, rejected any populated generations root, copied
older-SHA snapshots to the archive and never garbage-collected that archive.
After cutover its canonical guard rejected the selected split completely.
There is no routine full-monolith scheduled writer; accumulation was caused by
the bounded recovery call sites plus the missing post-cutover lifecycle.

## Canonical policy

The existing `finance-storage-snapshot-retention-plan|apply|readback` contour
now dispatches by canonical manifest state. Pre-cutover monolith behavior
remains compatible. A selected post-cutover split uses the same canonical
snapshot root, lock, deterministic plan/result contracts, audit and hosted
wrapper; it is not a second retention registry.

The retained policy is:

- exactly one selected, verified raw+operational+source-manifest restore-set;
- at most two complete sets only while replacement is in progress;
- per-set hard cap `32 GiB`, temporary cap `64 GiB`, hard age seven days;
- daily due-check at 04:30 Europe/Moscow; a full replacement runs only after
  source change and a six-day minimum interval, or at the seven-day age cap;
- RPO at most seven days; bounded restore/readback RTO target four hours;
- hard free-space reserve `8 GiB` during copy, degraded watermark `30 GiB`;
- cleanup acceptance targets `40,000,000,000` root bytes and
  `60,000,000,000` backup bytes available;
- projected retained growth is zero over 30 and 90 days because superseded
  current bytes are removed after every successful selection.

The timer is safe to enable and start at deploy: without the private, fingerprinted
`retention_policy.json` created by the approved first apply it returns
`policy_inert` and creates or deletes no bytes. After activation it uses the
same implementation and lock as the reviewed cleanup. A non-terminal scheduled
transaction retains its reviewed plan and resumes that exact fingerprint; a
new plan cannot silently replace it.

The current-live target marks the timer for both `enable` and `restart`. Merely
creating the enablement symlink does not activate a newly installed timer in
the running systemd manager; the deploy restart is therefore required to
publish a real next trigger. Restart remains inert before the first approved
policy, and after policy activation its first persistent invocation must read
back as `not_due` unless the source/age policy independently requires a
replacement.

The Finance source timer checks hourly for newly available weekly reports; it
does not perform an hourly full ingest. The backup timer is a separate daily
due-check and cannot overlap a global business-data maintenance hold. The
cutover baseline contained three committed batches and zero pending outbox. A
separate incremental backup log was rejected because
raw already owns the durable ordered outbox and operational is its idempotent
projection; another log would create a second recovery state machine. The
bounded weekly full pair is simpler to restore, while missed raw source weeks
remain re-fetchable through the existing Finance acquisition contract.

## Atomic replacement and restore proof

The plan binds deployed SHA, manifest SHA, generation epoch, source
path/device/inode plus observed size/mtime, backup mount/device/options, all known root/archive/current
file identities, openers, count/byte/age limits and before/projected capacity.
Unknown, foreign, corrupt or drifted artifacts are recorded as protected and
are never deletion candidates. New inventory, selector, mount, manifest,
source identity or candidate SHA/stat drift fails before mutation.
Legacy archive byte validation preserves a declared zero-byte value instead of
treating it as a missing field. This matters for valid, fully hashed empty
SQLite WAL sidecars: their exact zero length and empty-file SHA-256 are checked
like every other file, while a genuinely missing size or mismatched hash still
classifies the whole archive as protected and blocks deletion.
Integrity fallback for a legacy `captured_unverified` archive additionally
requires an absent or exactly empty WAL and opens the closed main database with
SQLite `immutable=1`. The query-only plan therefore cannot create or refresh a
WAL/SHM read-mark after capturing the candidate identity. A non-empty WAL,
symlink or non-file sidecar remains protected rather than being ignored.

Apply performs one durable transaction:

1. If current backup capacity cannot hold the split plus reserve, release only
   the oldest verified legacy backup archives under exact per-file SHA/stat;
   keep the newest verified fallback and all root sources.
2. Copy operational first and raw second directly to a private partial set on
   the mounted backup filesystem. Root fallback is forbidden.
3. For each SQLite copy require `query_only`, full `integrity_check`, empty
   `foreign_key_check`, every logical-table row count/digest equal to its
   pinned source read transaction, the real data-capture timestamp and fsynced
   file/directory evidence. Crash resume preserves that original capture age;
   a later verification timestamp cannot make an old partial look fresh.
4. Reopen both backup-local files as an isolated restore target, require raw,
   raw-ack and operational cursors to agree, require zero pending
   outbox/mismatch/actionable dead letters, persist the original source
   manifest identity plus a backup-local manifest whose paths resolve the two
   retained files, and persist the watermarks read from the restored files.
   `StoreRegistry` must load that local manifest and open both stores
   query-only before the verified backup manifest is fsynced.
   The inactive post-cutover live-tail bridge cursor remains recorded as
   historical migration evidence but is explicitly non-applicable to retained
   backup lag; an enabled or drifting shadow state blocks planning/apply.
5. Rename the complete set and atomically replace `current.json`; read back
   the selector and its backup-manifest fingerprint.
6. Only after selection, remove the superseded current, remaining proven
   legacy backup archives and proven root snapshots. Manifest files are
   removed last. The original monolith and generation root are re-reported as
   untouched.

Every deletion is preceded by an external fsynced pending intent and exact
file identity recheck. Restart after copy, fsync, verification, selection or
deletion resumes the same phase. Missing bytes without the matching pending or
completed journal state are ambiguous and fail closed.

Restore and backup manifests use deterministic private `.pending` files. A
restart can publish an exact prepared payload, records the pending-file digest
as recovery evidence, and removes only an unreadable runner-owned pending file;
a valid payload from another transaction remains fail-closed. Operator health
uses bounded stat/manifest checks, while full byte hashes remain mandatory at
selection and terminal restore readback, so UI polling never re-hashes the
multi-gigabyte restore set.

The hosted durable worker never retries silently. A terminal failed
post-cutover apply can be continued only with the explicit
`finance-storage-snapshot-retention-resume` command carrying the same deployed
SHA, plan bytes, fingerprint and approval reference. It archives the exact
preceding failed status, reuses the same request/job identity, rejects a live
or ambiguous worker and caps the complete attempt chain at eight. Other
Finance mutation actions retain their observe-only transport behavior.

An apply that fails before every candidate release and before every copy may
leave only a `phase=started` runner transaction. A later deterministic plan
may terminalize that sidecar as `superseded_before_mutation` under the same
exclusive lock only when exact file SHA/stat, embedded plan/deployed SHA,
empty deletion intent/receipts, empty copy proofs, absent result/audit and
absent partial/final replacement paths all read back. The terminalization is
atomic, audited, idempotent and independently verified; it removes no snapshot
or backup bytes. Any transaction that reached a deletion, copy, manifest,
selection or unknown state remains exact-resume-only and fail-closed.

## Verification matrix

`apps/finance_storage_backup_rotation_smoke.py` proves two complete replacement
cycles, one-current/temporary-two invariants, zero steady-state growth,
integrity/FK/logical/restore readback, every copy/verify/select crash boundary,
concurrent-worker rejection, missing-mount/root-fallback rejection, ENOSPC
projection, file-digest drift before mutation, protected unknown files and a
corrupted new backup preserving the already-selected current. It additionally
proves that an exact zero-byte legacy WAL is verified rather than falsely
classified as corrupt, that fallback integrity does not mutate WAL/SHM
identity, and that only a proven zero-mutation `started` transaction can be
terminalized by a subsequent reviewed plan. The original monolith and all
generation paths remain unchanged.

The writer inventory classifies this as the only post-cutover full split
restore-set writer. Hosted apply uses the existing durable Finance transport
job; submit disconnect observes the same job instead of spawning a duplicate.
Storage health and the operator card expose retained identity/count/bytes, age,
RPO/RTO, available next-replacement capacity, last success/failure, 30/90-day
projection and blockers.

Required repository checks:

- `python3 apps/finance_storage_backup_rotation_smoke.py`;
- `python3 apps/finance_storage_split_smoke.py`;
- `python3 apps/finance_storage_transport_job_smoke.py`;
- `python3 apps/storage_recovery_writer_inventory_static_smoke.py`;
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`;
- `python3 apps/wb_finance_weekly_browser_smoke.py`.

## Production apply and terminal ledger

Merge/deploy only installs inert capability. Production apply requires the
fresh deployed-SHA query-only plan, exact fingerprint, active Finance migration
lease and separate trusted human approval. The apply/readback ledger must name
every removed and retained artifact, before/after bytes per filesystem,
restore digests/cursors, protected non-target digest, service/timer/barrier
state and public/operator UI evidence. Actions-owned production-mutation
terminalization is required before `release:production`.

No old-monolith or `/dev/sdc1` generation retirement is included. After this
task is terminal, a separate human-gated task may assess whether the original
monolith, prior split and rollback generations still satisfy a required
rollback contour; this document grants that task no deletion authority.
