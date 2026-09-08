# WBC0054 — B2-00 / B2-01 check preparation

Date: 2026-09-08. Approved scope: boundary delta, read-only baseline and selected
checks only. B2-02 and later are not implemented. No business entrypoint,
publication behavior, source data, timer, workflow or selector policy changes.

## Boundary and evidence

The approved B2 v0.4 plan was checked at SHA256
`d1f899489ea7a6f1233e49bcc67cecd639064662103d6ccae1d8dd56bd2b091a`.
Prior source/writer matrices were reused, then compared to main `6e9cc950`
(#1243). Since the plan's `a1041f28` baseline, the relevant delta is confined to
the dated trading pool and period-reader evidence handling (#1242/#1243).
Catalog membership, daily pool, structured metadata and historical isolation
remain unchanged by this PR. Further concurrent main changes require a fresh
integration check before ready-for-review.

Read-only production observation used the active target contract and registry,
SQLite `mode=ro`, `PRAGMA query_only=ON`, bounded queries and a manifest recheck.
Operational and FBS book stores were separately identified; the book is not a
logical member of StoreRegistry. At 13:12–13:13 UTC the book was active with
effective date 2026-09-08. Runtime marker transitioned from #1242 to #1243 during
the observation. Deployment/readback ownership remains with task 0066; this
observation is not a replacement release receipt.

Five existing successful background warehouse cycles lasted 364–440 seconds.
These are **full background cycle durations, not page-opening latency** or a
percentile benchmark. The latest service reported 286 CPU seconds and 4.87 GiB
peak memory; no per-phase resource attribution follows from those counters.
At the observation cut, all 165 warehouse queue rows had warehouse `complete`,
but most did not have all downstream completion fields. Thus queue complete is
not end-to-end completion. The latest ready date was the previous closed day.

No production save/refresh was triggered. Existing logs did not provide a
bounded status/save latency sample. Lock hold/wait distributions, per-phase
CPU/RAM and I/O remain unmeasured. Instrument the first changed boundary in
B2-02/05/07, without synthetic business documents or a week-long prerequisite.
Exact private locations, revision IDs and timestamped observations remain in
the execution report outside the repository; no production dumps are included.

## What the check map protects

| Selected boundary | Subject checks |
|---|---|
| Central runtime / HTTP / warehouse runner / journal / locks | Existing refresh/read/ready/group, job/journal, functional/targeted replay checks; real two-process lock fixture |
| Central ready save and historical importer | Historical completion smoke actually calls `materialize_historical_ready_snapshots` |
| Calculation parameters, FBS runtime/apply and source adapters | Existing runtime, apply, snapshot-cost, shared-cost and inventory-presentation smokes |
| Catalog, dated economics, daily pool and history | Catalog-economics, daily-pool, management-history checks retained |
| Supplier, financial, CNY and fulfillment sources | Existing HTTP, confirmation, financial-document, invoice-revision, CNY and fulfillment checks |
| FF sources / request workflows / warehouse projections | Inventory, overhead, business projection, pool-document, dense-FBS and unified acceptance checks |
| Direct ready writers / recovery | Historical/material recovery, Finance recovery, canonical, Proxy V4, management history, cost carry-forward, promo/SPP, disabled legacy and candidate-only checks |

The independent expectations in `ci/select_checks_smoke.py` exercise every one
of 51 selected production paths individually, plus old/new rename pairs. A real
disposable Git rename checks the name-status parser. Two unrelated changes
individually and together do not select the B2 block. The process helper has an
explicit consumer check; arbitrary future renamed boundaries must still update
their map or carry a sibling smoke. This is not automatic transitive dependency
analysis.

`ci/fixture_process.py` supplies named bounded barriers and process termination
cleanup only. `apps/warehouse_process_fixture_smoke.py` tests the actual existing
job/write locks in a temporary directory: cross-process exclusion, separate
lock domains, writer reentrancy and reacquisition after release or termination.
It does not claim HTTP admission, durable status, source intent or ready CAS is
already fixed.

## Four legacy fixture repairs

| Baseline failure | Bounded test correction / current contract |
|---|---|
| Refresh/read stock source failed unknown/incomplete nomenclature scope | Seed the fixture's full active catalog and return all requested identities using the StocksSuccess contract. No stock guard is bypassed. |
| Supplier HTTP expected routine FF acceptance via factual dates | Assert rejection and unchanged source/receipts. Preserve accepted-history/frozen-invoice checks with an explicit pre-cutover read fixture. Existing unified form smoke proves factual date, one receipt effect, history preservation and replay. |
| Supplier confirmation expected a combined shipment/FF date mutation | Exercise shipment-date preview/confirm, real targeted transaction/readback, idempotence and stale rejection on an active empty temporary functional warehouse. Missing payment is not manufactured into paid capital. FF remains unified-document-only. |
| Carry-forward imported the retired historical-missing-repair script | Remove only that retired script's test/import (script removed in #1194). Current carry-forward source/target/formula CAS, one-submit, receipt and closed-period assertions are unchanged. |

Supplier fixtures also needed current functional activation and a scheduling
barrier, replacing the retired monolithic `before_transaction` hook. Production
guards remain intact. See the current contracts in
[module 34](../modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md) and
[module 43](../modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md).

## Trusted-base bootstrap and stop

The old map selected only `process` with `pip=[]` for CI plus the four legacy
test paths. The existing launcher smoke imports openpyxl indirectly, including
its isolated `-I` subprocess; a user-site installation is insufficient.
The new substantive `apps/warehouse_process_fixture_smoke.py` legitimately
selects the **old** warehouse group and its pinned openpyxl dependency. The new
map separately declares openpyxl for future process-only changes. No fictitious
business-code touch, workflow bypass or full-suite-on-every-PR was introduced.

Exact old-base plan `6e9cc950` → code head `3ee27676`: process + warehouse,
openpyxl 3.1.5 only, 11 commands including compilation. It passed through the
unchanged trusted harness in a clean venv in 28.32 seconds. All four repaired
smokes, importer, unified FF form, process and selector also passed individually.

Because this PR includes `apps/*_smoke.py`, the existing release classification
is **live_runtime**, not repo_only. Keep the PR DRAFT until task 0066 releases
ownership, the final main delta is integrated, review is resolved and the
coordinator authorizes ready-for-review. A new exact-base/head Gate must then
run normally. Green tests alone neither authorize production operations nor
close B2-02 onward. Rollback is an ordinary code revert, no data rollback.
