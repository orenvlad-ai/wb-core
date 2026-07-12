---
title: "Модуль: own_product_capital_block"
doc_id: "WB-CORE-MODULE-45-OWN-PRODUCT-CAPITAL-BLOCK"
doc_type: "module"
status: "active_read_side_facade"
purpose: "Зафиксировать paid-capital projection и пять физических стадий поверх единого canonical cost engine."
scope: "Public SKU/TOTAL metrics, Decimal aggregate semantics и underaccepted presentation; legacy event/state rows остаются audit-only."
source_basis:
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
related_modules:
  - "packages/application/canonical_cost_engine.py"
  - "packages/application/own_product_capital.py"
  - "packages/application/sheet_vitrina_v1_own_product_capital.py"
  - "apps/canonical_cost_engine_backfill.py"
related_tables:
  - "sheet_vitrina_v1_canonical_cost_baseline_versions"
  - "sheet_vitrina_v1_canonical_cost_baseline_lines"
  - "sheet_vitrina_v1_canonical_cost_components"
  - "sheet_vitrina_v1_canonical_cost_movement_layers"
  - "sheet_vitrina_v1_canonical_cost_wb_outstanding_layers"
  - "sheet_vitrina_v1_canonical_cost_daily_state"
  - "sheet_vitrina_v1_own_capital_* (legacy audit)"
related_endpoints:
  - "POST /v1/sheet-vitrina-v1/product-capital/recalculate"
  - "GET /v1/sheet-vitrina-v1/product-capital/status"
related_runners:
  - "apps/canonical_cost_engine_backfill.py"
  - "apps/canonical_cost_engine_smoke.py"
  - "apps/canonical_cost_engine_backfill_smoke.py"
related_docs:
  - "migration/99_unified_canonical_cost_engine.md"
source_of_truth_level: "module_canonical"
update_note: "Module 45 больше не владеет parallel inventory/cost baseline; quantity и costs являются двумя projections единого engine."
---

# 1. Five physical stages

Стадии ровно пять:

1. `На производстве` (`PRODUCTION`);
2. `Производство → ФФ` (`PRODUCTION_TO_FF`);
3. `На ФФ` (`FF`);
4. `ФФ → WB` (`FF_TO_WB`);
5. `На WB` (`WB`).

Physical sources не принадлежат module 45:

- production/in-transit — supplier registry, factual statuses/dates и полностью matched product lines;
- FF — сумма authoritative `ff_stock_ledger` operation lines по factual effective dates;
- FF→WB — exact FF debit минус persisted cumulative accepted/doprinato evidence;
- WB — official persisted WB stock.

Canonical daily rows являются replay/cache, а не отдельным складом. Они обязаны reconciliate FF/WB/registry quantities с источниками.

# 2. Paid and recognized projections

Один component graph строит две проекции на одних quantities/linkage:

- paid capital: только factual payments с factual effective dates;
- recognized cost: подтверждённые понесённые/признанные расходы, даже если paid date позже.

Physical quantity не исчезает при неполной оплате или cost gap. Для production отдельно выводятся physical quantity, paid-equivalent quantity и paid capital. Partial supplier payment распределяется по всем matched product lines: при 15% оплаты 100 000 физ. единиц дают 15 000 paid-equivalent, а paid unit cost использует denominator 15 000.

Public fields дополнены `paid_equivalent_qty` и `cost_coverage_pct`. `confirmed_share_pct` остаётся отдельным quality ratio. 1С fallback и bounded `business_approved_primary_wac_fallback` для nmID `497415593/497416931` считаются covered, но не primary-confirmed; production paid projection по-прежнему использует только factual payments.

# 3. TOTAL semantics

- physical quantity = сумма SKU physical quantities;
- paid-equivalent quantity = сумма SKU paid-equivalent quantities;
- capital = сумма SKU paid capital;
- paid unit cost = `SUM(paid capital) / SUM(paid-equivalent quantity)`;
- coverage = `SUM(cost-covered physical quantity) / SUM(physical quantity)`;
- confirmation = `SUM(confirmed physical quantity) / SUM(physical quantity)`.

Никаких arithmetic means SKU averages/percentages.

# 4. Underaccepted WB

`Недопринято WB` — derived substate `ФФ → WB`, не шестая стадия и не отдельный склад. Exact immutable layers хранят original supply, nmID, warehouse/destination, FF movement snapshot, sent/accepted/open quantities, recognized/paid costs и provenance.

Reconciliation: direct original identity, иначе strict FIFO по `warehouse + destination + nmID`; future layer не eligible; surplus/negative блокируются; retry idempotent; second FF debit отсутствует. Cost layers не смешиваются при переносе.

UI добавляет только:

- `Недопринято WB: количество, шт`;
- `Недопринято WB: средняя себестоимость, ₽/шт`.

TOTAL quantity суммируется, cost равен `SUM(open paid capital) / SUM(open quantity)`. Underaccepted уже включён в stage `ФФ → WB` и повторно в stage/overall capital не прибавляется. Нет age/color/drilldown/actions/manual loss writeoff.

# 5. Legacy boundary and migration

Даты раньше `2026-07-01` и legacy `own_capital` events/daily rows не переписываются. После approved baseline apply `OwnProductCapitalBlock.load_daily_metric_lookup()` читает canonical rows; старые events/outstanding остаются audit evidence и не являются live physical truth. Targeted historical orphan exceptions legacy runner не переносятся в canonical methodology.

Production candidate/apply выполняет только `apps/canonical_cost_engine_backfill.py`. Dry-run доказывает exact primary shipment, fallback dates, 100% coverage, stage reconciliation, current-vs-candidate delta, affected Finance weeks и preservation digests. Apply запрещён без exact human-approved fingerprint и backup plan.
