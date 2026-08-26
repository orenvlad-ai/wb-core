# Migration 158 — Root storage Stage 0

Status: authoritative root-capacity, large-writer admission and journald
retention activation contract. This migration does not authorize cleanup,
archival, compression, movement or relocation of any non-journal file and does
not introduce the future full capacity-reservation ledger.

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
root-storage registry. The five-minute repo-owned systemd monitor executes
`apps/root_storage_policy.py status`, publishes JSON at
`/run/wb-core-root-storage/status.json`, emits the same warning/critical/hard
alert payload to journald, and scans the configured root directories for files
at least 256 MiB. A large artifact/backup/evidence path that matches no exact
registered producer is a critical `unregistered_large_root_producer` alert.
No status scan deletes, compresses, moves or opens SQLite.

## Journald retention

The versioned repo-owned drop-in is
`artifacts/registry_upload_http_entrypoint/journald/60-wb-core-root-retention.conf`:

```ini
[Journal]
SystemMaxUse=2G
SystemKeepFree=15G
MaxRetentionSec=14day
```

For systemd 255, the `G` suffix is binary, so the first two settings are
exactly 2 GiB and 15 GiB. `SystemMaxUse` and `SystemKeepFree` are jointly
enforced through the smaller effective limit; only archived journal files are
removed for space limits. When KeepFree is already violated at journald start,
systemd raises that limit to observed free space and does not retroactively
delete existing journals merely to reach the configured KeepFree floor.
`14day` parses to exactly 1,209,600 seconds. Activation fails before config
installation unless the production major is exactly 255 and those semantics
are recorded with the deployed binary/man-page digests.

The canonical deploy adapter runs `journald-activate` once per exact config
digest. It does not call `journalctl --vacuum-*`, does not unlink a journal and
does not run a manual cleanup. Before the one allowed
`systemctl restart systemd-journald.service`, it writes a private mode-0600
manifest below `/var/lib/wb-core/root-storage-policy/activations/`. Each entry
records path, device, inode, size, mtime, journal header state/file/machine id,
head/tail realtime, exact cutoff, filename/archive classification, `/proc/*/fd`
opener proof and incident/forensic/legal-hold proof. Every other regular file
below the journal root is inventoried as an immutable non-target.

Eligibility is the intersection of all of these facts:

- current machine-id journal directory;
- matching machine id in the journal header;
- archived `@…journal` filename;
- journal header state `ARCHIVED`;
- tail realtime strictly older than the fresh 14-day cutoff;
- no matching active incident, forensic or legal hold.

The exact device+inode opener snapshot is retained in every entry; an opener
does not widen or narrow the authorized age/hold subset. Immediately before
config installation, every eligible and immutable protected identity, the
hold-registry proof and the cutoff classification are rechecked. Drift stops
before the single restart.

The supported optional hold registry is
`/etc/wb-core/journal-retention-holds.json`, contract
`wb_core_journal_retention_holds_v1`. An invalid registry fails closed. An
active `incident`, `forensic` or `legal` hold can bind all journals, exact paths
or exact `device:inode` identities. Any aged archived entry with a matching
hold blocks activation before config installation and returns the exact hold
and callback; held files are never silently omitted from the activation proof.

The manifest stores an exact eligible count/bytes/digest and a protected
non-target digest. Before restart the runner durably records submit intent and
`restart_submit_count=1`. A transport loss never permits another restart:
only `journald-retention-readback` is allowed. Reconciliation classifies every
manifest entry as exact deleted, exact retained or ambiguous; requires every
pre-existing protected path/device/inode to remain; requires exact immutable
younger/held/foreign journal identity; proves the service generation changed;
and reads effective settings plus journal/root/backup/generation status.
Ambiguous eligible identity, protected deletion/movement/drift, wrong config,
wrong service generation or a second submit fails closed.

## Deploy and acceptance

`root_storage_policy_file` in the active hosted target binds the policy to the
canonical target. The normal exact-merge `live_runtime` deploy performs:

1. repo sync and deployed-SHA marker;
2. root status plus unregistered-producer gate;
3. private journald manifest and at-most-once activation;
4. ordinary bounded deploy, managed monitor service/timer install and runtime
   probes;
5. query-only journald reconciliation and root-status readback.

Production acceptance requires exact merged/deployed SHA; manifest digest,
count and bytes; exact deleted/retained/ambiguous reconciliation; zero active
holds or the exact hold callback; effective 2 GiB / 15 GiB / 14-day settings;
journal disk usage; root/backup/generation available bytes, inodes, mount ids,
sources, types and UUIDs; synthetic hard/predicted-free admission proof;
published status/timer and zero unregistered large root producers; core
service/timer/refresh/business health; and proof that no non-target journal or
other file/database/backup/evidence datum was deleted or moved.

## Strict exclusions

This stage does not run raw SQLite copies, Promo GC or terminalization,
producer-specific retention migration, Finance/warehouse/monolith/generation/
Autoanswers data mutation, another backup cleanup, a new destination/mount, or
capacity expansion. It never removes, archives, compresses or relocates any
file outside the exact expired archived-journal manifest subset. Tests use
temporary sparse fixtures only and create no real large production file.

## Required checks

- `python3 apps/root_storage_policy_smoke.py`
- `python3 apps/storage_recovery_writer_inventory_static_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`
- all suites selected by the exact-base PR planner
- exact Release Runner receipt and production readback above
