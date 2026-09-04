---
title: "Модуль: единый реестр изменений — foundation"
doc_id: "WB-CORE-MODULE-54-CHANGE-REGISTRY-FOUNDATION"
doc_type: "module"
status: "active_foundation"
purpose: "Зафиксировать server-owned append-only contract seller actions и observation/health evidence для read-only observer и internal price/bid/campaign-state capture."
scope: "Additive operational SQLite schema, deterministic scalar canonicalization, immutable repository primitives, stable reads/cursors and a transaction-safe non-canonical manual-pending coordination seam."
source_basis:
  - "AGENTS.md"
  - "docs/architecture/11_github_release_train.md"
  - "docs/modules/22_MODULE__REGISTRY_UPLOAD_DB_BACKED_RUNTIME_BLOCK.md"
  - "docs/modules/37_MODULE__SHEET_VITRINA_V1_ADS_OPERATOR_BLOCK.md"
  - "docs/modules/41_MODULE__WB_PRICES_MANAGEMENT_BLOCK.md"
  - "docs/modules/46_MODULE__SKU_MANAGEMENT_BLOCK.md"
related_modules:
  - "packages/application/change_registry.py"
  - "packages/application/storage_registry.py"
  - "packages/application/registry_upload_db_backed_runtime.py"
related_runners:
  - "apps/change_registry_smoke.py"
source_of_truth_level: "module_canonical"
update_note: "Foundation remains immutable; module 57 owns observation/manual-pending, while module 58 captures proven internal price/bid/campaign-state writer lifecycles."
---

# 1. Решение о storage и граница активации

Реестр живёт в logical `operational` store, который выбирается только
`StoreRegistry`. В implicit-monolith режиме это существующий
`registry_upload_runtime.sqlite3`; после split — manifest-selected operational
generation. Обычный `_ensure_schema` создаёт только пустые additive tables,
indexes и triggers. Он не создаёт отдельный database/manifest, не меняет
current canonical stores и не записывает seller actions.

SQLite является текущим operational implementation, а не решением о финальном
PostgreSQL target. Смена storage architecture, cross-store move или PostgreSQL
migration остаются отдельным решением.

Canonical baseline/diff engine описан в module 56; активный read-only observer
и UI/API — в module 57. Internal Prices, Ads, SKU Management, Balance и SPP
writes используют только application seam модуля 58. Balance calculation,
preview, dry-run и refresh не создают action rows; confirmed live bid/state
writer создаёт их непосредственно перед единственным submit. Existing Prices/Ads JSONL и
`sheet_vitrina_v1_sku_action_events` остаются native evidence и не удаляются,
не переписываются и не импортируются этим блоком.

# 2. Канонический scope и atomic identities

Каждая canonical row несёт обязательные `seller_id` и `account_scope` прямо
либо наследует их через immutable parent. Это business scope одного seller
account. Token/profile/credential path не является business identity и никогда
не хранится в registry schema. Текущий product поддерживает один seller account;
multi-account UI не появляется из-за наличия scope columns.

Atomic target identity:

- `price`: `seller_id + account_scope + nm_id + parameter_field`;
  `advert_id=0`, `placement=''`;
- `bid`: `seller_id + account_scope + nm_id + advert_id + placement +
  parameter_field`; placement только `combined`, `search` или
  `recommendations`;
- `campaign`: `seller_id + account_scope + advert_id +` доказанный ровно один
  `nm_id + parameter_field`; `placement=''`.

Один `advert_id` должен разрешаться ровно в один `nm_id` для campaign action.
Cardinality `0` или `many` создаёт immutable
`campaign_nm_mapping_cardinality` incident с sorted unique candidate list и не
может создать target/action item. Exact-one helper возвращает identity и не
создаёт incident.

`operation_id` — стабильный provenance header для пользовательской/системной
операции. `change_item_id` — единица анализа и всегда один exact target/field.
Header не используется как агрегированная аналитическая единица.

# 3. Canonical values и mappings

`wb_change_registry_mapping_v1` обязателен во всех target/value rows. Он
фиксирует current field names, placement normalization и границу будущих
versioned status/payment translations. Foundation принимает уже нормализованный
non-empty text token для campaign state/payment model/unit; adapter-specific WB
codes не получают неявное человекочитаемое значение внутри storage layer.

Поля v1:

- price: `original_price_minor`, `discount_bps`, `seller_price_minor`;
- bid: `bid_minor`;
- campaign: `campaign_state`, `payment_model`, `payment_unit`.

Money и bid хранятся только как SQLite `INTEGER` minor units. `discount_bps` —
`INTEGER` от `0` до `10000`. В change-registry tables нет `REAL` columns.
Status/payment values — non-empty canonical text plus mapping version.

Scalar representation состоит из `kind + integer + text`:

- `missing` — источник не дал поле;
- `null` — источник явно вернул null;
- `integer`, `text`, `boolean` — exact value.

`missing`, explicit `null` и integer `0` различны. Observation status
`exact_zero` требует именно integer zero; `missing`, `inapplicable` и `error`
требуют missing value и различаются самим status. Deterministic JSON использует
sorted keys, UTF-8, compact separators и запрещает NaN/Infinity; evidence links
используют lowercase `sha256:<64 hex>`.

# 4. Таблицы и ownership

## 4.1 Seller actions и proof

- `change_registry_operations` — immutable provenance header: seller/account,
  source surface, immutable actor principal/kind, request/create timestamps,
  optional native idempotency/correlation/calculation/apply references и
  provenance digest. Reason/comment не живут в header.
- `change_registry_items` — immutable atomic requested change: exact identity,
  field, canonical before/requested value и optional
  `recommendation_item_id`. Composite FK не позволяет operation/item scope
  drift.
- `change_registry_attempt_events` — append-only per-attempt lifecycle. Attempt
  sequence начинается `created`, затем допускает `submitted`, terminal failure/
  rejection/cancellation/confirmation либо `ambiguous -> resolved` с explicit
  terminal resolution. Receipt/readback fields содержат только sanitized
  reference/digest/error evidence, не request/response body.
  `created -> ambiguous` разрешён только для транспорта, где WB submit был
  вызван, но ответ не доказывает принятие; повторный submit запрещён.
- `change_registry_facts` — только proven transitions. Row хранит exact
  identity/field, before/after, observed interval, proof kind, evidence digest и
  proven time. Same values с другим interval/evidence допустимы: value-only
  dedupe запрещён. Duplicate одного и того же proof identity блокируется.
- `change_registry_fact_links` — append-only late links к change item,
  checkpoint, native audit reference или recommendation item. Change-item link
  требует exact seller/account/target/field equality; checkpoint link — тот же
  seller/account scope. Благодаря этому observer/write race разрешается поздней
  связью уже сохранённого fact без создания duplicate fact.

Only facts with an admitted proof kind (`wb_readback`, `native_audit`,
`checkpoint_diff`, `reconciliation`) могут в будущем питать interval
projection. Attempt status, request item или value coincidence сами fact не
создают.

## 4.2 Observations, health и identity incidents

- `change_registry_checkpoints` — immutable scan metadata: seller/account,
  source/scan kind, interval, complete/partial/failed status, expected/observed
  counts, completeness/evidence digests и optional previous complete baseline.
  Previous baseline обязан быть complete в том же seller/account scope.
- `change_registry_observation_values` — immutable normalized per-target values
  и health fields. Checkpoint completeness не выводится из количества rows
  задним числом и не превращает missing в zero.
- `change_registry_identity_incidents` — append-only fail-closed identity
  evidence. Campaign cardinality 0/many представляется здесь, не action item.

`budget_exhausted` является operational observation/health evidence, не seller
action и не campaign transition fact без отдельного proof.

## 4.3 Annotations и manual pending seam

- `change_registry_annotation_revisions` — append-only linear revisions с
  parent. Изменение reason/comment создаёт новую revision; canonical
  operation/item/fact не UPDATE-ится.
- `change_registry_manual_pending_events` — append-only seam состояний
  `pending`, `superseded`, `matched`, `deviated`, `expired`.
- `change_registry_manual_pending_current` — единственная mutable table в этом
  модуле. Это non-canonical coordination pointer exact target → current
  pending event. Update требует stable identity и monotonic `revision+1`, delete
  запрещён; repository меняет event и pointer в одном `BEGIN IMMEDIATE`.

Manual-pending product behavior активируется только модулем 57/Balance manual
flow, не самим foundation. Lookup обязан начинаться с реальных pending events:
наличие live-writer item с `recommendation_item_id`, но без manual event, не
делает его pending candidate. Pointer нельзя использовать как historical truth
или proven fact.

# 5. Immutability, idempotency и reads

Все canonical tables имеют database-level UPDATE/DELETE rejection triggers.
Retention/delete API отсутствует. Только manual coordination pointer допускает
CAS-like update; его row также нельзя удалить.

Primary ids, native idempotency keys, atomic item identity, attempt/pending
sequence, observation identity и evidence identity имеют explicit uniqueness.
Exact repeated repository insert возвращает существующие bytes; тот же id или
idempotency key с другим payload fail-closed.

Internal repository предоставляет create/read/append primitives и stable
ascending cursor order `(timestamp, stable_id)` для operations/facts. Cursor
versioned и entity-bound. Public HTTP/UI route отсутствует.

# 6. Secret/raw-data boundary

Schema не содержит token, cookie, password, secret или raw WB payload fields.
References/digests должны быть sanitized. Error/health text bounded и отклоняет
credential-shaped markers. Full WB request/response, headers and bodies остаются
в их native protected contours и не копируются в ledger.

# 7. Точный excluded scope

Этот foundation не включает:

- historical import/backfill/baseline;
- observer, diff, scheduler, lease/job runtime или health collection;
- Prices/Ads/SKU writer instrumentation;
- campaign creation/deletion behavior (typed campaign-state writer capture принадлежит module 58);
- manual-pending UX/business behavior;
- public API, Web Vitrina UI или multi-account UI;
- outcome/causal analytics, recommendations generation или interval projection;
- Balance integration;
- deletion/migration of existing JSONL/SKU events;
- production activation, deploy, data writes or PostgreSQL migration.

Следующий bounded block материализован в
`56_MODULE__CHANGE_REGISTRY_BASELINE_ENGINE.md`: он использует эти immutable
primitives только при явном internal invocation и не активирует scheduler,
HTTP/UI или writer capture. Наличие empty schema и этого dark callable engine
не означает, что единый реестр автоматически включён.
