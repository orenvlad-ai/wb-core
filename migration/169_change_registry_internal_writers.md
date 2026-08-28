# Migration 169 — internal writer instrumentation

Status: additive live-runtime instrumentation; no schema or historical-data migration.

This bounded migration activates the migration-165 immutable registry for future
internal seller-controlled price and bid writes. One shared application seam is
wired to standalone Prices, SKU Management price, every SPP measurement and
restore step, standalone Ads bid, and SKU Management bid.

The seam atomically persists an operation, exact atomic items and `created`
attempts immediately before the sole WB submit. Registry configuration/storage
failure blocks that submit. A returned submit receipt appends `submitted`;
only exact WB tuple readback appends `confirmed` and one `wb_readback` fact per
changed canonical field. Rejection, failure and transport/readback ambiguity
remain explicit and never cause a blind retry. Existing JSONL and SKU action
audits remain native evidence links.

The baseline engine now reconciles the observer/writer race by exact target,
field, before/after and transition-start containment. The second proof late-links
the existing fact. Multiple possible matches fail closed; same values in a
different interval are not globally deduplicated.

There is no new table/column, backfill, scheduler, UI capability, WB probe,
production-mutation manifest or deploy-time seller write. Balance dry-run stays
outside the seam and creates zero registry rows.

Verification:

```bash
python3 apps/change_registry_internal_writers_smoke.py
python3 apps/change_registry_baseline_engine_smoke.py
python3 apps/change_registry_smoke.py
python3 apps/wb_prices_management_smoke.py
python3 apps/sheet_vitrina_v1_ads_smoke.py
python3 apps/sku_management_smoke.py
python3 apps/wb_spp_tester_smoke.py
python3 apps/sku_inventory_balance_smoke.py
```
