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
related_modules:
  - "packages/application/warehouse_functional.py"
  - "packages/application/warehouse_archival_estimate.py"
  - "packages/application/calculation_parameters.py"
  - "packages/application/stocks_block.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "apps/warehouse_functional_runner.py"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/warehouses"
  - "GET /v1/sheet-vitrina-v1/warehouses/{warehouse_key}"
  - "POST /v1/sheet-vitrina-v1/warehouses/sync"
  - "POST /v1/sheet-vitrina-v1/warehouses/emergency-rebuild/preview"
  - "POST /v1/sheet-vitrina-v1/warehouses/emergency-rebuild/apply"
  - "GET|POST /v1/sheet-vitrina-v1/settings/calculation-parameters"
  - "POST /v1/sheet-vitrina-v1/settings/calculation-parameters/preview"
source_of_truth_level: "module_canonical"
update_note: "Active truth принадлежит versioned functional balances; exact-date history, stable nomenclature identity, version-scoped unmatched audit, localized evidence UI and archived-metric cutover are enforced fail closed."
---

# 1. Active warehouse contract

Active state содержит ровно шесть складов:

1. `production` — `На производстве`;
2. `china_to_ff` — `Китай → FF`;
3. `ff` — `Склад FF`;
4. `ff_to_wb` — `FF → WB`;
5. `wb` — `Склад WB`;
6. `wb_acceptance_discrepancy` — `Расхождения приёмки WB`.

Supplier registry и warehouse projection не смешиваются. Invoice получает один стабильный `supplier_flow_id`; display name строится из invoice, но linkage и replay используют stable id. После смешивания на FF downstream identity принадлежит WB supply. Каждая positive line хранит exact text quantity, capital, WAC, coverage/quality и source provenance. Вычисления используют `Decimal`; UI округляет только display.

`GET .../warehouses` и detail routes сохраняют совместимость route/key, но после functional cutover читают только active functional version. Старые opening tables не суммируются с active balances.

# 2. Physical and cost rules

## 2.1 Production and China → FF

Invoice без counted supplier payment имеет zero warehouse quantity. Первый counted payment активирует полный physical invoice composition; следующие payments меняют capital/WAC, но не quantity. `counted` следует CNY ledger contract: `posted` и его детерминированный date-only ordering warning участвуют только при posted parent document; blocked/skipped и needs-review/excluded parent documents не участвуют. Отмена последнего payment через audit archive и targeted replay возвращает quantity к zero.

Supplier payments используют factual weighted RUB cost списанных CNY из CNY ledger. Конверсионная комиссия, уже включённая в RUB value CNY, второй раз не добавляется. CNY transfer fee и direct RUB bank fees имеют отдельную provenance. Supplier capital и bank fees распределяются по invoice value.

Фактическая supplier shipment date переносит тот же quantity/capital layer в `china_to_ff`. Logistics invoice и customs 1010 распределяются по quantity; duty 2010 и import VAT 5010 — по invoice value. Informational/needs-review/failed/duplicate/unmatched/excluded documents не капитализируются.

## 2.2 FF

Фактическая FF acceptance создаёт canonical append-only FF ledger receipt. Functional projection не создаёт второй ledger: cutover opening freezes current ledger quantity/cost, а post-cutover receipt/debit replay начинается от opening version. Supplier receipt получает exact source-flow capital; одинаковые SKU смешиваются moving WAC. Ordinary proportional debit сохраняет WAC.

WB status `Отгрузка разрешена` создаёт один idempotent canonical FF debit полного packed composition только когда весь состав физически доступен и каждый SKU имеет validated downstream cost state. До этого отдельный append-only reservation ledger фиксирует reserve по exact WB supply revision + nmID: физический остаток, капитал и WAC не меняются, строка получает `Ожидает поступления`. При достаточном последующем supplier receipt весь состав атомарно списывается, reservation закрывается и debit фиксирует фактический moving WAC FF именно в момент движения. Недостающая транзитная стоимость reservation-only supply не блокирует независимый official WB snapshot; фактическое движение без validated cost остаётся fail closed. Downstream supply layer добавляет только validated FF services/storage, transit and paid acceptance. Дополнительного manual shipment gate нет; `Допринято` не создаёт второй debit.

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

Каждый snapshot сохраняет requested IDs, raw rows, page offsets/count, completion flag, digest and `fetched_at`. Requested SKU разбиваются на official batches максимум по 1000 `nmIds`; только успешное завершение всех batches/pages образует один атомарный snapshot. Incomplete coverage, pagination failure, exhausted 429, transport error or invalid payload оставляют last good version; UI показывает freshness/error. True zero допускается только внутри complete response. Доказанный official special bucket `warehouseId=0`, `warehouseName=Остальные` сохраняется как отдельная warehouse-name/region identity: его in-way quantities входят в WB contour, но произвольный zero ID по-прежнему считается invalid payload.

Periodic WB WAC получает accepted inbound capital, но quantity всегда заменяется official contour snapshot. Каждый hourly apply переигрывает versioned daily WAC от functional cutover: closed days фиксируются отдельными daily rows, current day остаётся provisional, zero-stock SKU retains last valid WAC. Если точная историческая колонка объявляет более поздний SKU с нулевым остатком, которого не было в frozen opening, projection сохраняет его как нулевой капитал со статусом `zero_quantity_without_cost_basis`, а Registry/Proxy и weekly Finance consumers возвращают неизвестную, не нулевую себестоимость; положительный остаток без cost seed остаётся блокирующей ошибкой. Late expense/accepted correction публикует signed event с исходной business date и атомарно перестраивает только derived daily cost history от этой границы; positive pool и cost не могут стать negative/zero. Direct consumers сначала читают эту daily projection, поэтому `Себестоимость WB наша` не имеет независимого baseline.

Weekly Finance читает WB unit cost только через общий warehouse-domain resolver. Обычно это exact row `sheet_vitrina_v1_warehouse_wb_daily_cost`; для точного 18-SKU manifest migration 109 допустим active versioned source `business_approved_archival_estimate` 100 ₽ effective 01.07. Для operation date до `2026-07-01` resolver проецирует назад exact same-`nmId` basis на 01.07; на/после границы требует exact operation-date daily row либо тот же active archival basis до появления реального accepted cost layer. Отсутствие обоих required source является unknown и не может наследовать другой SKU, average, legacy cost или zero.

Проекция до 01.07 — только Finance management metadata, не warehouse history. Она не создаёт backdated balances/events и не меняет functional rows. Отдельная фиксированная Finance value/map запрещена; позднее исправление canonical row меняет digest и заставляет derived Finance projection перестроиться. Acceptance/transit может быть исключён из Finance period expenses только через exact `supplyId + nmId` lineage к текущему cost layer и не выше капитализированной суммы.

# 3. Frozen historical boundary

Новая warehouse history начинается functional cutover timestamp; текущий snapshot не размножается назад. Старые warehouse values остаются audit/empty.

Ready snapshots с 01.07 очищаются от несогласованных legacy warehouse stage/total cells. Для дня, где отсутствует полный доказанный шестиступенчатый functional state, canonical warehouse cells остаются пустыми, а `warehouse_history_coverage` и cell presentation показывают `Исторические данные отсутствуют` с source-level причиной; это неизвестность, не доказанный ноль. Дни с точной functional version публикуют quantity/capital и WAC из version, выбранной по `snapshot_date`. Отдельные доказанные `our_wb_unit_cost_rub` и Proxy 3 сохраняются по их daily projection и не зависят от доступности полного warehouse history.

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

Изменение только служебного marker/timestamp при `changed_cells=inserted_rows=archived_rows=0` не считается mutation: plan возвращает zero updates, apply является idempotent no-op и не создаёт многогигабайтный backup. Реальное изменение warehouse/economics cells по-прежнему требует exact fingerprint, coherent backup и atomic apply. Dry-run до и после расчёта, а apply повторно уже под `BEGIN IMMEDIATE` сверяют один manifest functional versions/snapshots/balances, version-scoped supplier cost states, active version, текущих supplier/CNY/financial source rows, daily WB costs и effective parameter versions; почасовая публикация или cost-source mutation во время длительного backup поэтому останавливает stale backfill до изменения ready snapshot. Для active exact-date rows backfill повторяет source/calculation fingerprint revalidation и публикует жёлтый `source_changed_provisional`, пока targeted replay не выпустил новую согласованную версию.

Dry-run фиксирует одну canonical business date в fingerprint на всю операцию. Apply требует ту же дату перед fresh recheck, после backup, под write lock и непосредственно перед commit; переход бизнес-полуночи до commit откатывает транзакцию. Live/closed coverage поэтому не смешивает две даты внутри одного плана.

`warehouse_history_coverage` — семантическая часть ready snapshot, а не служебный marker. Изменение `live/closed/partial/unavailable`, причины, covered scope или exact `functional_version_id`, из которой опубликованы числовые cells, публикуется даже при неизменных значениях; повтор с тем же coverage остаётся no-op. Mutable-source fingerprint revalidation применяется только к текущей canonical business date и лишь если version binding ready snapshot совпадает с активной functional version. При раздельном успехе warehouse sync и сбое/задержке economics publication старое число помечается `unavailable` и скрывается до согласованной публикации, а не получает статус новой версии. Закрытая историческая дата остаётся привязана к своей exact-date immutable functional version и не меняет зелёный/жёлтый статус из-за более позднего документа. Production UI acceptance определяет применимость `our_wb_unit_cost_rub` по положительному exact-date official WB `stock_total`, независимо от warehouse-history projection и наличия продаж; поэтому неизвестная pre-cutover `own_capital_WB_qty` не может ложно сделать все WB-cost cells неприменимыми. Для Proxy 3 applicability отдельно использует `orderSum`.

# 4. Targeted replay and certification

Source change/archive/exclusion сбрасывает `Все расходы учтены`, ставит coalesced queue по stable source id/revision/effective date и affected SKU, затем coherent calculation публикует новую version atomically. Physical source rows не удаляются. Failed calculation оставляет last good active version.

`invoice_no` и `invoice_date` входят в этот source-change contract. Supplier-registry stage cell вычисляет frozen quality/certification по выбранным supplier flow records, а затем сверяет их текущие fingerprints; агрегатный mixed SKU balance не подменяет статус конкретной поставки.

`Все расходы учтены` — certification exact source/calculation fingerprint, а не calculation trigger. Provisional calculation остаётся доступным; source revision автоматически снимает certification.

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
5. reconcile exact supply revisions: создать/скорректировать reservation либо атомарно выполнить полностью обеспеченный physical debit; reservation-only missing cost не блокирует независимые части цикла, actual movement остаётся fail closed;
6. fetch uncached complete official stock snapshot;
7. compute FF→WB, discrepancies, unmatched, WB snapshot and targeted/daily cost states из coherent capture;
8. publish one atomic good version. Unmatched audit identity включает owning functional `version_id`, поэтому одна и та же source evidence может безопасно присутствовать в последовательных versions без primary-key collision; повтор exact plan остаётся no-op.

`wb-core-sheet-vitrina-refresh.timer` больше не вызывает WB supply sync или Seller Portal automation. Global vitrina refresh только читает materialized warehouse/cost state. Manual WB refresh вызывает тот же bounded pipeline.

Для длительного reviewed Finance backfill используется repo-owned `warehouse-functional-maintenance status|hold|restore`. `status` фиксирует `is-enabled`/`is-active`, last/next timer trigger, service result, существование/занятость `.warehouse-functional-sync.lock` и отсутствие Finance apply. `hold` сохраняет mode-`0600` baseline/audit в canonical runtime, останавливает только functional timer (не отключая и не меняя его unit), не убивает уже запущенный service и bounded ждёт его штатного завершения и освобождения общего lock. Терминальный `failed` у oneshot-сервиса сохраняется как evidence (`Result`/`ExecMainStatus`), но считается quiescent наравне с `inactive`; running-переходы `active`/`activating`/`reloading`/`deactivating` остаются fail-closed ожиданием. `restore` разрешён только без Finance apply/warehouse writer и возвращает timer в exact исходные enabled/active состояния. Drift timer unit остаётся fail-closed. Если во время hold штатный deploy обновил service unit, restore принимает его только для quiescent service при свободном lock, неизменном timer digest, `NeedDaemonReload=no`, отсутствии drop-ins и точном byte-for-byte совпадении установленного fragment с repo-deployed systemd artifact; доказательство повторно проверяется после timer mutations, а старый/новый digest и пути сохраняются в audit. Любой недоказанный service drift блокирует restore. Canonical Finance apply держит тот же `.warehouse-functional-sync.lock` на всём интервале current plan → coherent backup → atomic apply → transactional readback; fingerprint/source validation не ослабляется. При drift после hold строится один новый стабильный plan и требуется новый exact human fingerprint.

# 6. Guarded functional cutover

`warehouse_opening_v1` и его шесть documents immutable и не меняются. Active cutover id — `warehouse_functional_cutover_v1`; timestamp берётся в production execution.

Canonical runner default dry-run получает coherent sources + uncached fresh WB snapshot, строит six-stage plan, frozen cost map, historical/daily WB cost projection, source watermarks/digests and invariants. Apply требует exact reviewed fingerprint, повторный uncached official snapshot, optimistic source recheck и совпадение semantic `calculation_digest` по costs/balances/events/documents/invariants, coherent SQLite backup `0600` with `integrity_check=ok`, one `BEGIN IMMEDIATE`, readback and idempotent second apply. Canonical business date сверяется до apply, после получения write lock и непосредственно перед commit; пересечение полуночи откатывает всю derived transaction. Каждый functional apply/rollback и archival-estimate apply/rollback входит в общий re-entrant `.warehouse-functional-sync.lock`, включая backup и transaction. Active archival version с exact row lineage входит в functional local-source digest, поэтому plan, рассчитанный до archival activation/rollback, после получения lock отклоняется и не может перезаписать correction. Archival dry-run держит одну explicit SQLite read transaction для rows и всех digests; daily provenance с factual inbound/accepted evidence или отличающимся WAC блокирует замену оценкой. Каждый target обязан иметь ровно одну nomenclature row: duplicate, identity conflict или factual purchase price блокирует estimate. Fresh post-apply archival plan имеет `status=no_op`, `apply_allowed=false` и при передаче runner остаётся inert readback без backup/новой version. Rollback сохраняет version/audit и делает использованный fingerprint необратимо non-reusable; parent functional rollback запрещён до archival rollback/deactivation. После factual acceptance archival row перестаёт быть допустимым сразу: до успешного daily replay resolver, archival readback и exact idempotent retry возвращают явный blocker, затем обычная WAC заменяет оценку. Targeted archival lookup сначала ограничивает active rows одним requested `nmId`, использует indexed factual-event range и для non-target не сканирует events. Current open business day сохраняет `periodic_snapshot_wac_provisional`, закрытые дни — `periodic_snapshot_wac_closed`. Shared backup API до открытия destination требует свободное место не меньше source size плюс bounded safety margin и при любой последующей ошибке удаляет только созданные этой попыткой partial destination/sidecars. Уже оставленный оборванной попыткой invalid backup удаляется только отдельным repo-owned dry-run/apply: exact path ограничен functional backup directory/name, stat/full SHA и invalid header/integrity входят в fingerprint, coherent SQLite/live DB fail closed, а `0600` cleanup manifest остаётся в audit. WB supply revision digest включает status, packed/accepted composition, raw goods and upstream business update, но исключает собственные `synced_at`/`last_list_synced_at`/`last_enriched_at`, чтобы повторный capture без business change не создавал ложный drift. Hourly/manual publication также pins `base_active_version_id`; concurrent stale plan отклоняется, а exact already-applied fingerprint остаётся idempotent. Initial Proxy settings version создаётся внутри той же transaction. Primary supplier/CNY/FF/WB records не изменяются.

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
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-enable-hourly
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-dry-run --output /abs/estimate-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-apply --plan-file /abs/estimate-plan.json --fingerprint 'sha256:...' --approval-reference '...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-readback
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-archival-estimate-rollback --fingerprint 'sha256:...' --reason '...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-ui-flow --evidence-dir /abs/outside-repo
```

# 7. UI and verification

Hosted backup-only, manual and reviewed sync-apply actions derive the restore-point directory from canonical runtime state as `state/backups/warehouse-functional-sync`; they must not redirect that checkpoint to the live-runtime filesystem sibling `/opt/wb-core-runtime/backups`.

Navigation is `Остатки → Склады и себестоимость / Отчёт об остатках`. One component renders quantity, WAC, capital, localized quality, sync status, SKU and document registry for all stages. Exact active nomenclature by `nm_id` enriches name/barcode; conflicting active identities remain visibly ambiguous and are never guessed. Every applicable row exposes a centralized Russian status plus human-readable evidence fields (document/date/invoice or supply/quantity source/cost source/confirmation/allocation/contribution). Source/stage date and per-source quality are persisted in provenance; a mixed SKU therefore shows the exact certified/provisional status of each contributing invoice, while a document line uses its known document occurrence as a bounded date fallback. For several FF→WB supplies each evidence row owns its exact open quantity and capital, and their sums equal the displayed SKU balance. FF evidence expands the cutover opening and every post-cutover append-only ledger operation with signed quantity/capital and operation date instead of duplicating an aggregate wrapper. Raw provenance JSON exists only in a nested technical disclosure.

The six warehouse detail pages are read-only and contain no duplicated update/rebuild buttons. Sibling tab `Обновление и пересчёт` owns `Пересчитать все склады и себестоимости`: it starts the same canonical current-source pipeline as the six-stage functional timer behind a background server job, returns `run_id`, polls status and shows last attempt/success, changed warehouses/SKU, functional version/business date and a short Russian log. A parallel start returns `Уже выполняется другой пересчёт`; page open performs no mutation. This action is not an emergency/full-history rebuild and returns controlled source/cost/lock blockers. Dark-theme normal/hover/pressed/disabled/loading/success/error states remain verified. Synchronous long apply through the proxy is forbidden; reviewed bounded recovery uses `warehouse-functional-sync-dry-run` followed by exact-fingerprint `warehouse-functional-sync-apply`, which refreshes official supplies and current downstream layers, reconciles reservations/physical movements, takes a coherent verified restore point before production source mutation, rechecks semantic source/snapshot/diff/invariants under the shared lock and publishes only the new current functional version plus actually dependent economics. Neither page open nor global vitrina refresh launches a warehouse mutation.

Document rows persist their own immutable SKU lines; discrepancy documents distinguish final-acceptance receipt, pooled `Допринято` and non-stock transitional audit. `wb_discrepancy_writeoff` is a reserved disabled type, not an automatic/manual action. WB adds four contour quantities; discrepancy detail adds transitional unmatched registry. The `Поставки → Реестр поставок` matrix exposes production/China stage cost fields and an aggregated `Комиссии банка, ₽` row derived from the same exact fee summary used by cost allocation. Settings exposes calculation parameters and three-week WB reference.

WB UI labels the headline quantity `Всего в контуре WB`, keeps `На складах WB` as a separate physical component and displays the exact formula with both in-way components. FF UI separately renders physical/reserved/available/unsecured quantities; reservation-only rows have zero physical capital and human-readable waiting status. Physical and reservation rows are frozen together in the same functional `version_id`, so a post-publication ledger change or failed sync cannot mix live reserve with last-good physical quantity. Legacy negative physical balance without a positive reservation remains a ledger warning and never fabricates `Ожидает поступления`. A certified balance never relies on an absent caption: centralized quality presentation always renders yellow provisional/mixed or green `Все расходы учтены / Подтверждено документами` text. Before green presentation, every supplier-origin balance revalidates the version-frozen certification against current source/calculation fingerprints and the exact active version. The Web Vitrina read contract repeats that revalidation for the active business-date cells before render and replaces persisted presentation metadata in memory; closed dates remain bound to their exact immutable versions. A changed source therefore immediately becomes yellow `Предварительная себестоимость — источники изменились`, so a queued or failed targeted replay cannot leave stale green state in either warehouse detail or Web Vitrina. Unprovable warehouse-history cells render compact `—` on the dark table surface with one accessible explanation instead of repeating a long light-background sentence. `warehouse-functional-backup` creates a coherent `0600` SQLite backup after a free-space check and reports `integrity_check=ok` plus SHA-256 without modifying business or derived rows. Hourly and operator-button sync share the same re-entrant cross-process lock with versioned calculation-parameter publication, independently reserve raw-plus-archive space on the backup mount and a bounded publication margin derived from the current coherent DB/WAL size on the live-runtime mount (or sum both requirements on one filesystem), and prepare the business day's coherent restore point before supplier/snapshot mutation. Its private raw provenance manifest pins date/path/size/SHA and is rechecked before reuse. After success the raw copy is losslessly replaced by a verified `0600` zstd archive, and later runs recheck the actual compressed SHA, zstd frame and decompressed SHA/size before reuse. Privileged CLI manual sync and each operator-authored calculation-parameter save deliberately take a fresh restore point; the latter fsyncs its exact preview fingerprint into a private raw sidecar before the settings transaction. Daily, operator-settings and privileged manual-sync archives use bounded per-scope retention before their capacity gates and reserve one slot before a fresh incoming checkpoint: only older reverified archive/manifest pairs are removed, every retained candidate is verified first, newest restore points remain, and every removal is recorded through an atomically replaced durable `0600` intent/completion journal that accepts prior completed audit rows. Successful readback exposes the verified archive as primary backup evidence and explicitly marks its raw source removed. A capacity or archive-integrity failure therefore occurs before the first source/projection write, and an interrupted run keeps its raw rollback file. Production Playwright acceptance always writes a terminal machine-readable report, including a sanitized failure report when an assertion aborts the flow.

The active version itself is never retroactively edited to repair a missing legacy certification projection. `supplier-certification-dry-run/apply` pins the exact active version plan fingerprint and a coherent manifest of supplier/CNY/financial sources. A correction requires either exact supplier-flow fingerprints already embedded in immutable balance provenance or, for a legacy version without those fields, exact target-scoped conservation of every frozen per-SKU quantity/capital and the same payment, CNY-fee and China→FF document identities; every contributing mutable source row must also have a server-owned revision timestamp strictly earlier than the immutable version, with equality blocked as ambiguous at whole-second precision. Deterministic CNY ledger replay preserves `updated_at` for byte-semantic unchanged operations instead of manufacturing a later target revision. Nested supplier-flow provenance may be retained by a later FF/WB balance; its outer warehouse location is audit context and is not falsely compared with the original supplier allocation stage. Global source-group watermarks and the broader local digest remain audit evidence rather than target eligibility gates because unrelated informational documents and WB/FF/history materializations legitimately change them; the complete current supplier-source manifest is still plan-pinned and transactionally rechecked. The runner then appends only ordered version-scoped correction rows plus audit provenance after a `0600` integrity-checked backup and an in-transaction optimistic recheck. Missing frozen proof or any target allocation/revision change fails closed and requires a newly calculated functional version; a replay cannot turn stale WAC/capital green. Exact repeat is a no-op; the paired rollback appends a tombstone instead of deleting audit. Effective reads prefer the latest non-rolled-back correction for that exact version, while the next successful hourly version carries ordinary base states and naturally supersedes the recovery overlay.

Targeted verification:

- `python3 apps/warehouse_functional_smoke.py`;
- `python3 apps/warehouse_archival_estimate_smoke.py`;
- `python3 apps/warehouse_supplier_cost_state_replay_smoke.py`;
- `python3 apps/ff_stock_reservation_smoke.py`;
- `python3 apps/stocks_block_smoke.py`;
- `python3 apps/warehouse_stocks_smoke.py` (immutable legacy opening regression);
- `python3 apps/our_wb_costs_smoke.py`;
- `python3 apps/own_product_capital_smoke.py`;
- `python3 apps/canonical_cost_engine_smoke.py` (exact period-column selection; no current-value backfill);
- `python3 apps/cny_ledger_smoke.py`;
- `python3 apps/supplier_financial_documents_smoke.py`;
- production `warehouse-ui-flow` in a fresh Playwright/Chromium context, entering the shared-shell operator/settings/report frames only after their explicit `src` navigation; its reusable default verifies six-stage arithmetic, identities/evidence, bank-fee aggregate/detail, correctly parsed dark-theme contrast, canonical product-capital keys, date coverage, archived-metric absence and rendered consumers without pinning the mutable SKU catalog or specific shipments. WB-cost applicability is taken from the persisted exact-date WB contour quantity (physical plus both in-way components), never physical stock alone. The bounded migration-104 controls run only with `--acceptance-profile warehouse_chain_recovery_20260719`; the current exact-cost profile pins the 33-SKU WB snapshot, 26GN310/26GN390 payment, allocation, certification and Anti-Spy proof plus 17–18 July controls under `--acceptance-profile warehouse_cost_transparency_20260720`. Historically unavailable warehouse cells must render one compact dark-theme `—` with an accessible Russian reason. Evidence stays outside Git.

## Supplier cost freshness projection

Supplier stage cells are certified only from the current active functional version after revalidating both source and calculation fingerprints. A queued/running targeted request suppresses the frozen numeric value and renders `Ожидает пересчёта`; an error renders its exact blocker; any other fingerprint mismatch is stale and also suppresses the number. After successful replay the current canonical value is green. `production`/`china_to_ff` cells for a shipment that already reached FF are neutral `Не применяется: поставка уже на ФФ`.

The supplier exact-cost cell follows its canonical proof independently of the operator completeness flag. `certified` is green, `provisional` is yellow with warnings, and `unavailable` has no current number and exposes blockers. Thus neither equal-looking values nor `expenses_complete=true` establish functional-version freshness.
