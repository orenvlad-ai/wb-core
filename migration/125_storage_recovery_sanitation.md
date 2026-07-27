# Migration 125 — Storage/recovery sanitation

Status: authoritative Stage 4 implementation and production-acceptance
contract. This migration changes recovery artifact lifecycle and backup
filesystem use; it does not authorize Finance raw backfill, live-tail,
source-of-truth switch, reader/writer cutover, monolith retirement, provider
resize or another business-data mutation.

## Query-only production baseline

The 2026-07-27 preflight pinned the active EU runtime and deployed
`79c172c080cfb83f53bdfbf7bda211c2ba0e86c9`. The canonical database was
`11,549,458,432` bytes. The observed filesystems were:

| Mount | Device/type | Capacity | Used | Available | Reserved/free gap |
|---|---|---:|---:|---:|---:|
| `/` | `/dev/sda1`, ext4 | 82,086,711,296 | 65,480,232,960 | 16,546,762,752 | 59,715,584 |
| `state/backups` | `/dev/sdb1`, ext4 | 105,087,164,416 | 56,861,265,920 | 42,840,518,656 | 5,385,379,840 |

The root device is therefore an approximately 80 GB provider volume
(`~76.45 GiB` filesystem), not a 100 GB volume. The backup device is the
approximately 100 GB volume. Stage 4 must not purchase/resize storage or run
partition/filesystem growth. If additional root capacity is wanted after
sanitation, the provider volume must first be resized and only then the
partition and ext4 filesystem may be grown through a separately approved
infrastructure procedure.

Observed trees:

- `/opt/wb-core-runtime/backups`: `27,696,378,293` bytes on root;
- `/opt/wb-core-runtime/state/backups`: `56,860,945,572` bytes on the backup
  device;
- legacy `state/warehouse-recovery`: `4,517,131,924` bytes on root;
- `state/promo_xlsx_collector_runs`: `4,457,046,939` bytes on root.

The recovery registry contained 14 retained T2 checkpoints: 11 hourly and
three manual, totaling `4,517,056,512` bytes. Hourly checkpoints were produced
at approximately 3,600-second cadence, grew from `313,913,344` to
`336,318,464` bytes over ten hours and had a 14-day expiry. No scheduled
`release_expired` apply path existed. At that cadence the policy retained
approximately 8 GB/day and would exhaust root before the first expiry.

Stage 1 intentionally selected exactly three failed
`warehouse-functional-sync` raw checkpoints. Its allowlist did not include
Finance, Ads, supplier one-off recovery, calculation-parameter, Autoanswers,
Promo or other legacy families. Stage 1 was lossless and exact, but it was
never a global backup-retention pass. Stage 2 removed full-monolith reachability
from 30 warehouse/cost entrypoints; it did not schedule retention or move the
already-retained T2 bytes. Those are the two independent reasons bytes
continued to grow after Stage 1.

## Writer inventory and routine invariant

`apps/storage_recovery_writer_inventory_static_smoke.py` is the
machine-readable writer catalog and fails when a new production
`sqlite3.Connection.backup`/`backup_database` source is not classified.
`apps/warehouse_recovery_policy_static_smoke.py` independently proves the 30
warehouse/cost entrypoints and the single central T3 call.

The only scheduled wide writer is hourly/manual warehouse publication, and it
uses T2 domain checkpoints without `wb_finance_weekly_raw_rows`. Calculation
parameters and dependent economics use T1 before images. Promo refresh writes
workbook/debug artifacts, not a database copy, and invokes bounded light GC.
Full coherent copies remain only in reviewed one-shot mutation/migration,
once-per-schema or private temporary-candidate contours. Disabled legacy
supplier entrypoints remain disabled. A scheduled hourly/daily full-monolith
writer is a CI failure.

## T2 routing and bounded retention

New T2 artifacts are rooted at:

`REGISTRY_UPLOAD_RUNTIME_DIR/backups/warehouse-recovery/domain-checkpoints`

The production `state/backups` path is the dedicated `/dev/sdb1` mount. Public
status reports both filesystem identities and whether routing is actually
distinct; the path alone is not treated as proof.

Retention is simultaneous and deterministic:

- protect the two newest verified rollback checkpoints;
- retain at most three successful T2 checkpoints;
- retain no more than 2 GiB when an extra checkpoint exceeds the byte cap;
- release superseded checkpoints after 24 hours even if the count cap was not
  reached;
- report the observed cadence, current bytes, projected 24-hour/14-day bytes
  without GC, bounded post-plan bytes, zero projected 30-day growth and
  filesystem headroom;
- warn below 8 GiB available and stop T2 writes below 4 GiB.

Pre- and post-publication retention run inside the existing warehouse writer
lock for backup-only, hourly, manual and reviewed sync-apply paths.
`apps/warehouse_recovery_retention.py` and hosted
`warehouse-recovery-retention-dry-run|apply` provide an exact deployed-SHA,
fingerprint-gated operator path.

The plan fingerprint excludes generation time but includes policy,
operation/state version, artifact path/stat/SHA and protected ids. Apply owns a
durable registry audit row, is idempotent and restart-safe, rechecks lifecycle
CAS and artifact identity, fsyncs removal, releases undo/registry ownership and
readbacks the result. `failed_recoverable`, `quarantined`, corrupt, incomplete,
foreign and current mutation states are never ordinary deletion candidates.

## Exact legacy-family sanitation

`apps/storage_recovery_sanitation.py` is the only Stage 4 legacy-family
mutation runner. Both backup roots are fixed by the hosted wrapper. Every
mutable immediate child is named in `FAMILY_POLICIES`; an unlisted directory,
symlink, live database name, unmatched archive or target outside one exact
family fails closed.

The runner emits a complete inventory and then one exact family action:

1. for a raw immutable SQLite source, reuse
   `apps/sqlite_backup_archive.py` to prove `mode=ro&immutable=1`,
   `query_only=ON`, SQLite integrity, source stat/SHA, empty WAL, capacity and
   non-target digest; create an fsynced zstd+manifest; test the zstd frame and
   streamed decompressed size/SHA; independently read back the retained
   archive; only then remove the raw source and owned unchanged sidecars;
2. after raw sources are gone, verify every standard archive by decompression,
   retain the configured newest restore set and remove all exact superseded
   archive/manifest generations in one fingerprinted action;
3. remove owned sidecars when the retained manifest records their exact source
   stat, or for older standard manifests when the source is absent, the
   same-basename archive passes decompressed SHA readback and the derived WAL
   is empty; SHM carries no restore data.

Every apply writes a private durable audit before its first unlink, fsyncs each
directory change, preserves a digest of all non-target family entries and can
resume the same fingerprint after a crash. One corrupt family stops only that
family; independent families remain eligible.

The large Stage 4 allowlist is:

- root: `ads-historical`, `wb-finance-canonical`,
  `ff-stock-targeted-reconciliation`, `warehouse-archival-estimate`,
  `warehouse-functional`, `warehouse-functional-economics`,
  `warehouse-functional-recovery`, `warehouse-functional-sync`,
  `warehouse-opening`, `warehouse-supplier-certification-replay`;
- backup device: `supplier-26gn390-recovery`,
  `supplier-26gn527-vtb-recovery`, `supplier-cny-payment-10-recovery`,
  `calculation-parameters`, `canonical-cost-engine`,
  `canonical-vitrina-publication`, both supplier factual-date family names,
  `warehouse-functional-sync`, `promo_metric_eligibility_recompute` and
  `sheet_vitrina_v1_proxy_margin_3_historical_backfill`.

Autoanswers custom-manifest families, `lost+found`, `.tmp`, control-plane
archives, root-level files and every unlisted family are inventory-only in this
runner. They cannot be adopted by filename similarity.

## Promo artifact GC

The ordinary Promo GC remains `apps/promo_campaign_archive_gc.py`. A full
production apply now requires:

- normalized persistence and exact workbook hash proof;
- candidate SHA/stat/inode/mtime and a stable plan fingerprint;
- exact deployed SHA;
- a private pre-delete audit with crash resume;
- fsync after each target and a digest of preserved files in candidate run
  directories.

Successful/blocked TTL rules remain unchanged. Current, running, unknown,
partial/incomplete and replay-critical archive files remain protected. The
automatic light GC keeps its bounded deadline and current-run protection; it
does not impersonate the full exact operator pass.

## Production closure

The release is inert until merged/deployed by the Release Train. Production
mutation then proceeds family by family through hosted
`storage-recovery-sanitation-inventory|plan|apply`, bounded T2 retention and
hosted `promo-archive-gc-dry-run|apply`. Every family ledger must record
source/archive identities, before/after available bytes, freed bytes,
non-target digest and restore probe.

Acceptance requires:

- no retained large raw full-monolith file in the allowlisted roots;
- no active orphan/raw T2 leak;
- business and warehouse-domain digests unchanged;
- at least three timer-owned cycles, or equivalent exact scheduled evidence,
  proving automatic retention and non-linear steady state;
- exact T2 rollback/readback;
- unrelated services and timer/writer state healthy;
- proven 30-day capacity headroom.

The target is at least 40 GB root available and 65 GB backup-device available.
If unique/corrupt evidence prevents it, closure reports the exact retained
families and shortfall instead of weakening a guard.

Only after production sanitation acceptance may the canonical Finance storage
split runner produce a new query-only dry-run. That dry-run must bind current
row counts/digests, source/destination and mount identities, watermarks, chunk
manifest, required/available/reserve bytes, rollback/non-target proof and a
fresh fingerprint. The Stage 3 fingerprint that predated sanitation is stale.
No candidate backfill, live-tail, cutover or monolith retirement is permitted
by this migration.

## Required checks

- `python3 apps/warehouse_recovery_policy_smoke.py`
- `python3 apps/warehouse_recovery_policy_static_smoke.py`
- `python3 apps/warehouse_recovery_retention_smoke.py`
- `python3 apps/storage_recovery_writer_inventory_static_smoke.py`
- `python3 apps/storage_recovery_sanitation_smoke.py`
- `python3 apps/promo_campaign_archive_gc_smoke.py`
- `python3 apps/sheet_vitrina_v1_refresh_promo_artifact_gc_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py`
- production isolated UI Flow and exact Release Train acceptance
