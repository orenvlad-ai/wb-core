# Migration 162: manifest-bound dense zero and historical material recovery

## Release boundary

This release installs two generic, owner-gated recovery mechanisms. It does not
run either mechanism, mutate production business data, change a service/timer,
or change Web Vitrina presentation and formulas. Both adapters require the exact
active hosted-runtime target and explicit StoreRegistry generation. A plan,
owner approval reference, actor and exact plan fingerprint are operation-time
inputs, never deploy defaults.

## Dense FBS zero repair v2

The strict v2 manifest has exactly two disjoint target partitions:
`historical_exact_zero` and `default_applicable_absent_history`. Their union,
plus the exact existing facility/FBS identities, must equal the complete active,
non-hidden stock-managed roster. Unknown, missing, extra, duplicate and
overlapping identities fail closed; the operation id is a deterministic
WBC0013 namespace digest. Planner qualification is query-only. Mapped identity evidence is scoped to the exact
seller warehouse; legacy facility-less reservation history blocks only when its
immutable net quantity is non-zero. Mapping-extension allocations, target
effects, historical zero evidence, canonical WB Content lifecycle/source
identity, target absence and bounded non-target fingerprints are all CAS
material. The absent-history partition must have no accepted target-facility
history; it does not invent a date, global Moscow inference or reservation
shortcut.

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
non-exponent text. New candidate values use canonical 38-digit text. A legacy
historical positive finite WAC retains its original longer text only while the
exact accepted row and provenance digests are unchanged; the source row is not
normalized or rewritten. Null, negative, non-finite or digest-drifted values
fail closed.

## Historical bounded recovery

The historical manifest binds one business date, one accepted immutable good
functional version, one facility/FBS/SKU and one immutable `handoff_debit`,
including separately typed version-plan, full-version-row, accepted-target-row,
accepted-provenance and event source/status/evidence/full-row digests, exact
accepted quantity/coverage/capital and expected current active, sync and pool
identities. Aliases are not accepted. Qualification uses one explicitly bound
StoreRegistry generation and one true query-only dependency connection; it runs
no schema ensure, DDL or hidden storage re-resolution. No binding is optional.
The candidate starts from the accepted version and proves the full pre-debit
facility/pool location set plus event arithmetic. It debits only the target
location while preserving Moscow and every other location. Current pool rows are preservation/CAS evidence only and
are never copied into the historical candidate.

Only the target FF balance/coverage/provenance is replaced; all other functional
rows and auxiliary version material are copied from the accepted version. Ready
materialization first proves one positive-order target, its blank own cost and
the exact six missing own-cost, Proxy 3 and Proxy 4 TOTAL dependencies; it then
restores only that target-and-TOTAL closure and proves the non-target digest is
unchanged. Publication creates a new immutable good
historical version and same-date business projection, but does not change the
current functional active pointer, WB sync pointer or current pool rows. The
Durable intent is created only under the shared lock and after under-lock CAS.
The existing intent, bounded retry and query-only exact
readback handle restart and ambiguous transport without a full database copy,
full-day reload or blind retry.

## Exact WBC0013 A to B production profile

`apps/wbc0013_fbs_recovery.py` is an inert-by-default, generic shape-discovery
adapter; deploy does not invoke it. The canonical Production Apply Runner accepts
only the exact `WBC0013 / dense-fbs-historical-recovery / 71 = 21 + (12 + 38) /
50 zero inserts / one historical repair` passport. It writes private 0600 JIT
plans, requires two consecutive identical material qualifications with at most
three regenerations, submits A once, performs query-only A reconciliation, then
builds a fresh B plan, submits B once and performs query-only B reconciliation.
Every ambiguous response goes directly to same-operation readback and never to
a blind submit retry. Barrier checks are read-only before and under the shared
lock; no ordinary service or timer is stopped or changed.
