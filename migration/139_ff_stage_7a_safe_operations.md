# Migration 139 — Stage 7A safe FF operations

Migration 139 deploys three connected, default-off product surfaces without
creating or changing production business rows.

## Facilities and Settings

`Настройки → Склады` reads the geographical FF registry, immutable audit and
the two fixed system pools `FBS`/`FBO`. A facility has immutable `facility_id`
and `code`; display name and active state remain audited. The additive profile
table stores `city` separately from identity and reserves a JSON object for
future fields. It contains no address in this MVP and imposes no city
uniqueness. Physical delete remains impossible. Deactivation fails closed
while a durable request is unfinished or any pool balance is non-zero.

The UI shows a reviewed future setup—`FF Москва` proposed active and
`FF Оренбург` proposed inactive—but this is display-only contract data.
Deployment does not seed either facility or any profile row.

## Guided China → FF acceptance

The order card renders factual FF acceptance date as a read-only document
result and exposes `Принять на FF`. The bounded XLSX owns one active
geographical facility, exact nmId/barcode/SKU evidence, immutable expected
quantity, actual quantity, FBS/FBO split, shortage/surplus/mis-sort evidence
and pool-scoped/proportional common expense allocation. Preview is a durable
reload-safe workflow.

Legacy factual-date preview refuses an FF-acceptance change. The guided posting
service is the sole owner of factual date, the existing aggregate FF receipt,
facility/pool movements, exact capital/expense allocation, a related immutable
discrepancy child and targeted cost replay. Template generation uses a
non-persisting exact cost preview; confirm pins that source revision and the
new cost-layer revision contains actual accepted quantities. Existing v1
cost-layer fingerprints remain byte-semantically unchanged. The service
rejects an already accepted shipment and uses the existing aggregate receipt
source key exactly once.

Both writer epoch and an applied exact opening/cutover are required at the
service and UI layers. With either gate absent, template and validation remain
available after facilities exist, while confirm fails closed before a business
write. The current production zero-state therefore remains inert.

## FBS read-only shadow continuation

The collector remains default-off. When separately enabled later, it may use
`GET /api/v3/orders` plus `POST /api/v3/orders/status`; the POST is explicitly
an official read semantic. Status observations are privacy-minimized and
append-only, bind exact order revision/status digest/`observed_at`/positive
quantity, and cannot trigger a debit.

Separate append-only mapping contracts require exact seller warehouse →
facility and exact nmId/chrtId/barcode/SKU identity. Evidence is classified as
matched, isolated unmatched or deferred when exact identity is incomplete; no
name/fuzzy match exists. Settings exposes
query-only cursor/error/status/mapping/unmatched evidence. Historical review
starts at `2026-08-01`, but earliest official order date is computed from data.
Backfill execution, reservations, historical debit, movements, balances,
routing, returns and every live physical trigger remain absent/default-off.

## Production boundary

Release Train may deploy this live/runtime code and verify its query-only UI.
It must not create `FF Москва`/`FF Оренбург`, activate an epoch, apply opening,
enable env/collector, execute backfill or create business documents. Those are
separate exact owner-gated production mutations. Migration 140 now owns only
the separately authorized facility-registry and official FBS shadow activation;
opening/cutover, writer epoch and every physical-stock effect remain outside it.
