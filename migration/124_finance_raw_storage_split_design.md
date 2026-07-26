# Migration 124 — Finance raw and warehouse/cost storage split design

Status: staged design only. No storage migration is performed by the
production-cleanup task that introduced this document.

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
- deterministic rebuild from raw for a bounded report period;
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

### Stage 1 — schemas, outbox and shadow infrastructure

- Add raw-store schema and migrations, operational inbox/receipt/cursor schema,
  generation manifest validation and health/status APIs.
- Ingestion writes raw+outbox atomically to the existing generation; the
  consumer builds the same derived results and compares them to current main.
- Add fault injection for crash before/after raw commit, outbox claim,
  operational apply and acknowledgement.

No read cutover occurs.

### Stage 2 — exact dry-run and capacity reservation

The repo-owned migration runner is dry-run by default and emits:

- deployed SHA and schema revisions;
- exact source file identity and query-only source fingerprint;
- Finance raw row count (`2,414,082` at the evidence baseline), min/max
  business watermarks and ordered chunk manifest;
- per-chunk and full logical row digests independent of SQLite page layout;
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

### Stage 5 — cutover

- Acquire only the exact Finance ingestion/derivation writers and affected
  warehouse/cost publication boundary; unrelated services remain running.
- Recheck dry-run identity, capacity, source tail and outbox drain.
- Apply the final tail, verify counts/digests, fsync both stores and atomically
  switch the generation manifest.
- Restore writers to their exact prior allowed state.
- Read paths fail closed on mixed generation or cursor lag beyond policy.

The old monolith remains immutable rollback evidence.

### Stage 6 — rollback and observation

During the observation window:

- every new raw event remains replayable into the old generation;
- a rollback first drains/replays the post-cutover tail into the old monolith,
  verifies derived and warehouse/cost watermarks, and only then atomically
  switches the generation manifest back;
- no manual cross-store SQL is allowed;
- health/UI shows generation ids, cursors, lag, failures and rollback readiness.

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
- Diagnostics requiring both stores open each `mode=ro`, set
  `query_only=ON`, pin the generation manifest and join in bounded application
  memory by stable identity.
- Production acceptance compares representative current query latency and lock
  wait. Warehouse timer lock time must improve or stay within an approved
  bound; Finance raw queries must not regress materially.

## Gates and reconciliation

Required gates are:

1. repository checks and schema/ownership review;
2. deploy of inert dual-store capability;
3. fresh query-only production dry-run and capacity reservation;
4. explicit human approval of exact plan/generation/fingerprint;
5. bounded backfill and shadow-read evidence;
6. exact cutover maintenance gate;
7. post-cutover counts/digests, outbox drain, service/public/UI verification;
8. observation window and rollback drill;
9. separate old-generation retirement gate.

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
- Cutover atomic manifest switch, process restart and tail catch-up.
- Rollback with post-cutover writes and exact forward replay into the old
  generation.
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
