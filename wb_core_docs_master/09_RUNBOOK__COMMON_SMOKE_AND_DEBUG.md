---
title: "Runbook: common smoke and debug"
doc_id: "WB-CORE-PROJECT-09-RUNBOOK"
doc_type: "runbook"
status: "active"
purpose: "Дать компактный набор частых smoke/debug команд для `wb-core` без погружения во все artifacts и module docs."
scope: "Registry upload chain, sheet-side MVP flow, live GAS checks, common failure signatures и минимальные debug entrypoints."
source_basis:
  - "README.md"
  - "apps/registry_upload_bundle_v1_smoke.py"
  - "apps/registry_upload_file_backed_service_smoke.py"
  - "apps/registry_upload_db_backed_runtime_smoke.py"
  - "apps/registry_upload_http_entrypoint_smoke.py"
  - "apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py"
  - "apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py"
  - "apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py"
source_of_truth_level: "secondary_project_pack"
related_paths:
  - "apps/"
  - "gas/sheet_vitrina_v1/"
  - "artifacts/"
update_triggers:
  - "изменение smoke runner"
  - "изменение live operator flow"
  - "изменение common failure signature"
built_from_commit: "cd67e6ef0a2355b6b2373c53d971c68611d79260"
---

# Summary

Этот runbook нужен для быстрого ответа на вопросы:
- broken ли registry upload chain;
- broken ли sheet-side MVP flow;
- broken ли live GAS wiring;
- где искать first useful signal.

# Current norm

## Core local smokes

```bash
python3 apps/registry_upload_bundle_v1_smoke.py
python3 apps/registry_upload_file_backed_service_smoke.py
python3 apps/registry_upload_db_backed_runtime_smoke.py
python3 apps/registry_upload_http_entrypoint_smoke.py
python3 apps/sheet_vitrina_v1_registry_upload_trigger_smoke.py
python3 apps/sheet_vitrina_v1_registry_seed_v3_bootstrap_smoke.py
python3 apps/sheet_vitrina_v1_mvp_end_to_end_smoke.py
git diff --check
```

## Live local runner

```bash
python3 apps/registry_upload_http_entrypoint_live.py
```

Expected routes:
- `POST /v1/registry-upload/bundle`
- `GET /v1/sheet-vitrina-v1/plan`

## Live GAS checks

```bash
clasp push
clasp run prepareRegistryUploadOperatorSheets
clasp run uploadRegistryUploadBundle
clasp run loadSheetVitrinaTable
clasp run getSheetVitrinaV1State
```

## What to verify in sheet

- `CONFIG / METRICS / FORMULAS` have expected headers and non-empty rows;
- `prepareRegistryUploadOperatorSheets` currently materializes `33 / 102 / 7`;
- `uploadRegistryUploadBundle` currently accepts and persists all `102` `metrics_v2` rows;
- `CONFIG!H:I` preserves `endpoint_url`, `last_bundle_version`, `last_status`, `last_http_status`;
- `DATA_VITRINA` gets `95` displayed metric keys and `1631` data rows;
- `STATUS` names live sources such as `registry_upload_current_state`, `seller_funnel_snapshot`, `sales_funnel_history`, `web_source_snapshot`, `prices_snapshot`, `sf_period`, `spp`, `ads_bids`, `stocks`, `ads_compact`, `fin_report_daily`, plus blocked `promo_by_price` / `cogs_by_group`;
- blank values для promo/cogs-backed metrics трактуются как известный live-adapter gap, а не как повод срезать rows.

## Common failure signatures

| Signal | Meaning |
| --- | --- |
| `CONFIG!I2 должен содержать URL registry upload endpoint` | sheet-side endpoint URL is missing |
| `sheet vitrina endpoint returned non-JSON response` | stale/invalid external URL or upstream HTML error |
| `ReferenceError: URL is not defined` | Apps Script runtime bug in sheet-side URL derivation |
| `ACCESS_TOKEN_SCOPE_INSUFFICIENT` for `clasp` | local GAS OAuth scopes are insufficient for content read/write |

# Known gaps

- This runbook is compact and does not replace module-specific evidence.
- It intentionally omits full deploy/platform operations.

# Not in scope

- Full SRE runbook.
- Full legacy debug cookbook.
- Secrets or host-specific credential instructions.
