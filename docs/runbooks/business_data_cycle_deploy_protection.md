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

## Первый выпуск этой защиты

До первого sync installed owner helper отсутствует. Trusted candidate source
передаётся inline только после независимой проверки **существующих installed**
pause/profile/barrier/admission APIs. Claim/start и checks первого mkdir/rsync
используют этот bootstrap; rsync receiver не печатает служебный JSON в протокол.
Затем используется installed helper из exact synced code. Проверки bounded;
force/weakening режима нет.

Старый trusted Runner ещё не содержит pre-merge claim, а старые central release
APIs ещё не знают owner. Поэтому **первый выпуск выполняет один ответственный
writer под заранее доказанным explicit held/quiet pause**; concurrent resume,
barrier release, иной deploy и restore запрещены эксплуатационно до sync новой
защиты. Первый pre-merge участок опирается на это явное окно. Не выдавать его за
автоматически защищённый новым Runner. Subsequent releases используют новый
trusted Runner и central owner guards. При неизвестном исходе первого sync
сохраняют окно и читают ту же operation; старый runtime без helper не допускает
новый generic recovery.

Старый Runner передаёт PR/head, но ещё не передаёт operation ID. Новый adapter
до claim восстанавливает **тот же release-v3 ID**, который старый Runner запишет
в receipt: trusted `workflow_run` event, successful Gate run/jobs, один immutable
checked plan с верным hash, exact PR/head/base и единственный merge parent,
совпадающий с plan base, плюс текущий checkout exact merge. Это шесть read-only
GitHub запросов с существующим timeout 30 секунд каждый. Недостаток, timeout или
drift evidence блокирует выпуск до claim/sync; release context никогда не
переходит на manual identity. Вне release context сохраняется standalone manual
identity. Active owner не переименовывается. Поэтому поддержанный recovery после
первого sync получает исходный ID и не оставляет foreign owner.
GitHub после merge может вернуть пустой список PR links у Gate run; он не
является обязательной связью. Binding дают checked plan PR/head/base, env PR/head
и exact merged PR/parent. Непустой противоречивый список links отвергается.

Первый старый Runner также не передаёт defer-finish: candidate adapter завершает
owner после собственного полного readback, а явное ручное окно продолжает
держаться через оставшийся внешний readback старого Runner. Single-writer
граница первого выпуска действует до его окончательного receipt; resume до
этого запрещён. Новые Runner удерживают сам owner через внешний readback.

Этот блок не восстанавливает старый uncertain цикл. Его canonical receipt
остаётся interrupted с честным исходом; следующий distinct scheduled slot
допустим после exact resume. Persistent timer catch-up может быть пропущен под
barrier. Автоматического replay, resume или новой глобальной очереди нет.
