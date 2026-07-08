---
title: "Модуль: wb_spp_tester_block"
doc_id: "WB-CORE-MODULE-42-WB-SPP-TESTER-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать production MVP инструмента `Цены -> Проверка СПП` для безопасного live измерения `SPP-прокси` по пользовательскому диапазону discounted price."
scope: "Server-owned one-nmID SPP tester inside unified operator shell: baseline capture, safe-slow plan/start/status/restore endpoints, guarded WB Prices live writes, anonymous public buyer-price polling, threshold detection over high-confidence points, runtime lock/state/audit, staged baseline restore and fake-upstream smokes. The block does not change promo semantics, promo denominator, current prices table behavior or scheduled price experiments."
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
related_runners:
  - "apps/wb_spp_tester_smoke.py"
  - "apps/wb_spp_tester_browser_smoke.py"
related_docs:
  - "docs/modules/41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "docs/modules/35_MODULE__SPP_PROXY_BLOCK.md"
  - "docs/architecture/09_official_api_secret_boundary.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Initial production MVP for `Цены -> Проверка СПП`: separate `WB_SPP_TEST_ENABLED` guard, server-owned runtime job state/audit, one active job, one nmID, user-specified range, safe-slow cadence, stale/429 handling and staged baseline restore."
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

`current_job.json` stores active/last job id, status, heartbeat and TTL. An active or unrestored job blocks another start. Status startup/read path exposes unfinished jobs; if baseline is not restored, UI must keep emergency restore visible.

The browser is not source of truth. It renders server baseline, plan, job, measurements, thresholds and restore proof.

# 4. Safety Rules

Start requires:
- `WB_SPP_TEST_ENABLED=true`;
- `WB_PRICES_WRITE_ENABLED=true`;
- one positive `nmID`;
- `editableSizePrice=false`;
- quarantine absent at baseline;
- explicit live-change confirmation;
- `restore_baseline=true`.

All live writes are server-owned. Tests/smokes use fake upstream sources and must not call live `POST /api/v2/upload/task`.

# 5. Algorithm

Inputs:
- `nmID`;
- `range_min_discounted`;
- `range_max_discounted`;
- `precision_rub`, default `2`;
- `max_measurements`, default `8`, allowed `3..12`;
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

# 8. UI

UI is intentionally small:
- SKU selector from current prices/active registry rows;
- baseline card;
- test input card;
- plan/status area;
- measurements table;
- threshold table.

Danger states use short explicit labels: `429`, `stale`, `карантин`, `нужен restore`.

# 9. Verification

Targeted smokes:
- `python3 apps/wb_spp_tester_smoke.py`
- `python3 apps/wb_spp_tester_browser_smoke.py`

Regression smokes:
- `python3 apps/wb_prices_management_smoke.py`
- `python3 apps/wb_prices_management_browser_smoke.py`
- `python3 apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py`
- `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`

# 10. Out Of Scope

- Scheduled SPP probes.
- Multiple SKU/nmID runs.
- Automatic price experiments.
- Promo denominator changes.
- Promo column fixes.
- Redesign of the whole `Цены` table.
- WB Club discount writes.
- Size-level price editing.
