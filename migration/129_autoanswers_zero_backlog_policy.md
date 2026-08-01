# Autoanswers policy v4 and exact backlog recovery

Status: implemented in schema v10; production activation is separately human-gated.

## Incident evidence

The full official unanswered list captured on 2026-08-01 contained 57 distinct
feedbacks and matched the count endpoint. Exact detail GETs proved that all 57
were still unanswered and content-hash stable. The durable composition was:

- 29 unstarted publication aggregates paused by `policy_epoch_stale`;
- one unstarted stale rating-template publication whose current classification
  had become content-bearing;
- 16 `seller_chat` rows intentionally held for an operator (10 from the
  original cohort and six later admissions);
- four terminal rows (`reservation_missing` ×2, `node_invalid_json` ×1,
  `stale_content_version` ×1);
- seven August/September 2025 rows absent locally because all ordinary
  acquisition paths had a 2026 history floor or a 48-hour steady window.

The original 44-row cohort therefore had not disappeared: it was exactly the
30 publication-bound rows, the older 10 seller-chat rows and four terminal
rows. The later six seller-chat and seven missing-ingestion rows explain 57.

Both `reservation_missing` rows and the old `node_invalid_json` row already had
append-only frozen `job_complete` audit evidence with exact final route/reply
hashes. Recovery can reuse those results without another provider call. The
`stale_content_version` row had no provider call and no publication.

## Code and contract changes

- server contract advances to `wb_autoanswers_server_v5`;
- SQLite schema v10 adds only
  `sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs`; migration does not
  activate the new policy and preserves the schema-v9 publication lookup index;
- `owner-policy-2026-08-01-v4` introduces the versioned, zero-cost
  `wb_autoanswers_safe_public_policy_v1`;
- automatic `seller_chat` results are archived/audited and transformed into a
  deterministic `public_only` acknowledgement with no operator handoff, case
  code, monetary/remedy promise or WB-decision promise;
- a completed frozen Node result is bound to one exact audit invocation and may
  be restored atomically from append-only evidence without another provider
  call or an intermediate claimable state;
- a second opaque boundary failure queues a zero-cost safe-public processing
  kind instead of becoming permanent review work;
- exact unstarted publications are rebound to a new epoch without regeneration,
  provider cost or WB POST; write-started publications remain readback-only;
- a periodic full unanswered inventory uses `dateFrom=0`, independently of the
  historical backfill floor, and exposes full backlog/oldest/reason counters.

The frozen v1.4.2 bundle, evaluation signature and its prompts/contracts remain
unchanged.

## Canonical production mutation

The hosted `autoanswers-backlog-recovery` command is the only production path
for the incident cohort; it invokes
`apps/wb_autoanswers_backlog_recovery.py` on the canonical active runtime:

1. `capture` builds `wb_autoanswers_t0_manifest_v1` from a count-matched full
   paginated official unanswered list and one unanswered detail GET per ID;
2. `dry-run` validates that external manifest, performs a fresh full list and
   exact detail GETs, reads SQLite with `mode=ro`
   plus `PRAGMA query_only=ON`, verifies the schema-v10 backup and emits the
   deployed-SHA-bound plan fingerprint/pre-change digest/action manifest. The
   identity binds exact content/job/publication/write/reservation/cost fields
   and one complete frozen audit invocation per recoverable result. Apply is
   not ready while `budget_state_unknown`, unresolved provider-cost evidence
   or an active reservation exists; those boundaries use the existing
   dedicated budget-reconciliation lifecycle first;
3. `apply` requires that exact reviewed plan and fingerprint, the exact complete
   deployed SHA, an explicit actor and the exact human-gate reference. It first
   persists a resumable `planned` ledger, materializes every exact T0 detail,
   activates policy v4 once, rebinds/reuses/transforms/queues only T0, and
   transaction-locally proves all non-target job/publication/write/cost/
   reservation counts and every non-policy setting unchanged. After an
   interruption it resumes only when current state is the same reviewed state
   plus a deterministic prefix of its own exact detail upserts;
4. ordinary workers perform any required AI generation and all WB writes. The
   recovery runner performs zero provider calls and zero WB POSTs;
5. `readback` repeats the full official list and all T0 detail GETs and opens
   SQLite only with `mode=ro` plus `PRAGMA query_only=ON`. It returns
   `reconciled` only when both official list/count are zero and matched, every
   T0 detail has an answer, local DB/API answer observations are present, and
   local not-materialized, review, terminal, stale-policy, unpublished
   seller-chat, ambiguous-write and active-pipeline tails are all zero. It
   also requires zero active reservations, zero unresolved provider-cost
   boundaries and no `budget_state_unknown` latch. It
   never writes reconciliation evidence into the database; the canonical
   post-apply GitHub reconciliation comment owns that terminal evidence.

No direct remote command or server-only script is an authorized apply path.
Replay of an applied fingerprint is bounded and idempotent.

Any row with a possible previous WB write remains GET-reconciliation-only. No
apply path manufactures an answer, deletes immutable evidence, guesses provider
cost or repeats a POST.
