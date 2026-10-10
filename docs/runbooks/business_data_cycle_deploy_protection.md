# Выпуск кода при выбранном трёхчасовом профиле

## Граница защиты

Для выбранного business-data schedule profile каждый runtime deploy требует
**заранее удерживаемое и полностью тихое** окно существующего
`apps.business_data_maintenance_pause`. Выпуск не создаёт окно и не возобновляет
работу автоматически. Legacy runtime без выбранного профиля и без owner сохраняет
прежний контракт.

Pause закрывает admission и останавливает только таймеры, затем ждёт текущие
допущенные workers без остановки сервисов. Shared admission lease тяжёлого цикла
удерживается до конца worker. Поэтому уже начатый цикл заканчивается до выпуска,
а новый не может пройти между drain и последним restart/readback.

Тяжёлый цикл может идти около 57 минут; Release Runner ограничен 45 минутами.
Ожидание drain выполняют **до запуска Runner**. Default pause timeout 1200 секунд
ограничивает ожидание, а не срок окна. Timeout оставляет явный `draining` и активный
barrier; сервисы не убиваются, sync не начинается. Продолжение использует то же
окно и сохранённый baseline. Нельзя заменять baseline или открывать admission
ради выпуска.

## Порядок выпуска и ownership

1. Ответственный создаёт явный pause с точным window ID и причиной, затем получает
   fresh status: pause `held`, barrier `held/hold_confirmed`, тот же window и
   baseline fingerprint, admission idle, timers paused и activity quiet.
2. Для `live_runtime` доверенный Runner перед merge выполняет bounded (120 секунд)
   `claim`. Нет held/quiet, foreign owner, неизвестная цель или SSH timeout —
   отказ до merge. `repo_only` не обращается к runtime.
3. Owner хранится в приватном `.business-data-deploy-owner.json` внутри runtime
   directory. `prepared` связывает release operation ID, window и baseline.
   Перед первой runtime mutation `start` под теми же restore/barrier locks
   атомарно проверяет identity и переводит owner в `mutation_started` с exact SHA.
4. Sync, prepare-deploy, service reconciliation, restart, activation и completion
   CAS проверяют того же active owner. Central barrier acquire/confirm/restoring/
   abort/release и обычный restore/resume отвергаются при active owner до изменения
   timers или фаз. Optional autoanswers quiesce не отключает guard prepare-deploy.
5. Runner удерживает owner через последний restart и полный внешний
   `runtime_readback`. Только exact complete markers и живой registry позволяют
   `finish` → `complete`. Standalone hosted adapter завершает owner после своего
   полного readback. Это снимает только ownership: **pause остаётся held**.
6. Ответственный отдельно выполняет существующий exact resume того же окна.
   Исходный pause baseline и owner intent не заменяются.

Если выпуск изменил конфигурацию units или owner intent относительно baseline,
обычный exact resume честно откажет при drift. Успешный deploy не разрешает
перезапись baseline; такой случай требует отдельного согласованного recovery.

Для единственного изменения literal formula-epoch в установленном
finished-snapshot service после exact completed deploy предусмотрен отдельный
[проверяемый formula resume](business_data_formula_resume.md): он доказывает
точную before/after конфигурацию и сохраняет исходный baseline. Во всех остальных
случаях действует прежний exact resume; редактирование baseline не разрешено.

## Диагностика и неопределённость

Owner можно прочитать без изменения через:

```sh
python3 -m packages.application.business_data_deploy_protection status \
  --app-dir "$APP_DIR" --runtime-dir "$RUNTIME_DIR" --env-file "$ENV_FILE" \
  --operation "$RELEASE_OPERATION_ID"
```

Значения берут из проверенной target-конфигурации и receipt; не используют
примерную identity. `status` не меняет owner, pause, timers или runtime.

Prepared owner можно отменить только явным `cancel-prepared` с тем же operation и
точным fingerprint прочитанного owner. Проверка фазы и запись cancellation идут
под тем же lock, что `start`: concurrent cancel и start не могут оба победить.
Отмена снимает только owner и не открывает окно. При uncertain merge сначала
читают исходную GitHub operation; cancellation сама по себе не означает rollback.

После `mutation_started` cancellation запрещена. Неопределённый SSH/timeout
оставляет owner и pause удержанными. Обычный повторный deploy/start отвергается;
нет blind resend. Только существующий canonical recovery с его exact original
operation, prestate и one-shot stage claim может выполнить разрешённый хвост.
Все поддержанные mutating recovery cases защищены, включая derived storage status,
normal restart tail, activation и stdin metadata CAS. Если installed helper
отсутствует, recovery отказывает до этих mutations.

Canonical completed-claim readback сначала проверяет exact runtime completion и
затем завершает owner. Неопределённый owner finish даёт `ambiguous`, не complete
receipt. Повторное чтение той же claim может завершить только owner; оно не
повторяет sync/restart/activation/CAS. Уже completed owner читается без перезаписи.

Границы первого выпуска со старым Runner и runtime описаны в
[частных случаях выпуска](release_recovery.md#первый-выпуск-защиты-deploy-ownership).
Они применяются только к соответствующему переходу, а не к каждому выпуску.

## Свежий цикл после обслуживания

После полностью проверенного exact restore один свежий запуск допускается,
если внутри этого окна пропущен фиксированный трёхчасовой слот и до следующего
слота осталось **не меньше 7200 секунд**. Проверка использует полную длительность,
без округления, и повторяется под actual heavy EX непосредственно перед записью
canonical acceptance receipt. Persistent timer catch-up проходит ту же серверную
проверку. Обычный distinct scheduled slot сохраняет прежнюю семантику; расписание
не сдвигается, сбор FBS остаётся раз в 3 часа; 9 часов — максимальный допустимый
возраст полного снимка. Тяжёлый single-flight сохраняется.

Central release перед открытием admission сохраняет приватный
`.business-data-cycle-wakeup.json`, только если latest fixed slot внутри окна
не имеет canonical receipt и исходный восстановленный owner действительно
включён. Несколько пропусков объединяются в один latest slot. Уже принятый слот,
включая failed/interrupted, никогда не переигрывается. Baseline pause не меняется.
Latch связан с точными window ID, started_at и plan fingerprint; новое окно делает старую
потребность obsolete и не получает права от её записи.
Deadline fence для самого пропущенного слота сохраняется до следующего distinct
слота даже после нового окна: superseded потребность не разрешает поздний
Persistent catch-up или допуск прежней signed identity.
При несовпадении generation тот же пропущенный слот консервативно закрыт до
следующего distinct slot, включая раннее завершение второго окна. Проверка
действует и при actual admission: старый coordinator не получает допуск после
смены окна между prepare и POST/приёмом.

Существующий registry HTTP server проверяет pending latch при startup и раз в
30 секунд своим service hook. Короткий daemon worker использует существующий
canonical loopback launcher, transport lock/journal и signed identity.
Запуск coordinator привязан к точному пропущенному слоту: signed identity из
GET prepare проверяется до journal/POST. Задержка до нового слота завершает старый
долг ожиданием штатного таймера; новый слот не заимствуется этим worker. После
prepare прежняя identity всё равно проходит свежую серверную проверку при допуске.
Новых systemd units/drop-ins нет. GET/status/preflight показывают метаданные и причины,
но не запускают работу и не меняют latch. Diagnostic `maintenance_wakeup` доступен
в существующем read-only prepare endpoint `/v1/business-data-cycle/dispatch`.

Полное завершение resume доказывает released barrier того же окна и сохранённый
до release exact restore receipt с совпадающим fingerprint. Поэтому crash между
barrier release и финальным `phase=restored` не теряет потребность: service hook
может прочитать этот proof после рестарта, а повторный exact resume завершает
только bookkeeping. При отсутствии proof или новом обслуживании admission остаётся
закрыт обычными guards. Initial target schedule transition не создаёт долг нового
профиля за время старого расписания: fixed profile ещё не был выбран. Исходный
baseline и штатные guards target transition не меняются.

До отправки POST catch-up ждёт свободного heavy admission и обязательную backup
priority через bounded read-only проверки. Если время ожидания вышло за первый час
слота, coordinator сначала записывает `wait_next_slot` с причиной late/busy/uncertain
и больше не запускает этот долг. Если гонка после prepare исчерпала единственный
POST, сохраняется `single_submit_not_accepted` либо честная неопределённость той же
identity. Blind resend запрещён; после deadline неопределённый старый transport
разрешается только readback при следующем distinct scheduled slot. Сервис не
обещает повторную попытку текущего слота. Неизвестный результат внешней операции
и старый interrupted цикл этим механизмом не восстанавливаются.

Текущий worker не убивается ради catch-up или следующего слота. Если сам HTTP
service недоступен, durable latch и transport journal остаются до его штатного
возврата; правило двух часов всё равно проверяется заново. Окно с выключенным
owner, пауза без пропущенного слота и обычное чтение не создают новую потребность.
