# WB Autoanswers policy addendum v3

Status: owner-approved, 2026-07-21. This addendum extends server orchestration policy v2. It does not rewrite v2 history or alter doctrine v1.0, frozen AI bundle v1.4.2 prompts, schemas, guards, thresholds, golden data or evaluation signature.

Policy identity: `owner-policy-2026-07-21-v3`.

## Canonical content taxonomy

One server-side classification is persisted for the current content version and frozen into each mode-transition membership snapshot.

- `content_bearing`: trimmed text, pros or cons is non-empty; at least one tag is non-empty; or a photo/video is evidenced by canonical media, content media, `has_photo` or `has_video`.
- `rating_only`: every content surface above is empty and rating is an integer 1–5.
- `indeterminate`: malformed, contradictory or insufficient data. It is never routed through `rating_only_template` and requires review.

The unchanged v2 template file remains the sole source for true `rating_only` replies. The resulting audited job carries orchestration policy v3 plus template-policy identity v2, makes zero model calls and costs exactly `$0`.

## Content-first automatic barrier

All background automatic stages order `content_bearing` before `rating_only`, then `created_at_wb DESC`, `first_seen_at DESC` when WB date is absent, and `feedback_id DESC` as deterministic tie-breaker. Rating is not a priority input.

The ordering applies to preview/snapshot, reconciliation, lazy materialization, processing/retry/expired-lease claims, ready-result reuse, publication enqueue and publication claims. Explicit owner-triggered manual work retains its separate semantics.

No `rating_only` job may materialize, claim processing or begin a new WB write while a scoped `content_bearing` review has an automatic next step. Budget, throughput or run-cap pauses do not open the empty-review lane. Regeneration, retry/backoff, queued/processing work, automatic publication and mandatory readback hold the barrier. Human-only `needs_review`, terminal/hard-gate outcomes, external WB answers and policy-ineligible publication do not hold it indefinitely.

Existing rating-only queues/results/audit remain intact. Current content capacity ignores old rating-only occupancy, and old jobs are adopted rather than deleted or recreated. A WB write already started remains the sole safety exception: its mandatory GET readback runs first and a second POST is forbidden.

## Bounded run and progress contract

Preview and apply use the same immutable membership and classification snapshot. A run must retain its explicit USD or paid-review cap. Reviews observed after preview are exposed as outside the current run and require a new preview.

The local API derives four stages from that snapshot and current local aggregates: all preparation, all readback-confirmed publication, content preparation and content readback-confirmed publication. Ready current drafts count only as prepared. `publish_pending_readback`, stale results and `needs_review` are incomplete. External WB answers are separate from system publication. A zero denominator has no percentage; 100% requires numerator equal to denominator. Manual mode preserves counters and reports a manual pause.
