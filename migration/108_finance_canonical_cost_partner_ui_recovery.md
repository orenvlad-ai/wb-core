# Migration 108 — canonical Finance cost and UI-first Partner Report recovery

## Scope and immutable boundaries

This recovery changes bounded derived Finance/Partner state only. It never mutates raw Finance rows, accepted ads snapshots, canonical warehouse/cost rows, supplier/CNY/FF/WB supply ledgers, documents or archived GAS/Sheets.

Repository delivery and production-data mutation are separate gates. Missing production credentials/session does not block implementation/PR/deploy. Production apply remains forbidden until the deployed all-history dry-run has a clean newly reviewed fingerprint and explicit human approval.

## Repository verification

```bash
python3 apps/wb_finance_weekly_smoke.py
python3 apps/wb_finance_weekly_cost_cutover_smoke.py
python3 apps/wb_finance_weekly_business_approved_backfill_smoke.py
python3 apps/wb_finance_weekly_canonical_scale_smoke.py
python3 apps/wb_finance_weekly_stale_cost_safety_smoke.py
python3 apps/wb_finance_weekly_browser_smoke.py
python3 apps/partner_report_smoke.py
python3 apps/partner_report_browser_smoke.py
python3 apps/registry_upload_http_entrypoint_auth_smoke.py
python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py
python3 apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
```

The Partner smoke includes the agreed Excel reference fixture and a second confidential SKU. It verifies the UI/XLSX values, no other-SKU content, no ZIP/raw exports, root/nested ads envelopes, missing-source blockers, stale aggregate detection and indexed performance against a measured full-decode baseline with 295,919 unrelated raw rows.

The Finance scale smoke independently builds the complete canonical dry-run for 295,919 sale rows across 26 weeks, including roughly 148k operations of an SKU without canonical cost. Raw/non-target identities use deterministic streaming JSON-array digests, expected target evidence contains only persisted/read-back fields, and missing-cost rows collapse by week/SKU/operation-date/reason while retaining operation and sale/return quantities. The regression fails on manifest/quantity loss, duplicated gap evidence, a 60-second local runtime, or 512 MiB peak RSS.

## Phase 1 — production all-history dry-run

After the exact merged SHA is deployed, run only:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  finance-canonical-dry-run \
  --output /ABSOLUTE/OUTSIDE-REPO/finance-canonical-plan.json
```

The output is mode `0600`, outside Git, and covers the full loaded Finance date range. Review requires:

- week/raw-row/nmId scope and Finance manifest;
- canonical 01.07 cost manifest and post-01.07 exact-date manifest;
- week × nmId × operation-date matrix, sale/return quantities, source date/quality/unit cost and signed COGS;
- exact missing-cost/blocker list, with no average/legacy/zero fallback;
- agent remuneration/acquiring/combined-control reconciliation;
- paid acceptance/transit cost-layer lineage and a chronological all-history cap that cannot be reused in another week;
- all-history stale-derived detection for canonical cost, classifier and supply-layer profit changes;
- before/after COGS, profit, margin and before-COGS profit for every week;
- COGS/profit/margin deltas, every raw/derived/source input affecting profit, and explicit explanation when profit change is not the negative COGS change;
- Finance/cost/ads/source/target/non-target digests, write set, backup/recovery plan and exact fingerprint;
- confirmation that Finance apply does not write ads or canonical cost, and exact values from 01.07 remain non-target.

The review must call out `29.06–05.07`, the last two closed weeks and early/January weeks. The former migration-107 plan/fingerprint is invalid even if some values happen to match.

## Human gate and apply

Stop after dry-run. Apply requires a new explicit approval tied to the complete fingerprint:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  finance-canonical-apply \
  --plan-file /ABSOLUTE/OUTSIDE-REPO/finance-canonical-plan.json \
  --fingerprint 'sha256:EXACT_NEW_REVIEWED_FINGERPRINT' \
  --approval-reference 'APPROVED_CHANGE_REFERENCE'
```

The hosted wrapper accepts only schema `wb_finance_canonical_cost_backfill_v2`, dry-run plan and `apply_allowed=true`. The application runner re-plans exact sources, checks fingerprint, free space and human reference, creates a coherent `0600` SQLite backup with SHA-256 and `integrity_check=ok`, and uses one `BEGIN IMMEDIATE` transaction. Drift, blockers, target mismatch or non-target change roll back everything. There is no partial/force path.

The write set is limited to weekly aggregate/coverage/reconciliation, per-SKU aggregate, sync status and audit. It writes zero retro-map rows. Exact repeated apply uses the prior audit plus the post-apply fingerprint and returns a no-op without a second backup only while raw/ads/cost/target state is unchanged; later drift requires a new dry-run and approval.

## Readback and production acceptance

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py \
  finance-canonical-readback
```

Readback requires zero blockers and zero COGS/profit/margin/before-COGS deltas across all loaded weeks.

Then run authenticated `finance-ui-flow` in a fresh isolated Chromium context. The flow may calculate preview and download the source-digest-bound preview XLSX, but never saves settings, creates finalized reports or mutates business data. It verifies Finance rows/microcells, Partner UI preview/blockers, desktop/390 px layout and downloaded XLSX. Render the XLSX to PDF/PNG and compare it with the supplied light desktop reference before LOOP acceptance.

LOOP acceptance remains fail closed until post-apply reconciliation, production UI/Excel evidence and the exact active PR acknowledgement reach terminal `release:production`.
