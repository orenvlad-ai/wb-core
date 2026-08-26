# Root storage warm archive — WBC0008 block 006

Status: implementation and production-mutation contract. This migration grants
authority only to replace the six exact inactive raw SQLite recovery/evidence
copies encoded in `apps/root_storage_warm_archive.py` with verified compressed
warm archives on the existing backup filesystem.

## Fixed scope

The destination is exactly
`/opt/wb-core-runtime/state/backups/root-warm-archive-wbc0008-006` on
`/dev/sdb1`. The six source paths, archive names, owner/family provenance and
restore roles are literal policy entries. No discovery result can add or
substitute a target. An opener, lock, sidecar, hold marker, provenance mismatch,
SQLite integrity failure, mount drift, count drift or material identity drift
blocks the whole pre-submit contour.

The dedicated runner records device/inode, apparent and allocated bytes, mode,
uid/gid, mtime/ctime, SHA-256, SQLite header/schema identity, immutable
query-only `quick_check` and `integrity_check`, sidecars, process openers,
kernel locks, related operations, provenance and incident/forensic/legal-hold
evidence for every source. StoreRegistry current paths and the root-storage
owner/classification are independently checked. Reclaimed bytes are calculated
from 512-byte allocated blocks.

WBC0008 block 007 is the same exact-six lifecycle scope and only corrects the
pre-submit repeatability defect found by the first block-006 operation. Every
new readiness/manifest/apply record uses contract
`root_storage_warm_archive_wbc0008_006_v2`; v1 evidence and the terminal old
operation stay immutable and are never resumed or replayed. Every
qualification and mutation CAS gate now retains structured per-source activity
evidence: exact path, PID, FD, access mode, process `comm`, resolved FD target
and device/inode binding, kernel locks, sidecars, before/after identity and hash
state, hold/provenance evidence and observation-only related-process matches.
A read-only FD may remain open only while source identity/SHA and sidecars stay
exact and no lock or hold exists. Write-only, read-write or unknown access,
any kernel lock, sidecar, hold, provenance/material drift blocks. A process
whose command line merely contains a source/family string without an exact FD
or lock binding is retained as an observation and is never classified as file
activity. Unknown access mode fails closed. Error JSON, qualification receipt
and detached-job failure state name the exact source and structured blocker;
command-line contents are represented by a digest and matched terms, not copied
as potentially secret text.

Every fresh readiness also persists one complete query-only snapshot of the 27
literal required systemd units before compression projection. Each unit records
`LoadState`, `ActiveState`, `SubState`, `Result`, `MainPID`,
`ExecMainStatus` and `UnitFileState`; timer rows additionally retain
`LastTriggerUSec` and `NextElapseUSecRealtime` when systemd exposes them. The
gate classifies current persistent ownership, a correct inactive one-shot, an
expected active/waiting timer, a current active one-shot, an absent/masked unit,
a real unhealthy owning service/timer control, stale result/exit fields whose
current PID or waiting predicate is authoritative, and a query/predicate/literal
unit-list defect. Missing fields, a failed query or an `Id` mismatch fail closed.
Any service-gate block writes the complete 27-row snapshot, exact failing rows
and reason codes to the private readiness receipt and the trusted callback; it
must not collapse the failure to a generic health string. The same structured
gate is repeated in final readiness qualification and terminal readback.

## Capacity and lifecycle

Compression is zstd level 1 with one thread and one source at a time. Temporary
archive and full restore files exist only in the destination family, mode 0600.
The worker accounts for every already-published archive, the current archive,
manifest/control reserve and a complete restored SQLite copy. Available backup
bytes must remain at or above the live Finance
`next_replacement_required_bytes` plus an additional 8 GiB emergency reserve at
every stage. Projected terminal root availability must be at least 25 GiB.

For each source the worker:

1. verifies exact source CAS and non-target digest;
2. compresses and fsyncs the destination temp;
3. stream-decompresses to the exact original byte count and SHA-256;
4. materializes a full restored file and repeats SQLite quick/integrity/schema
   proof;
5. durably publishes and independently rereads the archive/manifest pair;
6. repeats source identity/hash/sidecar/opener/lock/hold/provenance CAS,
   non-target digest and capacity immediately before unlink;
7. writes an fsynced pending-unlink intent, submits one source unlink, fsyncs
   the source directory, and reconciles absence before proceeding.

An interrupted owned temp or pair publication is resumed only from the exact
operation identity. An absent source is accepted only with its pending/completed
unlink intent and a fresh full restore proof. The durable detached sanitation
job serializes the batch and holds the Finance storage lock. It never launches
parallel compression or a second whole-operation submit.

## Trusted apply

The PR planner classifies this capability as `live_runtime`: the trusted
Release Runner merges and deploys only inert code and managed-unit permissions,
then emits its exact `done` receipt. The separate default-off Apply Runner owns
the production-mutation boundary. The task-scoped owner authorization syntax
is:

```text
/wb-core authorize-goal-v1 task WBC0008 profile root-warm-archive-six target wb_core_eu_hosted_runtime_active sources 6 archives 6 manifests 6 unlinks 6 reclaimed-allocated-bytes <exact-allocated-byte-total> root-minimum-bytes 26843545600 backup-floor-bytes <finance-next-replacement-plus-8GiB>
```

Before a new production-goal operation exists, the trusted Apply workflow's
`warm-archive-readiness` mode runs one canonical query-only contour against the
exact deployed SHA. It tolerates a transient activity sample and requires three
consecutive clean post-projection witnesses inside a maximum 60-second
stabilization window. Persistent write-capable/unknown FD, lock, sidecar, hold
or material drift returns one terminal structured callback and no operation is
created. The immutable ready receipt binds the release operation, deployed SHA,
exact source material/SHA, conservative capacity proof and one private full
compression projection.

The trusted Apply Runner accepts the later task-scoped operation only with that
single exact ready receipt. It creates at most four private JIT candidates and
requires two consecutive identical material-qualification digests. Both JIT
witnesses cryptographically reuse the ready projection only after fresh
lightweight stat/sidecar/FD/lock/hold/provenance/material-CAS and capacity/
non-target checks; they never repeat compression measurement, full SQLite
integrity or full source hashing solely to obtain equivalent witnesses. The
mutation-start CAS also reuses the exact projection under the same guards. A
fresh full source hash is still mandatory immediately before each unlink, and
the actual archive plus independent stream/full-restore SHA, SQLite quick,
integrity and schema proofs remain mandatory. The Runner then submits exactly
one caller-known detached job. A nonzero/ambiguous submit is never repeated;
the only next action is query-only job and archive readback. The material hash
is evidence, not a second owner authorization field.

## Terminal acceptance

`COMPLETE` requires exactly six absent sources, six private archives and six
private manifests on `/dev/sdb1`; independent full restore, exact size/SHA and
SQLite quick/integrity proof for each; unlink count six; exact allocated-byte
reconciliation; root available at least 25 GiB; backup above the Finance plus
8 GiB floor; fresh normal root-monitor status; healthy Registry HTTP, AI API,
Data MCP, root-monitor and Finance timers; preserved StoreRegistry/non-target
identities; unchanged journald PID, effective values and protected inventory;
and zero Promo or business-data mutation. Anything else is terminal
`BLOCKED` with no substitute file and no replay.

Strict exclusions are all other raw or compressed copies, incident/forensic or
legal-hold material, the sole DCP archive, active/rollback monoliths, Finance,
warehouse and Autoanswers restore sets, Promo GC, journald changes, generation
retirement, a new mount/destination, capacity expansion and WBC0008 block 007.

Required checks:

- `python3 apps/root_storage_warm_archive_smoke.py`
- `python3 apps/storage_recovery_sanitation_smoke.py`
- `python3 apps/storage_recovery_sanitation_job_smoke.py`
- `python3 apps/production_apply_runner_smoke.py`
- `python3 apps/root_storage_policy_smoke.py`
- `python3 apps/finance_storage_backup_rotation_smoke.py`
- every suite selected by the exact-base PR planner
