# MODULE 57 — Change Registry observer

## Status

`active/live_runtime`. The Registry consumer is enabled only by
`CHANGE_REGISTRY_OBSERVER_ENABLED=true` on the canonical hosted target. Its
fixed seller scope is `SELLER_PORTAL_CANONICAL_SUPPLIER_ID` plus
`seller-portal-primary`.

## Boundary

`ChangeRegistryObserver` consumes one joint read-only Prices + Ads manifest
from `ChangeRegistrySourceAcquirer`. All official WB GET calls finish
outside a SQLite transaction. Admission uses one short transaction for the
scheduled-slot/idempotency row and seller-scope lease. A second short atomic
transaction uses `ChangeRegistryBaselineEngine` and its transaction hook to
persist the result: checkpoint, bounded source summaries, scalar observations,
identity incidents, immutable facts and links, terminal job event, scheduled
health and lease release. A failure rolls that result back as a unit. No WB
POST/PATCH adapter, Balance writer, recommendation or
`manual_pending` row is reachable from this module.

The service is `wb-core-change-registry-observer.service`; its timer runs every
two hours, around minute 17, 24/7, independently from Vitrina refresh. The
service is also started by the live-runtime deploy after the repo-owned flag is
installed. Its deterministic two-hour scheduled slot makes a deploy and timer
collision one scan. The first successful production run is therefore an
explicit activation baseline.

## Observation and fact semantics

- The first `joint_complete` checkpoint persists source summaries and
  observations and creates zero facts.
- Only two scalar observations with status `exact`/`exact_zero` and a concrete
  integer, boolean or text value can prove a transition.
- Partial/failed source acquisition creates no facts and never becomes a
  baseline. Missing, null, inapplicable and error scalars do not advance the
  per-target exact value used for comparison.
- An exact zero is a real value. A transition to or from zero creates exactly
  one immutable fact.
- A target absent from a new complete manifest receives a `missing`
  observation with `target_disappeared`; it is never deleted or rewritten to
  zero.
- A campaign `advert_id` must map to exactly one `nmID`. Cardinality zero or
  many persists an immutable identity incident and creates no campaign/bid
  target or fact.
- Fact identity is derived from the prior/current observation proof. It is
  replay-idempotent and links the fact to its current proof checkpoint, leaving
  the same exact identity available for later change-item links.
- `observed_from` and `observed_to` are the observation window. The Registry UI never
  presents either boundary as an invented effective time.

## Health and concurrency

The DB lease has one owner per seller/account scope and CAS revision; concurrent
manual and scheduled starts produce one winner. Scheduled-slot uniqueness makes
replay deterministic. Two consecutive scheduled `partial`/`failed` outcomes set
health to `degraded`. Manual jobs do not change that counter. The next scheduled
complete outcome resets it to `normal`.

## Authenticated read surface

Under `Управление SKU → Реестр изменений`, the existing `sku_management`
authorization section owns:

- `GET /v1/sheet-vitrina-v1/sku-management/change-registry` — sanitized
  overview/status, fact intervals, identity incidents, jobs and annotation
  revisions;
- `POST .../change-registry/manual-scan` — asynchronous read-only scan admission;
- `POST .../change-registry/annotations` — append-only fact/checkpoint/incident
  annotation revision.

The payload contains no WB raw response, token, secret or mutable business
action. The already-published narrow `/sku-management/` nginx prefix owns all
three routes.

## Operational registration and proof

The service/timer are repo-owned systemd units and managed by the canonical
Europe hosted target. The observer service is a declared reader-writer of the
operational StoreRegistry generation. Business-data maintenance classifies its
timer as a continuous observer and does not stop it with unrelated business
writers. Production readback must prove the release receipt SHA, service result,
active timer, API/UI persisted status and a first complete checkpoint with zero
facts; foundation writer tables (`operations`, `items`, `attempt_events`,
`manual_pending`) remain empty.
