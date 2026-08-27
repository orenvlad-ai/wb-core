# Autoanswers owner-policy v5

This release changes no SQLite schema and no frozen bundle artifact. New empty
stores start on `owner-policy-2026-08-08-v5`; existing stores retain their
persisted policy until the repo-owned v5 reconciliation is explicitly applied
after the exact deployed SHA is live.

The production sequence is:

1. hold the Autoanswers worker with the feature-owned lifecycle and leave the
   GET-only timer active;
2. deploy through the GitHub Release Train, which restores that held timer
   topology after its quiet window;
3. save an external `autoanswers-policy-v5-reconciliation dry-run` plan;
4. apply only that exact fingerprint while the worker timer/service readback is
   disabled/inactive;
5. require query-only `readback=status:reconciled`, exact counts and unchanged
   non-target digests;
6. use `autoanswers-lifecycle reconcile` to restore persisted feature intent.

The apply transaction owns the only permitted exception to pre-write
publication immutability. A zero-attempt publication may be atomically rekeyed
to the owner-policy reply hash while its prior key/reply hash/route and reason
are retained in append-only audit. Started writes/readbacks are never changed,
deleted, repeated or superseded.

The ordinary processing path has one separate typed semantic refusal:
`owner_policy_unsafe_public_reply`. It terminalizes only the exact processing
job, clears its lease and appends hash/pattern evidence before any publication
aggregate exists. If Node audit and settlement already committed, recovery
reuses that exact audited result, preserves the settled amount, performs zero
new provider calls and reaches the same `terminal_error`. Repeated worker ticks
cannot reclaim the terminal job. No generic `RuntimeError` is caught or
terminalized by this contract.
