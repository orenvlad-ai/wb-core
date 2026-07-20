# WB autoanswers server v1 — activation and rollback runbook

Status: activation release candidate after completed force-OFF deployment acceptance.

## Preconditions

1. Review `IMPLEMENTATION_REPORT.md` and `docs/modules/49_MODULE__WB_AUTOANSWERS_SERVER.md`.
2. Keep persisted master-switch OFF and set `WB_AUTOANSWERS_FORCE_OFF=true` for the first database/schema and read-only checks.
3. The schema-v2 initializer must create a coherent `runtime/backups/wb_autoanswers_schema_v2/` SQLite backup, verify `PRAGMA integrity_check=ok`, and abort before mutation on failure.
4. Before that raw backup, require free capacity of live DB size plus 2 GiB. The repo-owned capacity gate may replace only the autoanswers pre-v1 raw backup with a verified zstd representation and restore manifest; it must prove SQLite integrity and byte-exact decompression before deleting the raw representation.
5. Install the lockfile-pinned Node dependencies and ffmpeg in the target image; never commit `node_modules`.
6. Bind only the named runtime secrets through the existing hosted secret boundary.
7. The repo-owned GET-only runner may set its narrow external-I/O gate for approved sync/backfill; the full worker timer stays disabled through initial force-OFF acceptance.

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

### Gate C — remove force-off and activate manual without AI

- merge/deploy the tracked configuration release that changes HTTP, target and full-worker force-off pins to false;
- keep persisted master OFF and both autoanswers timers disabled through deploy;
- prove `master_enabled=false`, `force_off=false`, `effective_enabled=false`, zero jobs and disabled generate/publish controls through authenticated UI acceptance with `--expected-state off-unforced`;
- run `autoanswers-lifecycle activate-manual`;
- require Node >=20, ffmpeg, all 28 frozen hashes, an empty AI/publication queue and current schema backup evidence;
- the command disables the force-OFF timer, persists master ON/manual, runs one bounded GET-only canary with no Node/OpenAI/writer import, proves zero AI/publication jobs and enables the full worker timer;
- run authenticated UI acceptance with `--expected-state manual`;
- do not click generation and do not publish.

### Gate D — first owner-operated manual generation

- the owner explicitly clicks `Сгенерировать ответ` for one eligible review in `Отзывы → Отзывы`;
- verify frozen identity, usage/cost, route, warnings, media status and guard evidence;
- do not publish until the owner reviews/edits, reruns final guards and separately confirms `Опубликовать`.

### Gate E — fake transport publication rehearsal

- no WB write;
- exercise approved, 204/no readback, different readback, timeout and 429 paths against the test transport.

### Gate F — owner-confirmed production manual write

- only one separately confirmed guarded manual reply;
- mandatory readback is the only publication proof.

## Rollback

1. Run `autoanswers-lifecycle deactivate`; it disables the full worker timer, persists master OFF, and enables the GET-only sync timer.
2. Restore `WB_AUTOANSWERS_FORCE_OFF=true` in a tracked release for emergency fail-closed operation.
3. A `publish_pending_readback` job may perform only detail GET reconciliation while OFF; never replay its POST blindly.
4. Restore code independently. Additive SQLite tables may remain inert; destructive down migrations are not required.
5. Restore the pre-activation SQLite backup only for database corruption, never to hide ambiguous publication attempts. Reconcile WB first.

## Acceptance

- master OFF really blocks all new processing and writes while still allowing mandatory GET-only readback;
- expired leases recover with one claimant;
- backfill creates zero automatic AI jobs;
- daily/monthly reservations prevent concurrent overspend;
- seller_chat remains review-only;
- 204, timeout and all ambiguous writes lead to readback before any decision;
- only exact normalized readback reaches `published`;
- existing `GET /feedbacks` and operator UI smoke tests remain compatible.
