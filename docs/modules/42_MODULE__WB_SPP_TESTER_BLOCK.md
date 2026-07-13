---
title: "Модуль: wb_spp_tester_block"
doc_id: "WB-CORE-MODULE-42-WB-SPP-TESTER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать production MVP инструмента `Цены -> Проверка СПП` для безопасного live измерения `SPP-прокси` по пользовательскому диапазону discounted price."
scope: "Server-owned one-nmID SPP tester inside unified operator shell: baseline capture, safe-slow plan/start/status/restore endpoints, guarded WB Prices live writes, anonymous public buyer-price polling, threshold detection over high-confidence points, cross-process runtime lock/state/audit, stale/orphan reconciliation, bounded history, one persistent daily schedule with a repo-owned due runner/systemd timer, staged baseline restore and fake-upstream smokes. The block does not change promo semantics, promo denominator or current prices table behavior."
source_basis:
  - "packages/contracts/wb_spp_tester.py"
  - "packages/application/wb_spp_tester.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/adapters/spp_proxy_block.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_modules:
  - "41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "35_MODULE__SPP_PROXY_BLOCK.md"
related_tables: []
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/baseline?nmID=..."
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/plan"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/start"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/status"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/restore"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/history?limit=...&cursor=..."
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/history/{job_id}"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/schedule"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/schedule"
related_runners:
  - "apps/wb_spp_tester_smoke.py"
  - "apps/wb_spp_tester_browser_smoke.py"
  - "apps/wb_spp_tester_schedule_tick.py"
related_docs:
  - "docs/modules/41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "docs/modules/35_MODULE__SPP_PROXY_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Production SPP tester now reconciles interrupted runtime jobs through fresh live restore proof, terminalizes emergency restore, exposes bounded history over existing job files, and supports one consented persistent daily `Автопроверка` schedule through the same lock/write/restore path and repo-owned systemd due ticker."
---

# 1. Идентификатор и статус

- `module_id`: `wb_spp_tester_block`
- `family`: `sheet_vitrina_v1/operator/official-api/prices/spp-test`
- `status_main`: active production MVP
- `status_write_path`: guarded backend-only; disabled unless both `WB_SPP_TEST_ENABLED=true` and `WB_PRICES_WRITE_ENABLED=true`

# 2. Product Semantics

`Проверка СПП` is an operator tool under `Цены`, not a promo feature.

Operator chooses exactly one SKU/nmID and manually enters discounted-price range. The tool:
- captures baseline WB seller price/discount/discountedPrice;
- captures anonymous public buyer price and current `SPP-прокси`;
- builds a safe-slow measurement plan from min/mid/max plus refinement budget;
- temporarily changes seller price through WB Prices and Discounts API;
- measures actual WB discounted price after readback and public buyer price after stable proof;
- detects adjacent material jumps in `SPP-прокси`;
- restores baseline and records proof.

The formula is unchanged:

`SPP-прокси = (seller discounted price - public buyer price) / seller discounted price`

# 3. Runtime State

Runtime state is bounded under:

- `sheet_vitrina_v1_prices/spp_tests/current_job.json`
- `sheet_vitrina_v1_prices/spp_tests/jobs/{job_id}.json`
- `sheet_vitrina_v1_prices/spp_tests/audit.jsonl`
- `sheet_vitrina_v1_prices/spp_tests/schedule.json`
- `sheet_vitrina_v1_prices/spp_tests/execution.lock`

`current_job.json` stores active/last job id, status, heartbeat, runner identity and TTL diagnostics. `jobs/*.json` and `audit.jsonl` remain the canonical history/evidence; schedule support does not add a DB or a parallel job journal. `schedule.json` stores the single daily business schedule and at-most-once claim state. `execution.lock` is an OS-level cross-process lock shared by manual threads, the systemd runner and emergency restore.

An active or unrestored job blocks another start. TTL alone never unlocks or declares a restore. A live lock holder is reported as live; an active status without a lock holder is stale/orphaned and is reconciled by a fresh WB price/discount/discountedPrice readback, quarantine read and public buyer-price/SPP capture. Exact proof moves it to `interrupted_restored`; a mismatch, quarantine, unavailable proof or readback error moves it to `manual_restore_required`. This also closes jobs interrupted by deploy/restart while a daemon worker was sleeping. A successful emergency restore writes a terminal status instead of leaving `current_job.json` in `restoring`.

The browser is not source of truth. It renders server baseline, plan, job, measurements, thresholds and restore proof.

# 4. Safety Rules

Start requires:
- `WB_SPP_TEST_ENABLED=true`;
- `WB_PRICES_WRITE_ENABLED=true`;
- one positive `nmID`;
- `editableSizePrice=false`;
- quarantine absent at baseline;
- nmID is still present in active server-owned nomenclature;
- public buyer-price/SPP baseline is available and is not marked 429/timeout/stale;
- explicit live-change confirmation;
- `restore_baseline=true`.

Manual and scheduled starts acquire the same execution lock before baseline capture. The scheduled path additionally requires stored consent to future temporary live price changes, always passes `restore_baseline=true`, and captures a fresh baseline only after the due claim and lock acquisition. Saving or enabling a schedule never calls `start`.

All live writes are server-owned. Tests/smokes use fake upstream sources and must not call live `POST /api/v2/upload/task`.

# 5. Algorithm

Inputs:
- `nmID`;
- `range_min_discounted`;
- `range_max_discounted`;
- `precision_rub`, default `2`;
- `max_measurements`, default `8`, allowed `3..30`;
- mode `safe_slow` only.

Measurement conversion keeps current discount and changes only integer `price`. After upload, the runner uses WB readback `discountedPrice`, not target price, as actual seller discounted price.

Initial points are min/mid/max. Threshold detection uses high-confidence points only:
- delta `< 0.005` = noise;
- delta `>= 0.015` = material;
- delta `>= 0.03` = strong.

MVP refines one strongest material interval by midpoint until bracket width is within precision or measurement budget is exhausted.

# 6. Confidence And Backoff

A point is high confidence only when:
- upload task succeeds;
- WB readback matches expected discounted price;
- public buyer price reaches stable proof;
- quarantine is absent;
- there is no unresolved 429/timeout/stale evidence.

Stale public price evidence is kept in the table/journal but excluded from threshold detection.

WB Prices 429 stores endpoint/status/safe headers/body summary/retry hint in audit, respects `Retry-After` when present, otherwise uses a minimum cooldown, probes read-only after cooldown and stops probing after repeated rate limits so restore can run.

# 7. Restore

Baseline restore is mandatory. Direct final restore is allowed for small moves. Large downward discounted moves use bridge steps before final baseline.

Bridge steps require:
- upload success;
- WB readback;
- quarantine absent.

Final proof requires:
- WB price equals baseline price;
- WB discount equals baseline discount;
- WB discountedPrice equals baseline discountedPrice;
- quarantine absent;
- public buyer price and `SPP-прокси` captured.

If proof fails or quarantine appears, status becomes `manual_restore_required` and no further probing is performed.

Exact restore proof uses equality at kopeck precision for `discountedPrice`, exact integer `price` and `discount`, absent quarantine, plus captured public buyer price and final `SPP-прокси`. A prior proof is not trusted blindly by an emergency restore or orphan reconciliation: current live evidence is read again.

# 8. History

`GET .../history` returns newest-first summaries with bounded `limit` (`1..50`) and an opaque keyset cursor. It scans the existing canonical `jobs/*.json`, so pre-feature jobs remain visible. Legacy rows without `trigger_source` return `null`/unknown rather than being labelled manual. `GET .../history/{job_id}` accepts only a bounded safe id and rejects traversal; response sanitization removes secret-like keys, headers and internal paths.

New jobs store `trigger_source=manual|schedule`. Scheduled skips are terminal history rows with a reason and no price mutation; they do not replace an actually active `current_job` pointer.

# 9. Автопроверка And Scheduler

The UI exposes one fixed `Ежедневно` schedule:
- enabled flag;
- one SKU/nmID;
- discounted-price min/max, precision and max measurements;
- local time;
- fixed `Asia/Yekaterinburg` timezone labelled `Оренбург`;
- next run, last automatic run/status;
- explicit future-live-change consent.

The repo-owned `apps/wb_spp_tester_schedule_tick.py` reads the persisted schedule and is invoked once per minute by non-persistent `wb-core-spp-tester-schedule-tick.timer`. Business time stays in `schedule.json`; systemd is only a due ticker. The oneshot allows up to three hours for a bounded safe-slow probe and restore. A due business date is claimed atomically before execution, so restart/deploy cannot duplicate it. The runner records due/start/skip/finish/restore evidence in the existing audit/jobs contour.

Late-run policy is bounded: a due run may start at most 15 minutes late. A later tick records `missed_late_window`, advances to the next business date and does not mutate prices. Active/unrestored jobs, lock contention, disabled write guards, quarantine, `editableSizePrice=true` and incomplete baseline are visible scheduled skips. There is no arbitrary catch-up after a long restart.

# 10. UI

The upper block continues to show the current/last job. Above manual parameters, `Автопроверка` renders persistent schedule controls. Below the current job, `История проверок` renders newest-first expandable rows and lazily loads safe detail per job. Compact rows show time, SKU/nmID, manual/automatic/unknown source, status and duration; details expose range, baseline, measurements, thresholds, warnings/errors, restore proof and lifecycle diagnostics.

Manual UI remains intentionally small:
- SKU selector from current prices/active registry rows;
- baseline card;
- test input card;
- plan/status area;
- measurements table;
- threshold table.

Danger states use short explicit labels: `429`, `stale`, `карантин`, `нужен restore`.

# 11. Verification

Targeted smokes:
- `python3 apps/wb_spp_tester_smoke.py`
- `python3 apps/wb_spp_tester_browser_smoke.py`

These cover legacy history compatibility, cursor pagination/detail traversal safety, interrupted/stale reconciliation, unrestored blocking, cross-process contention, schedule save/enable/disable, `next_run_at`, no immediate start, at-most-once/restart, late skip, safety skip, mandatory restore, 429/timeout/stale/quarantine, UI history expansion and deploy/systemd wiring.

Regression smokes:
- `python3 apps/wb_prices_management_smoke.py`
- `python3 apps/wb_prices_management_browser_smoke.py`
- `python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`

# 12. Out Of Scope

- Multiple SKU/nmID runs.
- Cadences other than daily.
- Multiple independent schedules.
- Promo denominator changes.
- Promo column fixes.
- Redesign of the whole `Цены` table.
- WB Club discount writes.
- Size-level price editing.
