# WB Autoanswers: production full-mode incident

Дата расследования: 2026-07-21 (Asia/Yekaterinburg). Production timestamps ниже приведены в UTC.

## Executive verdict

Система не остановилась на бюджетном лимите около `$3.03`. До аварийной паузы worker продолжал выполнять по одному processing claim на scheduler tick; последний claim был в `2026-07-20T20:05:54Z`, а master-switch был выключен в `20:06:17Z`. Активных или orphaned reservations на момент паузы не было, ошибок `429`, `insufficient_quota`, WB rate limit или retry/backoff не зафиксировано.

Наблюдение владельца объясняется сочетанием трёх дефектов:

1. Local ledger ошибочно settlement-ил полный резерв `$1.00` как actual cost при `node_invalid_json`, хотя usage не был получен. Поэтому UI показывал `$3.069336`, тогда как доказанная usage-данными сумма составляла `$2.069336` плюс неизвестная фактическая стоимость одного оборванного Node-run.
2. Два media-aware отзыва дали `$1.65250700` из `$1.80035900` доказанного расхода full-mode run. Base64 media refs находились не только в `input_image`, но повторялись внутри текстового dynamic JSON classifier/writer/validator. Один JPEG размером 182,330 bytes породил 521,863 input tokens и стоил `$1.27352350`.
3. С `19:45:32Z` до `20:01:14Z` worker продолжал обрабатывать пустые пятизвёздочные отзывы, которые старая policy пропускала с cost `$0`. Расход поэтому визуально застыл на `$3.03222150`, а UI не показывал queue progress или stop reason. В действительности processing не остановился.

Full-mode переход сразу не создал все 41,122 jobs, но materialization batch `25/min` значительно опережал throughput `1 processing job/min`: за 48 минут sweep reconciled 1,025 отзывов и оставил 982 scope jobs в durable queue; steady sync добавил ещё 11 новых jobs. Run cap, hourly cap, paid-review cap и bounded outstanding queue отсутствовали.

## Safety pause

До любых code changes выполнено:

- атомарный application snapshot текущих settings/queues;
- `master_enabled=false`; stored mode оставлен `auto_all`, чтобы не переписывать incident evidence;
- worker timer disabled/inactive; read-only sync timer enabled/active;
- очереди, leases, reservations, processing results, publication attempts и audit не удалялись;
- publication pending readback отсутствовал; blind retry не выполнялся.

Доказательство пяти тиков OFF:

| Метрика | До | После 5 `run-once` ticks | Delta |
|---|---:|---:|---:|
| Coordinator tick | 458 | 463 | +5 |
| AI processing claims | 46 | 46 | 0 |
| Frozen Node audit events | 265 | 265 | 0 |
| Generation completed | 14 | 14 | 0 |
| Paid reservations | 44 | 44 | 0 |
| Active reserved USD | 0 | 0 | 0 |
| Publication attempts | 11 | 11 | 0 |
| Confirmed publications | 11 | 11 | 0 |
| Queued jobs | 993 | 993 | 0 |

После доказательства worker timer остался `disabled/inactive`; read-only timer остался `enabled/active`.

## Runtime identity and drift

- Baseline, указанный владельцем: `352ad43bca89a80c4a3bce984b9d6d2f59832d50` (PR #705).
- Exact production deploy metadata и `.wb-core-runtime-sha`: `6b102548f8e87521290ffee7193c712a88ec8455`, deployed at `2026-07-20T20:04:37Z` (PR #707).
- Drift состоит из merged finance/export PRs #696, #698 и #707.
- Semantic diff по Autoanswers runtime, worker, coordinator, publication, Node bundle и feedback UI между двумя SHA пустой. Incident semantics соответствуют #705.

## Timeline

| UTC | Event |
|---|---|
| 19:17:50 | Owner preview: scope from `2026-01-01`, 41,122 unanswered, 41,118 need generation, 2 need regeneration, 2 ready. Unbounded estimate `$41,120`. |
| 19:17:53 | Owner confirmed `auto_all`; `policy_epoch=1`; one reconciliation sweep created. |
| 19:18:11 | First sweep batch; first media regeneration claimed. |
| 19:18:25 | Seller-chat regeneration completed, cost `$0.37898350`, moved to `needs_review`. First ready draft sent and then read back. |
| 19:19:19 | Second media regeneration failed `node_invalid_json`; reservation incorrectly settled as `$1.00`. Processing continued on later ticks. |
| 19:20:21 | First old `empty_five_star` skip. |
| 19:24:02 | First new paid public-only generation claimed. |
| 19:34:57 | Media JPEG outlier settled at `$1.27352350`; displayed ledger jumped to `$2.99351200`. |
| 19:45:32 | Displayed ledger reached `$3.03222150`. |
| 19:46:34–20:00:00 | Repeated `$0` empty-five-star skips; UI spend appeared frozen although worker progressed. |
| 20:01:14 | Paid generation settled; ledger `$3.05004500`. |
| 20:03:31 | Last paid generation settled; ledger `$3.06933600`; publication write returned 204. |
| 20:04:23 | Mandatory detail readback confirmed that publication. |
| 20:04:37 | Unrelated PR #707 deploy metadata written; no Autoanswers semantic drift. |
| 20:05:54 | Last pre-pause processing claim completed as `$0` empty-five-star skip. |
| 20:06:17 | Incident response set `master_enabled=false`, `policy_epoch=2`; worker timer stopped. |
| 20:12:58 | Fifth controlled OFF tick completed; no AI/reservation/publication delta. |

## Scope and queues at pause

Original transition scope:

- total unanswered: 41,122;
- ready and reusable: 2;
- regeneration required: 2;
- generation required: 41,118;
- reconciled/materialized by the sweep: 1,025;
- not yet materialized: 40,097.

Transition outcomes before pause:

- 41 processing jobs claimed;
- 9 AI jobs completed with usage;
- 31 empty five-star jobs skipped at zero cost under old policy;
- 1 job reached `terminal_error` (`node_invalid_json`);
- 0 jobs in retry/backoff;
- 0 active or expired leases;
- 1 generated seller-chat answer remained `needs_review`;
- 8 newly generated `public_only` answers were published and confirmed;
- 2 already-ready manual drafts were published and confirmed by the full-mode policy;
- total incident WB POST attempts: 10; all returned 204 and all 10 detail readbacks matched;
- total lifetime publications, including the earlier owner manual test: 11.

Durable state after pause:

| State | Count | Provenance |
|---|---:|---|
| queued | 993 | 982 transition scope + 11 new steady-sync reviews |
| skipped | 31 | old `empty_five_star` behavior |
| needs_review | 1 | seller_chat media regeneration |
| terminal_error | 1 | video regeneration / `node_invalid_json` |
| published | 11 | 10 incident + 1 earlier owner manual publication |
| publication jobs | 11 published | no pending write/readback |
| active reservations | 0 | no orphaned reservation found |

Relative to the original 41,122-review scope, 10 obtained confirmed WB answers during the run and 41,112 still lacked a confirmed WB answer at pause. Of these, 40,097 had not yet been materialized by the sweep.

## Cost reconstruction

### Ledger reconciliation

| Component | USD |
|---|---:|
| Actual known before full-mode transition | 0.26897700 |
| Successful full-mode calls reconstructed from usage | 1.80035900 |
| False actual settlement for `node_invalid_json` | 1.00000000 |
| Local dashboard/ledger at pause | 3.06933600 |
| Reconstructed usage-backed actual through pause | 2.06933600 |
| Active reserved at pause | 0.00000000 |
| Unknown provider cost of failed Node-run | unknown; no usage envelope persisted |

The failed run may have incurred zero or non-zero provider cost. The existing whole-pipeline Python↔Node boundary persists usage only after a complete Node response, so an exact value is unavailable locally. No OpenAI Admin API key is configured and no provider billing endpoint was queried during this investigation.

### Full-mode successful jobs

- paid reviews completed: 9;
- zero-cost processed reviews: 31;
- exact known spend: `$1.80035900`;
- mean paid review: `$0.20003989`;
- median: `$0.02126100`;
- p90: `$1.27352350`;
- p95: `$1.27352350`;
- maximum: `$1.27352350`;
- retries: 0;
- successful role calls: 27;
- rewrite calls: 0.

Per-review reconstruction below uses a one-way 12-character SHA-256 prefix instead of the WB feedback ID. `Cost` is the cost incurred inside this full-mode run, not the lifetime job total. The first row was a media-aware regeneration: its lifetime job total also contains `$0.02547850` incurred before full mode, which is deliberately excluded here.

| Feedback ref | Completed UTC | Route | Input | Cached input | Output | Reasoning | Role calls | Cost USD |
|---|---|---|---:|---:|---:|---:|---:|---:|
| `977112d9809f` | 19:18:25 | seller_chat | 165,265 | 20,324 | 770 | 227 | 3 | 0.37898350 |
| `a3be212ec3e8` | 19:24:26 | public_only | 23,875 | 20,324 | 586 | 0 | 3 | 0.02274850 |
| `206daa0ff9ad` | 19:27:38 | public_only | 24,147 | 20,324 | 892 | 207 | 3 | 0.02801850 |
| `89efb38d445b` | 19:33:41 | public_only | 23,436 | 20,324 | 560 | 67 | 3 | 0.02126100 |
| `5f5d586953f1` | 19:34:57 | public_only | 521,863 | 20,324 | 973 | 322 | 3 | 1.27352350 |
| `e0737968427b` | 19:39:28 | public_only | 23,082 | 20,324 | 564 | 156 | 3 | 0.02043600 |
| `73babf8b1f4e` | 19:45:32 | public_only | 23,159 | 20,324 | 407 | 0 | 3 | 0.01827350 |
| `cec8e7efbf24` | 20:01:14 | public_only | 23,063 | 20,324 | 393 | 0 | 3 | 0.01782350 |
| `22c508f13941` | 20:03:31 | public_only | 22,912 | 20,324 | 516 | 133 | 3 | 0.01929100 |
| **Total** |  |  | **850,802** | **182,916** | **5,661** | **1,112** | **27** | **1.80035900** |

The terminal review `a8e7a3b371ad` has an unknown incident cost because the old Node boundary lost partial usage. Its `$0.05225250` lifetime job cost was incurred before full mode and is excluded from the run; the `$1.00` full-mode reservation was falsely labelled actual by the old runtime and is not evidence of provider spend. The remaining 31 claimed empty-five-star reviews each incurred exactly `$0` and made zero role calls.

By route:

| Route | Jobs | Outcome | Cost USD |
|---|---:|---|---:|
| public_only | 8 | published + readback confirmed | 1.42137550 |
| seller_chat | 1 | needs_review | 0.37898350 |

Two additional pre-existing ready `public_only` drafts were published during full mode. Their generation cost (`$0.16337750` lifetime) was incurred before the transition and is not counted again in the `$1.80035900` run spend.

By role/model (`gpt-5.6-terra` only):

| Role | Calls | Input | Cached input | Cache-write | Output | Cost USD |
|---|---:|---:|---:|---:|---:|---:|
| classifier | 9 | 373,315 | 154,332 | 0 | 3,586 | 0.63983050 |
| writer | 9 | 240,903 | 17,100 | 0 | 1,311 | 0.58344750 |
| validator | 9 | 236,584 | 11,484 | 0 | 764 | 0.57708100 |
| rewrite | 0 | 0 | 0 | 0 | 0 | 0.00000000 |
| total | 27 | 850,802 | 182,916 | 0 | 5,661 | 1.80035900 |

`reasoning_tokens=1,112`, `total_tokens=856,463`. Usage was reported for every successful role call.

### Prompt-cache finding

The explicit cache breakpoint and prompt-cache keys were not lost. Normal text-only jobs reported approximately 85–89% cached/input ratio. Every successful normal-path role had a cache hit. Aggregate hit ratio fell to 21.50% because review-specific base64 image strings were repeated in dynamic text and correctly could not be cached.

The two image jobs consumed:

- 51,408-byte JPEG: 165,265 input tokens, `$0.37898350`;
- 182,330-byte JPEG: 521,863 input tokens, `$1.27352350`.

Together they were 91.79% of full-mode known spend. Static prompt repetition or unnecessary rewrite loops were not the driver.

## Budget-reservation defect

`record_processing_terminal` converted any still-`reserved` row into `settled` with `actual_cost_usd=reserved_usd`. In this incident the fixed per-review reserve was `$1.00`; a fast `node_invalid_json` therefore became a fake one-dollar actual charge. Retry paths also retained reservations and there was no stale-reservation reconciliation after lease loss/restart.

This defect did not stop the worker in this incident because total used + next `$1.00` reservation remained below the `$5/day` cap. It could, however, produce premature stops after enough failures and cannot remain in production.

## Throughput and observability defects

- Reconciliation materialized 25 candidates/tick while processing handled 1 job/tick.
- Materialization checked mode/policy epoch but not run/hour/day/month budget or queue depth.
- No mandatory run cap existed; the owner confirmed a preview whose theoretical reservation estimate was `$41,120`.
- New steady-sync jobs were enqueued in parallel with the historical sweep.
- There was no persisted stop reason, last successful AI/publication timestamps, rate or ETA.
- The first page had no aggregate scope/processing/publication progress.

## OpenAI and WB evidence

- No `429`, `insufficient_quota`, OpenAI rate-limit or retry event was found in application audit or worker journal.
- One Node boundary failure was recorded as `node_invalid_json`; stderr/return code were not persisted, so the lower-level cause and provider usage are unknown.
- No WB rate-limit or ambiguous publication result occurred in the incident.
- Ten incident WB POSTs returned HTTP 204, and ten mandatory detail readbacks confirmed exact answers.
- No blind retry occurred.

## Corrective design required by this report

1. Never treat a reservation as actual cost. Settlement requires measured usage; every failure/timeout/cancel/lease loss releases remaining capacity while preserving explicitly persisted per-call actual usage. A crash after the durable provider-entry marker latches further paid work closed until cost reconciliation.
2. Add stale reservation reconciliation and fail closed on inconsistent budget state.
3. Persist hourly/day/month/run budgets, paid-reviews/hour, global concurrency and max in-flight role calls.
4. Require a bounded transition run (`max_usd` or `max_paid_reviews`) before automatic modes.
5. Materialize lazily with a small outstanding queue target and budget/throughput checks before every batch.
6. Strip binary/data URL refs from textual dynamic JSON while retaining the same review-specific media as `input_image` after the frozen cache breakpoint.
7. Persist sanitized Node exit evidence and per-role usage as soon as it is available.
8. Replace old empty-five-star skip with owner-approved deterministic zero-cost `rating_only_template` for ratings 1–5.
9. Expose exact queue progress, actual/reserved balances and explicit stop reason in local-DB-backed UI/API.

## Direct answers

1. **Сколько отзывов обработано?** 41 processing claims: 9 successful paid, 31 zero-cost skipped, 1 terminal error. Sweep reconciled 1,025 candidates.
2. **Сколько опубликовано?** 10 during full mode, all readback confirmed; 11 lifetime including the earlier owner manual publication.
3. **Сколько денег потрачено?** `$1.80035900` is proven for successful calls during full mode. Local lifetime usage-backed total is `$2.06933600`, plus an unknown amount for one failed boundary run. UI incorrectly displayed `$3.06933600`.
4. **Сколько стоил один отзыв?** Mean `$0.20003989`, median `$0.02126100`, p95/max `$1.27352350` for nine successful paid reviews.
5. **Почему расход рос быстро?** Base64 media was charged repeatedly as text across roles; two media reviews consumed 91.79% of known run spend.
6. **Почему всё остановилось около `$3.03`?** It did not stop. It kept processing `$0` empty reviews while the displayed total stayed near `$3.03`, then processed two more paid reviews. Incident response stopped it at `$3.06933600` displayed.
7. **Это штатный лимит или дефект?** Not a cap. It is a combination of false settlement, media token amplification and missing observability.
8. **Что осталось?** 993 queued jobs persisted (982 scope + 11 steady), 40,097 original-scope reviews not yet materialized, 1 needs review, 1 terminal error, 31 old skips requiring new template policy.
9. **Можно ли продолжить?** Yes, only after corrected ledger, mandatory run cap, bounded lazy materialization and observable stop reasons are deployed; the preserved old queues must be reconciled under a new policy epoch in manual mode before any future owner-confirmed run.

## Evidence boundary

All production inspection was aggregate/read-only except the owner-authorized emergency master-switch OFF and five OFF-gated coordinator ticks. No secret value, review text, answer text, signed media URL or raw identifier is included in this report. During diagnosis: OpenAI live calls initiated by the investigator = 0; WB POST/PATCH initiated by the investigator = 0.
