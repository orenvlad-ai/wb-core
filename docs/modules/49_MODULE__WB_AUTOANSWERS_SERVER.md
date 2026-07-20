---
title: "WB Autoanswers Server v1"
doc_id: "49_MODULE__WB_AUTOANSWERS_SERVER"
doc_type: "module"
status: "manual_media_and_policy_reconciliation_release_candidate"
purpose: "Server-native synchronization, frozen AI drafting and readback-confirmed WB answer publication"
scope: "SellerOS / wb-core feedbacks section"
source_basis: "Owner decisions plus frozen AI bundle v1.4.2"
source_of_truth_level: "implementation contract"
update_note: "Production remains manual. Schema v3 adds safe WB media/HLS ingestion, media-aware regeneration, compact UI and preview-bound policy-epoch reconciliation."
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

All schema changes are additive in the existing runtime SQLite database. The canonical tables cover feedbacks, immutable feedback versions, media, sync runs/cursors/commands, AI jobs, publication jobs/attempts, budget reservations, backlog previews and audit. Schema v3 adds:

- `policy_epoch` to settings, processing jobs and publication jobs;
- media preview metadata and media processing version;
- `regeneration_required` evidence;
- append-only AI result revisions and historical cost events;
- actor-bound automated-mode previews;
- leased, resumable policy reconciliation sweeps.

`content_version_hash` includes text, pros, cons, rating, tags, product identity and stable media identity. It excludes answers, `wasViewed`, WB service state and temporary media query/fragment signatures. `wb_observation_hash` owns those service observations. Therefore signed-link rotation and WB state/readback changes do not create a paid semantic version.

Processing idempotency is `feedback_id | content_version | 1.4.2`. Publication idempotency is `feedback_id | content_version | normalized-final-reply-sha256 | create-answer-v1`. One feedback version can have at most one publication aggregate.

## Sync and modes

Initial history begins at `2026-01-01`. Answered, unanswered and archive streams use durable cursors; backfill never creates AI jobs. Steady sync has a 48-hour overlap and upserts before eligibility decisions. `429`, `5xx` and transport failures do not advance an incomplete cursor.

The persisted default remains OFF and `WB_AUTOANSWERS_FORCE_OFF=true` always has highest priority:

- `off`: sync/UI/readback continue; new AI claims and new WB writes stop;
- `manual`: no background AI generation; explicit generate/regenerate/review/publish only;
- `draft_only`: eligible scoped reviews receive reusable drafts, never publication;
- `auto_safe`: only `public_only`, `wb_return`, `wb_support` may auto-publish;
- `auto_all`: every route that passes all hard gates may publish, except `seller_chat`, fallback, unsafe, stale, external-answer or media-uncertain results.

Entering `draft_only`, `auto_safe` or `auto_all` requires an actor-bound expiring preview over unanswered history from `2026-01-01`. It reports total, current drafts, new generation, media regeneration, automatic publication, manual review, estimated cost and budget impact. Apply creates a new `policy_epoch` and one resumable sweep. Reapplying the same target is an exact no-op.

Sweep priority is manual-started work, completed drafts, repairable in-flight work, media regeneration, then untouched reviews newest-first. Current drafts are reused, in-flight jobs are not duplicated, existing WB answers skip permanently, and published answers are never recreated. Downgrades immediately invalidate old-epoch pre-write claims. A possible write already started remains readback-only.

## Budgets and OpenAI UI

Defaults are `$5/day`, `$50/month`, 70% warning and a conservative `$1` claim reservation. `BEGIN IMMEDIATE` makes reservation/claim concurrency-safe. Actual settled usage plus archived regeneration cost is retained per review.

The UI shows actual local daily/monthly spend, caps, last update and the official billing link. It never calls undocumented billing endpoints and never labels local usage as a credit balance. Without an already configured official Admin API integration it states: `Точный остаток доступен в кабинете OpenAI`.

## Media

Media accepts HTTPS only from explicit WB/CDN suffixes including observed `*.geobasket.ru` photo hosts and `*.wbbasket.ru` video hosts. The initial URL and every redirect are allowlisted and DNS-checked for exclusively public addresses. Signed URLs and query signatures never appear in normal API/UI evidence. Limits are 20 MiB per photo, aggregate 100 MiB per video, bounded wall time and private `0600` storage.

MP4 and WB HLS master/variant playlists are supported. HLS selects the first variant and at most four evenly spaced segments deterministically. ffmpeg produces at most four JPEG frames. A WB preview is fetched when available; otherwise the first deterministic frame becomes the preview without claiming the complete video was viewed.

Validated photos, preview and frames are encoded as review-specific classifier inputs after the cache breakpoint. Missing/invalid media stops before any paid AI call, releases its reservation and stores `media_uncertain + regeneration_required + needs_review`. Old unpublished text-only media failures are archived on regeneration; their cost remains accounted. TTL cleanup resets matching DB rows before removing private files, so absent bytes can never retain `downloaded` status.

## Publication safety

Before any POST, the repository atomically rechecks effective ON, current `policy_epoch`, permission, content version/hash, no external WB answer, exact reply/hash, frozen identity, JSON contract, hard gates, final guard, no fallback/media uncertainty/regeneration requirement and idempotency. Manual publication additionally requires current manual mode, preserved reviewed edit revision and permission readback.

`seller_chat` is review-only, requires exactly one deterministic case code, and its public text cannot ask for photo, video, screenshot, label, proof or other materials. No money, replacement, compensation, return approval or WB decision promise is introduced by server policy.

Exact publication evidence is committed before transport. Every HTTP success/error/timeout goes to `publish_pending_readback`; 204 alone is never proof. Exact normalized detail readback is the only path to `published`. Missing/different/external answers go to review. A possible write is never blindly repeated.

## UI and API

Legacy `GET /v1/sheet-vitrina-v1/feedbacks` is unchanged. Additive routes include local list/detail/settings/sync, automated transition preview, manual generate/regenerate/edit, review approval and authenticated private media GET.

The first `Отзывы → Отзывы` screen reads SQLite immediately, defaults to 50 rows and uses server pagination/filters. Table system replies remain in a fixed-height internal scroller with a copy-only button. Missing replies have a compact neutral state.

Detail keeps only rating/date, review, non-empty pros/cons/tags, product identity, customer media, WB answer, AI reply, friendly status and actions visible. Routes, raw states, case code, attempts, cost, warnings, contracts, guards, media uncertainty, hashes, idempotency and audit are in closed-by-default `Техническая информация`. The full-width reply textarea auto-grows on render, generation, input and detail refresh. Desktop and 390px narrow behavior are browser-tested.

Permissions remain server-enforced:

- `feedbacks`: list/detail/media/status;
- `feedbacks.ai_review`: manual generation, regeneration, edit guard and publication enqueue;
- `feedbacks.autoanswers_admin`: master/modes, transition preview/policy and budgets.

Every mutation requires JSON, same-origin CSRF evidence and the relevant capability. HTTP handlers only enqueue durable work; they never perform an inline WB write.

## Deploy, verification and rollback

Deploy verifies Node >=20, npm, ffmpeg, lockfile install and all frozen hashes. For an unapplied schema version it temporarily uses process-local force-off, creates a coherent current-version backup with `PRAGMA integrity_check=ok`, then applies DDL atomically. Existing production manual state and all owner-published data/audit remain unchanged.

Lifecycle `status` is strictly observational: if the target schema is absent (including an absent database), it reports `schema_preparation_required` from read-only inspection and never constructs the schema-owning repository. Only `prepare-deploy` may apply additive DDL. If a complete raw current-schema pre-deploy snapshot remains after an interrupted capacity run, the next preparation compresses only that owned snapshot, verifies SQLite integrity, zstd integrity, archive hash and exact decompressed SHA-256, publishes the v3 manifest, reads it back through the canonical verifier, and only then removes the raw snapshot and its sidecars. A failed verification leaves the raw source recoverable.

After that current v3 restore proof, capacity recovery may remove only the minimum exact older autoanswers archive+manifest pairs needed to restore the 256 MiB operational headroom. Each candidate is confined to an older `wb_autoanswers_schema_vN` directory, must match its manifest size/hash/integrity contract, and is bound into a private cleanup audit before unlink. Unrelated files and the current v3 backup are never candidates. Cleanup stops after the first sufficient pair; failure to reach headroom remains fail-closed.

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
  apps.wb_autoanswers_ui_browser_test
python3 -m compileall -q apps packages
```

Production acceptance keeps effective manual, performs one GET/media-only `manual-media-canary`, then read-only authenticated UI Flow. It must prove a real photo, real video preview/frames, compact/narrow UI, zero 5xx/page/console errors, zero claimable background AI jobs and zero new publication attempts. It must not click generation/regeneration/publication or switch into an automated mode.

Emergency rollback sets `WB_AUTOANSWERS_FORCE_OFF=true`. Code can roll back while additive tables remain inert. Restore the verified pre-v3 database only for demonstrated corruption and only after reconciling any ambiguous publication by GET. Never delete audit/revisions or replay a WB POST to simulate rollback.

After acceptance the owner may open an unpublished quarantined review and click `Перегенерировать с учётом медиа`; ordinary eligible reviews use `Сгенерировать ответ`. Publication remains a separate explicit confirmation with mandatory readback.
