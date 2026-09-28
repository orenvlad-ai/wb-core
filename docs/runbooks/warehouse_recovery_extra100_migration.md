# Warehouse recovery on the existing extra100 disk

This is a **single, governed production cutover** for WBC0114. The code release
comes first and remains valid on the old `/dev/sdb1` topology. Do not lower the
root, Finance, backup, or T2 reserves. Do not run this procedure while any
warehouse sync or interactive warehouse writer is active. The extra disk is
already occupied by protected Proxy V4/buyout evidence; do not format it.

## Fixed identities and expected result

| Item | Contract |
| --- | --- |
| Existing backup | `/opt/wb-core-runtime/state/backups` on `/dev/sdb1`, UUID `bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0` |
| Extra filesystem | `/mnt/wb-core-extra100` on `/dev/sdd`, ext4 UUID `9fcfe929-827e-4f81-b2ef-186fd794e671` |
| Protected existing archive | `/mnt/wb-core-extra100/proxy-v4-pr948` and `/opt/wb-core-runtime/state/backups/proxy-v4-pr948`; both must remain read-only from their served paths |
| Moved family | Entire `/opt/wb-core-runtime/state/backups/warehouse-recovery`, including retained, failed, held and superseded artifacts; keep this **literal path** after a bind from `/mnt/wb-core-extra100/warehouse-recovery` |
| Activation markers | Exact JSON payload from `warehouse_recovery_placement.placement_state` in both `/opt/wb-core-runtime/state/backups/.warehouse-recovery-extra100-active.json` (outside the bind) and `warehouse-recovery/.warehouse-recovery-extra100-active.json` (inside; write it to old underlay and new copy before binding) |

The storage policy carries an inactive fourth role until both markers exist.
After activation, status/admission require `/dev/sdd` UUID and rw mount flags;
the native T2 writer checks the bind identity **before** opening a recovery
transaction or creating a directory. The old underlay marker remains visible
if the bind disappears, preventing fallback to `/dev/sdb1`. The old source is
kept through full new-target/restore readback; only its exact verified files
may be retired afterward. The copy-stage free-space gain is not real until
that retirement completes.

## Preview: record the candidate, never submit from stale output

1. Confirm deployed code contains this switch; read runtime SHA, current
   storage status, Finance health, both mount UUIDs/options, filesystem sizes
   and inodes, active service/timer state, `findmnt` in all service mount
   namespaces, and exact warehouse job/write lock state. The hourly unit,
   registry HTTP service and any interactive writer must have no warehouse
   write in flight. Observe the two lock files
   `.warehouse-functional-job.lock` and `.warehouse-functional-sync.lock`;
   neither a PID file nor a journal row substitutes for `flock` ownership.
2. Make a read-only, immutable-source inventory with
   `python3 apps/warehouse_recovery_extra100_manifest.py scan --root /opt/wb-core-runtime/state/backups/warehouse-recovery --output <external-plan-path>`.
   The manifest has each relative path, size, SHA-256 and SQLite quick check.
   Save the plan fingerprint, source device and mount IDs, held checkpoint IDs,
   source `df -B1` and inode counts in the operation record. Recheck active
   writer state immediately before any apply step. Preview also includes a
   read-only exact inventory/digest of the Proxy archive at **both** paths.
3. Calculate temporary copy coexistence on extra: existing 8.270 GB archive,
   ~11.628 GB warehouse tree, next T2 peak ~5.043 GB, and an 8 GiB extra-role
   floor; these are separate from `/dev/sdb1`'s Finance next-copy + 8 GiB +
   4 GiB floor. Refresh the numbers from the actual candidate, not this note.

## Apply: one submit with a durable intent and held locks

Prepare an operation ID and a root-owned intent file outside both artifact
trees with the plan fingerprint, source/destination paths, UUIDs, source
manifest digest, service state, approval and current phase. Write and fsync it
once; subsequent commands must inspect this same intent, never create a new
operation to retry an ambiguous response. Only the migration owner advances
phases. Keep the shell/process holding **both actual `flock` descriptors** for
the entire cutover; stop/disable the warehouse timer, wait for its service to
be inactive, and prevent new manual starts. Verify no other warehouse writer
or reader has open handles on the to-be-bound directory. This is essential
because the job lock alone does not cover interactive writers.

1. Recheck `findmnt -no SOURCE,UUID,FSTYPE,OPTIONS` and `df -B1` for the exact
   source, extra mount, archive paths and target. Remount the existing extra
   ext4 filesystem rw only under the approved mount plan. Preserve the
   current archive bind and add a **read-only bind over the direct archive
   path** (or a demonstrably equivalent read-only mount namespace) before
   exposing the writable volume to application services. `findmnt -T` must
   report `ro` at both archive paths, while the new warehouse destination
   reports `rw,noatime,nodev,nosuid,noexec`. If either archive path is writable,
   stop before copying. Verify the archive digests did not change.
2. Create `/mnt/wb-core-extra100/warehouse-recovery` on the UUID-verified
   filesystem. Copy the **entire** source tree preserving ownership, modes,
   times, hard links and xattrs (for example, `rsync -aHAX --numeric-ids` under
   the held locks). Run `...manifest.py compare --manifest <external-plan-path>
   --root /mnt/wb-core-extra100/warehouse-recovery`. Re-run the same compare
   against the old source to prove it stayed unchanged. Do not rewrite stored
   checkpoint paths, manifests, selectors or registry rows.
3. Write the exact marker payload, with `fsync(file)` and `fsync(parent)`, to
   the old underlay recovery directory, then to the new extra directory, and
   lastly to the backup-parent outside-marker path. At this point the marker
   is durable intent: policy/writer deliberately fail until the bind is live.
   Never delete the outside or underlay marker to obtain a legacy fallback.
4. Bind `/mnt/wb-core-extra100/warehouse-recovery` onto the literal old
   runtime path. Add persistent fstab/systemd mount declarations for the
   UUID-backed extra filesystem, the warehouse bind and both read-only archive
   presentations, with explicit ordering/dependency. Preserve the existing
   Proxy bind. Validate **all** mount units with `findmnt` and `systemctl
   daemon-reload`; do not rely on `mount -a` alone. The warehouse sync and
   root-storage-policy units include `RequiresMountsFor` for the logical
   warehouse path; the native writer also validates UUID/mountpoint at write
   time. Test a service-style mount namespace, not just the operator shell.
5. Run copy compare again at the logical runtime path, inspect SQLite
   quick-check and recovery manifests, check at least the two protected T2
   checkpoint readbacks and their rollback eligibility without executing a
   production rollback. Check `WarehouseRecoveryRegistry.public_status()`,
   native capacity/retention projection, Finance health, archive hashes and
   both archive paths' `ro` flags. Generate a fresh root-storage status
   artifact and run `apps/root_storage_policy.py status-readback`; require
   fourth-role `identity_ok=true`, `reserve_breached=false`, overall `ok=true`
   and unchanged old-backup Finance floor. Verify boot/fstab declarations will
   restore the same topology, then release locks and re-enable the timer.

**Retirement is a separate exact phase of the same intent.** Keep an access
path to the old `/dev/sdb1` underlay while the new bind is live (a controlled
non-recursive parent bind or independently mounted old filesystem). Compare
the old underlay to the original manifest once more, list each exact path,
prove no readers, holds or new files, then remove only those exact old files
under the approved intent and verify `df -B1` on `/dev/sdb1`. Do not remove the
underlay activation marker or bind mountpoint. Read the result by operation ID;
an uncertain response means status/readback only, never repeat deletion.

## Failure, rollback and next release

Before old-source retirement, a failed copy/verify/mount can be backed out
under the same held locks: remove only the new bind, return archive mounts to
their original read-only state, keep both activation markers until a governed
rollback explicitly restores legacy placement and verifies the old source.
After any old-source retirement, **do not** unbind to use the old location;
restore from the verified extra copy or another approved recovery source. A
missing bind after activation must make the writer and status fail closed.
Read the durable intent and readback rather than reissuing any ambiguous
mount/copy/delete command. A governed release is complete only after its
receipt has `deployment_complete=true` on the activated topology.

Finance's active private policy remains at a 32 GiB per-set cap during this
cutover. After actual old-backup `df` and a fresh Finance source/next-copy
projection, a **separate** fingerprinted private-policy review may consider
34 GiB. That is a bounded candidate, not a code default: the 8 GiB Finance
hard reserve and 4 GiB global emergency remain unchanged. A 36 GiB cap would
consume almost the entire newly gained old-backup guard margin if both the
incumbent and next source grow to that size; it must not be applied blindly.
