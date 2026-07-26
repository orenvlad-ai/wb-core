# Migration 123 — Warehouse/cost recovery policy design

Status: authoritative implementation contract. The global policy, operator
surface, call-site migrations and production canary are implemented by the
independent Stage 2 `NEW_TASK`. Release truth is the GitHub Release Train:
production acceptance is terminal only at `release:production` after the exact
deployed-SHA canary and UI acceptance described below.
The production measurements in the evidence baseline and the `Current
copied/read bytes` column remain the pre-policy baseline, not the desired
runtime behavior.

## Evidence baseline

The 2026-07-26 production read preflight measured the current canonical SQLite
file at `11,501,142,016` bytes. A query-only `dbstat` inventory attributed:

- `10,468,679,680` bytes, including indexes, to
  `wb_finance_weekly_raw_rows` (`2,414,082` rows);
- `276,897,792` bytes to the 50-table warehouse/cost dependency closure;
- `103,550,976` bytes of that closure to 189 ready snapshots;
- `60,047,360` bytes to 11,872 versioned functional balance rows;
- `61,100,032` bytes to 11,890 versioned functional document-line rows;
- `20,975,616` bytes to 110 persisted WB snapshots;
- `15,941,632` bytes to derived `wb_finance_*` tables.

These are allocated page bytes, not estimates from serialized Python objects.
They show why copying the monolith is not proportional to a bounded
supplier-document or queue mutation.

The production incident that motivated this design left three coherent
`warehouse-functional-sync` checkpoints after source-drift failures. Each raw
checkpoint was about `11.25 GB`; a later successful compressed checkpoint was
about `418 MB`. The failure was lifecycle-safe for business data, but raw
retention consumed the filesystem and prevented the next full-checkpoint path.

The tactical contracts now available are:

- `apps/sqlite_backup_archive.py`: immutable `mode=ro`, `query_only=ON`
  planning; source stat/SHA/integrity and sidecar inventory; exact capacity
  reservation, including measured two-filesystem staging/destination contours;
  private unnamed cross-device staging plus fsynced archive/manifest;
  independent retained readback
  before raw removal; crash-safe `verified_pending_source_removal → retained`
  lifecycle; exact owned-sidecar cleanup; non-target directory digest; and
  idempotent resume;
- `apps/warehouse_cost_queue_replay.py`: query-only exact multi-invoice plan,
  exact queue revision and source totals, zero Finance-raw reads, zero full
  backup bytes, shared writer lock, durable pre-write checkpoints, exact
  functional publication, target-scoped economics undo and post-apply/no-op
  reconciliation.

These tactical paths prevent recurrence for the cleanup and two selected
commissions. They do not constitute the global policy below.

## Implementation truth

The policy implementation lives in
`packages/application/warehouse_recovery_policy.py`. It is the only warehouse
recovery component allowed to call `RegistryUploadDbBackedRuntime.backup_database`,
and that single call is reachable only from allowlisted T3 schema/store
migrations. Current bounded and wide warehouse/cost writers select their tier
through this module; legacy invoice-specific apply entrypoints fail closed.

Durable state is stored in the runtime SQLite recovery registry. T1 before
images, including reversible SQLite BLOBs, and undo rows remain inside that
registry. T2 artifacts live below
`state/warehouse-recovery/domain-checkpoints/` as private SQLite checkpoints
plus manifests; their table filter rejects `wb_finance_weekly_raw_rows` and
their schema/capacity inventory includes table, explicit/implicit index and
trigger closure.
Capacity reservations, artifact identity, CAS lifecycle, rollback expiry,
writer/timer state and orphan/quarantine status share the same registry.

Operator readback is
`GET /v1/sheet-vitrina-v1/warehouses/recovery`; the warehouse update tab renders
tier, scope, planned/actual/read bytes, lifecycle, next action, rollback,
capacity, writer/timer and orphan/quarantine state. The business-safe rollout
runner is `apps/warehouse_recovery_policy_canary.py`, exposed on the canonical
hosted runner as:

- `warehouse-recovery-canary-dry-run --deployed-sha <40-hex>`;
- `warehouse-recovery-canary-apply --deployed-sha <40-hex> --fingerprint <sha256>`;
- `warehouse-ui-flow --acceptance-profile warehouse_recovery_policy_20260726`.

The hosted wrapper verifies `.wb-core-runtime-sha` before either canary mode.
Apply creates no T0 row or bytes, proves one exact T1 marker replay and rollback,
creates one T2 domain checkpoint without a business-row mutation, compares the
warehouse-domain digest before/after and requires a clean orphan scan. The UI
profile requires the canary T1/T2 operations to be terminal and the recovery
surface to be visibly ready with zero orphan/quarantine leak. A later
deployed-SHA attempt releases only older canary-scoped failures whose durable
transition history proves that business mutation never began; ordinary failed
or quarantined operations remain fail-closed.

The scanner uses the first durable recovery operation as the activation
boundary for the pre-existing `backups/` tree. Known recovery-family files
whose filesystem identity predates that boundary are exposed as a separate
read-only pre-policy baseline, not attributed to a later canary. Any new file
or baseline identity touched after activation remains unclassified and blocks
acceptance. The classifier never adopts, removes or rewrites those legacy
files, so acceptance proves zero policy-era leak without silently hiding the
pre-existing inventory.

## Current call-site matrix

`Current bytes` are lower bounds at the measured production size. A coherent
full backup copies `11.50 GB` and reads at least the source plus the integrity
checked destination; compression and decompression verification add further
full-file reads.

| Operation | Mutation closure | Current copied/read bytes | Current failure/lifecycle | Required tier | Proposed bounded bytes | Operator signal |
|---|---|---:|---|---|---:|---|
| Semantic no-op for an exact queue or already-published fingerprint | none | some paths still scan operational sources; tactical replay copies `0` | historically inconsistent: some callers prepared recovery before proving no-op | T0 | `0` recovery bytes | explicit `would_change=false`, reason and stable fingerprint |
| Supplier document upload/status/commission confirmation | one shipment, document, expense lines, capital allocations and durable queue | no DB copy at the document write; downstream legacy recovery could later copy `11.50 GB` | queue can remain `queued`; status is distributed across supplier, capital and warehouse views | T1 | exact before-images and queue journal, normally KiB–MiB | one correlated operation/queue status with actionable error |
| Exact supplier-cost queue replay | selected queue revisions, affected nmIDs and dependent economics dates | tactical path: `0` copied, operational source scan only; target undo currently about `6.1 MB` accumulated across prior runs | durable staged audit; resumable functional and economics checkpoints | T1 | exact rows plus ready-snapshot before-images for affected SKU/date cells | queue, functional version and economics publication under one operation id |
| Supplier factual-date correction | one shipment plus dependent event dates, queues and publications | current path can create a full `11.50 GB` SQLite backup and run full integrity passes | job/audit exists, but backup can remain raw after a later drift/failure | T1 | exact before-images for the shipment, dated events, affected queues and derived publications | correction job plus recovery-tier/byte counters |
| FF ledger receipt, reservation, debit, write-off and WB auto-writeoff | append-only operations and selected checkpoint rows | normally `0` copied | append-only/idempotent evidence exists, but recovery signaling is local to each block | T1 | undo/tombstone journal for the exact operation closure | ledger operation id, reconciliation and retry state |
| WB supplies refresh/acceptance/cost-layer materialization | selected supply revisions, compositions and cost layers | ordinary source writes copy `0`; legacy recovery runners may copy the monolith | run/status tables are separate; historical one-off runners retain divergent lifecycle | T1 | exact supply/layer before-images and replay journal | supply revision, queue id and publication status |
| Targeted warehouse factual/cost publication | selected shipment/SKU/date closure | current reviewed targeted plans copy `0`; legacy paths and certification replay can copy `11.50 GB` | target undo exists for some paths, not as one enforced policy | T1 | affected balances/documents/states plus exact undo manifest | target publication/undo manifest and non-target digest |
| Functional economics targeted publication | affected nmIDs and dates plus dependent totals | target-scoped mode copies `0`; existing before-image manifests occupy `6.06 MB` | durable undo exists, but default callers may still request a full backup | T1 | exact changed snapshot cells and direct totals | plan, non-target digest, undo status and rollback action |
| Calculation-parameter update and dependent economics refresh | one parameter revision plus affected dates/totals | current daily recovery creates/archives a full `11.50 GB` checkpoint; planning and verification repeatedly read the full file | daily reuse/retention exists, but orphan detection is tied too closely to manifest-shaped artifacts | T1 | parameter row, affected ready-snapshot cells and totals | settings revision, dependent publication and undo status |
| Warehouse archival estimate/certification replay | selected estimate or supplier states and dependent read models | current implementations can call `backup_database`, copying `11.50 GB` | dedicated audit/rollback tables exist, but recovery volume is monolithic | T1 | exact version rows, active pointer and dependent cells | version/rollback state and byte counters |
| Hourly/manual warehouse sync | wide warehouse-domain publication from a fresh WB snapshot | current pre-sync checkpoint copies `11.50 GB`; integrity and archive lifecycle read multiples of that | source drift after backup can leave coherent raw; timer reports capacity failure but retention is not globally reconciled | T2 | one coherent warehouse/cost domain checkpoint, current measured upper bound `276,897,792` bytes | sync state includes checkpoint lifecycle, capacity reservation and orphan count |
| Emergency warehouse rebuild or rollback | wide warehouse/cost domain | current full backup/restore is `11.50 GB` | rollback is safe but couples recovery to Finance raw and global disk headroom | T2, unless schema-wide | domain checkpoint plus append-only source watermarks | rebuild/rollback operation with domain checkpoint id |
| Canonical-cost backfill/publication and historical Proxy/weekly Finance repair | selected cost dates/SKUs or wide derived publication | several runners copy the full DB or run full integrity scans | each runner owns a different backup/audit convention | T1 for bounded; T2 for wide domain | exact before-images, or one domain checkpoint for a wide publication | one recovery registry and affected-date/SKU scope |
| Warehouse/cost schema or store migration | explicitly allowlisted irreversible wide transformation | full `11.50 GB` backup is currently the only generic tier | coherent but expensive; raw artifacts can outlive the operation | T3 | full coherent backup is permitted and capacity-reserved | migration gate, backup lifecycle, cutover and rollback watermarks |
| Lossless retention of an already-created coherent raw checkpoint | one immutable backup file only | reads source integrity/SHA plus compression and repeated archive verification; frees raw size minus archive size | tactical runner now has crash-safe retained lifecycle | retention, not a mutation recovery tier | one `.zst` plus manifest | source/archive identity, lifecycle state and freed bytes |

Legacy invoice-specific runners remain migration evidence and must not be
treated as new normal call sites. The global task must either route them through
the policy or leave their mutation entrypoints disabled.

## Unified recovery policy

### Tier selection

Every mutation plan must declare one tier before any recovery bytes are
reserved:

1. **T0 — semantic no-op.** `would_change=false` means zero recovery bytes,
   zero temp files and zero integrity scans. The reason and exact source
   fingerprint are still audited.
2. **T1 — bounded undo journal.** A document/shipment/SKU/date closure stores
   exact before-images, an ordered undo journal, expected after-images,
   non-target digests and the target replay cursor. It must never call the
   generic full-database backup API.
3. **T2 — warehouse/cost domain checkpoint.** A publication that legitimately
   replaces a wide operational domain snapshots the domain store only. Finance
   raw is forbidden. The checkpoint pins source watermarks, schema revision,
   active pointers and append-only event cursors.
4. **T3 — full coherent backup.** Allowed only by a reviewed allowlist for
   irreversible schema/store migrations whose rollback closure genuinely spans
   every store included. Runtime business commands cannot select T3 by a
   free-form flag.

Tier selection is a deterministic policy function over mutation kind and
closure, persisted into the plan fingerprint and exposed in status.

### Durable lifecycle

All recovery artifacts use one registry with these states:

`planned → reserved → writing → verified → mutation_running → retained`

Terminal alternatives are:

- `released` after retention and rollback expiry;
- `rolled_back` after successful exact recovery;
- `failed_recoverable` with a machine next action;
- `quarantined` for corrupt or identity-drifted evidence.

Every transition is compare-and-set, fsynced and idempotent. A restart resumes
from the last durable state. A mutation cannot start before `verified`; raw
source removal cannot occur before `retained` readback. A failure never silently
drops the operation from status.

### Orphan scanner and retention

The scanner reasons about complete artifact families, not only
`*.zst.manifest.json`:

- raw SQLite, `-wal`, `-shm` and `-journal`;
- compression temp files;
- archives and manifests;
- T1 undo/before-image rows;
- registry entries without bytes and bytes without registry entries;
- audit records stuck in non-terminal states;
- expired retained artifacts and superseded generations.

It is read-only by default. Any cleanup plan is exact-fingerprint gated and can
only act on a lifecycle transition authorized by policy. Retention covers raw,
archive, manifest, temp, undo and registry states. A corrupt or incomplete
family is quarantined, never guessed to be disposable.

### Capacity contract

Reservations are first-class rows with filesystem identity, required bytes,
operational reserve, expiry and owning operation. A compare-and-set reservation
prevents two writers from independently consuming the same headroom.

Watermarks include:

- hard stop before a temp/checkpoint write;
- degraded operator warning before the hard stop;
- post-write minimum reserve;
- projected retention bytes and the next scheduled wide operation;
- actual-versus-planned byte counters.

T0 reserves zero. T1 reserves serialized before/after images plus journal and
margin. T2 reserves the measured domain checkpoint plus verification and
operational margin. T3 uses the measured coherent store size, verification
overhead and explicit migration reserve.

### Crash, retry and idempotency

Fault injection is required after every durable transition and immediately
before/after the business mutation. A repeated plan must either be a true no-op
or resume the same operation id. Source drift invalidates an unstarted plan; a
partially committed operation follows its recorded recovery cursor. Undo is
itself idempotent and verified with after-readback.

### Operator API and UI

The API returns, for every operation:

- operation id, kind, target scope and tier;
- planned/actual copied and read bytes;
- capacity reservation and watermarks;
- lifecycle state, last heartbeat and next executable action;
- exact source, checkpoint/undo and after-readback digests;
- orphan/quarantine status;
- timer/writer maintenance state;
- rollback availability and expiry.

The UI shows active and failed operations first, provides read-only artifact
family detail, and exposes only exact reviewed actions. It must distinguish
`waiting`, `recoverable`, `capacity_blocked`, `quarantined` and terminal states.

## Required tests and acceptance

- Static reachability test: every bounded mutation command fails CI if it can
  reach `RegistryUploadDbBackedRuntime.backup_database` or a full-store
  integrity/SHA scan.
- Policy table test: every registered operation maps to exactly one tier; new
  operations fail closed until classified.
- T0 test: semantic no-op creates no files, rows, reservations or read scans.
- T1 tests: exact before/after images, non-target digests, rollback, repeated
  no-op and bounded serialized bytes for supplier, commission, shipment,
  correction, FF, supply, certification, settings and economics regressions.
- T2 tests: Finance raw cannot be opened; domain checkpoint schema/watermarks
  are complete; wide publish and rollback reconcile.
- T3 tests: only explicit migration identifiers can select it.
- Fault injection after every lifecycle transition, write boundary and
  fsync/rename; restart must converge without orphan or duplicate mutation.
- Capacity races, expired reservations, low watermark and post-write reserve.
- Scanner fixtures for every raw/archive/manifest/temp/WAL/SHM/undo/registry
  combination, corruption and foreign non-target files.
- Retention tests over all lifecycle states, not filename patterns alone.
- Performance gates use byte counters: bounded regression cases copy `0`
  full-store bytes and never read Finance raw.
- Production canary demonstrates one no-op, one bounded replay and one wide
  domain publication; all reach terminal lifecycle and leave zero unclassified
  artifacts.
- Production UI Flow verifies final URL and redirect chain, visible render,
  non-empty title/body, no page errors/fatal surface/material console errors,
  operation detail, capacity status, actionable failure and terminal success.

## Ready `NEW_TASK` prompt

```text
КЛАСС ЗАДАЧИ: LOOP

Continuity: NEW_TASK. Репозиторий orenvlad-ai/wb-core.
Execution-контур: scope:live-runtime.

Цель: реализовать и довести до production UI acceptance единую recovery-policy
для всего warehouse/cost и зависимого economics-контура по authoritative design
migration/123_warehouse_recovery_policy_design.md. Комиссии — обязательный
regression case, но решение должно покрыть все current production call sites из
матрицы design.

Обязательный результат:
- централизованный deterministic tier selector T0/T1/T2/T3;
- T0 no-op создаёт 0 recovery bytes;
- bounded document/shipment/SKU/date paths используют exact before-images,
  undo journal и targeted replay и не могут достичь full backup/Finance raw;
- wide warehouse publication использует domain checkpoint без Finance raw;
- full coherent backup доступен только explicit allowlist schema/store migrations;
- единый durable lifecycle, CAS transitions, crash/restart/idempotency,
  capacity reservations/watermarks, полный orphan scanner и retention для raw,
  zst, manifest, temp, WAL/SHM/journal, undo и registry states;
- отсутствие silent failures; API/UI показывают tier, scope, planned/actual
  bytes, lifecycle, next action, writer/timer state, orphan/quarantine и rollback;
- legacy mutation entrypoints либо маршрутизированы через policy, либо остаются
  disabled;
- authoritative docs синхронизированы.

Acceptance:
- все тесты и production evidence из раздела Required tests and acceptance
  design выполнены;
- статический regression gate запрещает возврат full backup/full scan в любой
  bounded path;
- fault-injection/restart и concurrent capacity tests проходят;
- production canary no-op + bounded replay + wide domain publication достигают
  terminal lifecycle без raw/orphan leak и без non-target drift;
- production UI Flow проверен по актуальному протоколу и принят exact deployed
  SHA через Release Train LOOP.

Storage split из migration/124 в эту задачу не входит. Production business-data
mutation, migration или cleanup вне явно согласованного canary scope запрещены.
Выбор инструментов и источников не является требованием пользователя и всегда
перепроверяется по актуальному протоколу, если пользователь отдельно явно не
зафиксировал обратное.
```
