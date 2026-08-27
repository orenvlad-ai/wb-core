# Migration 162: manifest-bound dense zero and historical material recovery

## Release boundary

This release installs two generic, owner-gated recovery mechanisms. It does not
run either mechanism, mutate production business data, change a service/timer,
or change Web Vitrina presentation and formulas. Both adapters require the exact
active hosted-runtime target and explicit StoreRegistry generation. A plan,
owner approval reference, actor and exact plan fingerprint are operation-time
inputs, never deploy defaults.

## Dense FBS zero repair

The dense repair manifest binds the complete stock-managed SKU roster, exact
existing facility/FBS identities and exact missing target identities. Planner
qualification is query-only. Mapped identity evidence is scoped to the exact
seller warehouse; legacy facility-less reservation history blocks only when its
immutable net quantity is non-zero. Mapping-extension allocations, target
effects, historical zero evidence, target absence and bounded non-target
fingerprints are all CAS material.

Apply repeats qualification before and under the shared warehouse writer lock,
persists the existing dense `repair` intent, and emits exactly one deterministic
`pool_inventory` request. Its only possible physical effect is one insert per
still-missing target with `quantity=0`, `capital_rub=0` and `wac_rub=NULL`.
Existing/non-target rows are not updated. Exact canonical request readback
reconciles restart or lost transport; the adapter never blindly submits a
second request.

## Shared WAC contract

Ledger write validation, functional material comparison and recovery candidates
use one finite Decimal ratio with context precision 38 and canonical
non-exponent text. Equality is exact. There is no tolerance, scale heuristic or
rewrite of a stored WAC.

## Historical bounded recovery

The historical manifest binds one business date, one accepted immutable good
functional version, one facility/FBS/SKU and one immutable `handoff_debit`,
including the accepted version fingerprint/timestamps, exact accepted
quantity/coverage/capital, event values/time, source/status/evidence/full-row
digests and the expected current active version. No binding is optional.
The candidate starts from the accepted version and proves the pre-debit location
plus event arithmetic. Current pool rows are preservation/CAS evidence only and
are never copied into the historical candidate.

Only the target FF balance/coverage/provenance is replaced; all other functional
rows and auxiliary version material are copied from the accepted version. Ready
materialization first proves one positive-order target, its blank own cost and
the exact six missing own-cost, Proxy 3 and Proxy 4 TOTAL dependencies; it then
restores only that target-and-TOTAL closure and proves the non-target digest is
unchanged. Publication creates a new immutable good
historical version and same-date business projection, but does not change the
current functional active pointer, WB sync pointer or current pool rows. The
existing durable intent, shared lock, bounded retry and query-only exact
readback handle restart and ambiguous transport without a full database copy,
full-day reload or blind retry.
