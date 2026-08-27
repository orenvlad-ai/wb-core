# Root storage recurrence prevention — WBC0008 block 029

Status: authoritative bounded prevention contract after the completed exact-six
archive. This block changes future repository-owned storage routing, admission,
lifecycle classification and monitoring. It does not replay or modify the
exact-six operation, delete legacy material, retire Finance generations, run
Promo GC or change journald.

## Fresh production inventory

The bounded read-only inventory was collected on the canonical EU host at
`2026-08-27T14:28:59Z` through the deployed SHA
`1f2271c2d15ae17681acc37df054b9a2f8efc3a6`. `f_bavail`, inode and mount
identity are the admission facts.

| Role | Exact mount | Total bytes | Available bytes | Inodes free/total | Required reserve | Headroom after reserve |
|---|---|---:|---:|---:|---:|---:|
| root | `/dev/sda1` on `/`, ext4 UUID `d77f6a25-e90f-4292-a85d-9bcc1cecf9e2` | 82,086,711,296 | 37,192,888,320 | 10,067,698 / 10,354,688 | 26,843,545,600 | 10,349,342,720 |
| backup | `/dev/sdb1` on `/opt/wb-core-runtime/state/backups`, ext4 UUID `bd3d563f-e5ea-4e4a-a76a-be45e7f94ec0` | 105,087,164,416 | 50,754,703,360 | 6,553,438 / 6,553,600 | 42,198,454,272 | 8,556,249,088 |
| generation | `/dev/sdc1` on `/opt/wb-core-runtime/state/generations`, ext4 UUID `284b3362-b890-431d-a7da-7f0fcd2ee0a6` | 105,087,164,416 | 36,339,458,048 | 6,553,568 / 6,553,600 | 8,589,934,592 | 27,749,523,456 |

The backup reserve is the fresh healthy Finance
`next_replacement_required_bytes=33,608,519,680` plus the preserved 8 GiB
emergency floor. The current retained Finance set is
`finance-backup-459a091d48326c9be224`, one set, 24,951,476,224 allocated bytes,
with RPO 604,800 seconds and RTO 14,400 seconds. The generation reserve is a
cross-producer monitoring/admission floor; Finance candidate/cutover/rollback
retain their stricter measured domain guards.

Material consumers were classified as follows:

- root: runtime state 29,857,103,872 bytes excluding mounted children; legacy
  compressed backup families 4,084,047,872; journald 1,369,534,464; retired DCP
  archive 884,858,880. Inside runtime state, the protected retained monolith is
  12,659,240,960, current Promo collector artifacts 12,562,300,928,
  Autoanswers 1,751,519,232, forensics 648,314,880 and incident backups
  183,128,064 bytes;
- backup: Finance restore set 24,952,389,632; retained Proxy-v4 evidence
  8,270,315,520; warehouse recovery checkpoints 6,606,557,184; completed
  exact-six archives/manifests 1,909,886,976; other named supplier,
  Autoanswers and correction families are individually represented in the
  canonical producer/lifecycle registry;
- generation: current split generation 25,011,740,672; older split generation
  17,132,380,160; two retained rollback monolith generations total
  25,513,082,880 bytes. Their retirement is excluded from this block.

The existing root monitor reported `normal`, zero unregistered large root
files and healthy root/backup/generation mount identities. Root legacy backup
families are compressed retained material with no current writer. No current
misrouted artifact needed relocation to close recurrence, so this block has no
copy, compression, restore, unlink or production data mutation.

Protected exclusions observed by this inventory are the current/rollback
Finance generations, Finance restore set, warehouse recovery checkpoints,
Autoanswers restore families, exact-six destination, Promo artifacts,
journald, incident/forensic/hold material, the retained monolith and sole DCP
archive. Their presence is not deletion eligibility.

## Canonical storage registry

`artifacts/registry_upload_http_entrypoint/root_storage_policy_v1.json` now
contains `wb_core_storage_registry_v1`. It is the only repository-owned mapping
from producer owner to data class, destination role, relative roots, lifecycle
policy and single-write quota. Persistent current producers may resolve only
through `storage_destination_root` / `resolve_storage_destination`; an exact
hosted destination outside the registered root fails before file creation.

The filesystem registry binds the three exact paths, sources, UUIDs, ext4 type,
required mount options and reserves. Unknown owner, destination, current-write
authority, output, temporary/readback/control component, quota, mount identity
or reserve evidence fails closed. `admit_root_write` remains the compatibility
name but is now role-aware. Its capacity payload separates predicted output,
temporary bytes, readback bytes, control reserve, total peak, filesystem
reserve and predicted free-after.

Current large discretionary backup/evidence/full-copy writers route to the
backup role. Finance coherent source and rollback candidates remain on the
generation role. In-place canonical business-store restore remains a separately
classified essential domain write. Current root use is limited to canonical
business state and the explicitly protected, scope-excluded Promo artifact
family; retained legacy root families have `current=false` and cannot pass
admission.

The current discretionary full-copy set is one-shot and already serialized by
its reviewed operation/domain locks, so no second shared persistent reservation
ledger is introduced. Scheduled warehouse and Finance writers preserve their
existing reservation/replacement state machines. The common registry enforces
a per-producer peak quota and reports generic active/stale reservation arrays;
an unknown future concurrent producer must add an explicit reservation model
before CI will accept its lifecycle entry.

## Lifecycle and retention matrix

The machine-complete per-owner matrix is `storage_registry.producers` joined to
`storage_registry.lifecycle_policies`. The lifecycle policy is semantic; age or
copy count alone never authorizes deletion.

| Producer class | Owners/families | Destination | Retention and hold | Compression / restore | RPO/RTO |
|---|---|---|---|---|---|
| Domain checkpoints | warehouse recovery, calculation/economics | backup | registry eligibility plus verified rollback state; current, failed, quarantined, incomplete, foreign and rollback-required artifacts held | producer-specific; domain recovery registry and exact checkpoint readback | domain-specific |
| Finance restore set | post-cutover backup rotation | backup | one current set, temporary second only during atomic replacement; transaction/current/failure holds | raw exact set; isolated raw+operational restore then selection | 7 days / 4 hours |
| Reconciliation evidence | ads, FF, supplier factual date, buyout, Proxy V4, temporal recovery, SPP, hosted production evidence and warehouse one-shots | backup | through exact terminal reconciliation; active, ambiguous, failed, legal, incident, forensic and unreconciled material held | producer zstd when supported, otherwise exact copy; manifest-bound full restore/rollback | not applicable unless producer defines it |
| Compressed recovery evidence | Autoanswers schema and bounded retired supplier/metric families | backup | minimal verified compressed generation; sole restore and ambiguous material held | zstd with exact size/SHA and SQLite restore integrity | not applicable |
| Generation candidate | Finance coherent source and rollback candidate | generation | same candidate state machine only; active, ambiguous, opened, drifted and unjournaled candidates held | none; exact abort/cutover/rollback | domain-specific |
| Temporary candidate | Autoanswers activation and local diagnostic/preflight candidates | backup or isolated ephemeral test runtime | release only after terminal success or exact abort | none; producer candidate abort/no-op cleanup | not applicable |
| Retained legacy/no writer | old root/backup families, exact-six terminal destination, DCP evidence | current location, no writer | no automatic deletion; later exact allowlisted lifecycle only | retained form; family-specific restore | not applicable |

Finance, warehouse and Autoanswers healthy restore semantics are unchanged.
The complete owner list, exact relative roots and lifecycle join are emitted in
every periodic status artifact so omissions are machine-visible rather than
documentation-only.

## Monitoring and CI enforcement

The existing `wb-core-root-storage-policy.timer` remains the periodic contour.
Its atomic status now includes:

- exact root, backup and generation identity/capacity/inodes;
- root normal, dynamic Finance+8-GiB backup and fixed generation reserve state;
- current producer count and any current large-artifact root destination;
- a same-filesystem walk for large backup/generation files outside every
  versioned destination root, plus active/stale generic reservation and
  lifecycle matrix fields;
- deterministic alerts for filesystem identity or reserve breach and current
  root producer violations.

`status-readback` requires the registry contract and includes registry health
in `ok`. The monitor still performs no cleanup, compression, relocation,
SQLite mutation, service control or journald action.

`storage_recovery_writer_inventory_static_smoke.py` joins every AST-observed
SQLite backup writer to both owner registries and its lifecycle policy. It
fails for an unknown writer, missing admission, missing destination/lifecycle,
current backup/evidence/full-copy owner on root, or a new literal legacy root
artifact path. The exact-six/sanitation read-only legacy literals are retained
only as byte-stable digests; adding even one new literal in those files fails
the check.

## Release and production acceptance

This block is `live_runtime`. Completion requires exact-head PR Gate, one
non-draft PR, exact expected-head squash merge, canonical deploy of the exact
merge SHA and the Release Runner `done` receipt. Production readback must prove:

1. deployed SHA and policy digest are exact;
2. root remains at least 25 GiB available;
3. backup remains above fresh Finance next-replacement plus 8 GiB;
4. generation remains above its 8 GiB reserve and keeps all required mount
   options;
5. the periodic artifact is fresh, `storage_registry.ok=true`, and has zero
   current root producer or unregistered destination violations;
6. Registry HTTP, root-storage monitor and Finance backup rotation units are
   healthy;
7. no exact-six, Promo, journald, generation-retirement, business-data or
   unrelated service mutation occurred.

Required focused checks:

- `python3 apps/root_storage_policy_smoke.py`
- `python3 apps/storage_recovery_writer_inventory_static_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`
- `python3 apps/finance_storage_backup_rotation_smoke.py`
- every suite selected by the exact-base PR planner

## Explicit backlog and exclusions

The retained root compressed archives, backup-device legacy evidence,
Promo artifacts, old Finance/rollback generations, Autoanswers history,
supplier/browser/complaint material and journald remain later producer-specific
work. This block does not infer deletion from age/count, buy storage, create a
new destination, change credentials/publication/security, repair unrelated
service failures or alter the completed exact-six identities.
