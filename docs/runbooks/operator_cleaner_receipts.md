# Native квитанции cleaner

Общий журнал читает immutable `cleaner_requests` и сохранённые native результаты.
Новый источник, очередь, исполнитель, WB-вызов или право записи не создаются.
Owner command lock/CAS, generation/admission, immediate disable, external seals,
readback/recheck и cleaner isolation остаются у существующего native владельца.

`CleanerScope` строится только из текущего entrypoint/cleaner owner: native
operational registry, account key + seller/scope, generation и actual actor.
Перед count/search/page/detail проверяются текущая native привязка хранилища и
account/generation; SQL фильтрует account + actor. Auth-disabled поверхность
новый domain не получает. Общий GET не конструирует cleaner/store service, не
вызывает schema bootstrap, worker, reconciliation или внешние запросы. Request,
immutable events и item proof читаются в одном RO/query_only snapshot.

`cleaner-request:<request_id>` — ссылка original immutable native request.
Настройки, schedules/profile versions и решения без execution сохраняют
`source_saved`, `calculation_completed=false`: «Сохранено», а не изменение WB.
Scan completion говорит только о проверке доступных ключей; полный охват WB не
утверждается. Native job остаётся processing либо needs_attention, пока его
исходный actor/account/target и полный результат не доказаны. Unknown source
route получает явный `result_projection_unavailable`, а не fictitious completed.

WB completion требует exact saved manual-apply target/query/decision set без
дубликатов, exact actual set, native operation state confirmed/dispatch_count=1,
каждую item confirmed/confirmed_at и immutable readback_result/late_confirmation
того же account/run/operation/target. Prepared, partial, no child, чужой target
или только terminal label не подтверждают WB. Batch receipt сохраняет исходные
selected pairs и показывает каждый stable native child; целая группа завершена
только после каждого точного результата. No-change требует завершённый native
scan и отсутствие write run; это проверка без утверждения новой WB записи.

HTTP декорирует уже состоявшуюся native mutation после commit и точный native
GET. Ошибка projection после commit возвращает ambiguous/503, никогда definite
precommit rejection. Existing UI хранит pending request, после unknown/reload
читает тот же ID, без нового POST. Общая квитанция появляется только после exact
durable request proof. Result polls обновляют её из saved native state. Existing
proven rollback retry в batch относится к той же local not-accepted identity;
неопределённый ответ не даёт разрешения повторять внешнюю запись.

Новых HTTP routes нет. Изменены результаты существующих:

- POST `/v1/sheet-vitrina-v1/ads/keyword-cleaner/settings`, `/daily-schedules`,
  `/runs`, `/manual-clean`, `/manual-batches`, существующих `/recheck`, `/resume`,
  `/reviews/<id>/decision`, `/profiles/<nm>/versions|activate`;
- GET того же prefix + `/requests/<id>`, `/manual-clean/<id>`,
  `/manual-batches/<id>`;
- GET `/v1/sheet-vitrina-v1/operations` и existing exact `/operations/<id>`
  добавляют domain `cleaner_operations` с прежними source grants.

Проверки `operator_cleaner_operations_smoke.py` используют native temporary
stores, actual HTTP/forms и loopback FakeWB. Layout-only browser fixtures имеют
отдельные `ui-fixture:` native source records; они не создают реальные job
bindings в worker/summary и получают needs_attention при отсутствии native
result proof. Подставленные layout/WB данные не доказательство production
completion. `operator_cleaner_operations_fixture.py` не входит в runtime.
