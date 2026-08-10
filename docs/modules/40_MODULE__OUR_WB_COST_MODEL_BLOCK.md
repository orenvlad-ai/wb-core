---
title: "Модуль: our_wb_cost_model"
doc_id: "WB-CORE-MODULE-40-OUR-WB-COST-MODEL-BLOCK"
doc_type: "module"
status: "active_read_side_facade"
purpose: "Зафиксировать direct projection `Себестоимость WB наша`, неизменный Proxy 3 и immutable automatic Proxy 4 поверх canonical functional warehouse/cost engine."
scope: "Public metric keys, daily WB WAC, Proxy profit/margin 3 and 4, calculation parameters and legacy audit boundary."
source_basis:
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/44_MODULE__WB_FINANCE_WEEKLY_REPORT_BLOCK.md"
  - "docs/modules/45_MODULE__OWN_PRODUCT_CAPITAL_BLOCK.md"
  - "docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md"
related_modules:
  - "packages/application/warehouse_functional.py"
  - "packages/application/calculation_parameters.py"
  - "packages/application/calculation_parameters_v4.py"
  - "packages/application/our_wb_costs.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
  - "packages/application/sheet_vitrina_v1_proxy_v4.py"
  - "packages/application/proxy_v4_historical_projection.py"
  - "packages/application/warehouse_functional_economics_backfill.py"
  - "packages/application/sheet_vitrina_v1_proxy_margin_3_historical_backfill.py"
related_tables:
  - "sheet_vitrina_v1_warehouse_functional_balances"
  - "sheet_vitrina_v1_warehouse_wb_daily_cost"
  - "sheet_vitrina_v1_warehouse_archival_estimate_versions/rows/active"
  - "sheet_vitrina_v1_calculation_parameter_versions"
  - "sheet_vitrina_v1_proxy_v4_parameter_versions"
  - "sheet_vitrina_v1_wb_cost_daily_state (legacy audit/fallback before functional apply only)"
related_endpoints:
  - "GET|POST /v1/sheet-vitrina-v1/settings/calculation-parameters"
  - "POST /v1/sheet-vitrina-v1/settings/calculation-parameters/preview"
  - "GET|POST /v1/sheet-vitrina-v1/settings/calculation-parameters-v4"
  - "POST /v1/sheet-vitrina-v1/settings/calculation-parameters-v4/preview"
  - "GET /v1/sheet-vitrina-v1/warehouses"
source_of_truth_level: "module_canonical"
update_note: "Добавлен отдельный immutable Proxy V4 с fixed boundary 2026-08-01 и aligned three-week Buyout/Finance versions; V3 formula/history и canonical WB WAC не меняются."
---

# 1. Canonical WB WAC

`our_wb_unit_cost_rub` / `total_our_wb_unit_cost_rub` (`Себестоимость WB наша`) — direct read projection canonical daily WB WAC. Отдельная formula/baseline в module 40 запрещена. Единственное data-backed archival исключение — active versioned manifest migration 109: ровно 18 legacy `nmId`, 100 ₽ с effective date 01.07.2026 и quality `business_approved_archival_estimate`; Finance не содержит веток по этим ID.

WB quantity задаёт только complete official contour snapshot:

`quantity + inWayToClient + inWayFromClient`.

Accepted WB supply добавляет доказанный inbound capital, но не quantity поверх snapshot. Periodic WAC сохраняет last valid cost при zero stock; late cost evidence запускает targeted replay от effective business date. TOTAL cost:

`SUM(WB contour capital) / SUM(WB contour quantity)`.

Один warehouse-domain temporal resolver обслуживает Vitrina, Finance weekly/per-SKU projections, Partner и Proxy 3. Для даты до `2026-07-01` он выбирает exact canonical row того же `nmId` на `2026-07-01`; на границе и после неё — exact row соответствующей business/operation date. Для SKU/дат `2026-07-01..functional cutover` loader читает frozen functional historical cost projection. Она построена из frozen 24.06 opening map, persisted historical quantities и known downstream costs. Если ready snapshot содержит период, lookup выбирает только колонку точной business date, даже когда внешний `snapshot.as_of_date` новее. Fallback на предыдущий/current snapshot, other-SKU/average/legacy cost и копирование текущего остатка назад запрещены. После cutover loader читает active functional daily/current state. Legacy WB daily tables остаются audit и не являются параллельным active source.

# 2. Versioned calculation parameters

`Настройки → Расчётные параметры → Proxy прибыль и маржинальность · V3` хранит прежние immutable versions с effective date, revision, author/time, exact fingerprint и diff preview. Переименование блока не меняет его поля, формулу, историю или save/recalculation semantics. Верхняя навигация уже является заголовком раздела, поэтому страница не повторяет внутренний heading `Расчётные параметры`. Initial V3 version effective `2026-07-01`:

- buyout rate — `91%`;
- tax — `6%`;
- WB agent/other expenses — `38%`;
- acquiring/logistics/storage/penalties/other — `0%`;
- total included expenses — `44%`;
- retained share — `56%`.

Validation требует каждый процент в `0..100%` и total expenses `<100%`. V3 save создаёт новую version и targeted Proxy 3 recalculation от явно выбранной effective date; physical warehouses не пересчитываются.

Reference table переиспользует canonical `wb_finance_weekly_aggregates`, их classifier и signed-компоненты; отдельного классификатора справочника нет. Она всегда показывает ровно три последние полностью закрытые календарные slot-недели. Пропущенная/partial/stale неделя остаётся `—` и не заменяется более старой; combined каждой строки использует все READY COMPLETE недели внутри этих трёх слотов, от одной до трёх. Никакая частичная строка или день не входит в числитель/знаменатель, missing не становится zero, а canonical zero остаётся валидным. Каждая строка делится на единый denominator `net_revenue`; combined считается только как direct `SUM(amount) / SUM(net_revenue)`, а не arithmetic mean weekly percentages, и показывает coverage `N из 3` плюс contributing ranges. При нуле READY недель combined остаётся пустым.

В тех же трёх недельных колонках справочного UI и в объединённой колонке находится информационная строка `Расчётный выкуп (подтверждённый)`. Она не использует Finance denominator: backend берёт ровно три последние закрытые Monday-Sunday slot-недели в canonical `Asia/Yekaterinburg`, публикует каждую неделю только когда все семь дат не новее D-6 trusted cutoff и имеют полное valid mature coverage по enabled SKU, а затем считает `SUM(buyoutPercent * orderCount) / SUM(orderCount)` только по positive-`orderCount` SKU-day. Missing/immature/invalid дата делает недельную ячейку `—`; такая неделя не заменяется старшей, не считается частично и полностью исключается из combined. Combined публикуется по всем READY неделям в трёх слотах (одна–три) прямым SKU-day SUM/SUM, не arithmetic mean процентов; при нуле READY недель это `—`. UI показывает `N из 3`, contributing и pending ranges. `buyoutCount` не является весом, current open week исключена. Canonical historical восстановление использует только official Seller Analytics CSV `DETAIL_HISTORY_REPORT`: polluted mature окно `2026-07-22..2026-08-03` прошло полный `33 × 13` coverage proof и guarded exact-date replacement, а production readback bounded-ил отдельный `2026-07-13..2026-07-21` official refetch для `33 × 9` старых same-day-provenance rows. Timestamp-only rewrite, legacy finance/sheet rows и частичные данные не являются fallback. Строка read-only и не меняет `buyout_rate`, version history, targeted recalculation или формулу Proxy Profit 3.

Построчный audited contract справочника:

| Строка | Canonical source | Знак и учёт в Proxy 3 |
|---|---|---|
| Агентское вознаграждение WB | `agent_remuneration`, совместимый fallback `commission` | Signed expense; acquiring уже исключён classifier-ом и второй раз не вычитается |
| Эквайринг | `acquiring` | Signed expense; отдельная ставка |
| Логистика | `logistics` | Signed expense; отдельная ставка |
| Хранение | `storage` | Signed expense; отдельная ставка |
| Штрафы | `penalties` | Signed deduction; компонент penalties/adjustments |
| Корректировки (расходы) | `corrections` | Signed period correction; компонент penalties/adjustments |
| Подписки | `subscriptions` | Signed deduction; компонент other expense |
| Платные сервисы | `paid_services` | Signed deduction; компонент other expense |
| Баллы за отзывы | `review_points` | Signed deduction; компонент other expense |
| Прочие удержания | `other_deductions` | Signed deduction; компонент other expense |
| Маркетинг | `marketing` | Справочно: Proxy 3 вычитает canonical `ads_sum` отдельно |
| Платная приёмка — начислено | `acceptance` | Справочно: proven capitalized share исключается, неподтверждённый остаток остаётся расходом периода |
| Платная приёмка — капитализировано | `capitalized_acceptance` | Положительная proven capped часть; уже в canonical WB cost и повторно в ставку не входит |
| Транзитная логистика — начислено | `transit_logistics` | Справочно: proven capitalized share исключается, неподтверждённый остаток остаётся расходом периода |
| Транзитная логистика — капитализировано | `capitalized_transit_logistics` | Положительная proven capped часть; уже в canonical WB cost и повторно в ставку не входит |
| Корректировки и дополнительные выплаты (+) | `positive_adjustments` | Signed income adjustment, не расходная ставка |
| Контроль корректировки вознаграждения WB | `wb_remuneration_adjustment` | Контрольная disclosure; не складывается повторно с агентским вознаграждением или корректировками |

Все expense reversal/refund сохраняют канонический signed знак и уменьшают соответствующую строку; `abs()` запрещён. Reference UI публикует для каждой строки source fields, sign rule, denominator, aggregation rule и inclusion/capitalization note. Это только исправление справочного отображения: формула и сохранённые versioned settings Proxy 3 не меняются.

## 2.1 Immutable automatic Proxy V4 parameters

Отдельный блок `Proxy прибыль и маржинальность · V4` не переиспользует mutable V3 settings. Его product boundary фиксирован кодом как `2026-08-01`; до неё V4 resolver всегда возвращает NULL. В UI нет ручной даты начала. Только `tax_rate` остаётся ручным: preview и save автоматически привязывают новую immutable revision к текущей business date в `Asia/Yekaterinburg`; прошлые V4 даты не пересчитываются. Все остальные ставки, total included expenses, retained share, source range, status/version/effective-from metadata read-only.

Automatic revision использует exact пересечение READY COMPLETE Buyout и Finance недель среди трёх последних закрытых Monday-Sunday слотов. В version входит от одной до трёх общих недель; partial/immature/missing slot полностью исключается и не становится нулём. Buyout — прямой `SUM(buyoutPercent × orderCount) / SUM(orderCount)` по mature-proven D-6 enabled SKU-day с positive orders. Finance — прямой `SUM(signed amount) / SUM(net_revenue)` по тому же набору недель и полному seller/classifier coverage. Source-window fingerprint включает exact contributing ranges и digests обоих source payloads; повторный refresh того же окна idempotent, а появление готовой пропущенной недели создаёт ровно одну новую immutable revision с фактической materialization business date. При нуле общих READY недель новая revision не создаётся: resolver сохраняет последнюю подтверждённую версию либо fail-closed до первой версии. Изменение данных уже frozen range возвращает `historical_repair_required`; обычный rollover не является скрытым backfill. Занятый общий warehouse writer возвращает pending status, а не ломает Витрину.

V4 expense composition фиксирована так:

- `agent_remuneration_rate = SUM(agent_remuneration|commission) / SUM(net_revenue)`, acquiring исключён;
- acquiring, ordinary customer logistics и storage — отдельные signed rates;
- `penalties_adjustments_rate = SUM(penalties + corrections) / SUM(net_revenue)`;
- `other_expense_rate = SUM(subscriptions + paid_services + review_points + other_deductions + acceptance − capitalized_acceptance + transit_logistics − capitalized_transit_logistics) / SUM(net_revenue)`;
- marketing исключён, потому что `ads_sum` вычитается отдельно; positive adjustments и `wb_remuneration_adjustment` исключены; капитализированные acceptance/transit уже входят в canonical WB WAC и второй раз не вычитаются.

Initial historical materialization — отдельная production-mutation. После official repair `2026-07-06..2026-07-12` она создаёт as-of revisions effective `2026-08-01` (source window `2026-07-06..2026-07-26`) и `2026-08-08` (source window `2026-07-13..2026-08-02`), затем пересчитывает только V4 rows существующих ready snapshots начиная с boundary. Она использует dry-run manifest, exact deployed SHA/human gate, pre-change digests, verified backup, V3/non-target invariants, atomic CAS apply, idempotency and post-apply reconciliation; future-known coefficients не проецируются назад.

После materialization обычный full refresh переносит каждую уже опубликованную V4 SKU/TOTAL ячейку для business date строго раньше текущей из предыдущего ready plan byte-semantically; ранее пустая ячейка остаётся пустой. Только текущая business date может рассчитываться с новой effective version. Это сохраняет as-of историю и не блокирует обновление order/ads/cost или других V3/non-V4 строк. Если старый refresh уже переписал frozen V4 history, исправление выполняет только `apps/sheet_vitrina_v1_proxy_v4_reconcile.py`: dry-run берёт exact reviewed initialization manifest и bounded past-date window, сравнивает V4-only digests, затем apply требует exact deployed SHA и отдельный human gate, shared-writer lock, verified backup, compare-and-swap, V3/version/non-target invariants, readback и idempotent repeat. Ordinary refresh никогда не запускает этот reconcile автоматически.

# 3. Proxy 3 formula

Public Proxy 3 contract применяется ко всей активной истории. Для даты до `2026-07-01` canonical cost и versioned calculation parameters, effective на `2026-07-01`, проецируются назад; order/ads operands остаются значениями фактической исторической даты. На/после границы используются exact-date cost и effective settings. Legacy Proxy 2 definitions сохраняются только как technical audit и никогда не подменяют Proxy 3. Для SKU/date:

```text
expected_buyout_revenue = orderSum × buyout_rate
expected_buyout_qty     = orderCount × buyout_rate
included_expense_rate   = SUM(enabled versioned expense rates)
proxy_profit_3           = expected_buyout_revenue × (1 − included_expense_rate)
                           − expected_buyout_qty × canonical_WB_WAC
                           − ads_sum
proxy_margin_3           = proxy_profit_3 / expected_buyout_revenue
```

Advertising is not multiplied by buyout rate. Missing required operand remains NULL; it does not become zero. Zero expected revenue returns NULL margin. TOTAL:

- profit = `SUM(SKU proxy profit)`;
- expected revenue = `SUM(SKU expected buyout revenue)`;
- margin = `total profit / total expected revenue`;
- SKU margins are never averaged.

Public keys remain `our_wb_unit_cost_rub`, `proxy_profit_3_rub`, `proxy_margin_3_pct` and their existing TOTAL keys. `our_wb_cost_confirmed_share_pct`, Proxy 2 and old inventory-return metrics are archived at both catalog/read-contract boundaries; persisted legacy rows may remain only as technical evidence and are removed by the guarded economics cutover.

## 3.1 Proxy 4 formula

Для SKU/date on/after `2026-08-01` используется только effective immutable V4 revision:

```text
expected_buyout_revenue = orderSum × V4 buyout_rate
expected_buyout_qty     = orderCount × V4 buyout_rate
proxy_profit_4          = expected_buyout_revenue × (1 − included_expense_rate)
                          − expected_buyout_qty × canonical_WB_WAC
                          − ads_sum
proxy_margin_4          = proxy_profit_4 / expected_buyout_revenue
```

Любой missing operand оставляет SKU V4 blank; zero expected revenue даёт blank margin. TOTAL profit — сумма только eligible SKU profits, TOTAL expected revenue — сумма соответствующих SKU expected revenues, TOTAL margin — их ratio, никогда не среднее SKU margins. Public pairs `proxy_profit_4_rub`/`total_proxy_profit_4_rub` и `proxy_margin_4_pct`/`proxy_margin_4_pct_total` представлены как два общих picker item без отдельного duplicated TOTAL item. Proxy 3 keys/formula/history остаются полностью прежними.

## 3.2 Legacy group COST_PRICE / Proxy 1 boundary

`cost_price_rub` is not a warehouse WAC. Its source is the separately uploaded group-level `COST_PRICE` dataset resolved by `group + max(effective_from <= slot_date)`; `avg_cost_price_rub` aggregates those group-resolved SKU values. Proxy 1 directly consumes that value through the fixed historical coefficients `0.5096/0.91`, then feeds `proxy_margin_pct` and their TOTAL rows.

The complete dependency closure — `cost_price_rub`, `avg_cost_price_rub`, `profit_proxy_rub`, `proxy_profit_rub`, `total_proxy_profit_rub`, `proxy_margin_pct`, `proxy_margin_pct_total` — is audit-only. Central catalog/read filtering excludes it from active Web Vitrina rows, filters, settings/picker, activity/source status and group refresh. Accepted COST_PRICE upload rows/current-state and previously persisted ready rows are retained for reproducibility; no production business-data cleanup is implied. Finance already uses the shared canonical WB-cost resolver and cannot fall back to this legacy family.

# 4. Quality and consumers

Daily cost stores quality/provenance (`direct 24.06`, `same purchase price`, `interpolation`, `extrapolation`, `fallback average`, confirmed downstream layers, `business_approved_archival_estimate`). Vitrina does not invent a value when a required persisted source is truly absent. All active direct consumers, including товарный капитал, его рентабельность, web-vitrina, Finance, Partner, Proxy 3, Proxy 4 and `Управление SKU`, call the same temporal functional projection; independent retro maps and hidden fallback to 1C/legacy cost are prohibited.

Late transit, FF services, storage, paid WB acceptance, supplier financial rows and bank commissions bind to the originating shipment/supply cost layer. Transit/services/storage allocate over the full corrected sent composition; paid acceptance allocates over accepted quantity. Their business date is source provenance, not upload time. A late component queues one coalesced affected-SKU revision and rebuilds dependent WAC/capital/COGS/Finance/Proxy history without another physical movement. Confirmed zero is distinct from missing/not-requested/updating/not-found/source-error/session-expired; every unknown state stays `null`, never `0 ₽`.

Supplier financial-document exclusion is an active-source correction, not a
presentation filter. Excluded parent documents and their expense lines are
absent before supplier FF layer materialization, so the active same-SHA source
is counted once and archived capital cannot survive in WB WAC, товарный
капитал, Proxy 3 or Finance. Dependent consumers change only through a newly
published fingerprint-matching functional version; queued/error/stale supplier
proof never exposes the old numeric cost as current. No unrelated historical
Finance backfill is implied by such a correction.

WB supply cost materialization restores canonical normalized supply facts from
`normalized_row_json` before classifying transit cost. A positive official
transit fact therefore remains first-party evidence; a persisted successful
Seller Portal network enrichment is joined only as the bounded supplemental
fallback when the official amount is absent. Both paths feed the same
per-supply/SKU cost layer and full packed-composition denominator.

Finance has no separately valued cost source. Its shared consumer resolver projects the exact same-`nmId` canonical WB WAC from `2026-07-01` backwards for every Finance operation before that date, and uses the exact canonical operation-date row from `2026-07-01` onward. A missing 01.07 row is a blocker unless the same `nmId` is present in the active migration-109 archival manifest. That manifest is a warehouse-domain canonical cost source, not a Finance fallback: it pins owner approval, effective date, 100 ₽, target/source digests and fingerprints. With no later factual cost basis the last valid 100 ₽ survives zero stock and returns; a real accepted quantity/capital layer resumes ordinary moving WAC and supersedes the estimate. No quantity, capital, supply or movement is created by the estimate. Finance still cannot choose a later/other-SKU/average/legacy/zero value. Existing `wb_finance_retro_cost_map` rows from a superseded migration are ignored historical evidence, not a parallel source.

The Partner V4 marketing/classifier recovery does not alter this resolver, its 01.07 boundary, Vitrina values or Proxy 3. It rebuilds only Finance-derived projections under the same canonical cost digest and changes which already classified signed expenses Partner routes to its main/subrows.

# 5. Migration boundary

Legacy module-40 opening/supply/daily rows and the separate canonical-cost baseline stay immutable audit evidence. In particular migration 109 does not edit the frozen opening map: append-only version/row/active audit supplies a bounded overlay and only already materialized exact-target daily cost rows are corrected with their quantities preserved. `warehouse_functional_cutover_v1` activates the single warehouse/cost engine and initial settings version atomically. The bounded historical backfill may rewrite only `our_wb_unit_cost_rub`, Proxy 3 and direct dependent read models over its reviewed date scope. Before `2026-07-01` it publishes only the retrospective cost/true Proxy 3 read projection and never invents six-stage warehouse history; it removes only the centrally enumerated archived metric rows, preserves every other non-target snapshot cell/digest, pins the exact ready-snapshot manifest and is idempotent.

Non-goals: accounting FIFO, event-based WB customer movements, Proxy 2 substitution before the boundary, marketing as a percentage, transit double count, Google Sheets/GAS truth or ad-hoc production SQL.

## Late Seller Portal transit cost

Late Seller Portal success materializes only explicitly named supplies. Full
packed composition is the transit allocation denominator; accepted quantity
is the accepted-capital multiplier. The stable fact revision enqueues replay
from the supply's originating business date for exact affected SKU. The replay
may update dependent WAC/capital/COGS/Finance/Proxy/Vitrina projections, but
never quantity, reservation, FF debit or physical events and never performs a
global/current-cost backcopy. An identical revision is T0; materialization or
enqueue failure is durable and retryable.
