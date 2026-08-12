# Migration 136: FBW supply FF-origin assignment

## Goal and rollout boundary

Stage 4 adds only a durable operator assignment from an existing real WB/FBW
supply to an existing active FF facility. The commercial pool is fixed to
`FBO`. The assignment is identity/provenance evidence for a later shadow
writer; it does not post a warehouse operation, create a facility/pool movement,
reserve or debit stock, materialize a balance, change the WB supply cache or
switch any current producer/reader.

The existing FF facility/pool writer epoch is the fail-closed gate. No feature
epoch means assignment writes are disabled. Deployment creates no epoch,
facility, assignment, document, movement, opening, seed, backfill or cutover
state, so production remains default-off.

## Append-only contract

Operational schema ensure creates the empty
`sheet_vitrina_v1_wb_supply_ff_origin_assignments` table plus bounded supply and
facility indexes and immutable update/delete triggers. Each row stores:

- stable public assignment and client request ids plus semantic request
  fingerprint;
- exact canonical WB cache key and positive real `wb_supply_id`;
- a hash-only source evidence revision from current list/detail/goods/package
  digests and the exact authorizing writer feature epoch;
- one existing active immutable `facility_id` and fixed `pool=FBO`;
- optional `supersedes_assignment_id`, actor, bounded reason and UTC audit time.

The first assignment is unique per supply. A correction must name the current
assignment through optimistic concurrency, appends one successor and cannot
branch the chain. Exact request retry is T0; request-id reuse with different
semantics, stale current id, inactive/unknown facility, ambiguous cache identity
and a status-1 preorder without a real WB supply id fail closed. Physical delete,
in-place update, unassignment and guessed facility selection are not supported.

The table is covered by the existing reviewed `sheet_vitrina_v1_wb_suppl*`
warehouse-domain recovery allowlist. It contains no quantity or money and does
not require a new recovery tier or full-store backup.

## Protected API and operator UI

The existing supply-role protected family owns:

- `GET /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/wb-supply-origins`
  for paginated current/audit assignments;
- `GET .../wb-supply-origins/{supply_ref}` for one cached supply, active
  facility options, current assignment and bounded history;
- `POST .../wb-supply-origins/{supply_ref}` for one idempotent CAS-guarded
  assignment/correction.

GET opens SQLite `mode=ro` with `query_only=ON`, initializes no schema and calls
no external source. JSON is bounded and ETag-enabled. POST reuses the supply
role, global business-data write barrier, 256 KiB pre-buffer JSON limit and the
same-origin `X-WB-FF-Pool-CSRF: 1` gate.

The existing WB supply composition panel adds a compact Russian `Источник FF`
block. It loads assignment state lazily only after a supply is opened, renders
facility labels with DOM text, and disables selection in the normal default-off
state. Saving clearly states that no stock movement is created. Reload reads the
same server-owned current assignment; browser storage is not business truth.

## Verification and production acceptance

- `python3 apps/ff_wb_supply_origins_smoke.py` proves empty/default-off reads,
  real-supply identity, active facility/FBO restriction, idempotency, CAS,
  append-only correction, immutable audit, indexes and zero non-target
  operations/documents/movements;
- `python3 apps/ff_wb_supply_origins_http_smoke.py` proves protected routes,
  CSRF/cross-site rejection, pre-buffer size guard, conditional GET and
  default-off mutation rejection;
- `python3 apps/ff_wb_supply_origins_browser_smoke.py` proves lazy detail,
  text-safe facility options, assignment/readback after reload, narrow viewport
  and absence of fatal browser/server errors.

Production closure is exact-SHA deploy plus query-only schema/count/API/browser
evidence. Expected production rows remain zero because this stage neither
creates facilities/epochs nor performs an assignment. No production POST is
part of acceptance.

## Explicit later scope

Migration 137 adds the separate default-off official GET-only FBS observation
collector. Its activation/backfill, FBS order-origin assignment, shadow
movement writers/readers, facility seeds, historical opening/backfill/cutover
and live activation remain separate later stages and gates.
