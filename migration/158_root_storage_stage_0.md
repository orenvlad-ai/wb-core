# Migration 158 — Root storage Stage 0

Status: authoritative root-capacity/large-writer admission contract and the
bounded block-004 rollback of the failed journald-retention activation. The
active target does not retain a journald retention override. This migration
does not authorize cleanup, archival, compression, movement or relocation of
any journal or non-journal file and does not introduce the future full
capacity-reservation ledger.

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

`apps/storage_recovery_writer_inventory_static_smoke.py` retains the existing
SQLite backup-writer catalog and additionally fails when an observed backup
primitive lacks the common admission call or its owner is absent from the
root-storage registry. `apps/root_storage_policy.py status` publishes the same
warning/critical/hard and exact producer inventory read model without deleting,
compressing, moving or opening SQLite. The repo-owned monitor service/timer
artifacts remain dormant code: they are absent from the active target's
`managed_systemd_units`, and block 004 neither installs nor starts them and does
not claim monitor production acceptance.

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

## Deploy and acceptance

`root_storage_policy_file` in the active hosted target binds the policy to the
canonical target. The normal exact-merge `live_runtime` deploy performs:

1. repo sync and deployed-SHA marker;
2. root status plus unregistered-producer gate;
3. ordinary bounded deploy without root-monitor unit installation;
4. as the final operational submit, a private fresh journald inventory and one
   corrective drop-in removal/restart;
5. query-only corrective reconciliation and root-status readback.

Production acceptance requires exact merged/deployed SHA; manifest digest,
pre/post journal inventory and protected identity digests; zero deleted or
drifted pre-existing journal-root files; drop-in absence; empty effective values
for `SystemMaxUse`, `SystemKeepFree` and `MaxRetentionSec`; exactly one recorded
unlink and restart submit; exactly one attributed PID transition; journal disk
usage; root/backup/generation available bytes, inodes, mount ids, sources,
types and UUIDs; exact root-status readback with zero unregistered large root
producers; Registry HTTP, AI API and applicable canonical service health. The
root-storage monitor/timer is explicitly not an acceptance claim in block 004.

## Strict exclusions

This correction does not restore the already missing 128 MiB archive and does
not run raw SQLite copies, Promo GC or terminalization,
producer-specific retention migration, Finance/warehouse/monolith/generation/
Autoanswers data mutation, another backup cleanup, a new destination/mount, or
capacity expansion. It introduces no replacement retention or GC design and
never removes, archives, compresses or relocates any file except the exact
repo-owned journald drop-in. Tests use temporary sparse fixtures only and
create no real large production file.

## Required checks

- `python3 apps/root_storage_policy_smoke.py`
- `python3 apps/storage_recovery_writer_inventory_static_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`
- all suites selected by the exact-base PR planner
- exact Release Runner receipt and production readback above
