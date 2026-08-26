# Migration 165 — seller change registry foundation

Status: additive dark foundation only.

This bounded migration adds `packages/application/change_registry.py`, wires
its empty schema into the existing operational runtime initializer and adds
`apps/change_registry_smoke.py`. The authoritative data/identity/immutability
contract is
`docs/modules/54_MODULE__CHANGE_REGISTRY_FOUNDATION.md`.

Deployment of the code would only install empty tables/indexes/triggers in the
StoreRegistry-selected operational SQLite generation. It does not capture,
import, backfill or publish any seller action and does not replace current
Prices/Ads JSONL or `sheet_vitrina_v1_sku_action_events`.

Verification:

```bash
python3 apps/change_registry_smoke.py
```

The smoke covers byte/behavior-idempotent schema init; required seller/account
scope; invalid price/bid/campaign identities; exact-one campaign mapping and
0/many incidents; integer minor-unit/basis-point invariants; database-level
UPDATE/DELETE rejection; idempotent/duplicate inserts; attempt and manual
pending lifecycle; annotation parents; missing/null/zero separation; late fact
links and non-value-only fact identity; stable cursors; StoreRegistry query-only
readback; coherent SQLite backup integrity and foreign keys.

No production/data migration manifest is present. No release/apply action is
part of this migration.
