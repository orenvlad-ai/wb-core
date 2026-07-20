# Migration 107 — Finance retro cost and Partner Report

## Scope

This migration changes only bounded derived Finance/Partner Report state in `registry_upload_runtime.sqlite3`:

- adds immutable `wb_finance_retro_cost_map` and `wb_finance_projection_audit`;
- recalculates Finance derived aggregate/coverage/reconciliation rows from week `2026-04-27–2026-05-03` through the latest fully closed week;
- adds Partner Report settings/finalized/audit tables;
- does not mutate `wb_finance_weekly_raw_rows`, canonical functional warehouse sources, supplier/CNY/FF/WB supply ledgers, financial documents, accepted ads snapshots or archived Google Sheets/GAS.

Repository implementation and production-data execution are separate gates. Missing WebCore Data MCP, local production DB or browser credentials does not block code/PR. Production apply remains fail closed until the deployed canonical runner produces a clean read-only plan and the required human approval reference exists.

## Repository verification

```bash
python3 apps/wb_finance_weekly_smoke.py
python3 apps/wb_finance_weekly_cost_cutover_smoke.py
python3 apps/wb_finance_weekly_business_approved_backfill_smoke.py
python3 apps/wb_finance_weekly_stale_cost_safety_smoke.py
python3 apps/wb_finance_weekly_browser_smoke.py
python3 apps/partner_report_smoke.py
python3 apps/partner_report_browser_smoke.py
python3 apps/registry_upload_http_entrypoint_auth_smoke.py
python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py
```

`apps/partner_report_smoke.py` contains the agreed Excel-reference fixture and a second confidential SKU. It verifies 476,034 revenue, 83,837 COGS, 174,797 commission, 30,904 ads, 10,000 office, 6% tax, 20% reserve, 40% partner share, 186,496 margin, 110,634.76 distributable profit and 44,253.904 payout without assuming invested capital from the screenshot. It also proves exact finalization idempotency and persisted loss carry into the immediately following finalized period while period gaps fail closed.

## Production phase 1 — read-only preflight

After the exact merged SHA is deployed, run only the hosted repo-owned path:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  finance-retro-dry-run \
  --date-from 2026-04-27 \
  --output /ABSOLUTE/OUTSIDE-REPO/finance-retro-plan.json
```

The output file must stay outside the Git checkout and is written with mode `0600`. It is reviewable only if it includes:

- every candidate week and every Finance report/raw-row digest;
- union of sale/return `nmId`, row count without `nmId`, and exact affected weeks;
- immutable retro row for every May/June sale/return SKU, with canonical source date/full source row/hash/selection method/formula version;
- old and expected COGS, profit, final margin and gross-unit coverage for every checked week (a symmetric sale/return pair cannot hide missing cost);
- Finance, cost and ads manifests (missing ads is diagnostic for Partner Report and is not silently converted to zero);
- explicit blockers, source digest, target/non-target digest and exact fingerprint;
- backup/apply/reconciliation plans.

Block apply for missing/ambiguous SKU identity, missing/non-positive canonical cost, missing operation date at a temporal boundary, immutable-map conflict or incomplete Finance cost coverage. `confirmed_share_pct=0` alone is not a cost gap.

Identity blockers apply only to non-zero sale/return quantity movements. Zero-quantity Finance rows retain count/digest provenance and account-level allocation evidence, but do not require a unit cost. The runner scans full production scope with bounded per-week raw memory so the dry-run remains viable for large historical Finance datasets.

Record the plan file SHA-256, exact plan fingerprint, week/SKU counts, blockers and before/expected controls in the release evidence. No production data is changed in this phase.

## Human gate and apply

Apply is allowed only after the dry-run is reviewed through the applicable production approval process. The approval reference must identify that review and must not contain secrets.

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  finance-retro-apply \
  --date-from 2026-04-27 \
  --plan-file /ABSOLUTE/OUTSIDE-REPO/finance-retro-plan.json \
  --fingerprint 'sha256:EXACT_REVIEWED_FINGERPRINT' \
  --approval-reference 'APPROVED_CHANGE_REFERENCE'
```

The hosted wrapper validates active production identity, canonical runtime/env paths and reviewed plan scope before invoking the deployed runner. The runner re-plans sources, requires the exact fingerprint, checks free space, creates an online SQLite backup in the bounded Finance backup directory, verifies `integrity_check=ok`, SHA-256 and mode `0600`, then uses one `BEGIN IMMEDIATE` transaction. Drift/error rolls back all map/aggregate/audit writes. There is no partial/force mode.

## Readback and idempotency

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  finance-retro-readback \
  --date-from 2026-04-27
```

Required evidence:

- zero pending target weeks and zero blockers;
- 100% cost coverage for every selected week with real sale/return movements;
- COGS/profit/final margin populated and reconciled;
- non-target digest preserved;
- audit row links exact fingerprint/scope/result and the human approval reference;
- backup path/hash/integrity/mode recorded outside Git;
- repeat `finance-retro-apply` with the same plan/fingerprint/scope returns `already_current`, `runtime_mutation=false`, no second backup and no second audit row.

## UI and artifact acceptance

After deploy/data readback, run authenticated production UI Flow in a fresh isolated Chromium context. It must be read-only or use a disposable contour that is cleaned and proves non-target preservation; it must not leave a real partner finalized report.

Accept only when Finance weeks/COGS/profit/margin, clean headers, no-marketing metric, amount/percentage arrows and no-double-count semantics render correctly on desktop/narrow, and Partner Report preview/source blockers/ROI/layout work. Render the generated main XLSX to PDF/PNG and compare it to the supplied desktop reference. Inspect the preview ZIP and prove weekly Finance file count, selected-SKU-only content, ads/COGS/common-expense reconciliation, no hidden sheets/macros/external links/other-SKU data and stable finalized source digests.

LOOP acceptance remains fail closed until the active PR has production UI evidence and the exact `/wb-core loop accept-ui <PR>` acknowledgement reaches terminal `release:production`.
