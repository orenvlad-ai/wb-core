# Architecture

The portable MVP is split into a Telegram collector and a scheduled worker. This avoids Telegram album timing races and gives the worker an atomic, idempotent unit. `blueprint_build_map.json` is the normative module map.

## Topology

```text
Telegram /review ─┐
Telegram media ───┼─ collector ── wb_telegram_ingest (collecting)
Telegram /process ┘                         │ lock
                                            ▼
scheduled worker → media fetch → normalizer → JSON Schema → idempotency
                                                        ├─ cached result → Telegram
                                                        ├─ empty 5★ → audit → Telegram skip notice
                                                        └─ classifier → route guard → case code?
                                                             → writer → validator
                                                                 ├─ pass → draft guard → ready
                                                                 └─ rewrite 1 → validator
                                                                      ├─ pass → ready
                                                                      └─ rewrite 2 → validator
                                                                           ├─ pass → ready
                                                                           └─ approved fallback
                                                                                     │
                                                        audit/cost/recent reply ←──────┘
                                                                                     ▼
                                                                                  Telegram
```

There is no Wildberries branch or connection.

## Frozen boundary

The worker loads the byte-for-byte files in `frozen_bundle/`. Make filters may branch only on orchestration fields such as `prefilter.model_calls_allowed`, `validation.status`, rewrite count, idempotency outcome, HTTP status, and state. They must not encode taxonomy, route, seller-chat, promise, or fallback semantics. Those decisions stay in:

- `frozen_bundle/tools/route_guard.mjs`;
- `scripts/draft_guard.mjs`, which invokes and strengthens the frozen draft guard;
- `frozen_bundle/contracts/route_fallbacks.json`;
- the strict request/output contracts.

At scenario start, compare the packaged artifact hashes to `bundle_manifest.json` and require evaluation signature `sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da`. A mismatch is `technical_failed_terminal`; it is never silently accepted.

## Collector scenario

The collector accepts commands only from the configured manager-chat allowlist.

- `/review {JSON}` validates required operational fields and upserts one `collecting` session per chat. Starting a new session while one is collecting is a technical input error.
- Each following photo, document, or video stores only its Telegram `file_id`, kind, order, and MIME type in the session.
- `/process` atomically changes `collecting → locked`. A second `/process` is harmless.
- `/diag <review_id> <review_version>` is the only path returning service JSON. It reads redacted audit data and never runs a role.

The scheduled worker claims locked sessions with a short lease. Telegram retries therefore cannot create a parallel AI pipeline.

## Worker variables

| Variable | Value/source | Mutability |
|---|---|---|
| `prompt_bundle_version` | `1.4.2` | constant |
| `evaluation_signature` | full frozen SHA-256 signature | constant |
| `model` | `gpt-5.6-terra` | constant |
| `reasoning_effort` | `medium` for all roles | constant |
| `max_rewrites` | `2` | constant |
| `idempotency_key` | encoded `review_id|review_version|1.4.2` | fixed after normalization |
| `classification` | strict classifier output after route guard | fixed after guard |
| `final_route` | guarded `classification.route` | immutable through writer/rewrite |
| `case_code` | null, except deterministic allocation for seller_chat | immutable after allocation |
| `rewrite_count` | `0..2` | increments only before rewrite |
| `draft` | writer or latest rewrite output | replaceable |
| `trace` | append-only role usage records | append-only |

## Role interfaces

All calls use `POST /v1/responses`, `store=false`, medium reasoning, stable pseudonymous `safety_identifier`, and strict `text.format.type=json_schema`. Credentials come from `openai_responses_mvp`, not the request body or data stores.

Classifier receives the frozen static taxonomy/playbook/policy/universal context before the explicit cache breakpoint. Per-review data, selected line context, exact optional SKU match, downloaded photos, and extracted frames appear after it. Unknown SKU uses the `unknown` line context and no `sku_match`.

Writer receives the guarded classification, immutable `final_route`, orchestrator case code or null, selected facts/playbook/examples, and draft constraints. Validator receives the same fixed context plus the current draft and attempt number. Rewrite receives the validator result and may change only the draft. Rewrite is intentionally uncached.

## State machine

Nominal transitions are implemented by `scripts/state_transitions.mjs`:

`received → normalized → skipped | classified → guarded → drafted → validated → ready → delivered_to_telegram`

Rewrite transitions are `validated → rewrite_1 → validated → rewrite_2 → validated`. If the last validation or deterministic guard still fails, use `validated → fallback_ready → delivered_to_telegram`. Any role/transport/schema failure records a redacted error and moves to a retryable or terminal technical state according to the module map. States named `published` do not exist.

## Idempotency and case codes

`wb_review_jobs.idempotency_key` is unique. If its saved outcome is `ready`, `fallback`, `skipped`, or already delivered, the worker sends the saved manager message and performs zero role calls. A retryable in-progress job is claimed only when its lease expired.

`scripts/case_code.mjs` allocates a code only after the guarded route is seller_chat. It reuses the prior code for the idempotency key and probes deterministically on an active collision. Non-seller routes require null. The public text must contain the code exactly once.

## Validation, rewrites, and fallback

Every role request and response is JSON-Schema validated. A model `pass` does not override `scripts/draft_guard.mjs`. A deterministic error becomes a rewrite reason. Classification, final route, and case code are copied unchanged into both rewrite attempts.

Fallback is reachable only after two failed rewrite → validator cycles. `scripts/fallback.mjs` accepts only an owner-approved entry whose route/mode/issue constraints match. If none exists, the job fails terminally; it never invents a generic public response. The fallback itself passes the writer schema and final deterministic guard before delivery.

## Audit and cost

Every state transition, role request/output, response ID, guard event, fallback choice, latency, token bucket, and computed cost is appended to `wb_ai_audit`. `scripts/cost_accounting.mjs` is the same usage math as the frozen evaluator and separates cached inputs, cache writes, outputs, and reasoning tokens. Authorization, tokens, temporary URLs, media data URLs, and common personal-data fields are redacted before persistence.

## Media behavior

Photos are downloaded through the Telegram connection at worker time, converted to supported image URLs/data URLs, and appended after the classifier breakpoint at high detail. Failed downloads produce `fetch_failed`, not `none`. Video is accepted and audited; without a frame extractor in this credential-only MVP it is marked `video_present_unprocessed`. Managers can attach key frames as photos. The model is never told an unprocessed video was viewed.

## Delivery boundary

Only the ready public text is sent to the manager chat. Skip and technical branches send an operational notice that is not a buyer response. A Telegram delivery retry reads the saved result and never re-enters OpenAI. Automatic WB publication, WB API calls, Make deploy, and production scheduling remain outside this package.
