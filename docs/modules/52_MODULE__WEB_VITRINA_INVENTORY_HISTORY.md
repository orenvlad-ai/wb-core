---
title: "Module 52 — Web Vitrina Inventory History"
doc_id: "WB-CORE-MODULE-52-WEB-VITRINA-INVENTORY-HISTORY"
doc_type: "module"
status: "active"
purpose: "Хранить и показывать бессрочную server-owned историю WB и FBS остатков по TOTAL и каждому SKU без дублирования rendered Витрины."
scope: "Compact typed component revisions, closed-day finalizations, date-aware main Web Vitrina projection and owner-gated historical backfill."
source_basis:
  - "packages/application/sheet_vitrina_v1_inventory_history.py"
  - "packages/application/inventory_planning_read_model.py"
  - "packages/application/sheet_vitrina_v1_inventory_planning.py"
  - "apps/sheet_vitrina_v1_inventory_history_backfill.py"
  - "migration/156_web_vitrina_inventory_history.md"
  - "migration/161_applicability_gated_dense_fbs.md"
related_modules:
  - "29_MODULE__WEB_VITRINA_VIEW_MODEL_BLOCK.md"
  - "43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
  - "48_MODULE__WAREHOUSE_STOCKS_BLOCK.md"
related_tables:
  - "sheet_vitrina_v1_inventory_history_captures"
  - "sheet_vitrina_v1_inventory_history_components"
  - "sheet_vitrina_v1_inventory_history_finalizations"
  - "sheet_vitrina_v1_inventory_history_applies"
related_endpoints:
  - "/v1/sheet-vitrina-v1/web-vitrina"
related_runners:
  - "apps/sheet_vitrina_v1_inventory_history_backfill.py"
related_docs:
  - "migration/145_inventory_planning_and_fbs_orders_surfaces.md"
  - "migration/146_inventory_planning_main_vitrina_rows.md"
  - "migration/156_web_vitrina_inventory_history.md"
  - "migration/161_applicability_gated_dense_fbs.md"
source_of_truth_level: "canonical_module_contract"
update_note: "Compact typed component history remains immutable; Migration 161 publishes current FBS exact/exact_zero/missing/inapplicable reason/provenance from dense applicability without retrocopying current zero into history."
---

# 1. Public metric contract

Основная Web Витрина использует существующие canonical identities, а не новый
дублирующий общий показатель. Публичный порядок для TOTAL и каждого SKU:

1. `stock_total` / `total_stock_total` — `Остатки общие`;
2. canonical WB planning key — `Остатки WB`;
3. `Остатки FBS Москва`;
4. `Остатки FBS Оренбург`;
5. следующие facility keys — `Остатки FBS <facility name>` в сохранённом
   пользовательском порядке.

Source names могут уже содержать технический префикс `FF`/`FBS`; presentation
нормализует ровно один такой начальный префикс, поэтому он не дублируется в
публичной подписи. Logical metric key и исходное server-owned facility name
при этом не меняются.

`Остатки общие = Остатки WB + available всех applicable active FBS facilities`.
WB остаётся одним typed operand со своей действующей reserve semantics. FBS
available остаётся exact `physical - active reserved`. FBO, aggregate FF,
transit и seller-stock reconciliation не являются operands этой формулы.
Internal warehouse audit routes могут сохранять aggregate FBS diagnostics, но
основная таблица не показывает отдельную дублирующую FBS-total строку.

Новая facility входит в общий результат и metric catalog автоматически.
Отдельная строка становится видимой конкретному пользователю только после её
явного включения; существующий server/browser presentation state сохраняет
пользовательский порядок и повторно не включает скрытую строку.

# 2. Typed component history

Одна immutable capture revision хранит только компоненты
`business_date × TOTAL/SKU × WB/FBS facility`. Компонент имеет один state:

- `exact` — доказанное ненулевое целое значение;
- `exact_zero` — отдельно доказанный ноль;
- `missing` — applicable operand без exact evidence;
- `inapplicable` — facility ещё не применима к дате/scope.

Capture сохраняет formula version, ready/source revision identities,
source digests/watermarks, provenance, capture time, facility roster revision и
effective dates. Таблицы защищены no-update/no-delete triggers: retention
бессрочная. Full rendered Web Vitrina JSON на refresh не копируется.

`missing row != zero`. История не интерполируется, не экстраполируется, не
восстанавливается из продаж/заказов и не получает current balance задним
числом. Архивный SKU остаётся в component history, если он присутствует в
доказанном source revision; его отдельная UI-видимость не входит в этот модуль.
Поэтому исторические capture/date counts, `missing / NULL`, exact-zero lineage и
semantic equality между presentation revisions являются audit-only для
current-state forward-zero cutover: они не разрешают и не запрещают будущий
`T0`, а сама операция не пишет ни в одну history table.

# 3. Time and quality semantics

Current business day читает latest accepted capture по durable insertion sequence. Closed day читает latest
append-only finalization общего `yesterday_closed` contract. Перед финализацией
accepted ready evidence для exact закрываемой даты materialize-ится отдельной
immutable capture: WB берётся из колонки этой даты, а FBS — только из прежней
same-date capture. Current FBS balance никогда не переносится в закрытую дату.
Если same-date capture отсутствует, writer fail closed и не синтезирует
historical FBS. Поздняя принятая revision создаёт новую capture и superseding
finalization; предыдущие capture, finalization и provenance не изменяются и не
удаляются. Повтор той же source revision идемпотентен даже при новом writer
timestamp; новый доказанный ready snapshot/source revision создаёт append-only
supersession. Stock-specific cutoff, таймер или отдельная кнопка не вводятся.

Для current state после Migration 161 facility × SKU становится applicable с
future proven dense activation `T0`: active facility и active/non-hidden
positive-`nmId` SKU применимы по умолчанию, кроме явного датированного
`inapplicable`. До T0 и для inactive facility/SKU текущая пара `inapplicable`;
её сохранённая история и физический row не удаляются. Archive/reactivation не
переносит balance назад и не сбрасывает его. Historical applicability остаётся
только из independently persisted same-date evidence; current default или zero
никогда не ретрокопируется в старую дату.

Если все current applicable operands exact, публикуется numeric `full`. Если
хотя бы один applicable component `missing`, facility/SKU aggregate и общий
FBS operand недоступны с exact missing list; известная под-сумма не выдаётся за
полный текущий остаток. В строке missing показывается `—`, доказанный
`exact_zero` показывается `0`, а `inapplicable` не является operand. Browser не
вычисляет business truth и не вводит отдельный health/UI contour.

# 4. Capture and read path

`RegistryUploadDbBackedRuntime.save_sheet_vitrina_ready_snapshot(...)` атомарно
сохраняет ready snapshot и вызывает единый history capture path. Поэтому
manual `Загрузить`, automatic refresh и group refresh используют один writer;
отдельного history schedule/button нет. Read-side выбирает bounded date window,
читает latest finalized/captured revision из canonical SQLite в
`mode=ro + query_only` и передаёт уже materialized values/quality в существующую
Web Vitrina row surface. Полный ledger на page request не replay-ится.

Current-day WB capture использует active WB snapshot operand из того же
`InventoryPlanningReadModel`, который публикует текущую Web Витрину. Если
active snapshot не относится к current business date, применяется accepted
current ready column, то есть тот же fallback, который остаётся видимым без
current planning overlay. Source revision/digest/watermark сохраняются на WB
component; accepted column и active snapshot не смешиваются для одного
публичного scope.

# 5. Historical backfill boundary

`apps/sheet_vitrina_v1_inventory_history_backfill.py` — versioned repo-owned
runner. По умолчанию он выполняет только query-only dry-run и пишет private
machine-readable manifest вне репозитория. Legacy historical `stock_total`
сохраняется как исходный WB-only fact; unified `Остатки общие` materialize-ится
отдельно. Facility components появляются только со своей exact applicability
date. Не доказанный промежуток остаётся `partial`/`unavailable`, current balance
назад не переносится.

Manifest фиксирует exact deployed SHA/schema/generation, formula/finalization
identity, cutoff, source revisions/digests/watermarks, roster/effective dates,
target dates/SKU/components, typed before и proposed values/quality/missing
lists, ожидаемые row counts, source gaps и non-target/recovery contract.
Dry-run проверяет byte-identical canonical DB before/after.
Canonical DB выбирается только через validated `StoreRegistry` generation
manifest: retained legacy monolith не является fallback после split cutover.
CAS связывает exact operational generation identity и digest required
source/history schema; отсутствующая deployed history schema блокирует dry-run
до публикации manifest.

Source CAS использует contract
`sheet_vitrina_v1_inventory_history_backfill_source_cas_v4` и ограничен exact
`date_from..date_to`. Для WB в него входят только выбранные ready revisions
целевых дат; ready snapshot с `as_of_date > date_to` не является историческим
источником. Для FBS один и тот же reconstruction contour одновременно фиксирует
relevant facility roster/mappings, opening allocations и движения/резервы не
позже `date_to`. Глобальные table counts и доказанно post-cutoff строки не входят
в CAS. Late revision выбранного target-date WB source либо любое изменение
relevant roster/mapping/FBS material до cutoff меняет digest и блокирует stale
apply.

Каждая выбранная WB revision нормализуется по contract
`sheet_vitrina_v1_inventory_ready_evidence_v2`. Apply-blocking material identity
включает stable `bundle_version`, `activated_at`, snapshot as-of,
`snapshot_id`, `plan_version`, stable suffix selection rank без его volatile timestamp element,
exact business date и `DATA_VITRINA` header/date-column/key schema, а также
отсортированный typed set только `TOTAL/SKU stock_total` (`exact`,
`exact_zero`, `missing`). Его `inventory_evidence_digest` является WB component
source digest и apply CAS. `refreshed_at`, полный selection rank и
`observed_plan_digest` полного multi-metric plan сохраняются отдельно как
immutable audit provenance с ролью `audit_only_not_apply_cas`; они не входят в
capture identity/source watermark. Поэтому обычный refresh того же stable
snapshot/revision и изменение non-inventory metric не stale-ят qualified
manifest. Replacement stable source identity/rank suffix, header/date/key/scope,
schema/generation/target-history либо любое target `stock_total` value/state
обязательно меняют material qualification digest и fail closed.

Legacy exact-manifest apply сохраняет отдельный owner gate. Для уже accepted
bounded reversible task используется durable OWNER/MEMBER scope-goal passport
без manifest hash: trusted-main Runner сам выводит exact deployed merge SHA,
JIT создаёт private immutable manifests на canonical host и требует два
consecutive полных material-CAS совпадения. При pre-submit material drift он
boundedly регенерирует candidate до трёх раз; это не mutation retry и не требует
нового user confirmation. Только последний qualified candidate может один раз
вызвать mutation. Apply всё равно требует deployed SHA/schema/generation/
source/target CAS, canonical writer lock и coherent verified target-scoped
`0600` before-image. Full-store/T3 backup для этой bounded append-only mutation
запрещён. Write allowlist ограничен четырьмя history tables; операция
append/supersede-only. Manifest hash остаётся DB idempotency key. После
единственного submit, включая ambiguous transport, выполняется только отдельный
query-only readback через exact applies/finalization и visible-history
reconciliation; blind replay запрещён.

# 6. Verification

- `apps/inventory_planning_read_model_smoke.py` — physical/reserved operands,
  applicable-missing fail-closed aggregate, inactive current exclusion with
  retained history and seller-readback exclusion;
- `apps/sheet_vitrina_v1_inventory_planning_smoke.py` — public identities,
  order, TOTAL/SKU and no duplicate aggregate;
- `apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py` — user visibility,
  inactive-facility current exclusion and full-value marker absence;
- `apps/sheet_vitrina_v1_inventory_history_smoke.py` — idempotent capture,
  exact zero/missing, archived SKU, WB-only prelaunch full, late supersession,
  late exact closed-day WB with same-date-only FBS, numeric partial,
  `50 162 + 82 900 + 26 697 = 159 759`, current-FBS retrocopy exclusion,
  current UI WB equality, indefinite retention and 174-day × 34-scope
  realistic window;
- `apps/ff_pool_dense_fbs_smoke.py` — current typed state/reason/provenance,
  future-T0 explicit zero, fail-closed non-zero/reservation/order SKU retirement,
  zero archive/reactivation retention, exact-id transport resume, canonical EKT
  date boundary, compact new-SKU coverage and no historical retrocopy;
- `apps/sheet_vitrina_v1_inventory_history_backfill_smoke.py` — Moscow/Orenburg
  applicability/exact boundaries, full/partial partitions, dry-run byte safety,
  target-scoped source CAS (post-cutoff tick и same-plan non-inventory drift
  stability; real `refreshed_at`/rank[0] v3 false-drift regression; stable
  selected identity/rank suffix/schema/scope/value/state и target-date FBS
  drift invalidation), guarded apply, query-only visible-history reconciliation
  and idempotent replay;
- `apps/production_apply_runner_smoke.py` — scope-goal passport, exact Release
  receipt binding, consecutive qualification, bounded regeneration, one-submit
  boundary, ambiguous-transport query-only reconciliation and immutable
  done-receipt recovery without any production command.
