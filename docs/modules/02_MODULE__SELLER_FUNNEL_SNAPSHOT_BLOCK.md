---
title: "Модуль: seller_funnel_snapshot_block"
doc_id: "WB-CORE-MODULE-02-SELLER-FUNNEL-SNAPSHOT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `seller_funnel_snapshot_block`."
scope: "Legacy consumer-facing daily contract, target snapshot semantics, артефакты, кодовые части и текущие smoke-path этого web-source модуля."
source_basis:
  - "migration/20_seller_funnel_snapshot_block_contract.md"
  - "migration/23_seller_funnel_snapshot_block_legacy_sample_source.md"
  - "artifacts/seller_funnel_snapshot_block/evidence/initial__seller-funnel-snapshot__evidence.md"
  - "apps/seller_funnel_snapshot_block_smoke.py"
  - "apps/seller_funnel_snapshot_block_http_smoke.py"
  - "apps/web_source_temporal_adapter_smoke.py"
related_modules:
  - "packages/contracts/seller_funnel_snapshot_block.py"
  - "packages/adapters/seller_funnel_snapshot_block.py"
  - "packages/application/seller_funnel_snapshot_block.py"
related_tables: []
related_endpoints:
  - "GET /v1/sales-funnel/daily"
related_runners:
  - "apps/seller_funnel_snapshot_block_smoke.py"
  - "apps/seller_funnel_snapshot_block_http_smoke.py"
  - "apps/web_source_temporal_adapter_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/20_seller_funnel_snapshot_block_contract.md"
  - "migration/21_seller_funnel_snapshot_block_parity_matrix.md"
  - "migration/22_seller_funnel_snapshot_block_evidence_checklist.md"
  - "migration/23_seller_funnel_snapshot_block_legacy_sample_source.md"
  - "artifacts/seller_funnel_snapshot_block/evidence/initial__seller-funnel-snapshot__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует seller funnel daily snapshot checkpoint."
---

# 1. Идентификатор и статус

- `module_id`: `seller_funnel_snapshot_block`
- `family`: `web-source`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как consumer-facing daily contract `GET /v1/sales-funnel/daily`.
- Ключевая semantics задаётся на уровне daily snapshot `date` и item identity `nm_id`.
- В checkpoint сохранены:
  - поля `name`, `vendor_code`, `view_count`, `open_card_count`, `ctr`
  - `count` и состав `items`
  - отдельный режим `not_found`
- Current live adapter semantics для `scenario = normal`:
  - сначала делается explicit-date read `GET /v1/sales-funnel/daily?date=...`;
  - при `404` adapter пробует latest read без query params;
  - latest payload принимается только если его `date` точно совпадает с requested day;
  - иначе блок остаётся truthful `not_found` с note `resolution_rule=explicit_or_latest_date_match`.
- Hosted EU default producer for this adapter is the localhost-only owner runtime API `http://127.0.0.1:8000`; `SHEET_VITRINA_SELLER_FUNNEL_SNAPSHOT_BASE_URL` or shared `SHEET_VITRINA_WEBSOURCE_CURRENT_SYNC_API_BASE_URL` may override it only when the owner runtime is intentionally moved.
- Для `sheet_vitrina_v1` consumer path request может передавать enabled/relevant `nm_ids`:
  - raw `items` сначала фильтруются до relevant SKU rows;
  - strict validation `view_count` / `open_card_count` / `ctr` применяется только после этой фильтрации;
  - NULL/invalid поля в нерелевантных строках не валят весь snapshot и surface-ятся диагностикой `ignored_non_relevant_invalid_rows`;
  - NULL/invalid поля в relevant строках остаются strict error и проходят через existing accepted/fallback/error policy.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `date`
  - `count`
  - `items[]`
- Not-found shape:
  - `kind = "not_found"`
  - `detail`
- Целевой смысл блока: server-side daily snapshot boundary для downstream-потребителей без переноса producer-side runtime.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/seller_funnel_snapshot_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/seller_funnel_snapshot_block/legacy/not-found__template__legacy__fixture.json`
- target samples:
  - `artifacts/seller_funnel_snapshot_block/target/normal__template__target__fixture.json`
  - `artifacts/seller_funnel_snapshot_block/target/not-found__template__target__fixture.json`
- parity:
  - `artifacts/seller_funnel_snapshot_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/seller_funnel_snapshot_block/parity/not-found__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/seller_funnel_snapshot_block/evidence/initial__seller-funnel-snapshot__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/seller_funnel_snapshot_block.py`
- adapters: `packages/adapters/seller_funnel_snapshot_block.py`
- application: `packages/application/seller_funnel_snapshot_block.py`
- artifact-backed smoke: `apps/seller_funnel_snapshot_block_smoke.py`
- relevant-filter regression smoke: `apps/sheet_vitrina_v1_seller_funnel_relevant_filter_smoke.py`
- live read-side/API smoke path: `apps/seller_funnel_snapshot_block_http_smoke.py`
- targeted latest-match smoke path: `apps/web_source_temporal_adapter_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/seller_funnel_snapshot_block_smoke.py`.
- Relevant-filter regression подтверждён через `apps/sheet_vitrina_v1_seller_funnel_relevant_filter_smoke.py`.
- Текущий live read-side/API smoke path поддерживается в `main` через `apps/seller_funnel_snapshot_block_http_smoke.py`.
- Локальный adapter smoke `apps/web_source_temporal_adapter_smoke.py` подтверждает bounded semantics `explicit-date -> latest-if-date-matches` и truthful `not_found` при mismatch latest date.

# 7. Что уже доказано по модулю

- Contract-level parity подтверждена для `normal-case` и `not_found`.
- Daily snapshot semantics и item-level fields сохранены без изменения downstream boundary.
- Current seller-funnel adapter aligned with `sheet_vitrina_v1` two-slot contour without contract shift: `today_current` может materialize-иться через latest-match fallback, а previous exact day остаётся truthful `not_found`, если source не даёт requested date.
- Модуль зафиксирован в `main` как канонический seller funnel snapshot checkpoint.

# 8. Что пока не является частью финальной production-сборки

- producer/runtime внутренний path;
- jobs/API/deploy вокруг producer-side контура;
- более широкий production pipeline beyond bounded daily snapshot contract.
