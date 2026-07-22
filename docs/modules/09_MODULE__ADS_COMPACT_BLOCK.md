---
title: "Модуль: ads_compact_block"
doc_id: "WB-CORE-MODULE-09-ADS-COMPACT-BLOCK"
doc_type: "module"
status: "active"
purpose: "Зафиксировать канонический модульный reference по уже перенесённому блоку `ads_compact_block`."
scope: "Legacy-source, target contract, артефакты, кодовые части и подтверждённый official-api checkpoint для compact ads snapshot."
source_basis:
  - "migration/49_ads_compact_block_contract.md"
  - "migration/52_ads_compact_block_legacy_sample_source.md"
  - "artifacts/ads_compact_block/evidence/initial__ads-compact__evidence.md"
  - "apps/ads_compact_block_smoke.py"
  - "apps/ads_compact_block_http_smoke.py"
  - "apps/ads_historical_recovery_smoke.py"
related_modules:
  - "packages/contracts/ads_compact_block.py"
  - "packages/adapters/ads_compact_block.py"
  - "packages/application/ads_compact_block.py"
  - "packages/application/ads_historical_recovery.py"
related_tables:
  - "temporal_source_slot_snapshots"
  - "temporal_source_closure_state"
  - "ads_historical_recovery_audit"
related_endpoints:
  - "GET /adv/v1/promotion/count"
  - "GET /adv/v3/fullstats"
related_runners:
  - "apps/ads_compact_block_smoke.py"
  - "apps/ads_compact_block_http_smoke.py"
  - "apps/ads_historical_recovery.py"
  - "apps/ads_historical_recovery_smoke.py"
related_docs:
  - "00_INDEX__MODULES.md"
  - "migration/49_ads_compact_block_contract.md"
  - "migration/50_ads_compact_block_parity_matrix.md"
  - "migration/51_ads_compact_block_evidence_checklist.md"
  - "migration/52_ads_compact_block_legacy_sample_source.md"
  - "artifacts/ads_compact_block/evidence/initial__ads-compact__evidence.md"
source_of_truth_level: "module_canonical"
update_note: "Фиксирует official-api checkpoint и bounded plan/apply/readback recovery только для отсутствующих accepted closed-day slots."
---

# 1. Идентификатор и статус

- `module_id`: `ads_compact_block`
- `family`: `official-api`
- `status_transfer`: модуль перенесён в `wb-core`
- `status_verification`: модуль проверен
- `status_checkpoint`: рабочий checkpoint подтверждён
- `status_main`: модуль смёржен в `main`

# 2. Legacy-source и legacy semantics

- Legacy-source фиксируется как `promotion/count` + `fullstats` + current RAW/APPLY semantics.
- Результат задаётся на уровне `snapshot_date + nmId`.
- Ключевая semantics:
  - raw агрегирует nested `days -> apps -> nms`
  - базовые поля `ads_views`, `ads_clicks`, `ads_atbs`, `ads_orders`, `ads_sum`, `ads_sum_price` суммируются по `(snapshot_date, nmId)`
  - apply-level derivation сохраняет `ads_cpc`, `ads_ctr`, `ads_cr`

# 3. Target contract и смысл результата

- Success shape:
  - `kind = "success"`
  - `snapshot_date`
  - `count`
  - `items[]` с базовыми ads-полями и derived `ads_cpc`, `ads_ctr`, `ads_cr`
- Empty shape:
  - `kind = "empty"`
  - `items = []`
  - `count = 0`
- Целевой смысл блока: bounded compact ads snapshot для requested `nmId` без переноса более широкого ad-runtime.

# 4. Артефакты по модулю

- legacy samples:
  - `artifacts/ads_compact_block/legacy/normal__template__legacy__fixture.json`
  - `artifacts/ads_compact_block/legacy/empty__template__legacy__fixture.json`
- target samples:
  - `artifacts/ads_compact_block/target/normal__template__target__fixture.json`
  - `artifacts/ads_compact_block/target/empty__template__target__fixture.json`
- parity:
  - `artifacts/ads_compact_block/parity/normal__template__legacy-vs-target__comparison.md`
  - `artifacts/ads_compact_block/parity/empty__template__legacy-vs-target__comparison.md`
- evidence:
  - `artifacts/ads_compact_block/evidence/initial__ads-compact__evidence.md`

# 5. Кодовые части

- contracts: `packages/contracts/ads_compact_block.py`
- adapters: `packages/adapters/ads_compact_block.py`
- application: `packages/application/ads_compact_block.py`
- artifact-backed smoke: `apps/ads_compact_block_smoke.py`
- authoritative server-side smoke: `apps/ads_compact_block_http_smoke.py`

# 6. Какой smoke подтверждён

- Artifact-backed smoke подтверждён через `apps/ads_compact_block_smoke.py`.
- Authoritative server-side smoke подтверждён через `apps/ads_compact_block_http_smoke.py`.

# 7. Что уже доказано по модулю

- Parity подтверждена для `normal-case` и `empty-case`.
- Server-side checkpoint подтверждён как реально рабочий: `normal -> success`, `normal: count -> 2`.
- Прежнее paused-состояние снято без дополнительных правок кода после замены server-side canonical WB token path.

# 8. Что пока не является частью финальной production-сборки

- `CONFIG/METRICS/FORMULAS` migration;
- jobs/API bundle/deploy;
- более широкий runtime-pipeline beyond bounded checkpoint.

# 9. Finance/Partner historical consumers and envelope compatibility

Persisted accepted closed-day snapshots exist in two compatible shapes:

- root payload: `{kind,snapshot_date,items}`;
- nested payload: `{result:{kind,snapshot_date,items}}`.

Every Finance/Partner manifest, coverage and calculation consumer uses the shared `resolve_ads_snapshot_payload` compatibility resolver. A valid root payload must never be reported as missing merely because `result` is absent. Invalid JSON/envelope/value remains missing; it is never silently empty.

`Партнёрский отчёт` consumes exact `date + nmId`. Weekly `Маркетинг WB` is `SUM(ads_sum)` for the selected `nmId`; it never combines that value with Finance marketing deduction. Persisted `kind=empty` is confirmed zero. Missing date or successful payload without selected-SKU coverage is an explicit blocker. Finance canonical apply treats ads as non-target and cannot write snapshots or turn missing date/SKU combinations into zeros.

# 10. Historical missing-slot recovery

`apps/ads_historical_recovery.py` is the only repo-owned production-data path for a reviewed exact set of absent accepted closed-day `ads_compact` slots. Dry-run is default. It reads the official campaign manifest and `/adv/v3/fullstats` only for campaign statuses `7`, `9`, `11`, with at most 31 inclusive days, 50 campaign IDs and 3 requests/minute. Invalid/incomplete upstream data, a non-empty global response without the requested `nmId`, or an invalid existing target snapshot fails closed; a valid existing snapshot is skipped and never overwritten.

The 3 requests/minute pacing is the current Personal/Service fullstats contract. A Base-plan token is limited by WB to 1 request/hour and therefore cannot use this bounded production recovery as configured; `429` is an upstream/token-plan blocker and is never retried as empty data.

Apply requires the exact fresh plan fingerprint, exact `nmId`/date scope, fresh human approval reference, canonical warehouse-functional write lock and a coherent mode-`0600` SQLite backup with `integrity_check=ok` and SHA-256. One `BEGIN IMMEDIATE` transaction inserts only planned missing snapshots, updates their closure state, records audit evidence and proves exact readback plus the non-target digest. An unchanged retry is an audited no-op. `kind=empty` is written only when the complete official response for every eligible campaign contains no row for any `nmId` on that date; the runner never manufactures a selected-SKU zero.
