# Migration 166 — strict Prices + Ads source acquisition

Status: additive dark read-only acquisition only.

This bounded migration adds
`packages/application/change_registry_source_acquisition.py`, extends the two
existing official HTTP adapters only with structured sanitized rate-limit
errors, and adds `apps/change_registry_source_acquisition_smoke.py`. The
authoritative contract is
`docs/modules/55_MODULE__CHANGE_REGISTRY_SOURCE_ACQUISITION.md`.

The acquisition uses exhaustive Prices GET pagination and Promotion count plus
detail batches. It returns deterministic in-memory manifests for one explicit
seller/account and does not connect them to the change-registry repository,
runtime schema initializer, refresh/scheduler, HTTP/UI or any writer.

Deployment installs code only. It creates no schema/table/row/file, imports no
history, starts no observer and performs no source request until a future
separately authorized consumer explicitly calls the internal class. No
production apply manifest is present.

Verification:

```bash
python3 apps/change_registry_source_acquisition_smoke.py
python3 apps/change_registry_smoke.py
python3 apps/wb_prices_management_smoke.py
python3 apps/sheet_vitrina_v1_ads_smoke.py
python3 apps/sku_management_smoke.py
python3 apps/sku_inventory_balance_smoke.py
```

All acquisition tests use fake read sources. No test calls WB upload/PATCH or
writes registry/business data.
