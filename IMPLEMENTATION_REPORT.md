# WB Autoanswers Server v1 — implementation report

Дата локальной приёмки manual increment: 2026-07-20

Актуальный baseline: local `refs/remotes/origin/main` at `e9f90c306763e252790eeb3c921875198d2aca64`

Исходный server-v1 baseline: `8cd6eeb62c4b9c6ef98d52b0a38270978999b229`

Рабочая копия: `/Users/ovlmacbook/Downloads/wb-core-autoanswers-manual_20260720T081019Z/repo`

Локальная ветка: `agent/wb-autoanswers-manual`

## Local release-candidate status

`MANUAL_MODE_RELEASE_CANDIDATE_READY__FORCE_OFF_STAGE_FIRST`

Первоначальный server-native контур был выпущен из baseline `8cd6eeb62c4b9c6ef98d52b0a38270978999b229`. Текущий manual increment построен от актуального `origin/main` `e9f90c306763e252790eeb3c921875198d2aca64`. Локальная реализация, fake-transport публикация, release hardening, тесты и документация готовы. Первый production deploy сохраняет emergency force-off и устанавливает full worker disabled. Снятие force-off выполняется только последующим tracked config release после OFF acceptance.

## Manual mode increment

- В стабильный enum добавлен `manual`; UI теперь имеет один selector: `Выключено`, `Ручной`, `Черновики`, `Безопасный`, `Полный`.
- В `manual` steady sync и backfill создают ноль AI jobs. Только явный POST `manual/generate` с permission `feedbacks.ai_review` ставит один idempotent durable job для точной текущей версии.
- Generated reply, route, warnings, cost и checks доступны в detail. Редактирование вызывает новый `guard_final` operation узкого Python↔Node boundary: strict draft JSON Schema плюс неизменённый frozen final draft guard.
- Публикация требует сохранённый guard pass, exact edit revision/hash и отдельный `confirmed=true`. HTTP лишь создаёт durable publication job.
- Непосредственно перед write повторно проверяются effective ON, текущий `manual`, permission инициатора, version/hash, отсутствие WB answer, exact reply, hard gates, fallback/media uncertainty и seller_chat invariants.
- Все autoanswers POST защищены capability checks и JSON/same-origin CSRF marker.
- Schema v2 создаёт verified pre-migration backup, расширяет persisted mode constraint и хранит manual review/publication evidence. Duplicate publication для одной feedback version запрещён уникальным индексом.
- Full worker service/timer устанавливается disabled и force-OFF. Repo-owned lifecycle проверяет зависимости/frozen hashes/empty queues, atomically activates manual, запускает только GET-only canary без Node/OpenAI/writer capability и умеет fail closed deactivate.
- Production UI acceptance имеет два read-only профиля: `off-force` и `manual`; manual profile не нажимает generation/publication и доказывает нулевой job delta.

## Что реализовано

### 1. Runtime boundary, contracts, additive schema and switches

- Добавлен server contract `wb_autoanswers_server_v1` и JSON boundary `wb_autoanswers_node_boundary_v1`.
- Зафиксированы frozen bundle `1.4.2` и evaluation signature `sha256:5f305d7eceba13e90b5b51f2a774b6ce71c24b9b2af07cc2637210f2e25b30da`.
- Frozen `make_mvp_v1.0.0` перенесён побайтно из архива с SHA-256 `350b15bdfab9f8139a83920fbce7f1c9876607b594cea0d8c19a6f9ddc38f7e5`; prompts/contracts/guards/golden/fallbacks не редактировались.
- Реализован узкий stdin/stdout JSON boundary Python → Node. Node перед каждым запуском проверяет все 28 manifest hashes и вызывает исходный frozen orchestrator.
- В существующей runtime SQLite добавлены отдельные canonical feedback/version/media, sync, command, processing, publication, attempt, budget, backlog-preview and audit tables.
- Master-switch default OFF; `WB_AUTOANSWERS_FORCE_OFF=true` имеет абсолютный приоритет.
- OFF разрешает синхронизацию, локальный UI и обязательный GET-only readback уже возможной отправки, но блокирует enqueue/claim AI, ручное approve, все новые write claims и каждый WB write.
- OFF→ON увеличивает `enable_epoch`; старые jobs не продолжаются автоматически и переводятся в `needs_review`.
- Реализованы `manual`, `draft_only`, `auto_safe`, `auto_all`. Начальный auto_safe allowlist: `public_only`, `wb_return`, `wb_support`. `seller_chat` всегда review-only.
- Реализован двухшаговый historical backlog: expiring preview с count/max cost → explicit enqueue. История не проходит эту границу автоматически.
- Бюджет: warning 70%, hard cap $5/day and $50/month. Atomic reservation учитывает параллельные claims; exact frozen usage settles per-review cost.

### 2. WB read synchronization and reconciliation

- Изолированный read adapter реализует только официальные feedback GET operations.
- Initial backfill начинается с `2026-01-01`, разделён на answered/unanswered, идёт по одному bounded page/day и сохраняет cursor только после commit.
- Backfill и archive reconciliation никогда не ставят отзыв на AI автоматически.
- Steady sync использует 48-hour overlap, сначала upsert, затем idempotent enqueue только для новой semantic version, впервые замеченной в текущем ON epoch.
- Реализованы archive reconciliation и local-vs-remote unanswered count seam.
- 429, 5xx и transport error не продвигают незавершённый cursor.
- `content_version_hash` отделён от `wb_observation_hash`: answer, wasViewed и WB state не создают новую AI version. Query-параметры media URL также не меняют content hash.

### 3. Local API and SellerOS UI

- Существующий `GET /v1/sheet-vitrina-v1/feedbacks` сохранён без изменения контракта.
- Добавлены local list, detail, settings, sync-command, backlog preview/enqueue and review-approve routes.
- Local list по умолчанию отдаёт последние 50 строк; page/page_size and server filters поддерживают unanswered, status, rating, route, SKU, date, photo, video, needs_review, published and error.
- Existing `Отзывы → Отзывы` открывает новую server subtab первой, сразу читает SQLite и отдельно отправляет неблокирующую sync command.
- Таблица показывает AI and WB statuses, media flags, route, generated/existing reply, cost and attempts.
- Detail показывает text/pros/cons/tags/product/nmId/article, media processing, route/case code, generated reply, actual WB reply and audit trail.
- ON требует confirmation. `auto_all` требует отдельного typed confirmation and cannot be enabled in one click.
- Capability boundary:
  - `feedbacks`: list/detail/settings view and sync command;
  - `feedbacks.ai_review`: additionally manual publication enqueue;
  - `feedbacks.autoanswers_admin`: additionally switch/mode/budget/backlog controls.

### 4. Media and frozen AI in draft_only

- Photo download: HTTPS + WB CDN host allowlist + 20 MiB hard limit.
- Video download: 100 MiB hard limit; ffmpeg extracts at most six frames.
- File paths are server-owned and scoped by a hash of feedback ID/content version.
- Downloaded photos and extracted video frames are passed to the frozen classifier as image inputs.
- Unprocessed/failed video is never represented as viewed. Any media uncertainty forces `needs_review`.
- Unknown SKU passes empty line-specific context; frozen normalizer/guard remains the semantic owner.
- Empty five-star review returns frozen prefilter skip with zero role calls.
- Normal frozen path is classifier → writer → validator; route is immutable across maximum two rewrites; approved same-route fallback remains frozen. Fallback result cannot auto-publish.

### 5. Durable publication with fake transport acceptance

- Processing idempotency key: feedback ID + semantic content version + bundle version.
- Publication key: feedback ID + semantic content version + normalized final reply hash + create-answer adapter version.
- HTTP handlers never call WB write; they only create durable commands/jobs.
- Before transport the repository rechecks effective ON, current version, absence of external answer, frozen identity, all hard-gate flags, no fallback/media uncertainty and seller_chat public invariants.
- Exact reply, hash, feedback ID, content/bundle/evaluation versions and attempt are committed before POST.
- `204` is not publication proof. HTTP success/error/timeout all go to `publish_pending_readback`.
- Pending publication can execute only GET detail reconciliation; blind POST retry is structurally excluded.
- Exact normalized readback reaches `published`. Missing/different/external reply reaches `needs_review` without a second write.
- Readback 429/5xx/timeout сохраняет только readback retry. Даже при master/force-off такой job может claim-иться исключительно для обязательного GET readback; повторный POST структурно невозможен.
- PATCH existing WB answers is absent.

### 6. Documentation and explicit external boundary

- Added `docs/modules/49_MODULE__WB_AUTOANSWERS_SERVER.md`.
- Added `migration/105_wb_autoanswers_server_v1.md` with staged activation and rollback.
- Updated module index and README.
- `apps/wb_autoanswers_worker.py` is inert by default. `--run-once` is rejected unless `WB_AUTOANSWERS_EXTERNAL_IO_ENABLED=true`; full timer устанавливается disabled и включается lifecycle gate только после manual activation proof.
- Active production target and HTTP systemd unit pin `WB_AUTOANSWERS_FORCE_OFF=true`; OFF→ON is rejected while the override is active.
- `apps/wb_autoanswers_readonly.py` is a separate GET-only capability for bounded canary/backfill. It imports no writer/Node/OpenAI code, requires persisted master OFF, reasserts force-off after env load, rate-limits calls and proves zero AI/publication job delta.
- A dedicated five-minute GET-only steady timer is deployed disabled, then can be enabled only by the repo-owned timer gate after production read acceptance. It also drains UI sync commands without importing AI/publication capabilities.
- Authenticated production Playwright acceptance has a repo-owned flow that proves exact URL/render, OFF reason and disabled controls, local 50-row pagination, detail/media/status contracts, zero cross-page duplicates, no 5xx/page/console/fatal errors and screenshot evidence.
- Schema-v2 execution takes a coherent SQLite backup under `backups/wb_autoanswers_schema_v2`, verifies `PRAGMA integrity_check=ok`, and applies DDL plus marker/settings atomically; failure aborts before activation.

## State and idempotency summary

Implemented states cover:

`discovered → synced → queued → processing → generated → needs_review/approved → publishing → publish_pending_readback → published`, with `skipped`, `retryable_error` and `terminal_error` branches.

SQLite uses WAL, foreign keys, 10-second busy timeout and `BEGIN IMMEDIATE` around claims/reservations/transitions. Claims have owner/lease timestamps. An expired processing or new-write publication lease is recoverable by exactly one claimant only while effective ON. Publication pending readback remains durable and may perform only GET reconciliation while OFF; it can never become a second POST.

## Проверки

### Autoanswers tests — PASS (75 methods)

```text
apps/wb_autoanswers_activation_test.py         3 PASS
apps/wb_autoanswers_runtime_test.py           21 PASS
apps/wb_autoanswers_sync_test.py               7 PASS
apps/wb_autoanswers_node_bridge_test.py        5 PASS
apps/wb_autoanswers_media_worker_test.py      4 PASS
apps/wb_autoanswers_publication_test.py       15 PASS
apps/wb_autoanswers_http_ui_test.py            7 PASS
apps/wb_autoanswers_readonly_test.py           7 PASS
apps/wb_autoanswers_release_safety_test.py     6 PASS
```

Покрыты обязательные сценарии:

- OFF at enqueue, processing, approval and pre-write;
- all five selector states, four enabled modes and complete initial auto_safe allowlist;
- emergency force-off;
- OFF→ON without automatic historical/old-epoch backlog;
- explicit backlog preview/enqueue;
- content hash vs WB observation hash;
- media URL query churn;
- duplicate sync/job/approval/publication;
- stale content version and external WB answer;
- seller_chat review-only, one case code and public materials prohibition;
- 70% warning, hard caps and concurrent reservations;
- unknown SKU;
- SQLite concurrent lease reclaim/crash recovery;
- 204 with exact/missing/different readback;
- ambiguous timeout and readback-only retry;
- readback 429 remains durable and unclaimed while OFF;
- coherent pre-schema backup and atomic additive migration;
- production target/unit force-off pin and GET-only hosted runner capability;
- Python/Node contract, frozen identity, media frames and empty-five-star prefilter.

### Frozen package — PASS (28/28)

`npm test` from `packages/node/wb_autoanswers_v1_4_2/make_mvp` passed every artifact/runtime/payload/guard check. The frozen lock currently reports one moderate and one high dependency advisory under `npm audit`; dependencies were not silently changed because frozen identity is authoritative. This needs a separately versioned bundle decision, not an in-place v1.4.2 edit.

### Existing wb-core regression/static checks — PASS

```text
python3 -m compileall -q apps packages
apps/registry_upload_http_entrypoint_smoke.py
apps/registry_upload_http_entrypoint_auth_smoke.py
apps/registry_upload_http_entrypoint_public_routes_smoke.py
apps/registry_upload_http_entrypoint_users_admin_smoke.py
apps/sheet_vitrina_v1_feedbacks_http_smoke.py
apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py
apps/sheet_vitrina_v1_web_vitrina_http_smoke.py
apps/registry_upload_http_entrypoint_hosted_runtime_smoke.py
git diff --check
```

Two stale smoke expectations found in baseline were aligned with pre-existing code truth: supply users already receive both `factory-order` and `warehouses` tabs, and current-live invariant fixtures must use the canonical target ID. No deploy implementation or target artifact changed.

The rendered UI test emits pre-existing `ResourceWarning` messages from legacy `registry_upload_db_backed_runtime.py` connection initialization; the test passes and the new repository closes all of its own connections. This warning is outside the autoanswers runtime and remains visible rather than hidden.

### Fail-closed worker check — PASS

```text
python3 apps/wb_autoanswers_worker.py
=> {"status":"ready","external_io":false,...}

python3 apps/wb_autoanswers_worker.py --run-once
=> exit 2, {"status":"blocked","code":"external_io_gate_off"}
```

## Security review

- No secret value, connection ID or token is stored in this change.
- Only environment names are documented: `WB_API_TOKEN`, `WB_FEEDBACKS_API_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_RESPONSES_BASE_URL`, `REGISTRY_UPLOAD_RUNTIME_DIR`, `WB_AUTOANSWERS_FORCE_OFF`, `WB_AUTOANSWERS_EXTERNAL_IO_ENABLED`.
- The read adapter has no write method. The write adapter is a separate capability and implements POST create-answer plus inherited detail GET only.
- Media fetch is HTTPS/allowlist/bounded and verifies redirect host.
- User input cannot select filesystem destination, executable or model prompt.
- Frozen Node audit keeps its signed-media URL redaction behavior.
- UI renders server data through HTML escaping.
- Base and nested feedback permissions are server-enforced, not browser-owned.

## Что намеренно осталось

These are release/owner actions, not missing local implementation:

1. Complete the LOOP release train and exact-SHA hosted deployment while force-off remains true.
2. Run authenticated production OFF acceptance.
3. Complete the tracked configuration release that removes force-off while persisted master remains OFF.
4. Activate `master_enabled=true, mode=manual` only through the repo-owned lifecycle and run read-only manual acceptance without clicking generation.
5. The first real generation is intentionally left to the owner through the UI; any production WB publication still requires a later explicit confirmation after review and mandatory readback.

No automatic PATCH/edit of an existing answer is planned for v1.

## Rollback strategy

Before any future activation, take and verify a runtime SQLite backup. Emergency rollback is:

1. set `WB_AUTOANSWERS_FORCE_OFF=true`;
2. stop only the future autoanswers timer/unit;
3. keep ambiguous attempts durable and unclaimed; after a later authorized re-enable, reconcile them by GET before any write decision;
4. never retry their writes blindly;
5. roll code back independently; additive tables can stay inert;
6. restore the database only for corruption and only after WB readback reconciliation.

## External-action accounting at release-candidate creation

- Wildberries API calls: **0**
- OpenAI live/evaluation calls: **0**
- Wildberries writes: **0**
- Deploys/server mutations: **0**
- Make calls: **0**
- Telegram calls: **0**

All WB and model behaviors were exercised through fakes or frozen local fixture role outputs.

## Exact post-release external gate

Текущий owner authorization включает release train, force-OFF acceptance, tracked снятие force-off и activation в `master_enabled=true, mode=manual` без генерации. После успешной manual acceptance единственный следующий gate — владелец открывает `Отзывы → Отзывы`, выбирает eligible отзыв и нажимает `Сгенерировать ответ`. Release train не нажимает эту кнопку, не вызывает OpenAI и не создаёт WB write. Публикация остаётся отдельным последующим подтверждением владельца после просмотра и повторных guards.
