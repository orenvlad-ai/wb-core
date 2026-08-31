---
title: "Модуль: warehouse_functional"
doc_id: "WB-CORE-MODULE-48-WAREHOUSE-STOCKS-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать единый production-контур шести складов, себестоимости, товарного капитала, WB snapshot и guarded functional cutover."
scope: "Canonical warehouse/cost state, Decimal WAC, source provenance, targeted replay, hourly WB sync, UI/API and production cutover."
source_basis:
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
  - "docs/modules/44_MODULE__WB_FINANCE_WEEKLY_REPORT_BLOCK.md"
  - "migration/152_fbs_handoff_cost_and_overhead_backfill.md"
  - "migration/153_vitrina_wb_ff_inventory_cost_blend.md"
  - "migration/155_functional_economics_inventory_blend_publication.md"
  - "migration/161_applicability_gated_dense_fbs.md"
  - "migration/162_dense_fbs_zero_and_historical_material_recovery.md"
  - "migration/163_historical_analytical_cost_carry_forward.md"
related_modules:
  - "packages/application/warehouse_functional.py"
  - "packages/application/ff_pool_foundation.py"
  - "packages/application/ff_pool_documents.py"
  - "packages/application/ff_pool_fbs_applicability.py"
  - "packages/application/ff_pool_dense_fbs.py"
  - "packages/application/warehouse_fbs_material_rematerialization.py"
  - "packages/application/ff_pool_overhead_backfill.py"
  - "packages/application/ff_pool_documents_xlsx.py"
  - "packages/application/ff_pool_surfaces.py"
  - "packages/application/ff_warehouse_documents.py"
  - "packages/application/ff_overhead_allocation.py"
  - "packages/application/ff_document_workflow.py"
  - "packages/application/warehouse_archival_estimate.py"
  - "packages/application/calculation_parameters.py"
  - "packages/application/stocks_block.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/application/ff_stage_7a_production.py"
  - "packages/application/wb_fbs_shadow_polling.py"
  - "packages/application/wb_fbs_warehouse_registry.py"
  - "apps/wb_fbs_warehouse_registry.py"
  - "apps/warehouse_functional_runner.py"
  - "apps/ff_pool_overhead_backfill.py"
  - "apps/warehouse_fbs_material_rematerialization_smoke.py"
  - "apps/warehouse_fbs_historical_recovery.py"
  - "apps/wbc0013_fbs_recovery.py"
  - "apps/sheet_vitrina_v1_historical_cost_carry_forward.py"
  - "apps/sheet_vitrina_v1_historical_missing_repair.py"
  - "apps/warehouse_cost_queue_replay.py"
  - "apps/sqlite_backup_archive.py"
  - "apps/ff_stage_7a_production.py"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/warehouses"
  - "GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}"
  - "GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}/documents"
  - "GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}/documents/{document_id}"
  - "POST /v1/sheet-vitrina-v1/warehouses/sync"
  - "POST /v1/sheet-vitrina-v1/warehouses/emergency-rebuild/preview"
  - "POST /v1/sheet-vitrina-v1/warehouses/emergency-rebuild/apply"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/inventory/status"
  - "GET /v1/sheet-vitrina-v1/warehouses/ff/overhead/status"
  - "GET|POST /v1/sheet-vitrina-v1/warehouses/ff/facility-pools/*"
  - "GET|POST /v1/sheet-vitrina-v1/settings/calculation-parameters"
  - "POST /v1/sheet-vitrina-v1/settings/calculation-parameters/preview"
source_of_truth_level: "module_canonical"
update_note: "Active truth remains versioned functional balances. Closed Vitrina repair is exact source-operation/as_of-date bound and cannot project the current warehouse state backward; deploy remains inert."
---

# 1. Active warehouse contract

Active state содержит ровно шесть складов:

1. `production` — `На производстве`;
2. `china_to_ff` — `Китай → FF`;
3. `ff` — `Склад FF`;
4. `ff_to_wb` — `FF → WB`;
5. `wb` — `Склад WB`;
6. `wb_acceptance_discrepancy` — `Расхождения приёмки WB`.

Migration 133 does not extend this list or `warehouse_key`. Facilities and
`FBS|FBO` pools are dimensional detail strictly inside `ff`. The only future
admissible aggregate has exact INTEGER quantity and canonical RUB minor-unit
capital equal to `SUM(facility × pool)` per SKU and in total;
detail rows are never additional stage or all-stage operands. The foundation
is empty and feature-off after deploy, and no active functional writer, read
route, compact read model, public total, Vitrina cell or recommendation imports
it. A mismatch diagnostic can only keep the future detail reader disabled; it
cannot change the current global FF value or its certification.

Migration 134 additionally deploys an additive empty Stage 2 document schema
and domain service. It can post only when a future explicit writer epoch is
enabled and therefore remains inert in production. No public API/UI imports
it; supplier acceptance, aggregate inventory/overhead, functional publication
and all six-stage readers retain their current paths. Its immutable movements
can later support exact pool WAC, transfer/loss conservation and linked
correction/storno/late-expense evidence without becoming a second FF ledger.

Migration 135 routes bounded read models and guarded operator orchestration
under the existing protected warehouse prefix. Facility/pool GET models use
query-only connections, exact Decimal aggregation, server pagination, ETags
and lazy document evidence. Their aggregate revision is the current immutable
functional `ff` version; a stale/missing parity proof keeps only detail hidden
and cannot replace the active aggregate balance. The compact modal is
explanatory and never adds its quantities/capital to the existing six-stage
summary or TOTAL. Facility/document writes remain fail-closed until a separate
writer feature epoch exists; deploy creates no epoch, seed, opening or
movement.

Supplier registry и warehouse projection не смешиваются. Invoice получает один стабильный `supplier_flow_id`; display name строится из invoice, но linkage и replay используют stable id. После смешивания на FF downstream identity принадлежит WB supply. Каждая positive line хранит exact text quantity, capital, WAC, coverage/quality и source provenance. Вычисления используют `Decimal`; UI округляет только display.

`GET .../warehouses` и detail routes сохраняют совместимость route/key, но после functional cutover читают только active functional version. Старые opening tables не суммируются с active balances.

Migration 138 does not reuse `warehouse_opening_v1` and does not introduce a
seventh stage. Its future opening manifest decomposes the exact active `ff`
revision into existing active facilities and `FBS|FBO` pools with equality of
integer quantity and exact Decimal capital for every `nmId` at the immutable
opening boundary. Facility identity,
Moscow/Orenburg allocation and `T` are external reviewed decisions, never
deploy-time defaults. Detail remains unpublished and is never added a second
time to total stock.

Migration 142 activates that detail only through an exact owner-gated Stage 7C
manifest. The live aggregate remains the public six-stage `ff` truth and must
equal the sum of facility × `FBS|FBO` after opening, historical FBS debit and
every later lifecycle/guided document. Opening accepts signed INTEGER quantity
and exact Decimal text capital, including fractional kopecks. Later operational
parity keeps quantities exact and compares capital through the centralized
`ROUND_HALF_UP` kopeck policy per SKU and in total. Conserved, attributed raw
sub-kopeck differences remain audit diagnostics; canonical/cross-boundary or
unattributed differences fail closed. FBS reservations
change `available`, not physical aggregate quantity; negative available is a
valid explicit state. Any aggregate/detail, source, mapping, pending-receipt or
checkpoint drift disables/rejects the cutover instead of publishing a second
total. Once this cutover is applied, every ordinary functional publication
derives physical `ff` quantity/capital directly from the complete current
facility × pool balance set. Legacy FF replay is retained only for chronology,
historical evidence and WB-outbound WAC. The pool manifest, writer epoch and
exact rows are captured in the functional local-source digest and rechecked
under the apply lock, so a concurrent pool movement rejects a stale plan and a
later hourly version cannot undo an already posted FBS lifecycle debit.

Migration 143 separates immutable opening truth from continuous FBS ingestion.
The reviewed source fingerprint covers only frozen rows at/below compound `W`;
new order/status/transition rows above `W` are a post-checkpoint suffix, not
aggregate or manifest drift. Aggregate, facilities/mappings, policy, deployed
SHA, non-target evidence and pending-receipt proof remain exact apply-time
gates. The suffix drain preserves the same aggregate = sum(detail) invariant.
After cutover that drain and guided FF receipt confirm share the functional
warehouse writer lock. A ready receipt is owner-bound to its immutable business
effect rather than the continually changing whole-balance digest; confirm
rebases its T1 before-image only after current global parity passes under the
lock. A collector cycle that meets the short window retains its observations
and reports the drain held, then consumes the unchanged sequence suffix exactly
once on the next pass. Live FBS is never paused or written back to WB.

Migration 144 does not alter that opening or the active facility/pool ledger.
It may mark one earlier failed Stage 7C Recovery Policy operation
`superseded` only after exact append-only proof of a distinct later reconciled
cutover. The remediation writes no functional/business projection itself.
Once the blocker is terminal, the ordinary warehouse publisher takes physical
FF quantity/capital and reservations from one current facility × pool model,
publishes `ff = SUM(facility × pool)` exactly once, and derives available from
physical minus reservations. A prior business projection remains last-good
until that publication succeeds; it is never patched directly.

Migration 145 adds a query-only current planning composition beside this
accounting projection. It combines the fresh official WB aggregate with signed
FBS available across active facilities for the user-facing planning metrics,
while leaving six-stage quantity/capital `TOTAL` and every existing calculator
unchanged. The formula/cutover epoch and provenance are explicit, old history
is not rematerialized, and the ordinary publisher performs only a readback of
the combined surface after canonical publication. Incident-adjusted values
fail closed without exact snapshot-bound per-SKU evidence; aggregate-only WB
never fabricates current district values.

The same boundary protects persisted closed Web Vitrina economics. Once a ready
date is bound to an exact functional version, a later warehouse business
projection or capital-event revision cannot rewrite that date through ordinary
hourly/current publication. Candidate drift or WB/FF evidence mismatch preserves
the last-good warehouse/cost/Proxy values and emits typed
`historical_repair_required` evidence. It never changes the exact historical
version or silently replaces numbers/confirmed zero with blanks. Any historical
change requires a separate version-bound reconciliation contract; current-day
warehouse publication remains independent.

Repair for the WBC0010 incident uses only the retained verified T1 before-image
of the first destructive publication. Each date is bound to the source row whose
`as_of_date` is that exact date; the overlapping prior temporal bundle is not an
equivalent source. One reviewed manifest enumerates 33 SKU × six cost/Proxy
families plus six TOTAL values for each included date, applies all exact ready
rows in one `BEGIN IMMEDIATE` CAS under the shared writer lock, and records a new
T1 rollback operation. The independent `2026-08-26 / 428853741` cost-coverage
invariant is excluded until its immutable event/version proof is sufficient;
the repair never copies that known-bad before-image or claims full incident
completion. Finance, functional versions, current/future projections and
non-target ready cells are immutable acceptance boundaries.

Full Vitrina refresh preserves that boundary by carrying the exact closed-date
coverage, functional-economics marker, repair registry and target presentation
evidence together with the frozen last-good cells. It never copies current-date
mutable evidence backward or fabricates closure. The following hourly publisher
must complete in one pass and an immediate rebuild must be a no-op.

Migration 147 narrows the ordinary main Web Vitrina planning profile to raw WB,
signed FBS total, one row per active FBS facility and raw combined total.
Incident-aware effective WB/combined aliases and every incident metric family
are archived from public read/catalog/settings/picker without deleting their
definitions, evidence or persisted historical snapshot cells. `stock_total`
remains the current raw-combined presentation alias; persisted ready history
and all calculation consumers are unchanged. A historical exact-date request
is never overlaid from the current planning snapshot. Missing per-SKU physical
FBS evidence remains unavailable and is never synthesized as zero.

Migration 161 closes the current-state coverage gap at the registry boundary.
Every active facility × active stock-managed SKU is applicable to FBS by
default, unless an immutable dated exception says otherwise. New facility/SKU
activation is staged inactive, covered by the existing `pool_inventory`
document/CAS/readback contour under the shared warehouse lock, and published
active only after all applicable physical rows are exact. Missing rows become
document-proven explicit zero without movement, quantity/capital delta or WAC;
existing rows are retained field-for-field and an existing canonical zero gets
an immutable dense-T0 receipt without a rewrite. Receipts, writeoffs,
reservations and order lifecycle cannot create a missing FBS row. FBO, WB and the six-stage
aggregate are unchanged by initialization. Current readers publish typed
`exact|exact_zero|missing|inapplicable` evidence and exclude inactive facility
and inactive-SKU pairs without deleting their historical rows.
Retiring a SKU is blocked under the same warehouse lock while any prior FBS row
is non-canonical-zero, any active-facility applicable row is missing, or an
active reservation/unfinished FBS lifecycle/order dependency exists. A
zero-covered SKU may archive/reactivate without resetting
its row. Facility deactivation is likewise blocked for non-canonical-zero FBS
quantity/capital/WAC, pending pool requests, active FBS reservations, open
reconciliation, unresolved mapped identity or unfinished mapped FBS orders;
the pre-existing quantity guard for other pools is unchanged. New-SKU
activation stores only its facility pairs plus a compact
streamed proof of existing coverage; it does not persist the default
applicability cross-product. Current applicability uses the canonical EKT
business date.

The 26-August Migration 161 addendum prevents a later FBS business effect from
mixing accepted versions. A lifecycle debit, guided receipt/recovery or pool
overhead first commits the canonical facility/pool effect and derives the exact
aggregate FF quantity, capital, WAC, cost-covered quantity and location
provenance from that same physical ledger. In the same transaction and under
the shared warehouse lock it creates a successor immutable functional version,
retains the exact same-business-date WB snapshot, reservations, effective
supplier-cost state, unmatched audit and compact WB option/read models,
publishes the versioned business projection, then switches active last by CAS.
The prior version and history are never updated. A concurrent reader therefore
sees complete `1953/1953` or complete `1952/1952`, never the incident shape
`1952/1953`; a missing canonical snapshot/closure blocks the physical writer.

That lifecycle transaction also inserts the canonical targeted-recalculation
queue identity bound to its immutable event and successor version. Version
binding hides the prior ready material, so restart resumes economics from the
durable queue without repeating the physical event. Pool overhead retains its
existing same-transaction queue and guided receipt/recovery retains its durable
posted/replay continuation; no active physical version depends on an unrecorded
best-effort recalculation.

An internal query-only recovery planner handles only one proven active-date
`facility × FBS × SKU` mismatch and has no CLI, HTTP route, timer or automatic
apply. It requires one immutable lifecycle/document source watermark, exact
single-SKU mismatch breadth and bounded functional/ready closure; FBO, WB,
historical, broad or unknown-source cases fail closed. The deterministic
candidate recomputes the affected SKU plus TOTAL own cost, Proxy 3
profit/margin and Proxy 4 profit/margin/unit margin, and pins source/target
version, roster, provenance, auxiliary and ready-snapshot digests. Durable
append-only intent states are `repairable`, `repairing`, `repaired`,
`retry_exhausted`, `historical_recovery_required` and `unsafe_ambiguous`; the
complete bounded plan persists across process restart, and exact readback
reconciles a lost response without blind retry. Typed evidence publishes only
dependencies, invariant reasons, repairability and candidate/readback identity
for a future WBC0012 consumer. It defines no color, severity or health UI and
does not apply the historical incident automatically. Migration 162 adds a
separate manifest-bound historical lane for one accepted immutable version and
one handoff event. WBC0013 binds date `2026-08-26`, nmId `428853741`, version
`whfv_cb0657c384d5adebae01e585` and event
`ffbf_87cea959c9d600da99caa1ab68ef` directly; it does not enumerate a broad
mismatch set. It binds separate version-plan, full-version-row,
target-row, provenance and event source/status/evidence/row digests through one
query-only StoreRegistry generation. A positive finite legacy long WAC remains
acceptable only under unchanged exact row/provenance evidence and is never
rewritten; event multiplication, pre-debit location arithmetic and the
candidate ratio use Decimal precision 38. The accepted target must retain its
exact three-location evidence, and the ready closure must expose the target own
cost plus exactly six TOTAL dependencies. The full facility/pool location
transform debits Orenburg while preserving Moscow and the third location,
reconstructs the historical FF target and
target+TOTAL economics closure, and publishes a new
historical good version while preserving the current active/sync pointer and
every current facility/pool row, including all terminal A zeros. Candidate
timestamps come from the accepted source event, making independently generated
material witnesses identical. Explicit apply requires plan fingerprint,
approval and actor; intent is persisted only under the shared lock after CAS.
Deploy, timers and ordinary refresh never call it. The default-off WBC0013
Production Apply profile runs dense A once, query-only reconciliation, then a
fresh historical B once, with two identical material witnesses per phase and no
blind retry.

Migration 163 is a distinct presentation-only lane and never resumes or rewrites
historical B. For the exact owner-fixed `2026-08-26 / 428853741` target it accepts
the typed `117.537167 RUB` analytical estimate with owner-authorization digest;
this value is not lifecycle-derived or warehouse WAC. The ordinary route may
carry the latest earlier version-tagged own cost of the same SKU into one
historical ready-snapshot date only after query-only functional,
movement and lifecycle evidence proves no receipt, revaluation, WAC drift or
other cost-changing event between source and cutoff. The server-side economics
formula computes the target SKU and six TOTAL dependencies, but publication
copies only that exact closure and an `analytical_only` provenance marker. One
append-only analytical version and one ready-snapshot CAS are the entire write
set; functional versions/balances, current pools, movements, orders and raw
history remain unchanged. Exact target-payload and all-other-ready-snapshot
digests, one-submit identity, backup and query-only readback are mandatory.

# 2. Physical and cost rules

## 2.1 Production and China → FF

Invoice без counted supplier payment имеет zero warehouse quantity. Первый counted payment активирует полный physical invoice composition; следующие payments меняют capital/WAC, но не quantity. `counted` следует CNY ledger contract: `posted` и его детерминированный date-only ordering warning участвуют только при posted parent document; blocked/skipped и needs-review/excluded parent documents не участвуют. Отмена последнего payment через audit archive и targeted replay возвращает quantity к zero.

Supplier payments используют factual weighted RUB cost списанных CNY из CNY ledger. Конверсионная комиссия, уже включённая в RUB value CNY, второй раз не добавляется. CNY transfer fee и direct RUB bank fees имеют отдельную provenance. Supplier capital и bank fees распределяются по invoice value.

Фактическая supplier shipment date переносит тот же quantity/capital layer в `china_to_ff`. Logistics invoice и customs 1010 распределяются по quantity; duty 2010 и import VAT 5010 — по invoice value. Informational/needs-review/failed/duplicate/unmatched/excluded documents не капитализируются.

Active supplier source view contains only non-excluded financial documents and expense lines whose parent is active. An archived same-SHA duplicate remains in source/audit storage but is absent from `document_controls`, source/calculation fingerprints, supplier capital and every downstream layer. Exclusion changes the semantic source revision immediately, invalidates certification and queues exact replay. Immutable version publication completes only queue rows whose `queue_id`, stable source id and exact source revision were part of the verified plan; stale or unrelated requests cannot be blanket-completed. Green certification additionally requires complete conserved document allocation, so matching stale fingerprints or `expenses_complete` alone are insufficient.

## 2.2 FF

Фактическая FF acceptance создаёт canonical append-only FF ledger receipt. Functional projection не создаёт второй ledger: cutover opening freezes current ledger quantity/cost, а post-cutover receipt/debit replay начинается от opening version. Supplier receipt получает exact source-flow capital; одинаковые SKU смешиваются moving WAC. Ordinary proportional debit сохраняет WAC.

Facility/pool business documents use kopecks as their canonical posting
boundary even when canonical supplier allocation contains fractional kopecks.
Guided acceptance rounds only the exact shipment header and allocates the
canonical kopecks by largest fractional remainder then `nmId`; the immutable
replay row carries its per-SKU targets and explicit signed normalization
residuals. The append-only FF receipt lines freeze that same canonical capital
as authoritative `cost_snapshot` evidence; functional replay consumes the
capital first and derives its ratio, so repeating Decimal unit division cannot
lose a sub-kopeck residue. Invoice/payment/expense document controls remain
independently conserved while supplier receipt, aggregate FF and facility/pool
detail publish the same total. The recovery receipt freezes the exact negative
snapshot, and its immutable recovery row restores the pre-acceptance supplier
stage; it never subtracts a source document component or manufactures a
zero-cost flow.

WB status `Отгрузка разрешена` creates one idempotent canonical FF debit of the full exact packed composition when identity is confirmed and physical FF quantity is sufficient. The separate reservation ledger is used only for physical shortage or unresolved identity/composition; it never represents missing transit or another cost component. A later supplier receipt fulfils a physically justified reservation atomically. Known FF capital follows the quantity; absent downstream transit/services/storage/paid-acceptance amounts make the cost layer preliminary or unavailable with an explicit blocker and never synthetic zero. Late cost replay enriches that supply layer without another debit. `Допринято` does not create a second debit.

Successful positive Seller Portal transit enrichment is joined into this same downstream supply layer. It does not overwrite official WB facts. The full corrected sent composition remains the denominator, and one post-save callback idempotently rematerializes only dependent cost layers. Confirmed zero is distinct from not-requested/updating/not-found/source-error/session-expired; error or missing cost stays a truthful cost-freshness blocker and repeat sync/recovery cannot debit twice.

Audited FF inventory and overhead remain operations of the same append-only ledger. Inventory parent is an audit/document link only and contributes zero movement to functional replay; its linked receipt/writeoff children carry the physical and frozen capital effects. New operator overhead is facility-local `pool_overhead`: the selected facility and `FBS|FBO|both` positive physical quantity is the complete denominator, reservations are excluded, and every included positive row must have positive known capital or the whole preview fails closed. Its business date is the current date in the selected facility timezone, never the historical payment date. Overhead and its reversal carry zero quantity plus exact positive/negative `cost_adjustment` capital and retain stable category, comment, manual/PDF origin and payment evidence linkage. Confirm atomically changes detail and affected current aggregate capital/WAC at one durable event order, changes no quantity and inserts one exact targeted queue identity. A concurrent FBS debit therefore freezes either the previous or the new exact facility WAC, never a timestamp tie or split aggregate/detail state. Historical fulfilled debits retain their own frozen WAC. Legacy aggregate overhead remains read/reversal compatibility only; its operator creation surface directs to the facility/pool document and is not a second posting path.

## 2.3 FF → WB and discrepancies

До final acceptance по SKU:

`open quantity = max(packed - accepted, 0)`.

Final accepted supply обнуляет `ff_to_wb`; positive final difference поступает в pooled discrepancy warehouse по SKU. Accepted part никогда не прибавляется к WB quantity вручную. FF services, storage and transit распределяются по полному packed quantity даже при partial/zero acceptance; accepted quantity хранится отдельно и никогда не подменяет packed denominator. Official transit component имеет приоритет; Seller Portal transit используется только при отсутствии official transit.

Paid WB acceptance отделена от transit: она капитализируется только на фактически accepted quantity, входит в accepted WB inbound cost и исключена из `ff_to_wb`/discrepancy WAC. `cost_total` не может скрыто превратиться в transit: canonical layer сохраняет pre-acceptance cost и acceptance amount/per-accepted-unit отдельно.

Discrepancy WAC содержит все pre-acceptance costs. `Допринято` сопоставляется pooled строго по тому же `nm_id`: `matched=min(doprinato, positive balance)`. Surplus попадает в transitional unmatched audit и не создаёт negative quantity/capital. Targeted replay повторяет match, когда появляется positive balance. Automatic loss writeoff не реализован.

## 2.4 WB snapshot

WB является snapshot warehouse. Единственный quantity source — полный успешный official `/api/analytics/v1/stocks-report/wb-warehouses` response:

`WB contour = quantity + inWayToClient + inWayFromClient`.

`snapshot_date` — каноническая business date snapshot. UTC `fetched_at`/`effective_at` остаются временем аудита и не используются через строковый `[:10]` как дата витрины: около полуночи UTC это иначе относило локальный снимок `Asia/Yekaterinburg` к предыдущему дню. Daily replay и выбор persisted version группируют по сохранённому `snapshot_date`; fallback timestamp переводится через canonical business timezone. Историческая витрина принимает только exact-date good version: предыдущий snapshot не переносится на пропущенный день, а exact пустая версия остаётся доказанным нулём, а не отсутствием данных.

Каждый snapshot сохраняет requested IDs, exact source raw rows, page offsets/count, completion flag, raw-row digest and `fetched_at`. Requested SKU разбиваются на official batches максимум по 1000 `nmIds`; только успешное завершение всех batches/pages образует один атомарный snapshot. Incomplete coverage, pagination failure, exhausted 429, transport error or invalid payload оставляют last good version; UI показывает freshness/error. True zero допускается только внутри complete response. Доказанный official special bucket `warehouseId=0`, `warehouseName=Остальные` сохраняется как отдельная warehouse-name/region identity: его in-way quantities входят в WB contour, но произвольный zero ID по-прежнему считается invalid payload.

Official current response may additionally return the exact typed aggregate sentinel `warehouseId=-999999`, `warehouseName=Склад WB`, `regionName=Склад WB`. Only that full integer-and-names identity is accepted; float, bool, string, partial-name and every other negative ID fail closed. Raw rows and their digest remain source truth, while a separate normalization envelope maps the sentinel to service bucket `0 / Остальные` for exact SKU/TOTAL reconciliation and sets `warehouse_granularity_complete=false`. A historical date with `OfficeName=Склад WB` is also aggregate-only, including a mixed concrete + aggregate CSV. No aggregate quantity is allocated by warehouse/region fallback: exact `stock_total` and in-way facts survive, while regional stocks and incident regional actual/excluded/effective remain blanks. Strict Supply, SKU Management `Общее`, Factory Order and regional action consumers treat such evidence as incomplete and cannot act on it. The module-53 `Баланс запасов` exception is total-only and read-only: it may use a complete exact current per-SKU `stock_total` as coefficient-weighted opening after its own coverage/date/digest/numeric gates, records aggregate provenance/warning, and still cannot project incidents, regions, destinations or warehouse detail.

`warehouseId=0` is rendered as `Остальные — служебная группа WB` with an explanation that WB did not bind those aggregate balances to a concrete warehouse. It may participate in contour reconciliation under the rule above, but it is never a destination, acceptance-options probe, ranking candidate or recommendation.

`Остатки → Склад WB` retains the seller/account-level block only inside the default-collapsed `Legacy: инциденты на складах WB` disclosure. Opening the warehouse tab does not load it; its settings/options GETs run after explicit disclosure. Policy contract v2 stores append-only per-warehouse intervals keyed only by positive stable numeric warehouse ID. Every open entry has its own `effective_from`; an existing start date is immutable, while removal closes that interval at the revision change date and later re-selection creates a new interval. V1/global-date revisions are projected losslessly into entries. ID `0` remains a service bucket and is not an operational incident destination. The legacy readback includes bounded newest-first revisions plus every historical entry/identity, so exact names, IDs and dates survive an aggregate-only current snapshot.

Legacy checkbox/date changes remain browser-local draft only until the existing
explicit legacy Apply is used by a separately authorized operation. Migration
147 never calls that Apply, adds no policy runner and leaves every production
policy row and configured/effective value unchanged. Physical WB quantity, WAC
and capital are outside this write set. Supply and SKU Management keep reading
the same canonical policy for calculation compatibility, while their ordinary
screens no longer render incident selectors/badges/warnings.

The policy creates an availability projection only. The shared default projection used by Supply, SKU Management and every business-action contour excludes exact physical `quantity` from operational total/regions once upstream and requires complete pagination plus snapshot digest; incomplete evidence remains fail-closed. It never removes unattributed `inWayToClient`/`inWayFromClient`.

Web Vitrina retains the historical information-only incident adapter and its evidence as audit compatibility, but all `wb_stock_fact_qty*`, `wb_stock_incident_qty*` and `wb_stock_effective_qty*` rows/reasons/quality are filtered from ordinary catalog/read/UI. The migration does not rematerialize old quantities or restore missing loss evidence. The adapter is not imported by Supply or regional/SKU action calculations and cannot authorize a shipment, forecast, price/bid action or warehouse mutation. Neither mode changes this section's factual WB contour, raw snapshot, WAC, stage quantity/capital, `Всего единиц`, total capital, weighted cost or reconciliation. A product remains capital until a separate audited writeoff/compensation operation.

Periodic WB WAC получает accepted inbound capital, но quantity всегда заменяется official contour snapshot. Каждый hourly apply переигрывает versioned daily WAC от functional cutover: closed days фиксируются отдельными daily rows, current day остаётся provisional, zero-stock SKU retains last valid WAC. Если точная историческая колонка объявляет более поздний SKU с нулевым остатком, которого не было в frozen opening, projection сохраняет его как нулевой капитал со статусом `zero_quantity_without_cost_basis`, а Registry/Proxy и weekly Finance consumers возвращают неизвестную, не нулевую себестоимость; положительный остаток без cost seed остаётся блокирующей ошибкой. Late expense/accepted correction публикует signed event с исходной business date и атомарно перестраивает только derived daily cost history от этой границы; positive pool и cost не могут стать negative/zero. Direct consumers сначала читают эту daily projection, поэтому WB/FBO-контур единой `Себестоимость наша` не имеет независимого baseline.

Weekly Finance and Partner use one v2 resolver that is separate from physical
lifecycle. FBS primary is exact pooled active-facility FBS capital divided by
quantity as of the operation business date; only an absent primary may fall
back to the exact same-`nmId`, same-day published common WB+FF inventory cost.
The pooled value is never a facility mean or order/facility dependency, while
the lifecycle continues to own its immutable handoff WAC. WB/FBO continue
through the warehouse-domain daily resolver. Usually this is exact row
`sheet_vitrina_v1_warehouse_wb_daily_cost`; for the exact 18-SKU manifest
migration 109 allows active versioned `business_approved_archival_estimate`
100 ₽ effective 01.07. Missing required evidence is unknown and cannot inherit
another SKU/date, future value, legacy cost or zero.

Проекция до 01.07 — только Finance management metadata, не warehouse history. Она не создаёт backdated balances/events и не меняет functional rows. Отдельная фиксированная Finance value/map запрещена; позднее исправление canonical row меняет digest и заставляет derived Finance projection перестроиться. Acceptance/transit может быть исключён из Finance period expenses только через exact `supplyId + nmId` lineage к текущему cost layer и не выше капитализированной суммы.

# 3. Frozen historical boundary

Новая warehouse history начинается functional cutover timestamp; текущий snapshot не размножается назад. Старые warehouse values остаются audit/empty.

Ready snapshots с 01.07 очищаются от несогласованных legacy warehouse
stage/total cells. Для дня, где отсутствует полный доказанный шестиступенчатый
functional state, canonical warehouse cells остаются пустыми, а
`warehouse_history_coverage` и cell presentation показывают `Исторические
данные отсутствуют` с source-level причиной; это неизвестность, не доказанный
ноль. Дни с точной functional version публикуют quantity/capital и WAC из
version, выбранной по `snapshot_date`. До `2026-08-22` доказанные
`our_wb_unit_cost_rub` и Proxy 3 сохраняют frozen WB compatibility projection.
С этой даты current exact-day rows используют WB+FF inventory blend только при
полном WB/FF stage и FF facility/pool evidence; последующая business date не
переписывает более ранний ready snapshot.

Period-read объединяет не только значения exact-date snapshots, но и их `server_cell_presentation`; отсутствующая materialized business date получает явный `unavailable` reason вместо безымянного прочерка. Revalidation открытого дня использует SKU scope, замороженный в строках самого ready snapshot, а не более поздний current config: удалённая SKU не теряет warning, добавленная позже SKU не делает старый total частичным задним числом.

Каждая новая functional version связывает текущий coherent source capture только с official WB snapshot той же канонической бизнес-даты. Timestamp берётся после завершения локального read transaction, а apply повторяет boundary check до backup и под `BEGIN IMMEDIATE`; план, пересёкший локальную полночь, не публикуется. Historical consumer принимает exact-date version только когда её `effective_at` в business timezone совпадает с `snapshot_date`. В частности, emergency rebuild fail-closed отклоняет last-good snapshot прошлой даты: текущие supplier/FF/WB-supply данные нельзя выдать за exact historical state более раннего дня. Для pre-cutover gap UI объясняет отсутствие immutable исторического снимка, а для post-cutover gap — отсутствие точной successful functional version этой даты.

Отдельная разрешённая projection с `2026-07-01` покрывает `our_wb_unit_cost_rub`, Proxy 3 и direct consumers. Opening cost map строится из доказанно выбранной fully calculated FF acceptance около 24.06. Price band для отсутствующего в baseline SKU читается только из active server-side nomenclature `purchase_price_yuan` в coherent cutover capture; конфликтующие active prices одного `nmId` блокируют план. Значение и provenance копируются в frozen map, поэтому будущая правка справочника не меняет opening задним числом:

- direct SKU;
- weighted same purchase-price band;
- interpolation;
- extrapolation/single-band ratio;
- explicit fallback average при missing price.

Map frozen навсегда и сохраняет quality/provenance. Migration 109 не переписывает её: новый versioned overlay supersedes только WB WAC для exact owner-approved archival manifest, сохраняет исходный FF/opening proof и полный owner/source/fingerprint lineage. Exact target identity проверяется по `nmId + seller article + canonical nomenclature name`; отдельное человекочитаемое Finance-описание сохраняется в lineage и не используется как ключ номенклатуры. Apply не создаёт daily quantity; он сохраняет quantity уже существующей target row и пересчитывает только её derived capital. При zero stock 100 ₽ остаётся last valid basis, а первый будущий factual accepted quantity/capital layer запускает обычную moving WAC и меняет quality на periodic factual state. WB opening cost добавляет доказанные downstream costs, включая paid acceptance только для accepted quantity. Historical daily quantity переиспользуется только из persisted daily snapshot evidence. До первого functional cutover period-ready snapshot может закрыть отсутствующую canonical daily row только значением из колонки точной business date; canonical daily row имеет приоритет, а missing input не превращается в zero и не заменяется current/previous snapshot. Сам cutover сохраняет exact pre-cutover daily-cost projection как immutable versioned boundary. После cutover обычный hourly replay вообще не читает mutable ready snapshots для старых дат, сравнивает reviewed pre-cutover rows с замороженной projection и записывает только даты `>= cutover`; поздняя публикация snapshot с pre-cutover outer/date column не может переписать историю.

Единственное исключение — explicit emergency recovery отсутствующей целой business date. Только при фактической дыре frozen calendar его dry-run отдельно загружает и pin'ит normalized manifest выбранных exact `stock_total` columns вместе с digest; при полном calendar mutable snapshots вообще не входят в source digest обычного emergency rebuild. Correction принимает только exact `stock_total` column отсутствующей даты, в том числе из более позднего persisted bundle, если его outer `as_of_date` уже после cutover, но `date_columns` содержит эту exact старую дату; такой bundle не допускается в обычный hourly replay. Полнота SKU определяется union всех persisted candidate columns той же exact date: поздний bundle, целиком потерявший SKU scope, не может вытеснить более ранний полный bundle. Drift gate и manifest включают только реально потреблённые date/SKU/quantity и source identity, а не unrelated строки/metadata snapshot. Полный replay использует уже frozen quantities для overlap dates и exact snapshot quantities только для отсутствующих дат, поэтому сужение mutable snapshot window не блокирует корректировку; identity/quantity/WAC/capital каждой frozen строки обязаны арифметически совпасть. Затем plan содержит только отсутствующие rows, stable correction id, row fingerprints, provenance и точную связь `supersedes` с исходным cutover. Apply до backup и повторно под `BEGIN IMMEDIATE` заново выводит весь correction contract и exact rows из current persisted evidence и требует полного совпадения с reviewed plan; лишняя identity или уже заполненная дата блокируются. После этого сохраняется fresh coherent `0600` backup с `integrity_check=ok`, а backup незафиксированной попытки удаляется; missing identities вставляются через plain `INSERT` вместе с append-only audit row в одной transaction. `UPDATE`/`ON CONFLICT` для pre-cutover correction запрещены. Exact повтор — no-op, а последующий hourly replay читает исходные и corrected rows как единый frozen boundary.

Dry-run до публикации сравнивает projection с полным календарём `2026-07-01..candidate effective date` и fail-closed перечисляет любую отсутствующую business date; incomplete version не может стать active. Readback повторяет эту проверку, показывает correction audit и отдельно сообщает missing dates и positive-quantity cost gaps. Cost переигрывается через frozen map и confirmed post-01.07 inbound layers. Для positive quantity zero/NULL cost запрещён.

Targeted economics publication проверяет, что `DATA_VITRINA.header[2:]` точно совпадает с versioned `date_columns`, и изменяет только строки со стабильным projection key `scope|metric`. Сохранённые legacy presentation-only rows без такого ключа не участвуют в расчёте и остаются byte-for-byte неизменными; duplicate stable projection key или неоднозначный header блокируют весь dry-run с identity конкретного ready snapshot. Это compatibility read/write boundary, а не второй источник себестоимости.

Обычная functional-economics публикация на/после `2026-08-22` сначала
материализует exact-date product-capital lookup из той же functional version и
лишь затем строит единый `our_inventory_wac_wb_ff_v1` через
`build_inventory_cost_blend_lookup`. Per-SKU `Себестоимость наша`, Proxy 3 и
Proxy 4 используют ровно этот lookup; TOTAL cost использует только
`aggregate_inventory_cost_evidence`, а Proxy totals — суммы eligible SKU и
соответствующие revenue/quantity denominators. WB compatibility, exact
capital/location evidence, version/freshness/coverage и обе parameter versions
входят в один optimistic dependency fingerprint. Исторический boundary,
Finance/Partner realized COGS, capital stages и ledger/queue остаются
non-target; повторная публикация exact image является no-op.

Изменение только служебного marker/timestamp при `changed_cells=inserted_rows=archived_rows=0` не считается mutation: plan возвращает zero updates, apply является idempotent no-op и не создаёт многогигабайтный backup. Реальное изменение warehouse/economics cells по-прежнему требует exact fingerprint, coherent backup и atomic apply. Dry-run до и после расчёта, а apply повторно на том же writer connection сразу после bounded `BEGIN IMMEDIATE` сверяют один manifest functional versions/snapshots/balances, version-scoped supplier cost states, active functional identity, текущих supplier/CNY/financial source rows, daily WB costs, effective parameter versions, полный ready-snapshot manifest и exact target `plan_json` before-hashes. Каждая component digest сохраняется в плане и называется в structured drift evidence; material drift до первой записи остаётся fail-closed. DB-wide `PRAGMA data_version` служит только structured telemetry: commit FBS shadow observation/transition/poll/lifecycle в той же operational SQLite не входит в economics manifest и сам по себе не блокирует публикацию. Для active exact-date rows backfill повторяет source/calculation fingerprint revalidation и публикует жёлтый `source_changed_provisional`, пока targeted replay не выпустил новую согласованную версию.

Dry-run фиксирует одну canonical business date в fingerprint на всю операцию. Apply требует ту же дату перед fresh recheck, после backup, под write lock и непосредственно перед commit; переход бизнес-полуночи до commit откатывает транзакцию. Live/closed coverage поэтому не смешивает две даты внутри одного плана.

`warehouse_history_coverage` — семантическая часть ready snapshot, а не служебный marker. Изменение `live/closed/partial/unavailable`, причины, covered scope или exact `functional_version_id`, из которой опубликованы числовые cells, публикуется даже при неизменных значениях; повтор с тем же coverage остаётся no-op. Mutable-source fingerprint revalidation применяется только к текущей canonical business date и лишь если version binding ready snapshot совпадает с активной functional version. При раздельном успехе warehouse sync и сбое/задержке economics publication старое число помечается `unavailable` и скрывается до согласованной публикации, а не получает статус новой версии. Закрытая историческая дата остаётся привязана к своей exact-date immutable functional version и не меняет зелёный/жёлтый статус из-за более позднего документа. Production UI acceptance определяет применимость `our_wb_unit_cost_rub` по положительному exact-date official WB `stock_total`, независимо от warehouse-history projection и наличия продаж; поэтому неизвестная pre-cutover `own_capital_WB_qty` не может ложно сделать все WB-cost cells неприменимыми. Для Proxy 3 applicability отдельно использует `orderSum`.

# 4. Targeted replay and certification

Source change/archive/exclusion ставит одну coalesced queue revision по stable source id/source revision/earliest business date и affected SKU, затем coherent calculation публикует новую version atomically. Physical source rows не удаляются и quantity не двигается повторно. Failed calculation оставляет last good active version.

Inventory confirm/rollback and overhead confirm/reversal use the same exact targeted queue contract. Inventory ready means that the persisted source/date/full-nomenclature target intent is valid; confirm approves this absolute target, not the preview delta or the identity of the then-active global functional version. Confirm rereads current physical ledger, return and positive same-SKU cost inputs, recomputes the required actual delta and retries boundedly on concurrent ledger/publication/SQLite drift until one `BEGIN IMMEDIATE` transaction can prove the current target and non-target invariants. The volatile global active functional version remains manifest audit context only and cannot independently reject an inventory confirmation. The committed manifest records the successful attempt's actual before/delta/target and cost/readback evidence.

Primary inventory/overhead confirm atomically commits its append-only document and canonical queue row, returns the exact durable readback immediately and never holds interactive HTTP for functional/economics/Finance replay. Inventory inserts that queue row inside the same ledger transaction, closing the former commit-to-enqueue crash window. Idempotency is the exact inventory source/date/target intent, so double click, post-commit response loss, reload and exact retry cannot create another reconciliation, child or queue row or move the already-applied target again. Legacy stored ready inventory previews derive the same target intent from their persisted manifest after upgrade and do not require re-upload. The normal hourly/manual worker owns the continuation and persists separate Warehouse, economics and Finance completion/error fields; Finance completion pins its exact source/CAS fingerprint. Exact queue identity, affected `nmId`, earliest business date and source revision are pinned, and an overlapping unselected queue fails closed. Rollback/reversal retain their existing guarded compensating contracts.

Routine header-only factual-date correction is a distinct target-scoped publication. Query-only preview reads the exact shipment closure and compact active functional rows, never the Finance raw table or a disposable database copy. Apply and hourly/manual publication share `.warehouse-functional-sync.lock`; a bounded capacity check and stale target/non-target digests run before the single transaction. The successor version carries a coherent active WB snapshot/document projection, exact affected balance rows, queue/audit diagnostics and a target before-image rollback manifest. Unrelated global source anomalies and anomaly budgets remain diagnostics and cannot gate this local correction.

Top-level hourly/manual execution retains its official hourly capture cadence and
uses a separate `.warehouse-functional-job.lock` only for single-flight. It does
not hold `.warehouse-functional-sync.lock` across upstream capture, heavy
plan/digest, economics or Finance work. Canonical writers keep their own short
optimistic write/CAS sections; Finance builds target after-images from a
query-only projection and revalidates its exact canonical WB/FBS cost,
nomenclature and target raw/report dependency before `BEGIN IMMEDIATE`.
The writer boundary contains only the read-only observer's per-database
handoff token, an indexed target-before CAS, deterministic target replacement
and exact normalized readback. Unrelated UI/status commits during the long
projection remain admissible; an actual dependency, handoff or target change
still fails closed before replacement. Phase,
job-lock and writer-lock
timings are durable evidence. Interactive FF preview/confirm/status can commit
while the heavy job is planning, and source drift leaves last-good active for a
normal queued retry rather than blind restart.

`invoice_no` и `invoice_date` входят в этот source-change contract. Supplier-registry stage cell вычисляет frozen quality/certification по выбранным supplier flow records, а затем сверяет их текущие fingerprints; агрегатный mixed SKU balance не подменяет статус конкретной поставки.

Expense allocation and cost freshness are independent. Conservation/component arithmetic alone yields `Все расходы распределены`, `Расходы распределены частично`, `Расходы не распределены` or `Не требует распределения`; a fully allocated `9/9` with `0 ₽` unallocated stays fully allocated while replay is pending. Cost freshness separately yields preliminary, awaiting recalculation, recalculating, current certified, recalculation error, unavailable with blocker or not applicable. Unrelated global digest/refresh/publication cannot change either status because shipment fingerprints contain only semantically related cost-driving inputs.

The exact-cost tooltip follows that same durable state: a merely provisional value says that the active functional version is not yet certified and never claims that replay is waiting. `Ожидает пересчёта` is reserved for an actual `queued` or `running` targeted revision, so a completed replay cannot leave stale waiting text in the operator readback.

Canonical supplier allocation sorts invoice rows, CNY operations and financial expense lines by stable identities (including `line_id` as the tie-break for equal `sort_order`) before Decimal allocation and calculation fingerprinting. Detail read, version build, live vitrina and backfill therefore cannot disagree only because SQLite returned equal-sort rows in a different order.

The same canonical supplier allocation now publishes `document_controls` and `cost_affecting_document_types` as a bounded read proof for the supplier-order UI. Each control maps linked CNY operations back to their internal parent document when present, enumerates eligible/allocated component counts and amounts, conservation and explicit incomplete reasons; it does not perform a second allocation or change the established source/calculation fingerprint payload merely because presentation diagnostics were added. Informational Invoice/contract/packing-list/quote/control-statement documents remain outside the cost aggregate. Order-level green still requires every document control complete plus the existing exact active-version `expenses_complete` certification and matching source/calculation fingerprints.

Если первый counted supplier payment уже активировал полное quantity invoice, любая canonical-блокировка, включая отсутствие положительной RUB-оценки, останавливает публикацию functional version. Оплаченная поставка не может быть молча пропущена и тем самым исчезнуть из projection; только invoice без counted payment ожидаемо даёт zero warehouse quantity.

Emergency rebuild использует только persisted local sources, сначала возвращает dry-run/diff/fingerprint и требует explicit confirmation exact plan. External WB/Seller Portal API он не вызывает. Если recovery добавляет отсутствующие pre-cutover dates, mutable ready snapshot допускается только как pinned exact-column evidence внутри описанного выше correction gate; это не normal replay input и не разрешение менять существующую frozen строку.

# 5. Hourly WB operational sync

Repo-owned `wb-core-warehouse-functional-sync.timer` запускает bounded runner каждый час:

1. refresh official statuses/goods активных и recently completed WB supplies;
2. проверить complete active/recent status slices и enrichment; detail/goods transport, 429 and 5xx use bounded retries, while partial slice or retry-exhausted/persistent enrichment failure blocks the pipeline before any new FF debit/publication and returns bounded supply-specific diagnostics;
3. refresh normalised WB state без физического FF movement;
4. bounded-материализовать supply-specific downstream components без legacy daily/global rebuild;
5. reconcile exact supply revisions: создать/скорректировать reservation только при physical shortage/identity ambiguity либо атомарно выполнить полностью обеспеченный physical debit; missing cost never creates a reserve and instead marks the cost layer preliminary/unavailable;
6. fetch uncached complete official stock snapshot;
7. compute FF→WB, discrepancies, unmatched, WB snapshot and targeted/daily cost states из coherent capture;
8. publish one atomic good version. Unmatched audit identity включает owning functional `version_id`, поэтому одна и та же source evidence может безопасно присутствовать в последовательных versions без primary-key collision; повтор exact plan остаётся no-op.

`wb-core-sheet-vitrina-refresh.timer` больше не вызывает WB supply sync или Seller Portal automation. Global vitrina refresh только читает materialized warehouse/cost state. Manual WB refresh вызывает тот же bounded pipeline.

Для длительного reviewed Finance backfill используется repo-owned `warehouse-functional-maintenance status|hold|restore`. `status` фиксирует `is-enabled`/`is-active`, last/next timer trigger, service result, существование/занятость `.warehouse-functional-sync.lock` и отсутствие Finance apply. `hold` сохраняет mode-`0600` baseline/audit в canonical runtime, останавливает только functional timer (не отключая и не меняя его unit), не убивает уже запущенный service и bounded ждёт его штатного завершения и освобождения общего lock. Терминальный `failed` у oneshot-сервиса сохраняется как evidence (`Result`/`ExecMainStatus`), но считается quiescent наравне с `inactive`; running-переходы `active`/`activating`/`reloading`/`deactivating` остаются fail-closed ожиданием. `restore` разрешён только без Finance apply/warehouse writer и возвращает timer в exact исходные enabled/active состояния. Drift timer unit остаётся fail-closed. Если во время hold штатный deploy обновил service unit, restore принимает его только для quiescent service при свободном lock, неизменном timer digest, `NeedDaemonReload=no`, отсутствии drop-ins и точном byte-for-byte совпадении установленного fragment с repo-deployed systemd artifact; доказательство повторно проверяется после timer mutations, а старый/новый digest и пути сохраняются в audit. Любой недоказанный service drift блокирует restore. Canonical Finance apply держит тот же `.warehouse-functional-sync.lock` на всём интервале current plan → coherent backup → atomic apply → transactional readback; fingerprint/source validation не ослабляется. При drift после hold строится один новый стабильный plan и требуется новый exact human fingerprint.

# 6. Guarded functional cutover

`warehouse_opening_v1` и его шесть documents immutable и не меняются. Active cutover id — `warehouse_functional_cutover_v1`; timestamp берётся в production execution.

Canonical runner default dry-run получает coherent sources + uncached fresh WB snapshot, строит six-stage plan, frozen cost map, historical/daily WB cost projection, source watermarks/digests and invariants. Apply требует exact reviewed fingerprint, повторный uncached official snapshot, optimistic source recheck и совпадение semantic `calculation_digest` по costs/balances/events/documents/invariants, coherent SQLite backup `0600` with `integrity_check=ok`, one `BEGIN IMMEDIATE`, readback and idempotent second apply. Canonical business date сверяется до apply, после получения write lock и непосредственно перед commit; пересечение полуночи откатывает всю derived transaction. Каждый functional apply/rollback и archival-estimate apply/rollback входит в общий re-entrant `.warehouse-functional-sync.lock`, включая backup и transaction. Active archival version с exact row lineage входит в functional local-source digest, поэтому plan, рассчитанный до archival activation/rollback, после получения lock отклоняется и не может перезаписать correction. Archival dry-run держит одну explicit SQLite read transaction для rows и всех digests; daily provenance с factual inbound/accepted evidence или отличающимся WAC блокирует замену оценкой. Каждый target обязан иметь ровно одну nomenclature row: duplicate, identity conflict или factual purchase price блокирует estimate. Fresh post-apply archival plan имеет `status=no_op`, `apply_allowed=false` и при передаче runner остаётся inert readback без backup/новой version. Rollback сохраняет version/audit и делает использованный fingerprint необратимо non-reusable; parent functional rollback запрещён до archival rollback/deactivation. После factual acceptance archival row перестаёт быть допустимым сразу: до успешного daily replay resolver, archival readback и exact idempotent retry возвращают явный blocker, затем обычная WAC заменяет оценку. Targeted archival lookup сначала ограничивает active rows одним requested `nmId`, использует indexed factual-event range и для non-target не сканирует events. Current open business day сохраняет `periodic_snapshot_wac_provisional`, закрытые дни — `periodic_snapshot_wac_closed`. Shared backup API до открытия destination требует свободное место не меньше source size плюс bounded safety margin и при любой последующей ошибке удаляет только созданные этой попыткой partial destination/sidecars. Уже оставленный оборванной попыткой invalid backup удаляется только отдельным repo-owned dry-run/apply: exact path ограничен functional backup directory/name, stat/full SHA и invalid header/integrity входят в fingerprint, coherent SQLite/live DB fail closed, а `0600` cleanup manifest остаётся в audit. WB supply revision digest включает status, packed/accepted composition, raw goods and upstream business update, но исключает собственные `synced_at`/`last_list_synced_at`/`last_enriched_at`, чтобы повторный capture без business change не создавал ложный drift. Hourly/manual publication также pins `base_active_version_id`; concurrent stale plan отклоняется, а exact already-applied fingerprint остаётся idempotent. Initial Proxy settings version создаётся внутри той же transaction. Primary supplier/CNY/FF/WB records не изменяются.

FF replay сохраняет persisted chronology между различными timestamp. Если idempotent supplier receipt и зависимый WB outbound имеют одну и ту же секунду `created_at`, supplier receipt детерминированно применяется первым; случайный порядок `operation_id` не может создать ложный `no positive cost pool` blocker.

Guided China acceptance readiness evaluates the complete current active
functional `ff` revision against current-epoch pool detail on every preview.
`confirm_allowed=true` therefore pins the active revision, aggregate/detail
fingerprints and exact posting manifest. Confirm must reproduce the same proof
before recovery T1 and again under the immediate transaction lock; mismatch
blocks before any supplier date, legacy receipt, pool movement or projection
write.

Hourly timer включается только после successful cutover readback. Its oneshot uses `TimeoutStartSec=3h`: production-scale backup validation, source refresh, six-stage publication, reconciliation and lossless archive must complete under the shared lock instead of being killed at an intermediate post-publication stage. Rollback сначала disables timer, сохраняет backup и удаляет только functional derived state/initial settings when safe.

Supported production commands:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-dry-run --output /abs/plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-apply --plan-file /abs/plan.json --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-readback
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-backup
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-economics-dry-run --output /abs/economics-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-economics-apply --plan-file /abs/economics-plan.json --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-sync
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-sync-dry-run --output /abs/recovery-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-sync-apply --plan-file /abs/recovery-plan.json --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-maintenance status
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-maintenance hold
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-maintenance restore
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-cost-queue-replay-dry-run --invoice-no 26GN582 --invoice-no 26GN583 --output /abs/queue-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-cost-queue-replay-apply --invoice-no 26GN582 --invoice-no 26GN583 --plan-file /abs/queue-plan.json --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py sqlite-backup-archive-dry-run --source /opt/wb-core-runtime/state/backups/warehouse-functional-sync/exact.sqlite3 --reserved-free-bytes 4294967296 --output /abs/archive-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py sqlite-backup-archive-apply --source /opt/wb-core-runtime/state/backups/warehouse-functional-sync/exact.sqlite3 --reserved-free-bytes 4294967296 --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-enable-hourly
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-dry-run --output /abs/estimate-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-apply --plan-file /abs/estimate-plan.json --fingerprint 'sha256:...' --approval-reference '...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-readback
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-rollback --fingerprint 'sha256:...' --reason '...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-ui-flow --evidence-dir /abs/outside-repo
```

Exact supplier-cost queue replay is distinct from full current-source sync:
documents, expense lines and own-capital events must already reconcile, every
selected `supplier_costs` queue revision is pinned, and any unselected pending
queue that overlaps the target nmIDs fails closed. It copies no database,
publishes one targeted functional version and target-scoped economics under the
shared lock, and completes only reviewed queue identities. Completion proves
target quantities and non-target queue/warehouse digests unchanged and the
exact repeat is a no-op.

Hosted checkpoint archiving automatically uses the canonical runtime
filesystem as private staging when the backup directory is a separate mount.
The dry-run remains immutable/query-only and measures zstd bytes without
persisting them, so both the conservative staging envelope and the exact
destination archive-plus-reserve contour must pass before apply. Cross-device
apply uses an unnamed staged file, rechecks exact compressed size and
destination free space, publishes through a private fsynced destination temp,
and performs the same independent retained readback before raw removal; it
cannot leave a named staging artifact.

Lossless archive is a file lifecycle, not a warehouse publication. Its hosted
boundary accepts one raw checkpoint below the canonical
`state/backups/warehouse-functional-sync` directory. Immutable query-only
planning must prove capacity before temp bytes; apply finalizes a private
verified archive/manifest before removing the raw source and its unchanged
owned empty sidecars. Other backup scopes are never eligible through this
command.

# 7. UI and verification

Hosted backup-only, manual and reviewed sync-apply actions derive the restore-point directory from canonical runtime state as `state/backups/warehouse-functional-sync`; they must not redirect that checkpoint to the live-runtime filesystem sibling `/opt/wb-core-runtime/backups`.

Navigation is `Остатки → Склады и себестоимость / Отчёт об остатках`. One component renders quantity, WAC, capital, localized quality, sync status, SKU and document registry for all stages. Exact active nomenclature by `nm_id` enriches name/barcode; conflicting active identities remain visibly ambiguous and are never guessed. Every applicable row exposes a centralized Russian status plus human-readable evidence fields (document/date/invoice or supply/quantity source/cost source/confirmation/allocation/contribution). Source/stage date and per-source quality are persisted in provenance; a mixed SKU therefore shows the exact certified/provisional status of each contributing invoice, while a document line uses its known document occurrence as a bounded date fallback. For several FF→WB supplies each evidence row owns its exact open quantity and capital, and their sums equal the displayed SKU balance. FF evidence expands the cutover opening and every post-cutover append-only ledger operation with signed quantity/capital and operation date instead of duplicating an aggregate wrapper. Raw provenance JSON exists only in a nested technical disclosure.

The lazy FF `Реестр документов` is the single business read model over canonical FF quantity/reservation ledgers, inventory/overhead audit headers, legacy opening document and versioned functional technical records. It does not persist a second ledger. Business rows are newest business-date first and localize supplier receipt, WB shipment, inventory parent/children, return, manual receipt/writeoff, overhead, reservation lifecycle, opening and storno. Parent inventory has zero header movement, so linked children alone contribute quantity/capital. Canonical receipt/shipment/return rows expose a warehouse-neutral `warehouse-transfer:{source_type}:{source_object_id}` identity for future read projection on both ends without another movement; this pilot renders only FF. Technical cutover/sync/repair/archive is hidden until `Показать технические документы`.

Inventory UI has one business decision after a valid upload: yellow `Файл загружен и проверен — итоговый остаток <target>` and the explicit button `Провести инвентаризацию`. Internal retries are not exposed as operator actions. Durable commit with queued/running replay is partial `Документ проведён; пересчёт выполняется`; final green `Инвентаризация проведена: <actual before> → <target>` / `Остатки обновлены` is rendered only after exact replay completion and target readback. Reload recovers the same server preview/document identity, and neither page open nor status GET mutates business state.

FF document query applies `effect`, `reason`, inclusive business-date range, bounded search and `include_technical` before pagination over the whole server selection; `total_count`, `page_count` and `has_next` therefore describe the filtered result, not the first 25 already loaded rows. Search covers stable number, source object, supply/invoice and line `nmId`/SKU/barcode. Header shows total quantity/capital/expense and never derives a meaningless multi-SKU unit cost; per-line detail owns unit cost and created timestamp/actor/source/links. Projection uses a bounded fixed number of queries and lazy line detail, not N+1, and repeated functional sync may add technical audit versions but cannot clone the canonical business movement.

The six warehouse detail pages are read-only and contain no duplicated update/rebuild buttons. Every published version materializes a compact read model per warehouse. The initial endpoint returns only summary, current SKU balances, localized quality, lazy-document metadata and an ETag; it never embeds document lines or raw provenance. Documents are paginated separately, lines/provenance load only on expansion, SQL uses one grouped line-count query instead of N+1, and direct active-version lookup removes the former duplicate global readback. Browser cache is per every visited `(version_id, warehouse_key)`/ETag, not only the last tab. Initial payload budget is hundreds of KB and page open invokes no producer or external API.

Sibling tab `Обновление и пересчёт` separates durable `Автоматические обновления` from `Ручные обновления`. SQLite run/phase journal survives restart and exposes last attempt, last success, start/end/duration, next scheduled run, active version/business date/freshness, item counts, last-good and concise sanitized error for phases `WB supply registry`, `transit enrichment`, `FF ledger/reservations`, `official complete WB stocks`, `cost materialization`, `functional publication`, `dependent replay/economics`. Failure retains the last-good functional version and is visibly degraded. `Пересчитать все склады и себестоимости` starts the same canonical current-source pipeline behind a background server job; a parallel start returns `Уже выполняется другой пересчёт`. Page open performs no mutation. Synchronous long apply through the proxy is forbidden; reviewed bounded recovery uses `warehouse-functional-sync-dry-run` followed by exact-fingerprint `warehouse-functional-sync-apply`, which refreshes official supplies and current downstream layers, reconciles reservations/physical movements, takes a coherent verified restore point before production source mutation, rechecks semantic source/snapshot/diff/invariants under the shared lock and publishes only the new current functional version plus actually dependent economics. Neither page open nor global vitrina refresh launches a warehouse mutation.

The full-width WB incident card is nested inside the default-collapsed legacy disclosure. Its warehouse list remains a second internal disclosure with compact summary; the expanded grid is exactly 4 columns on desktop and 3/2/1 at narrower breakpoints. Warehouse tabs/cards/tables stay viewport-bounded. Current options sort by physical WB stock/name/ID, while identities retained only by policy history render read-only as legacy audit rows. The options GET reads the local immutable active-version model and never calls WB or mutates/publishes; it is not requested until the outer legacy disclosure opens. There is exactly one historical business Apply button inside that disclosure.

Document rows persist their own immutable SKU lines; discrepancy documents distinguish final-acceptance receipt, pooled `Допринято` and non-stock transitional audit. `wb_discrepancy_writeoff` is a reserved disabled type, not an automatic/manual action. WB adds four contour quantities; discrepancy detail adds transitional unmatched registry. The `Поставки → Реестр поставок` matrix exposes production/China stage cost fields and an aggregated `Комиссии банка, ₽` row derived from the same exact fee summary used by cost allocation. Settings exposes calculation parameters and three-week WB reference.

WB UI labels the headline quantity `Всего в контуре WB`, keeps `На складах WB` as a separate physical component and displays the exact formula with both in-way components. FF UI separately renders physical/reserved/available/unsecured quantities; reservation-only rows have zero physical capital and human-readable waiting status. Physical and reservation rows are frozen together in the same functional `version_id`, so a post-publication ledger change or failed sync cannot mix live reserve with last-good physical quantity. Legacy negative physical balance without a positive reservation remains a ledger warning and never fabricates `Ожидает поступления`. A certified balance never relies on an absent caption: centralized quality presentation always renders yellow provisional/mixed or green `Все расходы учтены / Подтверждено документами` text. Before green presentation, every supplier-origin balance revalidates the version-frozen certification against current source/calculation fingerprints and the exact active version. The Web Vitrina read contract repeats that revalidation for the active business-date cells before render and replaces persisted presentation metadata in memory; closed dates remain bound to their exact immutable versions. A changed source therefore immediately becomes yellow `Предварительная себестоимость — источники изменились`, so a queued or failed targeted replay cannot leave stale green state in either warehouse detail or Web Vitrina. Unprovable warehouse-history cells render compact `—` on the dark table surface with one accessible explanation instead of repeating a long light-background sentence. `warehouse-functional-backup` creates a coherent `0600` SQLite backup after a free-space check and reports `integrity_check=ok` plus SHA-256 without modifying business or derived rows. Hourly and operator-button sync share the same re-entrant cross-process lock with versioned calculation-parameter publication, independently reserve raw-plus-archive space on the backup mount and a bounded publication margin derived from the current coherent DB/WAL size on the live-runtime mount (or sum both requirements on one filesystem), and prepare the business day's coherent restore point before supplier/snapshot mutation. Its private raw provenance manifest pins date/path/size/SHA and is rechecked before reuse. The sync process applies a process-local 120-second SQLite busy wait to DB-backed runtime calls only, leaving web and feature-worker timeouts unchanged; a clamped operator override is available for production diagnostics. Machine-readable success returns per-phase timings and shared-lock wait, while exhausted DB contention records a bounded phase-specific reason and leaves the last-good version active. After success the raw copy is losslessly replaced by a verified `0600` zstd archive, and later runs recheck the actual compressed SHA, zstd frame and decompressed SHA/size before reuse. Privileged CLI manual sync and each operator-authored calculation-parameter save deliberately take a fresh restore point; the latter fsyncs its exact preview fingerprint into a private raw sidecar before the settings transaction. Daily, operator-settings and privileged manual-sync archives use bounded per-scope retention before their capacity gates and reserve one slot before a fresh incoming checkpoint: only older reverified archive/manifest pairs are removed, every retained candidate is verified first, newest restore points remain, and every removal is recorded through an atomically replaced durable `0600` intent/completion journal that accepts prior completed audit rows. Successful readback exposes the verified archive as primary backup evidence and explicitly marks its raw source removed. A capacity or archive-integrity failure therefore occurs before the first source/projection write, and an interrupted run keeps its raw rollback file. Production Playwright acceptance always writes a terminal machine-readable report, including a sanitized failure report when an assertion aborts the flow.

The active version itself is never retroactively edited to repair a missing legacy certification projection. `supplier-certification-dry-run/apply` pins the exact active version plan fingerprint and a coherent manifest of supplier/CNY/financial sources. A correction requires either exact supplier-flow fingerprints already embedded in immutable balance provenance or, for a legacy version without those fields, exact target-scoped conservation of every frozen per-SKU quantity/capital and the same payment, CNY-fee and China→FF document identities; every contributing mutable source row must also have a server-owned revision timestamp strictly earlier than the immutable version, with equality blocked as ambiguous at whole-second precision. Deterministic CNY ledger replay preserves `updated_at` for byte-semantic unchanged operations instead of manufacturing a later target revision. Nested supplier-flow provenance may be retained by a later FF/WB balance; its outer warehouse location is audit context and is not falsely compared with the original supplier allocation stage. Global source-group watermarks and the broader local digest remain audit evidence rather than target eligibility gates because unrelated informational documents and WB/FF/history materializations legitimately change them; the complete current supplier-source manifest is still plan-pinned and transactionally rechecked. The runner then appends only ordered version-scoped correction rows plus audit provenance after a `0600` integrity-checked backup and an in-transaction optimistic recheck. Missing frozen proof or any target allocation/revision change fails closed and requires a newly calculated functional version; a replay cannot turn stale WAC/capital green. Exact repeat is a no-op; the paired rollback appends a tombstone instead of deleting audit. Effective reads prefer the latest non-rolled-back correction for that exact version, while the next successful hourly version carries ordinary base states and naturally supersedes the recovery overlay.

Production UI status assertions wait for the visible warehouse label and timestamp/reason to equal the freshly read detail API payload. A still-rendering `Загрузка…` placeholder is not misclassified as a business mismatch, while a real divergence remains fail-closed.

Targeted verification:

- `python3 apps/inventory_planning_read_model_smoke.py`;
- `python3 apps/sheet_vitrina_v1_inventory_planning_smoke.py`;
- `python3 apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py`;
- `python3 apps/wb_incident_policy_legacy_readback_smoke.py`;
- `python3 apps/wb_incident_policy_legacy_ui_browser_smoke.py`;
- `python3 apps/warehouse_functional_smoke.py`;
- `python3 apps/warehouse_cost_queue_replay_smoke.py`;
- `python3 apps/sqlite_backup_archive_smoke.py`;
- `python3 apps/warehouse_archival_estimate_smoke.py`;
- `python3 apps/warehouse_supplier_cost_state_replay_smoke.py`;
- `python3 apps/ff_stock_reservation_smoke.py`;
- `python3 apps/ff_inventory_reconciliation_smoke.py`;
- `python3 apps/ff_overhead_allocation_smoke.py`;
- `python3 apps/ff_warehouse_documents_smoke.py`;
- `python3 apps/stocks_block_smoke.py`;
- `python3 apps/warehouse_stocks_smoke.py` (immutable legacy opening regression);
- `python3 apps/our_wb_costs_smoke.py`;
- `python3 apps/inventory_cost_blend_smoke.py` (ordinary publisher before/after, shared cost/Proxy 3/4 source and repeat no-op);
- `python3 apps/own_product_capital_smoke.py`;
- `python3 apps/canonical_cost_engine_smoke.py` (exact period-column selection; no current-value backfill);
- `python3 apps/cny_ledger_smoke.py`;
- `python3 apps/supplier_financial_documents_smoke.py`;
- production `warehouse-ui-flow` in a fresh Playwright/Chromium context, entering the shared-shell operator/settings/report frames only after their explicit `src` navigation; its reusable default verifies six-stage arithmetic, identities/evidence, bank-fee aggregate/detail, correctly parsed dark-theme contrast, canonical product-capital keys, date coverage, archived-metric absence and rendered consumers without pinning the mutable SKU catalog or specific shipments. WB-cost applicability is taken from the persisted exact-date WB contour quantity (physical plus both in-way components), never physical stock alone. The bounded migration-104 controls run only with `--acceptance-profile warehouse_chain_recovery_20260719`; the current exact-cost profile pins the 33-SKU WB snapshot, 26GN310/26GN390 payment, allocation, certification and Anti-Spy proof plus 17–18 July controls under `--acceptance-profile warehouse_cost_transparency_20260720`. Historically unavailable warehouse cells must render one compact dark-theme `—` with an accessible Russian reason. Evidence stays outside Git.

## Supplier cost freshness projection

Supplier stage cells are certified only from the current active functional version after revalidating both source and calculation fingerprints. A queued/running targeted request suppresses the frozen numeric value and renders `Ожидает пересчёта`; an error renders its exact blocker; any other fingerprint mismatch is stale and also suppresses the number. After successful replay the current canonical value is green. `production`/`china_to_ff` cells for a shipment that already reached FF are neutral `Не применяется: поставка уже на ФФ`.

The supplier exact-cost cell follows its canonical proof independently of the operator completeness flag. `certified` is green, `provisional` is yellow with warnings, and `unavailable` has no current number and exposes blockers. Thus neither equal-looking values nor `expenses_complete=true` establish functional-version freshness.

## Contention and targeted replay boundary

Warehouse writers use the shared bounded SQLite recovery contract, while the functional sync alone preserves its documented 120-second process-local wait. It does not widen web or feature-worker budgets. A failed commit leaves the immutable last-good functional version active.

Functional economics publication never performs full ready-snapshot/source
digest calculation while holding a SQLite write transaction. Apply first
rebuilds and compares the complete exact plan without a transaction while an
idle connection pins `PRAGMA data_version`. It checks the version again before
and immediately after a bounded `BEGIN IMMEDIATE`; only optimistic target-row
updates, exact readback and the undo manifest live inside that writer phase.
Any concurrent commit causes a fast fail-closed replan/retry instead of a
multi-minute rollback-journal reader blocking interactive FF
preview/status/confirm. This preserves atomic publication and last-good safety
in both WAL and rollback-journal modes. Contention telemetry measures writer
duration from an explicit `BEGIN IMMEDIATE/EXCLUSIVE` lock, or from the first
actual write for a deferred transaction, and aborts an unrecoverable
`SQLITE_BUSY_SNAPSHOT` immediately rather than retrying a stale snapshot.

FF inventory/overhead preview jobs persist accepted/processing/terminal state,
request aliases and per-stage latency events in the operational store. The
status surfaces remain bounded readbacks during concurrent background sync;
worker restart returns orphaned processing previews to accepted and resumes
the same deterministic identity without duplicating heavy planning.

A confirmed supplier bank-fee group changes only its exact shipment source revision. The existing targeted queue derives the shipment's exact matched SKU set and actually dependent warehouse stages/projections from canonical provenance. It never invokes Finance raw/history loading or a global/full-history rebuild. Publication still requires the existing immutable functional version, source/calculation fingerprints, conservation and certification gates; unrelated shipment/SKU/version digests remain invariant.

## Unified recovery policy (authoritative override)

Module 51 and migration 123 supersede every earlier recovery-volume statement
in this document. In particular:

- a true no-op is T0 and creates zero registry rows, files, reservations or
  recovery reads;
- targeted factual, cost, certification, archival, settings and economics
  publication is T1 with exact before/after images and targeted rollback;
- hourly/manual publication, emergency rebuild/rollback and opening
  publication are T2 domain checkpoints that exclude Finance raw;
- the initial `warehouse_functional_cutover_v1` remains T3 because it is an
  explicitly allowlisted schema cutover; runtime business commands cannot
  select T3.

`warehouse-functional-backup` is therefore a T2 domain-checkpoint command, not
a monolithic SQLite copy. Calculation-parameter saves no longer create or
archive a daily full-store restore point. Supplier certification, emergency
correction and functional economics no longer retain separate full backups.
Capacity, CAS lifecycle, retention, artifact identity, orphan/quarantine and
rollback status are read through the central registry and rendered on
`Обновление и пересчёт`.

## Bounded business-time rematerializer

The functional warehouse remains the sole calculator for the six-stage
warehouse/cost truth. Every source revision carries a stable source identity,
revision, business-effective date and affected SKU closure. The durable
projection outbox coalesces repeats/concurrent requests by earliest affected
date and SKU closure. Supplier/CNY/financial costs, factual supplier
boundaries, FF ledger operations, fulfillment costs, WB supply transitions,
official WB/WAC and audited event compensation enter this contour through
their existing canonical source/event or targeted-queue transaction.

The rematerializer reads no external producer and never invokes a full Vitrina
refresh. It publishes only public warehouse/product-capital SKU/TOTAL metrics,
exact coverage/presentation/provenance and affected dates. Each successful
hourly/emergency/targeted functional version is now the authoritative complete
producer for its exact business date and publishes those rows atomically inside
the functional transaction. A partial `functional_*`/`ff_stock_*` outbox request
without complete event proof is consumed as a replay signal while preserving
the last-good projection; it cannot replace exact rows with stale capital,
preserved quantities or `missing_exact_projection_date` placeholders.

If an exact daily base/event projection is missing, only a complete canonical
event revision may mark owned metric keys unavailable/provisional. A partial
functional replay signal leaves the last-good rows active. Other ready-snapshot
sources are neither fabricated nor copied from yesterday. Candidate publication
is atomic; conservation, cost-only quantity invariance, non-target digests and
source revision are checked before current-state switch.

`warehouse-july-recovery --batch projection` is the bounded repair path for an
already mixed post-inventory window. Dry-run is query-only and pins the applied
inventory source SHA/business date, exact per-date functional version and FF
watermark prefix, frozen document quantity/capital, current projection digest,
active physical/function non-target digest and target row fingerprints. Apply
requires its exact fingerprint and approval reference, changes projection
tables only under `sku_date` T1 before-images, leaves the active functional
pointer/ledger/balances unchanged, and exact repeat is T0. Rollback restores
only those projection rows/state.

## July 2026 bounded historical recovery

Migration 127 owns the one-off repo runner. Batch A publishes only exact
`2026-07-19..29` functional six-stage and owned product-capital rows, with
`2026-07-30+` and the active functional pointer hard non-targets. Batch B
publishes only persisted exact WB quantity/WAC/capital for `2026-07-01..18`;
other stages and all-stage totals remain unavailable with server provenance,
never copied from an adjacent/current snapshot or replaced by zero. Both
batches are external-manifest, exact-fingerprint, T1 rollback and T0-repeat
operations, and Batch B cannot run before retained Batch A reconciliation.

## Stage 7A FBS query-only lifecycle shadow

Migration 139 extends the default-off FBS cache with append-only official
status observations. `POST /api/v3/orders/status` is used only as a read
semantic and stores exact order revision, status digest, observed time and
positive quantity. Separate immutable contracts cover seller warehouse → FF
facility and exact nmId/chrtId/barcode/SKU mapping; unmatched evidence stays
isolated and visible. Settings reports cursor, last error, status count and
unmatched counts. Review begins at `2026-08-01`, while earliest official order
date is calculated from the observations. Collector/backfill, reservations,
debit, movements, balances, routing and returns stay off. In particular,
`supplierStatus=complete` never triggers physical stock.

Migration 140 separately turns on only collection and exact shadow mapping.
Its owner-gated runner creates active `FF Москва` and inactive `FF Оренбург`,
catches up `2026-08-01..watermark` and proves a next ordinary polling run. Its
original activation reused the hourly path; current polling is superseded
below. Exact
seller warehouse → official office IDs may map Moscow; Orenburg remains
unrouted. SKU mappings require exact `nmId/chrtId/barcode/SKU`; all uncertain
rows remain isolated. Aggregate FF quantity/capital, writer epoch,
opening/cutover, documents, reservations, debit, movements, returns and WB
writes remain invariant, so the six-stage active warehouse truth is unchanged.
Migration 141 supersedes only the polling schedule: a dedicated five-minute
single-flight read-only service replaces the FBS hook in this hourly writer.
It appends exact status-pair transitions and query diagnostics but cannot feed
the active six-stage warehouse projection.  `wbStatus=sorted` is a candidate,
not an enabled trigger; the query-only readiness remains `NO_GO` without
repeatable same-order transition evidence and a later owner-gated design.

## Closed-date exact-functional invariant (WBC0027)

After functional cutover, an outbox/event row is only a replay signal. It must
never publish product-capital cells from `own_capital_daily_state` or preserve
quantity from mutable projection `current_rows`. The only ordinary writer is
the exact good functional version whose `business_effective_date` and official
`snapshot_date` both equal the published date. Row provenance binds
`functional_version_id`, `snapshot_date`, source digest and publication
identity. Pending signals stay durably `pending_exact_functional` until that
same transaction moves them to `published_exact`.

The owning exact-functional/ready-snapshot publication transaction exhaustively
reconciles all 42 owned product-capital keys for every persisted date/scope by
bounded set/bulk reads and stores one compact revision/count/digest health row.
Any unbound row, numeric-to-missing transition, cell mismatch, cost-only
quantity drift or closed-date rewrite without a new exact version is
`historical_repair_required`; enqueue alone can never make health green.
Projection/ready-binding triggers only invalidate that durable materialization;
the next owning publication recomputes it exactly. The synchronous status GET
is query-only and reads the compact row with a fixed query count. A missing or
invalidated materialization is never green and never replays history on GET.

WBC0027 JIT recovery owns a closed product slice: dates 13, 14, 16 and 17–29
August, exactly 1,152 rows / 24,192 owned cells / 9,446 mismatches. The
15-August absence of an exact same-date functional version remains
`EVIDENCE_BLOCKED`; 30 August and later are hard non-target. SKU `497413772`
on 21 August contributes exactly 16 separately qualified cells. Apply rebuilds
the candidate beneath the shared writer lock, checks every target row's exact
before image and the ready pointer, then publishes once through T1. Events and
outbox are audit-only and are not acknowledged or rewritten by this recovery.
Its query-only deployed qualification runs two no-create semantic witnesses,
then exercises the same real non-blocking shared-lock candidate rebuild and
stops before T1. A missing recovery lifecycle, zero submit, zero database write
and release of the lock are required; the qualification never creates or
reuses a private phase plan.

Economics uses canonical semantic non-target contract
`wbc0027_economics_semantic_non_target_digest/v1`: all 224 ready snapshots are
covered, while the exact three reviewed target slices are normalized away.
Row counts and identity/payload/row component digests are retained at plan,
writer-lock T1, post-write, retain and readback. Ordinary publication may be
rebased only before submit; a real concurrent non-target mutation is blocked
and target CAS remains exact.

The 31-August source Apply committed three economics rows / 472 cells, then the
legacy 221-row raw digest was compared with the 224-row semantic digest and
caused a false quarantine. Post-COMMIT truth is now recorded in the business
transaction itself, so later exceptions report one written submit as
`applied_pending_reconciliation`. The bounded `finalize-only` path for that
same operation is strictly query-only and mutation-incapable: it validates the
immutable source receipt and T1 journal, proves product 1,152/24,192/mismatch0,
economics 12/0 and hard non-target exactness, preserves the quarantined row,
and never replays product, economics, events, outbox, timers or services.
