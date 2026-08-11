# Migration 133: FF facility × pool foundation

## Goal and boundary

This stage adds only the inert contracts needed to decompose the existing
aggregate `ff` stage by fulfillment facility and commercial pool. It does not
activate a producer or consumer. The canonical warehouse stages and keys remain
exactly:

1. `production`;
2. `china_to_ff`;
3. `ff`;
4. `ff_to_wb`;
5. `wb`;
6. `wb_acceptance_discrepancy`.

A facility and `FBS|FBO` pool are dimensions inside `ff`, never additional
warehouses or stage-total operands. The future invariant is exact per-SKU and
overall conservation:

`aggregate FF quantity/capital = SUM(facility × pool quantity/capital)`.

Detail is explanatory decomposition. It cannot be added to public stage,
Vitrina or all-stage totals a second time.

## Additive schema

`packages/application.ff_pool_foundation.ensure_ff_pool_foundation_schema`
is called from the existing idempotent operational schema ensure. It creates
only empty tables, indexes and integrity triggers:

`packages/contracts/ff_pool_foundation.py` exposes frozen typed facility,
operation, pool-line, relation, balance, feature-state and parity-result values.

- `sheet_vitrina_v1_ff_facilities` — stable `facility_id`/unique `code`, name,
  active flag and display timezone. Identity/code cannot change and rows cannot
  be deleted; display metadata may evolve. Deploy inserts no business facility.
- `sheet_vitrina_v1_warehouse_business_operations` — immutable posted generic
  headers with operation type, stable source system/type/id/revision,
  idempotency epoch, explicit `business_date`, UTC `posted_at` and metadata.
- `sheet_vitrina_v1_ff_pool_movement_lines` — immutable signed effects by
  operation/line, facility, `FBS|FBO`, stable `nm_id`, exact SQLite `INTEGER`
  quantity, Decimal-safe TEXT capital delta and optional WAC snapshot.
- `sheet_vitrina_v1_warehouse_business_operation_relations` — immutable typed
  `correction_of`, `storno_of` and `late_expense_for` parent/child edges.
- `sheet_vitrina_v1_ff_pool_feature_epochs` — append-only independent writer
  and reader configuration. No row means both are off; the reader cannot be
  configured before the writer.
- `sheet_vitrina_v1_ff_pool_balances` — future current projection keyed by
  `facility_id,pool,nm_id`, bound to a feature epoch and source watermark.
- `sheet_vitrina_v1_ff_pool_parity_diagnostics` — append-only current-epoch
  exact aggregate/detail pass or mismatch evidence.

No new accounting column uses SQLite `REAL`. Decimal normalization is exposed
by `canonical_decimal_text`; schema checks reject exponent/non-decimal text and
fractional quantity storage. Headers own source identity/revision,
idempotency epoch and Yekaterinburg business date; audit timestamps are valid
UTC strings ending in `Z`.

Operation and line tables are append-only. Posted headers/lines cannot be
updated or deleted. A relation requires an existing typed child, cannot point
from a later parent to an earlier child, is unique per child/type and is
checked with a recursive CTE before insert so a cycle cannot be created.
Relations reference only new roots. Existing FF ledger, reservation, inventory,
overhead and functional document rows are not backfilled; absence of a root or
relation for legacy evidence is expected.

## Feature and parity contract

`read_ff_pool_feature_state` resolves absent configuration to
`feature_epoch_absent_default_off`. A configured detail writer may later run in
shadow while the current warehouse reader stays on the aggregate. The detail
reader is effective only when the latest diagnostic for that same epoch is
`pass`, its detail fingerprint still matches the current projection and the
caller supplies the same current aggregate revision. Missing, stale or
mismatched evidence remains fail-closed.

`evaluate_ff_pool_aggregate_parity` is query-only. With feature off it returns
`feature_off` without reading or judging detail. With an active epoch and no
current detail it returns neutral `detail_empty`, not an aggregate error. With
detail it compares exact quantity and Decimal capital per SKU and in total.
Mismatch returns the exact mismatched `nm_id` set, blocks only the future detail
reader and never writes to the aggregate FF ledger, functional balance, public
projection or feature configuration. `record_ff_pool_parity_diagnostic` can
append the proven result only while the epoch and detail fingerprint remain
unchanged; it does not activate anything.

## Operational performance and rollout

The deploy contour is `scope:live-runtime` because normal operational schema
ensure materializes the empty schema in the canonical store. This is not a
production business-data mutation. There is no store rewrite, table scan,
journal-mode change, seed, backfill, opening apply or new full-store backup.
All `CREATE ... IF NOT EXISTS` work is bounded metadata against new empty
objects and is safe for the current multi-gigabyte rollback-journal store.

The feature remains off because deploy creates no epoch. Existing FF ledger,
reservations, supplier acceptance, WB supply lifecycle, functional balances,
warehouse/public projections, product capital, Vitrina totals and
recommendations keep their current code paths and semantics.

## Explicit non-scope for stage 1

- facility production seeds or managerial facility CRUD;
- FBS order registry or WB supply-origin assignment;
- posting service, API, collector, XLSX workflow or UI;
- switching any current writer/read consumer;
- transfer-expense allocation or an open-transfer/in-flight materialization;
- transit warehouse or transfer reservation;
- historical cutover/opening, production backfill or business-data apply;
- WB writes or live FBS lifecycle.

Generic operation roots and signed pool lines are sufficient for a later
parent-transfer contract. In-flight must then be derived from an open parent
transfer and its linked children; stage 1 does not pre-materialize a speculative
read model.

## Verification

`python3 apps/ff_pool_foundation_smoke.py` proves:

- operational bootstrap and repeated schema ensure are idempotent and empty;
- only `FBS|FBO` is accepted;
- quantity is stored as exact INTEGER and capital/WAC as Decimal TEXT;
- source/revision/epoch idempotency and append-only immutability;
- typed relation uniqueness, forward chronology and cycle rejection;
- feature-off and empty-detail no-op behavior;
- exact multi-facility/multi-pool per-SKU quantity/capital parity;
- mismatch fail-closed behavior with byte-semantic global FF invariance;
- indexed balance/movement access plans;
- the six functional `STAGES` tuple is unchanged.

The baseline also retains the existing FF ledger/reservation/inventory/overhead/
documents, warehouse functional/business projection and operational storage
smokes. Semantic acceptance requires no imports from current producer/consumer,
HTTP/UI, stage mapping or public-total code.
