# Migration 118 — Autoanswers lifecycle and auto-update ownership

## Preserved production intent

This migration is additive. It preserves the current Autoanswers feature
settings, `policy_epoch`, exact transition run, immutable membership and run cap.
Legacy `autoanswers_readonly` / `autoanswers_worker` values in the generic
auto-updates owner policy are not business intent and are not migrated. A valid
current run is resumed; no replacement preview or run is created.

The policy-v1 quiet-window implementation disabled feature schedule rows in
runtime JSON. When this release resumes a hold created by v1, it performs one
bounded compatibility restore from that hold's exact pre-hold schedule baseline
before saving owner-policy v2. Later v2 holds never rewrite schedules, so mode or
schedule changes made while paused remain the latest feature intent.
The active deploy manifest installs and reloads business timer units but does
not enable or restart them; an acquired hold therefore survives deployment
until the explicit lifecycle-aware restore.

Before deploy, capture read-only incident evidence with
`apps/wb_autoanswers_incident_evidence.py`. Hold all business-data writers through
the canonical `business-data-maintenance hold` boundary and retain the exact
feature/run/cap/schedule evidence.

## Schema and budget boundary

Schema v6 adds the append-only
`sheet_vitrina_v1_wb_autoanswers_budget_uncertainty_holds` table. If the readback
shows `budget_state_unknown`, run
`apps/wb_autoanswers_budget_reconciliation.py dry-run`, review the exact
provider-started reservation/job evidence, then apply only the returned
fingerprint. Apply records the maximum per-review reservation as a conservative
cap hold and append-only audit. It does not claim that amount was spent and does
not write zero or guessed actual cost.

An absent transition-run cap is a controlled blocker. Migration must not invent
or widen it.

## Runtime reconciliation

After deploy, resume through `business-data-maintenance restore`. The generic
control plane calls the dedicated Autoanswers lifecycle; it does not manipulate
Autoanswers timers directly. Mode mapping is:

- `off`: readonly timer on, worker timer off;
- `manual`, `draft_only`, `auto_safe`, `auto_all`: readonly and worker timers on;
- global master pause: both timers off while feature intent remains unchanged.

Readback must prove persisted settings, exact run/cap, timer enabled/active state
and no drift. `starting` remains non-success until a post-request scheduler tick.
After the grace interval, a stale tick is `worker_unavailable`.

A typed owner-policy semantic refusal
`owner_policy_unsafe_public_reply` is a successful bounded worker outcome, not
a lifecycle failure: the exact job is durably terminal, its lease is absent and
the next natural timer-owned tick may select later work. This path never
requires or authorizes lifecycle reconciliation, a manual service run or a
timer change. Untyped owner-policy/runtime invariant failures retain the
ordinary process-error lifecycle.

## UI ownership migration

The Web-vitrina schedule editor moves from `Витрина` to the
`Настройки → Автообновления` Vitrina card and continues to use the existing
runtime JSON and HTTP routes. No schedule row or policy is copied or reset.
Those existing routes become Settings-authorized after the move, so
Vitrina-only access cannot use the old path as a hidden mutation surface.
Functional indicators read a separate GET-only
`/v1/sheet-vitrina-v1/auto-updates/status` endpoint.
Autoanswers, auto-complaints and SPP test render as monitoring-only cards in
Settings and reject direct `set_process`. Their individual controls remain only
in their functional sections.
