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

`wb-core-change-registry-observer.service` is timer-owned only; its timer runs
every two hours, around minute 17, 24/7, independently from Vitrina refresh and
keeps a deterministic scheduled-slot identity. Deploy never restarts that
service. Instead the trusted deploy invokes
`wb-core-change-registry-activation@<deployed-sha>.service`. Its job identity and
request digest bind the exact deployed Git SHA and have no scheduled slot. A
new SHA is a new activation even inside the same two-hour slot; exact replay of
an already complete SHA is a no-op. Deploy succeeds only when the exact
activation job is terminal `complete` with a checkpoint. Both observer units
and `wb-core-registry-http.service` receive the activation flag/account scope
from canonical target `runtime_env`; the owner-managed environment file remains
the credential/source-secret boundary. A timer/activation collision is
serialized by the seller lease, never collapsed into one identity.

Acquisition timestamps are canonical UTC `...Z` before all acquisition
digests. The observer also defensively canonicalizes the acquisition interval
and uses canonical `completed_at` for source-manifest `created_at` and bounded
summary JSON. Offset-equivalent instants therefore cannot alter source
manifest or checkpoint identity.
Each persisted bounded source summary also carries the acquisition's explicit
zero-persistence and zero WB `POST`/`PATCH` counters for query-only production
readback.

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
manual, scheduled and activation starts produce one winner. An existing job id
must exactly match seller/account, trigger, scheduled slot, actor/client binding
and stable request digest. A conflict fails closed. Exact terminal replay keeps
its prior outcome; `failed` or `partial` is never reported as success. A live
`accepted/running` replay remains nonterminal, while an expired owner can be
resumed by bounded revision-CAS or terminalized before another worker claims
the lease. CLI exit zero is reserved for terminal `complete`; accepted,
running, busy, partial and failed are nonzero. Manual idempotency binds the
seller/account/actor and client key without wall-clock bytes. Two consecutive
scheduled `partial`/`failed` outcomes set health to `degraded`; manual and
activation jobs do not change that counter. The next scheduled complete resets
it to `normal`.

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
three routes. Overview/status opens only the StoreRegistry operational
generation in `mode=ro`, verifies `PRAGMA query_only=ON` and never calls schema
initialization. A missing generation/schema returns a controlled empty
`schema_missing` readback without creating or changing a database file. Schema
ensure remains a writer/runtime activation responsibility.

## Operational registration and proof

The timer service, activation template and timer are repo-owned systemd units
managed by the canonical Europe hosted target. Both worker services are
declared reader-writers of the operational StoreRegistry generation.
Business-data maintenance classifies only the timer as a continuous observer
and does not stop it with unrelated business writers. Production readback must
prove release receipt SHA, exact-SHA activation `complete`, separately active/
waiting timer, free lease, authenticated API/UI status, unauthenticated `401`,
and the first complete checkpoint with zero facts and source-derived manifest/
observation/incident cardinality. Module-58 foundation counts and fact links
are reported exactly as persisted; observer activation creates no operations,
items, attempts or `manual_pending` rows but does not assume writer-owned tables
are empty.
