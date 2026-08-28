# Migration 162: manifest-bound dense zero and historical material recovery

## Release boundary

This release installs two generic, owner-gated recovery mechanisms. It does not
run either mechanism, mutate production business data, change a service/timer,
or change Web Vitrina presentation and formulas. Both adapters require the exact
active hosted-runtime target and explicit StoreRegistry generation. A plan,
owner approval reference, actor and exact plan fingerprint are operation-time
inputs, never deploy defaults.

## Dense FBS forward-zero repair v3

The strict v3 manifest binds only the current canonical identity: one exact
facility, seller warehouse and office, the complete active stock-managed roster,
the exact existing facility/FBS identities and one owner-approved missing target
list. For WBC0013 that identity is Orenburg and `71 = 21 + 50`, where the 50 are
the accepted original-12 plus WB-Content-38 identities. Historical capture/date
counts, presentation lineage, an exact historical anchor and semantic equality
between old presentation revisions are not admission or CAS inputs. They remain
immutable audit evidence and are never inferred to mean zero.

Planner qualification is query-only and fails closed when a target already has
a current canonical balance or current Orenburg material state: an active
reservation/fulfilled lifecycle, open reconciliation, unresolved exact-warehouse
identity or a complete/sorted handoff not consumed by the Orenburg lifecycle.
Closed documents/history, missing or NULL presentation rows and Moscow activity
are non-material for this forward cutover. Exact mapping/allocation, roster,
target absence, current material evidence and current target/non-target row
fingerprints remain CAS material. Unknown, missing, extra, duplicate or
overlapping current identities fail closed.

Apply repeats qualification before and under the shared warehouse writer lock,
persists the existing dense `repair` intent, and emits exactly one deterministic
`pool_inventory` request. Its only possible physical effect is one insert per
still-missing target with `quantity=0`, `capital_rub=0` and `wac_rub=NULL`.
Existing/non-target rows are not updated. Exact canonical request readback
reconciles restart or lost transport; the adapter never blindly submits a
second request.
The query-only terminal receipt proves all 71 roster rows, exactly 50 new
explicit zeros, one document with 50 absolute-target lines, zero movement lines,
the forward `T0`, unchanged non-target rows and zero history writes.

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
WBC0013 qualification does not enumerate or classify a broad mismatch set. It
selects only business date `2026-08-26`, nmId `428853741`, accepted version
`whfv_cb0657c384d5adebae01e585` and causal event
`ffbf_87cea959c9d600da99caa1ab68ef`. It proves the one ready-side target, blank
own cost and exact six missing TOTAL dependencies, then reconstructs the exact
three-location pre-debit set and debits only Orenburg. Moscow and the third
location remain byte-semantically present. Current pool rows, including all 50
terminal A zeros, are preservation/CAS evidence only and are never candidate
operands. The candidate publication time is derived from the accepted event, so
qualification witnesses built at different wall-clock times are materially
identical.

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
only the exact `WBC0013 / dense-fbs-historical-recovery / 71 = 21 + 50 /
50 zero inserts / accepted date + nmId + version + event / one historical repair`
passport. It writes private 0600 JIT
plans only through the registered `production_apply_evidence` backup
destination inside the exact mode-0700 operation directory. Each plan is
bounded, file-and-directory-fsynced and atomically published without overwrite;
its path, size, modes and full storage-admission result are retained in the
qualification receipt until terminal reconciliation. A storage failure keeps
the exact `RootStoragePolicyError` type and reason in the immutable terminal
receipt and performs no submit. The runner requires two consecutive identical material qualifications with at most
three regenerations, submits A once, performs query-only A reconciliation, then
builds a fresh B plan, submits B once and performs query-only B reconciliation.
Every ambiguous response goes directly to same-operation readback and never to
a blind submit retry. The runner invokes the exact deployed adapter path with
an explicit `PYTHONPATH`, private mode-0700 evidence directory and exact target/
generation arguments. Failure receipts retain bounded typed `phase`, `stage`,
`code`, `message`, `predicate`, expected/observed cardinality, candidate digest
and details digest; stderr is only transport evidence.
Barrier checks are read-only before and under the shared lock; no ordinary
service or timer is stopped or changed.
