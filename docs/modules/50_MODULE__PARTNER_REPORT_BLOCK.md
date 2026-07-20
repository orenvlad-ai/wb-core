---
title: "Модуль: Partner Report"
doc_id: "WB-CORE-MODULE-50-PARTNER-REPORT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать server-owned отчёт доходности одной карточки, immutable finalization и конфиденциальный доказательный XLSX/ZIP."
scope: "Finance raw rows, exact selected nmId, ads_compact, temporal COGS, allocated account expenses, operator UI, Excel and privacy verification."
source_basis:
  - "docs/modules/09_MODULE__ADS_COMPACT_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/44_MODULE__WB_FINANCE_WEEKLY_REPORT_BLOCK.md"
  - "docs/modules/48_MODULE__WAREHOUSE_STOCKS_BLOCK.md"
  - "migration/107_finance_retro_cost_and_partner_report.md"
related_modules:
  - "packages/application/partner_report.py"
  - "packages/application/wb_finance_weekly.py"
  - "packages/application/registry_upload_http_entrypoint.py"
  - "packages/adapters/registry_upload_http_entrypoint.py"
related_tables:
  - "partner_report_settings_versions"
  - "partner_report_settings_current"
  - "partner_report_finalized_reports"
  - "partner_report_audit"
related_endpoints:
  - "GET /v1/sheet-vitrina-v1/partner-report/options"
  - "POST /v1/sheet-vitrina-v1/partner-report/settings"
  - "POST /v1/sheet-vitrina-v1/partner-report/preview"
  - "POST /v1/sheet-vitrina-v1/partner-report/finalize"
  - "POST /v1/sheet-vitrina-v1/partner-report/preview-package.zip"
  - "GET /v1/sheet-vitrina-v1/partner-report/finalized"
  - "GET /v1/sheet-vitrina-v1/partner-report/finalized/{report_id}"
  - "GET /v1/sheet-vitrina-v1/partner-report/finalized/{report_id}/package.zip"
source_of_truth_level: "module_canonical"
update_note: "Добавлен single-SKU Partner Report с Decimal v1 formula, server-owned settings, continuous finalized periods, persisted loss carry, immutable evidence and fail-closed privacy scanner."
---

# 1. Назначение и границы

`Отчёты -> Партнёрский отчёт` строит `Отчёт о доходности карточки` для ровно одного canonical `nmId`. Он не создаёт партнёрскую роль, публичный кабинет или постоянную ссылку. Все routes защищены существующей server-side секцией `reports`; browser/localStorage не являются источником параметров, прав или finalized truth.

Preview допускает любой уникальный набор закрытых недель. Finalization допускает только непрерывный недельный период. Если у карточки уже есть finalized history, следующий период должен непосредственно продолжать предыдущий; его immutable `loss_carry_out` становится новым `loss_carry_in`. Gap, overlap и out-of-order finalization блокируются, поэтому отрицательная неделя не может быть незаметно исключена из выплаты. Exact retry тех же settings/weeks/sources возвращает существующий report, а другой расчёт поверх уже finalized периода запрещён. Исторический finalized report читает сохранённые значения и provenance и не пересчитывается при последующем изменении настроек, себестоимости или источников.

# 2. Server-owned параметры и хранение

Обязательные параметры: `nmId`, доля партнёра, вложенный капитал, резерв ТО, офис в неделю, расчётная ставка налога и правило общих расходов. Значения не имеют скрытых business defaults; UI placeholders не записываются. Каждое сохранение создаёт immutable version с автором, временем и fingerprint, а current pointer выбирает актуальную версию карточки.

Finalized row хранит exact week list, `nmId`, название, settings/formula versions, все weekly/period values, Finance/ads/cost manifests and digests, source coverage, время/автора, loss carry fields, выплату и ROI. Audit фиксирует settings save и finalization. Generated packages не сохраняются и выдаются только как bounded response; finalized package каждый раз собирается из immutable report/provenance и не создаёт public URL.

# 3. Источники и распределение

- Finance source — immutable `wb_finance_weekly_raw_rows`. Direct row используется один раз только после direct `nmId` или deterministic canonical alias resolution. Account-level `nmId=0` не попадает в raw partner export.
- Общий account-level расход распределяется как `selected SKU net revenue / total weekly net revenue`. Нулевая/отрицательная общая база блокирует расчёт. Internal provenance хранит source amount и coefficient; партнёр получает только category, allocated amount, rule, formula version and safe source digest.
- Advertising source — только persisted `ads_compact` snapshots с role `accepted_closed_day_snapshot` на уровне `date + nmId`. `kind=empty` означает подтверждённый zero. Missing date или successful payload без выбранного `nmId` блокирует finalization. Finance marketing deduction не вычитается одновременно с `ads_sum`.
- COGS — тот же operation-date Finance cost contract из module 44. Продажа добавляет, возврат уменьшает COGS; detail сохраняет operation date, quantity, unit cost, source date/version и signed COGS.
- Acquiring остаётся раскрытием внутри комиссии. Paid acceptance и transit с `2026-05-01` не вычитаются повторно, потому что уже входят в выбранную себестоимость.

# 4. Versioned Decimal formula

Formula version: `partner_report_profitability_v1`.

```text
card_margin = net_revenue
              - cogs - commission - logistics - ads - storage
              - other_direct_expenses - allocated_common_expenses
              + positive_adjustments

estimated_tax = net_revenue * tax_rate
replenishment_reserve = MAX(card_margin, 0) * reserve_rate
distributable_profit = card_margin - office - estimated_tax
                       - replenishment_reserve - applicable_loss_carry
partner_payout = MAX(distributable_profit, 0) * partner_share

period_roi = period_partner_payout / invested_capital
annualized_return = period_roi * 52 / selected_week_count
```

Weekly payout remains an explanatory row. Period payout is recalculated from the aggregate distributable result, so a negative selected week offsets the period and weekly positive payouts are not blindly summed. Period ROI and annualized return are recalculated from period payout and manual capital; weekly percentages are never summed.

# 5. XLSX и доказательный ZIP

The first workbook sheet intentionally follows the supplied light Excel reference: Arial-like black text, white/light-gray surface, calm borders, week columns at top, metric labels at left, compact coefficients, frozen `C2`, print area/fit and blue `Выплата партнёру` row. It contains values without macros or external workbook links.

One ZIP contains:

- `00_Партнёрский_отчёт_...xlsx`;
- one `Финотчёт_WB_...xlsx` per selected week, explicitly titled `Финотчёт WB — выборка по SKU`;
- selected-SKU ads and COGS workbooks;
- safe allocated-common-expense workbook;
- human-readable methodology/parameter manifest.

Finance export uses a safe column allowlist and contains no account-level rows or other-SKU records. A zero-operation week still gets headers and an explicit empty state.

# 6. Privacy and reconciliation gate

Before ZIP response, the server inspects filenames, all visible/hidden/very-hidden sheets, raw workbook XML/shared strings, formulas, comments, document properties, embedded members, macros and external links. Tokens for every other canonical SKU (`nmId`, vendor code, barcode, name, our SKU) are forbidden. Internal paths and credential-like fragments are forbidden. Any finding blocks delivery.

The same gate reopens the generated workbooks and reconciles their actual cells: selected Finance rows to direct report values, ads rows to `Реклама WB`, signed COGS rows and explicit weekly totals to `Себестоимость`, safe allocated totals to the report, saved parameters to the manifest and weekly/period cells to immutable totals. The negative multi-SKU fixture in `apps/partner_report_smoke.py` proves both reconciliation and leak rejection, including malicious hidden/formula/comment/metadata/embedded-object content.

# 7. Verification

- formulas, settings, immutability, XLSX/ZIP/privacy: `python3 apps/partner_report_smoke.py`;
- desktop/narrow UI, week picker, preview/finalize/package: `python3 apps/partner_report_browser_smoke.py`;
- auth boundary: `python3 apps/registry_upload_http_entrypoint_auth_smoke.py`;
- public route allowlist: `python3 apps/registry_upload_http_entrypoint_public_routes_smoke.py`.

Production UI acceptance uses preview/read-only package or a disposable contour with cleanup; it must not leave a real partner finalized record.
