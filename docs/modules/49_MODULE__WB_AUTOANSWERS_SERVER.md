---
title: "WB Autoanswers Server v1"
doc_id: "49_MODULE__WB_AUTOANSWERS_SERVER"
doc_type: "module"
status: "manual_mode_activation_release_candidate"
purpose: "Server-native synchronization, frozen AI drafting and readback-confirmed WB answer publication"
scope: "SellerOS / wb-core feedbacks section"
source_basis: "Owner decisions plus frozen AI bundle v1.4.2"
source_of_truth_level: "implementation contract"
update_note: "Manual mode is implemented and force-OFF acceptance passed; the tracked activation release removes the environment override while persisted OFF remains authoritative, then selects manual through a guarded lifecycle without running AI or WB writes."
---

# WB Autoanswers Server v1

## Outcome and safety boundary

This module replaces the former Make/Telegram transport assumptions with a server-owned, durable contour:

```text
WB Feedbacks GET
  -> local SQLite canonical feedbacks + versions + media
  -> durable processing job with lease
  -> versioned Python/Node boundary
  -> untouched frozen v1.4.2 classifier/writer/validator/rewrite/fallback pipeline
  -> server publication policy
  -> durable publication job
  -> WB POST answer
  -> mandatory GET feedback detail readback
```

The first deployment stage pinned `WB_AUTOANSWERS_FORCE_OFF=true` in the production HTTP unit, target, and installed-but-disabled full worker. The tracked activation release changes those three pins to `false`, but deploy still leaves persisted master OFF and both autoanswers timers disabled. `apps/wb_autoanswers_worker.py` performs no external I/O by default and refuses `--run-once` unless `WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true`. That environment gate does not replace the persisted master-switch: AI and every WB write also require effective ON. Whenever present, `WB_AUTOANSWERS_FORCE_OFF=true` always wins.

Deploy has a dedicated idempotent dependency stage. It verifies Node >=20, npm and ffmpeg; if missing, it installs base packages through apt and the official Node `22.21.1` archive from `nodejs.org`, checks exact SHA-256 for amd64/arm64, then performs lockfile `npm ci` and all 28 frozen hash checks before schema migration or restart. A capacity preflight requires free bytes equal to the live DB size plus 2 GiB. If necessary, it compacts only the autoanswers-owned raw pre-v1 backup to zstd after SQLite integrity, compressed-stream and byte-exact hash verification, writes an atomic restore manifest, and only then removes the redundant raw representation. First schema-v2 preparation remains fail-closed on persisted master OFF. After schema v2 is already applied, later deploy preflight still runs with process-local `WB_AUTOANSWERS_FORCE_OFF=true`, but accepts and preserves persisted `master_enabled=true, mode=manual`; this lets ordinary releases reach the managed service restart without disabling an already accepted manual mode. After force-OFF UI acceptance, the tracked activation release changes the same three pins to false. The repo-owned `autoanswers-lifecycle activate-manual` command then requires an empty AI/publication queue, verifies Node >=20, ffmpeg and all 28 frozen hashes, disables the force-OFF-only sync timer, atomically persists `master_enabled=true, mode=manual`, runs one bounded GET-only canary through an entrypoint that imports neither Node/OpenAI nor a WB writer, proves zero AI/publication jobs, and only then enables the full worker timer. It never generates a real answer or calls a WB write during activation.

Production history and OFF-mode steady synchronization run only through `apps/wb_autoanswers_readonly.py`, invoked by the repo-owned hosted runner or its dedicated timer. That entrypoint imports only the GET adapter, reasserts force-off after loading its allowlisted env values, requires persisted master OFF, and proves that AI/publication job counts do not change. Deploy installs the timer disabled; it is enabled only after canary/detail/backfill acceptance.

No Make route, Telegram route, WB answer PATCH, browser-side WB write, or HTTP-request-to-WB-write path exists.

## Frozen identity

- prompt bundle: `1.4.2`;
- evaluation signature: `sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da`;
- Python/Node envelope: `wb_autoanswers_node_boundary_v1`;
- vendored bundle source ZIP SHA-256: `350b15bdfab9f8139a83920fbce7f1c9876607b594cea0d8c19a6f9ddc38f7e5`;
- frozen manifest verification: 28 artifacts on every boundary invocation.

The packaged `make_mvp/` bytes are preserved. The only new Node code is a sibling stdin/stdout adapter. It delegates routing, case-code allocation, JSON Schemas, deterministic route/draft guards, maximum two rewrites, usage accounting and approved same-route fallback to the frozen orchestrator.

## Code map

| Concern | Implementation |
| --- | --- |
| Stable orchestration constants and states | `packages/contracts/wb_autoanswers.py` |
| Boundary JSON Schema | `packages/contracts/wb_autoanswers_node_boundary.schema.json` |
| SQLite schema, idempotency, leases, budgets, audit, list/detail | `packages/application/wb_autoanswers_runtime.py` |
| Official GET adapter and isolated POST capability | `packages/adapters/wb_autoanswers.py` |
| Initial backfill, steady overlap and reconciliation | `packages/application/wb_autoanswers_sync.py` |
| Bounded media download and frames | `packages/application/wb_autoanswers_media.py` |
| Python/Node adapter | `packages/application/wb_autoanswers_node_bridge.py` |
| Frozen Node boundary and exact manual final-guard adapter | `packages/node/wb_autoanswers_boundary_v1/runner.mjs` |
| Frozen package | `packages/node/wb_autoanswers_v1_4_2/make_mvp/` |
| AI processing lease worker | `packages/application/wb_autoanswers_worker.py` |
| Publication/readback lease worker | `packages/application/wb_autoanswers_publication.py` |
| One bounded scheduler tick | `packages/application/wb_autoanswers_coordinator.py` |
| Fail-closed CLI entrypoint | `apps/wb_autoanswers_worker.py` |
| Force-off GET-only canary/backfill | `apps/wb_autoanswers_readonly.py`, hosted `autoanswers-readonly` command |
| Authenticated production browser acceptance | `apps/wb_autoanswers_production_ui_flow.py`, hosted `autoanswers-ui-flow` command |
| OFF-mode background GET sync | `wb-core-autoanswers-readonly-sync.service/.timer`, hosted timer gate |
| Manual lifecycle and schema-v2 backup gate | `apps/wb_autoanswers_activation.py`, hosted `autoanswers-lifecycle` command |
| Full bounded worker, installed disabled and enabled only by guarded manual lifecycle | `wb-core-autoanswers-worker.service/.timer` |
| Backend/UI integration | `packages/application/registry_upload_http_entrypoint.py`, `packages/adapters/registry_upload_http_entrypoint.py`, `packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html` |

## Local data model

All tables are additive in the existing runtime SQLite database:

- `sheet_vitrina_v1_wb_feedbacks`: latest canonical WB observation per feedback ID;
- `sheet_vitrina_v1_wb_feedback_versions`: immutable semantic versions;
- `sheet_vitrina_v1_wb_feedback_media`: full fetch URL, stable URL without query, download/frame status and hashes;
- `sheet_vitrina_v1_wb_sync_runs`, `sheet_vitrina_v1_wb_sync_state`, `sheet_vitrina_v1_wb_autoanswers_commands`;
- `sheet_vitrina_v1_wb_autoanswer_jobs`;
- `sheet_vitrina_v1_wb_publication_jobs`, `sheet_vitrina_v1_wb_publication_attempts`;
- `sheet_vitrina_v1_wb_autoanswers_budget_reservations`;
- `sheet_vitrina_v1_wb_autoanswers_backlog_previews`;
- `sheet_vitrina_v1_wb_autoanswers_audit_events`;
- singleton settings and schema version tables.

`content_version_hash` includes review meaning: text, pros, cons, rating, tags, product identity and stable media identity. It excludes answer, `wasViewed`, other WB state and all media URL query parameters. `wb_observation_hash` includes those WB-side observations. Therefore state/readback or expiring URL changes do not create a paid AI version.

Processing idempotency is `feedback_id | content_version | 1.4.2`. Publication idempotency is `feedback_id | content_version | normalized-final-reply-sha256 | create-answer-v1`.

## Synchronization

Initial history starts at `2026-01-01`, uses answered and unanswered streams, processes one bounded page of one UTC day per tick and persists a cursor only after all rows are committed. Backfill and archive reconciliation never enqueue AI.

Steady state uses answered and unanswered windows with a 48-hour overlap. Upsert happens before enqueue. A new review or a new semantic version is automatically eligible only when first observed during the currently active ON epoch. Reviews observed while OFF, historical rows and jobs from an older enable epoch require the explicit backlog preview/enqueue action.

Every twelfth coordinator tick rotates to archive reconciliation; other ticks advance a backfill stream. Unanswered count reconciliation is exposed at the service boundary. WB `429`, `5xx` and transport errors leave the cursor at the last durable point.

## Master-switch and modes

The persisted default is OFF.

- OFF: sync, local list and detail continue; processing, review approval and new writes fail closed.
- emergency OFF: `WB_AUTOANSWERS_FORCE_OFF=true`; OFF→ON is rejected and any pre-existing persisted ON remains ineffective.
- `manual`: sync never creates AI jobs. One explicit per-review action creates one idempotent durable job for the exact current content version. Generation stops for user review; edited text crosses the same frozen schema/final guard again; a separate confirmed action creates a durable publication job.
- `draft_only`: valid drafts stop at `generated`.
- `auto_safe`: only `public_only`, `wb_return`, and `wb_support` can become `approved`; `seller_chat` is always `needs_review`.
- `auto_all`: any route passing every hard gate can become `approved`, except `seller_chat`, fallback, media uncertainty, stale version, external answer, or technical/contract uncertainty.

The UI exposes one five-state Russian selector: `Выключено`, `Ручной`, `Черновики`, `Безопасный`, `Полный`. Changing OFF to ON increments `enable_epoch`. Old queued/retry work is quarantined into `needs_review`; it is not silently resumed. Manual mode does not expose backlog execution. Other enabled modes require an expiring, actor-bound backlog preview with count and conservative maximum estimated cost, followed by a second explicit request.

## Budgets

Defaults are persisted as USD decimal strings:

- daily hard cap: `$5.00`;
- monthly hard cap: `$50.00`;
- warning threshold: `70%`;
- conservative reservation per claimed review: `$1.00`.

A claim performs `BEGIN IMMEDIATE`, sums actual plus open reservations and reserves before Node/OpenAI can run. A second concurrent worker cannot oversubscribe the cap. Successful frozen usage settles exact estimated cost; skip settles zero. The current accounting period is UTC and must be treated as part of the v1 operational contract.

## Media

Media URLs are accepted only over HTTPS from the explicit WB CDN suffix allowlist. Photos are limited to 20 MiB each. Video is limited to 100 MiB and ffmpeg extracts at most six JPEG frames. Files are stored under the runtime directory by a hash of feedback ID, not by user-controlled paths.

Downloaded photos and extracted video frames cross the frozen media contract as image data URLs. A fetch or extraction failure is recorded as media uncertainty. The pipeline is never told that unprocessed video was viewed, and uncertainty forces manual review.

## Publication and readback

The browser/API only creates commands or durable jobs. It never calls WB synchronously.

Before POST, the repository atomically rechecks:

- effective master ON, including emergency override;
- current content version;
- absence of an external WB answer;
- frozen identity, exact final text and its hash;
- schema/route/validator/final hard-gate flags;
- no fallback and no media uncertainty;
- seller_chat has exactly one frozen case code and no public request for photos, video, screenshots, labels, evidence or other materials.
- for manual publication: current mode is still `manual`, the requester still has `feedbacks.ai_review`, the reviewed edit revision and exact reply hash match, and a frozen final-guard pass is persisted.

An attempt row containing exact reply, normalized hash, feedback ID, versions and evaluation signature is committed before transport. Any HTTP response, HTTP error or timeout becomes `publish_pending_readback`. The worker cannot write again from that state. It performs detail GET, compares normalized answer text and reaches `published` only on an exact match. Missing/different/external answer becomes `needs_review`. While master/force-off is active, no new-write publication job is claimed. A durable job for which a write may already have happened can still perform its mandatory GET-only readback while OFF; it can never become a second POST.

## UI and API

The existing `GET /v1/sheet-vitrina-v1/feedbacks` remains unchanged. New routes are additive:

| Method | Route | Effect |
| --- | --- | --- |
| GET | `/v1/sheet-vitrina-v1/feedbacks/local` | Last 50 by default; local pagination and filters |
| GET | `/v1/sheet-vitrina-v1/feedbacks/detail?id=...` | Feedback, media, AI jobs, publication attempts and audit |
| GET/POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings` | Status / protected settings update |
| POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/sync-now` | Idempotent background command only |
| POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/backlog/preview` | Count/cost preview |
| POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/backlog/enqueue` | Explicit preview-bound enqueue |
| POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/review/approve` | Manual durable publication enqueue only |
| POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/manual/generate` | Idempotent exact-version manual AI enqueue |
| POST | `/v1/sheet-vitrina-v1/feedbacks/autoanswers/manual/edit` | Frozen final guard and persisted reviewed edit |

Base local read/sync requires `feedbacks`. Manual generation, edit review, and publication enqueue additionally require `feedbacks.ai_review`. Switch, mode, policy/budget and backlog actions require `feedbacks.autoanswers_admin`. Every autoanswers POST also requires JSON content, the same-origin CSRF marker, and a non-cross-site browser request.

The first `Отзывы -> Отзывы` subtab reads immediately from SQLite, defaults to 50 rows, has server pagination/filters, AI and WB status badges, detail media/result/cost/attempt/error/audit blocks, and a nonblocking sync command. In manual mode an eligible review shows `Сгенерировать ответ`; the guarded result can be edited and rechecked, and `Опубликовать` appears only after a pass. Publication requires a normal confirmation and remains a background job until WB readback. Enabling any state from OFF requires confirmation; `Полный` additionally requires a typed confirmation.

## Required environment names

No values belong in source control:

- `REGISTRY_UPLOAD_RUNTIME_DIR`;
- `WB_API_TOKEN` (the existing canonical default name);
- `WB_FEEDBACKS_API_BASE_URL`;
- `OPENAI_API_KEY`;
- `OPENAI_RESPONSES_BASE_URL` (optional override);
- `WB_AUTOANSWERS_FORCE_OFF`;
- `WB_AUTOANSWERS_EXTERNAL_IO_ENABLED`.

## Local verification

```bash
PYTHONPATH=. python3 apps/wb_autoanswers_activation_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_runtime_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_sync_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_node_bridge_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_media_worker_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_publication_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_http_ui_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_readonly_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_release_safety_test.py
python3 -m compileall -q apps packages
```

The frozen package is tested separately with `npm test` from its directory. Fixture execution requires `WB_AUTOANSWERS_TEST_MODE=1`; it never uses an API key.

## Production release posture

- the completed first hosted deployment kept both persisted master and effective mode OFF under force-off;
- the activation deployment changes the HTTP, full-worker and active-target force-off pins to false while persisted master remains OFF;
- first additive schema takes and integrity-checks a coherent SQLite backup before mutation;
- the full worker timer stays disabled through the unforced-OFF acceptance;
- bounded GET-only canary/backfill is the only production WB capability authorized for this release;
- the GET-only steady timer is installed disabled and may be enabled only after read acceptance;
- no OpenAI call;
- no WB POST;
- only after authenticated unforced-OFF acceptance may the lifecycle atomically select manual and enable its timer;
- no PATCH of existing WB answers.

After manual activation acceptance, the only next gate is the owner's first explicit click on `Сгенерировать ответ` for one real eligible review. That click is intentionally not part of release acceptance; every WB write remains blocked until the owner separately confirms `Опубликовать` for a guarded result.
