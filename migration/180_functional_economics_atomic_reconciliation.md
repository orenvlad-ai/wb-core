# Migration 180: atomic functional-economics reconciliation

## Failure family

An ordinary hourly functional-economics publication could make a newly exact
closed date in pass one and add its provisional inventory repair signal only in
pass two. The built-in repeat no-op guard therefore failed after the first pass
had already committed ready-snapshot rows. That commit also preceded the
Recovery Policy `after_digest`, so the durable operation appeared
`failed_recoverable` without recording that its exact after-images were live.

## Same-pass closure

For a non-targeted date earlier than the pinned operation business date, a
complete exact warehouse roster plus an exact functional version is a
newly-closed candidate even when prior coverage is `partial` or `unavailable`.
The first pass publishes the exact cells and adds any provisional inventory
repair signals in the same plan. It retains
`ordinary_publication_applied=true`; the second pass preserves the closed
version, cells, presentation, coverage and repair registry byte-semantically
and produces zero updates. The established guard for an already closed date is
not relaxed: a different candidate version still creates repair evidence and
cannot rewrite the frozen cells.

## Atomic commit truth and old-operation reconciliation

Immediately before committing ready-snapshot rows and their T1 undo manifest,
the same SQLite transaction records the manifest digest and complete
non-target digest in the Recovery Policy operation. Post-commit readback can
still fail, but it cannot make the operation look unsubmitted or authorize a
second business attempt.

The two hosted commands
`economics-backfill-commit-readback` and
`economics-backfill-commit-retain` are the only forward-reconciliation path for
an already committed operation. Readback opens the operational store with
`mode=ro` and `PRAGMA query_only=ON`, binds the exact operation, T1 plan, ready
undo manifest and every current after-image, recomputes the whole
ready-snapshot non-target digest and emits a canonical evidence digest with
zero writes. Retain requires that exact evidence and the shared warehouse
writer lock, writes only the existing operation plus one transition, and binds
the evidence digest. It never changes ready rows, starts a new operation or
repeats an hourly/business submit. Drift remains fail closed and leaves the
rollback evidence intact.

## Incident acceptance

The WBC0027 incident capsule continues to require its exact qualified manifest,
one human-authorized Apply and independent readback. Terminal producer control
also requires at least one later natural collector cycle with both the
top-level collector and nested `lifecycle_processor` successful, followed by a
fresh public Vitrina readback. Top-level success alone is insufficient.

## Verification

The functional smoke reproduces `partial/unavailable -> newly exact closed`
with provisional cost evidence and proves the corrected second pass is a full
no-op. It also forces a post-commit non-noop, proves the commit digest survived,
replays the legacy missing-digest shape, rejects non-target drift and completes
only one metadata retain transition with zero business-row writes.
