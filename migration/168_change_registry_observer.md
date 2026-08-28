# Migration 168 — activate the Change Registry observer

This additive live-runtime migration builds on migration 167 and its canonical
baseline/diff engine. It:

1. adds immutable checkpoint source summaries, observer jobs/events and
   scheduled health events plus one CAS lease table to the operational
   StoreRegistry schema;
2. installs the two-hour read-only Prices + Ads observer and its controlled
   hosted-target activation flag;
3. exposes the authenticated `Управление SKU → Реестр изменений` read/manual
   scan/annotation surface;
4. registers the operational writer in root-storage, hosted target, systemd and
   business-maintenance inventories.

There is no historical import or backfill. The first complete production scan
after deployment is the baseline and must produce zero facts. Rollback is code
rollback plus disabling the timer/flag through a later repo-owned release; all
already persisted registry evidence remains immutable.

No WB POST/PATCH, price/bid/campaign mutation, Balance write, recommender,
analytics or `manual_pending` behavior is introduced.
