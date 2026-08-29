# Migration 171 — Balance manual portal pending

Status: additive live-runtime activation; no historical backfill and no WB
mutation.

Balance now publishes stable atomic `recommendation_item_id` values for exact
bid targets only. The authenticated `Применить на портале` endpoint persists
an operation, item, `pending` event and exact-target CAS pointer. It creates no
attempt, fact, apply job or external write. Existing `live_wb` remains 403 and
the dry-run adapter remains the only Balance apply adapter.

The current immutable manual-pending tables from migration 165 are activated
without a new table or column. Item before/requested canonical values are the
pre-pending observation and desired target; the initial event time determines
the exact 24-hour expiration. New recommendation bytes supersede the prior
active lifecycle append-only. Stable replay is idempotent; conflicting bytes,
missing exact baseline, seller/account mismatch and advert-to-nmID cardinality
zero/many fail closed.

The existing observer transaction hook resolves active pending rows after the
baseline engine has persisted facts. A first post-pending exact transition to
the desired value appends `matched` and exact change-item/recommendation links.
A different value appends `deviated`, retains the proven fact and adds no false
fulfillment link. Expiration appends `expired` and no fact. Observer-first and
manual-first order both reuse the same fact identity; the second path adds only
missing links.

Deploy/activation may ensure the existing schema and run the normal read-only
checkpoint, but must leave pending/fact/attempt counts unchanged unless an
operator has explicitly confirmed a recommendation.

Verification:

```bash
python3 apps/change_registry_manual_pending_smoke.py
python3 apps/change_registry_observer_smoke.py
python3 apps/sku_inventory_balance_smoke.py
python3 apps/sku_inventory_balance_browser_smoke.py
```
