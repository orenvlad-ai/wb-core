---
title: "Модуль: web_source_snapshot_block"
doc_id: "WB-CORE-MODULE-01-WEB-SOURCE-SNAPSHOT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `web_source_snapshot_block`."
scope: "Legacy consumer-facing contract, target snapshot semantics, артефакты, кодовые части и текущие smoke-path этого web-source модуля."
source_basis:
  - "migration/10_web_source_snapshot_block_contract.md"
  - "migration/17_web_source_snapshot_block_legacy_sample_source.md"
  - "artifacts/web_source_snapshot_block/evidence/initial__web-source-snapshot__evidence.md"
  - "apps/web_source_snapshot_block_smoke.py"
  - "apps/web_source_snapshot_block_http_smoke.py"
  - "apps/web_source_temporal_adapter_smoke.py"
related_modules:
  - "packages/contracts/web_source_snapshot_block.py"
  - "packages/adapters/web_source_snapshot_block.py"
  - "packages/application/web_source_snapshot_block.py"
related_tables: []
related_endpoints:
  - "GET /v1/search-analytics/snapshot"
related_runners:
  - "apps/web_source_snapshot_block_smoke.py"
  - "apps/web_source_snapshot_block_http_smoke.py"
  - "apps/web_source_temporal_adapter_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/10_web_source_snapshot_block_contract.md"
  - "migration/11_web_source_snapshot_block_parity_matrix.md"
  - "migration/12_web_source_snapshot_block_evidence_checklist.md"
  - "migration/17_web_source_snapshot_block_legacy_sample_source.md"
  - "artifacts/web_source_snapshot_block/evidence/initial__web-source-snapshot__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Добавлен как канонический модульный документ по текущему состоянию `main`; фиксирует web-source snapshot contract-level checkpoint."
---

# 1. Идентификатор и статус

- `module_id`: `web_source_snapshot_block`
- `family`: `web-source`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как current consumer-facing contract `GET /v1/search-analytics/snapshot`.
- Ключевая semantics задаётся на уровне snapshot period и item identity `nm_id`.
- В checkpoint сохранены:
  - payload shape `date_from`, `date_to`, `items`
  - item-поля `views_current`, `ctr_current`, `orders_current`, `position_avg`
  - отдельный non-fatal режим `not_found`
- Current live adapter semantics для `scenario = normal`:
  - сначала делается explicit-date read `GET /v1/search-analytics/snapshot?date_from=...&date_to=...`;
  - при `404` adapter пробует latest read без query params;
  - latest payload принимается только если его `date_from/date_to` точно совпадают с requested window;
  - иначе блок остаётся truthful `not_found` с note `resolution_rule=explicit_or_latest_date_match`.

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `date_from`
  - `date_to`
  - `count`
  - `items[]`
- Not-found shape:
  - `kind = "not_found"`
  - `detail`
- Целевой смысл блока: server-side snapshot contract для downstream-потребителей без переноса browser/acquisition runtime в этот checkpoint.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/web_source_snapshot_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/web_source_snapshot_block/legacy/not-found__template__legacy__fixture.json`
- target samples:
  - `artifacts/web_source_snapshot_block/target/normal__template__target__fixture.json`
  - `artifacts/web_source_snapshot_block/target/not-found__template__target__fixture.json`
- parity:
  - `artifacts/web_source_snapshot_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/web_source_snapshot_block/parity/not-found__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/web_source_snapshot_block/evidence/initial__web-source-snapshot__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/web_source_snapshot_block.py`
- adapters: `packages/adapters/web_source_snapshot_block.py`
- application: `packages/application/web_source_snapshot_block.py`
- artifact-backed smoke: `apps/web_source_snapshot_block_smoke.py`
- live read-side/API smoke path: `apps/web_source_snapshot_block_http_smoke.py`
- targeted latest-match smoke path: `apps/web_source_temporal_adapter_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/web_source_snapshot_block_smoke.py`.
- Текущий live read-side/API smoke path поддерживается в `main` через `apps/web_source_snapshot_block_http_smoke.py`.
- Локальный adapter smoke `apps/web_source_temporal_adapter_smoke.py` подтверждает bounded semantics `explicit-date -> latest-if-date-matches` и truthful `not_found` при mismatch latest window.

# 7. Что уже доказано по модулю

- Contract-level parity подтверждена для `normal-case` и `not_found`.
- Consumer-facing snapshot boundary перенесена без изменения downstream semantics.
- Current web-source adapter aligned with `sheet_vitrina_v1` two-slot contour without contract shift: `today_current` может materialize-иться через latest-match fallback, а previous exact day остаётся truthful `not_found`, если source не даёт requested window.
- Модуль зафиксирован в `main` как канонический web-source checkpoint.

# 8. Что пока не является частью финальной production-сборки

- browser automation и acquisition runtime;
- jobs/API/deploy вокруг producer-side контура;
- более широкий production pipeline beyond bounded snapshot contract.
