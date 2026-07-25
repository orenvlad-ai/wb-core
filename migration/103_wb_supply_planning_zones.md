# 103. WB supply planning zones and storage-only recommendations

`Поставки -> Расчёты -> Поставка на Wildberries` separates the canonical
federal district `central` from the operational units used to calculate and
route a supply.  The calculation and warehouse picker now expose `ЦФО Север`,
`ЦФО Восток` and `ЦФО Юг`; there is intentionally no `ЦФО Запад` until an
explicit full-storage warehouse is approved for that direction.

## Contracts and compatibility

- Canonical federal-district keys remain unchanged for reports, generic WB
  supply mapping and integrations that still need `central`.
- Supply calculation payloads use `payload_version=v2_planning_zones` and the
  separate `SUPPLY_PLANNING_ZONE_KEYS` contract.  The five non-Central
  directions retain their existing keys; only Central is expanded to
  `central_north`, `central_east` and `central_south`.
- A persisted v1 result is preserved and disclosed as a bounded
  `migration_status.legacy_snapshot`.  It is not guessed into thirds: the UI
  requests a recalculation from current authoritative stock/history.  A
  successful v2 calculation atomically replaces the single-slot result.
- Existing v1 JSON therefore remains the rollback evidence until a successful
  recalculation.  Code rollback reads the unchanged legacy payload; data
  recovery is a normal recalculation, not a destructive backfill.

No SQL schema migration or production data mutation is required.  Clean and
existing runtime databases use the same JSON state tables and lazy read path.

## Central storage registry

`packages/contracts/wb_supply_planning_zones.py` is the one typed registry for
Central planning warehouses.  Identity is `warehouse_id` first.  Exact
normalized canonical names and explicit aliases are allowed only when a
historical source genuinely has no ID; substring, prefix and fuzzy identity
matching are forbidden.

| Zone | Role | Warehouse | warehouseID | New recommendations | History |
|---|---|---|---:|---|---|
| `central_north` | primary | Тверь | 301806 | yes | yes |
| `central_east` | primary | Владимир Воршинское | 301981 | yes | yes |
| `central_east` | reserve | Рязань (Тюшевское) | 301760 | yes | yes |
| `central_east` | reserve | Электросталь | 120762 | blocked | yes |
| `central_east` | reserve | Котовск | 301809 | blocked | yes |
| `central_south` | primary | Коледино | 507 | yes | yes |
| `central_south` | reserve | Тула | 206348 | yes | yes |
| `central_south` | far reserve | Воронеж | 301808 | yes | yes |

To add or reactivate a warehouse, change exactly this registry, bump
`WAREHOUSE_REGISTRY_VERSION`, supply the exact official `warehouseID`, role and
explicit recommendation/history flags, update contract fixtures and pass the
registry/filter/date/UI smoke suites.  A newly returned WB warehouse remains
`warehouse_unclassified` and hidden until that review is complete.

Sorting centres are intake/transit points, not destination stock.  The manager
view excludes exact catalog sorting-centre evidence, canonical `СЦ` names,
known excluded IDs, `СГТ`, and specialised `Питание`, `Горючее`, `Шины`
warehouses.  Thus `Тверь != СЦ Тверь`, `Электросталь != Электросталь: Питание`
and `Коледино != Коледино: Горючее`.

## SKU-dependent availability and dates

The registry classifies a warehouse; it never grants supply availability.
Each manager option must also be present in the read-only official
`POST /api/v1/acceptance/options` answer for every requested barcode and
quantity, support the requested `box` package (`canBox=true`), be active in
`GET /api/v1/warehouses`, be a direct full-storage destination, be enabled in
the registry and not match an exact exclusion.  A missing expected registry
warehouse can receive a bounded warehouseID-specific read-only probe; a
same-named specialised warehouse does not suppress that probe.

For `package_type=box`, the official coefficient contract has two box rows:
`boxTypeID=1` (`Короба`) and `boxTypeID=2` (`Короба, сверхгабаритный товар`).
Both are retained as box evidence; other types are never mixed into the list.
Within the official 14-day horizon the normalizer:

- admits a day only when `allowUnload=true` and `coefficient` is `0` or `1`;
- deduplicates by calendar date after package-type filtering;
- sorts chronologically and computes separate first available/free dates and
  unique available/free day counts;
- keeps the bounded normalized `dates[]` evidence including coefficient,
  availability, free/paid status, `allowUnload` and box type IDs;
- never mixes transit dates into a direct destination recommendation.

The backend returns ready-to-render reason codes, ranking evidence and
diagnostics.  The UI does not recreate the eligibility rules from Russian text.

## Stock, history and deficit allocation

Current `stocks-report/wb-warehouses` rows retain `warehouseId`,
`warehouseName`, region and quantity through normalization.  Known Central
storage IDs aggregate into the three planning zones.  Historical rows without
IDs use only the registry's exact canonical names/aliases, so blocked
Электросталь and Котовск remain East history while never becoming new
recommendations.  Unknown or excluded rows are not guessed.

Every stock read exposes reconciliation:

`legacy Central total = North + East + South + unmapped + excluded + difference`.

The expected `difference` is zero.  Non-zero, unmapped or exclusion growth is
an operator diagnostic and a registry review signal.

Demand history, current zone stock and selected in-transit WB supplies are
calculated per planning zone.  The target shortage is rounded by box multiple,
then finite FF stock is assigned deterministically by marginal saved units,
coverage and demand.  A zone with no demand receives no demand allocation;
total allocation never exceeds FF availability.  Full recommendation,
allocated quantity, unfulfilled deficit, target stock, current stock,
in-transit quantity, average demand, source/confidence and allocation reason
remain visible in the result contract.  Warehouse/date planning is read-only
and never reallocates that saved calculation when a destination is blocked.

## Operator surface and observability

The warehouse picker is a single full-width card below the two-column
`Параметры расчёта` / `Результат` area.  Its wide table scrolls inside the card
on desktop and narrow viewports.  Opening it from a result row selects one
planning zone and scrolls to that card.  The ordinary manager view contains
only eligible storage warehouses in that direction; raw and excluded evidence
is diagnostic-only.

Sanitized diagnostics include WB request ID, requested SKU/barcode counts,
raw/grouped/visible counts, exact exclusion counts by reason, registry version,
classification source and Central stock reconciliation.  API tokens, full
barcode lists and other secrets are never logged.

## Recovery

Расчёт ЦФО выполняется в два уровня: общая потребность сохраняет legacy-историю
`central`, затем распределяется только между выбранными Севером, Востоком и
Югом. При cold-start доли равны, а переход к направленной истории идёт по
confidence достоверных наблюдений; отсутствие наблюдений не является нулевым
спросом.

Historical boolean `exclude_elektrostal_stock=true` читается только как
compatibility evidence для `warehouseID=120762`. Current payload использует
общий multi-select `excluded_wb_warehouse_ids` с stable numeric identity и
хранит его в одном server-owned user config `wb_warehouse_exclusions`.
Factory-order, WB regional calculations, read-only `Подобрать склады WB` и
SKU-management forecast читают тот же record: backend исключает эти IDs до ranking,
warehouse-specific probes и operator handoff. Если после исключений не осталось
допустимых вариантов, API возвращает controlled
`no_eligible_storage_warehouse_after_exclusions`, а не исключённый склад.
Selector
показывает только склады с ненулевым physical/in-way contour в свежем complete
official snapshot, но исключает из формул только physical stock, уже входивший
в соответствующую действующую формулу. Selected ID сохраняется, если склад
стал нулевым или временно исчез; удалить его может только пользователь.
Selector сортирует присутствующие склады по `total_contour desc`, затем
русскому названию и ID, а missing-selected выводит внизу.
Result pins IDs, snapshot date/fingerprint and actual/excluded/effective
reconciliation by `warehouseID + nmID`. WB regional exclusion happens before
planning-zone aggregation and never removes demand history or changes the
destination registry. SKU-management additionally subtracts warehouse quantity
from both total WB opening and mapped federal-district opening; incomplete
warehouse evidence with a non-empty list fails closed instead of yielding zeros.

`warehouseID=0` отображается как `Остальные — служебная группа WB`: это
агрегированные остатки без привязки WB к конкретному складу. Группа может
участвовать в stock reconciliation по текущему canonical contract, но всегда
имеет `destination_eligible=false` и никогда не попадает в planning candidates,
ranking, recommendation или download/operator handoff.

If WB, catalog or coefficient evidence is unavailable, the picker fails closed
with controlled blockers and does not alter the calculation.  If a release
must be rolled back, use the normal repository release/deploy path; do not edit
the runtime database or add a server-only warehouse fallback.  After recovery,
recalculate v2 from authoritative sources and verify reconciliation, excluded
warehouse counts, unique date counts and the full-width UI before acceptance.
