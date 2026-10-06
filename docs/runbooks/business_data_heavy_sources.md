# Shared heavy admission: Finance and official FBS sources

This candidate adds the bounded Finance/FBS coverage below to the earlier
[core producer integration](business_data_heavy_core_producers.md). It does not
activate the dormant cycle, a schedule, a profile or a nine-hour FBS policy.
No formula, source policy, SQLite schema, history epoch or publication CAS changes.
The supported lock environment remains Unix `flock` in the existing runtime.

## Admission boundaries

| Producer | Boundary and lifetime |
| --- | --- |
| Weekly Finance source | `sync_week`, `ingest_week`, split-outbox recovery and full backfill reserve before loading markers, source calls, raw/report/receipt acceptance and derived publication. Nested calls reenter the same actual worker owner. |
| Weekly Finance projection/cost | One/all/stale-week recalculation, candidate preview, canonical/stale planning and apply, SPP refresh and orphan repair reserve before schema, snapshots and scans, through their existing short CAS/acknowledgement. Query-only planning is also heavy work. The revoked old business-approved backfill remains revoked. |
| Daily Finance source/projection | `sync_day`, `tick`, pointer projection and source-free visible repair reserve before attempts, raw acceptance and scans, through their existing projection/recovery work. |
| Finance CLI | All commands except cached `status` reserve before block constructors, the daily worker lock and recovery before-images. Busy prints `status=busy`, `effects_started=false`, `retryable=true` and exits zero. Existing maintenance CLI handling is retained. |
| Official FBS collection | `collect` reserves before catalog/warehouse/stock acquisition, through both success and failed-attempt persistence and readback. Its explicit runtime root is required; deriving the root from a split SQLite path is unsafe. Read-only construction still permits an omitted root. CLI `collect` reserves before store/block construction; cached `readback` has no heavy reservation. |
| Explicit HTTP cost rebuild | The explicit `our_wb_cost` rebuild handler reserves before the first rebuild, through its Finance cost tail. |
| Exact FF overhead backfill | Whole `build_plan` and `apply`, plus their CLI dispatch before construction, reserve before scans, source projection, durable existing intent and Finance tail. Cached readback is unchanged. |

Root admission is independent maintenance SH, then nonblocking heavy EX, then
the existing domain locks/short SQLite transactions. A busy attempt does not
wait under SH or create a source attempt. No new thread startup algorithm or
idle-loop reservation is introduced. A source/cost exception or cancellation
unwinds the finite reservation through the existing context-manager finally.
Source error classifications and recovery receipts retain their old semantics.

## Nomenclature after-save continuation

The six nomenclature HTTP save/import/delete/barcode handlers commit their
short source operation first. Their Finance continuation now catches a busy
admission or maintenance pause as a separate `deferred` derived outcome. An
ordinary derived exception is a separate `failed` outcome. The original source
status and saved item payload remain intact. A non-successful source result
does not invoke the Finance tail. Exceptions are not turned into a false source
failure or permission to resubmit the source.

The bounded response proof contains the saved-item digest/count and reason,
`source_accepted=true`, `exact_revision_acked=false` and
`retry_policy=authoritative_source_stale_detection`. It is **not** a queued
operation or an acknowledgement that every intermediate revision was applied.
No second operator queue is added. Finance reads authoritative current source
identity with its existing raw/coverage/metrics and scoped dependency hashes;
stale projection detection survives process restart. The existing unconditional
warehouse weekly cost tail and the dormant cycle's visible daily repair can
repair that current source without another API collection or source save.

The offline regression commits a real nomenclature alias correction while a
separate process holds heavy admission. It verifies the successful source
response plus deferred Finance outcome, then starts a fresh process which runs
the actual warehouse Finance tail and daily source-free repair. The corrected
projections cease being stale, source revision and raw counts stay unchanged,
and repeated weekly repair is already current. This proof covers forward Finance
weeks and visible daily scope; it does not promise an automatic replay of the
frozen historical archive or acknowledgement of every superseded source revision.

## Remaining activation blockers

Independent supplies backfill/transit, standalone history, nomenclature dense
activation/drain and other supplier/CNY/FF after-save consumers remain outside
this candidate. Their source-owned intents and writer-lock ordering require
their separate integration. Migration/recovery administration and any other
unintegrated producer must not be inferred covered from a nested Finance guard.
There is no all-covered capability or global serialization claim.

Profile/dispatch integration, backup-priority checks, reviewed FBS reader policy
and the rolling14 total completion budget remain separate. The existing owned
history helper's one portion and incomplete-result behavior are not changed.

## Offline checks and read-only release validation

`apps/business_data_heavy_sources_smoke.py` checks real spawned-process busy
rejection before scans/constructors/effects, same-owner nesting, full acquisition
and commit/projection lifetime, error/cancellation cleanup, retained raw recovery
and the actual committed-source/restart repair described above. Existing Finance
canonical/stale-cost/scale, raw-storage, exact FBS, FF backfill, source activation,
core admission, cycle, ready/derive and immutable history checks remain relevant.

After a separately authorized release, observe `heavy_admission_status(runtime)`
and `admission_idle(runtime)` without provisioning. Read cached Finance/FBS and
operator/cycle state, and use read-only SQLite plus `PRAGMA query_only=ON` to
correlate the same raw pointers, attempt status and derived versions. Busy must
not introduce a fresh failed source attempt or consume pending work. A saved
nomenclature response must retain its source success and separate derived status.
Observe natural existing next-work recovery; do not resubmit sources, dispatch a
cycle, collect WB or activate a profile for this check. Legacy unintegrated
workers still require their path-specific idle proof at cutover.
