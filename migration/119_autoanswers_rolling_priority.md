# Migration 119 — Autoanswers rolling admission and literal priority

## Preserved owner intent

This migration is additive and continues the currently active automatic run
when it is otherwise valid. It preserves the owner-confirmed initial preview,
immutable initial membership, `policy_epoch`, transition-run identity and cap.
It does not create a replacement preview/run, increase the run cap or raise any
hourly, daily, monthly, concurrency or review limit.

The frozen bundle stays `1.4.2` with the existing evaluation signature and
artifact hashes. Tags, photo and video remain authoritative policy-v3
`content_bearing` evidence; they are not downgraded to the zero-cost
`rating_only_template`.

## Schema v7

Canonical `prepare-deploy` first creates and verifies the current schema-v7
pre-change backup, then atomically adds:

- `sheet_vitrina_v1_wb_autoanswers_rolling_admissions`;
- `sheet_vitrina_v1_wb_autoanswers_rolling_state`;
- `sheet_vitrina_v1_wb_autoanswers_provider_uncertainty_attempts`;
- the feedback-version admission cursor and priority lookup indexes.

The first scheduler tick starts its cursor at the active sweep's creation time.
Each tick scans a bounded ordered slice of immutable version rows, advances past
every examined row and append-only admits only a current exact unanswered
version within the run's date scope. Repeated ticks and restarts are
idempotent. Initial, admitted-since-start and current exact totals are separate
runtime/API/UI facts.

## Literal queue barrier

Every not-yet-started automatic action uses:

```text
content_bearing 1★
  -> 2★
  -> 3★
  -> 4★
  -> 5★
  -> indeterminate
  -> rating_only
```

Within a bucket the existing newest-first deterministic fallbacks remain.
Reconciliation, lazy materialization, processing/retry claims, ready-result
reuse, publication enqueue and publication claims all consult the same current
admitted set. A newly admitted higher bucket preempts lower waiting work on the
next safe claim. An in-flight provider call or already started WB write is not
interrupted; mandatory readback remains first.

Rows already resolved to the current policy epoch as `needs_review` or
`terminal_error` remain visible operator evidence but are not future automatic
actions, even when they retain `regeneration_required` hard-gate metadata.
Only a stale-policy regeneration candidate or an actually queued/in-flight
regeneration participates in the global barrier. This prevents a human-only
1-star media/error row from deadlocking claimable automatic 2–5-star work
while preserving the row and its review reason.

## Opaque Node boundary and affected-row recovery

After provider entry, invalid/no child JSON with
`node_timeout`, `node_invalid_json` or `node_process_exit_1` is conservatively
accounted per attempt. The reservation is released, one maximum-reservation
upper-bound hold is appended, and only digest/byte-count diagnostics are
stored. Attempt one uses bounded backoff; attempt two becomes `needs_review`.
No amount is guessed as actual provider cost, and an isolated persisted failure
does not terminate the whole worker oneshot.

Before recovery, collect query-only incident evidence and determine exact
candidate counts for the active transition run. The repo-owned runner defaults
to dry-run:

```bash
python3 apps/wb_autoanswers_rolling_recovery.py dry-run \
  --runtime-dir <canonical-runtime-dir> \
  --transition-run-id <run-id> \
  --expected-empty <exact-count> \
  --expected-node <exact-count>
```

Apply requires the returned fingerprint and verified schema-v7 backup:

```bash
python3 apps/wb_autoanswers_rolling_recovery.py apply \
  --runtime-dir <canonical-runtime-dir> \
  --transition-run-id <run-id> \
  --expected-empty <exact-count> \
  --expected-node <exact-count> \
  --expected-fingerprint sha256:<exact-plan>
```

The fingerprint binds the exact candidate projections, run/count scope and
verified backup identity. Live publication/cost/hold aggregates are captured
in the separate `pre_change_digest`; unrelated activity before apply may
change that digest without invalidating unchanged target approval. Apply still
acquires `BEGIN IMMEDIATE`, captures the current non-target snapshot and rolls
back unless the same snapshot survives all bounded writes. This separation
keeps the runner usable while the automatic queue is live without weakening
target drift or non-target protection.

The runner accepts only current exact unanswered, unpublished rows with no
cost/publication/revision conflict. For the Node incident it may select more
than the newest observed failure when older exact terminal rows were adopted
into the same transition run; every selected row still requires its own
released zero-actual reservation and exactly one legacy conservative hold.
It archives the prior job projection, requeues the same processing identity,
preserves all holds/cost/audit evidence and creates no provider call, cost
event, publication aggregate or WB write. `readback` with the same fingerprint
must confirm the exact prior apply. Ad-hoc SQL and server-only drift are
forbidden.

## Production acceptance and rollback

Acceptance requires:

- deployed SHA equals the Release Train merge SHA;
- active run/cap and every global limit are unchanged;
- recovery readback proves exact affected rows and non-target invariants;
- a version observed after initial run start is admitted without a new preview;
- its bucket wins the next safe claim and rating-only does not bypass content;
- normal policy publication reaches exact WB GET readback;
- ordinary budget pause and bounded Node failure do not kill queue progress;
- `Отзывы → Отзывы` shows rolling counters/current priority and has no 5xx,
  page error, fatal surface or material console error.

Emergency rollback uses `WB_AUTOANSWERS_FORCE_OFF=true`. Additive v7 tables can
remain inert. Restore the verified pre-v7 database only for demonstrated
corruption and only after GET reconciliation of every ambiguous WB write.
