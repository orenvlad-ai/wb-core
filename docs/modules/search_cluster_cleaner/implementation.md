# Чистка ключей: этапы B, C и D

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
  доказательство текущей активности. Явные excluded/archived имеют приоритет.
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
в том числе stats-only. Для manual_apply список ограничен сохранёнными target,
query_hash и decision_id; новые невиденные вхождения не присоединяются незаметно.
Изменившаяся ревизия вопроса даёт 409. Override действует только на тот же SKU и
точную фразу с подходящим fingerprint; старые окончательные решения закреплены.

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
Прежние решения не пересматриваются, «Оставить» не выполняет возврат исключений.

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
интервал не меньше 6.1 секунды. Чтение одной snapshot — три вызова list/stats/minus.
Общий бюджет обработки цели 120 секунд охватывает начальное чтение, fresh
preflight, ожидание лимита и CAS; отдельный HTTP-вызов ограничен 20 секундами или
меньшим runtime timeout. Абсолютный receive deadline проверяется при каждом recv,
включая медленные headers/body. 429 откладывает последующее чтение с Retry-After.
Запись не повторяется при 429/5xx, разрыве или redirect; HTTP redirects не следуют.

Writer сохраняет буквальный before и before∪additions. Предел 1000 проверяется
до сети; 1001 не разбивается и не обрезается, пустое добавление не вызывает POST.
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
этот запрет. В v1 нет команды удаления seals, автоматической смены поколения,
снятия target hold или компенсационного возврата исключений.

Реестр расширен только в items/facts: `search_cluster/excluded`, boolean
false→true, query_hash и неизменяемая domain-строка с буквальным query. Прежние
price/bid/campaign dataclass identities, значения, ключи и сериализация не
получают пустого query-поля. Миграция двух таблиц копирует прежние столбцы
буквально и сохраняет FK/индексы/триггеры. Отдельные unique-индексы сохраняют
старую уникальность. Общий observer не включает search_cluster в свой interval
state. Связь item(query A)→fact(query B) запрещена также SQL-триггером.

Readback использует независимые durable jobs и lease, работает после выключения
и не занимает FIFO-слот scan/manual_apply. В одном окне до трёх чтений с шагом
20 секунд, затем отсрочка 300 секунд; Retry-After может увеличить её. Каждая
появившаяся новая фраза получает один fact; следующие чтения добавляют evidence
к тому же fact. Совпадение полного списка закрывает исходную операцию. Missing
old/extra ставит target hold; корректирующей записи нет. Неопределённая цель A
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
