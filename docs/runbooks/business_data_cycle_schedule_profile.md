# Fixed business-data schedule profile (dormant)

One exact legacy → `business-data-cycle-3h-v1` transition is prepared here.
Installing this code activates nothing. The only configured file is the known
`50-business-data-cycle-profile.conf` drop-in of the existing warehouse timer.
There is no new service/timer or arbitrary installer, queue, owner-policy rewrite
or schedule JSON rewrite.

The fixed calendar is 00/03/06/09/12/15/18/21:00 Asia/Yekaterinburg, every day.
It resets the previous hourly OnCalendar, sets eight slots, one-second accuracy,
zero random delay and keeps Persistent=true. FBS is collected **in every 3h
cycle**. Nine hours is the maximum age, not collection cadence; current-day/full
generation proofs remain required. This PR records policy; readers retain their
existing defaults until separate reviewed wiring.

## Dependencies and raw intent

`activation_dependencies()` has three fixed unresolved code dependencies:
cycle dispatch, every conflicting legacy heavy producer, and FBS reader wiring.
Module presence does not prove readiness. No external flag, CLI override or
readiness file exists. Separately reviewed code must replace these blockers with
actual code-backed evidence. Current production apply refuses before mutation.

The sole cadence owner is `master_desired`. Required phases cannot be silently
skipped: raw `warehouse_functional`, `wb_finance_weekly` and `vitrina_refresh`
managed owner intent must be proven true under this conservative initial-profile
policy. This is not evidence that the current runner already obeys those owners.
Unknown/false blocks readiness; runtime/deploy projection refuses an enabled
complete cycle in that state. Future dispatch must check these raw owners before
effects. Current runner has no desired/source flag mapping: daily/FBS source
calls use canonical defaults; weekly no_due uses its own due policy. In particular,
warehouse owner is not asserted to own FBS source collection, and weekly owner
is not asserted to own daily source collection. Exact source-to-owner mapping
and hook integration remain part of the separately reviewed wiring dependency.

`projected_schedule(runtime, raw_feature_intent)` exposes raw intent plus a
separate effective schedule. Settings must preserve raw `activity.feature_intent`;
substituting the projection would create false owner drift. The exact target
proof binds raw control hash separately from profile fingerprint, deployed SHA
and effective timer states.

## Finite effective states

| Timer | Selected profile state |
| --- | --- |
| warehouse-functional-sync | enabled/active if master desired, otherwise disabled/inactive |
| fbs-warehouse-registry | disabled/inactive; collection belongs to every cycle |
| wb-finance-daily | disabled/inactive; source/derive belongs to cycle |
| wb-finance-weekly | disabled/inactive; source/cost eligibility belongs to cycle |
| sheet-vitrina-refresh | disabled/inactive; refresh belongs to cycle |
| web-vitrina-finished-snapshot | disabled/inactive; the same rolling14 publisher belongs to cycle |

Independent timers retain exact original enabled/active pairs during cutover.
Their schedules, full Finance backup cadence/policy, Autoanswers, FBS shadow,
buyer collector, observers and root-storage safety monitor are unchanged.
Before-write checkpoints remain before the corresponding writes.

## Exact transition and recovery

Use existing maintenance pause to obtain held. Preview requires the exact window,
unchanged raw controls/unit configuration, complete idle/jobs/process/service
drain and all classified timers paused. Every loaded unit digest is bound;
warehouse base must equal the reviewed repository base. Foreign overrides and
foreign bytes at the preset path refuse preview.

`apps/business_data_schedule_profile.py` actions are preview/status/apply/rollback.
Preview/status never provision state or run jobs. Common identity arguments are
runtime-dir/env-file/base-url/operation-id. Preview adds window-id/deployed-sha;
apply adds private reviewed-plan/expected-fingerprint/actor/reason. CLI verifies
completed deployment metadata and runtime SHA marker before preview/apply.
Save a preview with `umask 077`, inspect its full fingerprint/readiness/target,
then submit that exact plan once. This contract does not authorize production
application now; dependencies are deliberately unresolved.

The immutable plan/baseline and exact drop-in/selector before-images are retained
in private `.business-data-schedule-transitions/<operation-id>.json`. Phases:
prepared → drop-in → reload → selector → timers → verified target → committed
→ released. Audit and state are durable. Partial transition prevents deploy
reconciliation/legacy master restore before mutation. Missing selector after a
committed transition fails closed instead of restoring legacy ownership.

After an ambiguous answer first read status of the same operation. Continuation
compares the original plan, reads each actual file/timer and submits only a still
unfinished step. Reached states are not resubmitted; no new operation is created.
Committed recovery re-proves exact target before completing release and cannot
roll back. Target receipt honestly reports `exact_target_state_restored=true`
and `exact_prior_state_restored=false`. Original pause baseline is never replaced.
Ordinary pause resume and ordinary barrier release keep exact-prior requirements;
the separate fixed-target release requires committed private target proof.

Ordinary resume and central ordinary barrier release also reject partial/unknown
transition inventory before mutation, including prepared and preset-on-disk
before reload when loaded digests still match baseline. Both recheck under their
existing locks; the first prepared journal shares the barrier authority lock
and re-proves held binding. A competing direct release cannot strand the intent.
There is no caller bypass flag. Only exact committed target release or successful
durable rolled_back followed by ordinary exact-prior resume can complete it.

Before commit, rollback pauses all timers, proves drain, restores exact before
bytes/absence/mode/ownership and selector, reloads and proves original digests
and raw controls. It leaves the same barrier active; ordinary exact pause resume
restores original enabled/active pairs. If systemd still lists our removed preset,
only that exact missing path may use raw property readback, and all timers must
be proven paused before reload. Unknown missing/foreign content remains blocked.
Interrupted rollback continues the same operation. Before-images are retained.

Future deploy reconcile validates the exact preset, projects all six timer states,
adds warehouse owner even when absent from legacy enable list, and prevents
retired timers reopening. Active held pause keeps timers paused. Partial target
transition requires explicit recovery. Legacy master prepare/restore refuse a
nonlegacy profile before mutation; profile-aware master controls are separate work.

Persistent catch-up under the active barrier can be skipped. Receipt proves
scheduling configuration, not successful data update. No skipped cycle is
fabricated or replayed by this transition.

## Offline verification

The profile smoke covers legacy no-op, exact slots/TZ/FBS policy, dormant/disabled
phase refusal, foreign override/plain resume drift, every timer/transition
interruption, journal read from a new process, ambiguous enable/start without
resend, missing/existing drop-in rollback at timer/file/reload boundaries, partial
deploy/master refusal, committed drift and deploy under another held pause.
Existing pause/barrier/master restore/deploy/boundary smokes retain legacy coverage.
Fixtures are temporary and offline; no WB or production commands are required.
