# Hosted Runtime Deploy Contract

## Purpose

Этот документ фиксирует минимальный repo-owned deploy/publish contract для already materialized hosted runtime family вокруг `registry_upload_http_entrypoint_block` и `sheet_vitrina_v1`.

Цель bounded шага:
- убрать hidden operational knowledge о target routes и проверках;
- дать один canonical runner для `deploy -> loopback probe -> public probe`;
- не коммитить secrets и materialize-ить repo-owned runtime/timer wiring без ручного host drift.

## Canonical Scope

Contract покрывает active EU hosted contour на `https://api.selleros.pro` через EU host `89.191.226.88` для routes:
- `POST /v1/registry-upload/bundle`
- `POST /v1/cost-price/upload`
- `POST /v1/sheet-vitrina-v1/refresh`
- `POST /v1/sheet-vitrina-v1/load`
- `GET /v1/sheet-vitrina-v1/daily-report`
- `GET /v1/sheet-vitrina-v1/stock-report`
- `GET /v1/sheet-vitrina-v1/plan-report`
- `GET /v1/sheet-vitrina-v1/wb-finance-report`
- protected Partner Report `options`, `settings`, `preview` and `preview.xlsx` routes; no active finalization/ZIP/raw Finance export
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx`
- `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload`
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-status`
- `GET /v1/sheet-vitrina-v1/feedbacks`
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt`
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints`
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/sync-status/job`
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected`
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job`
- `GET /v1/sheet-vitrina-v1/prices/goods`
- `POST /v1/sheet-vitrina-v1/prices/preview`
- `POST /v1/sheet-vitrina-v1/prices/upload-task`
- `GET /v1/sheet-vitrina-v1/prices/upload-task/{upload_id}`
- `GET /v1/sheet-vitrina-v1/prices/upload-task/{upload_id}/goods`
- `GET /v1/sheet-vitrina-v1/prices/quarantine`
- `POST /v1/sheet-vitrina-v1/prices/spp-test/start`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/status`
- `POST /v1/sheet-vitrina-v1/prices/spp-test/restore`
- `GET /v1/sheet-vitrina-v1/prices/spp-test/history?limit=...&cursor=...`
- `GET /v1/sheet-vitrina-v1/plan`
- `GET /v1/sheet-vitrina-v1/status`
- `GET /v1/sheet-vitrina-v1/product-capital/status`
- `POST /v1/sheet-vitrina-v1/product-capital/recalculate`
- `GET /v1/sheet-vitrina-v1/job`
- `GET /sheet-vitrina-v1/operator`
- `GET /sheet-vitrina-v1/vitrina`
- `GET /sheet-vitrina-v1/instructions`
- `GET /login`
- `POST /login`
- `GET /logout`
- `POST /logout`
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/warehouses`
- `GET /v1/sheet-vitrina-v1/warehouses/recovery`
- `GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}`
- `GET /v1/sheet-vitrina-v1/seller-portal-session/check`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status`
- `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip`
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh`
- `GET /v1/sheet-vitrina-v1/web-vitrina/business-projection/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/stock-ff.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec-check`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-factory.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/inbound-ff-to-wb.xlsx`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/stock-ff`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-factory`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-ff-to-wb`
- `POST /v1/sheet-vitrina-v1/supply/factory-order/calculate`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status`
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/calculate`
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip`
- `GET /v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-options`
- `GET|POST /v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-settings`
- `GET|POST /v1/sheet-vitrina-v1/settings/auto-updates`
- `GET /v1/sheet-vitrina-v1/auto-updates/status`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options`
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync`
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status`
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/{supply_id}`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/template.xlsx`
- `POST /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}`
- `DELETE /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}`
- `GET /v1/sheet-vitrina-v1/supply/fulfillment-services/uploads/{upload_id}/payment-validation.pdf`
- `GET /sheet-vitrina-v1/supplier`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/registry`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/registry/compare-quote`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/parse`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}`
- `PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}`
- `DELETE /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/rematch`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/price-check`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/invoice`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract`
- `PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/contract`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents`
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}`
- `PATCH /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}`
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/{shipment_id}/financial-documents/{document_id}/file`
- `GET /sheet-vitrina-v1/settings`
- `GET /v1/sheet-vitrina-v1/settings/nomenclature`
- `POST /v1/sheet-vitrina-v1/settings/nomenclature`
- `POST /v1/sheet-vitrina-v1/settings/nomenclature/barcode-sync`
- `PATCH /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}`
- `POST /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}/barcode-sync`
- `DELETE /v1/sheet-vitrina-v1/settings/nomenclature/{item_id}`
- `GET /v1/sheet-vitrina-v1/settings/sku-groups`
- `POST /v1/sheet-vitrina-v1/settings/sku-groups`
- `PATCH /v1/sheet-vitrina-v1/settings/sku-groups/{group_key}`
- `DELETE /v1/sheet-vitrina-v1/settings/sku-groups/{group_key}`
- `GET /v1/sheet-vitrina-v1/settings/users`
- `POST /v1/sheet-vitrina-v1/settings/users`
- `PATCH /v1/sheet-vitrina-v1/settings/users/{user_id}`
- `DELETE /v1/sheet-vitrina-v1/settings/users/{user_id}`

Contract keeps runtime truth inside hosted WebCore and does not move supplier/order/nomenclature logic into Apps Script.

## Repo-Owned Execution Entrypoint

Canonical runner:
- `apps/registry_upload_http_entrypoint_hosted_runtime.py`

The same runner owns the only production path for the active functional cutover after deploy:
- `warehouse-functional-dry-run --output <absolute local plan.json>` captures coherent primary sources and fresh complete official WB stock without derived writes; functional plan builders use the same bounded 30-minute remote timeout as atomic apply because the official supply/stock capture can legitimately exceed the 5-minute readback timeout;
- `warehouse-functional-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>` pins the active EU target/runtime, selects allowlisted T3 only for the initial `warehouse_functional_cutover_v1` schema cutover, retains its coherent `0600` backup through the central recovery registry and atomically applies six-stage balances, frozen historical cost projection and initial calculation parameters;
- `warehouse-functional-failed-backup-cleanup-dry-run --source <exact functional backup path>` and paired `...-apply --fingerprint <exact sha256:...>` are the only hosted recovery path for a backup interrupted by filesystem exhaustion: the allowlist covers only the exact timestamped functional-cutover name below `/opt/wb-core-runtime/backups/warehouse-functional` and the exact fingerprint-derived emergency name below `/opt/wb-core-runtime/backups/warehouse-functional-recovery`; a full file SHA/stat plus invalid SQLite header/integrity proof is required, a `0600` audit manifest is retained, and an unrelated file, coherent backup or live database is never eligible. The shared backup API checks source-size-plus-margin capacity before opening a destination and removes only its own incomplete destination/sidecars if a later backup step fails;
- `warehouse-functional-readback` proves stage/source/capital reconciliation and the exact cutover identity;
- `warehouse-functional-backup` now creates a private T2 warehouse/cost domain checkpoint below `REGISTRY_UPLOAD_RUNTIME_DIR/warehouse-recovery/domain-checkpoints` without changing business or derived rows and without copying/opening Finance raw. All hourly/manual/backup paths hold the same process lock; capacity, artifact identity, lifecycle and rollback expiry are owned by the central recovery registry;
- repeated exact apply must return idempotent/no-op and create no second movement;
- `apps/warehouse_cost_unified_recovery.py` is the reviewed bounded production-data recovery for the coupled supplier-date, approved bank-fee, explicit WB-supply physical-debit and optional unique whole-box scope defined by migration 114. `apps/warehouse_cost_queue_replay.py` is the separate reviewed path when confirmed supplier documents/capital already exist and only exact durable `supplier_costs` queue revisions plus their dependent economics publication remain. Both are query-only by default, exclude Finance raw rows and any full-database copy/integrity/SHA pass, require the exact current fingerprint for apply, hold the shared warehouse lock, journal idempotent target-scoped steps before mutation and require a second no-op readback. Queue replay accepts multiple exact invoices, fails closed on an overlapping non-target queue, validates expense/capital totals and source revisions, completes only selected queue identities, proves target quantities and non-target warehouse/queue digests unchanged and uses target-scoped economics before-images. Hosted `warehouse-cost-queue-replay-dry-run|apply` pins the canonical active target and passes the reviewed plan over stdin. The former 26GN527 and transit/reservation apply entrypoints are disabled;
- Hosted `warehouse-functional-sync` backup-only, manual and reviewed sync-apply actions derive recovery state from canonical `REGISTRY_UPLOAD_RUNTIME_DIR`; legacy backup-directory arguments remain path-validation compatibility inputs and cannot redirect policy artifacts or regain a full-store backup.
- `warehouse-functional-emergency-dry-run --output <absolute plan.json>` and `warehouse-functional-emergency-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>` are the only post-cutover full-rebuild/recovery path. The public UI remains preview-only. Ordinary rebuild uses persisted functional sources and the immutable historical boundary. Only an actual missing whole pre-cutover date admits a pinned persisted ready-snapshot manifest with an exact dated `stock_total` column; complete frozen history excludes mutable snapshots from the source digest. The correction-only loader admits a later persisted bundle when its outer `as_of_date` is post-cutover but its `date_columns` contains the missing exact date; ordinary hourly replay remains cutover-bounded. For each corrected date the newest single coherent snapshot column is authoritative: its SKU universe must equal the union declared by every persisted candidate for that exact date, every `stock_total` cell must be valid, and the projected SKU identity set must match it exactly, without per-SKU stitching from older snapshot versions. A zero-stock SKU absent from the frozen opening remains an explicit zero-capital row with `zero_quantity_without_cost_basis`; canonical consumers expose no invented unit cost, while a later positive quantity without a cost seed still fails closed. Frozen quantities, not a mutable snapshot window, drive the overlap replay; exact snapshot columns enter only the missing dates. Drift and manifest pin the selected date/SKU/quantity evidence and its source identity, not unrelated ready-snapshot rows or metadata. Before its mutation and again under `BEGIN IMMEDIATE`, apply re-derives the complete correction contract and requires byte-semantic equality with the reviewed plan. Recovery is a central T2 domain checkpoint with Finance raw excluded; only the uniquely derived missing identities enter plain `INSERT` plus a versioned `supersedes` audit, and an exact repeat is T0/no-op;
- `warehouse-functional-economics-dry-run --output <absolute plan.json>` and `warehouse-functional-economics-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>` are the only bounded `2026-07-01+` ready-snapshot WB cost/Proxy 3 publication path; apply persists exact T1 before/after images, copies zero full-store bytes, byte-preserves snapshots outside target dates and proves non-target digest invariance and idempotent readback;
- `warehouse-july-recovery-dry-run --batch a|b|transit --output <external plan.json>` builds the three migration-127 submanifests on the canonical active generation. `warehouse-july-recovery-apply --batch a|b --plan-file <same plan> --fingerprint <exact sha256:...> --approval-reference <human gate>` and paired rollback are the only hosted mutations; Batch B also requires the retained Batch A fingerprint. Transit backup evidence is query-only and cannot be applied by this command. Plans stay outside Git at mode `0600`; apply regenerates the exact current fingerprint and fails before mutation on any source/scope/non-target drift;
- `vitrina-incident-rematerialization-dry-run --date-from <date> --date-to <date> --max-dates 14 --output <external plan.json>` and paired `...-apply --date-from ... --date-to ... --max-dates 14 --plan-file <same plan> --fingerprint <exact sha256:...> --approval-reference <exact approval> --actor <actor>` are the only hosted historical publication path for derived Web Vitrina incident families. The wrapper pins the active EU target, canonical runtime and target-owned `SELLER_PORTAL_CANONICAL_SUPPLIER_ID`, rejects plans inside Git, writes local plans mode `0600`, streams reviewed apply input without a server-side ad-hoc file and validates exact scope/contract/fingerprint/approval. Planning reads existing accepted `stocks` only; no upstream refetch or full-history rebuild occurs. Apply atomically replaces only reviewed incident cells/presentation/quality metadata in exact ready snapshots, persists compact recovery images and non-target digest in the runtime audit, preserves raw stocks/capital/WAC/refreshed timestamp, and finishes only after a zero-delta idempotent readback; an independently regenerated zero-delta plan preserves the existing snapshot/metadata digest instead of publishing a timestamp-only rewrite;
- `warehouse-functional-supplier-certification-dry-run --output <absolute plan.json>` and paired `...-apply --plan-file <same plan.json> --fingerprint <exact sha256:...>` are the bounded recovery path when a legacy active functional version lacks its supplier certification projection. They consume only canonical supplier/CNY/financial sources and require either exact frozen fingerprints or exact target-scoped conservation of every immutable per-SKU quantity/capital plus contributing payment, CNY-fee and China→FF document identity. Mutable target revisions are pinned and rechecked under the apply transaction. The runner retains exact T1 before/after rows and non-target digest in the central registry, then appends ordered version-scoped corrections with `supersedes` provenance without rewriting the immutable version. Changed target allocation/revision or unprovable supplier source state fails closed; exact repeat is T0. `warehouse-functional-supplier-certification-rollback --fingerprint <replay sha256> --reason <audit reason>` appends a rollback tombstone and preserves the replay audit;
- `warehouse-functional-sync` is the privileged CLI manual path; the hourly service and UI operator button use the same locked business pipeline. Manual/hourly publication selects T2 and takes a fresh warehouse/cost domain checkpoint instead of the monolithic store. Operator-authored calculation parameters and dependent economics select T1 and persist exact parameter/ready-snapshot before images. Central reservations use measured domain or serialized undo bytes plus operational reserve, with post-write watermark and lifecycle-aware retention. The pipeline then runs official supply refresh, the bounded process-owned global due transit-cost collector, supply-specific downstream component materialization, complete official stock capture, one canonical warehouse/cost publication and targeted Proxy publication. Transit collection is all-eligible rather than UI-page-scoped, joins an active single-flight run, applies durable classified attempt/backoff state, preserves last successful amounts on a later error and enqueues only confirmed changes for canonical recalculation. It is part of hourly/manual sync, not the reviewed `sync-apply` optimistic-plan contour. CLI and operator-button runs share one re-entrant cross-process lock, so the bounded pipeline never overlaps itself while nested canonical publications do not deadlock; versioned calculation-parameter publication uses the same lock. The warehouse sync process alone raises the DB-backed runtime's bounded SQLite busy wait to 120 seconds. Every hourly/manual phase and shared-lock wait is timed; exhausted contention leaves the last-good version active. The hourly oneshot has `TimeoutStartSec=3h`. It does not invoke legacy daily cost/product-capital rebuild or global vitrina refresh;
- New T2 checkpoints resolve from canonical `REGISTRY_UPLOAD_RUNTIME_DIR` to `state/backups/warehouse-recovery/domain-checkpoints`; production mount identity, not the pathname alone, proves routing to the dedicated backup device. The writer lock runs automatic retention before and after backup-only/hourly/manual/reviewed sync. It protects the two newest verified rollback points, retains at most three T2 generations, bounds the optional third by a 2 GiB steady-state cap, releases superseded points after 24 hours and exposes 24-hour/14-day no-GC projections plus 8 GiB degraded / 4 GiB hard-stop watermarks. Hosted `warehouse-recovery-retention-dry-run|apply` pins the exact deployed SHA and stable plan fingerprint; failed, quarantined, corrupt, incomplete, current and foreign artifacts are never ordinary candidates;
- `warehouse-functional-maintenance status|hold|restore` is the only bounded Finance-maintenance boundary for the hourly warehouse timer. It records the exact timer/service/last-next-trigger/shared-lock/Finance-process baseline in mode `0600`, stops only the timer and waits for an already-running service without killing it, then restores exact enabled/active state only after Finance has released the shared writer lock. Timer-unit drift remains fail-closed. If a nested warehouse restore succeeded but a later outer business-data restore failed and disabled every timer again, the explicit outer-hold recovery may reapply the original warehouse baseline only while the same unrestored outer boundary remains either `acquiring` and unconfirmed or exactly `held/restoring` with a confirmed quiet hold, the current timer is exactly disabled/inactive, the service is quiescent, locks/processes are absent and both unit digests match the prior successful restore; every other restored-state drift remains blocked. A service-unit refresh made by a deploy while the hold is active is accepted only when the service is quiescent, the shared lock is free, Finance is absent, the timer digest is unchanged, `NeedDaemonReload=no`, the loaded service has no drop-ins and its fragment is byte-for-byte equal to the repo-deployed systemd artifact; the evidence is revalidated after timer mutations, and both digests and paths are persisted in the maintenance audit. The canonical Finance apply itself holds `.warehouse-functional-sync.lock` across plan, coherent backup, atomic apply and transactional readback; arbitrary `systemctl`/SSH holds and a second lock are not supported;
- `business-data-maintenance status|hold|restore|set-process|barrier-*` is the canonical cross-writer quiet-window boundary and the backend for Settings `Автообновления`. `status`, `barrier-status` and Settings GET are strictly read-only. Owner-policy v2 contains individual desired state only for Settings-owned processes: Web-vitrina refresh, its separate closure retry, warehouse/cost and Finance. Autoanswers and auto-complaints are feature-owned monitoring-only processes; SPP is no longer scheduled or represented as an auto-update process. `hold` disables Settings-owned tickers, leaves canonical schedule JSON unchanged, suspends actual auto-complaints and invokes the dedicated Autoanswers lifecycle. It also inventories the manual SPP execution lock/current job as a cross-writer safety boundary without owning an SPP timer. The runner inventories every installed `wb-core-*.timer`, paired service, matching cron, known writer and shared lock; unknown or active unbounded writers fail closed, while already-running oneshots are waited rather than killed. Mode-`0600` maintenance/audit retains exact pre-hold and final readback evidence plus a stable control signature. Repeating `prepare` from an already `prepared` or `held` quiet boundary is an audited no-op only when revision/fingerprint and every current control intent still match exactly. `restore --expected-revision <N>` requires exact revision and quiet/no-writer/no-lock/no-drift proof. The durable HTTP barrier is acquired before writer drain; while active, authenticated business mutations return exact audited `423`. The browser disables mutations only after a valid successful `active=true` readback, renders ordinary maintenance as a warning and invalid fail-closed state as danger, preserves the last confirmed state across status timeout/error, and polls single-flight with bounded timeout/backoff plus hidden-tab throttling. The browser guard never rewrites application-owned native `disabled` state; server-side `423` remains authoritative even before the first confirmed client status. Registry HTTP, archived Data MCP compatibility, Release Train and deploy/verification infrastructure are not stopped. The deploy manifest never enables or restarts business timers;
- A long unconfirmed-window restore is submitted through `business-data-maintenance-restore-submit` and observed through the read-only `business-data-maintenance-restore-status`; it is never owned by the SSH connection. Immediately before submit, read-only `business-data-maintenance restore-continuity-status` captures the exact barrier window/fingerprint/phase, owner-policy revision and every continuing pre-hold service unit/PID/start timestamp. Submit recomputes that evidence and requires its exact fingerprint, then pins it with a caller-known 64-hex job id, exact deployed SHA, actor, reason and the explicit pre-hold service-continuity permission. The fixed `wb-core-business-data-maintenance-restore@.service` template accepts only that job id, loads the persisted private request and survives transport disconnect or systemd restart. If the bound service completes successfully after submit, the same persisted generation is accepted as `completed`; a different or ambiguous generation is rejected. One global worker lock plus the maintenance restore lock reject overlapping detached/foreground restores. Request, continuity evidence, status, result and append-only audit are fsynced and digest-bound; retry of the same queued/start-failed request is idempotent, resubmitting a running request never starts another worker, a different request under the same id is rejected, and a crash after exact prior state was restored finalizes from persisted maintenance/policy evidence without replaying the mutation. The worker has an end-to-end deadline and durable heartbeat. Status classifies active, stale-deadline, ambiguous-active, ambiguous and lost-worker evidence without mutation; every non-terminal ambiguity remains fail closed and never auto-starts a replacement restore. Deployed-SHA, barrier, policy, service-generation, control-signature or result drift also remains fail closed. A terminal `maintenance_restore_failed` remains terminal and cannot be resubmitted through the original submit path. If it demonstrably returned to the original paused boundary, an independently reviewed recovery deploy may use the distinct `business-data-maintenance-restore-resume` command: it requires the exact preceding-failure digest, original continuity fingerprint, new current deployed SHA, zero worker/restore locks and no extra writer, archives that attempt result and appends the next immutable binding under the same job id. The current incident contract admits only the contiguous bindings `resume.json`, `resume-2.json` and `resume-3.json`, for attempts 2, 3 and 4 respectively; a fourth recovery binding, a second job and automatic retry are rejected fail closed. Timer/service state and Autoanswers freshness use one observation timestamp captured before the feature-owned SQLite readback, preventing a slow read from comparing a stale systemd snapshot against a later wall clock. Restore acceptance uses the successful feature-owned lifecycle reconcile readback for Autoanswers only when a later outer systemd snapshot independently confirms the same desired components; lifecycle contract, business mode, policy epoch, transition run, timer state, service result and observation order must all agree. A redundant later feature-store `worker_unavailable` view cannot invalidate that bounded `starting` proof. A stale pre-request `worker_error` is non-terminal only while the exact desired worker service is actively executing with a successful service result; without that bounded generation evidence it remains blocking. Any other lifecycle block, identity/component drift, failed service or inactive timer still fails closed. `barrier-abort` is a separate command and is allowed only after terminal exact-restore plus independent timer/writer/policy/non-target readback;
- The detached maintenance restore continuity contract additionally admits a confirmed barrier only when it remains `held/restoring`, the maintenance phase is the exact original `held` state and the full boundary is quiet. That form binds the same window/fingerprint/timestamp with zero continuing services; barrier phase, policy revision, writer/timer state or quietness drift fails before job persistence/start and at worker readback. Query-only restore inventory reports every durable job plus submit/worker/foreground locks and permits a fresh snapshot boundary only with zero non-terminal jobs and all locks free. Autoanswers acceptance follows the feature lifecycle's blocking-stop classification: `reconciliation_in_progress` is non-blocking progress, while a blocking stop reason, identity/component drift, failed service or inactive desired timer still fails closed.
- Finance storage uses phase-local `finance-storage-snapshot-*`, `finance-storage-snapshot-retention-*`, `finance-storage-split-*`, `finance-storage-candidate-abort-*`, `finance-storage-shadow-*`, `finance-storage-live-tail-apply`, `finance-storage-cutover-*` and `finance-storage-rollback-*` commands. Deploy invokes none of them. `finance-storage-recovery-contract` is a query-only deployed readback of the complete snapshot/retention/candidate/abort/shadow/cutover/rollback continuity matrix and exact supported runner versions. Before every Finance storage mutation, including one that will later own a barrier, the hosted wrapper first runs remote `recovery-preflight`; it validates the lease/SHA/approval/fingerprint/generation/path bindings and all durable restore/release capabilities before any barrier or destination mutation. Snapshot/cutover/rollback mutation runners repeat the same contract inside the exact confirmed quiet hold. The remote CLI parses every streamed reviewed plan once and never attempts a second read from exhausted stdin. Snapshot/cutover/rollback reuse that exact in-memory object for both repeated recovery preflight and mutation; candidate apply validates it before destination bytes and then independently replans the immutable source under its own locks. Candidate operational tables are copied in the exact plan-bound foreign-key dependency order, verified checkpoints resume idempotently, self references defer only inside one table transaction, and any dependency cycle/order drift/final `foreign_key_check` violation prevents candidate-manifest publication. A missing capability or an acquiring/restoring/manifest ambiguity remains fail closed; it never guesses a manifest or starts a second restore. Before any migration action except read-only health and recovery-contract readback, the hosted wrapper requires a fresh private `wb_core_finance_migration_deploy_lease_readback_v1` outside Git and validates its Actions-owned global Release Train hold, task/anchor/head/deployed SHA, lease/window/phase/revision, bounded owner expiry and evidence fingerprint. The remote runner repeats validation against the canonical `.wb-core-runtime-sha`; evidence older than five minutes, expired/lost/ambiguous identity, a recovery/rebind revision or any SHA drift fails before snapshot planning or destination bytes. The global `finance:migration-deploy-lease` never auto-opens, blocks unrelated selection/merge/deploy and admits only an exact owner-bound recovery PR; recovery deploy must be rebound and invalidates all earlier baseline/snapshot/plan/fingerprint evidence. Lease release/abort requires exact deployed-SHA plus manual-barrier/writers/timers/policy/non-target reconciliation through the trusted Release Train. Snapshot plan uses bounded metadata/capacity/writer evidence, classifies every writable SQLite opener against exact systemd ownership and blocks any already-active non-HTTP business writer service before a window begins. Unknown ownership or an undrainable active generation blocks the window. When retained older-SHA coherent snapshots alone cause the root capacity gate to fail, `finance-storage-snapshot-retention-plan|apply|readback` is the only capacity recovery: it requires canonical monolith/no generation/inactive barrier, a distinct mounted `backups` device and fresh lease, hashes an exact allowlist, copies/fsyncs and independently verifies every archive byte, persists a durable crash-resume transaction/audit, then removes only the root-filesystem snapshot copy with its manifest last. A disconnect before verification leaves the source intact; an exact repeat after verification resumes only source release, and terminal repeat is a verified no-op. Unknown files/openers, a new snapshot/generation, device/capacity/SHA/lease/barrier drift or incomplete archive stay fail closed. Archived snapshots remain byte-exact evidence; the live monolith and business/split stores are untouched. If an interrupted candidate has bytes but no `candidate_generation_manifest.json`, `finance-storage-candidate-abort-plan|apply|readback` is the only permitted release before snapshot retention. It requires the implicit monolith, absent global/candidate manifests, inactive barrier/shadow, exactly one bound generation, no worker/opener/lock owner and a fresh lease. It recomputes the saved old plan fingerprint and validates exact generation/source/deployed-SHA/schema/checkpoint subsets. Apply locks out candidate creation, persists an external fsynced per-file transaction/result/audit and can unlink only both candidate DBs, their sidecars and the saved plan last. Disconnect resumes only the same transaction; unknown/drifted/reappearing or unjournaled-missing files fail closed. Readback proves generation/global manifest absent and monolith inode/schema plus all snapshot directory identities unchanged. The recovery deploy and abort make the old snapshot/plan/fingerprint unusable; after archival the flow must rebind the lease and start a wholly fresh snapshot/integrity/dry-run. The closure-retry oneshot itself has `TimeoutStartSec=1800`; if one pre-existing generation is nevertheless stale, `finance-storage-stale-writer-plan|stop` is the only recovery path. It requires a fresh deploy lease, exact reviewed fingerprint/approval, one-hour minimum PID/start/cgroup continuity, the exact installed bounded unit, preserved timer/owner intent, released barrier/restored maintenance, no runtime-store FD or internet socket and no non-Playwright child. Apply records a private durable audit, issues one exact service stop, preserves timer/policy and never retries automatically; any generation, FD, socket, child, SHA, lease or control drift fails closed. Snapshot apply automatically acquires the manual barrier, drains exact writers and recaptures the held source before backup. Ordinary data/mtime/page/freelist drift from the query-only plan is admitted only with unchanged source path/device/inode/page-size/schema/journal identity and fresh capacity for the actual held allocation plus reviewed reserve; both planned and actual identities are persisted. Path/schema/device/inode/journal drift or insufficient headroom fails closed. The wrapper derives one restore job id from deployed SHA/window/fingerprint, proves query-only global restore inventory, submits the fixed system-owned job and observes durable status. Re-dispatch from the same `restoring` barrier never replays acquisition, writer hold or snapshot copy; it observes only that job, restores the nested warehouse boundary and releases after terminal exact readback. An exact partial copy is rebuilt and an exact final database written before manifest publication is structurally rebound, while dual files, sidecars or stable-identity drift remain ambiguous. A missing/invalid manifest after proven restore is surfaced only after safe control release and cannot replay the reviewed plan. A drain failure before confirmation automatically attempts the exact continuity restore plus acquiring-barrier abort, while any incomplete abort remains fail closed. Full `integrity_check`/foreign-key verification runs only on that copy outside the live DB. The verified snapshot then anchors the dry-run, chunk manifest and candidate fingerprint. Candidate/backfill/live-tail/shadow soak run without a broad hold and preserve normal work. Cutover applies the final tail, freshly recopies arbitrary operational changes, drains outbox evidence, atomically switches the manifest and restarts only `wb-core-registry-http.service` under the exact final hold. An exact post-manifest client/process loss resumes as idempotent split readback without replay. Post-cutover weekly ingestion acknowledges ordered outbox events only after the full exact operational projection reads back; raw-first crash windows remain pending until idempotent replay completes them. Rollback is prepared and fully integrity-checked during normal operation; its short hold replays only post-prepare raw scopes, freshly recopies operational state, switches one monolith manifest and restarts the same service. Exact post-manifest rollback identity recreates or reads terminal evidence without replaying candidate mutation. Exact timer/writer state is restored before barrier release. Pre-switch failure restores controls; ambiguous post-switch failure retains the barrier and holds fail closed. Every mutation requires the reviewed external plan/fingerprint/approval, active target and fresh capacity/readback. Original monolith and split generations remain retained; no lifecycle action performs in-place delete/vacuum or old-generation retirement;
- The registry HTTP process resolves the current manifest at startup and keeps
  query-only handles on every distinct canonical Finance store until shutdown.
  The handles do not own transactions or writes; they provide an exact
  process/store binding for the cutover restart readback. After a recovery
  deploy that follows an already-persisted split manifest, cutover recovery
  may reuse the old reviewed fingerprint only as an idempotent result
  readback: the selected split identity, retained monolith, persisted
  `cutover_evidence.json`, active exact barrier and fresh recovery-deploy lease
  must all agree. It never replays the final tail, operational recopy or
  manifest mutation.
- After a selected split cutover, the same
  `finance-storage-snapshot-retention-plan|apply|readback` surface switches to
  `post_cutover_atomic_replace_v1`. It binds the exact deployed SHA, selected
  manifest/store identities, dedicated backup mount, complete legacy/current
  inventory and count/byte/age/capacity policy. Apply may release oldest
  verified legacy archives before copy only while a separate verified fallback
  remains. It copies operational then raw directly to a private set on the
  mounted backup device, requires integrity/FK/logical-digest and isolated
  cursor restore readback, fsyncs and atomically selects one current set before
  releasing any remaining root/backup legacy artifact or old current. Unknown,
  corrupt, incomplete, opened or drifted artifacts remain protected. The
  original monolith and every `generations/` path are structurally outside the
  deletion allowlist. The daily systemd due-check is byte-inert until an
  approved first apply writes the private policy; thereafter it resumes only
  the same durable transaction and holds one current set with a temporary
  second only during replacement. Operator health exposes identity, age,
  RPO/RTO, bytes, next replacement capacity, success/failure and zero 30/90-day
  retained-growth projection. A terminal failed post-cutover retention worker
  is resumed only by the explicit
  `finance-storage-snapshot-retention-resume` surface with the same request
  identity, SHA, plan, fingerprint and approval; it archives the prior failure,
  rejects active/ambiguous workers and caps the chain at eight attempts;
- Snapshot/snapshot-retention/cutover/rollback byte mutations run through one persisted
  `finance_storage_transport_job.py` request identity. Submit transport loss is
  followed only by status observation of that exact worker; request/SHA drift
  or a lost worker remains ambiguous and never triggers a replacement.
  Rollback plan, prepare and final raw readback traverse ordered seller/week
  scopes and require predicate pushdown through `finance_raw_current_rows`
  with `finance_raw_rows_by_week`; an unscoped all-history current-row
  materialization is not an allowed rollback path.
  After a manifest switch the registry HTTP restart is accepted only when the
  new systemd `MainPID` is observed opening both manifest-selected stores.
  For rollback-monolith recovery,
  `finance-storage-post-manifest-recovery-readback` uses SQLite query-only
  comparison against the retained split generation: core raw and every
  non-cache operational table must match. At most eight total Vitrina incident
  cache key/value differences are admissible: a retained-only row must match a
  deterministic rebuild from canonical accepted stocks and policy, while a
  canonical-only or common-key semantic difference is accepted only when the
  active canonical row matches that rebuild. Ordinary Vitrina refresh
  regenerates a missing canonical row. At most one retained-only row whose
  accepted canonical stocks snapshot is absent may be treated as a
  noncanonical cache-miss orphan and replaced only by a fresh ordinary
  refresh; a second such row fails closed. The retained generation remains
  immutable and direct cross-generation row copy is forbidden.
- The candidate-abort boundary also covers an exact completed-but-unselected
  generation after a recovery deploy, but only after repo-owned shadow
  deactivation. This extension supersedes the pre-manifest-only wording in the
  preceding lifecycle summary: the global manifest must still be absent, the
  monolith remains canonical, every raw checkpoint and every table in the
  saved `operational_copy.table_order` inventory must be terminal (excluded
  raw/schema entries in the broader owner matrix are not operational
  checkpoints), all raw batches must be terminal, and the inactive shadow
  state, candidate manifest, optional
  verification evidence and saved candidate-plan fingerprint must bind the same
  generation. Only those exact manifest/evidence files join the existing
  deletion allowlist; active/mismatched shadow, a selected manifest, an opener,
  an unknown file or any identity drift fails closed.
- The hosted Finance wrapper attaches the fresh canonical `deploy_lease` readback after a deterministic plan is built. Before any barrier or destination mutation, recovery preflight recomputes the runner-owned deterministic fingerprint for candidate, snapshot, snapshot-retention, stale-writer, cutover and rollback plans. Their fingerprint validation excludes only that top-level transport field in addition to the action's documented volatile plan fields; the lease is separately revalidated against exact deployed SHA, revision, window, phase and Actions-owned evidence fingerprint before mutation. Arbitrary added or changed plan fields invalidate the reviewed fingerprint fail closed.
- Exact internal `finance-storage-snapshot-status` is the only additional read-only exception to the preceding lease-readback freshness rule. It runs after the already-authorized hold/restore and binds the reviewed plan, capture intent and persisted snapshot manifest directly to canonical `.wb-core-runtime-sha`; no mutation uses this exception. This prevents a long restore from being misclassified solely because its original five-minute lease readback aged while preserving exact SHA/plan/manifest fail-closed validation.
- Canonical deploy verification keeps all GET/read probes mandatory while a maintenance barrier is active. Its single harmless POST route-publication probe accepts `423` only when the body is the exact active `wb_core_business_data_write_barrier_v1` contract, names a non-empty window, reports an allowed active phase, is retryable and proves `attempt_audited=true`; arbitrary or incomplete `423` responses still fail the release.
- `warehouse-functional-enable-hourly` enables the timer only after successful cutover/readback. Each hourly/manual functional sync finishes supply-layer, functional-version and functional-economics publication, then before releasing the shared warehouse write lock atomically recalculates every Finance week made stale by any of those cost writers; the sync result carries exact Finance post-verify and non-target-preservation evidence;
- `warehouse-functional-rollback --fingerprint <exact stored sha256:...>` restores only the T2 warehouse/cost domain checkpoint and removes no Finance raw;
- `warehouse-recovery-canary-dry-run|apply` pins `.wb-core-runtime-sha`, proves T0 zero bytes/rows, an exact rolled-back T1 marker replay, a retained T2 domain checkpoint, unchanged warehouse-domain digest and a clean orphan scan without business mutation. The first durable policy operation is the activation boundary: older known recovery-family files in `backups/` remain visibly classified as a read-only pre-policy baseline, while every new or subsequently touched unregistered file blocks acceptance;
- `warehouse-ui-flow --evidence-dir <absolute path outside repo> --acceptance-profile warehouse_recovery_policy_20260726 --deployed-sha <exact merge SHA>` first pins the server runtime marker, then uses a fresh isolated Playwright context and verifies the protected recovery API/card, terminal canary lifecycle for that same deployed SHA, capacity/writer/timer/rollback visibility and zero policy-era orphan/quarantine leak. Historical canaries and the visible pre-policy baseline cannot satisfy or invalidate the exact release acceptance. Evidence stays outside Git.
- `warehouse-ui-flow --evidence-dir <absolute path outside repo> --acceptance-profile vitrina_incident_provisional_20260727 --deployed-sha <exact merge SHA>` pins the same deployed marker and performs the read-only production acceptance for 2026-07-25: active revision 2/effective date/five warehouses, 33-SKU base family, every available regional SKU/TOTAL reconciliation, accessible provisional phrase, separate positive-incident marker, no yellow fill, navigation/5xx/pageerror/console/fatal guards and screenshot. It never clicks Policy Apply or another business mutation.
- `warehouse-ui-flow --evidence-dir <absolute path outside repo> --acceptance-profile ff_inventory_capital_20260803 --deployed-sha <exact merge SHA>` is the current read-only closure profile for the FF inventory/product-capital contour. It proves four persisted `2026-07-25` incident warehouses, one reversible local fifth selection defaulted to current business date, mixed-date preservation, one Apply control without clicking it, the current unified metric catalog (including hidden/collapsed entries), canonical zero-gap readback plus rendered Proxy 3 evidence when `orderSum` is intentionally hidden, all warehouse/Vitrina consumers and the normal navigation/`5xx`/`pageerror`/console/fatal guards.

Ad-hoc SQL, arbitrary remote commands and server-only scripts are not valid initialization paths. `warehouse_opening_v1` remains immutable audit under migration 102; active sources/non-target invariants are fixed in module 48 and `migration/103_warehouse_functional_cutover.md`. Exact-date warehouse-chain recovery and archived-metric cleanup additionally follow `migration/104_warehouse_chain_audit_recovery.md` through the same repo-owned dry-run/apply/readback/UI contours.

Canonical target template:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__example.json`

Canonical active target for the current EU hosted runtime:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json`
- `target_status = active`
- `target_role = primary_live`
- `target_lifecycle = current_live`
- `mutation_policy = routine_writes_allowed`
- `host_ip = 89.191.226.88`
- `public_domain = api.selleros.pro`
- `ssh_destination = wb-core-eu-root`
- `public_base_url = https://api.selleros.pro`
- current live DNS name = `api.selleros.pro`
- `runtime_env.REGISTRY_UPLOAD_RUNTIME_DIR = /opt/wb-core-runtime/state`
- `service_name = wb-core-registry-http.service`
- nginx `server_names = 89.191.226.88 api.selleros.pro`
- nginx managed TLS = `/etc/letsencrypt/live/api.selleros.pro/fullchain.pem` + `/etc/letsencrypt/live/api.selleros.pro/privkey.pem`
- This production domain/TLS publication is a hard current-live invariant. For targets marked `primary_live` or `current_live`, `deploy`, `deploy-and-verify` and `apply-nginx-routes` must fail locally before SSH/rsync/nginx/systemd mutation if the target regresses to IP-only HTTP, drops `api.selleros.pro` from `server_names`, or drops managed `443 ssl` TLS.

Archived legacy target:
- `artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__selleros_api.json`
- `target_status = archived`
- `target_role = rollback_only`
- `target_lifecycle = deprecated_live_target`
- `mutation_policy = do_not_deploy_without_emergency_rollback_override`
- `legacy_host_ip = 178.72.152.177`
- `public_domain = api.selleros.pro`
- `ssh_destination = selleros-root`
- `public_base_url = https://api.selleros.pro`
- This target is rollback/read-only migration evidence only. Routine deploy, apply-nginx, restart, update, audit, GC or hosted runtime write tasks must use the EU target. The runner fail-fast rejects archived/legacy target hosts for mutating actions unless an explicit emergency rollback override is present.
- The domain string in this archived JSON is historical metadata, not old-VPS identity. Old VPS identity is `selleros-root` / `178.72.152.177`; `api.selleros.pro` may be a current live DNS name for the EU target.
- Recommended provider-side label for the old VPS: `ROLLBACK-ONLY_DO-NOT-DEPLOY_wb-core-old-selleros`.

Canonical repo-owned systemd artifacts for this contour:
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-registry-http.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-refresh.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-sheet-vitrina-closure-retry.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-wb-finance-weekly.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-wb-finance-weekly.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-finance-backup-rotation.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-finance-backup-rotation.timer`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-business-data-maintenance-restore@.service`
- `artifacts/registry_upload_http_entrypoint/systemd/wb-core-data-mcp.service` is retained as an archived read-only compatibility boundary, not a normal Codex/ChatGPT data path. The current deploy implementation still manages the loopback-only `127.0.0.1:8766` unit for backward compatibility; its historical OAuth, concurrency, deadline, result-size, audit and redaction constraints remain fail-closed when that compatibility surface is explicitly maintained.

`wb-core-sheet-vitrina-refresh.timer` is a due-check ticker, not the business-time source of truth: it runs every 10 minutes and starts `apps/sheet_vitrina_v1_auto_refresh_tick.py`; the runner reads the existing runtime JSON schedules (editable only in `Настройки → Автообновления` through the unchanged web-vitrina auto-schedules API), builds an in-memory WebCore session cookie from hosted env, and then calls the protected refresh route with `auto_refresh=true`. The backend auto-refresh cycle first refreshes the web-vitrina ready snapshot and then runs a nonfatal WB supplies official incremental sync; the result payload/logs expose `wb_supplies_auto_sync_status` and `wb_supplies_auto_sync` diagnostics, while WB supplies failure or Seller Portal transit-cost preflight failure is warning metadata rather than a critical web-vitrina snapshot failure. The timer itself is non-persistent; catch-up is owned by the runner's schedule state so a deploy/restart does not immediately fire a stale systemd event while the app process is restarting.

SPP has no systemd service/timer and no business schedule. Manual jobs and emergency restore still share `sheet_vitrina_v1_prices/spp_tests/execution.lock`; deploy/maintenance must observe that lock and any active/unrestored pointer before a write boundary.

Canonical repo-owned public route allowlist:
- `artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json`

Every browser-fetched protected API used by the public operator surface must be
listed explicitly. In particular, the seller-level WB incident policy
depends on the exact read-only options route
`GET /v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-options` and the
append-only revision settings route
`GET|POST /v1/sheet-vitrina-v1/supply/wb-warehouses/exclusion-settings`; a
loopback-only implementation of either route is not a deploy-complete contract.
Production probes validate only the read-only `GET` side of the settings route.
The authenticated POST is the explicit policy gate and, after the append-only
revision is saved, invokes the bounded 14-day derived Vitrina incident
rematerialization over persisted accepted stocks. Its response exposes exact
status/date/snapshot/cell/fingerprint/readback evidence; failure is visible and
never reported as a fully rematerialized success. Supply/SKU strict projection,
raw stocks and warehouse/capital truth are outside that derived write set.
The same rule applies to the Settings control plane:
`GET|POST /v1/sheet-vitrina-v1/settings/auto-updates` must be explicitly
published, while production probes stay read-only and validate only `GET`.
Functional sections read actual state through the separately published,
read-only `GET /v1/sheet-vitrina-v1/auto-updates/status`; operators with
Feedbacks or Prices access do not need Settings mutation access for these
indicators.

The managed nginx block renders `client_max_body_size 32m` for the public WebCore routes so real supplier invoice XLSX uploads reach the app instead of failing at nginx with an HTML `413 Request Entity Too Large` page.

Runner работает от current checked-out worktree и поэтому применим к незамёрженному branch/PR without merge-before-verify, если доступны safe deploy rights.

Supported commands:
- `print-plan`
- `deploy`
- `loopback-probe`
- `public-probe`
- `deploy-and-verify`

## GitHub Release Train Binding

Repo-owned [GitHub Release Train](11_github_release_train.md) является optional explicit queue binding поверх этого canonical runner, а не вторым deploy implementation. Для STANDARD PR с `task:standard + scope:live-runtime + release:ready` workflow обязан до merge доказать required `baseline` на final head SHA и SSH connectivity к active EU target, затем squash-merge exact head, checkout exact merge SHA и вызвать только:

`python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy-and-verify`

LOOP PR использует тот же единственный deploy command, но после final sync/baseline сначала обязан остановиться на `release:awaiting-agent`. Exact-head acknowledgement одноразово потребляется перед merge. Потерянный владелец может получить только fail-closed overlay `release:needs-resume`; auto-ack, skip и перехват gate запрещены, а время ожидания не является deploy blocker. После успешного deploy/verify LOOP получает `release:awaiting-ui`, а не terminal success; несвязанные releases ждут UI acceptance либо exact-linked recovery. Ни agent handshake, ни UI gate не меняют canonical deploy implementation или target.

Production failure после merge ставит global `release:halted` и блокирует выбор следующего release. SSH exit `255` не считается доказанным deploy failure: это `transport-indeterminate`, после которого bounded reconciler читает exact metadata/runtime SHA, canonical EnvironmentFile key-presence без вывода values, systemd state/MainPID и mandatory status/operator probes. Healthy exact SHA продолжает transition; wrong/mixed SHA, invalid auth env, inactive systemd и failed probes сохраняют halted. Разрешены только safe retries `daemon-reload/restart/probes/readback`; rsync, metadata и dependencies не повторяются. Repo-owned `resume-halted` снимает label только по exact PR/head/merge/canonical-target evidence. `scope:repo-only` не вызывает deploy. `scope:production-mutation` автоматически не выполняется. GitHub Environment secrets остаются вне Git/logs.

После успешной команды restart deploy runner boundedly повторяет только read-only
`status_command` до трёх минут. Это покрывает асинхронный systemd restart, когда
короткий внешний SQLite writer заставил первый процесс завершиться, а
`Restart=always` запускает тот же exact-SHA runtime повторно. SSH exit `255`
немедленно передаётся exact-SHA reconciler без локального retry; исчерпание
bound, неверный SHA, failed probes или любой другой deploy-stage по-прежнему
fail closed до `deployment_complete=true`.

Read-only commands may inspect rollback-only target metadata (`print-plan`, `deploy --dry-run`, `apply-nginx-routes --dry-run`, bounded probes when explicitly needed), but routine writes must not target selleros.

## Canonical Target Definition

Checked-in target template фиксирует field names, которые больше не нужно угадывать руками:
- `target_id`
- `target_status`
- `target_role`
- `target_lifecycle`
- `mutation_policy`
- `host_ip`
- `legacy_host_ip`
- `public_domain`
- `provider_side_label_recommendation`
- `public_base_url`
- `loopback_base_url`
- `ssh_destination`
- `target_dir`
- `service_name`
- `restart_command`
- `status_command`
- `environment_file`
- `systemd_unit_directory`
- `systemd_units_source_dir`
- `managed_systemd_units`
- `retired_systemd_units`; explicit finite list of obsolete repo-owned units that deploy must disable, stop and remove before `daemon-reload`
- `nginx_public_routes`
  - optional `server_names` array may pin concrete nginx hostnames/IP names for the server block; when omitted, the runner derives the single name from `public_base_url`
  - optional `tls` object may render a managed TLS block into that same server block:
    - `listen` = explicit nginx listen directives
    - `certificate_path` = public certificate chain path
    - `certificate_key_path` = private key path reference only; deploy output must not print key content
- `runtime_env`

Known active EU target values теперь зафиксированы repo-owned:
- `target_status = active`
- `target_role = primary_live`
- `target_lifecycle = current_live`
- `mutation_policy = routine_writes_allowed`
- `host_ip = 89.191.226.88`
- `public_domain = api.selleros.pro`
- `public_base_url = https://api.selleros.pro`
- current live DNS name = `api.selleros.pro`
- `loopback_base_url = http://127.0.0.1:8765`
- `ssh_destination = wb-core-eu-root`
- `target_dir = /opt/wb-core-runtime/app`
- `service_name = wb-core-registry-http.service`
- `restart_command = systemctl restart wb-core-registry-http.service`
- `status_command = systemctl status --no-pager --full wb-core-registry-http.service`
- `environment_file = /opt/wb-ai/.env`
- `runtime_env.REGISTRY_UPLOAD_RUNTIME_DIR = /opt/wb-core-runtime/state`
- `systemd_unit_directory = /etc/systemd/system`
- `systemd_units_source_dir = artifacts/registry_upload_http_entrypoint/systemd`
- `managed_systemd_units = registry-http + wb-ai-api + refresh/closure/auto-complaints/Finance/warehouse/Autoanswers service+timer units + the fixed detached business-data restore template + archived-compatibility wb-core-data-mcp.service`; deploy installs every listed unit and runs `daemon-reload`, but only infrastructure services are deploy-enabled/restarted. Business timers and the detached restore template have `enable=false,restart=false`.
- `retired_systemd_units = wb-core-spp-tester-schedule-tick.timer + wb-core-spp-tester-schedule-tick.service`; immediately after auth preflight and before runtime sync/dependency work, deploy idempotently disables/stops both obsolete schedule units, removes their unit files and performs `daemon-reload`. This is the deployment proof that the removed SPP Autocheck cannot start inside a long rollout window or keep running from a previous release.
- `nginx_public_routes.server_config_path = /etc/nginx/sites-enabled/wb-ai`
- `nginx_public_routes.manifest_path = artifacts/registry_upload_http_entrypoint/nginx/public_route_allowlist.json`
- `nginx_public_routes.test_command = nginx -t`
- `nginx_public_routes.reload_command = systemctl reload nginx`
- `nginx_public_routes.server_names = ["89.191.226.88", "api.selleros.pro"]`
- `nginx_public_routes.tls.listen = ["443 ssl"]`
- `nginx_public_routes.tls.certificate_path = /etc/letsencrypt/live/api.selleros.pro/fullchain.pem`
- `nginx_public_routes.tls.certificate_key_path = /etc/letsencrypt/live/api.selleros.pro/privkey.pem`
- route paths inside `runtime_env` follow current entrypoint defaults
- losing `api.selleros.pro` or `listen 443 ssl` is production outage drift, not an acceptable deploy variant; repo-owned validation treats it as a blocker before live mutation.

Archived selleros target note:
- `selleros-root` and host `178.72.152.177` are not active runtime targets after the EU VPS cutover.
- `api.selleros.pro` is not by itself old-VPS identity; target safety is determined from repo target metadata plus `ssh_destination`, target dir, runtime dir, service name and the old IP guard.
- Selleros is `rollback_only` / `deprecated_live_target` / `do_not_deploy_without_emergency_rollback_override`; it is not a routine deploy, apply-nginx, restart, update, GC or hosted runtime mutation target.
- If `WB_CORE_HOSTED_RUNTIME_TARGET_FILE` points to archived selleros JSON or any target with `ssh_destination=selleros-root`, mutating commands must fail fast before SSH/rsync/nginx/systemd writes instead of silently touching the old VPS.
- Production actions validate the loaded file against the canonical exact `target_id`, `target_status=active`, `target_role=primary_live`, `target_lifecycle=current_live`, SSH alias and `/opt/wb-ai/.env` before any remote mutation. A local target-file override may support bounded diagnostics, but cannot redirect `deploy`, `deploy-and-verify`, reconciliation or production verification to a legacy/archive/placeholder target.
- Deploy metadata and `.wb-core-runtime-sha` are written as paired exact-SHA readback artifacts. A disconnect between their writes is a detectable mixed deployment and remains fail-closed.
- Emergency rollback writes require the exact explicit override `WB_CORE_ALLOW_ROLLBACK_TARGET_WRITE=I_UNDERSTAND_SELLEROS_IS_ROLLBACK_ONLY`; the runner prints a warning and still does not print secrets.
- `print-plan` and dry-run command planning may remain available for rollback evidence because they do not mutate the old VPS.
- DNS/TLS publication for `api.selleros.pro` is part of the current EU target contract; future DNS/TLS changes still require an explicit target-contract update before deploy. The current invariant is exact: `public_base_url=https://api.selleros.pro`, `nginx_public_routes.server_names=["89.191.226.88","api.selleros.pro"]`, and managed TLS with `listen=["443 ssl"]` plus the LetsEncrypt paths for `api.selleros.pro`.

Secrets and mutable credentials по-прежнему не хранятся в Git. Repo stores only non-secret target wiring and unit artifacts.

## Canonical Production Read-Only Evidence Path

Production server is the normal runtime/data source for diagnostics, analysis and user-artifact source acquisition. A task prompt never overrides target selection: Codex resolves the current active target JSON, runtime contract, exact store/document ownership and SSH destination from current `origin/main` and authoritative docs immediately before access.

The bounded sequence is:

1. validate `target_status=active`, `target_role=primary_live`, `target_lifecycle=current_live`, canonical runtime paths and the current `ssh_destination`;
2. perform an actual standard SSH connectivity/read preflight against that resolved target;
3. resolve each required database or document from current code/schema/document contracts rather than a hard-coded path copied from an old prompt;
4. open SQLite with URI `mode=ro` and set `PRAGMA query_only=ON` before any query; use an equivalent query-only session/account for another store;
5. read server-owned documents only through bounded paths resolved from authoritative runtime metadata/contracts; a remote-to-local read for the requested derived artifact is allowed, but arbitrary filesystem browsing and full dumps are not;
6. keep every command non-mutating: no deploy, service/schedule change, sync/backfill, temp file in production runtime, database write, permission change or secret output.

An access blocker is valid only after this preflight records the exact SSH/store/document error or proves that the required data is absent. A missing or misconfigured archived WebCore Data MCP is never relevant to this decision. The concrete host, runtime directory, store and document path must not be inferred from a stale prompt.

## Canonical Runtime Env Contract

Hosted service должна предоставлять current repo entrypoint env names:
- `REGISTRY_UPLOAD_HTTP_HOST`
- `REGISTRY_UPLOAD_HTTP_PORT`
- `REGISTRY_UPLOAD_RUNTIME_DIR`
- `REGISTRY_UPLOAD_HTTP_PATH`
- `COST_PRICE_UPLOAD_HTTP_PATH`
- `SHEET_VITRINA_HTTP_PATH`
- `SHEET_VITRINA_REFRESH_HTTP_PATH`
- `SHEET_VITRINA_STATUS_HTTP_PATH`
- `SHEET_VITRINA_OPERATOR_UI_PATH`
- `WB_CORE_WEB_AUTH_USERNAME`
- `WB_CORE_WEB_AUTH_PASSWORD_HASH`
- `WB_CORE_WEB_AUTH_SESSION_SECRET`
- `WB_CORE_SUPPLIER_AUTH_USERNAME` (optional supplier-only account)
- `WB_CORE_SUPPLIER_AUTH_PASSWORD_HASH` (optional supplier-only account)
- `WB_CORE_SUPPLIER_AUTH_DISPLAY_NAME` (optional)
- `WB_PRICES_WRITE_ENABLED` (optional safety gate; default false)
- `WB_SPP_TEST_ENABLED` (optional SPP tester safety gate; default false)
- `WB_BUYER_RECOVERY_LOCK_WAIT_SEC` (optional bounded supervisor wait for an in-flight buyer-session preflight; default `90`)
- `WB_BUYER_SESSION_VALIDATION_NM_ID` (optional positive read-only product used for persistent-profile restart proof; default `497416931`)

Production WebCore auth is app-level session auth, not nginx basic auth. The password hash uses the entrypoint PBKDF2-HMAC format `pbkdf2_sha256$iterations$salt_b64$digest_b64`; plaintext credentials must stay outside Git/docs/logs and are handed to the owner separately. `WB_CORE_WEB_AUTH_REQUIRED=1` may be set to fail closed when auth env is incomplete. The env web principal is the bootstrap/fallback `admin`; runtime users are stored server-side in SQLite `sheet_vitrina_v1_users` and may have legacy technical roles `admin`, `operator`, `supply_operator` or `supplier`, but shell/API authorization is section-based through `allowed_sections` plus internal `manage_users`. The internal `GET /sheet-vitrina-v1/instructions` route is protected by the independent `instructions` section: admin has it through administrative semantics, while non-admin users require an explicit setting through the existing user-management API; nginx publication never makes it public. Supplier env credentials are optional and remain backward-compatible supplier-only; when absent, supplier login is unavailable, but users with the `supply` section can access `Поставки -> От поставщика` through the shell.

The canonical deploy runner treats `/opt/wb-ai/.env` as owner-managed state. Before any rsync, systemd install or restart it checks that `WB_CORE_WEB_AUTH_USERNAME`, `WB_CORE_WEB_AUTH_PASSWORD_HASH` and `WB_CORE_WEB_AUTH_SESSION_SECRET` are present and non-empty, without reading them into logs. The same check runs after managed-unit operations. A missing key fails closed and names only the missing variable; deploy never creates, templates or deletes unknown environment keys. Recovery or rotation is performed by the production owner in the external secret store/owner handoff, followed by the normal service restart and repo-owned deploy/probe command.

The authenticated `supplier` principal is a distinct data-security boundary inside the supplier-shipment route family. Its list/detail/parse/create/update HTTP responses are server-side allowlisted before serialization and its HTML is a dedicated supplier-safe template; browser flags, query parameters and iframe state cannot upgrade this projection. Stored shipment invoice/contract, order-document archives/logistics packages and financial-document list/detail/file routes return consistent `403` to supplier even for known valid URLs. Internal `admin`, `operator` and `supply_operator` principals with `supply` access keep the full financial/document read model and download workflow.

### Archived MCP Compatibility Contract

WebCore Data MCP is retained only as an archived read-only compatibility gateway and must not expose browser session cookies as its MCP auth boundary. It is not a normal prompt/source/acquisition path and is never a prerequisite for canonical server-side reads. Its repo-owned compatibility runner is `apps/webcore_data_mcp_server.py`, defaulting to loopback `127.0.0.1:8766` and `POST /mcp`. When explicitly maintaining the legacy surface, the public nginx allowlist can route only exact OAuth/MCP paths to that loopback upstream: `/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/.well-known/openid-configuration`, `/oauth/authorize`, `/oauth/token` and `/mcp`; no prefix/static/runtime-file exposure is part of the compatibility contract. Relevant env names are:
- `WEBCORE_DATA_MCP_HOST`
- `WEBCORE_DATA_MCP_PORT`
- `WEBCORE_DATA_MCP_PATH`
- `WEBCORE_DATA_MCP_HEALTH_PATH`
- `WEBCORE_DATA_MCP_AUTH_MODE`
- `WEBCORE_DATA_MCP_BEARER_TOKEN` or `WEBCORE_DATA_MCP_BEARER_TOKEN_SHA256`
- `WEBCORE_DATA_MCP_DB_PATH` (optional override; default is `REGISTRY_UPLOAD_RUNTIME_DIR/registry_upload_runtime.sqlite3`)
- `WEBCORE_DATA_MCP_AUDIT_LOG_PATH`
- `WEBCORE_DATA_MCP_RESOURCE_URL`
- `WEBCORE_DATA_MCP_RESOURCE_DOCUMENTATION_URL`
- `WEBCORE_DATA_MCP_AUTHORIZATION_SERVERS`
- `WEBCORE_DATA_MCP_SCOPES`
- `WEBCORE_DATA_MCP_OAUTH_ISSUER`
- `WEBCORE_DATA_MCP_OAUTH_SIGNING_SECRET`
- `WEBCORE_DATA_MCP_OAUTH_OWNER_USERNAME` or `WB_CORE_WEB_AUTH_USERNAME`
- `WEBCORE_DATA_MCP_OAUTH_OWNER_PASSWORD_HASH` or `WB_CORE_WEB_AUTH_PASSWORD_HASH`
- `WEBCORE_DATA_MCP_OAUTH_SESSION_SECRET` or `WB_CORE_WEB_AUTH_SESSION_SECRET` (optional session-cookie auto-consent)
- `WEBCORE_DATA_MCP_OAUTH_CODE_STORE_PATH`
- `WEBCORE_DATA_MCP_OAUTH_ALLOWED_REDIRECT_PREFIXES`
- `WEBCORE_DATA_MCP_OAUTH_ALLOWED_CLIENT_ID_PREFIXES`
- `WEBCORE_DATA_MCP_OAUTH_CODE_TTL_SECONDS`
- `WEBCORE_DATA_MCP_OAUTH_ACCESS_TOKEN_TTL_SECONDS`
- `WEBCORE_DATA_MCP_MAX_HTTP_WORKERS`
- `WEBCORE_DATA_MCP_MAX_TOOL_WORKERS`
- `WEBCORE_DATA_MCP_TOOL_DEADLINE_SECONDS`
- `WEBCORE_DATA_MCP_MAX_TOOL_RESULT_BYTES`

Default MCP OAuth scopes:
- `webcore.analytics.read`
- `webcore.supply.read`
- `webcore.finance.read`
- `webcore.ops.read`

Retained EU private MCP compatibility state:
- `wb-core-data-mcp.service` is installed and enabled as a loopback-only private service;
- the generated bearer secret is stored outside Git in root-only `/etc/wb-core-data-mcp.env`;
- `/etc/wb-core-data-mcp.env` is consumed by the MCP unit and must not be printed, committed, copied into docs or placed in `/opt/wb-ai/.env`;
- when `https://api.selleros.pro/mcp` is published for legacy compatibility, unauthenticated access must be auth-blocked with no business data; historical connector access uses owner-only OAuth 2.1 auth-code + PKCE S256. This does not authorize selecting MCP for new prompts or normal execution.

Archived MCP compatibility publication gate:
- unauthenticated `POST /mcp` must return `401` with no business data;
- `tools/list` must expose the compact primary profile (currently 16 names) while known legacy names remain callable compatibility-only;
- every exposed tool must have exact input/output schemas and full read-only/non-destructive/idempotent/closed-world annotations;
- every exposed tool must carry an OAuth `securitySchemes` scope in the `webcore.*.read` namespace;
- ops diagnostics tools must carry only `webcore.ops.read`, accept only enum allowlists/bounded date-log args, and return sanitized summaries for fixed units/logs/refresh-load state/snapshots/deploy labels;
- the DB read path must use SQLite `mode=ro` and `PRAGMA query_only=ON`;
- no MCP tool may expose arbitrary SQL, shell/SSH, arbitrary filesystem browsing, upstream sync/backfill/refresh/load, restart, supplier write/upload/rematch/price-check, runtime file download, secrets, storage-state content, raw env or raw payload dumps;
- OAuth authorization codes are one-time, short-lived and stored outside Git under runtime state; access tokens are short-lived, HMAC-signed, audience-bound to `WEBCORE_DATA_MCP_RESOURCE_URL`, scope-bound and never logged or printed.
- the canonical deploy writes schema-v2 `.wb-core-deploy.json` only through
  the repo-owned runner after rsync. Its early atomic record exposes the exact
  40-character commit with `deployment_complete=false`; only after dependency,
  schema/backup, managed-unit, restart and readback stages succeed does the
  runner atomically replace it with `deployment_complete=true`. `deploy_state`
  exposes the commit, timestamp and completion bit even though `.git` is
  excluded from production;
- Autoanswers schema-backup verification explicitly closes every SQLite
  snapshot handle before deleting a byte-verified raw snapshot and reading
  filesystem headroom. This prevents an unlinked multi-gigabyte snapshot from
  remaining charged to the backup mount until process exit and falsely
  halting an otherwise complete deploy. An interrupted raw current-schema
  snapshot is compressed only after exclusive locking checkpoints every
  committed WAL page into its main file, so the archive never omits a valid
  sidecar-backed page;
- when the active volume cannot hold a second raw Autoanswers database and no
  earlier recoverable Autoanswers backup exists, the schema preflight may
  create the current-version restore point only through the repo-owned streamed
  path: deployment services remain quiesced, exclusive SQLite locking and a
  completed WAL checkpoint stabilize the main file, and zstd output is accepted
  only after source integrity, frame, compressed-hash and exact
  decompressed-SHA readback. The live source is not rewritten, and additive DDL
  remains blocked on any verification or operational-headroom failure;
- an Autoanswers runtime constructor first verifies the complete contiguous
  `1..current` schema-marker chain through a closed read-only connection and
  performs no DDL when that chain is already applied. Only a missing/new schema
  version enters the
  exclusive migration lock and `BEGIN IMMEDIATE`, so an ordinary HTTP restart
  cannot lose startup to a concurrent bounded worker transaction;
- canonical deploy sets `WB_AUTOANSWERS_DEPLOY_SERVICE_QUIESCE=1` for
  `prepare-deploy`. The repo-owned quiet window stops only the exact
  Autoanswers timers/services and registry HTTP service, records their prior
  state, migrates all Autoanswers tables from the main runtime DB into
  `wb_autoanswers_runtime.sqlite3` from one query-only snapshot, verifies
  per-table counts/digests plus SQLite integrity/foreign keys/source
  `data_version`, atomically publishes a private manifest and restores the
  registry service and exactly the timers that were active before the quiet
  window. Interrupted one-shot executions resume idempotently on those timers.
  The legacy tables remain intact as rollback evidence; no ordinary worker or
  readonly-sync connection returns to them.
- interrupted publication is fail-closed: the private `prepared` manifest is
  fsynced before the atomic store rename; startup accepts it only after full
  per-table digest, foreign-key and integrity re-verification of the published
  store. Emergency older-code rollback uses
  `autoanswers-store-rollback-plan`, then exact-fingerprint
  `autoanswers-store-rollback-apply`: under the same quiet window it snapshots
  and verifies the retained legacy table set, copies the current isolated
  queues/settings/audit back in one transaction, proves every table digest and
  preserves all non-Autoanswers registry tables;
- Autoanswers owner-policy v5 activation uses only hosted
  `autoanswers-policy-v5-reconciliation dry-run|apply|readback`, pinned to the
  exact complete deployed SHA. The command refuses to run unless the worker
  timer is disabled/inactive and its service is inactive; GET-only sync may
  remain active. Dry-run/readback are SQLite `mode=ro` plus
  `PRAGMA query_only=ON`. Apply requires the external reviewed fingerprint and
  atomically evaluates every zero-write publication, rebinds or rekeys it under
  v5, advances the policy epoch once and appends hash-only audit. A possible
  prior WB write/readback and every execution-owned job, attempt, cost,
  reservation and uncertainty row are protected by byte-stable immutable
  digests and are never modified. Feedback truth/version/media evidence is a
  separate GET-only group: while the canonical readonly-sync timer remains
  enabled/active it may advance between apply and readback, and the runner
  reports its before/after counts, count deltas and digests as a bounded
  observed delta. A count regression, any immutable execution drift, a WB POST
  attempt or provider-call boundary still blocks. Legacy flat v1 reviewed
  plans/audits are split on read without changing their applied fingerprint.
  Lifecycle resume is forbidden until readback is `reconciled` with exact
  scope/counts, zero stale/metadata-stale/incoherent rows and zero WB/provider
  deltas;
- human-gated Autoanswers zero-backlog recovery uses only hosted
  `autoanswers-backlog-recovery capture|dry-run|apply|readback`. Every action
  is pinned to exact complete `.wb-core-runtime-sha` and deploy metadata;
  external manifest/plan/evidence files stay outside the checkout and are
  streamed to the active target. Apply alone receives mutation capability and
  additionally requires the exact reviewed plan/fingerprint plus human-gate
  reference. Capture, dry-run and readback use official WB GETs only; dry-run
  and readback open SQLite with `mode=ro` and `PRAGMA query_only=ON`. The runner
  has no WB write adapter and performs zero provider calls. The reviewed
  fingerprint binds exact target job/publication/write/reservation/cost and
  frozen-audit evidence, backup, pre-change digest and non-target invariants.
  Apply is unavailable during an active reservation or unresolved
  `budget_state_unknown` provider boundary; that uses the dedicated budget
  lifecycle first. A persisted planned run resumes only across its own bounded
  T0 detail-upsert prefix. Readback accepts only matched official list/count
  zero, exact answered T0 details, current local answer observations, zero
  local recovery tails, zero active reservations and zero unresolved provider
  cost;
- stale local processed observations discovered by that terminal readback use
  only hosted `autoanswers-answered-inventory-recovery
  capture|dry-run|apply|readback`. The deployment-inert runner captures the
  complete official `isAnswered=true` processed inventory twice at bounded GET
  pace. Its v2 manifest binds stable content, the deployed SHA and exactly one
  disposition per row: a normalized observed-answer hash or answerless
  `state=wbRu`; a row with neither proof fails closed. Query-only dry-run selects
  exact local missing/divergent processed observations. Apply requires the
  reviewed external manifest/plan/fingerprint plus a fresh human approval
  reference, resumes through the schema-v10 ledger, performs only canonical
  feedback-observation upserts and preserves settings plus all
  job/publication/WB-write/cost/reservation counts. It has no provider or WB
  write capability and cannot fabricate answer text. Query-only readback proves
  every target answer hash or exact `wbRu` no-answer disposition and equality
  between locally actionable empty-answer IDs (excluding `wbRu`) and the fresh
  official unanswered inventory; new remote dispositions outside the manifest
  are not admitted;
- the same deploy quiet window runs
  `supplier_financial_source_migration_v1`: dry-run validates every existing
  bank-statement file against its recorded SHA-256, apply hard-links identical
  sources into one content-addressed path, transactionally updates only those
  document paths, performs exact readback and stores a private rollback
  manifest. A rerun is a verified no-op. Rollback reconstructs the original
  paths from the exact content source before restoring DB references;
- while the compatibility unit remains in the deploy manifest, canonical deploy installs/enables/restarts `wb-core-data-mcp.service`; explicit compatibility-maintenance verification covers authenticated initialize/list/direct business/ops calls, concurrent latency and commit equality;
- a connector refresh in the ChatGPT UI is relevant only to an explicitly scoped archived-compatibility maintenance task, never to ordinary data acquisition.

SQLite contention production acceptance is:

`python3 apps/registry_upload_http_entrypoint_hosted_runtime.py sqlite-contention-ui-flow --evidence-dir <outside-repo> --deployed-sha <exact-merge-sha>`

The runner waits for a timer-owned Autoanswers worker/readonly-sync process
without starting one, then runs the isolated Playwright calculation and
supplier statement preview/cancel Flow. It never confirms a new production
bank posting.

Current required upstream secret contract stays:
- `WB_API_TOKEN`
- `OPENAI_API_KEY`

Optional runtime overrides remain the same as in current official-api boundary:
- `WB_OFFICIAL_API_BASE_URL`
- `WB_ADVERT_API_BASE_URL`
- `WB_SELLER_ANALYTICS_API_BASE_URL`
- `WB_STATISTICS_API_BASE_URL`
- `WB_FEEDBACKS_API_BASE_URL`
- `WB_SUPPLIES_API_BASE_URL`
- `WB_PRICES_API_BASE_URL`
- `OPENAI_MODEL`
- `OPENAI_API_BASE_URL`
- `OPENAI_TIMEOUT_SECONDS`
- `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`
- `SELLER_PORTAL_CANONICAL_SUPPLIER_ID`
- `SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL`
- `SELLER_PORTAL_RELOGIN_SSH_DESTINATION`
- `SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_API_BASE_URL`
- `SHEET_VITRINA_WEB_SOURCE_SNAPSHOT_BASE_URL`
- `SHEET_VITRINA_SELLER_FUNNEL_SNAPSHOT_BASE_URL`

Current promo live-wiring note:
- if hosted runtime uses the repo-owned `promo_by_price` live seam, service env must expose a valid seller session state path for the bounded browser collector;
- canonical selleros host default = `/opt/wb-web-bot/storage_state.json`, but runtime may override it explicitly via `PROMO_XLSX_COLLECTOR_STORAGE_STATE_PATH`.
- when the hosted operator contour exposes permanent seller-session recovery, the same env also defines canonical organization truth and reusable launcher metadata:
  - `SELLER_PORTAL_CANONICAL_SUPPLIER_ID` = authoritative supplier that the saved seller session must target before recovery is considered successful;
  - `SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL` = operator-facing org label for the same supplier;
  - `SELLER_PORTAL_RELOGIN_SSH_DESTINATION` = SSH host alias baked into the downloadable macOS launcher for localhost-only noVNC tunneling.
- hosted deploy contract must materialize the bounded workbook/parser/browser dependency on the remote system python:
  - current canonical packages = `openpyxl==3.1.5`, `xlrd==2.0.1`, `playwright==1.58.0`, `pypdf==6.4.1`, `reportlab==4.4.5`
  - deploy runner installs them on host before restart if they are still missing;
  - deploy runner also verifies or installs Playwright Chromium with host browser dependencies before restart.
- current seller-portal relogin recovery on the EU hosted runtime is repo-owned dependency setup, not a manual one-off host state:
  - `/opt/wb-web-bot/venv/bin/python` must exist and carry `playwright==1.58.0` plus `psycopg2-binary==2.9.11` for seller-session probes and owner capture DB writes;
  - `/opt/wb-web-bot/storage_state.json` remains runtime data and is never created, printed or deleted by deploy;
  - the deploy runner creates/repairs `/opt/wb-web-bot/venv` with `python3 -m venv`, installs pinned packages there and ensures Chromium can launch from both the hosted runtime system python and the wb-web-bot venv.
- live Seller Portal automation uses one shared single-flight lock at `/opt/wb-core-runtime/state/seller_portal_automation.lock.json`. Status sync, complaint submit/batch, auto-complaints tick/run-now, target/matching/filter scouts, dry-run, confirmation/detail probes, relogin/recovery and future complaint parser/export browser jobs must acquire this lock before opening Playwright; public status/submit/auto launchers return controlled `seller_portal_automation_busy` metadata instead of starting a second browser context. Stale lock cleanup requires process/heartbeat evidence, and reports may include only safe lock fields (`owner`, `purpose`, `run_id`, `started_at`, `pid`, `host`, `expected_max_seconds`, `heartbeat_at`) without cookies/tokens/headers/storage-state content.
- EU live Seller Portal jobs use canonical bot session state `/opt/wb-web-bot/storage_state.json` by default, or an explicit `SELLER_PORTAL_STORAGE_STATE_PATH` pointing to that live contour. They must not implicitly fall back to local Mac paths such as `/Users/.../storage_state.json`; an invalid live path is a storage-state policy blocker, not a hidden fallback. The dedicated bot account/recovery process materializes only this canonical EU runtime file and never prints its contents.
- Seller Portal browser tasks validate the route capabilities they need rather than treating a generic cabinet page as enough. Capability names include `seller_portal_base`, `feedbacks_list`, `complaints_pending`, `complaints_answered`, `analytics_supplier_context` and `canonical_supplier_context`; status sync requires both complaints status tabs, parser/export requires `complaints_answered`, and submit/batch requires feedbacks list plus the guarded row/modal gates. Auth redirects become precise blockers such as `session_invalid_for_route: complaints_answered`.
- Navigation policy is human-stable and bounded, not stealth/evasion: warm up Seller Portal base, wait for canonical supplier/app shell, enter the relevant module, then open deep status URLs or tabs; after major navigation, tab/filter changes, pagination/detail drawer transitions and submit/status-changing actions, runners use deterministic short settle waits and stop on auth redirect rather than looping reloads.
- steady-state Seller Portal bot-backed capture on EU is a separate owner runtime contour, not a public nginx route:
  - non-secret owner code lives under `/opt/wb-web-bot/bot` and `/opt/wb-ai`;
  - `/opt/wb-ai/venv/bin/python` must carry `fastapi==0.129.1`, `uvicorn==0.41.0`, `psycopg2-binary==2.9.11` and `requests==2.32.5`;
  - `wb-ai-api.service` is repo-owned systemd wiring and binds `/opt/wb-ai/api.py` to `127.0.0.1:8000`;
  - web-vitrina materialization adapters default to that local owner API for `GET /v1/search-analytics/snapshot` and `GET /v1/sales-funnel/daily`, with env overrides only for an explicit alternate owner runtime;
  - local PostgreSQL is the EU handoff store for source raw tables and local read-side tables; credentials remain in host env files and must not be printed. DB/schema initialization is operational runtime setup, while deploy verifies packages, venvs, code/import contract and the localhost API systemd unit.
- current seller-portal relogin recovery also expects host OS packages that deploy now verifies/installs:
  - `python3-pip`
  - `python3-venv`
  - `xvfb`
  - `x11vnc`
  - `novnc`
  - `websockify`
  - `openbox`
- these packages are used only by the repo-owned seller-session recovery contour `apps/seller_portal_relogin_session.py`; it binds noVNC to `127.0.0.1` on the host, issues a per-run `run_id`, must materialize a real visible headed Chromium window before surfacing `awaiting_login`, and is intended for temporary auth recovery rather than for the steady-state ingest path. Recovery writes updated `storage_state.json` only after validated auth plus canonical supplier confirmation/safe switch, stages the validated candidate, atomically replaces the canonical file, then runs a fresh post-save probe and restores the previous backup if that probe fails. After saving a validated session, its post-login refresh trigger calls the protected WebCore refresh route with an in-memory app-session cookie derived from the hosted env file; cookie material, password hashes, session secrets and storage-state contents must not be printed.
- current steady operator path over that tool is bounded and HTTP-owned:
  - `GET /v1/sheet-vitrina-v1/seller-portal-session/check`
  - `POST /v1/sheet-vitrina-v1/seller-portal-recovery/start`
  - `POST /v1/sheet-vitrina-v1/web-vitrina/seller-portal-recovery/start`
  - `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status`
  - `POST /v1/sheet-vitrina-v1/seller-portal-recovery/stop`
  - `GET /v1/sheet-vitrina-v1/seller-portal-recovery/launcher.zip`
- `GET /v1/sheet-vitrina-v1/seller-portal-recovery/status` must remain a 200-shape status surface even when the EU host is missing `/opt/wb-web-bot/venv/bin/python`; that state is surfaced as a seller-session probe error, not as a public 500. Recovery payloads expose machine-readable lifecycle gates: `run_id`, `run_status`, `running`, `launcher_ready`, `can_download_launcher`, `can_open_login_window`, `launcher_url`/`launcher_download_path`, `reason`, `summary`, `storage_state_path`, `final_marker` and the separate `session_status`. Default reads may perform the current session probe; `probe=0` is the cheap run-state read for UI refreshes that must not synchronously probe Seller Portal.
- the downloadable launcher stays Mac-only and does not expose noVNC publicly: `launcher.zip` is a zip only while the current recovery run is `awaiting_login` and `can_download_launcher=true`; outside that state it returns truthful `409` JSON, not public `500`. The 409 body classifies known states (`no_active_run`, `run_starting`, `launcher_artifact_missing`, `run_replaced`, `run_final`, `launcher_not_ready`) so the UI can keep `starting` as normal "окно готовится" state instead of showing a fatal download failure. The launcher binds to the current recovery `run_id`, opens a SSH tunnel to the localhost-only host port, waits for local HTTP-ready, launches `http://127.0.0.1:<port>/vnc.html?...path=websockify&reconnect=1` locally, polls `GET /seller-portal-recovery/status?run_id=...` and always prints a final completion marker (`completed / not_needed / stopped / timeout / error`) before exiting.
- the buyer-session recovery for `Цены -> Проверка СПП` reuses only the installed headed-browser OS/runtime dependencies, never Seller Portal storage or lock. Its canonical state is the protected `0700` persistent Chromium profile `/opt/wb-core-runtime/state/wb_buyer_session/chromium_user_data` plus HMAC-only account metadata; the old `storage_state.json` remains untouched and may be read once only for best-effort cookies/localStorage migration. Session check, recovery, saved-account auto-login and authenticated SPP price reads all use `chromium.launch_persistent_context` with that same `user_data_dir`. Ordinary authenticated-price probes are headed Chromium operations on a private ephemeral Xvfb display under the buyer automation lock; headless exact-price probes are forbidden because they can re-trigger the WB security challenge after a successful headed recovery. Buyer use of `browser.new_context(storage_state=...)`, candidate storage state, `context.storage_state(...)`, IndexedDB snapshot capture and validation in a browser importing a snapshot is forbidden.
- buyer recovery has one single-flight start lock and one supervisor-owned automation lock, localhost-only VNC/web ports and protected recovery routes owned by `Настройки → Источники и сессии`. Opening the SPP subsection performs only the exact read-only capability check and never starts/rejoins recovery or exposes launcher/noVNC controls. That capability check uses one atomic persistent-context operation that proves both authenticated `/lk` and the validation-nmID price; it must not launch a redundant session-only context before the price operation. WB HTTP 498 / `Подозрительная активность` is a `security_challenge`, and the centralized headed recovery keeps the challenge surface alive until it clears or requires operator action instead of collapsing it to a generic probe error. The recovery supervisor still proves `/lk` plus a successful authenticated price response across a persistent-profile restart before publishing `completed`.
- buyer recovery supervisor cleanup is bound to exact `run_id + PID + process group + process-start` evidence. Launcher lifetime polls the exact terminal recovery status, closes the localhost noVNC tab and SSH tunnel, and does not infer completion only from a disappearing port.
- the buyer recovery supervisor is an isolated process-group leader; Xvfb/openbox/x11vnc/websockify and its browser stay in that same group. `stop` terminates the complete group and removes supervisor identity, while normal completion closes both persistent contexts, terminates every display/noVNC child and releases lock-owner metadata. No terminal path deletes the persistent profile or legacy `storage_state.json`.
- `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` for `source_group_id=seller_portal_bot` must preflight the same canonical Seller Portal session before heavy source fetch. Invalid, missing, wrong-supplier or probe-failed sessions return an action-required job result with `failed_stage=session_preflight`, session status/reason and operator guidance; valid canonical sessions continue into the normal bot-dependent source fetch over the same `/opt/wb-web-bot/storage_state.json` contract.

Secrets stay outside Git. Repo stores only env names and target shape.

## Canonical Completion Sequence

For live/public tasks affecting this contour `repo-only` does not count as complete. The canonical sequence is:
1. repo fix and local validation;
2. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py print-plan`;
3. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py deploy`;
4. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py loopback-probe`;
5. `python3 apps/registry_upload_http_entrypoint_hosted_runtime.py public-probe`;
6. verify the installed repo-owned systemd units via `systemctl cat` / `systemctl list-timers` when the task depends on scheduler truth;
7. if the task changes archived Apps Script guard code, finish `clasp push` plus guard-only verify.

For current web-vitrina work, final verification is the server/public web surface:
- `GET /v1/sheet-vitrina-v1/web-vitrina`
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`
- `GET /sheet-vitrina-v1/vitrina`

Promo current correctness guard:
- run `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py` after hosted deploys or live/public verification tasks where current promo correctness must be proven, and after any change touching `promo_by_price` materialization, promo archive/artifact validation, promo collector diagnostics/status handling, expected `ended_without_download` / non-materializable campaign handling, `sheet_vitrina_v1` refresh orchestration, promo temporal acceptance/fallback, promo source-status reduction, or web-vitrina read/page-composition code that can affect promo metric row visibility.
- The smoke is read-only: it reads public `status`, `web-vitrina` and `plan` surfaces and does not call `/v1/sheet-vitrina-v1/load`, Google Sheets/GAS, browser `localStorage`, or a refresh endpoint.
- It validates that `metadata.refresh_diagnostics.source_slots[]` contains `promo_by_price[today_current]`, source status/origin and `requested_count / covered_count` are coherent, `fatal_missing_artifact_count == 0` and `true_artifact_loss_count == 0` when exposed, expected ended/no-download artifacts remain diagnostic-only with `workbook_required=false` instead of fatal, current promo metric rows are present and not all blank, and truthful zero rows for ineligible SKU remain valid.
- Preferred command: `python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`.
- If the local machine cannot validate the selleros certificate chain, the accepted diagnostic-only fallback is `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1 python3 apps/sheet_vitrina_v1_promo_current_live_invariant_smoke.py`. This is only a local CA verification fallback; route timeouts, non-200 responses or bad payloads are real blockers.

Feedbacks tab/route guard:
- run `python3 apps/sheet_vitrina_v1_feedbacks_http_smoke.py`, `python3 apps/sheet_vitrina_v1_feedbacks_ai_smoke.py` and `python3 apps/sheet_vitrina_v1_feedbacks_browser_smoke.py` after changes touching the `Отзывы` tab, `GET /v1/sheet-vitrina-v1/feedbacks`, `feedbacks/ai-prompt`, `feedbacks/ai-analyze`, official feedbacks adapter/token path, OpenAI adapter path, server-side prompt storage or feedbacks date/filter/table UI.
- Live/public closure must first prove unauthenticated operator/product routes are blocked by login/401 and then authenticate through the app-level login cookie before reading `/sheet-vitrina-v1/vitrina`, one bounded `GET /v1/sheet-vitrina-v1/feedbacks?...`, `GET/POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt` and one bounded small `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` on the hosted runtime when AI feedback analysis changes. This verifies route wiring, auth cookie compatibility for same-origin fetches, `WB_API_TOKEN` permission for feedbacks, `OPENAI_API_KEY` visibility to the service without printing the key, friendly upstream error surfacing and normalized JSON shape without `/load`, Google Sheets/GAS, bypassing Seller Portal safety gates or accepted-truth persistence.

Google Sheets, GAS, `clasp`, `/v1/sheet-vitrina-v1/load` and `invalid_grant` are not active blockers for web-vitrina completion. If a task explicitly changes archived Apps Script guard code, verify blocked/archived behavior only.

`deploy-and-verify` may be used as one combined step when access is already safe and available.

Current deploy contract note:
- `deploy` does more than `rsync + restart`:
  - sync current checkout;
  - ensure host OS dependencies for SellerPortalBot recovery are present (`python3-pip`, `python3-venv`, `xvfb`, `x11vnc`, `novnc`, `websockify`, `openbox`);
  - use the same installed headed-browser dependencies for the independent WB buyer-session recovery while preserving separate state, lock and ports;
  - ensure host OS dependencies for SellerPortalBot owner runtime are present (`postgresql`, `postgresql-client`);
  - ensure required hosted runtime python packages are present (`openpyxl==3.1.5`, `xlrd==2.0.1`, `playwright==1.58.0`, `pypdf==6.4.1`, `reportlab==4.4.5`);
  - create/repair `/opt/wb-web-bot/venv`, install `playwright==1.58.0` and `psycopg2-binary==2.9.11` into it and ensure Playwright Chromium can launch from both Python contexts;
  - create/repair `/opt/wb-ai/venv`, install the pinned local API/handoff packages and verify `/opt/wb-web-bot/bot` plus `/opt/wb-ai/run_web_source_handoff.py` imports;
  - install/update repo-owned systemd units when configured;
  - render the repo-owned nginx public route allowlist into the configured server block, create a timestamped backup before changing the file, validate with `nginx -t`, and reload nginx only after validation succeeds;
  - restart runtime;
  - only after that run loopback/public verification.
- nginx public route publishing is idempotent: the runner removes prior `WB-CORE MANAGED PUBLIC ROUTES` block, prior `WB-CORE MANAGED TLS` block and matching legacy/manual locations from the configured server config, rewrites the target `server_name` directive to the target's explicit `nginx_public_routes.server_names` when provided, then inserts generated TLS and route blocks from target/manifest truth. New public routes for this contour must be added to that manifest and verified through the deploy runner; manual live nginx edits are not the completion path.
- The allowlist intentionally uses exact locations plus narrow route-family prefixes such as `/v1/sheet-vitrina-v1/warehouses/`, `/v1/sheet-vitrina-v1/supply/factory-order/`, `/v1/sheet-vitrina-v1/supply/wb-regional/` and `/v1/sheet-vitrina-v1/research/`; broad catch-all publication is not part of the current contract.

If deploy / publish / restart / probe / required verify steps are safe and available, Codex обязана выполнить их в том же bounded execution. `clasp` is part of this list only for archived Apps Script guard changes.
If any of these steps are unavailable or unsafe, execution must return incomplete with an exact blocker instead of a vague ops-gap.

## Probe Norm

Loopback/runtime probe validates the hosted process behind the reverse proxy or equivalent publish layer.

HTTP probe boundary: canonical `loopback-probe`, `public-probe` and `deploy-and-verify` доказывают transport, auth-aware route/content shape и service health, но HTTP `200`, `curl` или наличие HTML не являются полноценной production UI-проверкой. Когда acceptance требует UI evidence, дополнительно применяется browser contract из [`07_codex_execution_protocol.md`](07_codex_execution_protocol.md): фактический Playwright/Browser render, DOM/final URL, отсутствие `5xx`/`pageerror`/fatal surface, классификация существенных console errors и визуально проверенный screenshot. Этот UI Flow по умолчанию использует изолированный context без пользовательского profile/cookies/credentials и не выполняет clicks, input или business mutations вне explicit scope.

Canonical `loopback-probe`, `public-probe` and `deploy-and-verify` are auth-aware and fast by default:
- when production WebCore app-level auth is configured, the runner may create a short-lived same-origin session cookie from the hosted runtime env file and use it only in memory for probe requests; the cookie, password hash and session secret must not be printed in JSON, logs, PR text or handoff;
- unauthenticated browser/curl reads may return login redirect or auth error after auth hardening, but the canonical runner must verify the authenticated operator surface with sanitized auth metadata only;
- bounded JSON reads must continue across short socket chunks until EOF or the configured byte limit plus one byte; a short transport read is not EOF and must not turn a valid large response into a false `expected JSON object response` failure;
- warehouse detail responses declare a small `probe_shape` before their potentially large provenance collections. When a valid response exceeds the bounded body prefix, the probe requires the exact warehouse identity plus the `balances`/`documents` collection declaration instead of treating a collection key beyond the byte limit as absent; missing or mismatched bounded shape remains fail-closed;
- full `POST /v1/sheet-vitrina-v1/refresh` is a heavy mutating/deep check and is not part of ordinary health probes; it runs only when `--include-refresh` is passed, while `--skip-refresh` remains a compatibility force-skip flag;
- deploy closure must use canonical probes for service health and may run the explicit deep refresh probe only when the task scope actually changes refresh semantics.

Public probe validates:
- `GET /sheet-vitrina-v1/operator` returns `200` + `text/html` for the unified shell; public probe also checks `GET /sheet-vitrina-v1/operator?embedded_tab=reports` for the embedded report panel and `GET /sheet-vitrina-v1/operator?embedded_tab=factory-order` for the embedded supply panel. Together they must contain compact operator tokens for the top-level sections, server refresh, truthful manual-vs-auto blocks, report subsections, plan-report baseline controls, feedbacks tab, right-side settings action and bounded supply subsections (`Ручная загрузка данных`, `Источники и сессии`, `Поставки`, `Отчёты`, `Отзывы`, `Настройки`, `Выйти`, `Загрузить отзывы`, `Загрузить данные`, `Legacy Google Sheets`, `Еженедельные отчёты`, `Финотчёт ВБ`, `Партнёрский отчёт`, `Отчёт о доходности карточки`, `Скачать пакет для партнёра`, `Отчёт по остаткам`, `Выполнение плана`, `Равномерный годовой план`, `Прогноз к концу договорного периода при текущем темпе`, `Исторические данные для отчёта`, `planReportApplyButton`, `planReportAnnualEvenCheckbox`, `planReportProjectionTable`, `planReportBaselineTemplateButton`, `planReportBaselineFileInput`, `Total Order Sum`, `Негативные факторы`, `Позитивные факторы`, `Скачать лог`, `Лог`, `Автообновления`, `Часовой пояс`, `Следующий`, `Последний запуск`, `Последний успех`, `Статус`, `Общий вход для двух расчётов`, `Заказ на фабрике`, `Поставка на Wildberries`, `Цикл заказов`, `Цикл поставок`, `Скачать все рекомендации`). The supply frame must not render Seller login/recovery buttons; it links to centralized settings. The ordinary plan-report UI must not expose legacy contract-start checkbox/date controls; it sends canonical hidden contract-start params and sends `annual_plan_evenly_distributed=false|true` from the optional annual-even checkbox. Plan-report results render the contract-period projection card before selected/MTD/QTD/YTD cards, and long per-metric diagnostics such as ads-plan-base explanations are exposed through compact `?` tooltips rather than inline metric subtitles.
- The embedded supply probe also requires the `Счёт CNY` / `Конвертации RUB → CNY` delete wiring, including the existing CNY documents route token and the explicit balance/rate/ledger replay warning. Runtime UI verification must confirm that eligible conversion rows expose `Удалить`, source-owned rows do not expose an active direct delete, and no real production document is deleted during deploy verification.
- `GET /v1/sheet-vitrina-v1/seller-portal-session/check` returns `200` + JSON with one truthful status from `session_valid_canonical / session_valid_wrong_org / session_invalid / session_missing / session_probe_error` plus secret-free probe reason when available (`login_redirect`, `validate_401`, `security_challenge`, `access_denied`, `login_page`, `probe_failed`, etc.)
- `GET /sheet-vitrina-v1/vitrina` returns `200` + `text/html` as a real operator-grade web-vitrina page shell: page must contain `Web-витрина`, `Операторский сайт`, primary `Загрузить`, compact header labels `С инцидентами`, `Снимок:`, `обн:` and `Метрики`, main tabs `Витрина`, `Поставки`, `Отчёты`, `Отзывы`, `Исследования`, right-side system actions `Инструкции`, `Настройки` and `Выйти`, canonical JSON route token `/v1/sheet-vitrina-v1/web-vitrina`, feedbacks route token `/v1/sheet-vitrina-v1/feedbacks`, settings users route token `/v1/sheet-vitrina-v1/settings/users`, explicit `surface=page_composition` wiring, bottom `Действия и состояния` and grouped date-scoped source `Проверить`/refresh controls. The header renders snapshot and browser update time as two adjacent independent same-tone pills, with bold snapshot text, regular update text, no nested snapshot substrate and no visible timezone, seconds or freshness token. The opened compact filters rail includes the bounded `Столбцы` menu with exactly checked/disabled `Метрика`, checked/toggleable `Раздел` and checked/disabled `Даты`; it never exposes technical columns. Section visibility is browser-local and defaults on. When off, the section header/cells/badges and reserved width disappear, dates immediately follow metric, and row density compacts without changing disclosure or table truth. The Vitrina page contains no top Seller-session badge and no login, relogin, launcher or recovery control; those live only in `Настройки → Источники и сессии`. It also must not contain the Web-vitrina schedule editor, route token, hidden schedule fetch/listeners or a second run-now control. `JSON Connect`, the old cheap top-panel `Обновить` button and the permanent top status badge are not rendered. Page open must not trigger hidden full refresh or hidden heavy group/source fetch. At 760 px and 560 px the table header controls wrap inside the viewport without document overflow.
- `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition` returns `200` + bounded JSON `web_vitrina_page_composition` v1 with `meta`, `summary_cards`, `filter_surface`, `table_surface`, `status_summary`, `capabilities`; route stays read-only, defers heavy `table_surface.rows` unless `include_table_data=1` is explicit, and must not trigger refresh/upstream fetch from the public read path
  - summary/card tone must follow semantic source truth of the visible snapshot or selected period, not mere snapshot existence
  - main table must render before filters/history/actions, use Russian visible headers and expose per-row `Обновлено` timestamp without renaming backend/API field keys
  - `Загрузка данных` must render in the bottom actions block as a grouped compact table with source-group headers `WB API`, `Seller Portal / бот`, `Прочие источники`, one compact date input, a route-specific `Проверить` and optional safe `Повторить сбор` action per group, group-level last update timestamp, server/business `Сегодня: <YYYY-MM-DD>` and `Вчера: <YYYY-MM-DD>` status columns, reason columns, Russian metric labels and a secondary technical endpoint column; it must not fabricate stale-job success when exact transient log association is unavailable. The three groups must cover every visible main-table metric exactly once, with residual calculated/formula metrics assigned to `Прочие источники`.
  - `POST /v1/sheet-vitrina-v1/web-vitrina/group-refresh` must be publicly routed to the hosted runtime. A POST without `source_group_id` is the safe publish probe and must return app-level `400 {"error":"source_group_id is required"}`, not proxy/fallback `404 {"detail":"Not Found"}`. With supported `source_group_id` and `as_of_date`, it must return an async job payload and the job/log must report selected date plus stage-aware source fetch / prepare-materialize / load-to-vitrina outcome, including `updated_cells`/`latest_confirmed_cells` counters. The page may use returned `updated_cells` for session-only green/yellow highlighting, but no permanent styling state is stored.
  - `Лог` must render below that table as the secondary block and keep the existing job/log download contour
  - the former sibling block `Обновление данных` is no longer rendered or exposed as an active page-composition activity block; persisted STATUS/read-side fields remain internal truth for other status contracts
  - top summary must be compact (`Обновлено`, `Статус`, `Период`); the old bulky `Свежесть данных`/`Строки` cards are not separate page blocks. Automatic freshness is monitored in Settings.
- `GET /v1/sheet-vitrina-v1/web-vitrina/business-projection/status` is a
  repo-owned exact public route and a mandatory deploy probe. It returns only
  the bounded projection revision/outbox/failure status used by visible-tab
  revision checks; proxy/fallback `404` is a failed deployment, not an
  acceptable empty projection.
- `GET/POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules` returns/persists runtime-managed web-vitrina refresh schedules. Business cadence remains in the existing runtime JSON and is edited only by the Vitrina card in `Настройки → Автообновления`; response exposes timezone, mutability, `next_auto_run_at`, `last_auto_run_at`, `last_auto_success_at`, `last_auto_error_summary` and per-schedule next/last/status fields.
- `POST /v1/sheet-vitrina-v1/web-vitrina/auto-schedules/run-now` launches the existing async full-refresh job with auto-schedule trigger metadata and is exposed only in Settings. The route must return a job payload quickly and must not call archived Google Sheets/GAS load.
- `GET /v1/sheet-vitrina-v1/prices/spp-test/history`, bounded by `limit<=50` and an opaque cursor, reads `sheet_vitrina_v1_prices/spp_tests/jobs/*.json` newest-first and returns compact summaries only. Legacy detailed jobs remain readable internally; there is no public per-job raw-detail route.
- `POST /v1/sheet-vitrina-v1/prices/spp-test/start` accepts exactly one nmID and an ordered list of 1–6 positive money values. It performs a fresh exact authenticated-buyer-price capability check before baseline/read/write; that authoritative backend Start proof covers price one, and every later measurement repeats a fresh check before its seller write. The first worker must not add a redundant third pre-write Chromium launch after the browser and backend Start checks. It never generates, sorts, deduplicates or refines price points. Invalid/logged-out capability means zero seller writes. Mid-run loss stops remaining prices and proceeds to mandatory seller restore.
- The SPP result uses actual seller discounted price after WB readback and a stable authenticated buyer price. The two identical authenticated reads required for stable proof run inside one locked persistent Chromium context; separate context launches for each stable read are forbidden. Anonymous/public price is not used or exposed by this tester. Status/history/log responses are compact and sanitized; the UI shows exactly the latest ten useful technical events.
- `GET /v1/sheet-vitrina-v1/prices/spp-test/status` reconciles an orphan only after the cross-process execution lock proves that no runner is alive. It requires fresh exact WB tuple (`price`, `discount`, `discountedPrice`) and no quarantine before terminalizing/clearing the pointer; TTL expiry alone is never restore proof. Buyer availability cannot invalidate seller restore. A seller mismatch, quarantine or unsafe readback becomes `manual_restore_required` and blocks new starts until guarded restore succeeds.
- `GET /v1/sheet-vitrina-v1/web-vitrina` returns either:
  - `200` + JSON `web_vitrina_contract` v1 when a ready snapshot is present, with root fields `contract_name`, `contract_version`, `page_route`, `read_route`, `meta`, `status_summary`, `schema`, `rows`, `capabilities`
  - truthful `422 {"error": ...}` when the ready snapshot is absent
  - route remains read-only, optional `as_of_date` override stays on the same boundary and must not trigger refresh/upstream fetch
- `GET /v1/sheet-vitrina-v1/feedbacks` returns `200` + JSON `sheet_vitrina_v1_feedbacks` v1 for a bounded valid query (`date_from`, `date_to`, optional `stars`, `is_answered`). It is read-only over official WB `GET /api/v1/feedbacks` with canonical `WB_API_TOKEN`; it must not trigger refresh, `/load`, Google Sheets/GAS, complaint submission or runtime persistence. If the hosted token lacks feedbacks permission, 401/403 is a real live blocker for the `Отзывы` feature rather than a deploy-script success.
- `GET /v1/sheet-vitrina-v1/feedbacks/ai-prompt` and `POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt` manage server-side operational prompt config in the hosted runtime dir. This prompt is not ЕБД, accepted truth, ready snapshot truth or browser-local truth.
- `POST /v1/sheet-vitrina-v1/feedbacks/ai-analyze` runs a bounded OpenAI Responses API structured-output call over loaded feedback rows. The browser processes the current visible/filtered operator set as a bounded sequential queue and sends exactly one feedback row per request; large visible sets must be rejected client-side with a clear narrowing message. The route still enforces a hard cap of 3 rows per request as a safety guard. Results and per-row failures remain transient for the current UI session and must not persist AI labels, submit complaints, call Seller Portal or write Google Sheets/GAS.
- `POST /v1/sheet-vitrina-v1/feedbacks/complaints/submit-selected` is an auth-protected operator route for selected feedback rows. It must return quickly with a submit job `run_id`, reject `feedback_ids>20` and `max_submit>5`, allow only one active job, skip existing complaint-journal ids, and reuse the guarded Seller Portal submit runner/actionable resolver. It is not a public bypass around exact/actionable/description gates.
- `GET /v1/sheet-vitrina-v1/feedbacks/complaints/submit-job?run_id=...` returns bounded safe job state/events/counters/report paths for that route without secrets, headers, cookies, bearer tokens or storage state.
- `GET/POST /v1/sheet-vitrina-v1/feedbacks/automation/schedules`, `POST /v1/sheet-vitrina-v1/feedbacks/automation/run-now`, `GET /v1/sheet-vitrina-v1/feedbacks/automation/runs`, `GET /v1/sheet-vitrina-v1/feedbacks/automation/run?run_id=...` and `POST /v1/sheet-vitrina-v1/feedbacks/automation/tick` are auth-protected auto-complaints surfaces. Business schedules live in runtime JSON and default to `Asia/Yekaterinburg`; save returns canonical persisted rows, `run-now` accepts only persisted `schedule_id`, and stale ids return structured `schedule_not_found`. Schedule lifecycle fields (`created_at`, `enabled_since_at`, `last_run_at`, `last_success_at`, `last_due_at`, `last_status`, `last_run_id`, `last_stats`) are server-owned and are not accepted from browser save payloads. `run-now` also returns observable `run_id/status/summary` plus refreshed schedules/recent runs; the operator UI polls the sanitized run detail route and `Посмотреть лог` must show either the run report, explicit still-running guidance, or an explicit empty-log state. First runs process only the last 24 hours with no overlap fetch; recurring runs use server-owned `last_success_at..window_to` with a 24h overlap fetch. The systemd timer only invokes the idempotent due-check CLI every 10 minutes. Auto runs use the same feedback loader, saved AI analyzer, complaint journal idempotency, hard cap `5`, Seller Portal lock/storage-state policy and guarded selected-submit runner as manual complaint submission.
- `GET /v1/sheet-vitrina-v1/daily-report` returns `200` + JSON for both states:
  - `status=available` when two latest persisted ready snapshots `<= default_business_as_of_date(now)` are present and their `yesterday_closed` slots are comparable;
  - `status=unavailable` with truthful `reason` when fewer than two eligible ready snapshots exist or either selected snapshot is structurally unusable;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/stock-report` returns `200` + JSON for both states:
  - without explicit `as_of_date`, `status=available` when the latest persisted ready snapshot `<= default_business_as_of_date(now)` contains a valid `yesterday_closed` slot;
  - with explicit `as_of_date`, the route stays strict exact-read and returns `status=unavailable` when that exact ready snapshot or `yesterday_closed` slot is missing/stale;
  - optional `sales_avg_period_days=<positive integer>` controls the availability-adjusted `orderCount` averaging period; missing/empty uses the supply default `14`, non-integer returns controlled JSON `422`, and non-positive values follow the existing supply default semantics;
  - `rows[]` is the full active `config_v2` SKU table, not a legacy low-stock `<50` subset. Rows expose raw numeric sort fields: `supplier_production_qty`, `supplier_in_transit_qty`, `stock_ff`, `wb_supplies_inbound_qty`, `stock_wb`, backward-compatible `stock_total`, `zero_district_count`, `avg_sales_per_day`, `days_left_total`, and per-district `stock`, `avg_daily_burn`, `days_left`;
  - operator visible columns immediately after `Акция` are `на произв.`, `в пути Китай`, `ост. ФФ`, `поставки ВБ`, `ост. ВБ`. `на произв.` and `в пути Китай` are aggregated by active SKU `internal_nm_id` from existing supplier shipment product lines in statuses `production` (`На производстве`) and `in_transit` (`В пути`). `ост. ФФ` reads current balances from server-owned `ff_stock_ledger`. `поставки ВБ` is aggregated by `nmId` from current `sheet_vitrina_v1_wb_supplies` cache `raw_goods` quantity and excludes only status ids `1/2/5` (`Не запланировано`/`Запланировано`/`Принято`); all other status ids/labels are counted when goods composition has positive active-SKU quantity. `ост. ВБ` is the previous `stock_total` WB stock value from persisted ready snapshot, with data semantics unchanged;
  - payload also exposes `summary_row` for the top `Итого` row. The operator table keeps this row above detail rows during sorting; stock/supply values are summed and days-left values use aggregate stock / aggregate demand or burn rather than a simple row average;
  - operator header sorting stays local and must preserve the horizontal `scrollLeft` of the stock-report wrapper when the table DOM is rebuilt, for repeated ascending/descending clicks at the left edge, right edge and intermediate positions;
  - `promotion_participation` is read from canonical `promo_participation` in the persisted ready snapshot: numeric `>0` = `Да`, numeric `0` = `Нет`, missing = `н/д`/`null`;
  - district days-left uses persisted ready-snapshot depletion only: positive decreases between consecutive `yesterday_closed` district stock snapshots are averaged; missing, gap, restock/increase and zero-depletion days are diagnostics and are not fabricated as district расход;
  - route stays read-only and must not trigger refresh/upstream fetch from the public read path
- `GET /v1/sheet-vitrina-v1/plan-report` returns `200` + JSON for valid primary query params `period`, `h1_buyout_plan_rub`, `h2_buyout_plan_rub`, `plan_drr_pct`, optional `as_of_date`, optional boolean `annual_plan_evenly_distributed`, and optional contract-start params; legacy complete `q1_buyout_plan_rub`..`q4_buyout_plan_rub` may be accepted only as transitional fallback:
  - response contains `selected_period`, `month_to_date`, `quarter_to_date`, `year_to_date` blocks plus `contract_period_projection`;
  - each block has independent `available / partial / unavailable` status, coverage details, reason, source mix and metrics; an unavailable YTD block must not hide an available selected period;
  - daily fact source is persisted accepted closed-day snapshots `fin_report_daily.fin_buyout_rub` + `ads_compact.ads_sum` for current active `config_v2` SKU;
  - buyout and ads daily facts use the same accepted temporal source slot layer but keep source-specific coverage; missing one source for a date keeps the block partial instead of dropping the other source's available fact;
  - manual monthly source `manual_monthly_plan_report_baseline` may contribute only full months inside this plan-report route; if daily precision for a baseline month is incomplete, the monthly aggregate covers the month and overlapping daily rows are excluded from the block to avoid double-count;
  - ordinary operator UI defaults, when namespaced browser state is missing or invalid, are `h1_buyout_plan_rub=155379879`, `h2_buyout_plan_rub=294620121`, `plan_drr_pct=6`, `use_contract_start_date=true`, `contract_start_date=2026-02-01`, `annual_plan_evenly_distributed=false`; default period follows the current WB/VB target half-year (`first_half` through 2026-06-30, then `second_half`); H2 is the annual remainder to `450000000`, while Q3+Q4 source figures sum to `294620120`, so the 1 rub discrepancy is explicit;
  - default buyout plan is distributed by calendar day, with the daily amount derived from the H1/H2 plan for each date and independent from fact coverage;
  - fixed target periods (`first_quarter`, `second_quarter`, `third_quarter`, `fourth_quarter`, `first_half`, `second_half`, and selected `current_year`) use the full target-period buyout plan for the main `plan`/completion, clipped by contract start when enabled; their facts and coverage use only the closed fact window up to `min(as_of_date, target_date_to)`, so future target dates are not missing coverage;
  - with `annual_plan_evenly_distributed=true`, plan values use `h1_buyout_plan_rub + h2_buyout_plan_rub` evenly across calendar days of the year or contract-start calculation window, without mutating persisted H1/H2 inputs; ordinary UI exposes this as an optional strategic pace view and defaults it to unchecked/`false`;
  - optional `use_contract_start_date=true&contract_start_date=YYYY-MM-DD` trims selected/MTD/QTD/YTD fact windows and fixed target windows before facts, plan and coverage are calculated; ordinary UI always sends `use_contract_start_date=true&contract_start_date=2026-02-01`; annual-even mode then uses the remaining annual calculation window from contract start through year end;
  - metrics expose `completion_pct = fact / plan * 100` for buyout and ads; legacy `delta_pct = (fact - plan) / plan * 100` remains diagnostic;
  - DRR fact is `ads_sum / fin_buyout_rub * 100`; `plan_drr_pct` is the contractual minimum, so a fact at or above it is `ok`/positive margin and only a value below it is a minimum violation; ads plan follows WB/VB execution semantics: `max(buyout_plan, buyout_fact) * plan_drr_pct / 100`, with `ads_plan_base_rub`/`ads_plan_base_mode` disclosing whether the base was plan turnover or overperformance fact turnover; `ads_sum_rub` is ok when `fact_ads >= ads_plan`, not when spend is below a cost limit;
  - `contract_period_projection` is independent from the selected period and uses facts from `2026-02-01..min(as_of_date, 2026-12-31)` with the same accepted snapshot/monthly baseline source rules; projected buyout/ads = elapsed fact divided by elapsed days and multiplied by total contract days, annual ads plan = `(H1+H2) * plan_drr_pct / 100`, and future contract days are not missing coverage;
  - contract-period turnover truth is buyouts only: both elapsed fact and forecast use `fin_buyout_rub`; orders and `orderSum` are not substitutes;
  - `contract_period_projection` always exposes `annual_buyout_plan_rub`, fixed `usn_upper_limit_rub=490500000`, `projected_buyout_rub`, `projected_buyout_pct_of_annual_plan`, `projected_buyout_pct_of_usn_upper_limit`, `projected_buyout_remaining_to_usn_upper_limit_rub`, `projected_buyout_exceeds_usn_upper_limit`, `drr_minimum_pct`, `drr_requirement_type=minimum`, `projected_drr_pct`, `projected_drr_margin_to_minimum_pp` and `projected_drr_minimum_met`; fixed guardrails remain present for `partial/unavailable`, while derivatives are `null` when the required buyout or DRR projection is unavailable, and USN remaining may be negative on exceedance;
  - `usn_upper_limit_rub=490500000` is the 2026 management reference `450000000 × 1.090`, where `1.090` is the deflator coefficient established by Ministry of Economic Development of Russia order dated 06.11.2025 №734; it is a buyout-forecast reference and does not replace the tax-accounting income register;
  - operator projection UI renders the annual buyout plan `450 млн ₽`, `Верхний порог УСН` at `490,5 млн ₽` and `Минимальный DRR по договору` at `6%`, with accessible management/tax-register and contractual-minimum explanations; DRR above `6%` is rendered as margin, not an alert;
  - route stays read-only, never triggers refresh/upstream fetch, and returns truthful `available / partial / unavailable` coverage semantics instead of fabricating zero facts
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-template.xlsx` returns `200` + XLSX content type with compact Russian headers for monthly baseline upload
- `GET /v1/sheet-vitrina-v1/plan-report/baseline-status` returns `200` + JSON baseline status/totals/months/upload metadata
- `POST /v1/sheet-vitrina-v1/plan-report/baseline-upload` accepts a controlled XLSX upload with months `YYYY-MM` and non-negative numeric facts, rejects empty/invalid/negative rows, stores aggregates idempotently in runtime SQLite and does not write Google Sheets/GAS or accepted daily snapshots
- `/v1/sheet-vitrina-v1/partner-report/...` is an auth-protected `reports` surface. Immutable settings versions/audit live only in runtime SQLite. The primary result is an indexed UI preview; `preview.xlsx` is generated from the same service and must carry the visible preview source digest. Missing Finance/cost/ads inputs remain explicit blockers. No active route finalizes a payout, emits a ZIP/raw Finance workbook or creates a public persistent link.
- Canonical production Finance recalculation uses only `finance-canonical-dry-run`, `finance-canonical-apply` and `finance-canonical-readback` in `apps/registry_upload_http_entrypoint_hosted_runtime.py`. Dry-run/apply scope is the full loaded Finance history. Dry-run streams ordered raw/non-target identities into deterministic digests and retains only persisted target readback evidence plus the required aggregated operation-date matrix. Repeated missing-cost operations collapse by week/SKU/operation-date/reason with operation and sale/return quantities retained; their per-row identities remain in the source hash. Global capitalization allocations, canonical daily costs, the active archival overlay/first-factual boundary, nomenclature and resolved SKU/date evidence are cached only for that connection and rebuilt on a later connection/source state. Apply re-plans under `BEGIN IMMEDIATE`, so any intervening hourly source change invalidates the reviewed fingerprint before mutation. The production-scale regression covers 295,919 sale rows across certified/archival/missing states plus 50,000 functional events and 50,000 supply cost layers. Apply validates active target/runtime paths, schema-v2 plan, exact newly reviewed fingerprint and human approval reference before invoking `canonical-cost-backfill` with the bounded backup directory. The superseded `finance-retro-*`/`business-approved-backfill` path is unavailable; ad-hoc production SQL or server-only scripts are prohibited.
- Partner/Finance incident evidence and missing-ads repair are separate exact-scope hosted actions. `partner-finance-diagnostic --output <external-json>` is strictly read-only, preserves incomplete evidence and streams ordered raw Finance JSON one week at a time. It retains only aggregates, bounded examples and fail-closed maxima of 10,000 operation groups, 10,000 marketing-name candidates and 10,000 anomalous identity keys; invalid raw JSON becomes one exact-count blocker with bounded examples, and duplicate proof uses a second streaming pass only when an identity anomaly exists. A missing projection does not remove the corresponding raw count/digest/identity evidence. The production-scale smoke covers 300,013 raw rows, group/candidate overflow, 12,000 invalid-JSON rows, an explicit peak-RSS bound and unchanged SQLite bytes. `ads-historical-dry-run --nm-id ... --target-date ... --output <external-json>` builds the official fullstats plan; `ads-historical-apply` requires that external plan, exact fingerprint and fresh human approval reference; `ads-historical-readback` verifies only the exact recovered slots. The wrapper rejects Git-checkout plan/evidence paths, writes local evidence mode `0600`, validates the active EU target/canonical runtime directory and cannot redirect a mutation to archived Selleros. Ads apply is a production-data mutation and is not authorized by deploy or an old approval.
- Historical web-vitrina/report consistency repair is performed only through the repo-owned one-off CLI `apps/sheet_vitrina_v1_ready_fact_reconcile.py`: dry-run first, apply only for bounded windows/metrics, no overwrite of existing accepted diffs, no fake zeros from blank ready cells, and no recurring Google Sheets/GAS dependency.
- Proxy V4 initialization is a two-runner exact-SHA production mutation, never a deploy side effect. `apps/sheet_vitrina_v1_buyout_mature_backfill.py` may official-refetch and replace only `2026-07-06..2026-07-12` after strict `33 × 7` `DETAIL_HISTORY_REPORT` coverage. Only after that reconciliation may `apps/sheet_vitrina_v1_proxy_v4_initialize.py` create the two reviewed as-of parameter revisions and update V4 rows in exact ready snapshots from `2026-08-01`. Both default to dry-run, require private external manifests, exact deployed SHA and human approval reference, hold the shared warehouse writer lock for apply, create a verified coherent backup, preserve V3/non-target digests, and require idempotent readback/reconciliation evidence. Neither runner changes V3 formula/history, Finance raw, canonical WB WAC or any pre-boundary public value.
- `GET /v1/sheet-vitrina-v1/status` returns JSON with either success shape including `server_context` + `manual_context` or truthful `422 {"error": ..., "server_context": ..., "manual_context": ...}`
  - on `200`, root `status` is active-vitrina semantic snapshot outcome (`success / warning / error`), while transport/read completion stays separated in `technical_status`; archive-inclusive semantics remain in `technical_semantic_*`/`technical_source_*`, and active `source_outcome_counts` always reconciles with the returned active `source_outcomes` list
  - `server_context` / `manual_context` must keep persisted latest semantic result summaries, so restart/reload does not erase warning/error truth
- `GET /v1/sheet-vitrina-v1/plan` returns JSON with either success shape or truthful `422 {"error": ...}`
- after the current source-aware temporal-policy switch, `stocks[yesterday_closed]` must resolve through exact-date runtime snapshots sourced from Seller Analytics CSV `STOCK_HISTORY_DAILY_CSV`, while `stocks[today_current]` may truthfully stay `not_available`/blank and must not degrade source or aggregate semantic status by itself
- when strict bot/web-source closed-day acceptance is active, `STATUS` / `plan` / job surfaces must disclose truthful closure states (`closure_pending`, `closure_retrying`, `closure_rate_limited`, `closure_exhausted`, `success`) instead of silently reusing provisional same-day values in `yesterday_closed`; if exact closed-day capture is currently blocked but an accepted current snapshot for that same date already exists, the visible closed-day cell may be restored only as `latest_confirmed` fallback (`resolution_rule=accepted_current_from_prior_closed_day_latest_confirmed`) without creating accepted closed truth
- full refresh and date-scoped group refresh must keep prior confirmed visible cells when a selected source/date status is failed or unavailable, while still updating source STATUS/job diagnostics with the exact failure reason; failed bot/web-source materialization must not silently turn previous values into dashes
- when promo live wiring is active, `STATUS` / `plan` surfaces must disclose truthful `promo_by_price[*]` source facts, including `success/incomplete/missing`, collector trace note and accepted-current preservation instead of keeping promo rows as a permanent blocked gap
- `GET /v1/sheet-vitrina-v1/supply/factory-order/status` returns JSON with dataset states, active SKU count, recommendation path, selected `stock_ff_source` and 1C FF_STOCK summary
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/status` returns `payload_version=v2_planning_zones`, active SKU count, methodology note, shared dataset state, eight planning-zone options and an optional v2 last result. A persisted v1 federal-district result is preserved only in bounded `migration_status.legacy_snapshot` and returns `recalculation_required`; it is never guessed into three Central zones.
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options` returns a protected read-only storage-only payload for one selected planning zone of the latest result. For Central zones the exact warehouseID registry classifies North/East/South, while factual eligibility requires complete barcode coverage from WB `acceptance/options`, requested-package support, active catalog state, direct storage classification and no block/exclusion. The backend returns normalized unique box dates, first available/free dates, explicit blocker/exclusion codes, ranking and stock/demand diagnostics; СЦ/СГТ/specialised/partial/inactive/blocked/unclassified options never enter the manager list. It must not create/draft FBW/FBS supplies, book slots, run Seller Portal automation, mutate WB, write Google Sheets/GAS or persist/reallocate selected variants as fact.
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies` returns protected cached WB supplies JSON only, supports `sort_key=supply_date&sort_dir=asc|desc`, and sorts all filtered rows before pagination.
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options` returns protected server-validated selector options for calculation-only WB supply overlays, including eligibility, disabled reasons, dates, active-SKU usable quantity and warehouse district mapping diagnostics.
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/nomenclature...` remains the protected operator-only server-owned nomenclature surface. Default list visibility is `visible`; `visibility=hidden|all` exposes hidden rows. `POST /v1/sheet-vitrina-v1/settings/nomenclature/barcode-sync` is the read-only WB Content card sync: it calls only `POST /content/v2/get/cards/list`, matches local rows by `nm_id`, then barcode, then `vendor_code`, includes hidden rows in matching, does not auto-match by fuzzy WB title and does not unhide hidden rows. Existing rows may update only WB-owned/reference fields and never overwrite nomenclature name, SKU group, purchase price, match key, compatible models, operational `is_active`, hidden status or manual barcode overrides.
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/sku-groups...` is the protected operator-only server-owned SKU group dictionary. It stores group labels and aliases/patterns used for vendorCode auto-detection, seeds Clean/Anti-spy/Matte/No Frame groups plus `extra` and `other`, accepts legacy `clear` rows without destructive migration, and blocks disabling a group while active nomenclature rows still use it.
- `GET/POST/PATCH/DELETE /v1/sheet-vitrina-v1/settings/users...` is a `settings + manage_users` runtime user admin surface. It returns `available_sections`, returns/accepts server-owned `allowed_sections`, preserves legacy `role` for compatibility, never returns plaintext passwords or password hashes, rejects duplicate usernames against env/runtime principals, rejects reserved service/debug/test identities (`codex_*`, `smoke_*`, `test_*`) through ordinary user-facing create, validates section ids and must not allow the final active `settings + manage_users` path to be removed. The default list is user-facing only: server-side classification hides Codex/smoke/test service rows from `users[]`, reports `hidden_service_users_count`, and only admin diagnostic `include_service=1` returns them separately as `service_users[]`. The settings users UI presents `allowed_sections` and `manage_users` through compact access pickers; collapsed summaries are presentation only. Env bootstrap/supplier principals are listed as read-only env rows with compact summaries; their secrets are not mutated from this UI.
- `GET /v1/sheet-vitrina-v1/supply/supplier-shipments/registry` returns protected read-only supplier shipment matrix JSON for `Поставки -> Реестр поставок`; it is built from existing supplier shipment and financial-document runtime truth and must return grouped rows with null-equivalent missing values instead of `NaN`/`Infinity`. КП rows with known absence reasons return short displays such as `нет КП`, `нет в КП`, `ошибка парсинга КП`, `нет стоимости груза в КП`, `курс не подтверждён` or `ждём счета`; plain `—` remains for unknown/not-applicable cells.
- Supplier shipment list/detail/registry live reads must expose the
  server-derived factual-date status contract, including
  `order_status_display`. `GET
  .../supplier-shipments/{shipment_id}/factual-date-correction` returns the
  persisted reload-safe correction state. A live ordinary factual-date
  mutation uses the dedicated preview/confirmation job and central T1 targeted
  replay; the old combined historical-status/document chain remains dry-run
  evidence and its apply fails before backup/write. Direct SQL, server-only
  header edits and legacy movement rewrites are forbidden; authenticated
  UI/API readback must confirm the factual date and status badge after
  deploy/apply.
- If a verified correction candidate cannot be built because immutable historical SQLite backups exhaust the target filesystem, only `apps/sqlite_backup_archive.py` may losslessly transcode an existing file below a `backups/` directory. Hosted `sqlite-backup-archive-dry-run|apply` further restricts this path to one raw checkpoint in the canonical `warehouse-functional-sync` directory and pins private staging to the canonical runtime filesystem. Planning opens SQLite with `mode=ro&immutable=1`, requires `query_only=ON`, records unchanged sidecars, source stat/SHA/integrity and a non-target directory digest. On one filesystem it proves source-size-plus-expansion-plus-explicit-reserve capacity as before. When staging and backup retention use different filesystems, the query-only plan additionally measures the exact zstd output without persisting it, proves the conservative source-size staging envelope on the runtime filesystem and proves measured archive-size-plus-reserve publication headroom on the backup filesystem. Apply compresses into a private unnamed staged file, tests the frame and streamed decompressed size/SHA, rechecks the measured size and destination headroom, copies into a private fsynced destination temp, then publishes and independently revalidates the retained archive before raw removal. It removes only the exact source and its unchanged empty WAL/SHM sidecars, rechecks non-target entries and finalizes `verified_pending_source_removal → retained`. A failed or killed staging process cannot leave a named staging artifact; a published crash preserves a machine-resumable archive/manifest family, and an exact repeat completes or returns no-op. It never deletes or rewrites business/runtime rows.
- Global legacy backup cleanup is a separate hosted contour:
  `storage-recovery-sanitation-inventory|plan|apply`. The wrapper fixes both
  canonical roots, the deployed SHA and one exact allowlisted family. The
  runner either invokes the same lossless archive primitive for one raw SQLite
  source or removes exact superseded standard archive/manifest generations
  only after decompressed restore verification. It writes a private durable
  pre-delete audit, fsyncs each change, supports exact-fingerprint resume and
  proves a non-target family digest. Symlinks, live database names,
  custom/unmatched manifests, corruption and foreign/unlisted families fail
  closed. The detailed family allowlist and production acceptance contract are
  `migration/125_storage_recovery_sanitation.md`;
- Long legacy-family work uses hosted
  `storage-recovery-sanitation-submit|status`, not an SSH-owned foreground
  process. Submit requires a caller-known 64-hex job id and exact deployed SHA,
  persists one digest-bound request and starts only the installed
  `wb-core-storage-recovery-sanitation@.service` template. The template fixes
  the repository worker and both canonical roots; no shell payload is
  accepted. Status is read-only and returns durable request/state/result and
  systemd readback. Exact request drift, concurrent worker, symlink/job-path
  drift and non-terminal/mismatching sanitation audit fail closed;
- Hosted `promo-archive-gc-dry-run|apply` is the only Stage 4 full Promo
  artifact cleanup path. It keeps the existing normalized-persistence/hash and
  TTL/state guards, adds SHA/stat/inode/mtime identities, a stable fingerprint,
  exact deployed SHA, durable crash-resume audit, per-target fsync and
  candidate-run non-target digest. Current, running, unknown,
  partial/incomplete and replay-critical artifacts remain protected. The
  refresh-integrated light GC remains bounded and does not replace this exact
  operator pass;
- `POST /v1/sheet-vitrina-v1/supply/supplier-shipments/registry/compare-quote` is the protected temporary КП comparison route for the shipment registry. It accepts multipart `file` + `shipment_id`, reuses the logistics quote parser, returns grouped `КП` vs `Поставка факт` comparison rows, and must not persist the uploaded PDF as a supplier shipment or financial document.
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/sync` is the ordinary protected latest-window refresh: it fetches `offset=0`, additionally fetches bounded active `statusIDs=[1,2,3,4]` and recent historical `statusIDs=[5,6]` slices when no explicit status filter is requested, compares raw hashes/`updatedDate`/status, upserts new/changed rows, forces detail/goods refresh for up to 12 prioritized active/recent historical rows that changed, failed enrichment or have newer raw evidence, retries old critical-missing rows only when explicitly requested with `enrich=missing_critical`, exposes counters such as `forced_status_refresh_rows`, `refreshed_recent_historical_rows` and `accepted_qty_changed_rows`, and returns controlled JSON errors with sanitized upstream status/content-type/body prefix.
- `POST /v1/sheet-vitrina-v1/supply/wb-supplies/backfill` starts a protected background full-history backfill and returns `202` with `run_id`; the job walks WB list pagination by `limit/offset`, saves resumable progress after each page, keeps old rows, and records partial/blocker state on 429/timeout/non-JSON/upstream failures.
- `GET /v1/sheet-vitrina-v1/supply/wb-supplies/sync-status` returns protected JSON run progress and sync state for WB supplies incremental/backfill jobs.
- `GET /v1/sheet-vitrina-v1/supply/factory-order/template/*.xlsx` returns `200` + XLSX content type for all operator templates with Russian headers
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec-check` returns controlled JSON summary for the existing materialized 1C `FF_STOCK` source, including ready/partial/missing/error and coverage counts
- `GET /v1/sheet-vitrina-v1/supply/factory-order/stock-ff/onec.xlsx` returns `200` + XLSX with the same headers as the manual `Остатки ФФ` template when 1C coverage is ready, or truthful `422 {"error": ...}` when it is not ready
- `POST /v1/sheet-vitrina-v1/supply/factory-order/upload/inbound-*` accepts zero-quantity rows, drops them from normalized runtime payload and coverage, and still accepts a workbook that becomes an empty inbound dataset after zero-row filtering
- `GET /v1/sheet-vitrina-v1/supply/factory-order/uploaded/*` returns the exact currently stored operator workbook when the dataset is uploaded, or truthful `422 {"error": ...}` when it is absent
- `DELETE /v1/sheet-vitrina-v1/supply/factory-order/upload/*` returns a truthful deleted/absent state and is reflected back through `GET /v1/sheet-vitrina-v1/supply/factory-order/status`
- `GET /v1/sheet-vitrina-v1/supply/factory-order/recommendation.xlsx` returns either `200` + XLSX after calculation or truthful `422 {"error": ...}` before the first successful calculation
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/district/{district_key}.xlsx` returns either `200` + XLSX after regional calculation or truthful `422 {"error": ...}` before the first successful calculation or when the requested district was excluded from the latest calculation methodology; content-disposition filename is stable ASCII translit such as `wb_regional_central_fo.xlsx`
- `GET /v1/sheet-vitrina-v1/supply/wb-regional/recommendations.zip` returns either `200` + `application/zip` after regional calculation or truthful `422 {"error": ...}` before the first successful calculation/export validation. The atomic archive is named `Рекомендации_поставок_<date>_<time>_<calculation_id>.zip`; every included recommendation follows result/UI order and has a unique safe `ordinal + calculation_id + destination` folder/prefix with exactly `__01_РЕКОМЕНДАЦИЯ.xlsx` and `__02_ЗАГРУЗКА_WB.xlsx`. Operator XLSX excludes the former `Дефицит` column. WB XLSX is copied from checked-in `packages/application/resources/wb_supply_upload_template.xlsx`, preserves its sole `Sheet1` and `Баркод / Количество` headers, keeps exact nomenclature barcode text (including leading zeros), merges duplicate barcodes and reconciles positive integer totals. Missing/invalid/ambiguous barcode evidence, multiple nomenclature matches, negative/fractional quantity, name collision or workbook corruption returns one controlled `422` before any partial ZIP body is written
- `POST /v1/sheet-vitrina-v1/supply/wb-regional/planning-options` returns `200` with status `ready`, `blocked`, `empty`, `no_last_calculation`, `no_options` or `upstream_error`; missing barcode must be a controlled blocker before any WB call. Manager eligibility is fail-closed: incomplete catalog/acceptance evidence cannot be promoted by a name fallback, while non-critical tariff evidence failures stay warnings. For `package_type=box`, coefficients retain only official box type IDs `1/2`; a date is available only for `allowUnload=true` and coefficient `0/1`, and calendar-day counts are unique and chronological.
- `POST /v1/sheet-vitrina-v1/refresh` returns JSON with either success shape including `server_context` or truthful `422 {"error": ...}`
  - refresh completion must separate `ready snapshot persisted` from semantic source health via explicit semantic fields
  - after ready snapshot persistence and promo normalized archive sync, refresh runs bounded `promo_refresh_light_gc_v1`; it scans only promo artifact roots, protects the current collector run and replay-critical archive files, and surfaces `refresh_diagnostics.promo_artifact_gc` plus operator log summary. GC warnings stay warnings and must not convert a successful data refresh into an error.
- `POST /v1/sheet-vitrina-v1/load` is archived and must return blocked/archived behavior; `GET /v1/sheet-vitrina-v1/job` remains a current operator log route for refresh/supply jobs

If the task changes operator upload/calculate write paths inside this contour, live closure additionally requires one controlled end-to-end HTTP scenario on the hosted runtime:
- download the relevant operator templates;
- upload bounded test data through the published write routes, including mixed positive/zero inbound rows and zero-only inbound files when the changed contract touches inbound acceptance;
- verify current uploaded file download/delete lifecycle if the task changes upload state handling;
- run the server-side calculation or equivalent write action;
- verify the published result surface (`status`, operator HTML, downloadable XLSX, summary JSON), including truthful `row_count=0` for accepted empty inbound datasets, without inventing sheet/GAS steps that are outside the actual change scope.

Timeout, non-JSON body, wrong content type, `404`, stale HTML error surface or missing operator route tokens are treated as stale deploy/publish symptoms.

If the task introduces or changes temporal closed-day retry behavior for `sheet_vitrina_v1`, live closure additionally requires:
- verify the repo-owned retry runner `apps/sheet_vitrina_v1_temporal_closure_retry_live.py` on the hosted target;
- verify the repo-owned timer/service artifacts are installed on host as `wb-core-sheet-vitrina-closure-retry.service` / `.timer`;
- verify at least one affected `as_of_date` where a strict closed-day-capable source either transitions to `success` after retry or stays in a truthful retry/exhausted/blocker state without fake closed values in the visible slot.

The current active public probe target is `https://api.selleros.pro`. Live/public closure for website/operator tasks must verify the HTTPS production domain routes, including `GET /sheet-vitrina-v1/vitrina`, authorized `GET /sheet-vitrina-v1/instructions`, `GET /sheet-vitrina-v1/operator`, `GET /v1/sheet-vitrina-v1/status`, `GET /v1/sheet-vitrina-v1/web-vitrina`, `GET /v1/sheet-vitrina-v1/web-vitrina?surface=page_composition`, `GET /v1/sheet-vitrina-v1/warehouses`, and one `GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}`. `SELLEROS_HTTP_ALLOW_INSECURE_FALLBACK=1` remains a diagnostic-only legacy TLS escape hatch for historical checks and is not part of the active EU target closure.

Live/public verify that creates temporary runtime users must prefer temp/local runtime state. If a hosted verify must create a live runtime user, it must use an unmistakable service/test prefix or marker, run cleanup in a finally-style path, and verify that the default admin users list does not expose those rows. Archived/inactive service rows such as `codex_live_*`, `codex_debug_*`, `smoke_*` or `test_*` are not user-facing users and must be hidden by the default users API/UI even when cleanup cannot hard-delete an historical row; any bounded live cleanup for those prefixes must not touch env principals or real manual users.

## Finance generation filesystem

The active hosted target owns the Finance generation-filesystem contract:
exact `/opt/wb-core-runtime/state/generations` mountpoint, ext4 UUID/label,
required `rw,noatime,nodev,nosuid,noexec` options and separation from the
runtime filesystem. Every hosted Finance command receives and validates this
contract before plan or mutation. Candidate, shadow, cutover and rollback
revalidate the same mount identity/device at their persisted boundaries, and
capacity is measured on that filesystem rather than the root runtime path.
The deployed service unit has `RequiresMountsFor` plus
`ConditionPathIsMountPoint`, so a missing, replaced, wrongly mounted or
read-only generation filesystem stays fail closed across restart/deploy
instead of silently writing generations onto root.

The authenticated `finance-ui-flow` is phase-aware but not permissive. It
accepts only the implicit canonical monolith, one exact selected split, or an
explicit selected rollback monolith whose raw and operational paths are the
same manifest-bound legacy file.
Selected split binds the manifest, raw and operational stores to the same
generation and exact schema revisions; requires the latest outbox, raw
acknowledgement and operational cursor to match with zero pending records,
consumer lag, cursor mismatch, shadow mismatch or actionable dead letters;
and proves that `monolith` remains the rollback generation. Shadow,
mixed-generation, lagged or unhealthy storage fails before report/XLSX
evidence can pass.

The hosted `finance-canonical-dry-run` is also selected-split aware. It pins
one manifest, opens both persistent generations with SQLite `mode=ro`, creates
only a connection-local raw compatibility view and restores
`PRAGMA query_only=ON` before planning. The reviewed apply writes only
operational Finance projections and proves the raw generation unchanged
through its non-target digest. Canonical read actions have a one-hour remote
process bound and apply has a two-hour bound. Each call first creates or resumes
one request-digest-bound operation id in the server-owned
`.finance-canonical-operations` directory, starts the bounded worker at most
once with stdout/stderr redirected to durable exact-operation evidence, and
then uses short SSH status reads. The request and remote start are also bound
to the exact deployed `.wb-core-runtime-sha`. A connection reset therefore
cannot lose the terminal result or justify a duplicate planner/apply;
`--operation-id` resumes only an exact matching request/deploy and mismatches
fail closed.

## Human-Only Boundary

One minimal human-only step remains allowed only when repo-owned contract still cannot execute due missing access:
- grant deploy access for `wb-core-eu-root` / `89.191.226.88`

Without that step a live/public task stays `live-complete = blocked`; reporting only `repo-complete` is insufficient. For GAS/sheet-only scope the blocker is tracked as `sheet-complete = blocked`.
The blocker must name the concrete missing access/value and must not be phrased as unspecified operational uncertainty.

For server/operator-only changes that do not touch archived bound Apps Script guard code, `Sheet verify result` must stay `not in scope` rather than being filled with fake closure activity.

## SKU management runtime/write contract

The authenticated public route family is exact GET `/v1/sheet-vitrina-v1/sku-management`, narrow per-SKU GET `/v1/sheet-vitrina-v1/sku-management/sku/{nm_id}`, and guarded GET/POST prefix `/v1/sheet-vitrina-v1/sku-management/`; app session and `sku_management` section authorization remain authoritative. The narrow per-SKU route is a distinct quick mutation read model: one exact price read, one campaign/placement index read, local temporal projections and filtered history, with sanitized phase timings/call counts. It must not invoke the full SKU table, stock/forecast/supply collection, Ads fullstats/minimum/recommendation enrichment or all-active price reads. Dedicated price and exact-placement bid blocks are part of normal runtime construction and require no post-deploy feature-flag enablement. `WB_PRICES_WRITE_ENABLED` and `SHEET_VITRINA_ADS_WRITE_ENABLED` continue to gate their legacy standalone tabs but do not disable this separately authorized workflow. Its sufficient mandatory gates are one stored target/preview, explicit confirmation, stale/min/quarantine validation, backend-only WB call, audit and exact readback. Price upload status and exact tuple readback share one bounded early-cadence/deadline loop; bid readback targets only exact `nm_id + advert_id + placement`; optional public buyer-price enrichment never blocks already confirmed seller-price success. Deploy itself performs no WB mutation.

## Warehouse business projection deployment

Migration 126 deploys additive SQLite tables, nullable functional-version
columns, triggers, status route and bounded worker code. Schema initialization
does not rewrite historical business rows and does not run an external source
refresh. Projection publication remains source-triggered and transactionally
bounded; failed candidate retains last-good state.

Historical production warehouse/capital correction is explicitly not part of
deploy or UI acceptance. It requires a separate `scope:production-mutation`
task with query-only diagnostic manifest, human gate, exact
backup/reversibility and reconciliation. Production verification for this
LOOP is read-only schema/status/readback plus isolated UI revision-flow
evidence.
