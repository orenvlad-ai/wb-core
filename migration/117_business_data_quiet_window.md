# Migration 117 — audited business-data quiet window

## Problem

Canonical deploy intentionally enables the Web-vitrina, closure retry, Finance, auto-complaints and SPP tick timers. The existing warehouse and autoanswers controls did not provide one evidence-bound way to freeze every automatic business-data writer before a production-data maintenance transaction, and stopping only a ticker did not disable its server-owned schedule.

## Current contract

`python3 apps/registry_upload_http_entrypoint_hosted_runtime.py business-data-maintenance status|hold|restore|set-process` is the only cross-writer maintenance entrypoint. The same server-owned contract backs `Настройки → Автообновления`; UI and CLI do not maintain separate desired-state truth. `set-process` requires an exact optimistic revision, an allowlisted process key and audited actor/reason and exists for bounded owner-controlled correction of desired state before restore.

`hold`:

- disables the five target-managed business timers;
- saves Web-vitrina, complaints and SPP runtime schedules with `enabled=false` through the authenticated loopback APIs;
- deactivates autoanswers through its existing lifecycle and disables both force-off timers;
- obtains a durable warehouse maintenance hold, waiting instead of killing an active oneshot;
- inventories all `wb-core-*.timer` units, paired services, relevant cron entries, writer processes and shared locks;
- fails closed for any unknown timer, cron writer, active process, active runtime job or held writer lock;
- stores the exact pre-hold runtime schedule/systemd evidence and final readback in mode-`0600` state/audit files.

Owner policy is versioned separately in mode-`0600` `.auto-updates-policy.json` plus append-only audit. Initial migration derives each of the eight allowlisted processes only from canonical pre-hold evidence; unknown provenance stays `OFF/UNKNOWN`. `master OFF` preserves every individual desired state, while an individual change under the master hold changes only future resume intent.

An individual `ON` that requires a separate lifecycle contract is rejected before
policy write or hold. In particular, the Settings control plane may turn an
erroneous Autoanswers desired state back `OFF`, but cannot enable either
Autoanswers timer. A no-op and a stale revision are controlled conflicts and do
not append audit or start a quiet window.

`restore --expected-revision <N>` requires the exact optimistic policy revision, a quiet readback, no unknown timers/cron/writers/locks and no unknown desired state. It restores only desired `ON` processes and leaves intentional `OFF` processes off, then verifies actual timer/schedule state process by process. Successful restore closes that hold generation; the next hold captures a fresh timer/schedule baseline. Partial failure disables the allowlist again and remains fail closed. Autoanswers `ON` is never inferred and still requires its dedicated lifecycle contract. Registry HTTP, Data MCP, Release Train and deploy infrastructure remain active. After any later deploy, `hold` and `status` must be repeated because deploy may re-enable managed timers.

Settings POST returns success only with a mutation receipt proving revision
advance, persisted desired state and runtime readback. The browser immediately
performs a second GET and compares revision, policy fingerprint and requested
desired/actual state. Rejected/no-op actions and post-write readback mismatch keep
an error visible after the factual state is reloaded; a successful GET must never
erase the preceding mutation error.
