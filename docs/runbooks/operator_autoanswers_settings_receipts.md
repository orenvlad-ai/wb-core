# Квитанции native настроек AI

Команда `ai-settings:<id>` сохраняется в существующей транзакции AI-настроек.
Native audit содержит исходные операнды, actual actor, seller/account scope,
точные before/after, исходную дату и settings revision. Режим с автоматическим
запуском дополнительно связывает выбранный preview, sweep и transition run.
Повтор ID с другим содержимым или principal отклоняется. Прежний ID после
изменений или ABA возвращает исходную квитанцию и не меняет текущие настройки.
Новые ID на использованный preview не принимаются.

`source_saved` подтверждает сохранение настроек. Лимиты этим документом не
подтверждают новый запуск, расходы OpenAI или публикацию WB. Для смены режима
выполнение проверяется отдельно: существующий lifecycle owner даёт датированную
проверку конкретных timers/services и epoch. Только полная native проверка
может завершить действие. Удобные флаги `running`/`matched` без unit proof не
достаточны. Lifecycle ошибка после commit оставляет зелёную квитанцию сохранения
с отдельным статусом «Требует внимания». Native observation append-only;
исходный settings command не переписывается. Повтор/GET не вызывает lifecycle.

UI сохраняет ID до одного POST, под Web Lock точного source fence. Вкладки одного
scope используют тот же ID; потеря ответа или неполная квитанция разрешает только
GET `/v1/sheet-vitrina-v1/operations/<id>`. Reload читает retained ID. Новый ввод
не подменяет неопределённую прежнюю команду. Без Web Lock/доступного localStorage
запись не отправляется. У OFF отдельный fence: он получает текущий native epoch
и сохраняет выключение через существующий CAS, не ожидая неизвестный ON или
лимиты. Состояние UI после принятия читается обычным GET текущих настроек;
датированная квитанция не подменяется текущим состоянием после другого CAS.

Журнал — query-only проекция фиксированного native AI-файла. Он не создаёт
repository, не мигрирует БД, не запускает executor и не обращается к WB. Native
`feedbacks` + `autoanswers_admin` grants применяются перед source reader/count,
затем actual actor + configured seller/account scope перед поиском/page/detail.
В auth-disabled режиме используются ровно существующая native settings
авторизация и `local_operator`; production auth-enabled не обходится.

Существующий POST/GET `/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings`
сохраняется. Новый HTTP путь не добавлен: используются существующие common
journal/detail GET. Root проверяет публичную маршрутизацию. Legacy native вызовы
без `operation_id` остаются совместимы и не получают фиктивную квитанцию.
Schema v11 добавляет только append-only guards typed settings/observation audit;
обычный migration owner устанавливает их, query-only GET не делает bootstrap.

Локальная проверка: `PYTHONPATH=. python apps/operator_autoanswers_settings_smoke.py`.
Все fixtures временные, native HTTP/SQL и actual forms работают с fake lifecycle
или synthetic systemd adapter. Реальных WB/systemd/production действий нет.
