# Migration 159 terminal addendum: WBC0008 blocks 024, 026 and 027

This is the authoritative repo-only terminal-receipt addendum to
`159_root_storage_warm_archive_wbc0008_006.md`. It does not change the archive
worker contract or authorize a new production effect.

## Existing submitted operation

The mode `warm-archive-receipt-reconciliation` is valid only for the already
submitted exact-six operation whose immutable Production Apply receipt is
exactly `blocked/post-submit-readback-not-reconciled`, with one submit and a
succeeded attempt-1 detached job. It binds the exact source PR/run/artifact
name and SHA-256, authorization and blocked comment ids, release/readiness/
operation/job and qualified-manifest identities, deployed SHA, plus the new
merged trusted-main `repo_only/done` Release receipt. No new owner authorization
is required: under the authorization router this is same-operation query-only
`AUTO_CONTINUE`, not a new production effect.

Exactly one bounded SSH probe is permitted after GitHub-only preflight. It runs
with `PYTHONDONTWRITEBYTECODE=1` and only reads immutable journal/job/manifest
records, the six retained archive/manifest pairs and their saved proof digests,
direct source/destination/non-target/StoreRegistry/journald state, current
capacity, natural monitor and systemd show/config output. Readiness, a second
submit, apply/job/archive execution, the existing `readback_batch`, decompression
or full restore, temporary files, lock acquisition, service/timer action,
SQL/file writes and unlink are absent.

Any source/job/journal/hash/proof drift, source presence, missing/foreign/temp
destination object, active job/lock, unstable or below-floor capacity, stale or
non-normal monitor, 27/12/service, journald/non-target/StoreRegistry drift, or
nonzero Promo/business/non-target effect blocks `done`.

Block 026 preserves the terminal legacy `a01` from block 024 exactly: run
`33069817619`, artifact id `9645283377`, artifact
`root-warm-archive-reconciliation-pr-1075-run-33069817619`, receipt
`sha256:1b99b7a01127f963af31b0cafb2a764e928eb839662af665b1afa4646b9c4847`
and marker `5438726868`. It must validate that artifact and marker as
`blocked/query-only-reconciliation-not-proven`, production mutation count zero,
the same source operation/job, and the exact legacy blocker
`systemd timer/service pair is unhealthy: wb-core-sheet-vitrina-refresh.timer`.
They are never rewritten or redispatched.

The only continuation is derived `a02`, bound to that a01 artifact/marker
digest, the same source operation/job and a new merged `repo_only/done` release.
One exact existing a02 returns `already_terminal` without SSH or publication.
Duplicate/foreign/different evidence and `a03` fail closed. If a02 blocks, the
sequence is exhausted with no queue or retry.

The probe's general 27/12 pair classifier has no Sheet-Vitrina exception. Idle
is exactly enabled loaded timer `active/waiting` with exact next trigger and
`Triggers=<owner>`, plus successful owner `inactive/dead`,
`ExecMainStatus=0`, `MainPID=0`. Coherent natural firing is timer
`active/running` plus owner `activating/start` or `active/running`, empty/success
Result, exact zero ExecMainStatus and positive MainPID. Only a sequential
snapshot mismatch between those allowlisted phases receives up to three exact
paired resamples inside five seconds. Original and every resampled raw field and
classification are retained in the immutable artifact. Failed/unknown/masked/
not-found/disabled units, stale or failed Result, nonzero status, missing idle
next trigger, wrong trigger relation and impossible/ambiguous exhaustion remain
terminal fail-closed.

## Block 027 canonical classifier and generation v2

Legacy a02 is now fixed terminal evidence: run `33073151214`, artifact id
`9646668764`, artifact
`root-warm-archive-reconciliation-pr-1075-run-33073151214`, receipt
`sha256:ce87472b71d1545cb8383ec417b1d83cba1c5f46568beb6249b9e66368d4030a`
and marker `5439297992`. Its state is
`blocked/query-only-reconciliation-not-proven`, production mutation count is
zero and its old sequence is exhausted. It is never a03, retry, replay or
authorization for another operation.

The a02 artifact proves a classifier-code defect rather than a service failure.
All twelve timer rows omitted `MainPID`/`ExecMainStatus`, owner oneshots were
normally `static` and the root-storage owner was `disabled`; the old duplicated
classifier treated these as required/disabled failures. At the same instant
the exact canary pair was coherent natural firing: timer `active/running`, owner
`activating/start`, `MainPID=593451`, `ExecMainStatus=0`. Vitrina WB Finance
429 degradation is a separate excluded condition and is not systemd evidence.

Only a real repository code delta creates reconciliation generation `v2`. It
binds the original source Apply receipt and exact operation
`production-goal-v1-8692b24cb2491927bdadd5dec06a15d8`, job
`d8176c48b41b6d128aa9adacb3aa50f1d464dc318cc9cc8df58d3be637649d2d`,
both terminal a01/a02 run, artifact archive, receipt and marker digests, and one
new merged `repo_only/done` Release SHA. The only admitted attempt is
`v2-a01`, supplied by the workflow as a closed literal rather than a caller
input. Exact replay is `already_terminal` before SSH/comment; `v2-a02`,
`v2-a03`, queue and identity-nonce release are invalid.

The one SSH process first verifies deployed SHA exactly
`7d83c5d0ddf6bf86d6359409ef0f9a7bb4ad4747` and the exact deployed
`apps/root_storage_warm_archive.py` digest. It imports only that deployed
module's query-only `SERVICE_NAMES`, 27-unit snapshot, unit-row and bounded
paired classifier symbols and invokes them directly. Reconciliation owns no
second classifier. Timer rows are evaluated only on properties canonical code
requires; missing timer `MainPID/ExecMainStatus` is valid, and owner
`UnitFileState=static|disabled` follows canonical oneshot state/result/PID
semantics. Realtime and monotonic next-trigger values are preserved as raw
observations without adding a reconciliation-only predicate. Canonical
failed/unknown/masked/missing/nonzero states and impossible pair relations
remain fail-closed; only the canonical bounded transition receives at most
three resamples inside five seconds. Initial raw rows, final 27 units/12 pairs,
all resamples and module/contract identity are retained.

## Receipt and terminal acceptance

Full canonical evidence is uploaded as one immutable artifact first. Only then
may the Actions bot append one distinct compact supersession marker to the
original operation PR, binding the untouched source blocked comment/artifact,
new release/artifact/evidence digests and
`done/reconciled_existing_operation`. An exact existing marker is verified back
through its artifact and returns `already_terminal` without SSH or publication;
duplicate, foreign or different existing evidence fails closed. This block's
production mutation count is structurally and observably zero.

`COMPLETE` requires that immutable `done` artifact and compact marker for the
same existing operation, plus every terminal fact in migration 159: six absent
sources, six retained archives and manifests with current hashes and saved
stream/full-restore/SQLite proof digests, six completed unlink intents and exact
reclaimed bytes, stable capacity above root and Finance floors, fresh normal
natural monitor, healthy 27 units/12 pairs, no active sanitation job or held
lock, preserved journald/non-target/StoreRegistry state, and zero Promo,
business-data, non-target or block-024 production mutation.
All block-024/026/027 reconciliation receipts and markers must independently
record `production_mutation_count=0`.

Required additional check:

- `python3 apps/wbc0008_warm_archive_receipt_reconciliation_probe_smoke.py`
