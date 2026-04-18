---
title: "Модуль: sales_funnel_history_block"
doc_id: "WB-CORE-MODULE-08-SALES-FUNNEL-HISTORY-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `sales_funnel_history_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для historical sales funnel, включая current exact-date runtime seam для server-owned factory-order history."
source_basis:
  - "migration/45_sales_funnel_history_block_contract.md"
  - "migration/48_sales_funnel_history_block_legacy_sample_source.md"
  - "artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md"
  - "apps/sales_funnel_history_block_smoke.py"
  - "apps/sales_funnel_history_block_http_smoke.py"
related_modules:
  - "packages/contracts/sales_funnel_history_block.py"
  - "packages/adapters/sales_funnel_history_block.py"
  - "packages/application/sales_funnel_history_block.py"
  - "packages/application/factory_order_sales_history.py"
related_tables:
  - "temporal_source_snapshots"
related_endpoints:
  - "POST /api/analytics/v3/sales-funnel/products/history"
related_runners:
  - "apps/sales_funnel_history_block_smoke.py"
  - "apps/sales_funnel_history_block_http_smoke.py"
  - "apps/sales_funnel_history_block_batching_smoke.py"
  - "apps/factory_order_sales_history_smoke.py"
  - "apps/factory_order_sales_history_reconcile.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/45_sales_funnel_history_block_contract.md"
  - "migration/46_sales_funnel_history_block_parity_matrix.md"
  - "migration/47_sales_funnel_history_block_evidence_checklist.md"
  - "migration/48_sales_funnel_history_block_legacy_sample_source.md"
  - "artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Обновлён под current exact-date runtime seam для factory-order: merged official-api checkpoint блока `sales_funnel_history_block` теперь по-прежнему отвечает за truthful historical payload и bounded batching по `nmIds` / date windows, а server-owned consumers могут split-ить success payload на exact-date snapshots и persist-ить их в `temporal_source_snapshots`; live `DATA_VITRINA` допускается только как one-time bounded migration input для historical window reconcile, но не как постоянный source of truth."
---

# 1. Идентификатор и статус

- `module_id`: `sales_funnel_history_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `POST /api/analytics/v3/sales-funnel/products/history` + current RAW/APPLY semantics.
- Результат задаётся на уровне `date + nmId + metric`.
- Ключевая semantics:
  - apply берёт latest `fetched_at` per `(date,nmId,metric)`
  - percent metrics `addToCartConversion`, `cartToOrderConversion`, `buyoutPercent` нормализуются делением на `100`
  - empty-case определяется как item с пустым `history`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `date_from`
  - `date_to`
  - `count`
  - `items[]` с `date`, `nm_id`, `metric`, `value`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded historical sales funnel snapshot без переноса старой orchestration-логики.
- Current HTTP adapter keeps the same external request/response contract and still splits wide periods into bounded date windows before calling official API.
- Success payload также пригоден для server-owned exact-date persistence:
  - current factory-order helper split-ит `success.items[]` по `item.date`;
  - дальше каждый exact-date slice может truthfully сохраняться в `temporal_source_snapshots[source_key=sales_funnel_history]` без изменения business contract самого official-api блока.
- This batching only removes per-request span pressure; it does **not** bypass the current live authoritative depth boundary of the upstream source.
- If a bounded historical window is migrated from live `DATA_VITRINA`, that sheet acts only as one-time migration input for exact-date replacement/reconcile; ongoing source of truth remains official API payload + server-owned runtime snapshots.
- If the upstream source rejects older start days relative to current business date, the server-owned consumer must surface that boundary truthfully instead of inventing backfill or approximate history.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/sales_funnel_history_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/sales_funnel_history_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/sales_funnel_history_block/target/normal__template__target__fixture.json`
  - `artifacts/sales_funnel_history_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/sales_funnel_history_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/sales_funnel_history_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/sales_funnel_history_block/evidence/initial__sales-funnel-history__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/sales_funnel_history_block.py`
- adapters: `packages/adapters/sales_funnel_history_block.py`
- application: `packages/application/sales_funnel_history_block.py`
- runtime-backed consumer helper: `packages/application/factory_order_sales_history.py`
- artifact-backed smoke: `apps/sales_funnel_history_block_smoke.py`
- authoritative server-side smoke: `apps/sales_funnel_history_block_http_smoke.py`
- batching/rate-limit smoke: `apps/sales_funnel_history_block_batching_smoke.py`
- runtime/reconcile smoke: `apps/factory_order_sales_history_smoke.py`
- bounded reconcile runner: `apps/factory_order_sales_history_reconcile.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/sales_funnel_history_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/sales_funnel_history_block_http_smoke.py`.
- Exact-date runtime split/reconcile smoke подтверждён через `apps/factory_order_sales_history_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 140`.
- Percent-normalization и latest-`fetched_at` semantics сохранены внутри bounded target contract.
- HTTP adapter не только режет запрос по `nmIds`, но и bounded-chunk'ит длинный date range на несколько day windows с последующим merge без изменения target shape.
- Тот же target shape достаточно строг, чтобы server-owned consumers могли:
  - materialize-ить exact-date snapshots в runtime;
  - делать truthful window replacement/reconcile без merge с polluted rows;
  - затем считать покрытые averaging windows без fixed `<= 7` product rule.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.
