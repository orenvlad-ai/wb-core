---
title: "WB Autoanswers Server v1"
doc_id: "49_MODULE__WB_AUTOANSWERS_SERVER"
doc_type: "module"
status: "implemented_locally_external_io_gated"
purpose: "Server-native synchronization, frozen AI drafting and readback-confirmed WB answer publication"
scope: "SellerOS / wb-core feedbacks section"
source_basis: "Owner decisions plus frozen AI bundle v1.4.2"
source_of_truth_level: "implementation contract"
update_note: "No production deployment, live WB/OpenAI call or WB write is included in this checkpoint."
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

The implementation is intentionally not wired into a production timer or deployment manifest. `apps/wb_autoanswers_worker.py` performs no external I/O by default and refuses `--run-once` unless `WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true`. That environment gate does not replace the persisted master-switch: AI and every new WB write also require effective ON. `WB_AUTOANSWERS_FORCE_OFF=true` always wins.

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
| Frozen Node boundary | `packages/node/wb_autoanswers_boundary_v1/runner.mjs` |
| Frozen package | `packages/node/wb_autoanswers_v1_4_2/make_mvp/` |
| AI processing lease worker | `packages/application/wb_autoanswers_worker.py` |
| Publication/readback lease worker | `packages/application/wb_autoanswers_publication.py` |
| One bounded scheduler tick | `packages/application/wb_autoanswers_coordinator.py` |
| Fail-closed CLI entrypoint | `apps/wb_autoanswers_worker.py` |
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
- emergency OFF: `WB_AUTOANSWERS_FORCE_OFF=true`; persisted ON remains visible but is ineffective.
- `draft_only`: valid drafts stop at `generated`.
- `auto_safe`: only `public_only`, `wb_return`, and `wb_support` can become `approved`; `seller_chat` is always `needs_review`.
- `auto_all`: any route passing every hard gate can become `approved`, except `seller_chat`, fallback, media uncertainty, stale version, external answer, or technical/contract uncertainty.

Changing OFF to ON increments `enable_epoch`. Old queued/retry work is quarantined into `needs_review`; it is not silently resumed. Backlog requires an expiring, actor-bound preview with count and conservative maximum estimated cost, followed by a second explicit request.

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

An attempt row containing exact reply, normalized hash, feedback ID, versions and evaluation signature is committed before transport. Any HTTP response, HTTP error or timeout becomes `publish_pending_readback`. The worker cannot write again from that state. It performs detail GET, compares normalized answer text and reaches `published` only on an exact match. Missing/different/external answer becomes `needs_review`. Readback `429`, `5xx` or timeout retries readback only, including while master is OFF.

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

Base local read/sync requires `feedbacks`. Approval additionally requires `feedbacks.ai_review`. Switch, mode, policy/budget and backlog actions additionally require `feedbacks.autoanswers_admin`.

The first `Отзывы -> Отзывы` subtab reads immediately from SQLite, defaults to 50 rows, has server pagination/filters, AI and WB status badges, detail media/result/cost/attempt/error/audit blocks, and a nonblocking sync command. ON and `auto_all` require explicit confirmation; the latter cannot be enabled with one click.

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
PYTHONPATH=. python3 apps/wb_autoanswers_runtime_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_sync_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_node_bridge_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_media_worker_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_publication_test.py
PYTHONPATH=. python3 apps/wb_autoanswers_http_ui_test.py
python3 -m compileall -q apps packages
```

The frozen package is tested separately with `npm test` from its directory. Fixture execution requires `WB_AUTOANSWERS_TEST_MODE=1`; it never uses an API key.

## Not activated here

- no systemd/timer installation;
- no hosted deployment;
- no real SQLite migration execution outside temporary test directories;
- no WB sandbox or production GET;
- no OpenAI call;
- no WB POST;
- no production mode change;
- no PATCH of existing WB answers.

The next external gate is an owner-authorized, credentials-bound, one-page WB sandbox/read-only sync with master OFF. OpenAI canary and any WB write require later, separate gates.
