# WB Autoanswers — media, UI and policy-transition implementation report

Date: 2026-07-20

Status: `RELEASE_RECOVERY__DEPLOY_GATE_PENDING`

Production baseline at start: `82a96591db7e0fdb6b8a229912bcad1d77fb243c` (PR #686)

Development baseline: `2cd2879e2936bff4bd2fcb864edac7874eac045f`

The increment is local-test complete. It does not modify Doctrine, frozen prompts, JSON contracts, golden data, thresholds or evaluation signature. Release and production evidence are finalized in the PR/release-train audit and terminal handoff because their SHAs do not exist before merge.

## Outcome

- The feedback detail is compact and user-facing. Route, contracts, hashes, attempts, cost and audit evidence are under a closed-by-default `Техническая информация` disclosure.
- Empty pros, cons and tags are omitted. Product, rating/date, buyer text/media, existing WB answer, generated reply, status and primary actions remain visible.
- The full-width generated-reply textarea grows on initial render, generation refresh, manual edit and input.
- The list uses a fixed-height internally scrollable answer cell with an isolated copy action and `Скопировано` feedback. An absent answer renders a small neutral state.
- Local actual daily/monthly spend and caps are shown with update time. The only billing link is the official OpenAI billing overview; no private billing endpoint or model key is reused.
- All five modes use one selector with Russian labels. Automated-mode changes require an actor-bound preview and create a durable `policy_epoch` reconciliation sweep.
- Existing manual work, current drafts, in-flight jobs and publication/readback evidence are preserved. Old-policy writes are blocked before transport.

## Exact media root cause and repair

Production read-only evidence identified two independent causes:

1. Buyer photos are served from `*.geobasket.ru`; the old exact host allowlist did not include that official WB CDN family, so the safe fetcher returned `media_url_blocked`.
2. Feedback video URLs on `*.wbbasket.ru` return HLS playlists (`application/vnd.apple.mpegurl`, `#EXTM3U`) with relative variant/segment URLs. The old implementation treated that response as a monolithic MP4, so ffmpeg returned `video_extract_failed`.

The repair keeps the security boundary:

- HTTPS only; no URL credentials; exact WB/CDN suffix allowlist; DNS results must be globally routable;
- every redirect and every HLS variant/segment is independently revalidated;
- bounded redirect count, connect/read timeout and byte limits;
- MIME plus file-signature validation;
- deterministic first HLS variant and at most four evenly selected segments/frames;
- preview from WB when available, otherwise from the first validated frame;
- private `0600` storage, authenticated `no-store` media route and TTL cleanup that resets DB fetch state before deletion;
- signed query strings and private paths are removed from public detail, ordinary logs and canary evidence;
- expired temporary URLs are refreshed by a read-only WB detail GET before one bounded retry.

The classifier receives the real photo, video preview and no more than four deterministic frames as review-specific inputs after the frozen cache breakpoint. Writer behavior and all frozen semantics remain unchanged. No claim is made that the whole raw video was watched.

## Media-aware invalidation

Additive schema v3 marks only old results satisfying all of the following as `regeneration_required`:

- `media_uncertain=true`;
- no current WB answer;
- not already `published`;
- no durable publication attempt.

The owner-published answer, its publication attempt/readback and audit are explicitly excluded. Regeneration archives the prior result and cost, preserves audit history, increments a media-processing version and creates only one idempotent job. A fetch failure now stops before Node/OpenAI and enters `needs_review` with zero model calls.

## Mode-transition reconciliation

Automated target modes (`draft_only`, `auto_safe`, `auto_all`) require a server preview for unanswered synchronized history from `2026-01-01`. It reports current drafts, generation needs, media regeneration, automatic-publication candidates, manual-review candidates, estimated generation cost and daily/monthly cap impact.

Apply is bound to the same actor and exact preview snapshot. A real transition increments `policy_epoch` and creates one resumable durable sweep. Priority is:

1. manually started work;
2. current completed drafts;
3. recoverable in-flight work;
4. media-aware regeneration;
5. untouched reviews, newest first.

Ready current drafts are reused, in-flight jobs are not duplicated, external WB answers are skipped and automated publication never accepts fallback, unsafe, stale or media-uncertain results. A downgrade stops new claims immediately; a queued write with an old epoch cannot write. An already ambiguous transport attempt may complete readback only, never a blind retry.

`off` preserves queues and drafts while blocking new AI claims and pre-write publication. `manual` has no background generation. `draft_only` never publishes. `auto_safe` retains the initial allowlist `public_only`, `wb_return`, `wb_support`; `seller_chat` remains review-only. `auto_all` still enforces every contract and hard gate.

## Database and rollout

Schema v3 is additive:

- `policy_epoch` on settings, processing and publication jobs;
- preview metadata on media;
- media-processing version/regeneration evidence on AI jobs;
- append-only AI revision and cost-event tables;
- actor-bound mode previews and durable reconciliation sweeps.

The deploy preflight temporarily forces the migration process OFF, takes an integrity-checked compressed backup and applies DDL atomically. The active production setting remains `master_enabled=true`, `mode=manual`; the rollout does not activate an automated mode or sweep. Rollback is code rollback with additive tables left inert. Emergency `WB_AUTOANSWERS_FORCE_OFF=true` remains the highest-priority stop.

## Verification

Free local acceptance on the release-candidate tree:

```text
python3 -m unittest apps.wb_autoanswers_*_test  -> 96 PASS
python3 -m compileall -q apps packages         -> PASS
frozen make_mvp npm test                       -> 28/28 PASS
sheet_vitrina_v1_feedbacks_browser_smoke.py    -> PASS
sheet_vitrina_v1_web_vitrina_browser_smoke.py  -> PASS
registry/auth/public/users/feedbacks/hosted
and web-vitrina static smoke set                -> PASS
git diff --check                               -> PASS
```

Coverage includes compact/conditional detail layout, closed technical disclosure, textarea auto-grow, fixed scroll/copy cell, copy immutability, desktop/narrow rendering, real-format WebP and HLS fetches, redirect/DNS/host/MIME/size/time guards, expired URL refresh, preview/frame extraction, mock classifier media inputs after cache breakpoint, TTL cleanup, regeneration invalidation, all 25 mode pairs, preview counts, actor/snapshot binding, policy epochs, restart/lease recovery, budgets, duplicate suppression, external answers, OFF/force-off and stale-write blocking.

The browser test uses a local authenticated HTTP server and Chromium. Media tests use local fake transports and ffmpeg; frozen role execution uses spies. No live model or WB write capability is present in these tests.

## Production acceptance contract

After exact-SHA deployment, keep `master_enabled=true`, `mode=manual`, `force_off=false`. Do not click generation, regeneration or publication and do not switch modes.

1. Verify schema backup/integrity and exact deployed SHA.
2. Record published-answer/publication-attempt counts before acceptance.
3. Run one bounded repo-owned `manual-media-canary`. It imports no Node/OpenAI/writer and may perform only WB detail GET plus bounded WB/CDN media GET.
4. Require one validated real photo, one real video preview and one-to-four extracted frames; evidence exposes only hashed feedback identifiers.
5. Run authenticated production UI flow for compact detail, disclosure, auto-grow, answer copy/scroll, photo/video render and narrow layout.
6. Prove no 5xx, page/console errors, spontaneous AI jobs or new publication attempts.
7. Prove the owner-published answer and audit counts are unchanged.

## External-call ledger for implementation and local acceptance

- OpenAI live calls: `0`
- Paid evaluation calls: `0`
- WB POST/PATCH: `0`
- Published answers: `0`
- Deploys: `0` (until release train)

Production release is authorized separately by the current LOOP request. Its read-only media GET and UI evidence are reported in the terminal handoff.

## Release recovery after capacity halt

The first deployment of merged PR #694 halted before service restart because the root volume did not have space for another raw database-sized pre-schema snapshot. Read-only inspection proved the live database and both recovery sources remained structurally valid. A lifecycle `status` invocation on the old implementation also exposed that constructing the repository below target schema could apply additive DDL; the database reached schema v3, while persisted `master_enabled=true`, `mode=manual`, the existing owner-published answer and its audit remained unchanged.

The recovery increment makes `status` non-mutating for an old or absent database. It also resumes from the complete current-schema raw snapshot left by the interrupted preparation: only the owned backup filename boundary is accepted; SQLite integrity, compressed integrity, archive SHA-256 and exact decompressed SHA-256 are verified; the canonical v3 manifest is read back; and only then is the raw source removed to recover capacity. Failure before that proof retains the raw snapshot. Targeted tests cover both status cases and low-capacity exact backup compaction. The recovery performs no ad-hoc deletion, SQL mutation, WB/OpenAI call or service action; release remains owned by the standard deploy path.

## Owner’s first media-aware test after release

SellerOS → `Отзывы` → `Отзывы` → open an unanswered review with photo or video → verify the media preview → click `Перегенерировать с учётом медиа` for a flagged old draft, or `Сгенерировать ответ` for a new eligible review. Publication remains a separate explicit confirmed action and is outside release acceptance.
