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
source_of_truth_level: "canonical_module_contract"
update_note: "Введены compact typed component history, latest closed-day supersession, partial numeric totals and a dry-run-first exact-manifest backfill."
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

`Остатки общие = Остатки WB + available всех applicable FBS facilities`.
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

# 3. Time and quality semantics

Current business day читает latest accepted capture по durable insertion sequence. Closed day читает latest
append-only finalization общего `yesterday_closed` contract. Поздняя принятая
revision создаёт новую capture и superseding finalization; предыдущие capture,
finalization и provenance не изменяются и не удаляются. Stock-specific cutoff,
таймер или отдельная кнопка не вводятся.

Facility становится applicable только с independently evidenced
first-stock/launch date. До этой даты её state `inapplicable`, обычное
отображение `—` и нет warning. До первой applicable FBS facility общая сумма
равна WB и имеет `full` quality. Inactive facility с остатком продолжает
участвовать; inactive без остатка/applicability не создаёт synthetic operand.

Если все applicable operands exact, публикуется numeric `full`. Если часть
missing, публикуется сумма известных operands с `partial` и exact missing list.
В строке missing facility показывается `—`; доказанный ноль показывается `0`.
Partial total остаётся числом с маленьким нейтральным `◐`; tooltip/ARIA
перечисляет missing components. Для `full` marker отсутствует. Browser не
вычисляет business truth и не вводит current/final/health styling.

# 4. Capture and read path

`RegistryUploadDbBackedRuntime.save_sheet_vitrina_ready_snapshot(...)` атомарно
сохраняет ready snapshot и вызывает единый history capture path. Поэтому
manual `Загрузить`, automatic refresh и group refresh используют один writer;
отдельного history schedule/button нет. Read-side выбирает bounded date window,
читает latest finalized/captured revision из canonical SQLite в
`mode=ro + query_only` и передаёт уже materialized values/quality в существующую
Web Vitrina row surface. Полный ledger на page request не replay-ится.

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

Apply не наследует разрешение на deploy/dry-run. Он требует отдельный exact
human gate, trusted-main deployed runner, reviewed manifest SHA-256, deployed
SHA/schema/generation/source-watermark/target-history CAS, canonical writer
lock и coherent verified target-scoped `0600` before-image. Full-store/T3
backup для этой bounded append-only mutation запрещён. Write allowlist ограничен четырьмя
history tables; операция append/supersede-only. Manifest hash даёт idempotent
no-op, а ambiguous transport проверяется через applies/finalization readback —
blind replay запрещён.

# 6. Verification

- `apps/inventory_planning_read_model_smoke.py` — physical/reserved operands,
  partial known sum, inactive residual and seller-readback exclusion;
- `apps/sheet_vitrina_v1_inventory_planning_smoke.py` — public identities,
  order, TOTAL/SKU and no duplicate aggregate;
- `apps/sheet_vitrina_v1_inventory_planning_browser_smoke.py` — user visibility,
  partial marker/tooltip and full-value marker absence;
- `apps/sheet_vitrina_v1_inventory_history_smoke.py` — idempotent capture,
  exact zero/missing, archived SKU, WB-only prelaunch full, late supersession,
  indefinite retention and 174-day × 34-scope realistic window;
- `apps/sheet_vitrina_v1_inventory_history_backfill_smoke.py` — Moscow/Orenburg
  applicability/exact boundaries, full/partial partitions, dry-run byte safety,
  guarded apply, reconciliation and idempotent replay.
