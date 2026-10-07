# Чистка ключей: детерминированная ручная сверка

Реализация данных и локального механизма проекта WBC 0072K2. Никакие службы,
таймеры и WB-вызовы этим кодом не включаются. Web-интеграция C и связная цепь записи D реализованы. Внешний допуск закрыт
по умолчанию; production-включение относится к отдельному этапу E.

## Границы

- `packages/contracts/search_cluster_cleaner.py`: точные account/target/profile
  идентичности, явный каталог согласованных моделей, типизированный read port.
- `packages/domain/search_cluster_classifier.py`: чистая функция `classify(query,
  Profile | mapping)`. Возвращает `verdict`, `rule`, `reason` и разобранные факты;
  вход не содержит исходных меток, статистики или старого excluded-состояния.
- `packages/domain/search_cluster_sources.py`: объединение list/statistics/minus
  по точной строке. Missing/null не становятся пустым ответом. Статистическая
  строка сохраняет `state=statistics`: это кандидат новой формулировки, а не
  доказательство текущей активности. Явные excluded/archived имеют приоритет
  при сборке полного снимка; excluded не служит смысловой меткой решения.
- `packages/application/search_cluster_cleaner_store.py`: только logical store
  `operational` через `StoreRegistry`, явная установка схемы. Чтения read-only.
- `packages/application/search_cluster_cleaner.py`: короткие транзакции команд,
  профиль, baseline, вопросы, неизменяемые решения, очередь, расписание и leases.
- `packages/application/search_cluster_cleaner_worker.py`: один явный `tick()`;
  read source вызывается за пределами DB-транзакций. Нет фонового потока при
  открытии страницы или принятии команды. Порт записи подключается только явной
  композицией `product_tick` с внешним допуском.

Исходные файлы аудита и приватный эталон в Git не входят. Фикстура
`apps/fixtures/search_cluster_cleaner_holdout.json` содержит только отдельные
синтетические примеры и профили. Начальный независимый прогон прошёл до передачи
содержания исполнителю; последующие прогоны — регрессионные.

### Безрамочные стёкла

Профиль `frame=none` использует те же модельные и coating-правила, что остальные
стёкла. Указание `без рамки`, `без окантовки`, `безрамочное`, `No Frame`,
`no-frame` или `NoFrame` распознаётся единым `frame_requests`; оно не заменяет
совместимую модель и не обязательно для допуска обычного запроса на стекло.
Запрос без модели остаётся `BROAD`, несовместимая модель — `MODEL_WRONG`, смешанные
модели — `MODEL_MIX`. Все три покрытия `clean/matte/anti` сохраняются.

Для `frame=none` положительный запрос на окантовку, чёрную рамку или явно
`рамку стекла` исключается правилом `FRAME`. Для `frame=black` положительный
безрамочный запрос исключается правилом `NO_FRAME`. Эти конфликты проверяются
до неизвестной лексики, как остальные однозначные несовместимости. «Без чёрной
окантовки» и «не с чёрной рамкой» не подтверждают рамку; обычное «с рамкой»
без уточнений тоже не доказывает окантовку. Рамка, прямо названная установочной
или предназначенной для установки/наклеивания, не считается краем стекла.
Это уточнение применяется только к рамке-приспособлению: явная окантовка и
«рамка стекла» сохраняют конфликт даже рядом со словами об установке.
Отдельный установочный бокс не скрывает явно запрошенную окантовку.
Неописанная лексика и неоднозначные отрицания продолжают давать `review`;
application сохраняет прежнее осторожное `review → allow`, не переводит все
такие запросы в исключения.

`executable_rules_digest` включает код классификатора, `frame_requests`, все
его шаблоны, а также проекцию официальной карточки и её helper/regex. Изменение
этих правил меняет сохранённую семантическую идентичность. Новые кандидаты
требуют свежего scan/prepare под выпущенным кодом; digest или исторические
решения вручную не переписываются.

## Подключение в C

Создать `CleanerStore(StoreRegistry(runtime_dir))` и `KeywordCleaner(store,
Account(server_seller_id, server_account_scope), owner_username=server_owner)`.
`initialize(generation=...)` явно устанавливает пустую схему. На production это
обычная миграция operational; отключённый cleaner, готовность baseline=false и
restore_hold=true сохраняются по умолчанию. Реальное включение и admission
поколения относятся к отдельному внедрению.

`Principal(username, authenticated, auth_enabled, ads_access)` создаётся только
из проверенной web-сессии. Поля не принимаются из POST. В C остаются обязательными
same-origin/CSRF проверки каждого mutation route по проекту, а также ads-role.
`CLEANER_OWNER_USERNAME` приходит из server-owned конфигурации. Отсутствие owner,
аутентификации или рекламы блокирует команду на application-уровне.

| Внутренний маршрут | Метод сервиса |
|---|---|
| GET summary | `summary(principal)` |
| GET requests/{request_id} | `get_request(request_id, principal)` |
| POST settings | `update_settings(payload, principal)` |
| POST runs | `start_run(payload, principal)` |
| GET runs/{id} | `run_detail(id, principal)` |
| GET reviews | `reviews(principal, cursor='', limit=50)` |
| POST reviews/{id}/decision | `decide(id, payload, principal)` |
| GET history | `history(principal, cursor=0, limit=50)` |
| GET profiles/{nm_id} | `get_profile(nm_id, principal)` |
| POST profiles/{nm_id}/versions | `create_profile(nm_id, payload, principal)` |
| POST profiles/{nm_id}/activate | `activate_profile(nm_id, payload, principal)` |

Команды сохраняют `request_id`/digest и ответ атомарно; повтор возвращает прежний
результат, другой digest даёт 409. Запрос результата доступен исходному actor.
Все POST требуют request_id, изменяющие версии — expected_revision. Поле
`decision` строго allow/exclude. Профиль передаётся объектом `profile` с category,
models, kind, frame, source, verified_at (UTC/aware ISO date); version назначает
сервис. Активация получает `version`. Техническая ошибка имеет `code` и
`http_status`; query и reason в UI выводятся только текстом.

Сводка разделяет last_scan/current_work/queued, pending_count/unresolved_count,
профили и target holds. `transport_enabled=false` и `dry_run=true` честно обозначают
режим этапа B. `would_exclude` нельзя подписывать «Исключено»; подтверждённых WB
добавлений на этом этапе всегда 0. Неизвестный полный охват явно остаётся видимым.

## Подключение worker и D

`ReadSource.catalog()` возвращает валидированные Target и причины неполноты,
`ReadSource.snapshot(target)` — Snapshot для точной пары. Адаптер обязан держать
ограничение попытки чтения 120 секунд/не более трёх сетевых чтений, отдельные
сетевые timeout и общий account limiter. В B все источники — фикстуры.

`CleanerWorker.tick()` записывает heartbeat, плановую дату, атомарно захватывает
queued работу и вызывает порт чтения. Слот общий для scan/manual_apply, FIFO;
только один running на аккаунт. Возобновление истёкшего lease выдаёт новый token;
старый token не может сохранить результат. После dispatch по операции возможно
только независимое readback, даже когда enabled=false. Эта очередь не держит
слот следующего scan. Контроль токена/поколения встроен во все worker-команды.

`pending_candidates(run_id)` возвращает точные observation/decision и версии,
в том числе stats-only и подтверждённые minus-кандидаты на возврат. Для manual_apply список ограничен сохранёнными target,
query_hash и decision_id; новые невиденные вхождения не присоединяются незаметно.
Изменившаяся ревизия вопроса даёт 409. Override действует только на тот же SKU и
точную фразу с подходящим fingerprint. Каждый полный новый снимок снова сверяет
ранее известные фразы с активным профилем, точным override и согласованной
baseline. Старые решения остаются в неизменяемом журнале.

D реализует prepare/preflight/atomic dispatch admission, сетевой
one-submit и readback в операциях/items, узкое расширение change_registry и
server-owned admission вне business backup. Настройки и профиль используют ту
же БД, поэтому D вызывает `_lease` и проверяет текущие версии в своей **единой**
транзакции с реестром. Простое наличие pending candidate или lease не даёт
сетевого разрешения. `restore_hold` в БД по умолчанию закрыт, но не заменяет
внешний запрет после восстановления и проверку остановки старого процесса.

Таблицы write_operations/items/readback_jobs подготовлены. Уникальность запрещает
два неразрешённых намерения одной цели; dispatch_count не больше 1 и не может
уменьшаться. Завершённые операции, baseline, версии профиля, решения, команды и
журнал неизменяемы. D добавляет факты/проверки dispatch, включая восстановление
устаревшей копии; B не выдаёт fixture-чтение за проверку этого протокола.

`finish_run(..., remaining=...)` сохраняет queued-продолжение manual_apply в одной
транзакции с освобождением слота. Остаток ограничен исходными decision identities;
изменившиеся решения и dispatching/submitted/unresolved цели не получают новый
submit через продолжение. Уникальны apply_group_id/continuation_number.

## Приёмка

- `python3 apps/search_cluster_cleaner_smoke.py`: operational/команды/leases,
  scheduler/due reread/fairness, source union, профили и development-защита.
  Содержит реальную 30-секундную задержку read source при 100 быстрых GET summary.
- `python3 apps/search_cluster_cleaner_holdout_smoke.py`: 215 синтетических фраз,
  109 allow / 60 exclude / 46 review, 6 профилей/3 покрытия. Первый независимый
  результат: 0 ложных allow/exclude, 161/169 = 95.27% автоматических ответов среди
  однозначных, все 46 review оставлены review. Восемь осторожных review не правились.
- `apps/search_cluster_cleaner_replay.py --audit-root ... --output ...
  --verified-at ...`: приватный корпус 8207, реконструкция эталона audit/interview,
  проверки 375/310 и импорт/reimport в изолированную временную operational-БД.
  Отдельные baseline labels не выводятся из наблюдённого excluded-состояния.

Сверка исторической базы дала 8207/8207 (1333 allow,6874 exclude), 375/375 и
310/310. Импорт создаёт 33 профиля и 8207 baseline/decision/observation, 0 запусков
и 0 операций записи. baseline_ready сохраняется false: два исторических
расхождения и полнота текущего охвата не закрываются этим этапом.

## Web-интеграция C

`packages/application/search_cluster_cleaner_web.py` подключается один раз из
`RegistryUploadHttpEntrypoint.__init__`. Это setup схемы и read-only проекции для
подписей товаров, списка профилей, истории и числа ещё отправляемых операций.
Проекции используют только operational; сборщик карточек и WB source не создаются.
Классификатор и бизнес-переходы B не изменены.

Явные настройки окружения сервера:

| Настройка | Назначение |
|---|---|
| `SELLER_PORTAL_CANONICAL_SUPPLIER_ID` | Точный продавец существующего приложения. |
| `CLEANER_ACCOUNT_SCOPE` | Точный рекламный аккаунт, согласованный с будущим адаптером D. |
| `CLEANER_OPERATIONAL_GENERATION` | Явное поколение operational; несовпадение с сохранённым поколением закрывает команды. |
| `CLEANER_OWNER_USERNAME` | Нормализованный username существующей web-учётной записи с доступом к рекламе. Роль администратора сама по себе недостаточна. |
| `CLEANER_WEB_ORIGIN` | Точный публичный origin, например `https://vitrina.example`, без завершающего `/`. На loopback fixture допускается origin из listening socket. Клиентские Host/X-Forwarded-* не определяют этот допуск. |

При отсутствии продавца, scope или поколения сервис не устанавливает схему и
показывает «Чистка ещё не настроена». Отсутствие owner, готовой базы или совпадения
поколения отображается как безопасно выключенный режим. Включение при
`baseline_ready=false` или `restore_hold=true` запрещено. Чтение и открытие UI не
меняют настройки, не создают задания и не запускают worker. Конфигурация C не
является разрешением на сетевую запись: внешнее admission и readback принадлежат D.

Маршруты из таблицы выше работают под префиксом
`/v1/sheet-vitrina-v1/ads/keyword-cleaner`. Входной адаптер
`packages/adapters/search_cluster_cleaner_http.py` берёт Principal исключительно
из действующей проверенной web-сессии. Каждая POST-команда требует JSON,
`X-WB-Keyword-Cleaner-CSRF: 1`, непустой точный Origin и отсутствие cross-site /
same-site контекста. Поля actor/owner/account и неизвестные поля запрещены.
Сохраняется общий maintenance/barrier: POST получает 423, GET остаётся доступным.
При отключённой аутентификации cleaner закрыт даже в локальном приложении.

Все принятые POST возвращают 202; внутри ответа сохраняется результат B.
`request_id` и digest остаются атомарными с командой; повторный запрос не создаёт
повторного действия, изменение digest или устаревшая revision дают 409.
GET `/requests/{request_id}` доступен только исходному actor. GET-ответы имеют
`Cache-Control: private, no-store`.

UI использует фактическое место рекламы: **Управление SKU → Реклама →
Рекламные ставки / Чистка ключей**. Существующий рекламный panel и preview ставок
сохранены. Старый `?tab=ads` открывает ставки; `?tab=ads&ads_tab=keyword-cleaner`
открывает чистку; `cleaner_view=history|profiles` восстанавливает вложенный экран.
Шаблон и скрипт чистки выделены в `sheet_vitrina_v1_keyword_cleaner.html/.js` и
включаются в общий шаблон через его обычный renderer.

На основном экране три метрики с разной семантикой B, вопросы с точными кампаниями
и решениями, расписание Екатеринбурга, включение и ручной запуск. История и
версии профилей открываются отдельно. Черновик профиля не активируется скрыто.
Текст query/reason/source выводится как текст, без HTML-интерполяции. Подписи
товаров собираются из подтверждённого профиля; неизвестный товар требует настройки.
Известные active/statistics/minus фразы пересматриваются при каждом полном
снимке. «Оставить» возвращает фразу из minus только после отдельного точного
readback отсутствия. Смысловая неопределённость даёт allow и пометку «спорное»;
невалидный запрос, профиль или неполный ответ WB остаются технически недоступны.

Запросы браузера ограничены десятью секундами, включая чтение тела ответа.
Неоднозначный POST восстанавливается только GET исходного request_id, который
сохраняется в sessionStorage с областью account+actor до отправки. До выяснения
результата новые команды заблокированы. Перезагрузка сохраняет этот запрет и
восстанавливает ту же команду. Необнаруженная команда не отправляется повторно
автоматически; UI предлагает повторить чтение результата. Polling текущей работы
использует растущий интервал до 30 секунд, не вызывает WB и прекращается после
завершения. При выключении и живом dispatch/submitted показано «Останавливается»;
после этого отдельный unresolved-индикатор остаётся даже при пустом списке вопросов.

### Локальная проверка C

Все данные и логины следующих программ синтетические. Runtime временный,
слушается только `127.0.0.1`, auth включена; никакие системные таймеры не создаются.

```sh
python3 apps/search_cluster_cleaner_http_smoke.py --output /tmp/cleaner-http.json
python3 apps/search_cluster_cleaner_browser_smoke.py --output /tmp/cleaner-browser
python3 apps/search_cluster_cleaner_web_fixture.py --serve --mode normal
```

Последняя команда выводит локальный URL и тестовый логин; Ctrl+C завершает сервер
и удаляет временный runtime. Доступны также режимы `empty`, `partial`, `failed`,
`unresolved`, `unready`, `profile-required`. Они являются состояниями тестовой
модели, а не подтверждением production-обработки. Для browser smoke нужен локальный
Playwright с Chromium; для HTTP smoke достаточно зависимостей приложения.

Приёмка C: 46 HTTP-проверок; 100 настоящих GET во время 30-секундного ожидания
фикстурного источника, p95 roundtrip 17.233 мс и максимум 17.783 мс. Браузер:
29 проверок, включая потерю POST-ответа, восстановление после reload, deadline,
двойной клик, запрет прямого POST не-владельцу, XSS, partial/failed, пустые вопросы
с unresolved и явную активацию профиля. Отдельно пройдены общий auth smoke,
23 regression-проверки B, прежний browser preview ставок и narrow 390 px layout.
Приватные screenshots/receipts не входят в Git. Эти результаты подтверждают
локальный этап C, не WB write transport, production admission или выпуск.


## Подтверждаемая запись D

Продуктовая цепь: `CleanerWbSource` → `CleanerWorker` → `CleanerWriter` →
`prepare_search_cluster_operation_in_transaction` → один set-minus →
`CleanerReadback` → сохранённые summary/history. `product_tick` собирает эти
части явно; `apps/search_cluster_cleaner_worker.py` — одиночный entrypoint.
Он не создаёт supervisor/systemd/timer и не содержит переключателя активации.
`python3 apps/search_cluster_cleaner_worker.py --fixture` выполняет один полный
синтетический тик с временными путями и loopback WB, без чтения реального токена.

`CleanerWbSource.from_env(account)` использует существующий
`official_api_runtime.load_runtime_config`, канонический `WB_API_TOKEN`,
`SELLER_PORTAL_CANONICAL_SUPPLIER_ID` и server-owned account scope. `sid` токена
проверяется локально на совпадение продавца; это проверка привязки, а не замена
авторизации WB. Производственный origin строго `https://advert-api.wildberries.ru`.
Отдельный fixture-конструктор допускает только loopback HTTP. Браузер не передаёт
источник, аккаунт или токен. Сеть в этапе D была только синтетической локальной.

Каталог проверяет `count.all`, группы/count/advert_list, дубли и точные adverts.
Отсутствующий в adverts ID даёт явную ошибку каталога, сохраняя корректные
вернувшиеся цели той же пачки. Предварительная проверка конкретной цели строга:
кампания, SKU, состав всех nm, payment/bid/status и полный minus перечитываются.
Поддержан только проверенный manual CPM; null и отсутствующие пары не становятся
пустыми ответами. `stats` запрашивается за вчера/сегодня; этот период не доказывает
активность либо полный охват всех запросов WB. Stats-only кандидат допустим;
перед записью точная фраза должна снова присутствовать в статистике, а явные
excluded/archived имеют приоритет.

На аккаунт общий интервал normquery не меньше 0.5 секунды, для stats отдельный
интервал не меньше 6.1 секунды. Статистика берётся за семь календарных дней,
включая текущий; она сужает допуск новой строки к записи, но не доказывает, что
WB примет полный список. Чтение одной snapshot — три вызова list/stats/minus.
Общий бюджет обработки цели 120 секунд охватывает начальное чтение, fresh
preflight, ожидание лимита и CAS; отдельный HTTP-вызов ограничен 20 секундами или
меньшим runtime timeout. Абсолютный receive deadline проверяется при каждом recv,
включая медленные headers/body. 429 откладывает последующее чтение с Retry-After.
Запись не повторяется при 429/5xx, разрыве или redirect; HTTP redirects не следуют.

Writer сохраняет буквальный before, отдельные additions/returns и итоговый
`(before ∪ additions) − returns`. Предел 1000 проверяется до сети; 1001 не
разбивается и не обрезается. Без добавления или возврата POST отсутствует.
Текущая minus-фраза может возвращаться по точному allow-решению: её наличие
подтверждено полным minus. Новое исключение требует свежей точной статистики;
list-only фраза классифицируется и получает техническую причину
`statistics_missing`, но не попадает в payload. Остальные доказанные изменения
пары сохраняются в одном full-set, а batch помечает пару частичной и продолжает
независимые пары. Если доказанных изменений нет, для пары нет WB POST.
После ожидания лимита CAS повторяет enabled/settings/rules/profile/fingerprint,
точную override revision (либо её отсутствие), lease/token/generation,
restore_hold, общий business write barrier, внешний допуск, digest кандидата и
свежесть всех preflight наблюдений (не старше 30 секунд). Операция, отдельные
items общего реестра и dispatch_count=1 появляются в одном BEGIN IMMEDIATE,
одном operational connection и одном commit. Сеть начинается после commit.

Внешний `AdmissionGuard` хранится в отдельном каталоге **вне runtime/business
backup**. Отсутствующее/повреждённое состояние означает hold. На всё время
продуктового тика берётся process flock; владелец проверяется по PID и времени
старта процесса. `activate` — отдельная server-owned процедура release/recovery,
которой нужны подтверждённая baseline, поколение и ссылка на основание; она не
вызывается web/tick/флагом окружения. Живой старый процесс блокирует замену.

Перед operational commit fsync сохраняет внешний seal операции/digest/цели.
Если БД затем отказала, seal может закрыть дальнейшую запись до разбора; это
сознательный отказ от повторной отправки при неопределённости. Восстановленный
queued backup не проходит сверку с внешними seals, даже когда более новый
operational-журнал полностью утрачен. Загрузка старой env-конфигурации не снимает
этот запрет. Нет команды удаления seals или автоматической смены поколения.
Технический hold не снимается по неизвестному/неполному ответу. Полный текущий
снимок, где ранее пропавшая фраза снова присутствует и классифицирована,
может доказанно закрыть только исторический `external_state_drift`; остальные
причины hold остаются. Возврат из minus — guarded change 1→0.

Реестр расширен только в items/facts: `search_cluster/excluded`, boolean
0→1 и 1→0, query_hash и неизменяемая domain-строка с буквальным query. Прежние
price/bid/campaign dataclass identities, значения, ключи и сериализация не
получают пустого query-поля. Миграция двух таблиц копирует прежние столбцы
буквально и сохраняет FK/индексы/триггеры. Отдельные unique-индексы сохраняют
старую уникальность. Направление 1→0 устанавливает отдельная offline-миграция
`apps/search_cluster_cleaner_registry_upgrade.py`: read-only план с SHA строк,
DDL, поколения и размера, затем явный `--apply --expected-plan-sha256` с
fsynced 0600 журналом только двух таблиц и schema objects. GET, startup и worker
не запускают upgrade. Перед apply нужен внешний backup; rollback старой схемы
допустим только до новых бизнес-записей, после подтверждённого возврата нужен
forward repair. Транзакция проверяет неизменность trigger/index DDL, строк и
`foreign_key_check`. Общий observer не включает search_cluster в свой interval
state. Связь item(query A)→fact(query B) запрещена также SQL-триггером.
При выпуске нового кода со старой CHECK-схемой runtime может стартовать, но
`summary.registry_ready=false`, список массового выбора сообщает
`registry_upgrade_required`, новые single/batch POST получают 503 без durable
команды, а worker не потребляет старую очередь. Дополнительно общий реестр
отказывает до dispatch/POST. После reviewed backup, отдельного offline apply и
post-check read-only gate становится ready, очередь продолжает те же ID.
Исторический Stage E bootstrap receipt хранит SHA общей схемы на момент
первоначального импорта. После этой отдельной управляемой миграции его старый
readback закономерно не доказывает текущую общую схему; повторно запускать
bootstrap или переписывать его immutable event нельзя. Доказательство новой
схемы — reviewed plan, fsynced journal и post-check миграции; после неё
проверяется обычный manual Stage E preview/apply/readback на тестовом runtime.

Readback использует независимые durable jobs и lease, работает после выключения
и не занимает FIFO-слот scan/manual_apply. Для обычной ambiguous/partial
операции поздняя сверка сохраняет прежнее durable продолжение без нового POST.
Только для уже известного validation HTTP 400 после трёх недоступных readback
операция получает «требует разбора» и target hold, без бесконечного polling.
Для non-200 writer сохраняет
ограниченную redacted receipt (status, request id при наличии, hash и excerpt)
отдельно от readback. Распознанный validation HTTP 400 после обязательного
readback становится terminal rejected + target hold; буквальный `before` не
обрезается и новый POST не создаётся. Подтверждённое добавление или возврат
получает fact с фактическими 0→1 либо 1→0; следующие чтения добавляют evidence
к тому же fact. Совпадение всего ожидаемого списка закрывает исходную операцию.
Неожиданное отсутствие старой фразы, не указанной в returns, либо extra ставит
target hold; корректирующей записи нет. Неопределённая цель A
не мешает следующей цели B. Ошибка авторизации раннего readback прекращает новые
записи аккаунта в этом тике.

Manual_apply запускается из очереди после сохранённого решения. Бюджет,
выключение и общая ошибка аккаунта сохраняют недопущенный хвост в одном
продолжении; отправленная цель туда не входит. Поздняя сверка связана с исходным
run, не изменяет его завершённый summary/run_finished и видна отдельным событием.
Summary разделяет automatic/manual/late counts. UI показывает подтверждённые
исключения и не подписывает их прежним предупреждением о кандидатах dry-run.

### Проверки D и граница E

- `apps/search_cluster_cleaner_write_smoke.py`: связный fake WB, stats-only,
  1000/1001/0, freshness/CAS/storage faults, crash, настоящий SQLite backup/restore,
  lost journal, disable/readback, fairness, manual continuation, bounded slow body.
- `apps/search_cluster_cleaner_registry_smoke.py`: populated legacy fixture
  старой схемы, сохранение старых строк, rollback миграции, повторная инициализация,
  точные items/facts и повторное evidence без дублирования перехода.
- HTTP/browser smokes включают `running_fixture('confirmed')`: настоящая
  продуктовая цепь с fake WB, 2 automatic и 1 late manual; исходный scan неизменен.
- Сохранены B smoke/holdout и регрессии общего registry, внутренних writers,
  observer, ставок. В CI зарегистрирована только нужная cleaner browser suite;
  её output указан во временном каталоге, Playwright/Chromium — явная dependency.

Все перечисленные подтверждения относятся к локальной синтетике. Полнота
актуального WB-охвата, baseline_ready, два исторических allow/still-excluded и
production admission остаются открытыми. Классификатор не менялся:
`f8c3bccd2d843949a4416bf9f44ed0d507bb1a7414ce775311208e6a48627863`.
Этап E требует отдельного задания на внедрение, штатной резервной копии
operational, проверки серверного процесса/поколения и исходной базы. Этот этап D
не даёт разрешения на live WB, merge/deploy или включение таймера.
# Stage E: ручной запуск

Production bootstrap выполняется только через `search_cluster_cleaner_manual_v1`
и private server-owned package. До доступа к operational SQLite runner проверяет
права `0600`, hash package и независимого `current-card-evidence.json`,
канонические seller/scope и generation. Bootstrap импортирует immutable baseline,
создаёт закрытый admission и публикует только server-owned `stage-e-config.json`.
Расписание не создаётся и не включается: после bootstrap API отклоняет включение
авточистки и изменение времени.

Владелец выбирает в UI точную CPM-кампанию и SKU с утверждённым профилем и
нажимает «Почистить ключи». Старый список `manual_admission` остаётся
проверяемым историческим receipt пилота, но не ограничивает новые CPM-кампании
для тех же утверждённых SKU. Допуск опирается на immutable package profiles
и опубликованные добавления SKU, SHA-bound approved raw card source, активный semantic fingerprint и свежий
официальный WB detail точной пары; CPC полностью исключён из списка и счётчиков.
UI сохраняет одну явную команду с request ID. Отдельный
server worker исполняет её через общий Production Apply launcher и Stage E:
exact scan → подготовка сохранённых кандидатов → один submit → readback.
Legacy queued scan без новой команды UI никогда не исполняется сам.
Preview сверяет текущий WB snapshot и свежую official Content API карточку с
package-bound approved raw source по версии проекции, влияющей на правила:
категория защитного стекла, точная совместимость, тип покрытия и рамка.
Производитель телефона, совместимость и цвет рамки берутся из структурных
характеристик WB по их ID. Тип покрытия подтверждают распознаваемый артикул,
название или явная характеристика типа стекла; обычный рекламный текст
описания не служит самостоятельной проверкой профиля. Противоречивые или
неизвестные модели и типы останавливают чистку до уточнения.
Явные характеристики `Тип/Вид стекла` и `Тип покрытия` должны подтверждать
распознаваемый тип; характеристики с точными именами `Покрытие` и `Эффект`
учитываются только когда прямо называют тип стекла. Технические свойства вроде
олеофобного слоя не меняют правила чистки. В структурной характеристике
`Прозрачное` подтверждает обычное
прозрачное стекло.
Порядок характеристик нормализуется по уникальному ID; несущественное содержание
карточки допускает редактуру, а изменение или неоднозначность этих четырёх
свойств и semantic fingerprint активного профиля закрывает допуск.
Свежая проекция должна в точности совпадать с архивной Content-проекцией.
Список моделей Content может быть непустым подмножеством утверждённого профиля:
два ранее утверждённых профиля дополнительно включают iPhone 18 при карточках,
где WB пока указывает только iPhone 17. Новую совместимость в текущей карточке
это правило не разрешает. Свежая категория подтверждается `subjectID=1571`
из официального ответа; для архивной карточки без subjectID используется
утверждённое название. Проекция версии `phone_glass_v1` используется при
проверке допуска; сама карточка не копируется в журнал операций.
Структурный «Цвет рамки = бесцветный» подтверждает `frame=none` только вместе
с явной безрамочностью в названии или артикуле. Сам бесцветный цвет не доказывает
отсутствие окантовки; неизвестные цвета остаются неподтверждёнными. В проверенном
формате артикула точные префиксы `No Frame Clean`, `No Frame Matte` и
`No Frame Anti-Spy` подтверждают покрытие; несовпадение с названием или явной
характеристикой закрывает допуск. Все структурные модели сохраняются, включая
несколько совместимых поколений. Схема проекции остаётся `phone_glass_v1`,
поскольку её поля и значения не изменены; новая исполняемая семантика отражается
в `executable_rules_digest`.
apply не повторяет отправку при любом неоднозначном ответе. Readback работает
только с тем же production operation и его internal write operation. Existing
exclusions возвращаются только при точном согласованном `allow` после полного
свежего minus и guarded full-set. Target с действующим hold не допускается к
записи; отсутствие профиля или несовпадение карточки остаётся техническим отказом.

#### Runbook ручного Stage E

До bootstrap release-owner приватно размещает только вне runtime/operational
backup каталог `/var/lib/wb-core/search-cluster-cleaner-admission` (`root:root`,
`0700`) и два файла `approved-baseline-v1.json` и
`current-card-evidence.json` (`root:root`, `0600`). Для self-service также
размещается byte-identical `card-source-approved.json` (`0600`), SHA-256
которого совпадает с package provenance `fresh_cards_sha256`. Он сверяет полные SHA-256
файлов с reviewed receipt. Package содержит canonical seller/account
scope/generation, immutable baseline/profiles/provenance и exact verified
manual-admission; evidence содержит exact current-card digest и verified_at на
каждый допустимый nm. Эти private files и исходные WB-выгрузки не попадают в
Git или release workflow.

Там, где старый `current-card-evidence.json` не содержит SKU, Stage E берёт
package-bound approved source как исходный бизнес-эталон и **обязательно**
сверяет его с новой official Content карточкой перед prepare и каждым write.
Старый evidence и private package не изменяются. Любой drift карточки,
неподтверждённый профиль, не ручной CPM contract, изменившийся статус/SKU или
held target закрывают выполнение. Completed/archive видны как недоступные,
расписание всегда выключено.

#### Добавление новых SKU в допуск

`packages/application/search_cluster_cleaner_onboarding.py` расширяет допуск
отдельным проверенным пакетом, сохраняя historical `approved-baseline-v1.json`,
`current-card-evidence.json`, `card-source-approved.json`, baseline, старые
профили и историю исключений. Новый пакет находится в private
`extensions/<extension_id>/extension.json` и `cards.json` внутри каталога
допуска (`0700` для каталога, `0600` для файлов, symlink не допускается).
`extension.json` имеет schema `search_cluster_cleaner_admission_extension/v1`
и связывает canonical seller/account/generation/owner, SHA исходного baseline
пакета, выпущенный runtime SHA, `rules_version/rules_digest`, версию проекции,
точные новые профили, SHA `cards.json`, дату утверждения и provenance.
Карточки сохраняют точные business fields официального Content API и
`card_digest=sha256:digest(card_without_digest)`; production-материалы остаются
вне Git. Свежая официальная карточка должна в точности подтвердить утверждённую
проекцию каждого добавляемого профиля.
Чтение свежих Content-карточек ждёт `0.7` секунды перед каждым запросом,
включая первый запрос отдельного preview/apply прохода. Ожидание входит в
общий срок `90` секунд. Ответ `429` завершает подготовку до claim; автоматической
повторной отправки или расширения допуска нет.

Release-owner использует существующий зарегистрированный adapter
`search_cluster_cleaner_manual_v1`: request с `mode=admit_profiles`,
`extension_id` и `extension_sha256`. Общая envelope получает один operation ID,
ожидаемый runtime SHA и затем точные `prestate_sha256/candidate_sha256` из
preview. Порядок — preview → один apply → readback того же ID. До команд
`create_profile/activate_profile` fsynced `sku-admission-journal.json` фиксирует
`claimed`; частично добавленные профили остаются недопущенными. Общий admission
loader подключает добавление только после `published`, когда все active profiles
подтверждены readback. Потеря ответа или частичная запись не разрешает повторить
apply. Для явного восстановления используется новый operation ID и
`mode=admit_profiles_recover` с `original_operation_id` и теми же утверждёнными
байтами добавления. Публикация сохраняет исходный неизменяемый claim.

Строгое совпадение текущих runtime SHA, `rules_digest` и версии проекции нужно
для preview/apply/recovery нового допуска. Уже опубликованное добавление и его
исторический readback сверяют исходный immutable journal candidate и SHA
утверждённых файлов, а затем заново проверяют их текущим проектором против
точных утверждённых профилей. Поздний выпуск правил не переписывает исторический
пакет и не исключает SKU только из-за прежнего digest; новые write-кандидаты
получают текущий digest, а устаревшие решения остаются под защитой writer.

Добавление профилей не чистит ключи, не меняет расписание и не отправляет запись
в WB. После подтверждённого допуска владелец делает отдельный ручной запуск;
prepare и каждый write по-прежнему проверяют свежую карточку, активный
fingerprint, точную CPM-пару и полный свежий набор минус-фраз. Менять private
baseline/config или обходить checks ради отсутствующего SKU не требуется.
Проверка добавления и восстановления —
`python3 apps/search_cluster_cleaner_onboarding_smoke.py`.

Массовая команда замораживает выбранные точные пары и статусы одной записью;
worker проходит их последовательно через тот же одноцелевой Stage E guard.
Authenticated bootstrap owner получает доступ к cleaner через server-auth
маркер, без подмены configured cleaner owner или `actor`: batch и child журналы
сохраняют реального инициатора и account/generation binding.
Кампания проверяется по fresh official count и detail, detail читается
порциями до 50 ID с общим deadline. Размер HTTP body ограничен; лимита в
100 пар для пользователя нет. Если COMMIT SQLite вернул BUSY, только успешный
rollback даёт ответ `storage_rolled_back`: UI сверяет тот же request ID и
может повторить идентичную команду. Потеря ответа и любой иной неизвестный
исход разрешают только readback того же ID до доказанного результата.

При последовательном выполнении batch временный `SQLITE_BUSY` на COMMIT
локального claim не завершает всю группу. Доказанный rollback сохранения stage
или занятое чтение identity хранилища переводят worker в `storage_wait`:
сохранённые child, batch и точный operation ID остаются в очереди до следующего
прохода; незапущенный хвост не помечается ошибкой. Для ещё не привязанного
`queued` run
достаточно отсутствия Stage E binding и write operation. Для уже привязанного
run после истечения lease внешний guard дополнительно проверяет остановку
прежнего writer, все прежние seals, отсутствие seal и dispatch/readback job у
текущей операции, точный состав подготовленных фраз и account/generation.
Только тогда одна транзакция помечает старую подготовку как
`cancelled_before_send`, возвращает тот же run в очередь и записывает
неизменяемый recovery event. Старые записи остаются в истории, но не входят в
новый effective итог. Если процесс погиб после COMMIT до очистки capability,
следующий readback завершает эту очистку по той же записи. Тот же внешний
operation ID получает новый fresh preview с ограниченной сохранённой паузой.
Сохранённый полный scan snapshot после ошибки финального COMMIT завершается из
его фактов без повторного WB-запроса. После исчерпания локальных попыток группа
ожидает явной сверки текущей пары в UI; старые завершённые группы автоматически
не открываются. Любой seal, dispatch right или неизвестный ответ WB разрешает
лишь readback, без второго submit.
Выделение cleaner в отдельное хранилище остаётся будущей архитектурной задачей;
текущий ремонт не меняет общий SQLite, его journal mode или другие модули.
Отдельный общий deploy-drain остаётся будущей задачей: для первого выпуска
нужна согласованная проверка отсутствия исполняемой операции непосредственно
перед остановкой прежнего worker.

В server env с CAS и отдельным readback добавляются только
`CHANGE_REGISTRY_ACCOUNT_SCOPE`, `CLEANER_BOOTSTRAP_PACKAGE_PATH` и
`CLEANER_WEB_ORIGIN`; seller берётся из canonical server setting. Никакие
`CLEANER_*` web-trio и timer unit до bootstrap не публикуются. Production
Apply отправляет `mode=bootstrap` последовательно preview/apply/readback с
одним operation id. Preview требует пустой cleaner namespace и не делает WB
вызовов. Readback требует exact import digest baseline, exact active payload
profiles, сохранённый package receipt/config, held admission без owner/seals,
`enabled=false`, `restore_hold=true`, `transport_enabled=false` и отсутствие
таймера. После этого один controlled restart web и health readback создают
только read-only CleanerWeb projection.

Partial bootstrap восстанавливается только под held admission: сверяются
foreign SQLite schema fingerprint и отсутствие pre-existing cleaner namespace,
удаляется/переустанавливается лишь namespace `cleaner_*` из immutable package
и journal. Такой recovery не заменяет operational SQLite и не откатывает
свежие business data.

Новая UI-команда durable привязана к exact campaign/SKU; status и детали фраз
читаются по тому же job ID после reload или потери ответа. Worker обрабатывает
только такие команды и не имеет timer. Stage action `mode=manual`
preview/apply/readback сначала выполняет exact scan; он не пишет в WB. Для
statistics-fresh `pending_exclude` worker запускает `mode=manual_prepare`
preview/apply с exact scan run, target и candidate digest. Это создаёт ordinary
exact `manual_apply`; затем отдельные
`mode=manual` preview/apply/readback с новым exact operation id делают один
submit и readback только его internal operations. Reusing the same operation
id is readback-only. Перед launcher invocation worker сохраняет apply claim;
после crash этот этап только читает прежнюю операцию. Любой ambiguity, drift
или crash оставляет held admission; recovery закрывает только expired/dead-owner
exact run и никогда не создаёт replacement submit. Исторический partial run
не переписывается при позднем подтверждении: UI получает `effective_state`
из точных write items и показывает confirmed/pending с причинами. Отдельный
loopback listener 127.0.0.1:8776 обслуживает только cleaner API, чтобы долгий
Web Vitrina render не блокировал status; он запускается лишь при валидной private
конфигурации. Release probe до final metadata проверяет initialized worker в
`armed` и закрытое расписание; после выпуска отдельный read-only probe проверяет
`ready`. Scheduler остаётся выключен: API отклоняет `enabled=true` и любое
изменение времени.

#### Массовая ручная чистка

Массовый запуск начинается только после новой явной команды владельца в UI.
GET eligibility отдельно читает официальный каталог WB и показывает точные пары
кампания/SKU, разрешённые private admission, активным профилем и текущим
контрактом manual CPM. Активные кампании предлагаются по умолчанию,
приостановленные можно выбрать отдельно. Завершённые и архивные видны с
причиной недоступности: текущий безопасный contract разрешает только статусы
9 и 11; статус 7 не расширяется одной галкой. Отсутствие товара в кампании,
изменённый профиль и hold закрывают конкретную пару. Ошибка или неполнота
каталога не превращается в пустой успешный список.

POST перечитывает выбранные campaign IDs из WB и одной транзакцией сохраняет
упорядоченный immutable exact set, категории статусов и request ID. Нельзя
молча исключить из выбора недопущенную пару. Один server worker последовательно
создаёт прежние одноцелевые self-service задания с детерминированными child IDs.
Перед каждым child он снова проверяет точную пару и статус: изменившаяся или
утратившая допуск пара явно пропускается с причиной. Stage E выполняет
остальные fresh card, candidate, one-submit и exact readback guards без обхода.
Потеря ответа, reload и перезапуск worker восстанавливают тот же parent и child;
неподтверждённая операция останавливает следующие пары до повторного чтения
того же child. Расписание не включается. Первый production batch запускает
только владелец после выпуска и проверки UI; deploy/startup не создаёт intent.

Если прежняя minus-фраза отсутствует даже в полном объединении list/stats/minus,
для пары сохраняются `external_state_drift` и target hold. Если она есть в
текущем active/statistics и согласованное решение — allow, это уже выровненное
состояние: повторное исключение и бизнес hold не создаются. Только доказанный
scan без write operation и с точным событием настоящего drift позволяет
перейти к следующей паре. Неизвестный результат WB/dispatch не становится
автоматическим повтором. Технически отложенная list-only фраза показывается
отдельно, а остальные пары продолжаются, когда их данные полны.
Для ранее остановленной на таком scan группы владелец может явно продолжить
тот же frozen batch: сервер проверяет исходные child и отсутствие поздних
запусков, сохраняет старый журнал и создаёт задания только для не начатого
хвоста. UI отдельно показывает завершённые, удержанные и ещё не начатые пары.

Смысловые `review` правила без технической ошибки переводятся в allow и
помечаются спорными; точный owner override и согласованная baseline имеют
приоритет. Явный доказанный запрет чужой модели, бренда или покрытия проверяется
до неизвестного слова, но отрицание не выдаётся за позитивное доказательство
запрета. Полный scan сохраняет immutable decision/event для каждой фразы и её
источника. Вручную принимать спорное решение для движения batch не требуется.
Ранее открытый бизнес-вопрос закрывается после нового полного scan, когда все
его пары получили актуальное автоматическое решение; событие закрытия остаётся
в журнале. Технический вопрос без профиля или полного источника не считается
автоматически решённым.
GET `/controversial.csv` доступен только проверенному владельцу аккаунта и
отдаёт один UTF-8 CSV для месячного разбора: уникальная target/query/rules
identity, first_seen/last_seen, before/desired/actual/confirmed, причина,
правило, digest версии, operation ID и время подтверждения. Неподтверждённый
WB-результат остаётся `unknown`; CSV-ячейки с формульными префиксами экранируются.
Экспорт только читает journal и не создаёт запуск или расписание. Расписание
остаётся выключенным; его активация вне этой реализации.

#### Работа включённого расписания и ожидание исполнителя

После отдельного включения расписания сохранённый слот `03:45` в зоне
`Asia/Yekaterinburg` соответствует `02:45` в Тбилиси. Исполнитель проверяет
очередь при старте, чтобы восстановить незавершённую операцию с тем же ID.
Затем он обрабатывает сохранённое расписание и ручные команды. Когда работы
нет, исполнитель поддерживает свежую отметку здоровья и проверяет только
номер последнего события, файлы политики/выпуска и время следующего слота.
Новое событие или наступивший срок возобновляет полный проход. Завершённые
задания и группы не перечитываются в каждом цикле ожидания; после сбоя
неподтверждённая запись WB по-прежнему восстанавливается только чтением.
