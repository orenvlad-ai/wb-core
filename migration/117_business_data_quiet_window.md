# Migration 117 — audited business-data quiet window

## Problem

Canonical deploy intentionally enables the Web-vitrina, closure retry, Finance, auto-complaints and SPP tick timers. The existing warehouse and autoanswers controls did not provide one evidence-bound way to freeze every automatic business-data writer before a production-data maintenance transaction, and stopping only a ticker did not disable its server-owned schedule.

## Current contract

`python3 apps/registry_upload_http_entrypoint_hosted_runtime.py business-data-maintenance status|hold|restore|set-process` is the only cross-writer maintenance entrypoint. The same server-owned contract backs `Настройки → Автообновления`; UI and CLI do not maintain separate runtime truth. `set-process` requires an exact optimistic revision, a Settings-owned process key and audited actor/reason.

`hold`:

- disables the five target-managed business timers;
- disables Settings-owned systemd tickers while preserving the canonical Web-vitrina runtime schedule JSON unchanged;
- suspends auto-complaints and SPP through their feature-owned lifecycle/schedule APIs without replacing their desired state;
- suspends Autoanswers through its dedicated lifecycle, disabling readonly and worker timers while preserving its feature mode, transition run and cap;
- obtains a durable warehouse maintenance hold, waiting instead of killing an active oneshot;
- inventories all `wb-core-*.timer` units, paired services, relevant cron entries, writer processes and shared locks;
- fails closed for any unknown timer, cron writer, active process, active runtime job or held writer lock;
- stores the exact pre-hold runtime schedule/systemd evidence and final readback in mode-`0600` state/audit files.

Owner policy is versioned separately in mode-`0600` `.auto-updates-policy.json` plus append-only audit. Policy v2 contains only Settings-owned individual desired state. Feature-owned process cards are derived read-only from their canonical feature state and actual runtime. Legacy generic Autoanswers desired entries are ignored rather than migrated as intent. `master OFF` preserves every individual and feature-owned desired state, while a feature mode/schedule change under hold becomes the intent used by the next lifecycle-aware resume.

`set-process` rejects every monitoring-only process before policy write or hold,
including Autoanswers, auto-complaints and SPP test. Their only individual
control surfaces are `Отзывы → Отзывы`, `Отзывы → Авто-жалобы` and the SPP
functional section. A no-op and a stale revision are controlled conflicts and
do not append audit or start a quiet window.

`restore --expected-revision <N>` requires the exact optimistic policy revision, a quiet readback, no unknown timers/cron/writers/locks and no unknown desired state. It restores Settings-owned processes from owner policy and invokes each feature-owned canonical lifecycle using its current feature intent. For Autoanswers it never directly enables timers and never creates a transition run; lifecycle reconciliation verifies the existing mode/run/cap and returns starting until a fresh scheduler tick. Successful restore closes that hold generation; the next hold captures a fresh baseline. Partial failure disables the allowlist again and remains fail closed. Registry HTTP, Data MCP, Release Train and deploy infrastructure remain active. After any later deploy, `hold` and `status` must be repeated because deploy may re-enable managed timers.

Settings POST returns success only with a mutation receipt proving revision
advance, persisted desired state and runtime readback. The browser immediately
performs a second GET and compares revision, policy fingerprint and requested
desired/actual state. Rejected/no-op actions and post-write readback mismatch keep
an error visible after the factual state is reloaded; a successful GET must never
erase the preceding mutation error.
