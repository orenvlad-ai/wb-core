# Migration 158 — Root storage Stage 0

Status: authoritative root-capacity/large-writer admission contract, the
block-005 activation of its read-only monitor, and the bounded block-004
rollback of the failed journald-retention activation. The active target does
not retain a journald retention override. This migration does not authorize
cleanup, archival, compression, movement or relocation of any journal or
non-journal file and does not introduce the future full capacity-reservation
ledger.

WBC0008 block 029 extends this same monitor/admission contour across the
existing root, backup and generation filesystems without changing the Stage-0
thresholds. Its canonical producer routing, dynamic Finance+8-GiB backup
reserve, generation reserve, lifecycle matrix and static literal guard are
authoritative in
`migration/160_root_storage_prevention_wbc0008_029.md`. The completed exact-six
operation and all Stage-0 journald evidence remain unchanged.

## Root policy

The canonical machine-readable policy is
`artifacts/registry_upload_http_entrypoint/root_storage_policy_v1.json`.
Available bytes use `f_bavail`, not nominal capacity or `f_bfree`.

- normal: at least 25 GiB available;
- below-normal advisory: 20–25 GiB;
- warning: below 20 GiB;
- critical: below 15 GiB;
- hard: below 12 GiB.

Every large discretionary artifact/debug/full-copy writer supplies an exact
registered owner, absolute destination and predicted output to
`packages.application.root_storage_policy.admit_root_write` before opening or
creating the destination file. A discretionary root write is denied below 12 GiB.
When predicted output is at least 256 MiB, it is also denied when predicted
free-after would be below 15 GiB. Unknown owner, output or destination fails
closed. Output on a proven distinct filesystem is reported but is not charged
to root.

Essential bounded business writes are a separate explicit classification.
They remain subject to their existing domain capacity, transaction, CAS,
recovery and readback guards and are not converted into discretionary work by
root pressure. This preserves ordinary business continuity without permitting
an unbounded backup/evidence writer.

The same versioned policy owns the non-target CAS resolver registry used by the
exact-six warm archive. Mutable treatment is available only to explicit
`active_mutable_canonical_stores` bindings with a registered essential owner,
one supported literal or StoreRegistry resolver and an explicit repo-owned
service access-role/mode matrix. The current Finance raw and operational stores
resolve through StoreRegistry; Autoanswers resolves through its literal
canonical path. This registry does not derive mutability from a filename or
broad directory pattern. Every FD must bind the exact device/inode and exact
healthy declared systemd MainPID; unknown mode, undeclared/non-MainPID/
PID-ambiguous service, role/mode drift or pathname-only matching fails closed.

`apps/storage_recovery_writer_inventory_static_smoke.py` retains the existing
SQLite backup-writer catalog and additionally fails when an observed backup
primitive lacks the common admission call or its owner is absent from the
root-storage registry. `apps/root_storage_policy.py status` publishes the same
warning/critical/hard and exact producer inventory read model without deleting,
compressing, moving or opening SQLite. Every payload binds the exact policy
digest and UTC collection time.

## Block-005 monitor activation and runbook

The active target manages `wb-core-root-storage-policy.service` and
`wb-core-root-storage-policy.timer`. The timer is enabled and runs the oneshot
service every five minutes; deploy also starts the oneshot once after unit
installation. The service atomically replaces the mode-0644,
server-owned artifact
`/var/lib/wb-core-root-storage-policy/status.json`; its state directory is
created by systemd. `root-storage-readback` validates the artifact's policy
binding, classification and maximum age of ten minutes without rescanning or
writing. A stale, malformed, future-dated or policy-mismatched artifact fails
closed.

The service uses `--fail-on-unregistered`: it still publishes the complete
status artifact and then returns nonzero if a large root file inside a
configured producer scan root has no exact registered path owner. A current
hard-capacity result is valid monitoring evidence, but carries
`safe_for_discretionary_root_writes=false`; admission, not the monitor, rejects
the actual write. The monitor contains no cleanup, retention, compression,
movement, SQLite-open or journald operation.

Operator recovery is bounded:

1. read live state with `root-storage-status` and the durable artifact with
   `root-storage-readback`;
2. for a stale artifact inspect the service/timer result and run the same
   repo-owned oneshot once, then repeat readback;
3. for an unregistered large file, do not bypass the gate or delete/move the
   file: identify the exact producer and add an authoritative classification
   and path pattern through an ordinary reviewed release;
4. for a denied discretionary writer, do not retry blindly. Resume only after
   a separately authorized capacity/file-lifecycle action restores the policy
   boundary, or after a proven distinct-filesystem destination is introduced
   through its own target/capacity change;
5. never reclassify an artifact/debug/full-copy writer as essential merely to
   bypass pressure. Essential bounded business writes retain their domain
   capacity, transaction, CAS, restore and readback guards.

The AST inventory publishes exact source, function, backup line, primitive,
admission line and owner for every observed SQLite full-copy entrypoint. It
fails when an entrypoint lacks a preceding common admission call, uses an
unregistered literal owner, or is absent from the reviewed writer catalog.
Scheduled full-monolith writers remain forbidden; bounded warehouse
checkpoints, target before-images, Finance split restore sets and ordinary
business stores keep their explicit essential classifications.

## Failed block-003 activation

Block 003 introduced the repo-owned drop-in
`artifacts/registry_upload_http_entrypoint/journald/60-wb-core-root-retention.conf`:

```ini
[Journal]
SystemMaxUse=2G
SystemKeepFree=15G
MaxRetentionSec=14day
```

The block-003 Release Runner installed this exact file and submitted one
journald restart. Its fresh manifest contained zero eligible files, but
query-only readback found one protected 128 MiB younger archived journal
missing. The service PID did transition and the settings became effective, but
that protected deletion made PR #1054 terminal `blocked`. The missing archive
is not restored or synthesized. The old activation evidence remains immutable
under `/var/lib/wb-core/root-storage-policy/activations/`; it is never replayed
as corrective proof.

## Block-004 corrective removal

The current machine policy has `journald.mode=remove_block_003_dropin`. The
legacy retention source remains only as dormant repository evidence and is not
an installation input. Canonical deploy calls `journald-corrective-remove`.
Before mutation it requires the exact active drop-in path and SHA-256, the
three exact block-003 effective values, an active journald PID, fresh complete
regular-file inventory below `/var/log/journal`, opener evidence, exact
journal headers, protected identity/inventory digests and root/backup/generation
capacity, inode and mount evidence.

The operation writes a private mode-0600 manifest and durable state below
`/var/lib/wb-core/root-storage-policy/corrections/`. It then unlinks exactly
`/etc/systemd/journald.conf.d/60-wb-core-root-retention.conf`, fsyncs that
directory, records removal, records `restart_submit_count=1`, and submits
exactly one `systemctl restart systemd-journald.service`. It contains no vacuum,
rotate, archive, compression or journal unlink command. A transport ambiguity
may invoke only `journald-corrective-readback`; the drop-in removal and restart
are never retried.

Query-only reconciliation requires the drop-in absent, no remaining effective
`SystemMaxUse`, `SystemKeepFree` or `MaxRetentionSec` override, one attributed
PID transition, an active journald service and every pre-existing journal-root
device/inode still present. A current journal may appear under its normal
post-restart archived name with the same device/inode; immutable journal and
non-journal movement, shrinkage, deletion or drift fails closed. Newly created
current journal files are inventoried separately and never mask a missing
pre-existing identity.

## Block-006 bounded warm archive

After the block-005 monitor activation, block 006 is a separate
production-mutation contour. It may replace only the six literal inactive raw
SQLite recovery/evidence copies with exact independently restored warm archives
on the existing `/dev/sdb1` backup mount. It does not alter this policy,
journald, Promo, business data or generation storage. Its full material CAS,
capacity, crash-resume, one-submit and terminal readback contract is
`migration/159_root_storage_warm_archive_wbc0008_006.md`. Block 007 is not
authorized merely by block 006 completion; its separately accepted correction
may only repair pre-submit repeatability/readiness and execute the unchanged
literal six-target contract. It grants no later storage stage or new target.
Block 012 is the same authorized exact-six contract: it separates immutable
non-target content CAS from mutable canonical identity topology so ordinary
same-inode business-store writes do not invalidate an unrelated long archive.
It does not relax exact target CAS or authorize any additional lifecycle path.

## Deploy and acceptance

`root_storage_policy_file` in the active hosted target binds the policy to the
canonical target. The normal exact-merge `live_runtime` deploy performs:

1. repo sync and deployed-SHA marker;
2. root status, durable artifact publication and unregistered-producer gate;
3. ordinary bounded deploy, including installation and activation of the
   root-monitor timer;
4. fresh artifact readback after managed-unit activation;
5. the already-established block-004 corrective boundary, which is an
   idempotent query-only readback no-op after its durable completion;
6. final query-only reconciliation.

Production acceptance requires exact merged/deployed SHA; manifest digest,
pre/post journal inventory and protected identity digests; zero deleted or
drifted pre-existing journal-root files; drop-in absence; empty effective values
for `SystemMaxUse`, `SystemKeepFree` and `MaxRetentionSec`; exactly one recorded
unlink and restart submit; exactly one attributed PID transition; journal disk
usage; root/backup/generation available bytes, inodes, mount ids, sources,
types and UUIDs; exact root-status readback with zero unregistered large root
producers; installed repo-owned monitor units, enabled/active timer, successful
oneshot result and fresh policy-bound status artifact; Registry HTTP, AI API,
Data MCP and applicable canonical service health. Block 005 separately proves
that journald PID, effective configuration and file identity inventory did not
change during this monitor activation.

## Strict exclusions

This monitor activation does not restore the already missing 128 MiB archive,
change journald configuration or submit a journald restart. It does not run raw
SQLite copies, Promo GC or terminalization,
producer-specific retention migration, Finance/warehouse/monolith/generation/
Autoanswers data mutation, another backup cleanup, a new destination/mount, or
capacity expansion. It introduces no replacement retention or GC design and
never removes, archives, compresses or relocates any file. Tests use temporary
sparse fixtures only and create no real large production file.

## Required checks

- `python3 apps/root_storage_policy_smoke.py`
- `python3 apps/storage_recovery_writer_inventory_static_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`
- all suites selected by the exact-base PR planner
- exact Release Runner receipt, `root-storage-readback`, managed-unit readback,
  journald non-change proof and production service reconciliation above
