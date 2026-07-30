# Migration 124 — Finance raw and warehouse/cost storage split design

Status: **repository implementation complete through the inert maintenance
barrier, coherent-copy integrity gate, candidate/shadow/soak, atomic cutover and
reconciled rollback-drill capabilities, including a global fail-closed Finance
migration deploy lease and an explicit recovery-continuity contract**. The
canonical production source
remains the monolith until the staged runner is deployed and a fresh exact
fingerprint receives separate human approval. Old-generation retirement is not
implemented by this runner and remains a later independent gate.

## Measured production boundary

The 2026-07-26 query-only production inventory measured:

| Contour | Allocated bytes including indexes | Rows / detail |
|---|---:|---|
| Current monolithic SQLite file | `11,501,142,016` | all runtime contours |
| Finance raw table and index | `10,468,679,680` | `2,414,082` raw rows |
| Derived `wb_finance_*` tables | `15,941,632` | `2,715` weekly SKU aggregates plus small summaries/audits |
| Warehouse/cost dependency closure | `276,897,792` | 50 tables |
| Ready snapshots within that closure | `103,550,976` | 189 |
| Functional balances + document lines | `121,147,392` | 11,872 + 11,890 |
| Persisted WB snapshots | `20,975,616` | 110 |

Moving only Finance raw does **not** imply a `140 MB` operational database.
The measured warehouse/cost closure is about `277 MB`, and the resulting
non-Finance-raw runtime generation also retains unrelated runtime contours
(feedbacks, Autoanswers, temporal sources, configuration and other modules).
At current allocation the non-raw remainder is about `1.03 GB` before rebuild
packing, schema headroom and future growth.

## Canonical ownership

The target runtime has logical stores resolved by repo-owned configuration,
never by feature code hardcoding filesystem paths:

1. **Finance raw store**
   - owned exclusively by Finance ingestion;
   - append-only raw report rows, ingestion batches, source file/checksum
     identity, immutable classifier/source metadata and a transactional outbox;
   - no warehouse, supplier, cost, ready-snapshot or operator mutable tables;
   - retention is Finance-source policy, independent from warehouse recovery.
2. **Operational store**
   - supplier documents/expenses and shipment state;
   - CNY ledger, own-capital events/state, FF ledger/reservations/writeoffs;
   - WB supplies/acceptance/cost layers;
   - warehouse functional versions, balances, documents, queues, checkpoints
     and cost certification;
   - calculation parameters, ready snapshots and dependent economics;
   - derived Finance aggregates/read models and all unrelated runtime contours
     that are not intentionally moved by this migration.
3. **Generation manifest**
   - one fsynced, atomically replaced repo-owned manifest selects exact raw and
     operational store generations plus schema revisions and watermarks;
   - processes open stores through a registry/factory and reject a mixed or
     incomplete generation.

Cross-store foreign keys are forbidden. Stable source identities and revisions
replace them at the boundary.

## Current repository implementation

The implementation is deliberately inert on deploy:

- `packages/application/storage_registry.py` resolves `finance_raw` and
  `operational`, records logical opens and defaults both stores to the existing
  `registry_upload_runtime.sqlite3` without creating a manifest or another
  database. A selected split manifest must bind one epoch, exact generation
  ids, schema revisions and the identities stored inside both database files;
  mixed files or schema identities fail closed.
- `packages/application/finance_raw_storage.py` owns the raw batch/row/outbox
  schema and the operational inbox/receipt/cursor/dead-letter/shadow schema.
  Immutable rows are linked to every containing source snapshot through
  `finance_raw_batch_rows`, so an unchanged row can participate in a later
  batch without duplication or a false count/digest. The
  `finance_raw_current_rows` view selects the latest exact weekly snapshot and
  preserves append-only history. Its current-selection predicate remains
  pushdown-safe: bounded seller/week reads use `finance_raw_rows_by_week`
  instead of materializing all history, while an all-history reconciliation
  batch supersedes every covered weekly batch without duplicating current
  rows. Raw rows, batch links and one replay event commit in one SQLite
  transaction. The
  operational consumer is at-least-once, receipt-idempotent and keeps poison
  events actionable. After cutover the weekly owner acknowledges an event only
  after the exact seller/week row count, source hash, reports, aggregate,
  per-SKU, coverage and reconciliation projections all read back; a raw-first
  crash remains pending/actionable until the idempotent weekly replay completes
  those projections. Before choosing the next due week, the scheduled owner
  drains only a bounded consecutive prefix whose exact operational
  receipts/cursor/source revisions already exist. It never replays a missing
  projection under that recovery path, and the raw acknowledgement does not
  reacquire a redundant schema DDL lock after its read phase proved the schema.
  Its live-tail bridge mirrors committed batches and outbox
  sequence into an unselected candidate with its own cursor; crash-before-event
  rolls back, crash-after-commit retries as a no-op.
- `packages/application/finance_storage_migration.py` builds a coherent
  short-hold SQLite backup and then performs the full `integrity_check` and
  foreign-key check on that immutable copy outside the live database. Planning
  and candidate creation use only the integrity-verified copy. The planner
  emits the complete table owner/read/write matrix,
  direct-open inventory, ordered logical digests, watermarks, chunks, actual
  query plans, allocation/capacity evidence, writers/timers and exact writable
  opener ownership, target generations,
  non-target invariants and rollback scope. The fingerprint excludes the
  separately validated top-level `deploy_lease` transport evidence plus only
  volatile free-byte/PID/timer-clock counters and transient systemd execution
  states that are recaptured immediately before apply. Stable systemd unit
  identity, load state and enablement remain approval-bound, so policy/config
  drift still fails closed while an ordinary scheduled service transition
  cannot stale an otherwise unchanged immutable-source candidate plan.
- `packages/application/finance_generation_filesystem.py` makes the active
  target's dedicated `state/generations` ext4 mount part of every hosted
  Finance migration contract. The target pins its exact filesystem UUID,
  label, type, required mount options and distinct-device requirement.
  Candidate, shadow, cutover and rollback paths must remain on that mounted
  device; their capacity checks use that filesystem, not the monolith/root
  filesystem. The identity is recaptured before destination creation and
  again before cutover/rollback writes. A missing mount, root fallback,
  symlink, UUID/label/source/options/device drift or insufficient fresh space
  fails closed before destination bytes. The registry HTTP systemd unit also
  requires the mount and refuses to start against the underlying root
  directory.
- `packages/application/business_data_write_barrier.py` and the hosted HTTP
  adapter implement a durable fail-closed manual-write barrier. During exact
  snapshot/final-cutover/rollback windows authenticated `POST`, `PATCH` and
  `DELETE` business requests return `423`; UI controls are disabled with a
  visible banner, blocked attempts are audited without request bodies, and
  reads remain available. Invalid/private-state drift also fails closed.
- `business-data-maintenance` captures the exact prior owner policy, all known
  timers/writers/settings and warehouse timer state. Unknown writers/timers
  block a hold. Release is impossible until exact restore and readback have
  succeeded. Snapshot planning blocks any already-active business writer
  except the two exact Autoanswers oneshots whose loaded/static service,
  positive PID and enabled/active paired timer prove the existing
  feature-lifecycle drain contract. For only those services, snapshot apply
  places the HTTP barrier first, disables timer retrigger, waits boundedly for
  the current PID without killing it, and still requires the same quiet-hold
  readback; missing/mismatched timer or service evidence remains a blocker. If
  an unconfirmed acquire fails after controls were paused, the only abort path
  proves the same pre-hold service generation is still running, restores the
  exact timer/settings signature and records `barrier-abort`; it cannot abort
  a confirmed hold or any window in which protected mutation began. If a nested
  warehouse restore completed before a later outer restore failure disabled
  every timer again, the retry reuses the original warehouse baseline only
  for the exact audited outer rollback footprint while the same unrestored
  boundary is either unconfirmed/acquiring or confirmed quiet
  `held/restoring`; it never treats arbitrary restored-state drift as
  recoverable.
- `business-data-maintenance-restore-submit|status` is the transport-independent
  recovery path for a long exact restore of either an unconfirmed acquiring
  window or a quiet confirmed hold that remains `held/restoring` after the
  protected snapshot completed.
  The submit request is bound to one caller-known job id, deployed SHA, policy
  revision, window/fingerprint, actor/reason and a freshly recaptured exact
  continuity fingerprint. The unconfirmed form contains unit/PID/start
  evidence; the confirmed form requires a fully quiet boundary, contains zero
  continuing services and stays bound to the confirmed barrier phase. A fixed
  repo-owned systemd template persists request, continuity evidence, status,
  result and append-only audit, survives SSH disconnect/restart, rejects a
  concurrent foreground or detached restore, and resumes only from exact
  maintenance/policy evidence; successful completion of that same bound service
  generation is accepted without guessing another writer. A bounded deadline and
  durable heartbeat classify stale, ambiguous and lost workers fail closed;
  status never starts another restore. Query-only inventory classifies every
  durable job plus the submit/worker/foreground restore locks; a fresh
  snapshot boundary is forbidden unless it proves zero non-terminal jobs and
  every global restore lock free. A terminal failure is not resubmittable
  through the original submit path. Only a reviewed recovery deploy may
  explicitly append the next binding and resume that same job, with the exact
  preceding-failure digest, unchanged original continuity boundary, archived
  attempt result, zero restore locks and no additional writer. The current
  incident contract admits at most three immutable contiguous recovery bindings
  (`resume.json`, `resume-2.json`, then `resume-3.json`) and therefore at most
  attempts 2, 3 and 4; it never creates a second job or automatically retries,
  and any fourth binding is rejected fail closed. Timer/service state and the
  freshness clock used for Autoanswers reconciliation share one observation
  timestamp taken before the feature-store readback, so a slow SQLite query
  cannot falsely expire a stale systemd snapshot. Restore acceptance binds
  Autoanswers to the successful feature-owned lifecycle reconcile readback plus
  a later outer systemd snapshot: lifecycle contract, desired mode, policy
  epoch, transition run, component timers, service results and observation
  order must all match. A redundant later feature-store
  `worker_unavailable` view cannot override that already bound `starting`
  evidence. Feature-owned non-blocking progress such as
  `reconciliation_in_progress` remains acceptable, while any lifecycle
  blocking stop reason, identity/component drift, failed service or inactive
  outer timer still blocks and returns the whole restore to the paused
  boundary.
  Barrier abort remains a separate exact transition after terminal restore and
  independent timer/writer/policy/non-target readback.
- A repeated business-maintenance `prepare` while the same boundary is already
  `prepared` or `held` and quiet is an audited no-op only when the paused
  owner-policy revision/fingerprint, original control signature and current
  desired timer/schedule/control intent match exactly. Any non-quiet state or
  drift remains fail-closed. The no-op never replays the feature lifecycle, so
  a freshly read deployment lease can reach the snapshot byte boundary inside
  its bounded freshness interval.
- The GitHub Release Train owns one global
  `finance:migration-deploy-lease` on a proven terminal production anchor PR.
  A paired `finance:migration-deploy-lease-audit` guard makes loss of either the
  active hold or audit label ambiguous/fail-closed until exact recovery; both
  are removed only after the durable terminal proof is written.
  Its Actions-owned binding audits exact canonical task id, anchor/head/deployed
  SHA, lease/window/phase, revision and bounded owner duration. Acquire is
  serialized with release selection and is rejected while any
  running/awaiting/halted deploy exists. A missing, expired, duplicate,
  partially written or otherwise ambiguous lease never opens silently: the
  global label keeps unrelated selection/merge/deploy blocked while Finance
  migration actions reject stale evidence. Only one exact owner-bound
  `task:standard + scope:live-runtime` recovery PR can pass the hold; after its
  deploy, explicit production-SHA rebind creates the next lease revision and
  invalidates every earlier baseline/snapshot/plan/fingerprint. Release or
  abort requires an OWNER/MEMBER reconciliation comment and Actions-owned
  readback proving exact deployed SHA, inactive/released manual barrier, full
  writers/timers/policy restore, unchanged non-target state and either exact
  migration abort on the monolith or completed post-cutover reconciliation on
  split storage.
- `apps/finance_storage_split.py` defaults to `dry-run`. Its staged actions
  cover coherent snapshot, candidate creation, shadow activate/reconcile/tail/
  verify, cutover plan/apply and rollback plan/prepare/apply. Candidate build,
  shadow and soak occur without a broad maintenance hold. Cutover performs a
  fresh operational recopy and final raw tail under the short exact hold,
  fsyncs and atomically replaces the manifest. Rollback is prepared during
  normal operation, then replays only post-prepare raw scopes and recopies
  operational state under its short hold. Original monolith and split files
  remain retained.
- `finance-storage-snapshot-retention-plan|apply|readback` is the only
  pre-candidate capacity recovery for stale coherent migration snapshots.
  It runs only while the monolith is canonical, the manual barrier is
  inactive, no split generation exists and the global Finance deploy lease is
  bound to the exact deployed SHA. Plan hashes every allowlisted snapshot
  file and proves the `backups` path is a different mounted device with a
  2 GiB reserve. Apply takes one non-blocking retention lock, copies only
  snapshots captured by an older deployed SHA, fsyncs and independently
  hashes every archive byte, then persists a crash-resumable transaction and
  archive manifest before removing the root-filesystem source files
  (snapshot manifest last). Disconnect/crash before archive verification
  leaves every source byte; after verification an exact repeat resumes only
  the bounded source release. Unknown files/openers, a new snapshot or
  generation, device/capacity/SHA/lease/barrier drift, or incomplete readback
  fail closed. The live monolith, candidate/split generations and business
  rows are never opened for write or removed; the archived snapshots remain
  byte-exact recovery evidence on the dedicated backup device.
- `finance-storage-candidate-abort-plan|apply|readback` is the only recovery
  path for either an interrupted pre-manifest candidate or an exact
  completed-but-unselected candidate invalidated by a later recovery deploy.
  It requires the implicit canonical monolith, an absent global manifest,
  inactive manual barrier and shadow ingest, exactly one target generation, no
  active candidate worker/open file or migration-lock owner, and a fresh exact
  Finance deploy lease. A completed candidate is eligible only after the
  repo-owned exact `shadow-deactivate` readback; its inactive shadow identity,
  candidate manifest and optional shadow-verification evidence must all bind
  the saved candidate plan and target generation. Plan recomputes the saved
  candidate-plan fingerprint, binds its old deployed SHA/source fingerprint/
  generation paths, checks both destination schema identities and requires
  either exact subset checkpoints for an interrupted candidate or complete raw
  checkpoints plus the exact saved `operational_copy.table_order` checkpoint
  inventory for a completed candidate. The broader owner/read/write matrix
  remains binding evidence but does not invent operational checkpoints for
  excluded raw/schema tables. All raw batches must be terminal committed. The
  delete allowlist is limited to the two candidate
  databases, their SQLite sidecars, the exact candidate/shadow manifests when
  present, and `migration_plan.json`; any symlink, directory, unknown file,
  active/mismatched shadow, selected global manifest, identity/checkpoint/
  process/control drift fails closed. The saved candidate fingerprint is
  recomputed with the original candidate planner's stable-field algorithm
  (including its volatile capacity/process exclusions), while the new abort
  plan uses its own fingerprint; the two approval domains are never
  conflated. Apply holds the normal non-blocking migration lock,
  persists a private fsynced transaction outside the candidate directory,
  journals each exact unlink, removes the saved plan last, fsyncs the
  generations parent and writes a durable result plus append-only audit.
  Disconnect/crash resumes only that transaction; a missing unjournaled file
  or reappearing/changed file is ambiguous and never starts a second candidate
  apply. Terminal readback proves the target generation/global manifest absent
  and the canonical monolith inode/schema, barrier, shadow state and complete
  snapshot directory inventory unchanged. Because the recovery deploy itself
  invalidates the old snapshot/plan/fingerprint, the required order is exact
  shadow deactivation when applicable, candidate abort, stale-snapshot
  retention, lease rebind and a completely fresh coherent
  snapshot/integrity/dry-run cycle; discarded candidate bytes are never reused
  after that deploy.
- A pre-snapshot `finance-storage-stale-writer-plan|stop` recovery is limited
  to the exact closure-retry oneshot generation. The service has a 30-minute
  start bound; recovery additionally requires at least one hour of continuous
  exact PID/start/cgroup identity, byte-equal deployed unit, active preserved
  timer/owner intent, released barrier, restored maintenance state, no
  runtime-store file descriptor, no internet socket and only the allowlisted
  Playwright children. Apply is fingerprint/approval/lease bound, writes a
  private append-only audit, calls one exact `systemctl stop`, preserves timer
  and policy, and never retries automatically. It cannot stop a current,
  unknown, connected or data-owning writer.
- `apps/finance_storage_sqlite_open_inventory.py --check-migrated` inventories
  every Python SQLite open and rejects registry bypasses in migrated Finance
  and Partner runtime modules.
- hosted snapshot/split/shadow/cutover/rollback lifecycle commands are
  phase-local wrappers around the same repo-owned runner. Plan, health and
  status actions are read-only; every mutation action checks the active target,
  reviewed external evidence and exact approval. All migration actions other
  than health, recovery-contract and exact query-only post-restore
  snapshot-status require a fresh private GitHub lease readback outside Git,
  no older than five minutes; the remote runner rebinds it to the canonical
  `.wb-core-runtime-sha`. Snapshot-status instead binds the reviewed plan,
  capture intent and persisted manifest directly to that canonical deployed
  SHA, so a long already-authorized hold/restore cannot fail only because the
  original readback aged. Deployment invokes none of them.
- `finance-storage-recovery-contract` is the query-only deployed capability
  readback. Before **every** Finance storage mutation the hosted wrapper runs
  `recovery-preflight` remotely before barrier acquisition or destination
  mutation. It recomputes the runner-owned deterministic fingerprint from the
  complete reviewed candidate/snapshot/retention/stale-writer/cutover/rollback
  plan before accepting the plan field; only the separately validated
  top-level `deploy_lease` transport evidence is excluded. The remote mutation
  runner repeats the same validation after an exact quiet hold for
  snapshot/cutover/rollback. The validator binds deployed SHA, lease
  task/id/revision/window/phase, approval reference, reviewed fingerprint,
  active generation, snapshot/candidate/rollback paths and identities, runner
  contract versions and all downstream durable restore/release capabilities.
  Missing, stale, unsupported or ambiguous evidence creates no barrier and
  fails closed.
- The snapshot plan is intentionally query-only while ordinary writers remain
  active. After the exact quiet hold, capture therefore re-reads the source
  identity and may bind ordinary data/mtime/page-count/freelist drift only when
  path, filesystem device, inode, page size, schema digest, journal mode and
  query-only contract are unchanged and fresh free space still covers the
  actual held allocation plus the reviewed reserve. The planned and actual
  identities and fresh capacity calculation are both persisted in the capture
  intent/manifest. Path/inode/schema/device/journal drift or insufficient
  headroom remains fail closed.
- A reviewed candidate/snapshot/cutover/rollback plan streamed over stdin is
  parsed once by the remote CLI. Snapshot/cutover/rollback reuse that exact
  in-memory object for the repeated recovery preflight and mutation; candidate
  apply validates it before destination bytes and then independently replans
  the immutable source under its own locks. A second read from exhausted stdin
  is forbidden and covered by regression evidence before deploy. The hosted
  wrapper attaches the current `deploy_lease` readback only after planning.
  That single canonical transport field is excluded from the deterministic
  plan hash and is instead validated independently against the deployed SHA,
  lease revision/window/phase and Actions-owned evidence fingerprint before
  every mutation. No other reviewed field is excluded.
- Snapshot restore uses a deterministic job id derived from deployed SHA,
  barrier window and reviewed plan fingerprint. The outer hosted wrapper first
  proves global restore inventory, submits that one repo-owned systemd job and
  observes only durable status. If the client/SSH disappears, exact
  re-dispatch from the same `restoring` barrier skips barrier acquisition,
  writer hold and snapshot copy, observes the same job, restores the nested
  warehouse boundary and releases only after terminal exact restore readback.
  Missing snapshot manifest after restore is reported only after controls are
  safely released and cannot cause the reviewed plan to replay.

### Durable recovery-continuity matrix

The matrix is emitted machine-readably by the deployed recovery contract. Its
documented classifications are:

| Persisted transition | Durable state | Exact restart behavior |
|---|---|---|
| snapshot acquire | write-barrier `absent/released → acquiring` | same window/kind/fingerprint resumes; other identity fails closed |
| snapshot hold | maintenance `preparing/holding → held` | exact control signature resumes as a no-op |
| snapshot copy | partial/final database and snapshot manifest | bounded held-source data drift is recaptured with stable identity/capacity proof; exact partial is rebuilt; structurally exact final-without-manifest is bound and published; dual/sidecar/stable-identity drift is ambiguous |
| snapshot restore | maintenance plus one deterministic durable restore job and global inventory | only the same digest-bound job may continue; re-dispatch observes it without replaying the snapshot |
| snapshot release | barrier `held/restoring → released` | exact restore readback is mandatory |
| candidate backfill | verified chunk ledger | exact verified chunks are re-read and skipped only while their original deployed SHA/snapshot/plan remains valid |
| candidate manifest | candidate bytes → shadow manifest | exact manifest readback is idempotent |
| candidate abort | private transaction/result/audit plus exact partial or completed-unselected candidate identity | exact allowlisted per-file release resumes from the fsynced journal; completed candidates additionally require every raw chunk and every table in the saved operational-copy inventory (not excluded raw/schema owner-matrix entries), plus exact inactive shadow/candidate-manifest bindings; an unjournaled absence, unknown file, active/mismatched shadow, selected manifest, opener or identity drift stays fail closed and never dispatches another apply |
| shadow activate | durable shadow state | exact candidate activation is a no-op |
| shadow reconcile | immutable raw batch/link rows | source identity and committed chunks resume idempotently |
| shadow live-tail | outbox event plus bridge cursor | event/sequence commit resumes without duplicate apply |
| shadow soak | verification evidence | observations append/reverify; readiness is never guessed |
| cutover pre-manifest | held monolith plus candidate | monolith remains canonical or exact retry continues |
| cutover post-manifest | atomic global split manifest | exact selected split is terminal/idempotent even after client loss |
| cutover release | restored controls plus barrier | restore first, then exact idempotent release |
| rollback prepare | partial/candidate evidence | exact candidate rebuild/readback only |
| rollback pre-manifest | held split plus rollback candidate | split remains canonical or exact retry continues |
| rollback post-manifest | atomic global monolith manifest | exact selected rollback generation recreates/reads terminal evidence without replay |
| rollback release | restored controls plus barrier | restore first, then exact idempotent release |

There is no “unknown means retry” branch. A restoring barrier, mismatched
generation, unknown writer, missing retained generation, stale result, unsafe
path, concurrent restore or unsupported runner version remains held/fail-closed
and never starts another restore or guesses a manifest.

The offline production-shaped rehearsal covers the complete
snapshot→restore→release and
candidate→shadow→cutover→post-manifest-restart→rollback→post-manifest-restart
control-plane paths. It injects disconnect/crash boundaries around partial
snapshot copy, database-before-manifest, candidate chunks, raw/outbox commits,
live-tail cursor acknowledgement and both atomic manifest switches. The
durable detached restore suite separately kills the submitting client and
proves system-owned continuation, bounded heartbeat/deadline classification,
result/audit persistence and the prohibition on a second job.
The candidate-abort fault suite separately crashes after a persisted unlink
for both partial and completed-unselected candidates and proves exact resume,
terminal no-op replay, durable result/audit and unchanged
monolith/snapshot/non-target evidence. It also proves that an unknown file,
active or mismatched shadow, or selected global manifest blocks recovery before
deletion.

The private `.finance-storage-shadow-ingest.json` state defaults absent/off.
If a later reviewed stage enables it while the implicit monolith is selected,
legacy Finance writes and the new raw/outbox rows share the same transaction.
It fails closed if the logical files differ. This is shadow infrastructure,
not a canonical-writer switch.

The operator Finance card exposes generation ids, schema revisions, raw and
operational cursors, lag, mismatches, dead letters, filesystem free bytes and
rollback/cutover readiness. Reading it creates no schema and performs no
business mutation.

## Schemas and event flow

The Finance raw store introduces:

- `finance_raw_ingest_batches(batch_id, source_identity, source_sha256,
  report_period, row_count, rows_digest, status, created_at, committed_at)`;
- `finance_raw_rows` with the current business identity and an immutable
  `batch_id`;
- required raw lookup indexes proven from current query plans;
- `finance_raw_outbox(event_id, batch_id, sequence_no, event_type,
  payload_json, payload_sha256, created_at, published_at, attempt_count,
  last_error)`;
- `finance_raw_consumer_cursors(consumer_id, last_sequence_no,
  last_event_id, updated_at)`.

The ingestion transaction appends the batch, raw rows and outbox events in the
same Finance-raw transaction. It never opens the operational store for write.

An at-least-once repo-owned consumer applies events to operational derived
Finance tables using:

- unique `(consumer_id, event_id)` receipts;
- compare-and-set source revision/watermark;
- deterministic bounded weekly replay plus complete projection readback before
  acknowledgement;
- a dead-letter/actionable retry state, never silent skip;
- read-after-write row count and digest.

Warehouse/cost consumers use derived operational tables and source revisions.
They do not scan Finance raw during an ordinary publication or recovery.

## Staged implementation and migration

### Stage 0 — repository inventory and abstraction

- Freeze an authoritative table/owner/read/write matrix from current main and
  production schema.
- Instrument every SQLite open with logical store, mode and operation.
- Introduce the store registry while both logical stores still resolve to the
  monolith. CI rejects direct runtime DB opens from migrated modules.
- Record per-operation query plans, lock time and bytes read.

No production data movement occurs.

Repository status: implemented. The full per-table matrix and full direct-open
list are machine-readable parts of every dry-run rather than a manually
maintained partial list.

### Stage 1 — schemas, outbox and shadow infrastructure

- Add raw-store schema and migrations, operational inbox/receipt/cursor schema,
  generation manifest validation and health/status APIs.
- Ingestion writes raw+outbox atomically to the existing generation; the
  consumer builds the same derived results and compares them to current main.
- Add fault injection for crash before/after raw commit, outbox claim,
  operational apply and acknowledgement.

No read cutover occurs.

Repository status: implemented but disabled by default. No production shadow
ingest or operational replay is enabled by deployment.

### Stage 2 — exact dry-run and capacity reservation

The repo-owned migration runner is dry-run by default and emits:

- deployed SHA and schema revisions;
- exact source file identity and query-only source fingerprint;
- Finance raw row count (`2,414,082` at the evidence baseline), min/max
  business watermarks and ordered chunk manifest;
- per-chunk and full logical row digests independent of SQLite page layout;
- per-chunk and full length-framed `raw_json` payload digests, so an unchanged
  business key/hash cannot hide destination payload corruption;
- current derived and operational table row counts/digests;
- projected destination sizes from real shadow chunks, not the `277 MB`
  warehouse-only subset;
- filesystem identity, required bytes, operational reserve, rollback
  generation and shortfall;
- exact writers/timers and lock acquisition plan;
- expected target generation ids and non-target invariants.

The capacity model keeps the old monolith as rollback evidence while creating
both new stores. At the measured baseline it must reserve at least the measured
raw allocation, the complete non-raw generation, index-build overhead,
verification scratch and an operational margin. Apply is forbidden until a
fresh exact dry-run and explicit human gate match.

Repository status: implemented. A production dry-run is valid only when it
reports `mode=ro`, `PRAGMA query_only=1`, zero mutations, zero destination
bytes, exact deployed SHA, per-object `sqlite_dbstat` allocation and sufficient
capacity. The output file stays outside Git with mode `0600`.

### Stage 3 — raw backfill

- Copy immutable raw rows in bounded primary-key/source-period chunks.
- Each chunk is idempotent and records source count/digest, destination
  count/digest, byte counters and watermark.
- Resume skips verified chunks and rechecks their digests.
- Operational tables use a plan-bound topological order derived from the
  immutable snapshot's foreign keys, so every cross-table parent is committed
  before its dependants even when alphabetical order is unsafe. Self
  references are deferred only within that table's transaction. A cycle,
  ordering drift or final `foreign_key_check` violation fails before candidate
  manifest publication.
- A crash after any verified operational-table checkpoint resumes from the
  same saved plan: verified tables are re-read by count/digest and skipped,
  while the global manifest remains absent until all tables, schema objects
  and the final foreign-key check succeed.
- Build indexes after bulk copy when this reduces total space/time, while the
  source remains canonical.
- Full row count, key-set digest, ordered business digest and sampled semantic
  reads must match. SQLite file hashes are evidence, not the logical equality
  criterion.

The live reader still uses the monolith.

Repository status: candidate-builder code, dependency-order and
operational-checkpoint restart fixtures are implemented. Production execution
still requires the external exact plan/fingerprint/lease gate; deployment
itself never starts a candidate.

### Stage 4 — dual/shadow read and live-tail catch-up

- New ingestion commits to the canonical writer generation and emits outbox
  events; a generation bridge mirrors the live tail to the shadow raw store
  idempotently.
- Shadow Finance reads run against both sources and compare result digests,
  latency and query plans.
- The operational outbox consumer proves the same derived aggregates, ready
  inputs and warehouse/cost source revisions.
- A bounded soak window must have zero unexplained mismatches and zero
  unacknowledged outbox events.

There is no distributed transaction: raw commit is authoritative, and every
operational effect is replayable from the durable outbox.

Repository status: implemented and disabled by default. All-week comparison
persists exact candidate/generation evidence, requires zero mismatch/lag/
duplicates and enforces a bounded observation duration before cutover can
become machine-ready. Production candidate bytes, live-tail and soak have not
started.

### Stage 5 — cutover

- Acquire only the exact Finance ingestion/derivation writers and affected
  warehouse/cost publication boundary; unrelated services remain running.
- Recheck dry-run identity, capacity, source tail and outbox drain.
- Apply the final tail, verify counts/digests, fsync both stores and atomically
  switch the generation manifest.
- Restore writers to their exact prior allowed state.
- Read paths fail closed on mixed generation or cursor lag beyond policy.

The old monolith remains immutable rollback evidence.

Repository status: implemented behind exact plan/fingerprint/approval and
held-barrier readback, but not authorized or run in production. The hosted
runner restarts only `wb-core-registry-http.service`; an ambiguous failure
after manifest switch leaves the write barrier and writer holds active for
bounded recovery.

### Stage 6 — rollback and observation

During the observation window:

- every new raw event remains replayable into the old generation;
- a rollback first prepares a new monolith generation, drains/replays the
  post-prepare tail into it, verifies derived and warehouse/cost watermarks,
  and only then atomically switches the generation manifest; the original
  pre-cutover monolith stays immutable evidence;
- no manual cross-store SQL is allowed;
- health/UI shows generation ids, cursors, lag, failures and rollback readiness;
- the authenticated UI acceptance recognizes only the exact implicit-monolith
  or selected-split phase. Selected split requires one generation epoch across
  the manifest/raw/operational stores, exact schema revisions, zero pending
  outbox/consumer lag/cursor mismatch/mismatches/dead letters and the retained
  `monolith` rollback generation. Transitional shadow, mixed or unhealthy
  identities stay fail closed.

Repository status: rollback plan/prepare/apply is implemented but inert.
Preparation builds and fully checks a new rollback monolith without modifying
the retained original. Under the rollback hold, post-prepare raw scopes are
replayed, all operational tables are freshly recopied and logically verified,
then one monolith manifest is selected atomically. A post-switch ambiguity
keeps the barrier fail closed. Both the original monolith and split generation
remain retained.

### Stage 7 — old-generation retirement

After the observation window, production reconciliation and a separate exact
human gate:

- archive the old monolith through the global T3 lifecycle from migration 123;
- verify retained archive and rollback-expiry policy;
- remove old raw bytes only through the lifecycle runner;
- prove no code/process still opens the retired generation;
- retain audit manifests and logical digests.

An in-place `DELETE` plus `VACUUM` is not the migration strategy: it creates
large temporary capacity and lock risks and obscures the generation rollback
boundary.

## Read paths and performance

- Finance raw/history endpoints read only the raw store.
- Finance weekly derived APIs and operator dashboards read operational
  projections and expose their raw outbox watermark.
- Warehouse/cost planning reads only operational sources. It must report
  `finance_raw_rows_read=0`.
- Finance projection code may attach the registry-selected raw generation
  `mode=ro` as a connection-local schema and expose only a temporary
  `finance_raw_current_rows` compatibility view while writing derived rows to
  operational storage. The attach is identity-checked and observed by the
  registry; no persistent cross-store view, trigger or foreign key is allowed.
- The all-history canonical Finance dry-run uses that same pinned attach in
  read-only mode and restores `PRAGMA query_only=ON` before any data query.
  Its apply may change only reviewed operational projection rows; the selected
  raw generation remains a digest-bound non-target.
- Bounded current-row reads must keep seller/week predicates inside the indexed
  lookup plan and must not scan/materialize all historical rows. Removing a
  weekly scope and all-history reconciliation must both retain one deterministic
  latest committed batch per covered scope.
- Diagnostics requiring both stores pin one generation manifest, open both
  files `mode=ro`, set `query_only=ON` and use only bounded connection-local
  qualified reads or application-memory joins by stable identity.
- Production acceptance compares representative current query latency and lock
  wait. Warehouse timer lock time must improve or stay within an approved
  bound; Finance raw queries must not regress materially.

## Gates and reconciliation

Canonical hosted sequence is phase-local:

1. after any recovery incident, first prove the manual barrier released and
   exact writers/timers/policy/non-target state restored;
2. deploy and read back the inert recovery-continuity contract, then rebind the
   owner-bound lease to that exact SHA; this recovery deploy invalidates every
   earlier snapshot/plan/fingerprint;
3. independently read back the rebound global Finance migration deploy lease
   on the exact current deployed SHA; any pre-acquire/rebind deploy or
   SHA/schema drift invalidates prior baseline/plan/fingerprint evidence;
4. if the fresh capacity preflight is blocked by stale coherent snapshots,
   run the exact lease-bound
   `finance-storage-snapshot-retention-plan|apply|readback`; any recovery
   deploy invalidates the prior snapshot evidence, so archive only
   older-SHA snapshots and restart this sequence from a fresh lease readback;
5. `finance-storage-snapshot-plan`, then the automatically held
   `finance-storage-snapshot-apply`, then
   `finance-storage-snapshot-integrity`;
   an active pre-existing writer service blocks the plan and leaves normal
   operation unchanged; a proven stale closure-retry generation uses the
   separately reviewed lease-bound recovery above before a completely fresh
   snapshot plan;
6. `finance-storage-split-dry-run` against that exact verified snapshot and
   stop for approval of its exact fingerprint/generation/capacity;
7. only after approval, `finance-storage-split-apply`, shadow activate,
   legacy reconcile, bounded live-tail applies and repeated shadow verify until
   the minimum soak becomes `ready`;
8. cutover plan/apply; the apply owns the final barrier/hold/restore and HTTP
   restart;
9. production readback/UI observation, rollback plan/prepare and rollback
   apply drill if the approved program calls for it;
10. release the global deploy lease only after exact abort or post-cutover
   reconciliation plus full SHA/writer/timer/policy/barrier/non-target
   readback;
11. never retire any retained canonical/split generation in this lifecycle.

Every evidence/plan file is private and outside Git. A new plan fingerprint is
not normalized into an old approval. The initial program authorization permits
the short coherent snapshot window, but candidate/backfill/cutover still waits
for the explicitly requested fresh fingerprint approval.

Required gates are:

1. repository checks and schema/ownership review;
2. deploy of inert dual-store capability;
3. active/readback-proven global Finance deploy lease on current production
   SHA, with bounded owner duration and owner-bound recovery policy;
4. fresh query-only production baseline, short write-barrier snapshot and
   full coherent-copy integrity check outside the live DB;
5. exact coherent-snapshot dry-run and capacity reservation;
6. explicit human approval of exact plan/generation/fingerprint;
7. bounded backfill and shadow-read/soak evidence;
8. exact cutover maintenance gate;
9. post-cutover counts/digests, outbox drain, service/public/UI verification;
10. observation window and rollback drill;
11. evidence-bound global deploy-lease release;
12. separate old-generation retirement gate.

Closure requires:

- exact source/destination row counts and logical digests for raw and derived
  data;
- no missing/duplicate outbox event;
- operational non-target digests unchanged;
- warehouse/cost active versions and queues reconcile;
- repeated runner is a no-op;
- writers/timers are restored exactly;
- no unclassified raw/temp/WAL/SHM/archive artifact;
- old generation is either retained within policy or retired through the
  verified T3 lifecycle.

## Required tests

- Store registry rejects direct paths, mixed generations and wrong schema.
- Raw ingestion and outbox atomicity under every crash point.
- At-least-once duplicate delivery, reordered retry and poison event handling.
- Backfill chunk resume, source drift, corrupt destination and digest mismatch.
- Shadow-read equality for Finance raw reports, derived weekly summaries and
  warehouse/cost source revisions.
- Query-plan/index parity, seller/week predicate-pushdown, all-history
  reconciliation semantics and bounded lock/performance tests.
- Capacity shortfall before any destination bytes, reservation races and
  restart.
- Candidate-plan fingerprint stability across transient systemd execution
  transitions, with unit identity/load/enablement drift still rejected.
- Snapshot planning admits only exact drainable Autoanswers oneshots with
  their enabled active paired timers; mismatched timer/service identity and
  every other active writer remain fail-closed.
- Exact stale-writer generation drift, runtime FD/socket/child-process
  rejection, stop failure without retry, timer/policy preservation and
  idempotent terminal audit.
- Durable HTTP/UI barrier restart, invalid-state fail-closed behavior, blocked
  request audit and exact release only after control restore.
- Recovery-contract transition completeness and stable fingerprint; preflight
  must reject missing lease/approval/downstream capability before creating a
  barrier or destination byte.
- Candidate, snapshot, snapshot-retention, stale-writer, cutover and rollback
  apply must accept the wrapper-added canonical `deploy_lease` transport
  evidence without changing the reviewed deterministic plan fingerprint,
  while independently rejecting any altered reviewed-plan field or lease
  SHA/revision/window/phase/evidence-fingerprint drift before a barrier or
  destination mutation.
- Disconnect/crash rehearsal at each persisted transition, including snapshot
  partial/final-without-manifest, cutover post-manifest and rollback
  post-manifest exact resume; ambiguous variants must remain fail closed.
- Coherent snapshot capture under a short hold and full offline integrity gate;
  live monolith never receives a long full-database scan.
- Cutover atomic manifest switch, process restart and tail catch-up.
- Rollback with post-cutover writes and exact forward replay into the old
  logical monolith generation while retaining the original file.
- Non-target runtime contours remain byte/logically unchanged.
- Full production readback and public/operator UI verification.

## Ready `NEW_TASK` prompt

```text
КЛАСС ЗАДАЧИ: СТАНДАРТ

Continuity: NEW_TASK. Репозиторий orenvlad-ai/wb-core.
Execution-контур: scope:production-mutation с staged repository/deploy/migration
closure.

Цель: реализовать staged разделение большого immutable/append-heavy Finance raw
и mutable operational warehouse/cost/economics storage по authoritative design
migration/124_finance_raw_storage_split_design.md, сохранив replayable
transactional-outbox boundary вместо distributed transaction.

Границы:
- сначала current main/GitHub/production read preflight и актуальная
  table-owner/read-write matrix;
- Stage 0–1 repository implementation и inert deploy не зависят от production
  mutation gate;
- никакой production data mutation до свежего query-only exact dry-run,
  capacity reservation и отдельного explicit human approval exact
  plan/generation/fingerprint;
- никакого ad-hoc SQL, server-only script, in-place DELETE/VACUUM или остановки
  несвязанных сервисов;
- old monolith остаётся rollback generation до observation/retirement gate.

Обязательный результат:
- logical raw/operational store registry и atomic generation manifest;
- Finance raw schema, ingest batches, transactional outbox, operational inbox/
  receipts/cursors и at-least-once idempotent replay;
- chunked resumable raw backfill, live-tail catch-up, shadow/dual reads,
  row counts/digests/query-plan/performance evidence;
- exact bounded cutover writers, atomic manifest switch, restart and rollback
  with post-cutover tail replay;
- warehouse/cost paths читают 0 Finance raw rows;
- staged reconciliation, writer/timer restore, no orphan/raw leak;
- old-generation retirement только через отдельный exact gate и verified T3
  lifecycle;
- authoritative docs синхронизированы.

Measured baseline для повторной проверки, а не для слепого принятия:
monolith 11,501,142,016 bytes; Finance raw 10,468,679,680 allocated bytes и
2,414,082 rows; warehouse/cost dependency closure 276,897,792 bytes; derived
Finance 15,941,632 bytes. Apply использует только свежие фактические значения.

Acceptance/closure:
- все tests/gates/reconciliation из migration/124 выполнены;
- PR прошёл semantic review/checks и применимый Release Train closure;
- inert capability задеплоена до migration gate;
- после human-approved migration source/destination counts и logical digests
  совпадают, outbox drained, no duplicates/loss, non-target invariants
  unchanged, repeated run no-op;
- service/public/operator readback успешен, writers/timers восстановлены;
- rollback доказан, old generation retained либо отдельно безопасно retired;
- итог содержит PR, merge/deploy SHA, exact machine manifests/evidence без
  secrets.

Выбор инструментов и источников не является требованием пользователя и всегда
перепроверяется по актуальному протоколу, если пользователь отдельно явно не
зафиксировал обратное.
```
