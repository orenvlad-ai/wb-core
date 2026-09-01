# Migration 178 — general FBS manifest mapping and recovery

## Decision

WBC0027 replaces the incident-shaped FBS recovery code with one reusable,
manifest-driven protocol.  Product names, SKU ids, facilities, order/status
counts, groups and business dates are data in an incident passport or a fresh
reviewed manifest; they are not application constants.

The release installs capability only.  It performs zero mapping inserts, zero
lifecycle/history mutations and zero WB writes.  Mapping and recovery keep
separate owner-authorized one-submit boundaries.

## Versioned contracts

`fbs_identity_mapping_manifest/v2` binds one arbitrary exact tuple
`source_nm_id/source_chrt_id/source_barcode/source_sku/target_nm_id` and its
canonical digest to:

- operation id and exact target/runtime;
- StoreRegistry manifest, operational generation, operational schema revision
  and SQLite schema version as four distinct fields;
- cutover and immutable forward-generation identities;
- stable external, owner, warehouse and facility-admission evidence;
- the exact `owner=1, active mappings=0, all mappings=0, insert=1`
  expectation, proposed mapping id/digest and material CAS.

Orders, statuses, groups and dates are forbidden anywhere in this mapping
contract.  Material CAS is stable-only: tuple, mapping absence, owner,
storage, cutover and external identity. It deliberately excludes order/status/
group/date/WAC/cardinality observations. Apply rechecks CAS under the shared
warehouse writer lock, writes immutable `O_EXCL` fsynced before-image, backup
and operation/auth/storage journal, authorizes at most one canonical mapping
INSERT and then uses exact same-operation query-only readback without a retry.

`fbs_lifecycle_impact_manifest/v2` is generated only from a terminal mapping
readback digest and a fresh storage/cutover/generation/cursor snapshot.  It
contains the complete unresolved classification, every affected
facility × SKU plus facility/SKU/global totals, earliest evidence dates and
sequence digests, dependent FBS/capital/WAC/economics/history surfaces,
same-date history evidence, non-target/WB baselines and its own digest.

`fbs_lifecycle_recovery_manifest/v2` exact-binds the independently generated
and admitted impact artifact digest and complete
current target sequence/row digests.  It predicts lifecycle, balance, capital
and same-date history append/supersession effects plus physical/reserved/
available facility × SKU, facility totals, global SKU/total, capital, WAC and
functional economics after-images. It carries writer-lock, RootStorage
admission, immutable before-image/backup/journal, CAS, one-submit and exact
same-operation query-only readback guards.
It cannot write mapping or WB state.

History evidence is explicitly split into `recoverable_exact` and
`remain_missing_no_same_date_evidence`.  Unsupported cells remain missing and
do not block a recovery whose other target evidence is exact.  The WBC0027
incident rehearsal expects the four already proven unsupported cells to stay
in that second class.

## Incident passport

The exact current incident input is
`release/production-mutations/wbc0027_fbs_lifecycle_incident.json` with contract
`fbs_lifecycle_incident_passport/v1`.  It carries the diagnosis snapshot,
runtime/storage/cutover bindings, one canonical tuple and evidence digests.
Changing the incident requires changing that passport and rerunning the same
general parser, planner, smoke and full production query-only/no-submit
rehearsal; it does not require SKU-specific code.

## Staged production acceptance

Every dispatch first derives one immutable root goal operation from the exact
repository, source PR, OWNER/MEMBER passport comment and parsed goal. It then
derives a different `production-goal-v2-*` phase operation under
`wb-core.fbs-phase-binding/v1`. The phase binding exact-binds the source and
correction release binding digests, incident-passport digest, passport body,
phase and the complete predecessor marker/artifact descriptor. The closed order
is:

`mapping_qualification -> mapping_apply -> impact_generation -> recovery_qualification -> recovery_apply`.

The first phase forbids a predecessor. Every later phase requires the exact
predecessor marker comment, downloads and hashes its canonical receipt artifact,
validates marker, artifact archive, receipt, phase operation, root goal, releases
and expected terminal state, and derives its own phase operation from those
bytes. A predecessor is never treated as the current phase terminal. Missing,
duplicate, foreign, cross-mode, skipped, reordered or drifted evidence blocks
before SSH or submit.

1. Validate the source `production_mutation/awaiting_apply` and correction
   `live_runtime/done` releases independently through exact PR/base/head/Gate/
   plan/Release/comment/downloaded-artifact/file/manifest bindings. Release the
   generic code after a full candidate rehearsal passes; mutation count remains
   zero. The normal correction base is the source merge. A moved main is
   admissible only through a bounded linear chain of exact trusted
   `repo_only/done` receipts whose downloaded artifacts verify and whose exact
   paths are restricted to `docs/**` and executable `*_smoke.py`; all other
   paths fail closed. The sole migration exception is exact PR 1145 runtime
   `068446766a144348578cd8460d8f22f267460681` / deployed
   `5cdd45b5a499e630bed5277d46bd7047ac6624e2`, operation
   `release-v2-76858aebf78533adc107428d99a7aa33`, artifact `9774197000` and
   changed-file digest
   `sha256:2ca8871159a4ca9d79f3c0f9bb948e95d56b75634a202d6ca263cf4b04ba741b`.
   It is accepted only as an exact `superseded_fbs_runtime` ancestry record,
   never as current correction, phase terminal or authorization; no other
   intervening runtime release is allowed.
2. `fbs-mapping-qualification` terminates `qualified_no_submit`. Only its exact
   terminal artifact opens `fbs-mapping-apply`; that phase owns the accepted
   single mapping insert submit and terminal query-only mapping readback. Its
   fresh two-witness candidate must match the predecessor fingerprint,
   material-CAS and tuple digests before the submit boundary.
3. `fbs-impact-generation` consumes only terminal `mapping_apply` and its exact
   readback digest, then publishes one fresh immutable
   `fbs_lifecycle_impact_manifest/v2` with submit count zero.
4. A separate `fbs-lifecycle-recovery-v2` passport exact-binds the mapping root
   goal operation, terminal mapping readback, impact and recovery digests.
   `fbs-recovery-qualification` consumes the terminal impact artifact and
   terminates `qualified_no_submit`; only its exact terminal artifact opens
   `fbs-recovery-apply`, whose independent budget is one recovery submit.
   Recovery Apply freshly requalifies and requires exact equality with the
   predecessor recovery fingerprint, impact, storage/boundary, scope and
   history binding before its submit boundary.

Mapping/recovery qualification and impact generation stop before submit with
`qualified_no_submit`; their combined submit count is zero. Mapping Apply and
recovery Apply have independent one-submit budgets. Once either Apply command is
issued, ambiguity remains inside that same phase and permits only its query-only
readback; it cannot derive or submit a new phase. Every attempt uses a distinct
private admitted plan path. Workflow publication uploads the canonical receipt
first, then downloads and hashes it, then publishes the phase-scoped closed
marker. Exact replay validates the same phase marker and artifact before
returning `already_terminal` with zero SSH, comment, dispatch and submit counts.

No deployment receipt, rehearsal artifact or prepared passport text is itself
authorization for either Apply.
