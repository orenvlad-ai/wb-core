---
title: "WB Autoanswers Server v1"
doc_id: "49_MODULE__WB_AUTOANSWERS_SERVER"
doc_type: "module"
status: "manual_content_priority_release_candidate"
purpose: "Server-native synchronization, frozen AI drafting and readback-confirmed WB answer publication"
scope: "SellerOS / wb-core feedbacks section"
source_basis: "Owner decisions plus frozen AI bundle v1.4.2"
source_of_truth_level: "implementation contract"
update_note: "Production remains manual. Schema v5 and policy v3 add canonical content classification, a strict content-first barrier across every automatic claim stage, immutable bounded-run membership and four exact progress counters in two visual cards."
---

# WB Autoanswers Server v1

## Outcome and immutable boundary

The production contour is:

```text
WB Feedbacks GET
  -> canonical local SQLite feedback/version/media rows
  -> durable processing lease
  -> versioned Python/Node boundary
  -> untouched frozen v1.4.2 classifier/writer/validator/rewrite/fallback
  -> server policy/write gate
  -> durable publication lease
  -> WB POST answer
  -> mandatory feedback-detail GET readback
```

Production is `master_enabled=true`, `mode=manual`, `WB_AUTOANSWERS_FORCE_OFF=false`. Manual steady sync creates no AI jobs. Only an explicit per-review action can enqueue generation; only a separate confirmed action can enqueue publication. Make, Telegram, WB answer PATCH, inline HTTP-to-WB write, and any silent rewrite of frozen prompts/contracts/guards/golden/fallbacks remain absent.

Frozen identity:

- bundle `1.4.2`;
- evaluation signature `sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da`;
- boundary `wb_autoanswers_node_boundary_v1`;
- source ZIP SHA-256 `350b15bdfab9f8139a83920fbce7f1c9876607b594cea0d8c19a6f9ddc38f7e5`;
- 28 frozen artifact hashes verified on every Node boundary invocation.

## Code map

| Concern | Implementation |
| --- | --- |
| Stable modes, states, permissions, identity | `packages/contracts/wb_autoanswers.py` |
| SQLite schema, hashes, queues, budgets, audit, API reads | `packages/application/wb_autoanswers_runtime.py` |
| Official WB GET and isolated POST adapters | `packages/adapters/wb_autoanswers.py` |
| Backfill/steady/archive reconciliation | `packages/application/wb_autoanswers_sync.py` |
| SSRF-safe photo/MP4/HLS pipeline | `packages/application/wb_autoanswers_media.py` |
| Versioned Python/Node input boundary | `packages/application/wb_autoanswers_node_bridge.py` |
| Untouched frozen package | `packages/node/wb_autoanswers_v1_4_2/make_mvp/` |
| Processing/publication workers | `packages/application/wb_autoanswers_worker.py`, `wb_autoanswers_publication.py` |
| Bounded coordinator tick | `packages/application/wb_autoanswers_coordinator.py` |
| GET-only sync and manual media canary | `apps/wb_autoanswers_readonly.py` |
| Lifecycle/current-schema backup gate | `apps/wb_autoanswers_activation.py` |
| Authenticated production UI Flow | `apps/wb_autoanswers_production_ui_flow.py` |
| Backend/UI | `registry_upload_http_entrypoint.py`, `sheet_vitrina_v1_web_vitrina.html` |

## Data model and versioning

All schema changes are additive in the existing runtime SQLite database. The canonical tables cover feedbacks, immutable feedback versions, media, sync runs/cursors/commands, AI jobs, publication jobs/attempts, budget reservations, backlog previews and audit. Schema v3 introduced media/policy epochs; schema v4 adds incident controls and bounded lazy materialization. Schema v5 additionally adds:

- canonical `content_classification` on the current feedback content version;
- immutable `content_classification_at_preview` on each transition-run member;
- content-class/date indexes used by preview, reconciliation, processing and publication ordering;
- conservative v2-result quarantine when old `rating_only_template` evidence no longer satisfies the v3 classification.

Schema v4 fields retained by v5 include:

- `policy_epoch` to settings, processing jobs and publication jobs;
- media preview metadata and media processing version;
- `regeneration_required` evidence;
- append-only AI result revisions and historical cost events;
- actor-bound automated-mode previews;
- leased, resumable policy reconciliation sweeps;
- hourly/throughput/run caps and reservation expiry/release evidence;
- transition-run identity, bounded materialization cursor and visible pause reason;
- processing kind for deterministic zero-cost templates;
- append-only budget corrections and runtime scheduler timestamps.

`content_version_hash` includes text, pros, cons, rating, tags, product identity and stable media identity. It excludes answers, `wasViewed`, WB service state and temporary media query/fragment signatures. `wb_observation_hash` owns those service observations. Therefore signed-link rotation and WB state/readback changes do not create a paid semantic version.

Processing idempotency is `feedback_id | content_version | 1.4.2`. Publication idempotency is `feedback_id | content_version | normalized-final-reply-sha256 | create-answer-v1`. One feedback version can have at most one publication aggregate.

## Sync and modes

Initial history begins at `2026-01-01`. Answered, unanswered and archive streams use durable cursors; backfill never creates AI jobs. Steady sync has a 48-hour overlap and upserts before eligibility decisions. `429`, `5xx` and transport failures do not advance an incomplete cursor.

The persisted default remains OFF and `WB_AUTOANSWERS_FORCE_OFF=true` always has highest priority:

- `off`: sync/UI/readback continue; new AI claims and new WB writes stop;
- `manual`: no background AI generation; explicit generate/regenerate/review/publish only;
- `draft_only`: eligible scoped reviews receive reusable drafts, never publication;
- `auto_safe`: only `public_only`, `wb_return`, `wb_support` and the exact owner-approved `rating_only_template` may auto-publish;
- `auto_all`: every route that passes all hard gates may publish, except `seller_chat`, fallback, unsafe, stale, external-answer or media-uncertain results.

Entering `draft_only`, `auto_safe` or `auto_all` requires an actor-bound expiring preview over unanswered history from `2026-01-01`. It reports exact total, `content_bearing`, `rating_only`, indeterminate/manual-review rows, current content drafts, content requiring OpenAI or regeneration, expected WB writes per category, zero-cost templates, cost, content/full ETA, hourly/daily/monthly caps and the mandatory run cap. Preview and apply share one immutable membership/classification snapshot. Reviews observed after preview remain outside that run and require a new preview. Apply creates a new `policy_epoch`, transition run and resumable lazy sweep. Replaying the same consumed preview is an exact no-op; a fresh owner-confirmed capped preview creates a new run even when the selected automatic mode is unchanged.

Every automatic stage uses the same strict ordering: `content_bearing` before `rating_only`; within a class `created_at_wb DESC`, falling back to `first_seen_at DESC`, then `feedback_id DESC`. This governs snapshot ordinals, reconciliation, lazy materialization, processing/retry/expired-lease claims, ready-result reuse, publication enqueue and publication claims. Rating does not participate. Explicit manual work retains its separate owner-triggered semantics.

`rating_only` cannot materialize, claim or begin a new WB write while a scoped `content_bearing` member still has an automatic next step, including regeneration, retry/backoff, budget wait, publication or readback. Budget/run caps do not open the empty-review lane. `needs_review`, terminal/hard-gate outcomes, external answers and policy-ineligible publication do not hold the barrier forever. Old rating-only jobs remain immutable evidence but are excluded from current content capacity accounting and cannot be claimed ahead of content. A write already started is the sole safety exception: mandatory GET readback remains first and never creates a second POST.

Current valid drafts are reused, in-flight jobs are not duplicated, stale results are quarantined, existing WB answers skip permanently, and published answers are never recreated. Downgrades immediately invalidate old-epoch pre-write claims without making preserved work terminal.

## Budgets and OpenAI UI

Defaults are `$0.50/hour`, `$5/day`, `$50/month`, 20 paid reviews/hour, paid-review concurrency 1, in-flight role-call concurrency 1, queue depth 5 and a `$0.10` atomic review reservation after removal of repeated media bytes. `BEGIN IMMEDIATE` makes reservation/claim concurrency-safe. Actual settled usage plus archived regeneration/failed-call cost and append-only corrections is retained per review. The reservation records the provider-call boundary: retry, timeout, terminal failure and lease loss release unused capacity, while a lease lost after provider entry latches paid processing fail-closed as `budget_state_unknown` until cost reconciliation.

The legacy incident's unsupported `$1.00` settlement is removed from confirmed actual by an append-only adjustment but remains displayed as `Legacy без usage` and conservatively held against the applicable caps. It is never silently relabelled as measured provider cost.

The UI shows hourly/daily/monthly actual spend, active reserved spend, remaining caps, current run spend, last update and the official billing link. Queue progress is split into visually separate `Все отзывы` and `Отзывы с содержанием` cards. Each contains preparation and readback-confirmed publication stages with exact percent, `X из Y`, remaining, status and pause reason; the content card additionally shows `needs_review`, current operation, throughput and ETA. A zero denominator is `Нет отзывов в этой категории`, never a false 100%. Manual mode retains the durable counters and displays `Приостановлено вручную`.

Policy v3 classifies a current version as `content_bearing` when trimmed text, pros, cons, any non-empty tag, photo or video exists. Canonical media rows and `has_photo`/`has_video` are conservative positive evidence. Only a review with none of those surfaces and a rating 1–5 is `rating_only`; malformed or contradictory data is `indeterminate` and fails closed to review. True `rating_only` continues to use the unchanged owner-approved v2 template contract, costs `$0` and never invokes Node/OpenAI. The frozen v1.4.2 prefilter is not modified.

## Media

Media accepts HTTPS only from explicit WB/CDN suffixes including observed `*.geobasket.ru` photo hosts and `*.wbbasket.ru` video hosts. The initial URL and every redirect are allowlisted and DNS-checked for exclusively public addresses. Signed URLs and query signatures never appear in normal API/UI evidence. Limits are 20 MiB per photo, aggregate 100 MiB per video, bounded wall time and private `0600` storage.

MP4 and WB HLS master/variant playlists are supported. HLS selects the first variant and at most four evenly spaced segments deterministically. Each short HLS segment yields its first decodable frame (`select=eq(n,0)`), avoiding a successful zero-frame result from a 15-second cadence on 4–5 second MPEG-TS segments with absolute timestamps. Multi-frame MP4 sampling keeps the bounded cadence. ffmpeg produces at most four JPEG frames. A WB preview is fetched when available; otherwise the first deterministic frame becomes the preview without claiming the complete video was viewed.

Validated photos, preview and frames are encoded as review-specific classifier inputs after the cache breakpoint. Binary data URLs are replaced by short stable attachment references in tagged request text, preventing the same base64 bytes from being billed again in classifier, writer and validator context. Missing/invalid media stops before any paid AI call, releases its reservation and stores `media_uncertain + regeneration_required + needs_review`. Old unpublished text-only media failures are archived on regeneration; their cost remains accounted. TTL cleanup resets matching DB rows before removing private files, so absent bytes can never retain `downloaded` status.

## Publication safety

Before any POST, the repository atomically rechecks effective ON, current `policy_epoch`, permission, content version/hash, no external WB answer, exact reply/hash, frozen identity, JSON contract, hard gates, final guard, no fallback/media uncertainty/regeneration requirement and idempotency. Manual publication additionally requires current manual mode, preserved reviewed edit revision and permission readback.

`seller_chat` is review-only, requires exactly one deterministic case code, and its public text cannot ask for photo, video, screenshot, label, proof or other materials. No money, replacement, compensation, return approval or WB decision promise is introduced by server policy.

Exact publication evidence is committed before transport. Every HTTP success/error/timeout goes to `publish_pending_readback`; 204 alone is never proof. Exact normalized detail readback is the only path to `published`. Missing/different/external answers go to review. A possible write is never blindly repeated.

## UI and API

Legacy `GET /v1/sheet-vitrina-v1/feedbacks` is unchanged. Autoanswers responses use additive contract `wb_autoanswers_server_v3`; local list/filter/detail/settings expose the canonical classification and exact all/content progress counters. Additive routes include local list/detail/settings/sync, automated transition preview, manual generate/regenerate/edit, review approval and authenticated private media GET.

The first `Отзывы → Отзывы` screen reads SQLite immediately, defaults to 50 rows and uses server pagination/filters, including `Без ответа Wildberries`, server-side `Ответ системы` states and `content_bearing`/`rating_only`/`indeterminate` classification. Table system replies remain in a fixed-height dark internal scroller with a copy-only button. Missing replies have a compact neutral state. The obsolete independent `Исторический backlog` control is hidden and disabled; its legacy backend routes fail closed so it cannot bypass the capped preview-bound transition action.

Detail keeps only rating/date, review, non-empty pros/cons/tags, product identity, customer media, WB answer, AI reply, friendly status and actions visible. Routes, raw states, case code, attempts, cost, warnings, contracts, guards, media uncertainty, hashes, idempotency and audit are in closed-by-default `Техническая информация`. Before generation the same technical fields remain named explicitly with a `не запускался` state, rather than disappearing or implying a passed check. The full-width reply textarea auto-grows on render, generation, input and detail refresh. Desktop and 390px narrow behavior are browser-tested.

Permissions remain server-enforced:

- `feedbacks`: list/detail/media/status;
- `feedbacks.ai_review`: manual generation, regeneration, edit guard and publication enqueue;
- `feedbacks.autoanswers_admin`: master/modes, transition preview/policy and budgets.

Every mutation requires JSON, same-origin CSRF evidence and the relevant capability. HTTP handlers only enqueue durable work; they never perform an inline WB write.

## Deploy, verification and rollback

Deploy verifies Node >=20, npm, ffmpeg, lockfile install and all frozen hashes. For an unapplied schema version it temporarily uses process-local force-off, creates a coherent current-version backup with `PRAGMA integrity_check=ok`, then applies DDL atomically. Existing production manual state and all owner-published data/audit remain unchanged.

Lifecycle `status` is strictly observational: if the target schema is absent (including an absent database), it reports `schema_preparation_required` from read-only inspection and never constructs the schema-owning repository. Only `prepare-deploy` may apply additive DDL. If a complete raw current-schema pre-deploy snapshot remains after an interrupted capacity run, the next preparation compresses only that owned snapshot, verifies SQLite integrity, zstd integrity, archive hash and exact decompressed SHA-256, publishes the v5 manifest, reads it back through the canonical verifier, and only then removes the raw snapshot and its sidecars. A failed verification leaves the raw source recoverable.

After that current v5 restore proof, capacity recovery may remove only the minimum exact older autoanswers archive+manifest pairs needed to restore the 256 MiB operational headroom. Each candidate is confined to an older `wb_autoanswers_schema_vN` directory, must match its manifest size/hash/integrity contract, and is bound into a private cleanup audit before unlink. Unrelated files and the current v5 backup are never candidates. Cleanup stops after the first sufficient pair; failure to reach headroom remains fail-closed.

Required local checks:

```bash
PYTHONPATH=. python3 -m unittest \
  apps.wb_autoanswers_activation_test \
  apps.wb_autoanswers_runtime_test \
  apps.wb_autoanswers_sync_test \
  apps.wb_autoanswers_node_bridge_test \
  apps.wb_autoanswers_media_worker_test \
  apps.wb_autoanswers_publication_test \
  apps.wb_autoanswers_http_ui_test \
  apps.wb_autoanswers_readonly_test \
  apps.wb_autoanswers_release_safety_test \
  apps.wb_autoanswers_incident_regression_test \
  apps.wb_autoanswers_ui_browser_test
python3 -m compileall -q apps packages
```

Production acceptance keeps effective manual, performs one GET/media-only `manual-media-canary`, then read-only authenticated UI Flow. Detail media starts loading when its card opens; the flow waits for successful image decoding and records only redacted private-asset HTTP/MIME evidence. It must prove a real photo, real video preview/frames, compact/narrow UI, zero 5xx/page/console errors, zero claimable background AI jobs and zero new publication attempts. It must not click generation/regeneration/publication or switch into an automated mode.

Emergency rollback sets `WB_AUTOANSWERS_FORCE_OFF=true`. Code can roll back while additive tables remain inert. Restore the verified pre-v5 database only for demonstrated corruption and only after reconciling any ambiguous publication by GET. Never delete audit/revisions or replay a WB POST to simulate rollback.

After acceptance the sole owner step is to open a new automated-mode transition preview and inspect exact scope, `content_bearing`/`rating_only` distribution, estimated cost, ETA and run cap without confirming the run.
