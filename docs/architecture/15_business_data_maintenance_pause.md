# Бессрочный режим обслуживания

Отдельный CLI останавливает новые business-процедуры, дожидается завершения
текущих и оставляет сайт доступным для просмотра. Master автообновлений,
feature JSON, расписания и `Persistent` не переписываются. HTTP, Finance HTTP,
Data MCP, SSH и root-storage safety monitor остаются доступны.

Верхняя жёлтая плашка: «Режим обслуживания — доступен только просмотр.
Изменения данных и запуск обработок временно отключены». Сервер блокирует
business POST/PATCH/DELETE, ручные session probes/launchers, price-status
reconciliation и explicit cleaner refresh. Site login/logout остаются доступны.
Статус recovery/SPP и cleaner каталог читаются без запуска обработок при паузе.

## CLI и восстановление

Из установленного `/opt/wb-core-runtime/app`, после проверки цели и версии.
`WINDOW_ID`, `ACTOR` и `REASON` — выбранные для этой операции значения, а не
исторический пример. Один ответственный writer ведёт окно до завершения:

```sh
python3 apps/business_data_maintenance_pause.py preflight --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765
python3 apps/business_data_maintenance_pause.py pause --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765 --window-id "$WINDOW_ID" --actor "$ACTOR" --reason "$REASON" --wait-timeout-seconds 1200
python3 apps/business_data_maintenance_pause.py status --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765
python3 apps/business_data_maintenance_pause.py resume --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765 --window-id "$WINDOW_ID" --actor "$ACTOR" --reason 'Работы завершены'
```

`preflight` и `status` не создают файлы, не меняют systemd и не запускают jobs.
Preflight требует authenticated admin activity/readiness, известную карту
процедур и восстанавливаемый baseline **до первой мутации**. Смешанные
`enabled/inactive` и `disabled/active` поддерживает новый CLI; старый master/hold
отказывает таким состояниям до изменений, поскольку его restore их не сохраняет.
Masked/неизвестные состояния нового CLI также отказывают до изменений.

Baseline сохраняется в `.business-data-maintenance-pause.json`; аудит — рядом.
Барьер бессрочный: TTL и автоматического resume нет. `held` означает фактический
idle proof по SH leases, полному HTTP job readback, process/service drain и
отключённым timers. Таймаут/ошибка оставляют `draining`/`restoring` и ошибку,
никогда не заявляют quiet. Текущие writers не убиваются.

После обрыва сначала выполнить `status`. Повторить `pause` с **тем же window-id**
для завершения drain либо `resume` для точного восстановления baseline. Не
удалять state/barrier/lock и не создавать другой baseline поверх незавершённого.
После неоднозначного локального systemctl результата продолжение читает
фактический результат шага; уже достигнутое состояние не отправляется повторно.
Resume проверяет baseline, конфигурацию unit fragments/drop-ins и feature intent,
восстанавливает прежние enabled/active состояния, сохраняет проверенный receipt
и снимает барьер последним. Drift оставляет явный отказ и активный барьер.

Пропущенные циклы не переигрываются гарантированно. Catch-up под ещё активным
барьером может получить `skipped_maintenance`; это не successful data update.
Обычные будущие старты восстанавливаются с прежним `Persistent` и конфигурацией.

## Startup и выпуск внутри паузы

HTTP startup явно готовит приватный admission lock, не обнуляет barrier.
Поддержанные production actors работают под root; readiness/permissions должны
быть проверены после выпуска для HTTP, Finance HTTP и cleaner child. Отсутствующий
или недоступный lock при pause preflight не означает idle. Обычные автономные
entrypoints до инфраструктурной инициализации сохраняют прежний режим запуска.

При HTTP restart warehouse pending picker не стартует при активном барьере;
cleaner supervisor может поднять idle child, но business cycle/catalog refresh
не запускается. Существующий release reconcile сохраняет paused business timers
и change-registry observer для `window_kind=maintenance_pause`, root-storage
monitor исключён. Штатный release уже перезапускает Finance HTTP; отдельной
системы выпуска не добавлено. Изменение конфигурации защищённых units внутри
паузы требует разбора drift до resume, а не обхода проверок.

Отдельная derived read-only history сборка требует явной области техоперации;
обычный выпуск не запускает её и не включает профиль расписания.

## Фоновые workers

FF inventory/overhead preview, SKU Balance calculation, WB supplies backfill,
transit-cost enrichment и manual change-registry observer удерживают SH admission
lease до выхода worker, включая сохранение результата/ошибки и `finally`.
Caller admission предшествует durable acceptance; FF claim находится внутри
admitted worker. Уже принятую работу пауза не прерывает.

При отказе допуска или доказанном незапуске child FF остаётся `accepted` для
`resume_incomplete`; остальные finite starts сохраняют ошибку и освобождают слот.
Неопределённый startup не доказывает no-start: lease/слот/ссылка сохраняются,
допустим только readback того же job. `KeyboardInterrupt`/`SystemExit` пробрасываются.
Raw SKU poll также не очищает ссылку без no-start proof; `finally` очищает только
собственную ссылку. Native proof реализован в admission-коде и проверяется
`business_data_procedure_admission_smoke.py` и `business_data_async_workers_smoke.py`.

SKU live apply получает admission перед claim каждого job и удерживает его через
submit, readback, recovery неопределённого результата и terminal evidence.
Poll/wakeup ожидание не держит lease; pending/recoverable jobs ждут pickup после
resume. Admission lifetime и drain наблюдают по уже существующим jobs;
отсутствие активной работы не доказывает lifetime в production.
Первый переход со старых raw-thread путей имеет
[отдельную границу выпуска](../runbooks/release_recovery.md#первый-выпуск-admission-leases-для-фоновых-workers).

Фиксированный переход в общее расписание описан отдельно в
[dormant schedule profile](../runbooks/business_data_cycle_schedule_profile.md).
Он сохраняет старый baseline и использует отдельный точный target receipt;
обычный resume отказывает при intentional unit drift. Код сам не включает
профиль; применение требует фактической readiness и проверенного target-плана.
