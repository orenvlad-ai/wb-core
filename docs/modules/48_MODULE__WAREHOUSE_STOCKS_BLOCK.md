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
update_note: "`warehouse_opening_v1` сохранён immutable audit. Active truth после guarded apply принадлежит `warehouse_functional_cutover_v1` и versioned functional balances."
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

WB status `Отгрузка разрешена` создаёт один idempotent canonical FF debit полного packed composition. Этот debit фиксирует фактический moving WAC FF в момент движения; downstream supply layer добавляет к нему только validated FF services/storage, transit и paid acceptance, поэтому «последняя supplier-поставка того же SKU» не может стать скрытым cost baseline после смешивания. Дополнительного manual shipment gate нет; `Допринято` не создаёт второй debit. Legacy FF route остаётся совместимым переходом к unified warehouse screen.

## 2.3 FF → WB and discrepancies

До final acceptance по SKU:

`open quantity = max(packed - accepted, 0)`.

Final accepted supply обнуляет `ff_to_wb`; positive final difference поступает в pooled discrepancy warehouse по SKU. Accepted part никогда не прибавляется к WB quantity вручную. FF services, storage and transit распределяются по полному packed quantity даже при partial/zero acceptance; accepted quantity хранится отдельно и никогда не подменяет packed denominator. Official transit component имеет приоритет; Seller Portal transit используется только при отсутствии official transit.

Paid WB acceptance отделена от transit: она капитализируется только на фактически accepted quantity, входит в accepted WB inbound cost и исключена из `ff_to_wb`/discrepancy WAC. `cost_total` не может скрыто превратиться в transit: canonical layer сохраняет pre-acceptance cost и acceptance amount/per-accepted-unit отдельно.

Discrepancy WAC содержит все pre-acceptance costs. `Допринято` сопоставляется pooled строго по тому же `nm_id`: `matched=min(doprinato, positive balance)`. Surplus попадает в transitional unmatched audit и не создаёт negative quantity/capital. Targeted replay повторяет match, когда появляется positive balance. Automatic loss writeoff не реализован.

## 2.4 WB snapshot

WB является snapshot warehouse. Единственный quantity source — полный успешный official `/api/analytics/v1/stocks-report/wb-warehouses` response:

`WB contour = quantity + inWayToClient + inWayFromClient`.

Каждый snapshot сохраняет requested IDs, raw rows, page offsets/count, completion flag, digest and `fetched_at`. Requested SKU разбиваются на official batches максимум по 1000 `nmIds`; только успешное завершение всех batches/pages образует один атомарный snapshot. Incomplete coverage, pagination failure, exhausted 429, transport error or invalid payload оставляют last good version; UI показывает freshness/error. True zero допускается только внутри complete response. Доказанный official special bucket `warehouseId=0`, `warehouseName=Остальные` сохраняется как отдельная warehouse-name/region identity: его in-way quantities входят в WB contour, но произвольный zero ID по-прежнему считается invalid payload.

Periodic WB WAC получает accepted inbound capital, но quantity всегда заменяется official contour snapshot. Каждый hourly apply переигрывает versioned daily WAC от functional cutover: closed days фиксируются отдельными daily rows, current day остаётся provisional, zero-stock SKU retains last valid WAC. Late expense/accepted correction публикует signed event с исходной business date и атомарно перестраивает только derived daily cost history от этой границы; positive pool и cost не могут стать negative/zero. Direct consumers сначала читают эту daily projection, поэтому `Себестоимость WB наша` не имеет независимого baseline.

# 3. Frozen historical boundary

Новая warehouse history начинается functional cutover timestamp; текущий snapshot не размножается назад. Старые warehouse values остаются audit/empty.

Отдельная разрешённая projection с `2026-07-01` покрывает `our_wb_unit_cost_rub`, Proxy 3 и direct consumers. Opening cost map строится из доказанно выбранной fully calculated FF acceptance около 24.06. Price band для отсутствующего в baseline SKU читается только из active server-side nomenclature `purchase_price_yuan` в coherent cutover capture; конфликтующие active prices одного `nmId` блокируют план. Значение и provenance копируются в frozen map, поэтому будущая правка справочника не меняет opening задним числом:

- direct SKU;
- weighted same purchase-price band;
- interpolation;
- extrapolation/single-band ratio;
- explicit fallback average при missing price.

Map frozen навсегда и сохраняет quality/provenance. WB opening cost добавляет доказанные downstream costs, включая paid acceptance только для accepted quantity. Historical daily quantity переиспользуется только из persisted daily snapshot evidence; cost переигрывается через frozen map и confirmed post-01.07 inbound layers. Для positive quantity zero/NULL cost запрещён.

# 4. Targeted replay and certification

Source change/archive/exclusion сбрасывает `Все расходы учтены`, ставит coalesced queue по stable source id/revision/effective date и affected SKU, затем coherent calculation публикует новую version atomically. Physical source rows не удаляются. Failed calculation оставляет last good active version.

`Все расходы учтены` — certification exact source/calculation fingerprint, а не calculation trigger. Provisional calculation остаётся доступным; source revision автоматически снимает certification.

Emergency rebuild использует только persisted local sources, сначала возвращает dry-run/diff/fingerprint и требует explicit confirmation exact plan. External WB/Seller Portal API он не вызывает.

# 5. Hourly WB operational sync

Repo-owned `wb-core-warehouse-functional-sync.timer` запускает bounded runner каждый час:

1. refresh official statuses/goods активных и recently completed WB supplies;
2. проверить complete active/recent status slices и enrichment; detail/goods transport, 429 and 5xx use bounded retries, while partial slice or retry-exhausted/persistent enrichment failure blocks the pipeline before any new FF debit/publication and returns bounded supply-specific diagnostics;
3. только после complete validation провести idempotent FF debit и bounded-материализовать supply-specific downstream components без legacy daily/global rebuild;
4. fetch uncached complete official stock snapshot;
5. compute FF→WB, discrepancies, unmatched, WB snapshot and targeted/daily cost states из coherent capture;
6. publish one atomic good version.

`wb-core-sheet-vitrina-refresh.timer` больше не вызывает WB supply sync или Seller Portal automation. Global vitrina refresh только читает materialized warehouse/cost state. Manual WB refresh вызывает тот же bounded pipeline.

# 6. Guarded functional cutover

`warehouse_opening_v1` и его шесть documents immutable и не меняются. Active cutover id — `warehouse_functional_cutover_v1`; timestamp берётся в production execution.

Canonical runner default dry-run получает coherent sources + uncached fresh WB snapshot, строит six-stage plan, frozen cost map, historical/daily WB cost projection, source watermarks/digests and invariants. Apply требует exact reviewed fingerprint, повторный uncached official snapshot, optimistic source recheck и совпадение semantic `calculation_digest` по costs/balances/events/documents/invariants, coherent SQLite backup `0600` with `integrity_check=ok`, one `BEGIN IMMEDIATE`, readback and idempotent second apply. Shared backup API до открытия destination требует свободное место не меньше source size плюс bounded safety margin и при любой последующей ошибке удаляет только созданные этой попыткой partial destination/sidecars. Уже оставленный оборванной попыткой invalid backup удаляется только отдельным repo-owned dry-run/apply: exact path ограничен functional backup directory/name, stat/full SHA и invalid header/integrity входят в fingerprint, coherent SQLite/live DB fail closed, а `0600` cleanup manifest остаётся в audit. WB supply revision digest включает status, packed/accepted composition, raw goods and upstream business update, но исключает собственные `synced_at`/`last_list_synced_at`/`last_enriched_at`, чтобы повторный capture без business change не создавал ложный drift. Hourly/manual publication также pins `base_active_version_id`; concurrent stale plan отклоняется, а exact already-applied fingerprint остаётся idempotent. Initial Proxy settings version создаётся внутри той же transaction. Primary supplier/CNY/FF/WB records не изменяются.

Hourly timer включается только после successful cutover readback. Rollback сначала disables timer, сохраняет backup и удаляет только functional derived state/initial settings when safe.

Supported production commands:

```bash
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-dry-run --output /abs/plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-apply --plan-file /abs/plan.json --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-readback
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-economics-dry-run --output /abs/economics-plan.json
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-economics-apply --plan-file /abs/economics-plan.json --fingerprint 'sha256:...'
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-sync
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-functional-enable-hourly
python3 apps/registry_upload_http_entrypoint_hosted_runtime.py warehouse-ui-flow --evidence-dir /abs/outside-repo
```

# 7. UI and verification

Navigation is `Остатки → Склады и себестоимость / Отчёт об остатках`. One component renders quantity, WAC, capital, quality/provenance, sync status, SKU and document registry for all stages. Document rows persist their own immutable SKU lines; discrepancy documents distinguish final-acceptance receipt, pooled `Допринято` and non-stock transitional audit. `wb_discrepancy_writeoff` is a reserved disabled type, not an automatic/manual action. WB adds four contour quantities; discrepancy detail adds transitional unmatched registry. Supplier registry exposes production/China stage cost fields. Settings exposes calculation parameters and three-week WB reference.

Targeted verification:

- `python3 apps/warehouse_functional_smoke.py`;
- `python3 apps/stocks_block_smoke.py`;
- `python3 apps/warehouse_stocks_smoke.py` (immutable legacy opening regression);
- `python3 apps/our_wb_costs_smoke.py`;
- `python3 apps/own_product_capital_smoke.py`;
- `python3 apps/cny_ledger_smoke.py`;
- `python3 apps/supplier_financial_documents_smoke.py`;
- production `warehouse-ui-flow` in a fresh Playwright/Chromium context, with screenshots/report outside Git.
