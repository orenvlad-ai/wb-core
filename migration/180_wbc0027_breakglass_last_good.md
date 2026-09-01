# WBC0027 guarded public last-good fallback

## Boundary

This contour is a break-glass read-side fallback for a proven incomplete FBS
lifecycle.  It does not reconstruct or certify the warehouse ledger and does
not write WB, FBO, FF balances, lifecycle, ready snapshots, capital or
economics source rows.

One immutable operation may persist exact public cell values copied from a
verified previously published inventory capture and a verified retained domain
checkpoint.  Web Vitrina uses those cells only when the ordinary public cell is
blank and only on or after the source business date.  A non-empty ordinary cell
always wins.  Every fallback presentation is explicitly
`last_good_provisional`; it is never labelled exact.

The apply boundary is one operation, one submit and one SQLite transaction
under the existing warehouse writer lock.  The manifest binds the exact source
identities/digests, target cell digest, enabled public SKU scope, target
prestate and non-target digest.  Before-image evidence is durable before the
transaction, and the terminal receipt is written only after query-only
readback.  A second submit with the same identity fails closed.  Ambiguous
transport permits only query-only readback.

Rollback is an append-only revocation of the operation.  No source or target
row is deleted or updated.  Ordinary healthy publication supersedes fallback
cell-by-cell automatically because the overlay cannot replace non-empty
values.

Full exact FBS lifecycle reconstruction and historical certification remain a
separate recovery scope.
