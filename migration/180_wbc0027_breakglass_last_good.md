# WBC0027 guarded public last-good fallback

## Boundary

This contour is a break-glass read-side fallback for a proven incomplete FBS
lifecycle.  It does not reconstruct or certify the warehouse ledger and does
not write WB, FBO, FF balances, lifecycle, ready snapshots, capital or
economics source rows.

One immutable operation may persist exact public cell values copied from a
verified previously published inventory capture and an exact sealed retained
economics JSON. The economics adapter opens that JSON directly read-only; it
does not recreate a SQLite carrier. It binds the whole-file digest, exact
`functional_economics.patches[index].before_plan_json` raw UTF-8 digest,
bundle/as-of/snapshot identity digest and selected date-column digest before
plan and again before either mutation. Web Vitrina uses those cells only when the ordinary public cell is
blank and only on or after the source business date.  A non-empty ordinary cell
always wins.  Every fallback presentation is explicitly
`last_good_provisional`; it is never labelled exact.

The production profile is closed at 303 immutable cells: 68 facility FBS, 34
combined totals, 34 WAC and 167 dependent economics. Its two public date
columns have 606 eligible presentations. The three source-empty identities
`SKU:497413772|proxy_margin_3_pct`,
`SKU:497413772|proxy_margin_4_pct` and
`SKU:497413772|proxy_margin_per_unit_rub` are excluded from the target and stay
blank. Any family, source-empty, inventory-total or presentation-count drift
fails before submit.

The apply boundary is one operation, one submit and one SQLite transaction
under the existing warehouse writer lock.  The manifest binds the exact source
identities/digests, target cell digest, enabled public SKU scope, target
prestate and non-target digest.  Before-image evidence is durable before the
transaction, and the terminal receipt is written only after query-only
readback.  A second submit with the same identity fails closed.  Ambiguous
transport permits only query-only readback.

Rollback uses the same repo-owned CLI and is an append-only revocation of the
exact operation. Its immutable plan is written before the revocation marker
and binds the manifest, original target prestate, non-target digest, apply
before-image digest and applied query-only readback digest. A second query-only
readback proves that the overlay is inactive while all operation/cell/revoke
audit rows remain. No source or target row is deleted or updated. Ordinary healthy publication supersedes fallback
cell-by-cell automatically because the overlay cannot replace non-empty
values.

Before a production Apply, the full production-shaped scratch rehearsal is
`plan -> one-submit apply -> DB/read-model overlay readback -> guarded revoke ->
second readback`. It proves deterministic 303/606 scope, fresh fill-blank-only
effect, preservation of ordinary values, exact three exclusions, one
transaction per mutation, zero WB/FBO/history/ready/source/capital/non-target
writes, source-drift rejection, same-operation readback after transport
ambiguity and no blind retry. Production maintenance uses the existing
deploy-persistent `business-data-maintenance` barrier/inventory and detached
restore watchdog/continuity contract; this runner does not invent a second
timer or barrier mechanism.

Full exact FBS lifecycle reconstruction and historical certification remain a
separate recovery scope.
