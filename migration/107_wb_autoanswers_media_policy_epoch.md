# WB autoanswers schema v3 — media and policy-epoch rollout

Status: release candidate. Production must remain `master_enabled=true`, `mode=manual` throughout this rollout.

## Additive migration

The repo-owned deploy preflight temporarily sets process-local `WB_AUTOANSWERS_FORCE_OFF=true`, takes a coherent integrity-checked schema-v3 backup, and applies only additive columns/tables/indexes:

- `policy_epoch` on settings, AI jobs and publication jobs;
- preview metadata on feedback media;
- media processing version and regeneration evidence on AI jobs;
- append-only AI revision and cost-event tables;
- actor-bound transition previews and durable reconciliation sweeps.

Existing feedback rows, the owner-published answer, publication attempt/readback and audit events are never rewritten. The migration marks only unpublished, unanswered, media-uncertain results with no publication attempt as `regeneration_required`.

## Release acceptance

1. Verify exact deployed SHA and schema-v3 backup/integrity evidence.
2. Verify production is still effective manual with zero claimable background AI jobs and zero active publication jobs.
3. Run exactly one repo-owned `autoanswers-readonly manual-media-canary`. It may perform WB detail GET and bounded WB/CDN media GET only; it cannot import OpenAI/Node execution or a WB writer.
4. Require one validated real photo, one real video preview and one-to-four extracted frames. Do not expose feedback IDs, signed query strings or private paths in evidence.
5. Run `autoanswers-ui-flow --expected-state manual`; require compact detail, closed technical spoiler, auto-growing reply, fixed-height/copy table answer, real photo/video rendering, narrow layout, no 5xx/page/console errors and unchanged job counts.
6. Do not click generate/regenerate/publish and do not switch modes during release acceptance.

## Future automated-mode gate

`draft_only`, `auto_safe` and `auto_all` are not activated by this rollout. A future admin switch first creates an actor-bound preview over unanswered history from `2026-01-01`; apply creates a new `policy_epoch` and a resumable sweep. Any downgrade makes old-epoch pre-write jobs ineligible. A possible write already started may complete only its mandatory readback.

## Rollback

1. Emergency: set `WB_AUTOANSWERS_FORCE_OFF=true`; this overrides every mode and blocks all new AI/WB write claims.
2. Code may roll back while additive schema-v3 tables remain inert; no destructive down migration is required.
3. Do not delete revisions, regeneration flags or policy epochs to simulate rollback.
4. Restore the verified pre-v3 SQLite backup only for demonstrated database corruption. First reconcile any ambiguous publication via GET; never replay a POST blindly.
5. Private media files may be removed by TTL. Cleanup resets DB fetch state before deletion so later processing refetches safely.

## Interrupted-capacity recovery

Lifecycle `status` must not initialize or migrate SQLite. When schema v3 is not yet proven it returns `schema_preparation_required`; additive DDL remains owned by force-off `prepare-deploy`.

If an interrupted schema-v3 preparation has already left a complete raw pre-schema snapshot in the autoanswers-owned v3 backup directory, the next repo-owned preparation resumes without deleting production data. It integrity-checks and hashes that snapshot, creates a zstd archive, verifies the compressed stream and exact decompressed SHA-256, writes and canonically reads back the v3 manifest, and only then removes the raw copy and orphaned sidecars. Any compression or verification error retains the raw snapshot for recovery.

If the new verified archive itself leaves less than 256 MiB free, a subsequent preparation may prune the minimum older compressed autoanswers backup pair. This is allowed only after canonical current-v3 restore proof and only for an exact older-version archive whose paired manifest matches filename, size, SHA-256 and recorded SQLite integrity. A `0600` cleanup audit records the current recovery proof and removed pair before unlink. The current v3 archive, live database and unrelated backups remain untouched.
