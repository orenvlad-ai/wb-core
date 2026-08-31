# WBC0027 post-COMMIT reconciliation

## Scope

This correction closes a deterministic lifecycle-classification defect in the
already accepted WBC0027 economics operation. It does not add product or
economics material, repair the twelve evidence gaps on 26 August, alter Finance
or Proxy non-targets, or authorize another production submit.

## Cause and prevention

The source plan stored a raw digest over 221 unpatched ready rows. After the
three target rows committed, retain compared it with a semantic digest over all
224 rows with the three target slices normalized away. Both represented the
same non-target state but were different contracts, so the operation was
quarantined after COMMIT.

`wbc0027_economics_semantic_non_target_digest/v1` is now the only active
non-target contract. It records scope version, all/target row counts and
identity, semantic-payload and row component digests. Planning, consecutive
witnesses, writer-lock rebase, T1 pre-submit/post-submit, retain and readback
all use this builder. Target before-images remain exact CAS. Ordinary semantic
rebase is permitted only before submit; a genuine non-target mutation fails
closed.

The recovery registry records exact after/non-target digests and
`committed_pending_reconciliation` inside the business transaction immediately
before COMMIT. A later exception therefore reports
`applied_pending_reconciliation`, submit 1 and database-written true.

## Existing-operation finalization

The source economics operation
`recovery_ae66a56f72d90b469b75d8adb893c51f` remains quarantined and immutable.
The default-off `wbc0027-receipt-reconciliation` workflow exact-binds source PR
1129, Apply run 33345644125, artifact 9741910399, receipt and marker, original
OWNER passport, deployed SHA/generation, private manifest/fingerprint and the
exact live-runtime reconciliation release. The two reconciliation inputs remain
bound to that deployed PR/SHA and its `live_runtime/done` receipt.

The trusted workflow checkout is a separate, derived bridge. It must be an exact
first-attempt `workflow_dispatch` checkout of `main`, derived from one merged
same-repository PR with a successful exact PR Gate and trusted Release receipt.
It is either the same live-runtime SHA or a descendant with an exact
`repo_only/done` receipt. In the latter case every Git blob in the closed
`wbc0027_reconciliation_runtime_source_binding/v1` set must remain byte-identical
to the deployed SHA. The set owns the Apply Runner, WBC0027 finalizer, warehouse
recovery/storage/lock boundary and imported release receipt validators. A changed
blob, divergent ancestry, missing or ambiguous PR/Gate/receipt, or a non-main
checkout fails closed. Workflow-only/test/docs changes may bridge repo-only; a
runtime-source change requires a new live-runtime release. The reconciliation
receipt records deployed release and workflow bridge separately.

Its fixed remote command exposes only `finalize-only`. That command opens the
operational database query-only and proves:

- three exact current target after-images and their aggregate digest;
- three exact T1 undo before/after rows and the mutation-running to quarantined
  transition/reason;
- source-locked legacy raw 221-row aggregate plus three target-removed
  before/planned-after equalities, without inventing unavailable historical
  semantic component digests;
- retained exact product predecessor, product 1,152 rows / 24,192 cells /
  mismatch 0, economics missing 12/0, protected cost `117.537167`, and hard
  non-target exactness.

It performs zero database writes and zero product/economics replay. The workflow
uploads one canonical receipt before posting one compact supersession marker.
Exact repeat validates that artifact and returns `already_terminal` before SSH;
missing, foreign, duplicate or drifted evidence fails closed.

The exception contract is
`wbc0027_source_economics_transaction_legacy_adapter/v1`. It is admitted only
for the exact PR-1129 source bindings and the private manifest whose real shape
contains raw `non_target_digest`, three patches and three semantic patches, but
contains neither `semantic_non_target` nor `semantic_non_target_contract`.
It binds the exact 3/472 write set, undo artifact and source code order
CAS → after-readback → semantic equality → COMMIT → retain/quarantine. The
canonical versioned semantic builder and every future Apply guard stay strict.
The deployed finalize-only result is explicitly `qualified`; an identical
query-only repeat is byte-stable and reports `already_qualifiable`.

## Verification

`apps/wbc0027_capital_recovery_lifecycle_smoke.py` reproduces the source
after-COMMIT false quarantine, durable write truth, genuine concurrent
non-target drift rejection and byte-unchanged idempotent finalization.
`apps/wbc0027_capital_recovery_runner_smoke.py` verifies the mutation-incapable
command, provenance/receipt contract, artifact-first workflow, the exact PR 1133
live-runtime plus PR 1134 repo-only bridge, direct-equality binding and negative
source-drift/ancestry/receipt evidence.
