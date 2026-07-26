# Migration 121: global SQLite contention resilience

## Status and incident evidence

This is the authoritative migration contract for the July 2026 `database is locked` incident class.

Read-only production preflight on the canonical EU runtime confirmed:

- main store: `/opt/wb-core-runtime/state/registry_upload_runtime.sqlite3`, approximately 11.5 GiB with WAL and a 5-second default busy timeout before this migration;
- Autoanswers worker runs of roughly 24–27 seconds overlapped the 25 July 23:38 supplier statement failure and 23:58 WB regional calculation failure;
- Autoanswers readonly sync itself exhausted the same shared lock at 23:56:08 and 26 July 00:01:27 Asia/Yekaterinburg;
- Autoanswers tables occupy approximately 521 MiB and the host has enough bounded free capacity for an isolated verified copy;
- statement SHA `132901c6faaa83901ef445787b3b6f4bb4478f79ac2aa4b2a7dd95ae40c1569d` is already represented in shipment `26GN527`; shipment `26GN582` owns CNY payment №9 for 54,883 CNY.

No production rows or files were mutated during preflight.

## Writer inventory and transaction boundaries

The canonical main SQLite writers are:

- interactive registry HTTP operations: factory/WB regional calculation persistence, supplier shipment/document/CNY/financial confirmation, FF ledger, nomenclature/users/settings and warehouse operator actions;
- background vitrina/temporal refresh, Finance/partner materialization, supplier/warehouse targeted replay and warehouse functional sync;
- schema/bootstrap work, restricted to deploy or one successful per-process/per-inode verification.

Two high-frequency or staging contours are physically independent after migration:

- Autoanswers worker, readonly sync, lifecycle, maintenance evidence and recovery use `wb_autoanswers_runtime.sqlite3`;
- supplier confirmation/financial preview tokens use `supplier_confirmation_runtime.sqlite3`, while staged/content-addressed files live outside SQLite.

External HTTP calls, PDF/XLSX parsing, file generation, hashing, source migration and heavy calculations run before a write transaction. A main-store transaction contains only current-revision/conflict validation and the minimal row mutations/readback required for atomic publication.

## Shared contention contract

`packages/application/sqlite_contention.py` owns the connection contract:

- interactive budget: 30 seconds;
- background budget: 10 seconds with longer yielding backoff;
- individual SQLite attempt: 250 ms;
- exponential bounded backoff with jitter;
- retries only for `SQLITE_BUSY`/`SQLITE_LOCKED`;
- worker and readonly-sync process owners are always classified as background,
  including their direct query-only/status connections;
- warehouse functional sync keeps its process-local 120-second override;
- transaction rollback and business idempotency remain authoritative.

Sanitized JSON observability includes endpoint, operation, phase, the effective
connection priority, process owner, actual wait, retry count and
write-transaction duration. It excludes SQL, paths, payloads, documents, bank
data, credentials and secrets.

Bound exhaustion before commit returns `503`, `Retry-After: 2`, `retryable=true`, contract `wb_core_sqlite_contention_v1` and a Russian retry message. No raw SQLite error or partial committed business state is returned. Bank-fee confirmation commits its document/expense/assignment/CNY-document unit atomically; contention only in subsequent derived replay returns Russian `202 pending`, `operation_applied=true`, and exact idempotent resume.

## Autoanswers store split

`ensure_autoanswers_store` is a deploy-only versioned migration:

1. acquire the migration file lock and deployment quiet window;
2. stop the exact Autoanswers timers/services and registry HTTP service while retaining their prior state;
3. copy every legacy Autoanswers table from one query-only main-store snapshot into a private candidate;
4. compare per-table row counts and deterministic ordered row digests;
5. verify foreign keys, `integrity_check`, source `data_version`, fsync and capacity;
6. atomically publish `wb_autoanswers_runtime.sqlite3` and a mode-0600 manifest;
7. restore the registry service and exactly the timers that were previously
   active; interrupted one-shot work resumes idempotently on those timers.

Legacy main-store tables are retained for rollback. All ordinary Autoanswers processes resolve only the isolated path after publication. A prepared-before-rename manifest makes interruption recoverable only after full digest/integrity readback. `autoanswers-store-rollback-plan` plus exact-fingerprint `autoanswers-store-rollback-apply` snapshots the legacy table set, then reconciles current isolated queues/settings/audit back to legacy in one quiet-window transaction before an older-code deploy; non-Autoanswers registry tables and the isolated source remain unchanged.

## Bank statement source, preview and assignment

- One physical PDF and deterministic parse exist per SHA-256 under `supplier_financial_sources/sha256/...`.
- `supplier_financial_source_migration_v1` validates every byte, hard-links equal legacy copies to one content inode, atomically updates recorded paths, writes a private rollback manifest and supports verified no-op/reversal.
- Generic upload/confirm tokens are durable in the separate confirmation store; the bank-specific selection preview is a private fsynced sidecar beside the content-addressed source. Neither is active accounting evidence. A failed preview save removes owned staging; expiry performs bounded repo-owned cleanup.
- A DB-save failure after legacy file materialization removes that owned file immediately. Deploy also runs `supplier_financial_orphan_lifecycle_v1`: an unreferenced file older than 24 hours is moved out of the active tree into a private, hash-read-back quarantine, and only a still-unreferenced quarantine item older than 30 days is deleted with machine evidence.
- A logical group expands to the complete same-currency atomic tariff-debit set for one exact payment anchor, including mixed VK/SWIFT categories and distinct bank references. Durable sidecars pin `logical_grouping_version`; stale inactive previews are regenerated from the content-addressed source and stale confirm fails before writes. Global assignment identity prevents one atomic bank row from belonging to two shipments.
- Confirm requires exact SHA, target revision and selected operation IDs; document, expense, assignment and CNY bank-fee rows commit together and read back together. Same target is idempotent; another target is an explicit conflict, never a silent relink.
- The VTB fixture proves `26GN582` sees only payment №9 fees 951.08 + 12,574.81 = 13,525.89 RUB while operations №7/№11 already assigned to `26GN527` are not offered as new.

## Targeted recalculation

A confirmed cost-driving source change queues only its exact shipment and matched SKU set. Warehouse targeted replay derives the actually dependent stages/projections from canonical provenance. It does not load Finance raw/history and does not launch a global/full-history rebuild. Existing immutable-version, source/calculation fingerprint, conservation, certification and last-good publication gates remain unchanged.

## Deterministic verification

Required regression commands include:

```text
python3 apps/sqlite_contention_smoke.py
python3 apps/wb_autoanswers_store_rollback_smoke.py
python3 apps/supplier_financial_source_migration_smoke.py
python3 apps/supplier_financial_documents_smoke.py
python3 apps/supplier_confirmation_flows_smoke.py
python3 apps/cny_ledger_smoke.py
python3 apps/warehouse_targeted_replay_smoke.py
python3 apps/warehouse_functional_backup_smoke.py
PYTHONPATH=. python3 -m unittest apps.wb_autoanswers_activation_test apps.wb_autoanswers_runtime_test apps.wb_autoanswers_sync_test apps.wb_autoanswers_readonly_test apps.wb_autoanswers_lifecycle_test
```

The contention smoke holds an independent main-store writer longer than the former five-second limit while concurrently exercising WB regional, factory, supplier bank confirm and settings writes. It separately proves durable preview and isolated Autoanswers progress, rollback/no partial rows, controlled exhaustion and safe retry.

## Deploy, rollback and production acceptance

Deploy uses only the canonical hosted runner and Release Train. It performs the isolated-store and source-path migrations inside the repo-owned quiet window with backup/capacity/readback evidence. Ad-hoc production SQL/file cleanup is forbidden.

Production UI acceptance uses a fresh isolated Playwright context through `sqlite-contention-ui-flow`. It waits for a timer-owned Autoanswers worker/readonly-sync process without starting one, verifies calculations and the exact 26GN582 bank statement preview/cancel, Russian retry/resume presentation, no raw 5xx/fatal/pageerror/console surface and no orphan/partial accounting state. It must not confirm a real new bank posting. Machine-readable evidence is retained outside Git and the exact LOOP root reaches terminal `release:production`.
