---
title: "WB Autoanswers Server v1"
doc_id: "49_MODULE__WB_AUTOANSWERS_SERVER"
doc_type: "module"
status: "feature_owned_lifecycle"
purpose: "Server-native synchronization, frozen AI drafting and readback-confirmed WB answer publication"
scope: "SellerOS / wb-core feedbacks section"
source_basis: "Owner decisions plus frozen AI bundle v1.4.2"
source_of_truth_level: "implementation contract"
update_note: "Schema v8 adds exact per-sweep/member/version reconciliation acknowledgements, action-first materialization, terminal/human-only barrier exclusion, truthful stall telemetry and fingerprint-bound incident recovery while preserving immutable execution evidence, schema-v7 rolling admission, the frozen bundle and the owner-confirmed active run cap."
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

Feature settings are the sole owner of the persisted business mode; the generic
auto-updates owner policy is not an Autoanswers intent source. `WB_AUTOANSWERS_FORCE_OFF=true`
still has the highest emergency priority. In `manual`, steady sync creates no
automatic AI jobs: only an explicit per-review action can enqueue generation and
only a separately confirmed action can enqueue publication. Make, Telegram, WB
answer PATCH, inline HTTP-to-WB write, and any silent rewrite of frozen
prompts/contracts/guards/golden/fallbacks remain absent.

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
| Feature-owned systemd reconciliation/readback | `packages/application/wb_autoanswers_lifecycle.py`, `apps/wb_autoanswers_lifecycle.py` |
| Current-schema backup gate | `apps/wb_autoanswers_activation.py` |
| Incident evidence and bounded recovery | `apps/wb_autoanswers_incident_evidence.py`, `apps/wb_autoanswers_budget_reconciliation.py`, `apps/wb_autoanswers_prefilter_skip_recovery.py`, `apps/wb_autoanswers_rolling_recovery.py`, `apps/wb_autoanswers_reconciliation_recovery.py` |
| Authenticated production UI Flow | `apps/wb_autoanswers_production_ui_flow.py` |
| Backend/UI | `registry_upload_http_entrypoint.py`, `sheet_vitrina_v1_web_vitrina.html` |

## Data model and versioning

Autoanswers persistence is physically isolated in `<runtime_dir>/wb_autoanswers_runtime.sqlite3`. The legacy autoanswer tables in `registry_upload_runtime.sqlite3` are migration/rollback evidence only and are no longer opened by the worker, readonly sync, lifecycle or recovery paths after cutover. The canonical tables cover feedbacks, immutable feedback versions, media, sync runs/cursors/commands, AI jobs, publication jobs/attempts, budget reservations, backlog previews and audit. Schema v3 introduced media/policy epochs; schema v4 adds incident controls and bounded lazy materialization. Schema v5 additionally adds:

- canonical `content_classification` on the current feedback content version;
- immutable `content_classification_at_preview` on each transition-run member;
- content-class/date indexes used by preview, reconciliation, processing and publication ordering;
- conservative v2-result quarantine when old `rating_only_template` evidence no longer satisfies the v3 classification.

Schema v6 retains the v5 classification and immutable initial membership model and adds
append-only `budget_uncertainty_holds`. A hold is a conservative cap reservation,
not asserted actual spend. It is created only from an exact dry-run fingerprint
when local evidence proves that a provider call started but no usage/cost
readback exists.

Schema v7 adds:

- append-only `rolling_admissions`, keyed idempotently by transition run,
  feedback and exact content version/hash;
- one bounded admission cursor per sweep over the indexed immutable
  feedback-version log;
- admission source, source sync run, version time, classification/rating and
  canonical evidence for every admitted version;
- append-only per-attempt provider uncertainty evidence for bounded opaque
  Node exits;
- indexes used by admission and the literal cross-stage priority barrier.

Schema v8 adds one append-only
`sheet_vitrina_v1_wb_autoanswers_reconciliation_acknowledgements` row for each
exact `sweep_id + feedback_id + content_version`. The row binds the content
hash, policy epoch, transition run, outcome class, member fingerprint and
acknowledgement time. A preserved publication, terminal/human-only result or
already-current job therefore advances a sweep exactly once without changing
its immutable job/publication/readback/cost identity.

The schema-v8 migration does not rewrite the initial membership, content
versions, frozen bundle identity, policy epoch, transition run or cap. Schema
v4 fields retained by v8 include:

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

- `off`: readonly sync/UI/readback continue; worker timer, new AI claims and new WB writes stop;
- `manual`: readonly sync and worker run, but only explicit generate/regenerate/review/publish jobs are serviced;
- `draft_only`: eligible scoped reviews receive reusable drafts, never publication;
- `auto_safe`: only `public_only`, `wb_return`, `wb_support` and the exact owner-approved `rating_only_template` may auto-publish;
- `auto_all`: every route that passes all hard gates may publish, except `seller_chat`, fallback, unsafe, stale, external-answer or media-uncertain results.

The dedicated lifecycle maps those modes to two components. Readonly sync is
enabled for every mode except a global master pause; the worker is enabled for
`manual`, `draft_only`, `auto_safe` and `auto_all`. The global master is a
cross-system suspension only: it disables both actual components while
preserving feature intent, and resume reconciles the latest feature mode rather
than a stale generic desired flag. A mutation is not confirmed by the SQLite
mode write alone. It must read back settings, required `policy_epoch`, exact
transition run and run cap, timer enabled/active states and absence of lifecycle
drift. Until the first post-request scheduler tick the operational state is
`starting`; after the grace interval a stale tick is `worker_unavailable`, not
healthy.

The force-off readonly runner proves its own safety from the causal `enqueued=0`
result of every sync tick plus its provider/writer-free import boundary. It does
not compare store-wide AI/publication queue totals, because the independently
scheduled worker can legitimately advance those shared queues during the same
GET window; that concurrent progress is not attributed to the readonly sync.

Entering `draft_only`, `auto_safe` or `auto_all` requires an actor-bound expiring preview over unanswered history from `2026-01-01`. It reports exact total, `content_bearing`, `rating_only`, indeterminate/manual-review rows, current content drafts, content requiring OpenAI or regeneration, expected WB writes per category, zero-cost templates, cost, content/full ETA, hourly/daily/monthly caps and the mandatory run cap. Preview and apply share one immutable membership/classification snapshot; that initial snapshot remains the owner-confirmed audit proof.

While the resulting automatic run remains active, each ordinary bounded
scheduler tick scans the indexed feedback-version cursor and append-only admits
new current unanswered versions. It never recalculates or reorders the initial
snapshot. A repeated tick is a no-op, restart resumes from the durable
watermark, and a changed content version receives its own hash-bound admission
only while it is current and unanswered. Admission does not create a provider
call, WB write or publication aggregate; normal reconciliation materializes
the exact admitted version under existing idempotency keys. The admission
shares the remaining owner-confirmed run cap and every global/hourly/daily/
monthly/review limit. It never enlarges or renews a cap.

Apply creates a new `policy_epoch`, transition run and resumable live sweep.
The sweep remains active while any admitted automatic action is pending, so a
later admission can re-enter materialization without a new preview. Replaying
the same consumed preview is an exact no-op; a fresh owner-confirmed capped
preview creates a new run even when the selected automatic mode is unchanged.

Every automatic stage uses one strict order. `content_bearing` is split into
rating buckets `1`, `2`, `3`, `4`, `5` in that order. Within each bucket the
deterministic tie-break is `created_at_wb DESC`, falling back to
`first_seen_at DESC`, then `feedback_id DESC`. Only after every currently
admitted content bucket has no claimable or pending automatic step may
non-content work proceed; the existing deterministic non-content order remains
`indeterminate` review evidence followed by the final `rating_only` block,
newest first with the same fallback/tie-break. A newly admitted higher-priority
version preempts every not-yet-started lower bucket at the next safe claim.
This single order governs initial ordinals, rolling reconciliation, lazy
materialization, processing/retry/expired-lease claims, ready-result reuse,
publication enqueue and publication claims. Explicit manual work retains its
separate owner-triggered semantics.

`rating_only` cannot materialize, claim or begin a new WB write while an
admitted `content_bearing` member still has an automatic next step, including
regeneration, retry/backoff, budget wait, publication or readback. The same
barrier prevents a prepared 5★ publication while any admitted automatic 1★
action remains unfinished. Budget/run caps do not open a lower-priority lane.
`needs_review`, terminal/hard-gate outcomes, external answers and
policy-ineligible publication do not hold the barrier forever. Old rating-only
jobs remain immutable evidence but are excluded from current content capacity
accounting and cannot be claimed ahead of content. An in-flight provider call
or WB write is not interrupted. A write already started retains mandatory GET
readback priority and never creates a second POST.

Reconciliation selects candidates action-first. Exact automatic actions in the
current priority bucket are considered before lower-value preserved/unchanged
bookkeeping, even when the preserved row is an older 1-star publication.
Terminal/human-only rows remain visible evidence but are excluded from the
automatic barrier. Sweep progress is rebuilt from unique acknowledgements,
not per-tick return counts: a restart or repeated candidate is idempotent and
cannot report another synthetic `+N/min`. The runtime exposes acknowledged,
action/preserved/unchanged, remaining, recent delta rate, ETA, repeated-batch
fingerprint, priority-bucket age, real AI/WB throughput and sanitized SQLite
contention evidence. A budget-free automatic bucket with no claimable action
or real output for 15 minutes is surfaced as a stall; ordinary budget,
rate-limit and retry-backoff pauses remain ordinary pauses.

Current valid drafts are reused, in-flight jobs are not duplicated, stale results are quarantined, existing WB answers skip permanently, and published answers are never recreated. Downgrades immediately invalidate old-epoch pre-write claims without making preserved work terminal.

## Budgets and OpenAI UI

Defaults are `$0.50/hour`, `$5/day`, `$50/month`, 20 paid reviews/hour, paid-review concurrency 1, in-flight role-call concurrency 1, queue depth 5 and a `$0.10` atomic review reservation after removal of repeated media bytes. `BEGIN IMMEDIATE` makes reservation/claim concurrency-safe. Actual settled usage plus archived regeneration/failed-call cost and append-only corrections is retained per review. The reservation records the provider-call boundary: retry, timeout, terminal failure and lease loss release unused capacity, while a lease lost after provider entry latches paid processing fail-closed as `budget_state_unknown` until cost reconciliation.

The admin-only operator settings envelope is server-owned and applies to the
existing persisted schema-v7 settings row:

| Limit | Minimum | Maximum |
| --- | ---: | ---: |
| USD/hour | `$0.01` | `$10.00` |
| USD/day | `$0.01` | `$50.00` |
| USD/month | `$0.01` | `$500.00` |
| paid reviews/hour | `1` | `200` |
| concurrent paid reviews | `1` | `4` |
| concurrent role calls | `1` | `8` |
| materialized processing queue depth | `1` | `100` |

Every value must be finite and positive. Monetary limits additionally satisfy
`hour <= day <= month`; both concurrency limits must not exceed the processing
queue depth. The server rejects zero, negative, fractional integer values,
`NaN`, infinities, out-of-envelope and contradictory combinations. Settings
survive service restart and deploy because the UI and API mutate only the
server-owned SQLite row; no browser-local state is authoritative.

Global limit changes do not create a new transition run, change its immutable
initial membership or modify its owner-confirmed `run_max_usd` /
`run_max_paid_reviews`. A scheduler claim always re-evaluates current global
budget settings. Therefore a run paused only by `hourly_budget_reached`,
`daily_budget_reached` or `monthly_budget_reached` resumes on the next normal
worker tick after a sufficient increase. A decrease below already consumed or
reserved usage preserves all evidence and leaves the corresponding pause in
place. Run caps, `budget_state_unknown`, provider quota, holds, reservations and
other safety gates remain effective and are never cleared by a settings write.

The legacy incident's unsupported `$1.00` settlement is removed from confirmed actual by an append-only adjustment but remains displayed as `Legacy без usage` and conservatively held against the applicable caps. It is never silently relabelled as measured provider cost. A current provider-started crash boundary is reconciled only by `budget_reconciliation_v1`: the read-only plan binds the exact reservation/job evidence and maximum per-review reservation into a fingerprint; apply appends the conservative hold and audit, then clears `budget_state_unknown` only after readback proves no unresolved boundary. It never writes zero or guessed actual cost.

The reconciliation plan also exposes its pre-change digest, exact affected-row
counts and named non-target invariants for provider calls, cost events, WB
writes and the immutable reservation/job evidence. Apply returns the actual
affected counts and readback. Re-applying an already consumed
exact fingerprint returns a confirmed idempotent no-op only when every
fingerprint-bound hold and audit row still exists and no provider-cost
uncertainty remains. A different or stale fingerprint still fails closed.

An opaque Node child exit after the provider boundary does not guess usage and
does not discard the review. The runtime releases the active reservation,
appends one maximum-reservation uncertainty hold for that exact attempt and
stores only return code, byte counts and SHA-256 diagnostics; raw child output
is not persisted. Attempt one becomes `retryable_error` with bounded backoff.
A second opaque failure appends its own hold and becomes
`needs_review/*_repeated_needs_review`. The contained result is returned to the
worker coordinator, so an isolated, durably recorded failure does not make the
whole oneshot exit. Existing quota, budget and other stronger pause reasons are
never cleared by this path. Valid partial usage follows the existing failed-cost
event path instead. Both legacy and per-attempt holds count against the same
global and transition-run caps.

A completed frozen `skipped` result is terminal for the immutable
content/bundle identity. Policy-epoch reconciliation adopts the new epoch/run
metadata without queuing that processing key again or reopening its settled
zero-cost reservation. The bounded `prefilter_skip_recovery_v1` runner repairs
only rows already misclassified as `reservation_missing` when an earlier
`empty_five_star` skip, a settled zero-cost reservation, no cost events and no
publication are all proven. Dry-run is query-only and exact-row/fingerprint
bound; apply uses `BEGIN IMMEDIATE`, restores only the job projection, appends
audit, preserves all financial/provider/WB evidence and repeats as a no-op.
If that incident already latched `worker_error`, the separate
`prefilter_skip_latch_recovery_v1` phase releases only that exact
`reservation_missing` latch after the projection restore audit, zero active
reservations, zero processing jobs and zero unresolved provider uncertainty
are all read back. It has its own dry-run/fingerprint/apply/readback contract,
changes one runtime-state row plus one audit event, and preserves provider,
cost and WB evidence.

Policy v3 makes tags, photo and video authoritative content-bearing evidence
even when text/pros/cons are blank. The frozen v1.4.2 prefilter and all frozen
hashes remain untouched. The versioned server boundary marks such an exact
version as content-bearing, requires the existing media hard gate first, and
adapts only real WB tag/media facts into the frozen input so the frozen
classifier/writer/validator path can run. The adapter and source fields are
stored in result audit. It does not route the row to the rating-only template
and does not synthesize a publication outside the frozen guards.

`wb_autoanswers_rolling_recovery_v1` repairs already affected unpublished
content-bearing `empty_five_star` rows and every exact eligible legacy
`node_process_exit_1` row in the selected transition run. Dry-run/readback are
query-only. Apply requires the
verified schema-v7 pre-change backup, exact run/count/fingerprint coverage,
`BEGIN IMMEDIATE`, current version/hash, no publication/WB write, no cost event
and no revision conflict. It archives the prior job projection, requeues the
same processing identity under the unchanged frozen bundle, preserves every
legacy hold/cost/audit row and appends recovery audit. Replay is an exact no-op.

`wb_autoanswers_reconciliation_recovery_v1` repairs only exact preserved
members of a stalled active sweep after schema v8 is deployed. Dry-run and
readback use SQLite `mode=ro` plus `PRAGMA query_only=ON`. The approval
fingerprint binds the sweep/run/policy/caps, exact candidate execution
projection and verified pre-v8 backup. Apply uses `BEGIN IMMEDIATE`, inserts
only missing preservation acknowledgements, rebuilds the derived sweep
projection and appends one audit event. It never rewrites an AI job,
publication/readback/POST attempt, reservation, provider boundary, cost,
uncertainty hold or run limit. Readback verifies exact member fingerprints and
the immutable target execution projection plus sweep identity/caps. The apply
transaction separately proves its non-target snapshot unchanged; later normal
queue progress does not invalidate readback. Replay is a confirmed no-op.

Policy reconciliation never sends an immutable publication aggregate back
through regeneration. Existing `needs_review`/terminal evidence is adopted
before regeneration checks; any other unpublished publication-bound
regeneration candidate remains unchanged and receives only an exact preserved
acknowledgement for explicit operator handling. A clean scheduler tick may
release only a prior reconciliation-stage
`worker_error/publication_already_exists` presentation latch (including the
legacy stage-less form), and only after exact current-run scope readback proves
zero remaining stale publication-bound candidates, zero active reservations
and zero unresolved provider-cost boundaries. The release appends one audit
event and is idempotent. It does not clear `reservation_missing`,
`budget_state_unknown`, quota or other provider/safety latches.

The UI shows hourly/daily/monthly actual spend, active reserved spend, remaining caps, current run spend, last update and the official billing link. The main Autoanswers card has a visible `Настроить лимиты` action which opens one opaque dark modal for all seven global limits. The former technical disclosure links to that same modal instead of duplicating controls. An hourly/daily/monthly budget pause adds `Увеличить лимит`, opens the same modal, focuses the corresponding field and shows `used + reserved / current / new`. The modal displays the active transition-run cap separately as read-only with an explanation that an ordinary global-settings save cannot enlarge it. It also shows immutable initial membership, admitted-since-start totals by content class and rating, current exact total, last admission refresh/batch, current literal priority bucket and pause/error reason. Queue progress uses the full current admitted set and is split into visually separate `Все отзывы` and `Отзывы с содержанием` cards. Each contains preparation and readback-confirmed publication stages with exact percent, `X из Y`, remaining, status and pause reason; the content card additionally shows `needs_review`, current operation, throughput and ETA. A zero denominator is `Нет отзывов в этой категории`, never a false 100%. Manual mode retains the durable counters and displays `Приостановлено вручную`. Ordinary budget/rate/backoff pauses render as the yellow `Работает · штатная пауза`, even if the one-shot service's last exit presentation is error; genuine lifecycle drift and fatal reasons remain red.

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

Legacy `GET /v1/sheet-vitrina-v1/feedbacks` is unchanged. Autoanswers responses use additive contract `wb_autoanswers_server_v4`; local list/filter/detail/settings expose the canonical classification, rolling membership/current-priority evidence, exact all/content progress counters and server-owned lifecycle. Settings GET includes `settings_revision` and the authoritative operator-limit bounds. Every settings POST requires `expected_policy_epoch`; a global-limit mutation additionally requires the exact `expected_settings_revision`. The server hashes the complete persisted settings projection, rejects a stale epoch or revision, performs a fresh repository read after the write and returns only exact requested fields in `confirmed_limits`. The client reports success only when that readback equals every requested value. Automated apply additionally requires its preview and cannot be combined with a global-limit mutation. A successful result includes the persisted settings/run/cap readback plus lifecycle state. A partial systemd failure is an error, and a successfully enabled timer without a fresh tick is explicitly pending/starting. Additive routes include local list/detail/settings/sync, automated transition preview, manual generate/regenerate/edit, review approval and authenticated private media GET.

The first `Отзывы → Отзывы` screen reads SQLite immediately, defaults to 50 rows and uses server pagination/filters, including `Без ответа Wildberries`, server-side `Ответ системы` states and `content_bearing`/`rating_only`/`indeterminate` classification. Table system replies remain in a fixed-height dark internal scroller with a copy-only button. Missing replies have a compact neutral state. The obsolete independent `Исторический backlog` control is hidden and disabled; its legacy backend routes fail closed so it cannot bypass the capped preview-bound transition action.

The same screen is the only Autoanswers enable/disable surface and shows an
actual-runtime indicator: selected mode, readonly sync, worker, scheduler-tick
freshness, transition run, stop/pause reason, last error and budget state. It
cannot render a green full-mode state when a timer is inactive, the tick is
stale, lifecycle drift exists or budget truth is unknown. `Настройки →
Автообновления` exposes the same server-owned fields as a monitoring-only card
and has no Autoanswers individual mutation control.

The monitoring card reduces readback to explicit human states:
`Работает штатно`, `Запускается`, `Приостановлено пользователем`,
`Приостановлено общей паузой`, `Есть расхождение`, `Ошибка процесса`,
`Нет свежего подтверждения` and `Состояние неизвестно`. A yellow/error state
always includes its reason. The primary card shows desired/actual, last
successful cycle, scheduler freshness, pause/stop reason, last error and
readback source; Autoanswers additionally shows mode, readonly and worker
desired/actual pairs, lifecycle, policy revision/transition run and budget.
Conservative hold is explained as uncertainty reserved against a cap, not
confirmed provider spend. Service/timer IDs, raw reasons and fingerprints stay
inside a closed technical disclosure. Rendering or polling this card performs
no mode/policy/cap/hold mutation, provider call or WB write.

If the initial settings GET observes a desired runtime in `starting`, `error`
or lifecycle drift, the visible `Отзывы` tab performs a bounded refresh of at
most 30 additional settings GETs at two-second intervals. The refresh stops
when the tab is hidden, the subsection changes, the policy epoch changes or
server truth becomes stable. It performs no lifecycle, queue, provider or WB
mutation and cannot turn stale client state into a green badge without a fresh
server payload whose lifecycle is `running`, `actual=true` and
`drift_status=matched`.

Detail keeps only rating/date, review, non-empty pros/cons/tags, product identity, customer media, WB answer, AI reply, friendly status and actions visible. Routes, raw states, case code, attempts, cost, warnings, contracts, guards, media uncertainty, hashes, idempotency and audit are in closed-by-default `Техническая информация`. Before generation the same technical fields remain named explicitly with a `не запускался` state, rather than disappearing or implying a passed check. The full-width reply textarea auto-grows on render, generation, input and detail refresh. Desktop and 390px narrow behavior are browser-tested.

Permissions remain server-enforced:

- `feedbacks`: list/detail/media/status;
- `feedbacks.ai_review`: manual generation, regeneration, edit guard and publication enqueue;
- `feedbacks.autoanswers_admin`: master/modes, transition preview/policy and budgets.

Every mutation requires JSON, same-origin CSRF evidence and the relevant capability. HTTP handlers only enqueue durable work; they never perform an inline WB write.

## Deploy, verification and rollback

Deploy verifies Node >=20, npm, ffmpeg, lockfile install and all frozen hashes. The deploy-only quiet window records active Autoanswers timers, stops the worker/readonly timers and registry HTTP service, then copies all legacy Autoanswers tables from one query-only snapshot into a candidate isolated store. It verifies every table row count and deterministic row digest, foreign keys, integrity and an unchanged source `data_version`, fsyncs a `prepared` manifest before the atomic store rename, and restores the registry plus exactly the previously active timers; interrupted one-shot executions resume idempotently on those timers. An interrupted publish is accepted only after full store re-verification. Existing feature mode, `policy_epoch`, transition run, immutable initial membership, cap and all owner-published data/audit remain unchanged; migration must never infer Autoanswers intent from legacy generic owner-policy entries. Legacy main-DB tables are retained for bounded rollback.

Lifecycle `status` is strictly observational: if the target schema is absent (including an absent database), it reports `schema_preparation_required` from read-only inspection and never constructs the schema-owning repository. Only `prepare-deploy` may apply additive DDL. If a complete raw current-schema pre-deploy snapshot remains after an interrupted capacity run, the next preparation takes exclusive locking and checkpoints its committed WAL into the main snapshot before hashing or compression. It then compresses only that owned stable snapshot, verifies SQLite integrity, zstd integrity, archive hash and exact decompressed SHA-256, publishes the v8 manifest, reads it back through the canonical verifier, and only then removes the raw snapshot and its sidecars. A failed verification leaves the raw source recoverable.

If the live volume cannot hold a second raw database and neither a current nor
older recoverable Autoanswers backup exists, `prepare-deploy` keeps the
repo-owned service quiet window, acquires exclusive SQLite locking, checkpoints
the WAL, verifies the stable source, and streams that exact main-database image
directly to the private v8 zstd archive. The source database is never removed or
rewritten. Schema migration remains blocked until the zstd frame, archive hash,
exact decompressed source SHA-256, manifest and canonical verifier readback all
succeed; failed output from the attempt is removed while the source remains
unchanged.

After that current v8 restore proof, capacity recovery may remove only the minimum exact older autoanswers archive+manifest pairs needed to restore the 256 MiB operational headroom. Each candidate is confined to an older `wb_autoanswers_schema_vN` directory, must match its manifest size/hash/integrity contract, and is bound into a private cleanup audit before unlink. Unrelated files and the current v8 backup are never candidates. Cleanup stops after the first sufficient pair; failure to reach headroom remains fail-closed.

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
  apps.wb_autoanswers_lifecycle_test \
  apps.wb_autoanswers_readonly_test \
  apps.wb_autoanswers_release_safety_test \
  apps.wb_autoanswers_incident_regression_test \
  apps.wb_autoanswers_reconciliation_recovery_test \
  apps.wb_autoanswers_ui_browser_test \
  apps.wb_autoanswers_rolling_recovery_test
PYTHONPATH=. python3 apps/business_data_maintenance_status_smoke.py
python3 -m compileall -q apps packages
```

Production acceptance preserves the already confirmed feature intent and exact
transition run/cap. After deploy it runs exact query-only recovery planning,
applies only fingerprint-bound candidates after verified backup proof, and
reads back the recovery invariants. It reconciles any
`budget_state_unknown`, resumes through the dedicated lifecycle, proves both
component readbacks and a fresh scheduler tick, then observes ordinary queue
movement without synthetic OpenAI/WB writes. It must show a feedback version
first observed after the initial run start being automatically admitted,
selected at the next safe claim ahead of lower/rating-only work, published
through the normal policy path and confirmed by exact WB readback.
Authenticated UI Flow covers Settings monitoring/ownership and
the actual indicator in `Отзывы → Отзывы`, including a live `auto_all` mode
that requires master/effective ON, Full selector, lifecycle
`running/actual/matched`, both desired/actual/matched components and a fresh
scheduler tick. In live automatic mode the read-only Flow permits independent
background worker progress but performs zero business mutations itself. It
still requires compact/narrow/dark render and zero 5xx/page/console errors. Any
publication proof comes only from normal policy-allowed flow and mandatory WB
readback.

The default production UI Flow remains read-only. For this limit-control LOOP,
the explicit `--verify-limit-save` flag additionally opens the modal from the
main card, captures its opaque dark render, submits exactly the seven current
values once, confirms the new settings revision and exact readback, and proves
that every global value and the active transition-run cap stayed unchanged.
It never substitutes a dangerous value or creates a preview/run.

Emergency rollback sets `WB_AUTOANSWERS_FORCE_OFF=true`. Before older code is deployed, `autoanswers-store-rollback-plan` binds the current isolated and retained legacy table digests; exact-fingerprint `autoanswers-store-rollback-apply` enters the deploy quiet window, creates and verifies a private legacy-table snapshot, replaces only the Autoanswers table set in one transaction and proves digest/foreign-key readback. The current isolated source and all non-Autoanswers registry tables remain intact. Restore a verified database only for demonstrated corruption and only after reconciling any ambiguous publication by GET. Never delete audit/revisions or replay a WB POST to simulate rollback.

No migration or acceptance step creates a replacement transition preview/run
when the current run is valid. A new preview is an explicit future owner action.
