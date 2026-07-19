# WB autoanswers server v1 — activation and rollback runbook

Status: production release train; feature deployed physically but activation deliberately blocked.

## Preconditions

1. Review `IMPLEMENTATION_REPORT.md` and `docs/modules/49_MODULE__WB_AUTOANSWERS_SERVER.md`.
2. Keep persisted master-switch OFF and set `WB_AUTOANSWERS_FORCE_OFF=true` for the first database/schema and read-only checks.
3. The first schema initializer must create a coherent `runtime/backups/wb_autoanswers_schema_v1/` SQLite backup, verify `PRAGMA integrity_check=ok`, and abort before schema mutation on failure.
4. Install the lockfile-pinned Node dependencies and ffmpeg in the target image; never commit `node_modules`.
5. Bind only the named runtime secrets through the existing hosted secret boundary.
6. The repo-owned GET-only runner may set its narrow external-I/O gate for approved sync/backfill; the full worker gate stays disabled.

## Staged external gates

### Gate A — WB sandbox/read-only

- master remains OFF;
- one bounded feedbacks page and detail GET only;
- verify canonical upsert, hashes, media metadata, cursor and local UI;
- verify no AI job and no publication attempt;
- remove external-I/O enable flag after the check.

### Gate B — backfill/read reconciliation

- still master OFF;
- advance from `2026-01-01` under rate limits;
- compare local/remote unanswered count and archive samples;
- verify history created no AI jobs.
- after successful reconciliation, enable only `wb-core-autoanswers-readonly-sync.timer`; the full AI/publication worker remains unscheduled.

### Gate C — OpenAI draft-only canary

- explicit owner approval and daily/monthly budget dashboards required;
- enable master in `draft_only` only;
- one newly observed eligible review;
- verify frozen identity, usage/cost, media status and no publication job.

### Gate D — fake transport publication rehearsal

- no WB write;
- exercise approved, 204/no readback, different readback, timeout and 429 paths against the test transport.

### Gate E — separately approved production write canary

- not authorized by this implementation;
- exact review IDs and maximum count must be owner-approved;
- start with `auto_safe`, not `auto_all`;
- mandatory readback is the only publication proof.

## Rollback

1. Set `WB_AUTOANSWERS_FORCE_OFF=true`; this blocks AI, manual approval, every write and all publication claims, including pending readback.
2. Stop only the future autoanswers scheduler unit after it exists; do not delete queued data.
3. Keep `publish_pending_readback` jobs unclaimed while OFF. After a separately authorized re-enable, reconcile them by GET before considering any new write; never replay their POST blindly.
4. Restore code independently. Additive SQLite tables may remain inert; destructive down migrations are not required.
5. Restore the pre-activation SQLite backup only for database corruption, never to hide ambiguous publication attempts. Reconcile WB first.

## Acceptance

- master OFF really blocks all new processing and writes;
- expired leases recover with one claimant;
- backfill creates zero automatic AI jobs;
- daily/monthly reservations prevent concurrent overspend;
- seller_chat remains review-only;
- 204, timeout and all ambiguous writes lead to readback before any decision;
- only exact normalized readback reaches `published`;
- existing `GET /feedbacks` and operator UI smoke tests remain compatible.
