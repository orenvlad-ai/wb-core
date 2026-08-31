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
contract.  Apply rechecks CAS under the shared warehouse writer lock, writes a
private before-image, authorizes at most one canonical mapping INSERT and then
uses query-only readback without a retry.

`fbs_lifecycle_impact_manifest/v2` is generated only from a terminal mapping
readback digest and a fresh storage/cutover/generation/cursor snapshot.  It
contains the complete unresolved classification, every affected
facility × SKU plus facility/SKU/global totals, earliest evidence dates and
sequence digests, dependent FBS/capital/WAC/economics/history surfaces,
same-date history evidence, non-target/WB baselines and its own digest.

`fbs_lifecycle_recovery_manifest/v2` exact-binds the impact digest and complete
current target sequence/row digests.  It predicts lifecycle, balance, capital
and same-date history append/supersession effects and carries writer-lock,
private before-image, backup, CAS, one-submit and query-only readback guards.
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

1. Release the generic code after a full candidate rehearsal passes; mutation
   count remains zero.
2. A separate `fbs-identity-mapping-v2` passport may authorize one mapping
   insert.  Its terminal query-only readback digest becomes the next input.
3. Generate and review one fresh `fbs_lifecycle_impact_manifest/v2`.
4. A separate `fbs-lifecycle-recovery-v2` passport exact-binds the mapping
   readback, impact and recovery digests and may authorize one recovery submit.

No deployment receipt, rehearsal artifact or prepared passport text is itself
authorization for either Apply.
