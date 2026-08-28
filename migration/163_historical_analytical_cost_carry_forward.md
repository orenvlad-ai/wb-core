# Migration 163: historical analytical own-cost carry-forward

## Release boundary

This release adds one inert-by-default presentation correction adapter and one
task-scoped Production Apply profile. Deploy does not execute a correction,
change a service/timer, fetch WB data or modify warehouse truth. The operation
requires an exact date/SKU owner passport, active hosted-runtime target, exact
deployed SHA, explicit StoreRegistry generation and private JIT evidence.

## Source and event admission

The only admissible source is the latest earlier positive own-unit-cost cell of
the same SKU from a ready Vitrina snapshot whose inventory-cost publication is
tagged with `our_inventory_wac_wb_ff_v1`. The plan binds its date, value,
snapshot identity and digest. A 31-day maximum lookback prevents an unbounded
historical substitution.

Query-only qualification requires exact source and cutoff functional-version
boundaries. Every positive WB/FF stage row must preserve its anchor WAC and its
quantity/capital arithmetic. An intervening receipt, zero-quantity capital
adjustment, WAC change, unknown lifecycle event or non-proportional debit blocks
before submit. A handoff debit is admissible only when its frozen and implied
WAC equal the source FF WAC. Current/future cost, another SKU and warehouse
reconstruction are never alternatives.

## Presentation-only publication

The adapter reuses the server-side functional-economics materializer to compute
the candidate but projects back only one exact dependency closure: target SKU
own cost, Proxy 3 profit/margin, Proxy 4 profit/margin/unit margin, plus the six
canonical TOTAL dependencies. It adds an immutable provenance marker declaring
`analytical_only=true` and `warehouse_truth_reconstructed=false`. Every other
cell and metadata field in the target plan and every other ready snapshot are
digest-bound non-target evidence.

Apply holds the shared warehouse writer lock, rechecks the business-data barrier
and the complete material plan, creates a coherent capacity-admitted backup,
then performs one `BEGIN IMMEDIATE`. Immediate CAS binds both the exact target
plan and all other ready snapshots. One append-only accepted analytical version
and one exact ready-snapshot update are the complete write set. Warehouse
versions/balances, movements, lifecycle, facilities/pools, reservations, orders
and raw/history sources are read-only evidence.

## Exact production profile

The default-off `historical-analytical-cost-carry-forward` profile accepts only
WBC0013 date `2026-08-26`, nmId `428853741`, one accepted Vitrina version and one
updated ready snapshot. Two consecutive identical query-only material witnesses
qualify at most one submit. After the submit, only same-operation query-only
readback is allowed; a terminal or ambiguous identity is never retried. The
receipt preserves prior source/date/digest, before/after metrics, manifest,
backup, exact target/generation, submit count and non-target digests.
