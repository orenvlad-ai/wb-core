# Fixed business-data schedule profile (explicit selection)

One exact legacy → `business-data-cycle-3h-v1` transition is prepared here.
Installing this code activates nothing. The only configured file is the known
`50-business-data-cycle-profile.conf` drop-in of the existing warehouse timer.
There is no new service/timer or arbitrary installer, queue, owner-policy rewrite
or schedule JSON rewrite.

The fixed calendar is 00/03/06/09/12/15/18/21:00 Asia/Yekaterinburg, every day.
It resets the previous hourly OnCalendar, sets eight slots, one-second accuracy,
zero random delay and keeps Persistent=true. FBS is collected **in every 3h
cycle**. Nine hours is the maximum age, not collection cadence; current-day/full
generation proofs remain required. Readers use the selected profile's reviewed
freshness policy; an absent selector retains their legacy defaults.

## Dependencies and raw intent

`activation_dependencies()` is code-owned ready: independent Linux proof covers
an actual operator Thread, closed-date/current ready publication and the actual
owned history child with native acknowledgement. It is not production timing or
runtime availability evidence. No external flag, CLI override or readiness file
exists. Installing the code does not choose a selector or alter timer states.

Runtime readiness still requires raw owner intent and the existing safe canonical
heavy inode. Storage admission is a separate mandatory history-worker check:
exact root/mount identity, reserve, runtime contract/epoch and formula pins. It is
not included in `activation_readiness()`; source stages may precede this check.
Prove installed history storage and Linux worker infrastructure read-only before
the first target selection, then retain the worker's checks at each execution.
The synthetic integration proof does not show production completion within 3h.

The sole cadence owner is `master_desired`. Required phases cannot be silently
skipped: raw `warehouse_functional`, `wb_finance_weekly` and `vitrina_refresh`
managed owner intent must be proven true under this conservative initial-profile
policy. This selected-profile policy explicitly maps warehouse owner to mandatory official
FBS collection and warehouse materialization; Finance owner to daily source plus
weekly own-due source; vitrina owner to full auto_daily, ready and rolling14.
Raw vitrina schedule any-enabled is also required. Unknown/false blocks readiness
and enabled deploy projection; the dispatcher checks again under acquired heavy
before acceptance. This mapping is new profile policy, not a claim about legacy
scheduler behavior. No owner flag narrows source groups, metrics, dates or
selectors within full auto_daily. A false mandatory phase is blocked, never skipped.

The full `auto_daily` collection completes all original external source groups
and both native temporal slots once, including known missing/error/partial
outcomes. This is capture evidence, not a claim that every source is accepted.
The cycle receipt records exact accepted flags/digests separately from outcome
digests and bounded status/coverage metadata; provider notes and raw data are not
stored there. Native unavailable observations degrade the capture stage. An
incomplete scope, inconsistent accepted proof, wrong-date consumed payload,
fabricated zero-fill or unproved complete/qualified-partial payload fails closed.
The current-only rollover and native backoff rules still determine whether a
source is fetched; a completed slot is not a promise of a new provider request.

Finance, full official FBS and warehouse phases validate their own canonical
operands and versions. Missing vitrina SPP/buyer/display stocks do not replace
those operands or prevent unrelated phases from running. The same retained
capture is derived once after the material stages, with unchanged source/material
CAS. A technically complete current ready publication with semantic warning/error
remains a truthful degraded report; `ready_semantic_status` records the exact
status. This does not fill missing cells with zero or claim accepted sources.
Technical publication failures and native history guards still fail the cycle.
Old dated publication and exact closed-date/native acknowledgement retain their
stricter existing proofs; current report degradation cannot acknowledge old debt.

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
| sheet-vitrina-closure-retry | disabled/inactive; bounded closed-date retry belongs to cycle |

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
then submit that exact plan once within the authorized cutover. A plan prepared
with code readiness false remains invalid after a code update: obtain and review
a fresh exact plan; never rewrite its captured proof or fingerprint.

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

Future deploy reconcile validates the exact preset, projects all seven timer states (one owner, six retired),
adds warehouse owner even when absent from legacy enable list, and prevents
retired timers reopening. Active held pause keeps timers paused. Partial target
transition requires explicit recovery. Legacy master prepare/restore refuse a
nonlegacy profile before mutation; profile-aware master controls are separate work.

Persistent catch-up under the active barrier can be skipped. Receipt proves
scheduling configuration, not successful data update. No skipped cycle is
fabricated or replayed by this transition.

## Offline verification

The profile smoke covers legacy no-op, exact slots/TZ/FBS policy, unpatched code
readiness/exact target, stale false-plan and disabled/unknown owner/infrastructure
refusal, foreign override/plain resume drift, every timer/transition
interruption, journal read from a new process, ambiguous enable/start without
resend, missing/existing drop-in rollback at timer/file/reload boundaries, partial
deploy/master refusal, committed drift and deploy under another held pause.
Existing pause/barrier/master restore/deploy/boundary smokes retain legacy coverage.
Fixtures are temporary and offline; no WB or production commands are required.

## Fixed dispatch and Settings mapping

The existing warehouse hourly-sync command dispatches the selected profile to
`/v1/business-data-cycle/dispatch` on the fixed local HTTP daemon. It does so
before warehouse constructors/job locks/heavy EX and keeps only the existing
short maintenance SH plus its nonblocking transport lock. No new unit is added.
The route requires configured session authentication, Settings/admin authority
and a loopback caller. It accepts only a server-issued opaque dispatch ID.
Client root/epoch/date/source/ownership selection is forbidden.

GET preparation and same-ID readback are read-only: no job, directory or source
acceptance. The deterministic ID binds latest completed wall-clock 3h slot in
Asia/Yekaterinburg, fixed profile, deployed SHA and server-owned history contract
(with existing 240s portion/max31 limits). POST delegates to canonical same-daemon
cycle acceptance; backup priority/recheck and actual lease handoff are preserved.
The ID grants dedup evidence, never heavy ownership. Canonical source stages
require the actual same-thread live cycle lease; names, copied IDs and forked
ContextVars do not grant exemption.

One private bounded atomic `.business-data-cycle-dispatch.json` transport record
is written before the single POST. Unknown POST/404/timeout/process loss means
same-ID GET only, including after slot rollover/restart. No negative idle proof
causes another POST. A still active or uncertain old request blocks a newer slot.
Known terminal/no-accept permits one preparation in the same launcher invocation.
A different latest-slot ID receives at most one new POST; the old POST is never
resent. Same-slot terminal results remain read-only. This prevents resolution
of a previous completed operation from consuming the sole next 3h timer slot.
Current-slot absence remains
unknown. After rollover, exact absent+expired proof is checked under the operator
lock and the existing heavy inode EX opened read-only/nonblocking (no provision).
Busy, missing, unsafe or replaced heavy infrastructure cannot prove no acceptance;
absence remains unknown. Selected activation requires proven existing canonical
heavy infrastructure. Neither GET nor readback repairs/provisions an inode. Canonical POST repeats slot,
deployed SHA, history contract and selected-profile guard immediately before
durable acceptance under actual EX plus the same operator lock. A paused old
handler that passed the guard retains EX until receipt/terminal; another process
cannot prove false absence in that gap. A handler still before the guard cannot
later accept the expired request. Only this server proof resolves uncertain
transport and permits a new slot; old POST is never resent. Unknown404/timeout
alone resolves nothing. Session-secret rotation fails closed rather than
silently discarding unknown intent.

Selected legacy health06:30/night/auto tick/scheduled/synchronous/direct refresh,
group/manual warehouse, daily/weekly/FBS source CLI and ordinary no-date closure
launcher return cycle-managed before source/job/ledger effects. HTTP manual
trigger refusal is 409 with accepted=false. Existing pending warehouse intent
remains pending without standalone pickup; no false completed/consumed receipt
is manufactured. Explicit approved date recovery retains its existing separate
maintenance/backup/CAS contract. Source-free cost replay and saved-source operator
acceptance are unchanged. Absent selector preserves legacy behavior; malformed
selector fails before dispatch or schedule save.

Settings returns raw rows/policy/fingerprint separately from eight effective
24/7 slots and exact owner timer. Raw file byte digest is distinct from projection
fingerprint; existing 0644 raw source JSON is read bounded/NOFOLLOW without format
or mode rewrite, while private transport/profile metadata stays 0600. Maintenance
activity and controls explicitly use the raw helper, never projected slots.
Cadence editing/run-now is marked cycle-managed; saved raw intent remains intact.

Old dated ready publication reuses the canonical CAS/save/complete receipt with
exact as-of and current bundle verification, without repeated collection, promo
GC or manual-result tails. Current ready is still published last by the separately
reviewed cycle wiring. History passes only immutable dates and the actual internal
ClosedBacklog object to the independently owned helper. Module presence alone
does not prove completion; the independently reviewed whole-cycle Linux test uses
the real operator Thread and owned child. Final production source/history/total
duration, mount pressure and backup conflict still require controlled measurement.

## First cutover and controlled pilot

After the current full backup has terminal proof, obtain one fresh held
maintenance window with its immutable old baseline. Release the reviewed code
union while the selector is absent, then prove the exact installed SHA/completed
deploy metadata, services/admission, unchanged unit baseline, formula/repair pins
and the separate history storage contract. No SHA change while backup is pending.

Profile CLI checks `.wb-core-deploy.json` version2, `deployment_complete=true`
and exact commit/SHA marker. It does not require Change Registry source completion.
Save/review a fresh exact target preview for this same held window and installed
SHA; apply the same operation once. Unknown outcomes read back the same operation.
Pre-commit recovery may roll back; committed recovery proves only the exact target.
Target timer proof precedes barrier release. Ordinary resume is never a bypass.

Held maintenance can make the release's Change Registry activation return
`skipped_maintenance`; this is not activation completion evidence. After exact
target restore/release use the existing canonical
`wb-core-change-registry-activation@<finalSHA>.service` and
`apps/change_registry_observer.py ... activation-status --deployed-sha <finalSHA>`.
Submit once, then read the same unit/job on unknown outcomes. Require actual
terminal complete and source evidence. This observer acquires its own Prices/Ads
snapshot; selected-profile legacy Finance/FBS/refresh suppression does not block it.
No second maintenance window or new activation workflow is required by this chain.

Timer restoration/release can start a selected cycle before a manual pilot,
including persistent catch-up. Choose cutover time against the next fixed slot
and read the exact current slot receipt; a prior acceptance becomes that pilot's
readback, never a duplicate request. Use the fixed authenticated dispatch/server
ID for the full-cycle pilot; unknown POST means same-ID GET, never old POST resend.
Prove terminal ready/CURRENT/native acknowledgement and measure production phases.
The target receipt proves scheduling configuration, not successful data update;
code readiness and restore RTO do not provide a backup or cycle duration SLA.
