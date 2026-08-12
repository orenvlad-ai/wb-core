# Migration 138 — safe FF facility/pool cutover preparation

This migration deploys the default-off, additive planning and recovery evidence
contract for a later FF aggregate → facility × `FBS|FBO` opening. It does not
seed facilities, choose `T`, enable the Stage 5 collector, create an opening,
activate an epoch, process an FBS order or write to WB.

## Additive schema

- immutable cutover manifests and exact allocation lines;
- pre/post-boundary order classifications, opening-reservation evidence and an
  isolated `late_pre_t` lane labelled `Поздний заказ до границы`;
- explicit active-FBW origin assignments and collector checkpoint evidence;
- append-only recovery events;
- a warehouse-domain SQLite write epoch with DB triggers over canonical
  supplier acceptance, aggregate FF, pool documents, FBW origin, capital and
  functional projection tables. Cache/shadow collectors, previews and audit
  rows remain writable while the domain is held.

All quantities are signed SQLite `INTEGER`; monetary values and WAC are exact
Decimal text, including fractional kopecks already present in canonical
functional evidence. No existing table is rewritten and `journal_mode` is
unchanged.

Signed/fractional canonical state remains fully visible in a valid planning
manifest but is explicitly `apply_allowed=false` until the later production
mutation ships a signed exact-Decimal opening writer/readback. The fixture-only
Stage 2 cents/nonnegative proof is never presented as compatible with that
production-shaped state.

## Deployed boundary

`apps/ff_pool_cutover.py` is query-only (`preflight`, `dry-run`, `status`) and
opens the operational database with `mode=ro` and `query_only=ON`. There is no
production apply command or HTTP mutation route. The transaction implementation
is private and refuses databases without a test-only marker that operational
schema bootstrap never creates.

Normal deployment therefore materializes only empty tables, indexes and
triggers. An exact future production-mutation task must separately acquire the
canonical HTTP/maintenance/domain barriers, select `T`, review the manifest,
confirm exact facilities/mappings/opening allocation and invoke a trusted-main
runner that does not exist in this migration.

The manifest can reach `ready` only when each FBS classification and positive
quantity is pinned to separate append-only official-status shadow evidence.
Stage 5 order identity alone is insufficient. Opening reservations must fit the
exact facility/FBS/SKU allocation. Until a later shadow status collector writes
that evidence, a production manifest deliberately remains blocked.

## Invariants and recovery

- aggregate `ff` quantity/capital is unchanged and equals the sum of detail per
  SKU; the six warehouse stages remain unchanged;
- one China shipment can target one geographic facility and either/both pools;
  no facility identity or allocation is inferred or hardcoded;
- active FBW origin is explicit and never derived from WB destination
  `warehouse_id`;
- `supplierStatus=complete` is never a debit trigger and Stage 6 selects no FBS
  physical-debit transition;
- same manifest is idempotent, stale/different evidence fails closed;
- crash before commit leaves no rows; ambiguity after commit retains the
  barrier for exact readback; after live events only forward reconciliation or
  compensating documents are valid—delete/blind replay is forbidden.
