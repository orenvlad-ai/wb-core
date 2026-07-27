# Migration 124 — Finance raw and warehouse/cost storage split design

Status: **repository implementation complete through the inert maintenance
barrier, coherent-copy integrity gate, candidate/shadow/soak, atomic cutover and
reconciled rollback-drill capabilities**. The canonical production source
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
  preserves append-only history. Raw rows, batch links and one replay event
  commit in one SQLite transaction. The
  operational consumer is at-least-once, receipt-idempotent and keeps poison
  events actionable. After cutover the weekly owner acknowledges an event only
  after the exact seller/week row count, source hash, reports, aggregate,
  per-SKU, coverage and reconciliation projections all read back; a raw-first
  crash remains pending/actionable until the idempotent weekly replay completes
  those projections. Its live-tail bridge mirrors committed batches and outbox
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
  non-target invariants and rollback scope. The fingerprint excludes only
  volatile free-byte/PID/timer-clock counters that are rechecked immediately
  before apply.
- `packages/application/business_data_write_barrier.py` and the hosted HTTP
  adapter implement a durable fail-closed manual-write barrier. During exact
  snapshot/final-cutover/rollback windows authenticated `POST`, `PATCH` and
  `DELETE` business requests return `423`; UI controls are disabled with a
  visible banner, blocked attempts are audited without request bodies, and
  reads remain available. Invalid/private-state drift also fails closed.
- `business-data-maintenance` captures the exact prior owner policy, all known
  timers/writers/settings and warehouse timer state. Unknown writers/timers
  block a hold. Release is impossible until exact restore and readback have
  succeeded. Snapshot planning also blocks any already-active business writer
  service instead of entering a window that cannot drain. If an unconfirmed
  acquire fails after controls were paused, the only abort path proves the
  same pre-hold service generation is still running, restores the exact
  timer/settings signature and records `barrier-abort`; it cannot abort a
  confirmed hold or any window in which protected mutation began.
- `apps/finance_storage_split.py` defaults to `dry-run`. Its staged actions
  cover coherent snapshot, candidate creation, shadow activate/reconcile/tail/
  verify, cutover plan/apply and rollback plan/prepare/apply. Candidate build,
  shadow and soak occur without a broad maintenance hold. Cutover performs a
  fresh operational recopy and final raw tail under the short exact hold,
  fsyncs and atomically replaces the manifest. Rollback is prepared during
  normal operation, then replays only post-prepare raw scopes and recopies
  operational state under its short hold. Original monolith and split files
  remain retained.
- `apps/finance_storage_sqlite_open_inventory.py --check-migrated` inventories
  every Python SQLite open and rejects registry bypasses in migrated Finance
  and Partner runtime modules.
- hosted snapshot/split/shadow/cutover/rollback lifecycle commands are
  phase-local wrappers around the same repo-owned runner. Plan, health and
  status actions are read-only; every mutation action checks the active target,
  reviewed external evidence and exact approval. Deployment invokes none of
  them.

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
- Build indexes after bulk copy when this reduces total space/time, while the
  source remains canonical.
- Full row count, key-set digest, ordered business digest and sampled semantic
  reads must match. SQLite file hashes are evidence, not the logical equality
  criterion.

The live reader still uses the monolith.

Repository status: candidate-builder code and fixtures are implemented;
production execution is **not approved** by repository/deploy closure.

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
- health/UI shows generation ids, cursors, lag, failures and rollback readiness.

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
- Diagnostics requiring both stores pin one generation manifest, open both
  files `mode=ro`, set `query_only=ON` and use only bounded connection-local
  qualified reads or application-memory joins by stable identity.
- Production acceptance compares representative current query latency and lock
  wait. Warehouse timer lock time must improve or stay within an approved
  bound; Finance raw queries must not regress materially.

## Gates and reconciliation

Canonical hosted sequence is phase-local:

1. deploy inert code;
2. `finance-storage-snapshot-plan`, then the automatically held
   `finance-storage-snapshot-apply`, then
   `finance-storage-snapshot-integrity`;
   an active pre-existing writer service blocks the plan and leaves normal
   operation unchanged;
3. `finance-storage-split-dry-run` against that exact verified snapshot and
   stop for approval of its exact fingerprint/generation/capacity;
4. only after approval, `finance-storage-split-apply`, shadow activate,
   legacy reconcile, bounded live-tail applies and repeated shadow verify until
   the minimum soak becomes `ready`;
5. cutover plan/apply; the apply owns the final barrier/hold/restore and HTTP
   restart;
6. production readback/UI observation, rollback plan/prepare and rollback
   apply drill if the approved program calls for it;
7. never retire any retained generation in this lifecycle.

Every evidence/plan file is private and outside Git. A new plan fingerprint is
not normalized into an old approval. The initial program authorization permits
the short coherent snapshot window, but candidate/backfill/cutover still waits
for the explicitly requested fresh fingerprint approval.

Required gates are:

1. repository checks and schema/ownership review;
2. deploy of inert dual-store capability;
3. fresh query-only production baseline, short write-barrier snapshot and
   full coherent-copy integrity check outside the live DB;
4. exact coherent-snapshot dry-run and capacity reservation;
5. explicit human approval of exact plan/generation/fingerprint;
6. bounded backfill and shadow-read/soak evidence;
7. exact cutover maintenance gate;
8. post-cutover counts/digests, outbox drain, service/public/UI verification;
9. observation window and rollback drill;
10. separate old-generation retirement gate.

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
- Query-plan/index parity and bounded lock/performance tests.
- Capacity shortfall before any destination bytes, reservation races and
  restart.
- Durable HTTP/UI barrier restart, invalid-state fail-closed behavior, blocked
  request audit and exact release only after control restore.
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
