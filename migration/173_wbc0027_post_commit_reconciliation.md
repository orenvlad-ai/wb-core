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
new live-runtime release.

Its fixed remote command exposes only `finalize-only`. That command opens the
operational database query-only and proves:

- three exact current target after-images and their aggregate digest;
- three exact T1 undo before/after rows and the mutation-running to quarantined
  transition/reason;
- legacy raw witness equality and canonical semantic
  before=after=current over 224 rows;
- retained exact product predecessor, product 1,152 rows / 24,192 cells /
  mismatch 0, economics missing 12/0, protected cost `117.537167`, and hard
  non-target exactness.

It performs zero database writes and zero product/economics replay. The workflow
uploads one canonical receipt before posting one compact supersession marker.
Exact repeat validates that artifact and returns `already_terminal` before SSH;
missing, foreign, duplicate or drifted evidence fails closed.

## Verification

`apps/wbc0027_capital_recovery_lifecycle_smoke.py` reproduces the source
after-COMMIT false quarantine, durable write truth, genuine concurrent
non-target drift rejection and byte-unchanged idempotent finalization.
`apps/wbc0027_capital_recovery_runner_smoke.py` verifies the mutation-incapable
command, provenance/receipt contract, artifact-first workflow and negative
missing/foreign evidence.
