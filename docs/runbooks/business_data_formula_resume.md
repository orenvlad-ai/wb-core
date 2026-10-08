# Resume after a canonical formula epoch update

Ordinary `business_data_maintenance_pause.py resume` still requires the original
unit configuration. An installed finished-snapshot service whose formula pin
changed intentionally cannot satisfy that exact-prior proof. Do not edit its
pause baseline, revert a deployed artifact, reset the owner or use allow-drift.

`apps/business_data_formula_resume.py` supplies one separate, explicit target
authority. It **does not install or edit units**, reload systemd, edit the formula
contract, change schedule/owner policy, run a data cycle or finish a deployment.
It restores the original timer enabled/active pairs and releases the same held
barrier only after its exact target is independently proven. Receipt fields are
`exact_target_state_restored=true`, `exact_prior_state_restored=false`.

## Required proof

- Existing canonical deploy owner is `complete`, with the exact operation,
  window, original baseline fingerprint and expected 40-character SHA. Owner
  schema remains unchanged. Missing, prepared, unfinished or foreign owners fail.
- App `.wb-core-runtime-sha` and `.wb-core-deploy.json` prove the same completed
  SHA; the saved active Europe target binds that app, runtime and managed unit
  directory. Registry service is loaded/active with a positive MainPID.
- Current native history contract has a valid formula epoch computed from its
  full hash map; every named actual formula source file passes its saved hash.
- Only `wb-core-web-vitrina-finished-snapshot.service` may differ. Its installed
  fragment is byte-for-byte the canonical artifact in that completed deploy.
  Loaded ExecStart static command has exactly one literal formula-epoch replacement. All other
  loaded configuration properties, fragment and drop-in paths remain exact.
- Reverse exactly that one pin in installed bytes. Hash the reconstructed
  fragment plus **all actual unchanged drop-ins in their loaded order** and
  paths, using native SystemdClient's serialization. That aggregate must equal
  the original baseline UnitContentDigest; actual after bytes must also equal
  the current loaded aggregate. This rejects any additional artifact edit even
  when installed bytes match a newly changed canonical artifact.
- All other unit configurations and raw controls remain unchanged. Service
  enabled/activity pairs match the saved drained hold; timer pairs are initially
  disabled/inactive, then must match their exact original pairs. Admission,
  authenticated activity coverage, jobs, processes and services must prove idle.

Evidence reads are bounded regular files without symlinks (including parent
paths). No missing input, timeout, unknown response, caller success flag or
synthetic substitute grants authority. A drifted or unverifiable proof leaves
the barrier held and requires exact same-operation status/readback.

Loaded configuration comparison parses the COMPLETE native serialization.
ExecStart preserves ordered `path`, `argv[]`, `ignore_errors`; only recognized
`start_time`, `stop_time`, `pid`, `code`, `status` execution tails are separated.
Recorded terminated PID/time is not a live process: current MainPID, service
state, processes and authenticated admission still prove idle. Timers preserve
ordered exact OnCalendar (including timezone) or known On*USec interval operands;
only recognized `next_elapse` is separated. Valid UTC timestamps/durations,
execution numbers and terminated/reset states are checked. This narrow grammar
does not support arbitrary systemd serializations: unknown/partial/duplicate
records, extra fields/tokens, delimiter/control injection, invalid values or
missing-versus-empty config evidence fail closed, even when raw strings match.
Every foreign unit still needs the SAME valid UnitContentDigest, fragment path,
drop-ins and all other loaded configuration. No digest fallback or recursive
normalization. Original raw baseline/fingerprint and final raw receipt remain
unchanged; volatile fields do not enter delta/authority equality. The committed
receipt uses this SAME strict comparison when the timer deadline advances.

## Review, apply, recover

Use the existing maintenance pause and completed canonical deploy first. This
feature is an explicit release prerequisite; it does not perform either step.
The prerequisite itself changes no unit or formula contract and can therefore
be deployed with the old ordinary exact resume before the formula-changing
release. Root reviews the change before any production use.

### One bounded recovery when the formula-changing deploy is already held

If the completed original deploy changed the formula pin and its older resume
tool rejects ONLY the recognized runtime tails above, another canonical deploy
claim cannot pass the original baseline. Do not reset claim or rewrite baseline.
Root may authorize a private recovery ONLY after independent review and trusted
Gate PASS of the exact fix. This is no generic force/fallback command.

Use a full isolated verified candidate tree whose exact base equals the actual
installed runtime SHA. Its ONLY differences are these four reviewed paths:
`packages/application/business_data_formula_resume.py`,
`apps/business_data_formula_resume_smoke.py`, this runbook, and
`docs/runbooks/business_data_cycle_deploy_protection.md`. Retain private full Git
identity, candidate/archive/file manifests and actual installed source hashes.
Independently prove every other applicable candidate runtime source equals the
installed source and that the installed app is unchanged before/after recovery.
No extra/untracked executable shadow files or symlink source paths are allowed.
Do not copy candidate files into the installed app or mutate production business
databases, formula files, units, controls, owner or baseline. Only the existing
resume timer/barrier/private transition actions are authorized; no new cycle.

The unchanged CLI runs from that FULL private tree in isolated Python mode
(`-I`), with canonical package imports and no PYTHONPATH/alias overlay. Prove all
apps/packages import origins/hashes belong to that reviewed tree; native barrier
and wakeup must resolve the SAME canonical formula module object as the CLI.
`--app-dir` remains the actual installed app, which _authority reads independently
with the original completed deploy owner, expected SHA, window, original baseline,
installed artifact and native formula hashes. Explicit runtime/env/systemd paths
remain actual native authority, never candidate fixture defaults. The candidate
must never impersonate the installed app or supply replacement runtime metadata.
Save exact same-operation preview privately, root reviews its fingerprint and
full evidence, then submit one apply and use same-ID status/readback for any
uncertain result. Partial recovery requires the SAME reviewed candidate. Saved
committed/released schemas remain compatible with the original installed helper
and native wakeup; no fresh candidate receipt format is invented. After proven
resume, a fresh native pause/window and ordinary canonical release can deploy the
fix. No new deploy claim is attempted under the blocked original window, and
canonical deploy guards remain intact.

Preview/status never provision transition records or submit timer commands.
Preview takes `--runtime-dir`, `--app-dir`, `--env-file`, `--operation-id` (the
exact deploy owner operation), `--window-id`, `--expected-sha`. Save the emitted
plan in a private regular file (`umask 077`) and inspect its full fingerprint,
before/after contents, exact owner/runtime bindings and original baseline.

Apply takes the same identity plus `--reviewed-plan`, `--expected-fingerprint`,
`--actor`, `--reason`. It acquires the existing restore lock, independently
rebuilds preview before the first intent, and records the immutable reviewed plan
under `.business-data-formula-resume/<operation-id>.json`. Phases are prepared →
restoring → committed → released. Full prepared proof and committed receipt are
also preserved in the private append-only audit. Original pause baseline is never
replaced. Ordinary resume/direct exact-prior barrier release reject any partial
formula transition; their original configuration guard stays strict.

Each timer command is preceded and followed by live configuration, ownership,
formula, control and activity checks. A lost/ambiguous timer response stops the
attempt. Recovery reads the same plan and actual timer pair; an already observed
enable/start/disable/stop is not repeated. The typed barrier release reads the
committed record and independently verifies live proof again under the existing
barrier lock. It releases last. Missing or released barriers cannot authorize
completion of an uncommitted transition.

After fresh committed proof and before release, apply retains that exact receipt
as the original pause's `restore_readback`. Its baseline and phase stay unchanged.
The typed release independently requires the retained receipt to match before it
arms debt or opens admission. This ordering makes native wakeup authority durable
even when the process crashes immediately after barrier release, before metadata
bookkeeping. Same-operation recovery preserves the already retained receipt.

After a crash at final release, recover the same reviewed plan: verify the actual
original typed barrier release (operation/window/baseline/plan and exact receipt
readback fingerprint), then finish durable bookkeeping. This historical path
does not inspect current idle, timers, runtime SHA or services: lawful work may
already have started after admission opened. It performs no timer, deploy,
barrier or wakeup action. The archived original release proof is retained in the
released transition alongside its dated receipt.

A completed repeated apply returns that saved dated receipt through read-only
historical validation, including after a later lawful window/deploy/activity.
It is no new live-restoration claim and changes no timer/control/state record.
It does not rearm missed-slot debt or submit a cycle. Until bookkeeping becomes
released, new pause/preflight are blocked before and under the restore lock;
native barrier acquire is also guarded under its lock, preventing any new
maintenance window from replacing the sole original barrier proof during this
gap. Thus a crash after pause-restored but before transition-released remains
recoverable without replacing the immutable original baseline.

The validated typed release retains the existing native missed-slot wakeup:
same window/baseline/operation/receipt binding, original enabled/active warehouse
owner and current raw feature readiness. Its existing 7200-second remaining-time
boundary, accepted-slot guard, one-submit transport and subsequent-slot semantics
are unchanged. No fake exact-prior flag is passed to ordinary wakeup authority.

## Offline evidence

`apps/business_data_formula_resume_smoke.py` uses temporary synthetic files and
fake command responses with **real SystemdClient content hashing**, native
pause/deploy-owner/barrier APIs and the actual canonical artifact/formula path
set. It tests exact restore, immutable baseline, read-only repeat, ordinary guard,
foreign edits/drop-ins/loaded commands, invalid owner/SHA/metadata/source hashes,
symlinks, unknown/timeout, before/after action drift, forged release and crashes
through release. Typed wakeup tests cover exact7200 seconds versus one microsecond
later, native dispatch immediately after a release crash without bookkeeping,
and no rearm on recovery. Further regressions cover running jobs/new runtime
after release, the pause-restored bookkeeping gap and in-lock new-pause race,
foreign release/readback identities, and historical read-only repeat after a new
window with an active deploy. No production, SSH, external API or real systemd
command is involved.
