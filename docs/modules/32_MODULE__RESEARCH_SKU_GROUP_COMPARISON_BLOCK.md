---
title: "Модуль: research_sku_group_comparison_block"
doc_id: "WB-CORE-MODULE-32-RESEARCH-SKU-GROUP-COMPARISON-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический reference по MVP вкладке `Исследования` и read-only расчёту ретроспективного сравнения двух групп SKU."
scope: "Top-level вкладка `Исследования` в `/sheet-vitrina-v1/vitrina`, options/calculate API для `Сравнение групп SKU`, read-only calculation over persisted ready snapshots / accepted truth, исключение финансовых метрик и operator-facing result table."
source_basis:
  - "README.md"
  - "docs/modules/26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "docs/modules/31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
  - "packages/application/sheet_vitrina_v1_research.py"
related_modules:
  - "packages/application/sheet_vitrina_v1_research.py"
  - "packages/application/sheet_vitrina_v1_web_vitrina.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
  - "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
related_tables:
  - "DATA_VITRINA"
related_endpoints:
  - "GET /sheet-vitrina-v1/vitrina"
  - "GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options"
  - "POST /v1/sheet-vitrina-v1/research/sku-group-comparison/calculate"
related_runners:
  - "apps/sheet_vitrina_v1_research_sku_group_comparison_smoke.py"
  - "apps/sheet_vitrina_v1_web_vitrina_browser_smoke.py"
related_docs:
  - "26_MODULE__SHEET_VITRINA_V1_MVP_END_TO_END_BLOCK.md"
  - "31_MODULE__WEB_VITRINA_PAGE_COMPOSITION_BLOCK.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен первый read-only исследовательский MVP-контур без causal claims, без Google Sheets/GAS и без отдельной fact-системы."
---

# 1. Идентификатор и статус

- `module_id`: `research_sku_group_comparison_block`
- `family`: `web/operator/research`
- `status`: active MVP
- `surface`: top-level вкладка `Исследования` на unified `/sheet-vitrina-v1/vitrina`

# 2. Source of truth

Расчёт читает только server-side accepted truth / persisted ready snapshots, тот же контур, из которого строится текущая web-витрина.

Не используются:
- Google Sheets / GAS как active source;
- browser localStorage как source of truth;
- отдельная manual fact table только под исследование;
- refresh/upstream fetch/backfill/reconcile во время расчёта.

# 3. UI contract

Вкладка `Исследования` находится рядом с `Витрина`, `Расчет поставок`, `Отчеты`.

MVP-блок:
- title: `Сравнение групп SKU`;
- wording: `Ретроспективное сравнение исследуемой и контрольной группы по выбранным метрикам за базовый период и период анализа.`;
- две multi-select группы SKU с взаимным исключением;
- обязательные периоды `Базовый период` и `Период анализа`;
- selector `Метрики`;
- primary action `Рассчитать`;
- result table с базой/анализом, дельтами, отличием изменений и покрытием.

В UI и API wording нельзя утверждать causal effect или статистическую значимость. Нормальная формулировка: `динамика`, `отличие изменений`, `ретроспективное сравнение групп`.

# 4. API contract

`GET /v1/sheet-vitrina-v1/research/sku-group-comparison/options` возвращает:
- active/current SKU из `config_v2`;
- selectable metric options;
- readable date capabilities;
- deterministic `default_metric_keys`;
- `source_truth = server_side_accepted_truth_ready_snapshots`.

`POST /v1/sheet-vitrina-v1/research/sku-group-comparison/calculate` принимает:
- `research_sku_ids`;
- `control_sku_ids`;
- `metric_keys`;
- `baseline_period.date_from/date_to`;
- `analysis_period.date_from/date_to`.

Validation:
- все списки непустые;
- один SKU не может входить в обе группы;
- SKU должны быть active/current;
- metric keys должны быть selectable non-financial metrics;
- даты должны быть валидны и `date_from <= date_to`.

# 5. Metric scope

MVP исключает финансовый блок:
- primary rule: metric section/block/category financial/economics не selectable;
- fallback rule: obvious financial semantics (`buyout`, revenue, profit, margin, cost, DRR/spend/ads_sum as spend, rub totals) excluded;
- operational price-like metrics may remain selectable when current registry treats them as non-financial operational metrics.

# 6. Calculation semantics

Для каждой выбранной метрики, группы SKU и периода:
- expected points = selected SKU x dates;
- blank/null values ignored in numeric aggregation;
- `observed_points == 0` gives `null/unavailable`, never zero;
- sum metrics use `sum_observed_values`;
- rate/percent/average/index/position/price-like operational metrics use `mean_observed_values`;
- materialized formula/derived rows are read as existing ready snapshot values; MVP does not add a formula evaluator;
- each row exposes `aggregation_method`.

Deltas:
- research/control absolute delta = analysis - baseline;
- pct delta = absolute delta / abs(baseline), null when baseline is null or zero;
- difference in changes = research delta - control delta;
- pct-point difference uses both pct deltas when available.

Coverage is surfaced per group/period as expected/observed/missing points, coverage percent, status and missing dates.
