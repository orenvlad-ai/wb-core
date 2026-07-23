# WB autoanswers schema v5 — content priority and split progress

Status: release candidate. Deployment and acceptance remain in `master_enabled=true`, `mode=manual`, force-off false. No automatic run is confirmed by this migration.

## Production containment evidence

Read-only baseline was captured at `2026-07-21T16:21:30.296387Z` before development. Effective mode was `auto_all`, policy epoch `4`, transition run `ade2ade48e1849be9872fe400e0d2485`, with 4 claimable AI jobs and 59 claimable publication writes. Durable totals were 1,161 processing jobs and 131 publication jobs; 1,074 jobs were then classified by v2 as rating-only (989 nonterminal), 40,223 exact scope members were not yet materialized, and active processing/publication leases and reservations were zero. Last provider entry was `2026-07-21T16:10:51.996532Z`; last successful AI call was `2026-07-21T16:11:02.463214Z`; the last confirmed publication timestamp was `2026-07-21T16:21:11.693818Z`.

At `2026-07-21T16:22:06.693238Z`, the authenticated canonical settings API applied `selector_state=manual`. Readback proved `master_enabled=true`, effective `mode=manual`, `force_off=false`, policy epoch `5` and `stop_reason=manual_pause`. Scheduler observations at `16:22:20.494133Z`, `16:23:27.642218Z` and `16:24:33.673715Z` proved zero new provider entries, zero new WB POST/PATCH attempts, zero active AI/publication work and `$0` active reservations. Processing/publication aggregate totals remained 1,161/131. A later read-only lifecycle check at `16:52:28Z` still showed manual, zero claimable work, zero active processing/readback and the same 1,161/131 aggregate totals.

## Additive migration

Schema v5 adds `content_classification` to current feedback rows and `content_classification_at_preview` to immutable transition membership, plus class/date priority indexes. Existing feedback content/media evidence is backfilled conservatively. Malformed or contradictory rows become `indeterminate`; only proven empty rating 1–5 rows become `rating_only`.

Existing processing jobs, results, revisions, reservations, publications, attempts and audit are retained. Processing kind is reconciled from the canonical class. An unpublished legacy rating-template result that is now content-bearing is quarantined as regeneration/review evidence unless a WB write already started; published and started-write/readback evidence is never rewritten.

The API contract identity becomes `wb_autoanswers_server_v3`, and policy identity becomes `owner-policy-2026-07-21-v3`. The unchanged template payload remains `owner-policy-2026-07-21-v2`. No frozen AI artifact changes.

## Release gates

1. Keep production manual and prove zero new provider entries, WB POST/PATCH attempts, active jobs and reservations.
2. Create and verify the canonical pre-v5 backup before additive DDL.
3. Verify classification, immutable snapshot, content-first processing/publication barrier, queue-capacity release, retry/restart/readback behavior and four counters.
4. Deploy only through the repository-owned release path.
5. Run authenticated desktop and 390px production UI acceptance without generation, publication or automated-mode confirmation.
6. Reconfirm manual state, preserved queue totals, zero post-containment AI/WB writes and terminal `release:production`.

The read-only production UI evidence runner retries a GET at most three times
only when Playwright reports a transient connection reset. HTTP failures,
authentication errors, assertions and all mutation paths remain non-retryable
and fail closed.

## Rollback

Emergency force-off remains canonical. Code rollback leaves additive v5 columns/indexes inert. Restore the verified pre-v5 backup only for demonstrated corruption and only after GET reconciliation of any ambiguous started write. Never delete queues, results, revisions, reservations, publications or audit.
