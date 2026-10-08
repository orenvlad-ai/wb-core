# Приём изменений справочника SKU

`operator_nomenclature.py` — адаптер версий существующих источников, а не второй
справочник или универсальный исполнитель. Нативные таблицы номенклатуры и групп
остаются источником истины. Нативные `nomenclature_activation_intents` и
`DenseFbsService` владеют активацией; `WbFinanceWeeklyBlock` владеет проверкой
себестоимости; существующий owned consumer номенклатуры выполняет чтение WB.

## Приём и восстановление

Операторская запись справочника, её before/after версии, автор, immutable request
и обязательное продолжение фиксируются одной короткой транзакцией. Приём не
выполняет недельный Finance rebuild, Dense plan или обращение к WB. Зелёная
квитанция с `primary_effect=source_saved` доказывает сохранение источника;
`physical_applied=false` не означает движение запасов. Новый активный SKU сначала
имеет `activation_status=pending` и остаётся `is_active=0` до нативной публикации.

Браузер создаёт `opsku_<32 hex>` до единственной отправки и сохраняет только
непрозрачные ID в localStorage, в пространстве текущего автора. Сервер связывает ID
с фактическим authenticated actor, действием и своим digest разобранного запроса.
Клиент не сравнивает JS canonical JSON с Python JSON. Excel привязан к SHA256
исходных байтов. После неизвестного ответа разрешён только GET того же ID:
`/v1/sheet-vitrina-v1/settings/nomenclature/operations/{id}`. Чужой автор получает
404; explicit foreign domain или несовпадающий ID не дают зелёную квитанцию.
GET использует read-only connection и не создаёт схемы/службы/задания.

Идемпотентный ответ ищется до новой генерации item ID и любых внешних обращений.
Изменённый payload того же request ID отклоняется. Ошибка после source commit
разрешается точным readback сохранённого ID. Известный business rejection допускает
новый ввод только вместе с server proof `source_not_saved` для того же ID; 404
после неизвестного POST не разрешает повторную отправку.

## Версии и доказательство обработки

`operator_source_revision` — fingerprint полной native строки. CAS повторяется
в writer transaction. Клиентский before-version имеет приоритет; сервер также
фиксирует native before-state нормализации для старых клиентов и строк Excel,
которых нет в текущем видимом списке. Excel «Включено» сохраняет requested active
для pending intent; API `is_active` продолжает показывать только applied факт.
Неизменённое «нет» в explicit import отменяет ожидающую активацию.
Group/unique/retirement guards выполняются
под source transaction. Нельзя отключить группу с действующими или ожидающими
активации SKU. Hide остаётся изменением видимости; резервы, остатки и отмена
нативной активации сохраняют существующие правила.

Стоимость проверяет настоящий stale-cost consumer Finance. Его read-only план
захватывает только текущие exact saved versions; короткая CAS подтверждает тот
же источник и canonical cost dependency. `already_current` означает настоящую
оценку сохранённой версии и `derived_no_change`, а не успешный общий цикл или
текущий timestamp. `applied` подтверждается target image и post-verification,
`post_verify_stale_week_count=0`, неизменными источниками и non-target state.
Отсутствующий proof не равен нулю. Crash после native Finance commit до ack
восстанавливается настоящей повторной проверкой current projections.

`purchase_price_yuan` является справочной ценой для поставщика. Историческая
себестоимость Finance использует нативные приходные/canonical cost источники;
этот адаптер не заменяет её новой ценой CNY и не вводит новую формулу.
Изменение mapping проверяется настоящим Finance consumer. Группы и обычная
видимость без незавершённого cost demand дают короткий source-only terminal
receipt без фиктивного складского задания. Последующая версия переносит
нерешённое cost demand, но не выдаёт proof предыдущей версии за новое.

Активация подтверждается exact source revision и immutable Dense intent/event
с настоящим coverage fingerprint в той же transaction, которая включает SKU.
Новая native revision не стирает собственное уже сохранённое свидетельство
предыдущей квитанции. Непроверенная superseded версия — `needs_attention`.
Техническое ожидание Finance/Dense/provider остаётся retryable processing.

## Чтение WB и дочерние версии

Explicit row/bulk barcode sync принимает durable READ task. Operator-bound
`auto_save` отсутствующего ШК тоже не обращается к провайдеру в HTTP. Existing
owned nomenclature drain сохраняет immutable ответ WB, повторно использует
native matching/ручной override и native source writer. Дочерние operation ID
детерминированы родителем и captured card/index. Crash после child source commit
читается по тому же ID; карточки и случайные item ID повторно не создаются.

Auto continuation сначала ждёт exact первоначальные Dense/Finance proofs, затем
создаёт нативную barcode child version с CAS первоначальной строки. В действующей
native модели Dense и начальная Finance evaluation не требуют отсутствующий ШК;
это проверено также с raw Finance продажей, имеющей только будущий WB barcode.
Следующий existing owned pass допустим: до него квитанция честно processing.
Новый ручной barcode/nmID перед child writer не перетирается. Parent completed
требует всех собственных child proofs либо доказанного native no-change.
Provider failures повторяются существующим consumer; source drift — attention.
Права и порядок manual/hidden/matching сохраняются. Новых WB writes нет.

Общий журнал подключает `journal_source()` как domain `nomenclature`, с теми же
operator/settings permissions до поиска, total и detail. Этот модуль не изменяет
общий journal router/UI. Раздел параметров/налогов и историческая policy находятся
в отдельных блоках; здесь они не меняются.

Проверки: `apps/operator_nomenclature_smoke.py`,
`apps/operator_nomenclature_browser_smoke.py` и существующие native activation,
barcode, Finance stale-cost, heavy-source и settings browser smokes. Все fixtures
временные; production данные, API WB и ручные production cycles не используются.
