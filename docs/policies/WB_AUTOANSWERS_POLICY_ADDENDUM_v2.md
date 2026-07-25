# WB Autoanswers policy addendum v2

Status: owner-approved, 2026-07-21. This addendum extends only server orchestration policy. Doctrine v1.0 and frozen AI bundle v1.4.2 prompts, schemas, guards, thresholds, golden data and evaluation signature remain byte-for-byte unchanged.

Policy identity: `owner-policy-2026-07-21-v2`.

## Empty reviews

An unanswered review whose `text`, `pros` and `cons` are all empty and whose rating is 1–5 uses the deterministic server route `rating_only_template`. It never enters the frozen OpenAI pipeline.

- subcategory: `rating_<rating>_empty`;
- template source: `packages/contracts/wb_autoanswers_rating_only_policy_v2.json`;
- selection: `sha256(feedback_id) mod template_count`;
- repeated processing of the same WB feedback ID selects the same template ID and exact text;
- model calls and AI cost are exactly zero;
- result, policy identity, template ID and audit are durable;
- existing-answer, current-version, publication-idempotency, policy-epoch, pre-write and mandatory WB detail-readback checks remain required;
- `rating_only_template` is eligible under `auto_safe` because every exact text in the versioned contract is owner-approved;
- templates are never rewritten by a model.

This replaces only the old server outcome for an empty five-star review. The frozen bundle's legacy empty-five-star prefilter remains unchanged and unreachable for server jobs classified as `rating_only_template`.

## Spend and throughput controls

Every paid review requires an atomic reservation immediately before its claim. Defaults are:

- global paid-review concurrency: 1;
- maximum in-flight role calls: 1 (the Node boundary invokes roles sequentially);
- hourly cap: `$0.50`;
- paid reviews per hour: 20;
- daily cap: `$5`;
- monthly cap: `$50`;
- total materialized processing queue depth: 5, including zero-cost jobs.

An automated mode-transition run is invalid unless its owner-bound preview includes at least one positive cap: maximum USD or maximum paid reviews. Reservations settle only to reported actual usage. A durable provider-entry marker distinguishes a safe pre-call crash from an ambiguous post-call crash. Timeout, retry, cancellation, terminal execution failure and lease loss release unused reservation capacity; any post-entry outcome without usage latches paid processing as `budget_state_unknown`. Expired/orphaned reservations are reconciled durably. Unknown budget state fails closed.

Admin operators may change the persisted global limits without replacing the
active run. The allowed envelope is `$0.01..$10/hour`,
`$0.01..$50/day`, `$0.01..$500/month`, `1..200` paid reviews/hour,
`1..4` concurrent paid reviews, `1..8` concurrent role calls and `1..100`
materialized processing jobs. The monetary order is hour <= day <= month and
neither concurrency value may exceed queue depth. The active run's
owner-confirmed USD/review cap is not operator-editable and global changes
never reset accumulated usage, reservations, holds or stronger stop reasons.

## Lazy reconciliation and observability

Automated transitions persist the exact preview membership, scope, policy epoch, transition run ID, run caps and cursor. Jobs materialize in bounded batches only after current mode, epoch, budgets, throughput and total processing queue depth are rechecked. Paid caps still permit bounded zero-cost templates and ready-draft reuse. Restart resumes from durable database state, and a duplicate sweep/job cannot create a duplicate processing or publication aggregate. Replaying one consumed preview is idempotent; a fresh capped preview starts a new run even if the automatic mode name is unchanged.

The local UI/API exposes preparation/publication progress, actual and active-reserved cost, run cost, queue states, scheduler/AI/publication timestamps and a concrete stop reason. Spending that remains unchanged while zero-cost templates progress is not displayed as a stopped worker.

## Media payload economy

Review-specific image bytes are attached to the classifier only as `input_image` content after the explicit prompt-cache breakpoint. Textual request JSON contains stable attachment references, not repeated base64. Writer and validator retain media-derived review/classification context but do not receive duplicate binary bytes. Full-video viewing is never claimed; only preview and at most four deterministic frames are represented.
