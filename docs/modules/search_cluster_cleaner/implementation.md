# Чистка ключей: этап B

Реализация данных и локального механизма проекта WBC 0072K2. Никакие службы,
таймеры и WB-вызовы этим кодом не включаются. Этапы C (web-интеграция) и D
(транспорт и реестр подтверждённых изменений) подключаются последовательно.

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
  открытии страницы или принятии команды и нет WB write transport.

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

D владеет реализацией prepare/preflight/atomic dispatch admission, сетевым
one-submit и readback в операциях/items, узким расширением change_registry и
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
