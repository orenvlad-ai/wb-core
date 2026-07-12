---
title: "Модуль: own_product_capital_block"
doc_id: "WB-CORE-MODULE-45-OWN-PRODUCT-CAPITAL-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать независимый от 1С management-контур `Товарный капитал — наши данные` по фактически оплаченным затратам и физическим стадиям товара."
scope: "Persisted event/state contour, Decimal materialization по SKU/TOTAL, payment/SKU hard gates, FF moving weighted average, WB underacceptance reconciliation, server-derived confirmation presentation и безопасный bounded backfill runner."
source_basis:
  - "docs/modules/34_MODULE__SUPPLIER_SHIPMENTS_BLOCK.md"
  - "docs/modules/36_MODULE__WB_SUPPLIES_BLOCK.md"
  - "docs/modules/39_MODULE__FULFILLMENT_SERVICES_BLOCK.md"
  - "docs/modules/40_MODULE__OUR_WB_COST_MODEL_BLOCK.md"
  - "docs/modules/43_MODULE__FF_STOCK_LEDGER_BLOCK.md"
related_modules:
  - "packages/application/own_product_capital.py"
  - "packages/application/sheet_vitrina_v1_own_product_capital.py"
  - "packages/application/cny_ledger.py"
  - "packages/application/supplier_financial_documents.py"
  - "packages/application/supplier_shipments.py"
  - "packages/application/ff_stock_ledger.py"
  - "packages/application/sheet_vitrina_v1_live_plan.py"
related_tables:
  - "sheet_vitrina_v1_own_capital_payment_layers"
  - "sheet_vitrina_v1_own_capital_events"
  - "sheet_vitrina_v1_own_capital_wb_outstanding"
  - "sheet_vitrina_v1_own_capital_daily_state"
  - "sheet_vitrina_v1_own_capital_blockers"
  - "sheet_vitrina_v1_own_capital_expense_certifications"
related_endpoints:
  - "POST /v1/sheet-vitrina-v1/product-capital/recalculate"
  - "GET /v1/sheet-vitrina-v1/product-capital/status"
related_runners:
  - "apps/own_product_capital_smoke.py"
  - "apps/own_product_capital_backfill.py"
related_docs:
  - "migration/98_own_product_capital.md"
  - "docs/architecture/10_hosted_runtime_deploy_contract.md"
source_of_truth_level: "module_canonical"
update_note: "Модуль вводит отдельную WebCore-методологию вложенного товарного капитала и не меняет 1С, proxy2 или proxy3 truth."
---

# 1. Назначение и экономический смысл

`Товарный капитал — наши данные` — management invested-capital contour. Он отвечает на вопрос, сколько собственных денег фактически вложено в товар на выбранную дату и где физически находится соответствующий оплаченный эквивалент товара.

Это не бухгалтерский FIFO, не налоговый учёт и не физическое box/lot tracing. Контур не зависит от 1С. Существующий блок 1С, `proxy_profit_2_rub`, `proxy_profit_3_rub` и их исходная семантика сохраняются; WebCore-блок расположен рядом для параллельного сравнения.

В капитал попадают только исполненные платежи и расходы с фактической `effective_date`. Обязательство, КП, обычный invoice, packing list или будущий неоплаченный расход сами по себе капитал не создают. Upload time никогда не подменяет дату платежа. Поздно загруженный документ пересчитывает историю от подтверждённой даты исполнения, а реально поздний платёж не меняет предыдущие дни.

# 2. Пять пользовательских стадий

Стадии ровно пять и взаимоисключающие:

1. `На производстве`;
2. `Производство → ФФ`;
3. `На ФФ`;
4. `ФФ → WB`;
5. `На WB`.

Для каждой стадии материализуются по SKU и TOTAL капитал, количество, средневзвешенная стоимость и подтверждённая доля. Дополнительно материализуются общие количество/капитал/средняя стоимость/confirmed share и рентабельность `proxy_profit_3_rub / own_total_product_capital_rub`. TOTAL-проценты всегда являются отношением агрегатов; missing или нулевой denominator даёт blank.

`Недопринято WB` хранится как server-owned diagnostic substate стадии `ФФ → WB`, но не является шестой стадией.

# 3. Оплата supplier invoice и ownership layers

Каждый исполненный CNY-платёж создаёт неизменяемый ownership/cost layer:

- `incremental_paid_share = paid_cny / invoice_total_cny`;
- cumulative share не может быть отрицательной или превышать 1;
- paid-equivalent quantity распределяется пропорционально по всем полностью сопоставленным product lines;
- RUB-капитал распределяется по invoice value строк, включая существующий механизм общих invoice/order costs;
- новый платёж использует собственные фактические курс и дату и сразу появляется в стадии, фактически активной на эту дату.

Частичная оплата штатна и сама по себе не означает неполноту purchase-cost boundary. Overpayment, отсутствующая payment→shipment связь, неоднозначная allocation или конфликт одного `payment_id` fail closed и создают blocker.

# 4. Движение и стоимость

Фактические границы событий:

- supplier ownership — дата исполнения платежа;
- `Производство → ФФ` — `actual_shipment_date`;
- `На ФФ` — `actual_ff_acceptance_date`;
- `ФФ → WB` — idempotent фактическое FF writeoff/movement event;
- `На WB` — фактическое accepted quantity evidence;
- доприёмка — отдельное reconciliation event.

На ФФ применяется moving weighted average по SKU. Приход с новой стоимостью меняет среднюю; списание фиксирует текущую среднюю в cost snapshot, но не меняет среднюю оставшегося количества. Confirmed/estimated quantity и capital двигаются согласованно.

Для ordinary WB supply `in_transit = sent - accepted`. Status `4` переносит только cumulative accepted quantity; каждое увеличение создаёт только delta-event, а повтор того же значения даёт zero delta. Переходы `3 → 4 → 5` используют один FF debit и один cost snapshot, поэтому принятая часть не остаётся в транзите и не учитывается дважды. Acceptance date валидируется до любой записи и не может быть раньше FF writeoff date; нарушение оставляет zero supply events и fail-closed blocker. Регресс accepted quantity также fail closed. Финальная положительная разница создаёт outstanding row с original supply, SKU/nmID, warehouse/destination, original cost layer и event dates. `Допринято` не списывает ФФ повторно: direct upstream identity имеет приоритет, иначе применяется строгий `warehouse + destination + SKU` FIFO; outstanding с final acceptance позже даты `Допринято` не является кандидатом. Ordinary surplus, ambiguous identity и нарушение инварианта блокируются без fabricated allocation. Historical backfill дополнительно хранит `physical_*` outstanding рядом с tracked paid outstanding: фактическая доприёмка расходует physical остаток и переносит не больше tracked paid остатка; только превышение подтверждённого physical остатка является surplus blocker.

На WB official current stock quantity сочетается с существующими `our_wb_unit_cost_rub` и bucket-based confirmed-share данными. Компоненты transit/FF services/storage добавляются только как фактически оплаченные и без двойного учёта.

# 5. Документы, SKU и подтверждение

Payment documents проходят parse-preview до durable save. Amount, currency и прочие financial fields нельзя исправлять вручную. Любое нераспознанное обязательное поле, кроме даты, отклоняет файл целиком. Если отсутствует только дата, UI требует её ввода; сохраняются provenance, actor и confirmation timestamp. Дедупликация исключает повторное признание. Уже признанный payment document нельзя удалить без будущего audited reversal contour.

Документ с SKU distribution принимается атомарно только при 100% deterministic matching всех строк. Unmatched/ambiguous SKU, missing qty/price или неизвестный WB nmID блокируют financial/stock/capital movement целиком. Pseudo-SKU, буфер `Не сопоставлено`, partial save и ручное случайное распределение запрещены.

`expenses_complete` — certification полноты, а не сумма и не дата. Доступные фактические платежи показываются всегда; незавершённая applicable cost chain даёт server-derived yellow state и краткую причину. Любая cost-affecting mutation сбрасывает certification. При этом распознанные factual logistics invoices и customs/tax/VAT lines создают отдельные cost-payment events с датой документа; подтверждённые direct-RUB bank-fee rows используют собственную operation date, а CNY fees остаются единожды в CNY-ledger path. Суммы распределяются по SKU пропорционально invoice value, сохраняют financial-document/line provenance и deterministic dedupe key. Exclusion/delete уже признанного expense требует будущего audited reversal. Quote, unconfirmed bank statement и Fulfillment `К оплате` без factual payment date капитал не создают. Confirmed share по SKU и TOTAL quantity-weighted. Изменение certification пересчитывает presentation history, но не добавляет расходы до их effective dates.

# 6. Persisted history, audit и backfill boundary

Mutable source status не является историей. `payment_layers`, `events`, `wb_outstanding`, `daily_state`, `blockers` и `expense_certifications` образуют persisted event/state contour с Decimal-compatible TEXT values, evidence fingerprints и idempotent identities.

`apps/own_product_capital_backfill.py` — единственный repo-owned исторический runner. Он default dry-run, требует bounded date scope, а apply дополнительно требует exact fingerprint и explicit backup directory. Candidate восстанавливает ownership layers из уже persisted posted CNY operations, supplier fact boundaries, factual expense documents и только тех WB cache rows, для которых существует canonical FF-ledger debit evidence; CNY ledger, FF quantity operations и WB cache при этом не replay/rewrite. WB movements раньше первой persisted positive supplier-payment ownership date являются pre-contour physical history и пропускаются с отдельным diagnostic counter, а не сопоставляются с будущими capital layers. После этой границы полная физическая sent/accepted quantity не превращается в оплаченный капитал автоматически: historical movement использует `min(physical quantity, paid FF capital available)` и сохраняет bounded diagnostics для неоплаченной/overaccepted части. Historical final acceptance сохраняет раздельные physical и tracked paid outstanding; `Допринято` атомарно расходует фактический physical остаток, переносит только существующий paid-capital остаток и аудирует untracked physical часть как zero-capital quantity. Это не partial apply и не fabricated capital: весь candidate остаётся одной fingerprinted transaction; превышение physical outstanding блокирует apply, а строка без единого matching outstanding является внешней pre-contour историей и пропускается. Blocker diagnostics содержат bounded nmID/requested/candidate original supply/final date/tracked-open/physical-open evidence без raw payload. Confirmed bank statement без direct-RUB строк также не blocker: его CNY fees уже принадлежат deduplicated CNY-ledger path; direct-RUB строка с отсутствующей суммой/датой остаётся blocker. Runner выполняет coherent online backup с `integrity_check=ok` и mode `0600`, затем один `BEGIN IMMEDIATE` in-place apply: transactionally rechecks source/target/external digests, copies the complete own-capital event/state contour plus only bounded daily rows, verifies bounded/out-of-scope own rows and an explicit 1C/proxy2/proxy3 preservation digest, then commits only при полном совпадении. Live SQLite inode не меняется; WAL readers допустимы; exception откатывает весь scope. Force/partial mode и `os.replace` отсутствуют; unresolved source blockers fail closed, post-run integrity и второй zero-change run обязательны.

Подтверждённая historical orphan-строка имеет отдельную exact classification, а не ослабление FIFO: только одновременное совпадение `supply_id=40654176`, `effective_date=2026-07-06`, полного состава документа `{391660889:1, 391663632:1}`, orphan `nmID=391663632 / qty=1`, warehouse `Склад Шушары`, отсутствующего `original_supply_id`, первого FIFO candidate `40433285` и нулевых tracked/physical/paid outstanding создаёт zero-quantity/zero-capital audit event `wb_reconciliation:40654176:historical_orphan:391663632` с причиной `historical_orphan_doprinato_zero_capital`. Другая строка документа (`391660889`) обязана пройти ordinary outstanding transfer; любое отличие guard блокирует документ целиком. Classification не пишет quantity ledger ФФ, не меняет `40561872`, не переносит случайный cost layer и остаётся idempotent по event identity.

Production dry-run/apply не является частью merge самого модуля. Его разрешает и выполняет только отдельный merge/deploy-координатор после merge, canonical deploy и проверки backup/runtime boundary. Неподтверждённые исторические даты не выводятся из upload time и остаются blank/warning.

Публикация уже materialized истории в web-vitrina выполняется существующим source-group contour `webcore_product_capital`. Только эта группа вправе выбрать persisted ready snapshot из предыдущего bundle, когда для даты ещё нет current-bundle snapshot: partial plan содержит только `own_product_capital` metrics/status, merge сохраняет все чужие rows/cells/metadata и затем записывает новый current-bundle snapshot. Для WB API, 1С, Seller Portal и прочих групп cross-bundle доступ не расширяется; после первой WebCore-публикации дата становится обычным current-bundle snapshot. Это bounded historical publication, а не full upstream refresh и не переписывание accepted source truth.

# 7. Явные non-goals

- бухгалтерский FIFO и налоговый учёт;
- box/lot tracing;
- шестая пользовательская стадия;
- pseudo-SKU, unmatched buffers или random allocation;
- Google Sheets/GAS и browser/localStorage truth;
- изменение 1С-source или proxy2/proxy3 rows;
- server-only hotfix, ad-hoc SQL/SSH или production mutation вне repo-owned runner.
