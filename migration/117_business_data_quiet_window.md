# Migration 117 — audited business-data quiet window

## Problem

Canonical deploy intentionally enables the Web-vitrina, closure retry, Finance, auto-complaints and SPP tick timers. The existing warehouse and autoanswers controls did not provide one evidence-bound way to freeze every automatic business-data writer before a production-data maintenance transaction, and stopping only a ticker did not disable its server-owned schedule.

## Current contract

`python3 apps/registry_upload_http_entrypoint_hosted_runtime.py business-data-maintenance status|hold` is the only cross-writer maintenance entrypoint.

`hold`:

- disables the five target-managed business timers;
- saves Web-vitrina, complaints and SPP runtime schedules with `enabled=false` through the authenticated loopback APIs;
- deactivates autoanswers through its existing lifecycle and disables both force-off timers;
- obtains a durable warehouse maintenance hold, waiting instead of killing an active oneshot;
- inventories all `wb-core-*.timer` units, paired services, relevant cron entries, writer processes and shared locks;
- fails closed for any unknown timer, cron writer, active process, active runtime job or held writer lock;
- stores the exact pre-hold runtime schedule/systemd evidence and final readback in mode-`0600` state/audit files.

Registry HTTP, Data MCP, Release Train and deploy infrastructure remain active. The runner has no implicit restore command. After any later deploy, `hold` and `status` must be repeated because deploy may re-enable managed timers.
