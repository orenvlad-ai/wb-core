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

1. Validate the source `production_mutation/awaiting_apply` and correction
   `live_runtime/done` releases independently through exact PR/base/head/Gate/
   plan/Release/comment/downloaded-artifact/file/manifest bindings. Release the
   generic code after a full candidate rehearsal passes; mutation count remains
   zero. The normal correction base is the source merge. A moved main is
   admissible only through a bounded linear chain of exact trusted
   `repo_only/done` receipts whose downloaded artifacts verify and whose exact
   paths are restricted to `docs/**` and executable `*_smoke.py`; all other
   paths fail closed.
2. A separate `fbs-identity-mapping-v2` passport may authorize one mapping
   insert. It must be the unique equivalent OWNER/MEMBER passport. Its terminal
   query-only exact-operation readback digest becomes the next input.
3. Generate and review one fresh immutable
   `fbs_lifecycle_impact_manifest/v2` outside the recovery command.
4. A separate `fbs-lifecycle-recovery-v2` passport exact-binds the mapping
   operation/readback, impact and recovery digests and may authorize one
   recovery submit.

Mapping and recovery qualification modes stop at the native shared-lock
boundary with `qualified_no_submit`; they never invoke the remote Apply
command. Every attempt uses a distinct private admitted plan path. Workflow
publication uploads the canonical receipt first, then downloads and hashes it,
then publishes a closed marker. Exact replay validates both marker and artifact
before returning `already_terminal` with no SSH/comment/dispatch.

No deployment receipt, rehearsal artifact or prepared passport text is itself
authorization for either Apply.
