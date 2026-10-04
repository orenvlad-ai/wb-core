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

Из установленного `/opt/wb-core-runtime/app` (команды выполняет ответственный
production writer после выпуска и проверки версии):

```sh
python3 apps/business_data_maintenance_pause.py preflight --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765
python3 apps/business_data_maintenance_pause.py pause --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765 --window-id WBC0069K16-maintenance-20261004 --actor WBC0069K16 --reason 'Подготовка истории' --wait-timeout-seconds 1200
python3 apps/business_data_maintenance_pause.py status --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765
python3 apps/business_data_maintenance_pause.py resume --runtime-dir /opt/wb-core-runtime/state --env-file /opt/wb-ai/.env --base-url http://127.0.0.1:8765 --window-id WBC0069K16-maintenance-20261004 --actor WBC0069K16 --reason 'Работы завершены'
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

Явно разрешённая отдельная derived read-only history сборка остаётся контролируемой
техоперацией; этот PR не запускает её, не включает режим обслуживания и не
меняет UI/history reader, cron, owner-policy или business данные.

## Проверки кандидата

Новые focused smokes: `business_data_procedure_admission_smoke.py`,
`business_data_maintenance_pause_smoke.py`, `business_data_maintenance_boundary_smoke.py`.
Проверены overlap/nested/fork ownership, принятый async handoff через race,
thread.start/finally failure, specialized warehouse до регистрации job,
writer drain/no TTL, pause/restore interruption, unit drift и exact restore.
HTTP auth smoke дополнен side-effect GET и cleaner PATCH/DELETE; browser smoke
проверяет жёлтый текст и работоспособный read filter. Finance HTTP и существующие
cleaner/feedback/SPP/default maintenance/deploy-barrier smokes пройдены локально.
Полные команды, hashes и приватные результаты — в `candidate-final-005.json`
в evidence задачи; production acceptance выполняется отдельно после выпуска.
