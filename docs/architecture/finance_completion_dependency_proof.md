# Finance and posted overhead completion proof

The v3 completion receipt uses `sqlite_dependency_proof_v1`. This replaces the
whole-main `data_version` guard with monotone, transactional revisions of tables
actually read. An unrelated UI or journal table commit cannot invalidate it.
The scope within an observed table remains deliberately conservative: a change
to another week in that table can invalidate a proof. No claim of row-level
conflict elimination is made.

The checked-in [machine manifest](../contracts/finance_completion_dependency_manifest.json)
contains reader closure, concrete fixture tables/columns/trigger hashes, native
identity dependencies, authority writers and the five durable commit owners.
Each actual receipt also records its complete observed table/column vector,
physical file identities, schema versions, classifier and cost-policy versions,
accounting/publication identities and source/target digests. The fixture list is
an observation, not a whitelist: an instrumented table reached by another branch
is automatically added by SQLite's authorizer. All persistent main/raw tables
have the same trigger writer contract, so a direct SQL writer and an older
application writer cannot bypass counters. TEMP view reads include their real
underlying raw table. Missing tables and fallback appearance are fenced by the
schema cookie. Missing or altered triggers are hard failures, never repaired by
a proof invocation. The explicit apply-side installer can add newly introduced
tables; it never resets existing counters or repairs a damaged contract.

## Consistency and locks

A before vector is captured before all dependent reads. Expensive native planner,
resolver, JSON/allocation and hashing work runs without a writer reservation.
`DetachedReadConnection` fetches each SQL result before caller Python processing,
so DELETE mode does not retain its SHARED reader while Python loops compute.
Unchanged monotone counters afterward establish an optimistic consistent read
image even if unrelated tables committed during calculation. Existing native
resolution (including noncanonical IDs and aliases) determines scope; no SQL
approximation filters it. There is no long SQLite snapshot and no WAL migration.

The book is decoded once from a live query-only autocommit observer. Its current
revision and every referenced compressed blob retain their existing hashes.
The same observer's token before/after decoding and through handoff closes
concurrent direct SQL changes; allocation and publication receive that detached
verified book. Accounting's three-table schema is unchanged.

At each of S1–S5, a caller-owned main writer attaches raw and book with `mode=rw`
and starts one `BEGIN IMMEDIATE`. SQLite reserves all attached stores, including
against non-cooperating direct SQL writers. Attachments receive no DML. The
vector, book observer, source identity and authority are checked inside the
writer before DML and again before commit. Connection nonce evidence is local to
that transaction and consumed by its final guard. A rollback cannot reuse it.
Own lifecycle mutations do not enter the Finance/Ready table vector. A separate
request-scoped progress fence carries exact queue, acceptance proof, request state,
recovery row, workflow events and recovery transition images. Each next BEGIN
must match the last exact own image; each pre-commit check allows only the named
S1–S5 field/event delta, binds the original S1 proof and Finance marker fingerprint,
and requires complete/retained/event/after-image evidence inside S5. Recovery
checkpoint and full immutable scope are frozen too. This is an exact own-row
delta, never a whole-table ignore list or global-token baseline adoption.
Targeted native overhead normalization does not call unrelated historical/current
FBO dispatch providers inside any writer reservation.

Application lock order is warehouse job ownership → shared generation pin →
FF posting → warehouse functional writer → accounting writer → attached SQLite
reservation. The existing main/raw Finance writer order is retained. Reservations
release on commit/rollback/connection close; a conflicting direct multi-store
writer may receive BUSY and fails closed. There is no blanket business retry.
Active split-store metadata rows are explicitly read under the authorizer after
the before-vector and compared to all five pinned manifest fields (schema,
logical role, generation ID/epoch and source fingerprint). Their observed
revisions then protect later stages. This also closes the gap between initial
StoreRegistry connection admission and revision capture; a matching path alone
is not authority. Manifest publication and schedule-selector transitions take an exclusive
generation pin. Inactive migration candidates may be replaced before activation;
the published authority switch is pinned. Existing-file SQLite backup restore
changes the destination schema cookie and invalidates an active proof. Arbitrary
external filesystem overwrite outside the authority protocol is not a supported
online restore mechanism.

## Durable recovery

S1 commits queue proof and acceptance proof together. S2 commits replay state and
its event. Guarded retention owns its own transaction and writes S3; callers do
not hold a nested main transaction. Already-retained operations must match the
same after digest. S4 commits native completion and its matching event. S5 marks
the acceptance completed. All later invocations use those same native identities.
An uncertain result is read back before continuing. None of these paths calls
posting, creates a replacement operation, or duplicates a retained transition.
The publication-incomplete branch remains pending; already-complete still needs
external source/recovery/event readback. Guided replay is explicitly inadmissible
for confirmed pool-overhead completion.

## Budget and measurements

Initial one-document fixtures measured the handoff, then the existing bounded
eight-document cohort was retained to share the full Finance proof. A two-document
fixture verifies three Finance source scans total, one semantic seal, and no
Finance raw/image scan under the writer; its maximum writer transaction was
9.15 ms after the exact progress fences. `cohort_size=1` remains available for
isolated measurements. The configured proof ceiling is 1800 seconds,
below the existing three-hour cycle interval; it is an operational ceiling, not
a demonstrated SLA. A caller may supply a smaller finite positive budget. The
same deadline starts before initial book loading and covers SQL progress,
Finance native row loops, allocation loops, readset hashing and 64-KiB book inflate
chunks. All readonly budget contexts close before mutation; begun transactions
finish or roll back. Deadline checks between documents may leave some completed
and some pending, but that result raises a typed failure with cumulative processed
and pending counts. It cannot be returned as warehouse success or admit final_ready. A C JSON/hash call remains indivisible. Representative-copy
measurement is required before claiming a strict wall-clock ceiling.

Local structural benchmark, 358 tables/1074 triggers, 10,000 simple inserts and
20 proof captures on the same Mac/filesystem:

| mode | insert baseline | insert with triggers | install | max capture | max fence |
| --- | ---: | ---: | ---: | ---: | ---: |
| DELETE | 4.04 ms | 8.66 ms | 42.37 ms | 1.74 ms | 0.029 ms |
| WAL | 2.73 ms | 8.29 ms | 39.06 ms | 1.49 ms | 0.024 ms |

Read-only production observation on 2026-11-06 found 285 referenced blobs in the
current book. The three largest **compressed** blobs sampled were 72,604→1,210,465,
59,913→832,732 and 58,316→824,837 bytes compressed→JSON. Their measured SQL read,
inflate, parse and canonical-hash times were respectively 0.22/3.03/15.12/17.66 ms,
0.14/1.27/11.19/13.12 ms and 0.11/1.07/9.70/12.53 ms. This is a sample of three
largest compressed blobs, not all blobs, worst possible expansion, or a future
size guarantee. Present sampled indivisible operations are far below the ceiling.

These are structural fixtures, not production throughput measurements. Native
small-fixture completion was about 360 ms; existing source business images were
unchanged. The fault suite covers each of five commits before and after a lost
response, both journal modes and both store layouts, plus unrelated writer load,
retention pre-BEGIN mutation, final-check-to-commit raw/book/main/manifest races,
nonce reuse, corruption, native legacy acceptance and source/allocation parity.

## Compatibility

The schema addition is one revision table and three triggers per existing table
in main/raw. Existing source and lifecycle schemas are unchanged; old readers and
writers continue to work and old writers advance the counters automatically.
Receipts add a versioned proof but retain the native completion envelope. Existing
v2 receipts are re-proven before continuation. A rollback must preserve all
committed operations and counter objects. The installer starts a transaction before its first DDL;
interruption rolls back the complete trigger/counter installation.

The combined release has an additional journal compatibility boundary: the old
`a07b6a5` journal reader raises `KeyError` on new phase rows. While maintenance
remains held and before any new-version job/cycle is accepted, rollback to that
verified old SHA remains compatible with the additive revision schema and the
unchanged three-table book. After the first new journal job/phase row is written,
use a forward fix or a specifically prepared compatibility backport; do not
roll back the application blindly and do not delete journal evidence.

Generation/table-renaming migrations
must explicitly migrate the trigger contract or will fail closed; dropping or
resetting counters during normal operation is not a rollback procedure.
