# Сохранение расписания авто-жалоб

Нативный источник — `runtime/sheet_vitrina_v1_feedbacks_auto_complaints.json`,
владелец — `JsonFileFeedbacksAutoComplaintsStore`. Используются прежние
`GET/POST /v1/sheet-vitrina-v1/feedbacks/automation/schedules` и общий журнал
`GET /v1/sheet-vitrina-v1/operations[/<id>]`. Новых исполнителей нет.

Новая команда содержит `operation_id=complaint-schedules:<uuid>`,
`expected_source_revision` и один из вариантов: полный `schedules` или
`disable_schedule_id`. Сервер связывает её с реальным actor/account/scope.
Полный typed список содержит только семь native business fields: id, enabled,
local_time_hhmm, timezone, first_lookback_hours, overlap_hours, hard_cap_per_run.
Проверки времени/зоны/лимитов используют действующий native contract.

Под одним thread/process lock сохраняются источник, новая generation и
append-only `operator_schedule_receipts`. Квитанция содержит исходный запрос,
оригинальные before/after business intent, дату, версии и digest. В source CAS
не входят run/status/next_run/last_run/observation timestamps. Legacy source
save сохраняет квитанции и меняет generation, включая ABA; worker add/update/
terminal/interrupted записи сохраняют generation и квитанции. Точный повтор
ID возвращает старую квитанцию до CAS, не возвращая текущий источник назад.
Source+receipt commit — один fsynced atomic replace, без sidecar-транзакции.

`source_saved` означает только сохранённое расписание. Запуск и отправка WB
подтверждаются отдельно. Явная кнопка «Выключить расписание» использует свежий
CAS и отдельный fence точного schedule ID; она не ждёт неизвестного ON/save.
Меняется только enabled выбранного существующего расписания. Уже running
запуск не считается отменённым, а его последующее завершение не включает OFF.

UI использует Web Lock и retained собственный ID до единственного POST.
Unknown/lost response, generic 4xx/5xx и failed exact GET оставляют fence;
повтор/перезагрузка выполняют только GET того же ID. Clear допустим по точной
квитанции или положительно перечисленному native precommit refusal, включая
423/business_data_maintenance до source dispatch. Другая версия/actor/body
того же ID конфликтует. Без Web Lock запись не отправляется.

Common projection читает фиксированный файл без owner constructor (который
может пометить interrupted runs), provider, reconcile или write bootstrap.
Native path grant разрешает источник до чтения; actor/account/scope
ограничивают proof/search/count/page/detail. Foreign malformed proof не
участвует в диагностике своего источника. Старые записи без receipt не
становятся принятыми документами задним числом.

Новые настройки ограничены 64 schedules/80KiB request; ledger — 512 receipts/
8MiB, измеряется до source save и никогда не вытесняется. Worker terminal
write не имеет нового общего file-size gate: эта настройка не может отрезать
native terminal proof после внешней отправки. Readonly projection читает тот же физический source строгим streaming parser:
64MiB ограничивают сохранённую projection, не физический файл. Automatic
rows проверяются и пропускаются без загрузки целой строки; все retained
manual IDs и schedule receipts сохраняются. Malformed JSON, превышение
parser/projection bounds или смена source во время чтения явно отказывают
без truncation/нулевых substitutes. Native automatic last-200 retention,
полные reports, dedup и terminal writer сохраняют прежнюю семантику.

Synthetic tests: `apps/operator_feedback_schedules_smoke.py` (stdlib
multiprocessing/flock, existing APSW through HTTP fixture, Playwright Chromium).
Реальные WB/AI/SSH/production не используются.

## Ручной запуск из того же native source

Manual form отправляет typed `complaint-run:` ID с clicked schedule ID и
source revision. Значения фиксируются до WebLock и digest await. Native
source admission сохраняет immutable actor/account/scope/request digest,
назначает тот же native job, проверяет capacity до source commit/thread и
не повторяет source или worker при повторном ID. Automatic owner, provider
и существующий scheduled processing не заменяются новым executor.

Принятие source job означает `processing`, а не доставку жалоб WB. Native
terminal proof хранит завершение/ошибку отдельно от `external_confirmed`;
успешный noop не доказывает внешнюю отправку. Manual receipts не вытесняются
automatic last-200 retention. Новому command резервируются terminal proof
и будущий native config growth; при нехватке места отказ `not_saved` приходит
до commit и native owner. После provider новые capacity gates не вводятся.

UI хранит только opaque ID/digest, actor-config scope key защищён WebLock.
Unknown response/readback оставляет fence; reload/другая вкладка читает тот
же ID через GET. Только точный precommit refusal снимает собственный fence;
без WebLock POST запрещён. Старый unresolved manual job и отдельная OFF
source-настройка не подменяют друг друга.

Дополнительные synthetic проверки: `apps/operator_complaint_runs_smoke.py`
(stdlib `-S`, физический source growth, retained IDs и precommit capacity),
`apps/operator_complaint_runs_http_smoke.py` и
`apps/operator_complaint_runs_browser_smoke.py` (native TEMP HTTP/Chromium,
reload/two tabs/unknown readback, frozen operands до await).
