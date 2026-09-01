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
POST/PATCH adapter or Balance writer is reachable from this module. После
создания operator-owned `manual_pending` тот же transaction hook дополнительно
связывает уже доказанный exact fact либо детерминированно закрывает ожидание;
он не создаёт факт из pending и не вызывает WB.

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
Every local schema, admission, result and failure-evidence write boundary also
owns the canonical warehouse functional writer lock. A trusted activation
therefore waits behind an already running FBS or warehouse publication instead
of racing the shared operational SQLite generation. The lock is released before
all Prices/Ads WB GET calls and reacquired only for the short local persistence
transaction; source acquisition never extends the writer-lock hold.

Acquisition timestamps are canonical UTC `...Z` before all acquisition
digests. The observer also defensively canonicalizes the acquisition interval
and uses canonical `completed_at` for source-manifest `created_at` and bounded
summary JSON. Offset-equivalent instants therefore cannot alter source
manifest or checkpoint identity.
Each persisted bounded source summary also carries the acquisition's explicit
zero-persistence and zero WB `POST`/`PATCH` counters for query-only production
readback.

Every local persistence boundary is named before it executes:
`baseline_ingest`, `baseline_result`, the separate Prices/Ads source-manifest
inserts, terminal job-event insert, scheduled-health insert, lease release and
transaction commit. A rolled-back primary failure is copied into the fallback
terminal event with its original stage, logical table/operation, sanitized
SQLite error code/name, allowlisted constraint category/identifier, bounded
safe message and deterministic digest. SQL text, raw source payloads, runtime
paths, tokens and secrets are never retained. If the fallback transaction
itself fails, a rescue event retains both primary and fallback typed evidence;
the primary failure is never replaced by the fallback exception.

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
- Pending запоминает pre-pending exact observed value и desired target. Только
  первый proven transition после pending и до `+24h` может закрыть lifecycle:
  exact target даёт `matched` и links к change item/recommendation, иное exact
  значение даёт `deviated` без ложного fulfillment-link. Existing fact только
  получает недостающие links, поэтому observer/manual race не создаёт дубль.
- Если transition не доказан за 24 часа, ближайший scan/status reconciliation
  добавляет `expired` без fact/change. Значение, существовавшее до pending, не
  является match. Cardinality `0|many` не имеет exact bid observation и потому
  fail closed до создания pending.
- Manual-pending lookup типизирован отдельно от live writer lookup: только
  item, для которого существует append-only manual pending event/current
  lifecycle, является pending candidate. Совпавший
  `recommendation_item_id` у live bid или `campaign_state` writer item без
  manual event не участвует в pending reconciliation и не может сломать
  `GET latest`/Registry overview.

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

Terminal job evidence keeps source acquisition and local persistence separate.
`source_status` records `complete`, `partial`, `failed` or invalid returned
source evidence; `failure_origin=source_acquisition` is used only when source
acquisition raises before a result exists, while
`failure_origin=local_persistence` retains the exact persistence stage after a
source result exists. A failed persistence transaction creates no checkpoint,
source manifest, observation, incident, fact or baseline; fallback health and
lease release remain scheduled-slot idempotent.

## Authenticated read surface

Under `Управление SKU → Реестр изменений`, the existing `sku_management`
authorization section owns:

- `GET /v1/sheet-vitrina-v1/sku-management/change-registry` — sanitized
  overview/status, manual-pending state/history, fact intervals, identity
  incidents, jobs and annotation revisions;
- `POST .../change-registry/manual-scan` — asynchronous read-only scan admission;
- `POST .../change-registry/annotations` — append-only fact/checkpoint/incident
  annotation revision, включая append-only комментарий к deviated pending.

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
