# WB Finance Weekly Report migration

## Scope

Add the official WB Finance weekly report contour to the existing hosted runtime. No legacy endpoint, Google Sheets write, ad-hoc SQL, or separate database is introduced.

## Apply

Application startup calls the idempotent schema guard in `WbFinanceWeeklyBlock.ensure_schema()`. The guarded migration creates six `wb_finance_weekly_*` tables and indexes inside the existing runtime SQLite database. Existing tables and rows are not rewritten.

Production apply sequence is canonical deploy, service restart/schema guard, real repo-owned CLI backfill, stabilization resync, read-route verification, and UI smoke. Before deploy, copy the runtime SQLite file to a timestamped backup in the canonical runtime state backup directory. Re-running schema/backfill is safe because raw identity is `(seller_id, report_id, rrd_id)` and weekly aggregates are replaced after raw upsert.

## Rollback

Code rollback leaves the additive tables dormant. Restore the verified pre-deploy SQLite backup only if the application/database itself is damaged; normal code rollback must not delete finance history.
