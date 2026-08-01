---
title: "Модуль: wb_spp_tester_block"
doc_id: "WB-CORE-MODULE-42-WB-SPP-TESTER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать production MVP инструмента `Цены -> Проверка СПП` для безопасного live измерения персонализированной цены авторизованного покупателя и анонимного контроля по пользовательскому диапазону discounted price."
scope: "Server-owned one-nmID SPP tester inside unified operator shell: separate persistent Wildberries buyer session, exact authenticated-buyer-price capability check, authenticated buyer price as primary fact and anonymous public price as explicit control. Login/recovery and human-only noVNC escalation are exposed only in `Настройки → Источники и сессии`; the tester page is monitoring/testing only. Baseline, safe-slow plan/start/status/restore, guarded WB Prices writes, runtime locks/audit, stale reconciliation, bounded history and one persistent daily schedule remain unchanged."
source_basis:
  - "packages/contracts/wb_spp_tester.py"
  - "packages/contracts/wb_buyer_session.py"
  - "packages/application/wb_spp_tester.py"
  - "packages/application/wb_buyer_session.py"
  - "packages/adapters/wb_buyer_session.py"
  - "packages/adapters/wb_prices_management.py"
  - "packages/adapters/spp_proxy_block.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_modules:
  - "41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "35_MODULE__SPP_PROXY_BLOCK.md"
related_tables:
  - "sheet_vitrina_v1_source_health_status"
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
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/buyer-session/check"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/status"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/start"
  - "POST /v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/stop"
  - "GET /v1/sheet-vitrina-v1/prices/spp-test/buyer-session/recovery/launcher.zip"
  - "GET /v1/sheet-vitrina-v1/settings/sources-sessions"
related_runners:
  - "apps/wb_spp_tester_smoke.py"
  - "apps/wb_spp_tester_browser_smoke.py"
  - "apps/wb_spp_tester_schedule_tick.py"
  - "apps/wb_buyer_session_recovery.py"
  - "apps/wb_buyer_session_smoke.py"
related_docs:
  - "docs/modules/41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "docs/modules/35_MODULE__SPP_PROXY_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Buyer health now requires both persistent-profile authentication and the exact authenticated-buyer-price capability. Cached capability status is shown centrally; login/recovery/noVNC live only in `Настройки → Источники и сессии`, while `Цены → Проверка СПП` exposes a route-specific `Проверить` and no install/recovery UI."
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
- validates the dedicated persistent buyer session and captures an authenticated buyer price plus its payment/destination context;
- captures anonymous public buyer price independently as a control observation;
- builds a safe-slow measurement plan from min/mid/max plus refinement budget;
- temporarily changes seller price through WB Prices and Discounts API;
- measures actual WB discounted price after readback and both buyer prices after stable proof;
- detects adjacent material jumps from authenticated buyer price; the anonymous observation never substitutes for it;
- restores baseline and records proof.

Primary test formula:

`authenticated_spp_proxy = (seller discounted price - authenticated buyer price) / seller discounted price`

Control formula:

`anonymous_spp_proxy = (seller discounted price - anonymous public buyer price) / seller discounted price`

The global module 35 metric `spp_proxy` stays anonymous and unchanged. Only this tester and its schedule use the dedicated authenticated source as primary truth.

The tester's local green state requires `session.valid=true` and `capability_valid=true` from the exact authenticated buyer-price route for the configured validation nmID. A generic valid session is not sufficient. If either layer fails, the page links the operator conceptually to `Настройки → Источники и сессии`; it never launches recovery automatically or duplicates the recovery controls.

# 3. Runtime State

Runtime state is bounded under:

- `wb_buyer_session/chromium_user_data/`
- `wb_buyer_session/storage_state.json` (legacy migration input only; retained, never canonical after migration)
- `wb_buyer_session/legacy_storage_state_migration.json`
- `wb_buyer_session/fingerprint.key`
- `wb_buyer_session/account_fingerprint.json`
- `wb_buyer_session/session_probe.json`
- `wb_buyer_session/buyer_session_automation.lock`
- `wb_buyer_session/buyer_session_automation_owner.json`
- `sheet_vitrina_v1_prices/spp_tests/current_job.json`
- `sheet_vitrina_v1_prices/spp_tests/jobs/{job_id}.json`
- `sheet_vitrina_v1_prices/spp_tests/audit.jsonl`
- `sheet_vitrina_v1_prices/spp_tests/schedule.json`
- `sheet_vitrina_v1_prices/spp_tests/execution.lock`

The buyer-session files live below the hosted runtime state root, outside Git and separately from Seller Portal storage. Both the buyer state directory and `chromium_user_data` have mode `0700`; key/fingerprint/probe/recovery/lock metadata files have mode `0600`. The persistent Chromium profile is the only canonical buyer session. Session check, recovery, automatic login and authenticated price reads all use `chromium.launch_persistent_context(user_data_dir=...)`; `browser.new_context(storage_state=...)`, candidate state files, `context.storage_state(...)`, IndexedDB snapshots and validation in a browser importing a snapshot are forbidden. The old `storage_state.json` is not deleted and may be read once only to best-effort add WB cookies/localStorage to the persistent profile; the protected migration marker prevents it from becoming canonical again.

`fingerprint.key` is runtime-random. A stable `user_id`/`account_id`/`profile_id` found in a real authenticated WB response is normalized and stored only as an HMAC-SHA256 fingerprint; phone/login text is not accepted as account-id evidence, and a later different stable id is `wrong_account`. `buyer_account_context_missing` means only that fingerprint input was unavailable, not logout or probe failure: a real `/lk` browser proof plus a successful read-only authenticated WB price response keeps read-only SPP available. Fingerprint heuristics never override that browser proof.

Recovery is single-flight through a dedicated start lock plus one automation lock owned continuously by the spawned supervisor. `start_recovery()` publishes `starting` and starts exactly one supervisor; a second start, UI check, status probe or price request joins/reports the current exact `run_id` and must not launch a competing browser context. The supervisor starts headed persistent Chromium on Xvfb, checks the current profile, clicks exactly one safe saved-account continuation control, and starts x11vnc/websockify only for an actual SMS/OTP/phone/CAPTCHA/security/multiple-account challenge. After login it proves `/lk` and a read-only authenticated price response, fully closes that persistent context, starts a second Chromium process with the same `user_data_dir`, and repeats both proofs. Only that restart proof can complete recovery. Supervisor identity is a `run_id + PID + process-group + process-start` tuple; stop and terminal cleanup release the lock and terminate Chromium/Xvfb/openbox/x11vnc/websockify children.

`current_job.json` stores only an active or seller-unrestored job id, status, heartbeat, runner identity and TTL diagnostics. It is removed only after a fresh exact seller tuple plus quarantine-absent proof. `jobs/*.json` and `audit.jsonl` remain the canonical history/evidence, and status returns the newest canonical job when there is no current pointer; schedule support does not add a DB or a parallel job journal. `schedule.json` stores the single daily business schedule and at-most-once claim state. `execution.lock` is an OS-level cross-process lock shared by manual threads, the systemd runner and emergency restore.

An active or unrestored job blocks another start. TTL alone never unlocks or declares a restore. A live lock holder is reported as live; an active status without a lock holder is reconciled by a fresh WB price/discount/discountedPrice readback and quarantine read. Exact seller-tuple proof moves it to `interrupted_restored`, normalizes a transient `manual_restore_required` result to `inconclusive`, clears `current_job.json` and keeps the terminal job available as latest/history evidence. A mismatch, quarantine or seller readback error moves it to `manual_restore_required` and preserves the pointer. Authenticated/anonymous buyer captures at restore are diagnostic only: exceptions, session loss, 429 or unavailable public price cannot invalidate an exact seller restore. This also closes jobs interrupted by deploy/restart while a daemon worker was sleeping.

The browser is not source of truth. It renders server baseline, plan, job, measurements, thresholds and restore proof.

# 4. Safety Rules

Start requires:
- `WB_SPP_TEST_ENABLED=true`;
- `WB_PRICES_WRITE_ENABLED=true`;
- one positive `nmID`;
- `editableSizePrice=false`;
- quarantine absent at baseline;
- nmID is still present in active server-owned nomenclature;
- buyer session status is `valid` by current authenticated browser proof and authenticated buyer price/context is available; when both reads expose a stable account id its HMAC must match, while absent id data does not block read-only SPP;
- anonymous buyer-price control is available and is not marked 429/timeout/stale;
- explicit live-change confirmation;
- `restore_baseline=true`.

Manual and scheduled starts acquire the same execution lock before baseline capture. Their first buyer-session preflight may join or start the same bounded automatic recovery: a one-saved-account login can complete and continue the test, while SMS/phone/captcha/security/multiple-account input returns `action_required` before any seller write. The buyer-session preflight then repeats immediately before every measurement write. A mid-run session loss, account mismatch or recovery contention produces `buyer_session_lost`, stops further measurements, and proceeds directly to mandatory seller restore; there is no anonymous fallback. The scheduled path additionally requires stored consent to future temporary live price changes, always passes `restore_baseline=true`, and captures a fresh baseline only after the due claim and lock acquisition. Saving a disabled schedule is allowed without a buyer session; enabling one requires a valid session and never starts a job inline.

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

For every measurement the tester polls an authenticated and anonymous observation pair until it has two consecutive identical values for each source under compatible destination context. A present stable-account fingerprint must stay unchanged; when WB exposes no stable id, two successful reads from the same locked persistent profile with fresh authenticated-browser proof are sufficient. The result records:
- seller discounted price;
- authenticated buyer price as primary;
- anonymous buyer price as control;
- additional account discount in rubles and percent;
- authenticated and anonymous SPP proxies;
- ordinary, wallet, card and WB Club price fields when exposed;
- chosen payment context, destination context, parser/source endpoint and session fingerprint.

The authenticated network response owns the destination context for the pair. Module 42 creates an isolated anonymous card-source instance for that exact validated integer `dest`; it does not mutate the module 35 default source. An unsupported/invalid override fails the control read, and a mismatched destination blocks baseline/start instead of calculating a false account discount. The proven network primary is the authenticated browser response from `/__internal/card/cards/v4/detail`; its concrete price field is reported in `source_method` (for example `sizes.0.price.product`) rather than being hardcoded as an account-discount formula.

Initial points are min/mid/max. Threshold detection uses high-confidence points only:
- delta `< 0.005` = noise;
- delta `>= 0.015` = material;
- delta `>= 0.03` = strong.

MVP refines one strongest material interval by midpoint until bracket width is within precision or measurement budget is exhausted.

# 6. Confidence And Backoff

A point is high confidence only when:
- upload task succeeds;
- WB readback matches expected discounted price;
- authenticated and anonymous buyer prices both reach a two-read stable proof;
- any available authenticated fingerprint remains bound (or both reads carry current persistent-profile browser proof when no stable id is exposed), and both observations have compatible destination context;
- quarantine is absent;
- there is no unresolved 429/timeout/stale evidence.

Ambiguous payment context is recorded explicitly and cannot be silently normalized into a single factual price. Stale/ambiguous evidence is kept in the table/journal but excluded from threshold detection.

WB Prices 429 stores endpoint/status/safe headers/body summary/retry hint in audit, respects `Retry-After` when present, otherwise uses a minimum cooldown, probes read-only after bounded early cooldowns and stops immediately on the third repeated rate limit so restore can run instead of entering another cooldown.

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

If proof fails or quarantine appears, status becomes `manual_restore_required` and no further probing is performed.

Exact restore proof uses equality at kopeck precision for `discountedPrice`, exact integer `price` and `discount`, plus absent quarantine. Authenticated/anonymous buyer reads may be attached as diagnostics, but neither is part of seller restore success. A prior proof is not trusted blindly by an emergency restore or orphan reconciliation: current live seller evidence is read again.

Emergency restore is idempotent. If a fresh preflight already equals the baseline and quarantine is absent, no upload is made: the job is finalized, a stale/manual-only result is normalized to `inconclusive`, and the matching `current_job.json` pointer is removed. Repeated calls re-read seller truth and remain safe. A non-baseline job uses the normal bridge/final upload path and clears the pointer only after final seller proof.

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

Late-run policy is bounded: a due run may start at most 15 minutes late. A later tick records `missed_late_window`, advances to the next business date and does not mutate prices. Invalid/missing buyer session first attempts the same automatic recovery; human-required recovery records a visible `action_required` scheduled result before any price write, while a controlled terminal recovery error remains a no-write `buyer_session_invalid` skip. Active/unrestored jobs, lock contention, disabled write guards, quarantine, `editableSizePrice=true` and incomplete baseline are also visible scheduled skips. There is no arbitrary catch-up after a long restart.

# 10. UI

The upper block continues to show the current/last job while `active_job` is reserved for a truly active/unrestored pointer. Opening `Цены -> Проверка СПП` performs only the exact authenticated buyer-price capability check and never starts or resumes recovery. Generic auth without `capability_valid=true` keeps plan/schedule actions disabled and points to `Настройки → Источники и сессии`; the tester exposes one route-specific `Проверить` button and no install, noVNC or launcher control. The centralized settings card owns `checking_session`, `automatic_login`, `awaiting_human`, `validating_session`, valid, wrong-account, timeout and error recovery states. Above manual parameters, `Автопроверка` renders persistent schedule controls. Below the current job, `История проверок` renders newest-first expandable rows and lazily loads safe detail per job.

Manual UI remains intentionally small:
- SKU selector from current prices/active registry rows;
- baseline card;
- test input card;
- plan/status area;
- measurements table;
- threshold table.

Baseline and measurements display authenticated and anonymous prices side by side, additional account discount, both SPP proxies, payment method, wallet/card/club fields and the irreversible session fingerprint. API/UI/audit never return cookies, authorization headers, raw storage state, raw account identifiers or internal buyer-state paths.

Danger states use short explicit labels: `429`, `stale`, `карантин`, `нужен restore`.

# 11. Verification

Targeted smokes:
- `python3 apps/spp_proxy_source_smoke.py`
- `python3 apps/wb_buyer_session_smoke.py`
- `python3 apps/wb_spp_tester_smoke.py`
- `python3 apps/wb_spp_tester_browser_smoke.py`

These cover valid-session no-recovery, one-saved-account automatic click (including saved-account button variants), auth-token rotation for the same stable account, post-settle candidate capture, navigation-bounded independent post-login probing with controlled terminal failure, HMAC-only v1 migration, unprovable migration fail-closed, SMS `awaiting_human`, wrong-account blocking without fingerprint replacement, reload attachment without duplicate start, exact terminal launcher/tunnel lifecycle, prior-state rollback after failed final probe, full recovery process-group cleanup, and the production lock handoff through real `start_recovery()` plus the real supervisor lifecycle: a preflight holds the buyer lock, one double-started run waits, the same supervisor proceeds after release, browser recovery starts, `buyer_session_lock_busy` is never published, and locks/process state are cleared at terminal completion. They also cover manual/scheduled auto-recovery, scheduled `action_required` before writes, secret/path sanitization, per-read destination override without module 35 mutation, price/payment/destination parsing, legacy history compatibility, interrupted/stale reconciliation, session loss and mandatory restore, bounded 429/timeout/stale/quarantine, UI history and deploy/systemd wiring.

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
