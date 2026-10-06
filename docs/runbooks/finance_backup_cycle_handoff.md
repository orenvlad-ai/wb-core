# Dormant Finance backup → cycle handoff

This candidate connects only the existing internal cycle start hook. There is no
HTTP route, dispatch CLI, timer, selected profile, cadence activation or readiness
claim. Ordinary daily backup service and canonical plan/apply/CAS/copy/restore/GC
semantics remain the recovery path.

## Fixed boundary

`cycle_backup_priority` reads bounded signed policy/current/retained/pending/intent
metadata and source stat identities. It never scans SQLite or hashes backup bytes.
No-policy/no-due paths need no systemd access or lock provisioning. Priority is
checked before cycle acceptance and again after the actual cycle heavy EX is
acquired. That parent lease is not entered/bound: it is transferred to the actual
worker on acceptance. A raced backup priority closes it before service handoff.
A backup-priority response accepts no cycle receipt and starts no cycle sources.
Matching existing cycle receipts remain readback, not new acceptance.

`handoff_cycle_backup` can dispatch only
`wb-core-finance-backup-rotation.service`. Production runtime/app paths are fixed
`/opt/wb-core-runtime/state` and `/opt/wb-core-runtime/app`. It verifies the exact
root-owned non-writable artifact/installed unit, deployed SHA, loaded canonical
command, oneshot type, working directory, execution user and no drop-ins/reload/
extra commands/root/environment overrides. Bounded hashes cover canonical entry
code, not data. Unsafe or unsupported deployment identity refuses before start;
fixtures inject a private fake service class, not a public runtime/command bypass.

The existing `.finance-backup-admission.json` remains the only handoff intent.
Top statuses stay `waiting`/`resolved`. Under its existing nonblocking metadata
lock, one launch records attempt/unit/SHA/execution identity/prior InvocationID and
`prepared` **before** the fixed start command. There is no parent-held heavy lease
while the service acquires its own lease. Start success records submitted, not
backup completion. Start timeout/error/cancellation or a prepared crash remains
unknown; inactive/failed unit alone never proves that backup was not accepted.
Concurrent dispatch reads the same intent and sends no second command.

Only the actual `scheduled_rotation` produces canonical acknowledgement for the
request observed before its effects. Completion uses the unchanged verified
transaction/selector path. A positively acknowledged deferred/no-backup-effects
attempt may dispatch later only when the exact unit is terminal and its
InvocationID matches that acknowledgement. InvocationID is evidence, not lease
or reentry authority. Other unknown/failed starts use natural canonical daily
recovery or explicit investigation; no forced source/copy resend is added.
Same-SHA pending canonical recovery applies the stored reviewed plan; another SHA
or unknown metadata refuses. The intent is not replaced while a launch is unknown,
even if a crash already changed current.json.

## `not_due` proof

Canonical not-due returns a small consumed proof from the **actual plan**:
source stat fingerprint from its canonical guard, current selector fingerprint,
policy fingerprint and plan fingerprint. Acknowledgement requires the consumed
source/current/policy to match both the pre-plan and post-plan metadata views.
A short operator commit during the plan cannot become authority merely because a
fresh post-plan stat sees it. Unproven relation remains waiting/not_due_unproven.

A resolved not-due proof may allow a cycle only while those same three identities
still match freshly read metadata, deployed SHA matches, no pending transaction
exists and the RPO deadline has not arrived. Any source/policy/selector drift
invalidates that proof. The normal cycle acceptance rechecks under acquired EX.

Defaults remain earliest eligible changed-source slot at6days and RPO/age cap7days.
RTO4hours is restore policy. The existing service TimeoutStartSec43200 is12hours;
neither proves backup duration. Backup has a separate slot: this code does not
claim that backup plus the cycle fits3hours or that all legacy producers are
serialized. Unknown duration/latest-safe-start remains a scheduling proof gap.

## Evidence / later readback

`python3 apps/finance_backup_handoff_smoke.py` uses fake systemd and real canonical
backup functions against disposable SQLite/filesystems. It covers real-process
reservation, nested/busy heavy, exact pending recovery without replan/copy resend,
preaccept cycle gates and priority race, lost start responses, prepared crash after
selection, same/other SHA, consumed-plan acknowledgement race, metadata bounds,
6day/7day policy, and fixed unit identity/override refusal. No production unit or
backup destination is called.

After a separately authorized deployment, inspect only fixed unit properties,
deployed identity, signed intent, pending transaction/selector and cached cycle
receipts. Confirm no cycle receipt/source stage before canonical backup terminal,
no repeated start for an unknown prepared request and exact not-due fingerprints.
Do not dispatch a live backup/cycle to verify this dormant candidate. Readiness,
full schedule/profile wiring, all remaining producers and total cycle/history
completion budget remain separate acceptance dependencies.
