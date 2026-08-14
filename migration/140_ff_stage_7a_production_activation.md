# Migration 140 — Stage 7A production activation

Migration 140 owns one separately authorized production mutation after the
deploy-only Migration 139. Its entire effect is the geographical facility
registry plus the official FBS observation/mapping shadow. It does not open a
facility × pool ledger and is not the opening/cutover stage.

## Exact cohort

The reviewed plan may create exactly two stable facility identities through
the same server-owned request-ID → facility-ID/code derivation used by the
facility service, with task-local deterministic request IDs:

- `FF Москва`: `city=Москва`, `display_timezone=Asia/Yekaterinburg`, active;
- `FF Оренбург`: `city=Оренбург`, `display_timezone=Asia/Yekaterinburg`,
  inactive.

The existing system-owned `FBS` and `FBO` pools are reused. No pool, feature
epoch, opening manifest, checkpoint or `T` is created. `FF Оренбург` has no
active seller-warehouse mapping.

The FBS collector uses only official Marketplace evidence:

- `GET /api/v3/warehouses` supplies stable seller warehouse IDs and their
  exact `officeId` relation;
- `GET /api/v3/offices` supplies the exact official office identity/city;
- `GET /api/v3/orders` supplies privacy-minimized FBS observations;
- `POST /api/v3/orders/status` is the documented official read semantic and
  never a WB mutation.

A seller warehouse can map to `FF Москва` only when the observed
`warehouseId` exists in the official seller registry, its exact `officeId`
exists in the official office registry and that office has an explicitly
accepted canonical Moscow city value. No warehouse name, fuzzy match or
hardcoded warehouse ID participates. Orders on any other official warehouse
remain unrouted and counted.

SKU mapping is append-only and requires one active nomenclature owner for the
complete exact tuple `nmId + chrtId + one barcode + article/SKU`. Incomplete,
unmatched and ambiguous tuples remain isolated and counted. They do not block
the safe facility/collector activation unless an already active mapping
conflicts with the exact target.

## Production runner

The only apply path is `apps/ff_stage_7a_production.py`, reached on the
canonical server through these hosted commands:

- `ff-stage-7a-production-dry-run`;
- `ff-stage-7a-production-apply`;
- `ff-stage-7a-production-readback`.

Dry-run is the default and pins the exact deployed main SHA, official source
digest, watermark, facility identities, exact mappings, unmatched/deferred
counts, expected inserts and non-target invariants. The private reviewed plan
stays outside the Git checkout with mode `0600`. Apply accepts that exact JSON
over stdin and requires its fingerprint, the GitHub owner gate reference and
actor. The hosted wrapper refuses a non-canonical target/runtime/env/service
or a deployed SHA different from `.wb-core-runtime-sha`.

Before the first write the runner creates a mode-`0600` exact target
before-image and a separate full environment-file before-image, both with
SHA-256 evidence. Official shadow observations are immutable and retained;
there is no destructive automatic rollback. Recovery is forward
reconciliation or a separately authorized configuration restore. An exact
interrupted run reuses the same before-image and safely resumes the same
facility/mapping cohort.

Apply is serialized with the warehouse functional writer lock. It revalidates
the reviewed sources under that lock, installs only the two facilities and
exact mappings, collects `2026-08-01..pinned watermark`, runs one additional
ordinary 24-hour collection probe, and only after both reads succeed sets
`WB_FBS_COLLECTOR_ENABLED=true`. The hosted wrapper then restarts the canonical
HTTP service and performs a fresh query-only readback.

## Original activation polling and current supersession

The original activation used polling, not real time, as part of the existing
warehouse functional timer with:

- unit `wb-core-warehouse-functional-sync.timer`;
- `OnCalendar=*-*-* *:17:00 Europe/Moscow`;
- `AccuracySec=2m`;
- operational SLO: the next successful hourly warehouse sync.

Migration 141 supersedes this original hourly polling wiring with a dedicated
five-minute read-only collector.  The historical Stage 7A apply/reconciliation
still proves its original activation boundary, while current live cadence and
handoff evidence are owned by Migration 141.

Reconciliation proves exact facility rows/states, collector configuration,
successful complete cursor state, earliest official order date, latest
watermark/lag, order/status totals, exact mapping rows, matched/unmatched/
deferred evidence, the successful next collection probe and no collector
error. The evidence JSON is private and digest-bound to the reviewed plan.

## Hard non-targets

The runner snapshots and rechecks all of the following as invariants:

- aggregate FF quantity, capital, version and row count;
- feature epochs, opening/cutover/checkpoints and opening reservations;
- pool balances, operations and movement lines;
- FF stock operations and reservations;
- `actual_ff_acceptance_date` facts and warehouse-domain epoch events;
- WB mutations, historical/current FBS debit, returns, routing and guided
  China → FF final confirmation.

Any drift fails closed. The PR uses `scope:production-mutation`; merge, deploy,
apply, reconciliation comment and trusted-main terminalization remain under
the current GitHub Release Train production-mutation contract. No subsequent
opening/cutover stage starts automatically.
