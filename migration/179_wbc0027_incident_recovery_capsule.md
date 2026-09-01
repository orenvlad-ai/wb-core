# Migration 179 — WBC0027 incident recovery capsule

## Decision and boundary

The general five-phase WBC0027 Production Apply corridor described by
Migration 178 is terminal. It is not retried, repaired or used to qualify this
incident again. This migration installs one incident-only recovery capsule and
one independent default-off workflow. The release and its qualification perform
zero business-data mutations and stop at `HUMAN_REQUIRED`.

The clean iPhone 14 evidence remains a detector only. Capsule scope is derived
fresh from every currently proven incident facility × SKU status identity and
its dependent FBS/TOTAL, capital, WAC, functional-economics and same-date
history surfaces. Unsupported history remains typed missing/inapplicable; the
capsule never writes zero or copies a current value backwards.

## Immutable capsule manifest

`wbc0027_incident_recovery_capsule_manifest/v1` is generated only by two
identical query-only material witnesses on the exact deployed capsule SHA. It
binds:

- target, runtime and release receipt;
- StoreRegistry manifest, operational generation/revision and SQLite schema;
- cutover, forward generation/state and source/forward cursors;
- the exact absent mapping tuple, owner evidence and mapping material CAS;
- every proven facility × SKU row, status sequence and digest;
- predicted lifecycle, reservation, balance, capital, WAC, functional-
  economics and same-date history effects;
- exact history append/supersession rows and explicit remain-missing rows;
- non-target and semantic WB before-images;
- writer lock, RootStorage admission, private backup/recovery evidence and the
  exact per-table insert/update/delete counts expected from one transaction.

Incident values live only in this generated manifest. The executor contains no
incident facility, SKU, order/status count or business-date constants.

`wbc0027_incident_recovery_capsule_qualification/v1` exact-binds that manifest,
both witness digests, private immutable plan/backup evidence, release binding,
expected write digest and invariant digests. It records
`production_mutation_submit_count=0` and `github_apply_marker_published=false`.
Qualification does not create a general phase marker and does not authorize
Apply.

The trusted workflow resolves the explicit canonical hosted target before any
remote command and materializes a private SSH config with exact `HostName`,
`User=root`, identity and known-host bindings. It never depends on a hosted
runner's local alias or probes a legacy/default target. The mutation-incapable
simulation keeps scratch foreign-key enforcement disabled only while the full
forward/history dependency projection is populated; after projection commit it
enables enforcement and requires a complete zero-row `foreign_key_check`.
The production source connection remains `mode=ro` and query-only throughout.

## Single later Apply contract

Apply is intentionally not executed by this migration. A later invocation can
cross the mutation boundary only after one OWNER/MEMBER comment whose body is
byte-for-byte equal to the body emitted by the qualified manifest:

```text
/wb-core apply-incident-capsule-v1 task WBC0027 target wb_core_eu_hosted_runtime_active pr <PR> release <RELEASE_OPERATION> deployed <SHA> manifest <MANIFEST_SHA256> qualification <QUALIFICATION_SHA256> operation <OPERATION_ID> mapping-inserts 1 submits 1
```

The trusted workflow binds that comment to the same PR, deployed release,
manifest, qualification artifact and durable operation. It issues one Apply
command only. The executor obtains the shared warehouse writer lock, rechecks
the material CAS under `BEGIN IMMEDIATE`, creates exactly one mapping row and
executes only the manifest-enumerated lifecycle, balance, capital and history
writes inside that same SQLite transaction. A SQLite authorizer and temporary
audit triggers enforce the exact table and per-operation row-count budget.
There is no ad-hoc SQL surface and no call into the general five-phase runner.

Ambiguous transport permits only same-operation query-only readback; it never
permits a repeat submit. The workflow uploads the terminal receipt artifact
before its single terminal comment.

## Post-Apply acceptance and rollback

The query-only readback must prove the exact mapping and operation identity,
complete lifecycle/status coverage, FBS facility × SKU, facility/global TOTAL,
combined TOTAL, capital, WAC, functional economics, same-date history and the
explicit remain-missing list. It also proves semantic WB and non-target
invariants against the manifest.

Before mutation, Apply records an immutable private before-image and recovery
evidence under the admitted production-goal directory. Rollback is not an
automatic retry or inverse Apply: it is a separately authorized restore from
that exact before-image, followed by the same query-only acceptance surface.
