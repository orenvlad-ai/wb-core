# WBC0027 stdlib source binding and consolidated predecessors

## Scope

This live-runtime correction changes only the default-off query-only
WBC0027 receipt reconciliation validator and its immutable receipt lineage. It
does not submit product/economics data, replay either phase, create a recovery
row or private manifest, repair the twelve 26 August evidence gaps, or dispatch
finalization.

## Diagnosed dependency leak

Run `33370422066` used deployed SHA
`4e068cada7dbf41aa70486a2694f9ba78c16470b`. Its fixed remote probe returned a
qualified query-only result with an exact empty legacy `after_digest`, but the
trusted validator dynamically imported the full
`apps.wbc0027_capital_recovery` module. That import transitively required
`openpyxl`, which is absent from the default-off runner environment. Import
failure was collapsed to `source_recovery_binding=false`.

The immutable blocked evidence is artifact `9749833454`, canonical receipt
`sha256:518fc39f3c7a17e84a247075f540ef393aed0110b827d276d322075de1000951`
and evidence digest
`sha256:87017b579f91e8c49de9111a38098cfef5e02f401467ba1726fb15ed736f9e3b`.
It records query-only true, database-written false and zero production mutation
or product/economics replay.

## Correction

`apps/wbc0027_capital_recovery_source_binding.py` is the shared stdlib-only
contract. It owns the exact immutable legacy source allowlist, the complete
legacy transaction proof and the closed runtime-source path set. Both the
trusted Apply Runner and full recovery runtime import this same contract. The
Apply Runner no longer imports or mutates the full recovery module at receipt
validation time.

An empty `after_digest` now passes only when all of the following are exact:

- immutable source PR/run/artifact/receipt/passport/deployed SHA/manifest/phase
  and StoreRegistry generation;
- raw 221-row non-target aggregate and three exact target identities,
  before/planned-after hashes, removed-target equality and `3/472` write set;
- undo verification, mutation/quarantine transition ordering and deployed
  source-code order through COMMIT then retain/quarantine.

Identity, digest, cardinality, ordering or unexpected proof-field drift fails
closed. A non-empty value is accepted only when it equals the exact source
target-after digest.

## Terminal lineage

Generation v3 reconciliation receipts and compact summaries contain one
`wbc0027_reconciliation_terminal_predecessors/v1` object. It binds both:

1. failed artifact-less run `33363863580`, including its exact job/log/source
   inputs and zero-mutation preflight;
2. blocked artifact-bearing run `33370422066`, job `99420021737`, artifact
   `9749833454`, archive/receipt/evidence digests, sole failed predicate
   `source_recovery_binding`, PR 1137 release binding and zero-mutation truth.

The workflow input count and `prior_reconciliation_run_id=33363863580` contract
remain unchanged; the second predecessor is derived from immutable GitHub
evidence. Neither predecessor is retried or rewritten.

## Verification and next boundary

Production-shaped runner regressions validate the exact empty-digest result and
negative identity/digest/proof drift. A `python3 -S` subprocess proves validation
without `openpyxl` and without importing the full recovery runtime. The release
ends at exact `live_runtime/done`. One later separate default-off
`wbc0027-receipt-reconciliation` dispatch may use this deployed release; it is
query-only/finalize-only and must retain production mutation and both replay
counts at zero.
