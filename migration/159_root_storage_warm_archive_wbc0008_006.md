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
pre-submit repeatability defect found by the first block-006 operation. WBC0008
block 012 retains that unchanged scope and corrects the producer-ownership CAS
boundary after a legitimate same-inode 4,096-byte Autoanswers write changed the
old global protected-file `size/mtime` digest between readiness and the first
JIT witness. Block 022 supersedes the block-021 model with contract
`root_storage_warm_archive_wbc0008_006_v7`; v1/v2/v3/v4/v5/v6 evidence and
terminal old operations stay immutable and are never resumed or replayed. Every
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
literal required systemd units before compression projection. The classifier
derives all 12 timer/owning-service pairs from the literal names; no warehouse-
specific exception exists. Each unit records
`LoadState`, `ActiveState`, `SubState`, `Result`, `MainPID`,
`ExecMainStatus` and `UnitFileState`; timer rows additionally retain
`LastTriggerUSec` and `NextElapseUSecRealtime` when systemd exposes them. The
gate accepts an enabled loaded `active/waiting` timer only with its successful
inactive/dead one-shot owner, and accepts `active/running` only while that owner
is `activating/start` or `active/running` with `Result` empty/success,
`ExecMainStatus=0` and a positive `MainPID`. The post-trigger waiting plus
inactive/dead success pair is the ordinary steady state. Failed, unknown,
masked or absent controls, nonzero/invalid owner `ExecMainStatus`, failed
`Result`, an impossible pair and a query/predicate/literal-unit defect fail
closed. Timer properties that systemd does not expose remain recorded as empty;
an exposed nonzero timer status is unhealthy.

Because sequential `systemctl show` reads can straddle one trigger edge, only a
pair whose individual states are valid or a known in-flight transition receives
up to three exact paired resamples inside five seconds. The original affected
rows, every resampled row, pair classification and bounded-deadline evidence are
durable. A resample may succeed only after the pair reaches one of the two
accepted predicates; an unknown or still-impossible transition never becomes
healthy by timeout. Any service-gate block writes the complete final 27-row and
12-pair snapshot, exact failing rows/pairs and reason codes to the private
readiness receipt and trusted callback. The same structured gate is repeated in
final readiness qualification, mutation reconciliation and terminal readback.

WBC0008 block 011 corrects the pre-mutation lock-context protocol defect found
by the first block-011 scope-goal attempt. The Finance lock and all three other
lifecycle locks now have independent enter/exit ownership, close their current
handle on acquisition failure, release every acquired lifecycle lock in reverse
order after a partial acquisition, and always unlock/close after normal return
or a body exception. The smoke executes the exact nested `apply_batch` lock
path through the first durable journal-write boundary and proves that this
boundary is not called after an earlier failure. It also directly covers
normal exit, body-exception propagation, contention, partial acquisition,
symlink rejection, repeated acquisition and descriptor/lock cleanup. Terminal
readiness `readiness-v1-6e2294ca39ba7606c08d32dbc7454854`, operation
`production-goal-v1-5a329a68fad1c027014d4d8f905670c9` and job
`8d7f62433effef29df3e14ca77e590253733d08c9d65ffaf923cfcb7ad0c7ddb`
remain immutable and are never retried or reused.

WBC0008 block 012 partitions non-target evidence through the versioned
root-storage policy rather than by filename:

- exact target sources and sidecars retain the existing full identity, SHA,
  activity, hold and provenance CAS at every gate;
- non-target files inside the six affected source families retain exact
  enumeration, type, path, device/inode, mode and uid/gid topology. Their
  size/allocated bytes, mtime/ctime and content digest are observation-only
  writer-progress evidence after block 017; add/remove/replacement/type/
  ownership topology drift still fails closed. Any destination-family foreign
  object remains an immediate blocker;
- active mutable canonical stores are only the explicit policy bindings for
  current Finance raw, current operational and Autoanswers stores. StoreRegistry
  resolves the first two and the literal Autoanswers binding resolves the
  third. Their stable CAS contains canonical path, device/mount, inode/type,
  no-symlink proof, owner/classification, StoreRegistry generation identity
  where applicable and the declared service-access relationship. Ordinary
  same-inode content, allocated-byte, size and mtime/ctime evolution is retained
  as observation evidence but is excluded from the stable topology digest.

WBC0008 block 013 makes that relationship an explicit access-role matrix.
Block 021 advances it to `wb_core_non_target_cas_v3` and adds the exact declared
`root|generation` filesystem role for every active mutable binding. Each literal
repo-owned systemd unit has one declared `reader`, `writer` or `reader_writer`
role and an exact allowed FD-mode set; wildcard, pathname, process-name,
cgroup-parent and generic persistent-service fallbacks are absent. Registry
HTTP is explicitly declared for Autoanswers because the live module constructs
the isolated Autoanswers repository and the active unit legitimately held its
exact read-only FD. The current Finance raw/operational and Autoanswers matrices
also enumerate their other repo-owned direct readers and writers from the
checked-in entrypoints and unit contracts.

Every observed opener must bind the canonical path's exact device/inode through
its actual FD and must be the exact positive `MainPID` of exactly one declared,
healthy unit in the 27-row snapshot. A read-only FD needs declared read access;
read-write needs declared writer access; no current SQLite binding allows
write-only. Unknown mode, undeclared or non-MainPID process, PID reuse across
multiple units, unhealthy/ambiguous unit state, or path text without the exact
FD device/inode proof fails closed. Per-opener evidence retains canonical
path/device/inode, PID/FD/mode/comm and FD target, all matching units, exact
matched unit/MainPID, classified service health, declared role/modes and one
accepted or rejected reason. The sorted access-role matrix is part of the
stable mutable topology digest; policy drift therefore blocks even when the
file inode is unchanged. Terminal block-012 readiness
`readiness-v1-32faefe2d84925376c40b932f4d8e829` remains immutable and is never
retried or reused; it created no production-goal operation or mutation.

WBC0008 block 017 removes the fresh-PR/readiness carousel without weakening
source or topology safety. Material is explicitly partitioned into:

- `immutable_safety_v1`: the six literal source identities and SHA-256,
  sidecar/hold/provenance evidence, destination and all three mount/device
  identities, StoreRegistry generation/policy identity, root-policy ownership
  and protected-path topology, exact scoped non-target topology, and mutable
  canonical path/device/mount/inode/type/owner/access-role topology;
- mutable observations: current Finance retained-backup/capacity values,
  filesystem available bytes, canonical and protected non-target size/mtime,
  open-handle PID/FD samples, service PID/timing/state samples, source activity,
  sanitation-job inventory and journald health evidence.

At JIT and again after the Finance and lifecycle locks are held at mutation
start, the immutable partition must match exactly. Mutable observations are
re-evaluated through named semantic predicates: Finance health, the preserved
conservative backup floor, every six-stage capacity peak, projected root
minimum, all 27 units/12 pairs healthy, exact-source sidecar/opener/lock/hold
guards clear, no other sanitation job, and journald evidence available.
Ordinary same-inode canonical DB content/size/mtime, protected non-target
size/mtime, Finance rotation identity, service PID/timing and unrelated writer
progress therefore do not invalidate safety while these predicates and stable
topologies remain valid. Source content/stat/SHA, source sidecar/hold/lock,
destination/mount, StoreRegistry/policy/ownership, protected-path topology or
canonical topology drift still fails closed.

Any JIT or mutation-start mismatch writes
`root-warm-archive-material-cas-failure.json` with exclusive-create plus fsync
before raising and, at mutation start, before the mutation journal exists. The
private artifact binds readiness, goal operation, job identity (or the explicit
pre-submit `not_created` state), deployed SHA and manifest. It records bounded
safe component evidence, exact JSON paths, classification and before/after
component digests. The first failure is immutable: a later matching snapshot
cannot replace or erase it, and the same operation immediately reads back that
terminal failure. No destination creation, compressor, archive publication or
unlink can follow a mismatch.

An unknown resolver, owner, classification, path, destination object or
unrelated FD owner fails closed. Mutation authority remains the six literal
source unlinks plus their exact archive/manifest outputs and private control
evidence; the terminal ledger proves zero non-target unlink/move/write paths.
Readiness, both JIT witnesses, mutation start, every per-source pre-unlink gate,
crash resume and terminal readback persist separate immutable and mutable-
topology digests plus before/after mutable observations. Thus concurrent
ordinary store evolution is distinguishable from replacement/misrouting or an
operation-caused non-target mutation without weakening target CAS.

WBC0008 block 020 corrects the pre-submit activity aggregation defect proven by
terminal PR #1071 readiness
`readiness-v2-e9d36f60986f9aef7467c7201abd707a-a02`: the real full projection
records at least four clean observations for each of the six literal sources,
while the old mutable predicate incorrectly required exactly six observation
rows. Activity qualification is now semantic coverage of the exact literal
target key/path set. Every observation must bind the matching target's literal
identity and retain a clear accepted classification, exact identity/material,
sidecar, FD-mode/device/inode, lock, hold and provenance evidence. Any missing
or foreign target, malformed row, duplicate with a mismatching identity,
write-capable/unknown opener or unsafe evidence fails closed. Multiple clean
bounded observations of the same exact target are valid and are counted in the
receipt; no fixed total such as 24 is part of the safety contract. The same
review makes capacity stages cover each literal target exactly once and
lifecycle-lock evidence cover each literal lock exactly once, preventing a
duplicate row from hiding a missing identity. Direct smoke coverage executes
the production-shaped `_target_probe -> _material_snapshot ->
_mutable_safety_predicates` contour with four observations per target, then the
JIT and mutation-start predicates before the first durable mutation write.
Unsafe, missing, foreign and identity-drift cases are separately rejected.

WBC0008 block 021 corrects the namespace-local mount CAS defect proven by the
single terminal PR #1073 operation
`production-goal-v1-b1c08aecb19d5d4ee46941f9be8474fe`. Qualification ran in
the host mount namespace while the detached systemd worker ran in its private
namespace. The backing devices, sources, UUIDs, filesystem types and path
bindings were unchanged, but backup mount id `88 -> 434`, generation mount id
`219 -> 435`, and the root observation changed from mount id `29`, mount point
`/`, options `rw,relatime` to mount id `438`, mount point
`/opt/wb-core-runtime/backups`, options `rw,nosuid,relatime`. Those raw values
caused seven false immutable component changes before the mutation journal;
the terminal failure remains immutable and is never retried or reused.

Stable mount CAS is now `wb_core_semantic_filesystem_identity_v1`. It binds the
literal role and repo policy owner, canonical path-to-family placement, exact
`st_dev` plus major/minor, `/dev/sda1|sdb1|sdc1`, the corresponding filesystem
UUID, ext4 type, mandatory writable state and stable integrity/write options.
The destination family must remain under the existing backup role/device; root
and generation can never substitute for it. Active Finance raw/operational
stores are declared `generation`, while Autoanswers is declared `root`; this
role, existing owner/classification, StoreRegistry identity, inode/type/path and
access-role matrix all remain in the stable mutable topology digest.

Mount id, parent id, mount root, namespace-relative mount point, propagation
fields, atime observation and additional restrictive `nosuid|nodev|noexec`
flags remain structured observation evidence and are excluded from stable CAS.
Role-declared generation requirements `noatime|nosuid|nodev|noexec` remain
mandatory; only additional namespace restrictions outside that baseline may
differ.
`rw` is mandatory and `ro` is terminal. Known integrity/write semantics such as
`errors=`, `data=`, `commit=`, barrier/discard/sync/journal options remain stable
CAS fields; unknown or ambiguous options/records fail closed. The regression
uses the exact host/worker records above and proves equal semantic digests while
retaining unequal raw observations. Negative cases cover source/device/UUID/
fstype drift, backup-on-root placement, read-only, missing/ambiguous identity,
path binding, policy owner/access-role and topology drift. The production-shaped
fixture crosses host readiness into systemd-worker JIT and mutation-start
qualification and reaches the first durable mutation call only after all exact
six source SHA, sidecar, FD, lock, hold and provenance predicates remain intact.

WBC0008 block 022 corrects the remaining maximum-depth selector defect proven
by the single terminal PR #1074 operation
`production-goal-v1-083b70867b00f142b1a50a1169b8ca82`, detached job
`5443d9e3ea677fef6802da9bc1438a7fab690632a23a9b7cc989dababbaf0b79`
and immutable component `/material_collection`. The v6 selector rejected more
than one maximum-depth record for
`/opt/wb-core-runtime/state/backups` before the mutation journal and before any
archive, manifest or unlink. Its failure artifact is
`sha256:2878faf7e5339ccf7e6b868851af7808a022179ed435fee75784e651ac8de346`;
that readiness, operation and job remain terminal and are never retried or
reused.

Before any new readiness or apply, the deployed v7 Release must run exactly one
canonical `warm-archive-mount-probe`. The probe submits one caller-known
`warm-archive-mount-probe` request to the existing
`wb-core-storage-recovery-sanitation@.service`, then performs only query-only
status readback. Its immutable server-owned result and bound Actions artifact
record the deployed SHA, repo unit-template SHA, exact unit instance, mount
namespace link/device/inode, canonical target and family-anchor realpaths plus
device/inode, and every sorted raw maximum-depth mountinfo record including the
exact raw line. It has no archive, unlink, service-restart, timer-change or
business-data primitive. Missing, failed or contract-invalid probe evidence
blocks readiness rather than creating a production operation.
The fresh readiness receipt schema is
`wb-core.root-warm-archive-readiness-receipt/v4`; it binds that exact probe job,
evidence digest, artifact and Actions comment. Readiness additionally requires
a newer exact OWNER comment, so a pre-probe or reused authorization comment
cannot become the new v7 readiness identity.

Stable mount CAS is now `wb_core_semantic_filesystem_identity_v2`. The selector
may collapse multiple maximum-depth records only after independently proving
for every candidate the exact role and repo policy owner, target/family anchor
realpath/device/inode placement, normalized mount-root-to-target backing
subpath, `st_dev` and major/minor, block-device source and `st_rdev`, UUID,
filesystem type, unambiguous writable state, all role-required restrictions and
the same stable integrity/write-option semantics. The stable identity retains
the single distinct semantic identity plus distinct-identity count/digest.
Namespace-local mount/parent ids, record order, optional propagation fields,
atime observations and additional allowed restrictive flags remain observation
evidence only. The complete sorted raw candidate set, count, digest and each
candidate proof remain inspectable in readiness/JIT/mutation evidence.

Any candidate with missing or divergent device/source/UUID/type/writable/stable
option, normalized path/root backing subpath, path or anchor identity, role or
policy owner is terminal ambiguous with exact component/failure evidence. A
missing/foreign record, arbitrary same basename, unknown option, read-only
record or partial candidate set is never collapsed. Regression uses overlapping
production-shaped records, candidate-order permutation, allowed additional
restrictions, and one-field negative drift for device, source, UUID, type,
read-only state, stable option, mount root/path, anchor and owner/role. The full
readiness-to-JIT-to-detached-worker fixture supplies an equivalent overlapping
set and reaches the first durable mutation call only for that exact set.

## Capacity and lifecycle

Compression is zstd level 1 with one thread and one source at a time. Temporary
archive and full restore files exist only in the destination family, mode 0600.
The worker accounts for every already-published archive, the current archive,
manifest/control reserve and a complete restored SQLite copy. Available backup
bytes must remain at or above the live Finance
`next_replacement_required_bytes` plus an additional 8 GiB emergency reserve at
every stage. Projected terminal root availability must be at least 25 GiB.

For each source the worker:

1. verifies exact source CAS, immutable non-target digest and mutable canonical
   topology digest;
2. compresses and fsyncs the destination temp;
3. stream-decompresses to the exact original byte count and SHA-256;
4. materializes a full restored file and repeats SQLite quick/integrity/schema
   proof;
5. durably publishes and independently rereads the archive/manifest pair;
6. repeats source identity/hash/sidecar/opener/lock/hold/provenance CAS,
   immutable non-target and mutable canonical topology CAS plus capacity
   immediately before unlink;
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

Before mutation submit, the trusted Apply workflow's `warm-archive-readiness`
mode runs a bounded canonical query-only sequence against one exact deployed
SHA and the same exact owner passport/derived goal operation. It tolerates a
transient activity sample and requires three
consecutive clean post-projection witnesses inside a maximum 60-second
stabilization window. Persistent write-capable/unknown FD, lock, sidecar, hold
or material drift returns one terminal structured callback and no operation is
created. The immutable ready receipt binds the release operation, deployed SHA,
exact source material/SHA, conservative capacity proof, separate immutable
non-target and mutable canonical topology digests and one private full
compression projection. Its durable evidence records ordinary mutable fields
and exact service-role/open-handle relationships without placing those volatile
content fields in the qualification digest.

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

The v2 readiness base identity is deterministically bound to repository, PR,
exact `live_runtime/done` Release operation, authorization-comment identity and
derived goal operation. It admits at most three contiguous attempts `a01`..
`a03`. Every attempt is one immutable terminal receipt; a blocked attempt is
never rewritten or retried, a ready attempt must be the final attempt, and
duplicate, skipped, foreign or out-of-bound attempts fail closed. A later
natural recovery can use the next bounded attempt under the same deployed code
and goal, so an otherwise-empty PR is neither required nor accepted merely to
manufacture a readiness identity. Exhaustion creates no production command,
queue or unbounded retry. The later scope-goal parser accepts exactly one final
`state=ready` receipt for that same binding.

After a code/runtime correction such as block 022, the prior deployed-SHA
readiness sequence remains terminal and cannot authorize the new release. The
new exact `live_runtime/done` receipt, the same durable owner passport and its
derived goal operation bind a fresh readiness-v2 base whose first attempt is
`a01`. Producer consistency is selected per exact live-resource semantics:
semantic/material revalidation is used for ordinary online source observation;
no blanket pause of warehouse, FBS or unrelated producers is permitted.

Full readiness/apply evidence remains canonical JSON in the immutable private
Actions artifact. PR publication is a deterministic compact summary below
65,536 bytes containing terminal state, readiness/operation, apply count, job,
error/component-diff summary and the artifact name, byte size and SHA-256. A
GitHub 422 or other comment failure cannot hide or rewrite the artifact and
never causes readiness, qualification or mutation to run again; publication
recovery remains query-only and digest-bound.

## Terminal reconciliation of the existing submitted operation

Block 024 adds a separate `repo_only` release capability; it does not change or
deploy `root_storage_warm_archive.py`. The mode
`warm-archive-receipt-reconciliation` is valid only for the already submitted
exact-six operation whose immutable Production Apply receipt is exactly
`blocked/post-submit-readback-not-reconciled`, with one submit and a succeeded
attempt-1 detached job. It binds the exact source PR/run/artifact name and
SHA-256, authorization and blocked comment ids, release/readiness/operation/job
and qualified-manifest identities, deployed SHA, plus the new merged
trusted-main repo-only Release receipt. No new owner authorization is required:
under the authorization router this is same-operation query-only
`AUTO_CONTINUE`, not a new production effect.

Exactly one bounded SSH probe is permitted after GitHub-only preflight. It runs
with `PYTHONDONTWRITEBYTECODE=1` and only reads immutable journal/job/manifest
records, the six retained archive/manifest pairs and their saved proof digests,
direct source/destination/non-target/StoreRegistry/journald state, current
capacity, natural monitor and systemd show/config output. Readiness, a second
submit, apply/job/archive execution, the existing `readback_batch`, decompression
or full restore, temporary files, lock acquisition, service/timer action,
SQL/file writes and unlink are absent. Any source/job/journal/hash/proof drift,
source presence, missing/foreign/temp destination object, active job/lock,
unstable or below-floor capacity, stale/non-normal monitor, 27/12/service,
journald/non-target/StoreRegistry drift, or nonzero Promo/business/non-target
effect blocks `done`.

Full canonical evidence is uploaded as one immutable artifact first. Only then
may the Actions bot append one distinct compact supersession marker to the
original operation PR, binding the untouched source blocked comment/artifact,
new release/artifact/evidence digests and
`done/reconciled_existing_operation`. An exact existing marker is verified back
through its artifact and returns `already_terminal` without SSH or publication;
duplicate, foreign or different existing evidence fails closed. This block's
production mutation count is structurally and observably zero.

## Terminal acceptance

`COMPLETE` requires the immutable block-024 `done` reconciliation artifact and
compact supersession marker for the same existing operation, plus exactly six
absent sources, six private archives and six
private manifests on `/dev/sdb1`; independent full restore, exact size/SHA and
SQLite quick/integrity proof for each; unlink count six; exact allocated-byte
reconciliation; root available at least 25 GiB; backup above the Finance plus
8 GiB floor; fresh normal root-monitor status; healthy Registry HTTP, AI API,
Data MCP, root-monitor and Finance timers; preserved StoreRegistry/non-target
identities; unchanged journald PID, effective values and protected inventory;
separate immutable/mutable topology reconciliation, zero non-target mutation
paths and zero Promo or business-data mutation. Anything else is terminal
`BLOCKED` with no substitute file and no replay.

Strict exclusions are all other raw or compressed copies, incident/forensic or
legal-hold material, the sole DCP archive, active/rollback monoliths, Finance,
warehouse and Autoanswers restore sets, Promo GC, journald changes, generation
retirement, a new mount/destination, capacity expansion and every later storage
stage.

Required checks:

- `python3 apps/root_storage_warm_archive_smoke.py`
- `python3 apps/storage_recovery_sanitation_smoke.py`
- `python3 apps/storage_recovery_sanitation_job_smoke.py`
- `python3 apps/production_apply_runner_smoke.py`
- `python3 apps/wbc0008_warm_archive_receipt_reconciliation_probe_smoke.py`
- `python3 apps/root_storage_policy_smoke.py`
- `python3 apps/finance_storage_backup_rotation_smoke.py`
- every suite selected by the exact-base PR planner
