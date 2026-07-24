# Migration 119 — WB Autoanswers rating-bucket priority

Status: LOOP live-runtime release. This is a new release root and does not
inherit branch, PR, acknowledgement, ownership or LOOP identity from the
earlier Autoanswers incident chain.

## Read-only incident baseline

Repository and production preflight were taken against main/deployed
`765d9aa7441d1b2a702f30363f3d827cfc125bb3`. Production SQLite was opened
read-only with `PRAGMA query_only=ON`; no incident evidence was deleted or
rewritten.

The bounded window beginning `2026-07-24T18:20:00Z` separates these causes:

- four processing failures from `18:36:39Z` through `18:39:59Z` were
  `reservation_missing` after five completed `skipped(empty_five_star)`
  projections had been revived across a policy epoch. The prior bounded
  recovery restored exactly five projections and left zero remaining
  candidates. Current main already preserves completed skips, bootstraps the
  direct recovery CLI and performs explicit latch reconciliation; migration 119
  retains those regressions and does not bypass the reservation boundary;
- one readonly sync overlapping a successful worker tick failed at
  `19:11:16Z` because it compared store-wide queue totals. Current main already
  proves the causal `enqueued=0` result instead. Post-deploy readonly overlaps
  succeeded without provider or WB writes;
- current main still produced reconciliation `publication_already_exists` at
  `19:26:03Z` and `19:31:37Z`. Five v2 rating-template publications had been
  conservatively reclassified as content-bearing, with both processing and
  publication rows in `needs_review`, zero write attempts and
  `regeneration_required=content_classification_v3_changed`. Reconciliation
  called regeneration before accounting for the existing publication
  aggregate. This release preserves each pair as review-only current-run
  evidence instead of replacing it or entering the provider boundary;
- two `$0.10` uncertainty holds are immutable conservative evidence for
  provider-started boundaries without usage readback. The three preserved
  terminal rows likewise remain audit evidence. `unresolved_uncertainty_count`
  is zero, so neither the holds nor terminal/provider/cost rows are cleared;
- `no_eligible_jobs`, hourly/run budget pauses and an error banner observed
  before confirmed server recovery are not reclassified as worker/provider
  failures. UI health remains derived from matched lifecycle plus a fresh
  scheduler tick.

The incident evidence helper counts a confirmed WB write by its stored
`publication_confirmed_by_readback` outcome. A transport completion alone is
not confirmation.

## Schema-v7 and ordering contract

Schema v7 is additive. It creates only the content-class/rating/date priority
index and the verified current-version backup contract. Existing feedbacks,
jobs, results, reservations, holds, cost events, publications, attempts,
readbacks and audit remain unchanged.

One SQL ordering contract is used by:

1. transition preview snapshot and immutable membership ordinal;
2. reconciliation and lazy materialization;
3. new, retryable and expired-lease processing claims;
4. reuse and publication enqueue of ready results;
5. publication write and mandatory readback claims.

Eligible content-bearing buckets are rating `1`, `2`, `3`, `4`, `5`. Within
each bucket the order is `created_at_wb DESC`, falling back to
`first_seen_at DESC`, then `feedback_id DESC`. Rating-only work advances only
after scoped content-bearing work has no automatic next step; its existing
date/id order is unchanged. Manual work and indeterminate review-only rows keep
their existing separate semantics.

The provider boundary remains fail-closed. No processing claim can proceed
without its exact active reservation. A completed skip cannot be revived for a
new policy epoch. A reclassified unstarted publication is adopted into the new
run as `needs_review`, does not hold the rating-only barrier forever and does
not create a second publication aggregate or WB write.

## Regression and release gates

Required proof includes:

- the five-row skip incident across a policy epoch and reservation-missing
  recovery/latch invariants;
- direct CLI bootstrap and idempotent recovery readback;
- readonly/worker overlap with causal zero-enqueue proof;
- immutable ordinal, materialization, processing, retry/expired lease,
  ready-result reuse, publication and readback order for content ratings
  `1→2→3→4→5`, followed by rating-only date/id order;
- five reclassified unstarted publications retained as exactly five
  review-only pairs with zero attempts and no reservation;
- the full Autoanswers unittest suite, frozen Node identity and compile checks.

Deploy uses the repository-owned Release Train. Schema preparation must produce
and verify the pre-v7 backup before DDL. Deploy itself does not silently enable
business timers.

## Explicit new production run

After deployed SHA equals this PR's merge SHA, create a fresh exact
`auto_all` preview for `scope_from=2026-01-01` and apply it only through the
feature lifecycle. The run cap is at most `$10`. Hour/day/month caps must not
exceed `$0.50/$5/$50`; stricter persisted values remain strict. Pre-apply
readback must show no unsafe active processing/reservation or unknown provider
outcome. Existing spend, holds, publications, attempts, readbacks and audit are
preserved.

Acceptance requires matched readonly/worker desired and actual states, both
timers enabled and active, a fresh tick, no blocking latch, exact new
transition-run/policy-epoch/cap readback, runtime bucket-order evidence
including zero-count skipped buckets and no rating-only claim while content is
claimable, several worker cycles, a readonly overlap, one new confirmed WB
publication, authenticated UI reload with `Работает` and Full mode, and no
5xx/fatal/page/console error. LOOP closes only after exact deployed-SHA UI
acceptance and terminal `release:production`.

The canonical authenticated UI runner supports `--expected-state auto-all`
only with the exact deployed SHA, policy epoch and transition run. In this
active state it permits legitimate concurrent queue movement while proving
that the flow itself is read-only, both lifecycle components remain matched,
the scheduler tick is fresh, and neither a stale manual-pause label nor an old
error banner survives reload.

## Rollback

Emergency rollback is `WB_AUTOANSWERS_FORCE_OFF=true` plus the feature-owned
lifecycle. Code rollback leaves the additive v7 index inert. Restore the
verified pre-v7 database only for demonstrated corruption and only after GET
reconciliation of any ambiguous started write. Never delete or replay
reservations, holds, cost events, publications, attempts, readbacks or audit.
