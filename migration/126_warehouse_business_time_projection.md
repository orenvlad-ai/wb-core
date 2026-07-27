# Migration 126: warehouse/product-capital business-time projection

## Причина

Post-cutover historical read выбирал functional version по техническому
`effective_at`. Поздно записанная 25.07 фактическая отгрузка с business date
21.07 поэтому могла впервые изменить остаток только 25.07. Это смешивало
functional time и audit time.

## Новый temporal contract

- `business_effective_date` и `snapshot_date` задают функциональную дату;
- `recorded_at`, `published_at`, прежний `effective_at` и `created_at` остаются
  audit-временем и используются только для стабильного порядка одинаковых
  revisions;
- historical selector требует exact `snapshot_date` и допускает version только
  когда `business_effective_date <= snapshot_date`;
- nullable поля добавляются без переписывания существующих production rows;
  legacy version без нового поля наследует только собственный exact
  `snapshot_date`, но никогда не вычисляет business date из `effective_at`.

События одного business day воспроизводятся в функциональном порядке:
source quantity/cost credit, затем `production → China→FF → FF → FF→WB → WB`.
`created_at` не определяет этот порядок.

## Durable projection/outbox

`warehouse_business_projection_v1` добавляет:

- immutable revision registry;
- immutable revision rows;
- atomic current rows только для owned warehouse/product-capital metric keys;
- global revision state для лёгкого UI check;
- durable outbox с `stable_source_id`, `source_revision`,
  `business_effective_date`, affected SKU closure и terminal status.

Canonical event, expense-certification, official WB/WAC, targeted functional
queue и FF operation hooks записывают outbox в том же SQLite transaction, что
и соответствующий durable source/queue row. Requests coalesce по stable
source/revision, earliest business date и SKU closure. Exact repeat — no-op.
FF operations получают отдельный nullable `business_effective_date`: для
supplier/WB automation это factual source date, для новой manual operation —
Yekaterinburg business date подтверждения; `created_at` остаётся recorded
audit time.

Targeted supplier factual replay публикует source row, immutable functional
version, queue completion и business projection в одном transaction. Event
replay строит bounded date/SKU candidate из canonical product-capital events.
Functional-only или FF-only revision без exact event proof не запускает
внешний fetch и не фабрикует капитал: cost-only seam сохраняет quantity, а
недоказанные owned values остаются unavailable либо last-good provisional.

Candidate rows сначала полностью рассчитываются и проверяются. Только после
этого одним transaction переключаются current rows/state. Ошибка откатывает
candidate и оставляет last-good active projection; outbox хранит точную
retry/error причину. Failed attempt остаётся immutable audit evidence, но не
занимает active identity: exact retry той же stable source revision может
успешно опубликоваться, тогда как другая active projection для той же source
revision fail-closed запрещена.

## Web Vitrina

Ready snapshot остаётся immutable. Read path merge-ит projection только в
public owned warehouse/product-capital metric keys для exact date/SKU/TOTAL.
Чужие source cells и metadata не меняются. При отсутствии current ready
snapshot не копируется вчерашний день и не создаются значения других
источников.

`GET /v1/sheet-vitrina-v1/web-vitrina/business-projection/status` возвращает
revision/status/outbox failure. Видимая вкладка проверяет revision bounded
polling раз в 12 секунд. Новая revision перечитывает только table composition;
period, filters, metric presentation/preset, disclosure state и scroll
сохраняются. Изменившиеся cells кратко получают violet accent. Neutral
`Обновляется` не использует warning yellow; warning остаётся только для
provisional/incomplete/error и сохраняет last-good table.

Supplier factual Apply продолжает ждать durable terminal job и дополнительно
публикует same-origin `BroadcastChannel` signal. Другие процессы подхватываются
revision polling без page reload и без обязательной ручной загрузки Витрины.

## Safety и performance

- максимум одной bounded revision — 366 consecutive business dates;
- no Finance raw/full database scan, external producer fetch или full Vitrina
  refresh;
- diagnostics содержат affected dates/SKU/rows, elapsed time,
  `external_source_refresh_count=0`, `full_vitrina_refresh_count=0` и
  `all_history_rebuild=false`;
- cost-only revision проверяет byte-semantic неизменность quantity keys;
- physical replay проверяет conservation и idempotency;
- incident availability policy не участвует в physical capital projection.

## Rollout boundary

Deploy создаёт только additive schema, triggers, read seam и inert/retryable
worker capability. Он не меняет существующие production business rows.
Историческая production correction/backfill не входит в migration 126:
сначала требуется read-only diagnostic manifest, затем отдельная
`scope:production-mutation` задача с human gate, exact backup/reversibility и
post-run reconciliation.

## Проверка

- `python3 apps/warehouse_targeted_replay_smoke.py`;
- `python3 apps/warehouse_business_projection_smoke.py`;
- `python3 apps/warehouse_business_projection_browser_smoke.py`;
- `python3 apps/own_product_capital_smoke.py`;
- `python3 apps/ff_stock_ledger_http_smoke.py`;
- Web Vitrina contract/page/http/browser smokes;
- production isolated Playwright UI Flow после Release Train deploy.
