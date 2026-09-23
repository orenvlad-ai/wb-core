# Номер и статус ручного складского обновления

`POST /v1/sheet-vitrina-v1/warehouses/sync` принимает необязательный `request_key`
(16–128 латинских букв, цифр, `_`, `-`). Авторизованный principal задаёт область
на сервере; клиент не выбирает её. Канонический JSON остальных полей задаёт
отпечаток payload. Существующий legacy вызов с `{}` остаётся допустимым.

Перед HTTP acknowledgement существующий `WarehouseUpdateJournal` одной
транзакцией записывает состояние `accepted`, публичный alias, ключ/область и
точный payload. Отдельный `claim` под `warehouse_functional_job_lock` назначает
owner token и attempt ID перед любым эффектом. Ручной запуск сохраняет прежнюю
single-flight границу с CLI: занятый внешний owner возвращает `busy`, без новой
принятой операции. Повтор принятого key/payload возвращает исходный ID независимо
от возраста записи; несовпадение payload возвращает 409.

`GET /sync/status?run_id=...` и `GET /sync/status?request_key=...` читают точно
одну запись в разрешённой области через `mode=ro` / `PRAGMA query_only=ON`.
Неизвестный номер или чужая область дают 404. Чтение не мигрирует схему и не
исправляет journal. Пустой query сохраняет обзор последнего ручного запуска;
его нельзя использовать для восстановления потерянного acknowledgement.
Обзор, active_run и вложенные phases также фильтруются по области.
Старые durable ID читаются; связь утраченного in-memory UUID не угадывается.
Generic operator JSON/text endpoints не раскрывают warehouse jobs в обход
scoped route.

При построении application сначала подготавливается additive schema; после
построения всех зависимостей startup picker проверяет сохранённые операции.
Он повторяет только admission к занятому owner. Если accepted успел сохраниться
после истечения HTTP handshake, локальный picker запускается также без рестарта.
Активный или повреждённый write barrier, а также существующий warehouse maintenance
marker (holding/held или нечитаемый) откладывают pickup до восстановления допуска.
Проверка только читает существующие состояния, до pickup и повторно перед claim;
в подтверждённом окне обслуживания accepted не получает claim. `accepted` получает claim под
прежним ID. Запуск прежнего owner, который уже получил claim, переходит в
`interrupted` без replay с первой фазы; подтверждённые этапы и точные receipts
сохраняются. Если терминальный journal commit не состоялся, GET наблюдает
остановленного owner как `interrupted`, не объявляя успех по памяти worker.
Полное продолжение частично исполненных фаз относится к Б2-07.
Warehouse maintenance подтверждает quiet/held только после освобождения обоих
существующих locks: job admission и короткого writer. Уже допущенный job,
включая ожидание SQLite claim, должен завершиться до подтверждения held.
При free admission статус повторно читается по тому же scoped ID: terminal commit
между первым чтением и освобождением lock не превращается в ложный interrupted.

Browser хранит ключ до POST в localStorage, отдельно по principal. Потерянный
ответ и reload восстанавливаются только exact GET. Автоопрос раз в 5 секунд
ограничен 20 минутами и 240 попытками, один запрос — 15 секундами;
unknown/error прекращает опрос. По истечении бюджета UI предлагает обновить
страницу для проверки той же заявки и сохраняет её ключ.
Доказанный busy отказ без принятия (`request_accepted=false`) освобождает ключ;
неоднозначный timeout (`request_accepted=null`) сохраняет его для exact GET. Состояния
accepted/queued/deferred/consumer_pending/interrupted и ошибки не разрешают новый
эффект. Подтверждённый success освобождает ключ для следующего явного запуска.
Legacy caller без ключа получает устойчивый номер, но не может восстановить
неизвестный ему номер после потери ответа.
Старые compacted `diff.lines` с `item_count/details_omitted` не содержат точного
числа изменённых складов и SKU. Exact reader сохраняет terminal status и исходные
details, использует отдельно сохранённые точные счётчики, а при их отсутствии
возвращает `null` и показывает «—» с пояснением. Отсутствие подробностей не равно нулю.

## Выпуск и откат

Миграция добавляет поля в существующие runs и два частичных unique index.
Существующие rows, phases и таблицы не удаляются. Bootstrap выполняется при
построении journal до регистрации HTTP routes; GET не вызывает bootstrap.
Новые request/receipt поля сохраняются без диагностического `_bounded_details`;
legacy automatic diagnostics сохраняют прежнее ограничение.

Старый код открывает additive schema, но старый HTTP reader не умеет читать
новые публичные alias и подбирать accepted. Поэтому обычный откат на версию
до этого изменения **останавливается**, пока выбранные accepted/running jobs
не завершены либо не остановлены с сохранением состояния и согласованным
планом восстановления. Сначала остановить приём новых ручных заявок и снять
query-only список `public_job_id`, `run_id`, `status`, `request_scope`, `attempt_id`.
Сохранить operational DB подходящим штатным способом резервирования. Не удалять
pending/alias и не выполнять обратную миграцию. Если нужна непрерывная доступность
этих ID, откат должен сохранять reader/picker этой версии или совместимый backport;
иначе откат блокируется. Это не обещание автоматического отката через старый UI.

## Проверки

- `python3 apps/warehouse_durable_identity_smoke.py`: temp-process crash до claim,
  после подтверждённого эффекта и после terminal; startup pickup, точный ID старше
  50 строк, concurrent acceptance/claim, scope/auth, conflict, query-only SQL.
- `python3 apps/warehouse_current_sync_job_smoke.py`: общий admission с CLI,
  два POST, источник изменён до publication, ошибки и отмена handshake.
- `python3 apps/warehouse_update_journal_smoke.py` и
  `python3 apps/warehouse_process_fixture_smoke.py`: legacy journal и owner fencing.
- `python3 apps/warehouse_legacy_status_smoke.py`: реальная legacy compaction,
  exact ID старше 50 строк, query-only, неизвестные и сохранённые точные счётчики.
- `python3 apps/warehouse_durable_identity_browser_check.py`: локальный Chromium,
  реальные функции template, ключ до POST, потеря ack/reload, bounded polling.
  Эта адресная браузерная проверка выполняется локально; её Playwright dependency
  не добавляет изменение trusted PR selector в этот участок.

Аварийные тесты используют только временные данные, никогда production.
