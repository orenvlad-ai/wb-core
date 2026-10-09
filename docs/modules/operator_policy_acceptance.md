# Принятие параметров и политики оператором

`operator_policy.py` хранит неизменное подтверждённое намерение отдельно от
родной версии и отдельно от результата её публикации. Это доменный адаптер
трёх существующих источников, а не новая бухгалтерия или отдельный executor.

## Три результата

- `durable_saved`: final source принят; ответ «Принято / Документ сохранён.».
  Native-версия ещё может отсутствовать. Занятый расчёт не требует нового ввода.
- `source_ref.native_identity`: штатный writer применил намерение. Его binding
  сохраняется в той же транзакции, что версия источника и существующая очередь
  targeted recalculation, если она предусмотрена родным writer.
- `completed`: реальный штатный publisher проверил именно связанную версию,
  её даты и параметры. Для Proxy это functional economics с exact
  parameter dependencies, source/readback/ready manifest fingerprints;
  для инцидентов — native rematerialization audit, exact revision projection,
  transactional ready readback и неизменный non-target digest.

Проверка отсутствующего proof не равна нулю. Успех общего цикла, создание
версии, флаг очереди или одна актуальная дата не доказывают полный результат.

## Приём и восстановление

POST существующих форм принимает opaque ID `oppolicy_<32hex>` из тела и/или
`X-Operator-Request-ID`; разные alias отвергаются. ID связан с фактическим
автором подтверждения, kind и серверным semantic digest. Повтор того же ID с
тем же source возвращает исходную квитанцию. Другой source или автор не
получает её. После неоднозначного ответа браузер выполняет только exact GET
`/v1/sheet-vitrina-v1/settings/policy-operations/{kind}/{id}`. Он хранит только
номера операций, отдельно по автору и домену, и восстанавливает их после
закрытия/перезагрузки. Foreign domain и неизвестный результат не дают зелёную
квитанцию и блокируют новую отправку. Ошибка позволяет исправить форму только
при доказанном отсутствии сохранённого ID; существующий ID не удаляется.
GET не создаёт сервисов или схемы. Settings permissions применяются к Proxy,
Supply permissions — к политике складов, затем проверяется автор.

Короткая source transaction не берёт warehouse lock и не выполняет heavy
recalculation или provider call. Intrinsic business validation и CAS текущего
source выполняются до принятия. Технически отсутствующие полные stock options
не теряют подтверждённый источник: native READ и exact identity validation
выполняются штатным владельцем перед native apply. Временные имена validator
не сохраняются как canonical identities.

`dependent_replay` существующего owned warehouse cycle читает ограниченный
cutoff команд и применяет их под штатным lock. Поздняя команда остаётся для
следующего прохода. Native transaction повторяет source CAS и связывает её
точную версию атомарно; crash после commit не создаёт вторую версию. Технический
блок остаётся retryable. Чужая версия или drift авторизованного эффекта требуют
внимания. Для инцидентов сохраняется штатное last-good recovery; новый retry
разрешён только поверх собственного точного recovery binding. Чужую позднюю
revision recovery не заменяет. Потерянный ack после доказанного native
publication не откатывает успешно опубликованный источник.

## Разрешённые родными API даты

| Kind | Разрешённая дата источника | Поддержанный этим слоем publisher |
| --- | --- | --- |
| `legacy_proxy` | Явная `effective_date` от 2026-07-01; native API не задаёт верхнюю границу | Штатные открытые даты functional economics; исторические даты — typed policy publication + exact History |
| `proxy_v4_tax` | Только business date final confirm; UI не задаёт дату | При отложенном apply сохраняется исходная дата и automatic operands исходной версии; исторический эффект требует exact typed History proof |
| `wb_incident_policy` | Native ISO dates `warehouse_entries[].effective_from`, `effective_from`, `change_effective_from`, optional `effective_to`; native interval/identity guards сохранены | Current native rematerializer; исторические даты, включая даты после закрытия интервала, — typed policy publication + exact History |

Даты источника не переносятся на сегодня. Future effect ожидает своей даты.
До исторической публикации квитанция имеет точную причину
`policy_historical_authority_required`; текущая дата не заменяет старый эффект.

## Историческая authority и завершение

`operator_policy_history.py` задаёт отдельный контракт
`operator_policy_dated_publication_v1`. Он использует родной
`sheet_vitrina_v1_ready_publications`, существующий owned cycle и
аутентифицированный History supervisor. Это не товарная receipt authority и
не отдельная очередь/executor. Одна операция связывает immutable final command,
точную native version/attempt и все затронутые даты до даты захвата. Для Proxy
берутся только даты, на которых native version применима; для инцидента
проверяются также даты после `effective_to`: родной evaluator восстанавливает
факты и нулевой incident effect. Ранние даты до native V4 epoch сохраняют
существующие V4 cells; legacy Proxy не создаёт отсутствующую V4 policy.

Publisher независимо перестраивает кандидат из настоящих operand каждой даты:
сохранённых параметров, dated ready source и для инцидента actual raw stock.
Нет current/zero substitute. Exact zero activity допускает отсутствие margin,
если оба родных orderSum/orderCount явно нулевые. Non-target fence сохраняет
стоимость, количества, остальные исходные факты и даты. Saved input pins,
formula hashes, исходные ready bytes и независимое повторение проверяются
под source writer CAS перед атомарной публикацией всех dated ready targets.
Родные limits сохранены: максимум 366 дат, 160 MiB candidate. Более длинный
диапазон или отсутствующий исторический operand остаётся attention с исходной
датой; принятие источника не отменяется и полный результат не объявляется.

Нативная ready publication ещё не означает `completed`. Существующий History
worker получает только эту точную typed authority, удерживает свои четыре
lock FD и последовательно обрабатывает ограниченные portions одного frozen
source vector. Он проверяет immutable dated objects, roster/catalog/content
hash, actual CURRENT/edition и epoch/token каждой затронутой даты. Ordinary
closed backlog без этой authority сохраняет прежний предел в две даты.
Только одноразовый proof настоящего supervisor разрешает финальное
подтверждение. В финальной `BEGIN IMMEDIATE` повторяются exact pinned source
queries и выбранные dated ready digests; запись между readback и транзакцией
не может завершить оператора по старому proof. Ack родной targeted queue,
связь exact History publication и `completed` фиксируются атомарно.

Crash после ready commit восстанавливает тот же native operation/attempt.
Новый ready/book envelope допустим только при неизменных dated cells,
presentation и независимо прочитанных immutable native book полях
`wb_days`, `retained_days`, `shared_days`, `presentations` каждой даты.
Чужая lineage, operand или revision не получает authority. Изменённый code
authority оставляет старый manifest неизменным и получает
`policy_history_formula_changed` attention; fair scan продолжает следующие
документы. Автоматической миграции старого proof на другой code epoch нет. Законно вытесненная
native версия получает source-owner outcome `native_source_drift`; основной
цикл продолжает другие документы. Отсутствующие исторические operands остаются
честным attention. Fair cohort ограничен 32 строками, сортируется по
сохранённому `history_last_checked_at`, затем исходному времени/ID; один вызов
может публиковать максимум одну операцию. Заблокированная старая операция не
лишает следующие документы прохода.

Finance cost, FBS book и цены не меняются этим policy contract: существующие
Proxy/tax и incident policy publishers меняют только свои derived presentation
cells. Новая бизнес-формула или supplier cost-history authority сюда не входят.
Общий цикл выбирает одну source authority за проход. Если склад уже подготовил
историческую стоимость по товарному документу, сначала завершаются его
History/Finance. Policy остаётся принятой до следующего прохода без незаметной
ready mutation. Оба SQLite acknowledgement нельзя выполнять по одному уже
потреблённому source-stamp; parent и fixed child отвергают такую комбинацию.
Обычный ClosedBacklog может сопровождать выбранную authority, но его даты
остаются точным собственным набором из не более двух дат. Без специальной
authority лишние даты не допускаются даже внутри общего лимита в две даты.

## Общий журнал

`journal_sources()` отдаёт три filtered native descriptors: table,
identity/kind/actor/time columns, domain, permission guard, `public(conn,row)` и
family detail path. Common journal aggregation, pagination и глобальная UI
интеграция принадлежат общему модулю. Доменные ссылки несут ID и kind; ссылка
служит routing hint для actor-bound GET и не разрешает POST.
