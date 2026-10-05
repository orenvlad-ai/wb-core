# WBC0069K16: локальный backend единой истории

Кандидат этапов 2/3 не подключён к HTTP, UI, production reader или таймерам. Действующий готовый режим 14/31 и процедуры источников сохраняются. Этап 3 добавляет opt-in live read-only bridge и ручной ограниченный candidate launcher; production запуск и публикация этим этапом не выполнялись.

## Данные и публикация

`NativeDatedCompiler` вызывает действующий `SheetVitrinaV1WebVitrinaBlock → view model → adapter` внутри короткого query-only native/FBS read context с одним timezone-aware now. Формулы не скопированы. Общий raw template/facility/исторический identity context собирается для явно объявленной истории; каталог материализуется на одном дне, а не расчётом всего диапазона. Каталог переиспользуется лишь при совпадении context epoch, включая config/metrics и source dependency epoch. `update_frozen_history` проверяет no-change **до создания компилятора**.

`HistoryStore` хранит content-addressed SQLite объекта дня с индексом `row_id`, сжатыми JSON всех 16 полей, natural membership и признаком наличия ready binding. Общий каталог не хранит search/date cells. JSON edition ссылается на каталог и дни и содержит consumed dependency vector. `CURRENT.json` атомарно переключает current/previous только после fsync завершённых объектов, каталога и edition. Полная история не копируется при обновлении.

`PENDING.json` сохраняет завершённые объекты между ограниченными проходами. Published watermark не изменяется при остановке, исключении, нехватке места, смене source proof или base edition. Новая корректировка заменяет pending target; связанные dirty даты публикуются только вместе. Неизменившиеся refs переиспользуются. Revalidate проверяет источник непосредственно перед publication; expected base защищает от устаревшего сборщика. Это локальный producer contract, не универсальная очередь.

Чтение открывает только derived файлы `mode=ro&immutable=1` и `query_only`, последовательно один SQLite connection. Максимум 366 сохранённых/запрошенных дней, 512 строк страницы, 8 MiB ответа по умолчанию, 64 KiB ячейки, 64 MiB несжатых dated cells дня/каталога, 50 000 строк каталога, 2 GiB хранилища. SKU читаются по row IDs, группе или ограниченной странице; большой диапазон может требовать меньшей страницы. Поиск строится из static labels и display text **выбранных дат**, без business math; частичная SKU страница не объявляется глобальным поиском. `accepted_ready_available` означает наличие native ready binding/колонки, а не полноту отдельных метрик. Missing не становится нулём.

GC выполняется только writer: current/previous, четыре часа для старых edition pins и pending refs. Истёкший pin возвращает `snapshot_expired`. Served objects не переписываются; временные собственные `.building-*` удаляются следующим singleflight writer. Deadline cooperative: проверяется между днями и перед CURRENT; длительный callback не прерывается этой библиотекой. Production process deadline остаётся отдельной границей подключения.

## Явная canonical семантика

Контракт `web_vitrina_dated_cells_canonical_inventory_context_v1` не утверждает полного совпадения со старым range-local inventory gate. Исторические scopes/facilities получают общий каталог и честные unavailable/inapplicable объяснения. Identity precedence: enabled current config; иначе последнее accepted historical WB identity.name в объявленной истории; иначе raw template label или nm_id. Presentation ordering следует canonical context, затем ограничивается независимо выбранным natural row union; прежний range-local порядок historic scopes может отличаться. Search использует именно этот явно выбранный порядок.

Недопустимое отличие — потеря числа. Native helper воспроизводит случай: существующее `TOTAL|total_inventory_wb_total_qty_v1=42`, без stock_total/history/current evidence; глобальный legacy gate заменяет 42 на blank. `compile()` сравнивает value/display каждой существующей natural day cell с canonical day. При отличии выдаёт `dated_compiler_numeric_context_mismatch`; edition не публикуется, last-good и unconsumed pending остаются. Metadata-only отличие не маскируется и все 16 полей сохраняются. Проверка реальных старых дней перед UI cutover обязательна; этот guard — отказ от неподдержанного случая, а не новая бизнесформула.

## Конкретный локальный source adapter и оставшаяся граница

`FrozenNativeAdapter` принимает закрытый приватный native DB и явно объявленные frozen files. Он проверяет runtime/source path equality, автоматически охватывает все runtime side files, ограничивает source proof 128 MiB и отвергает hot journal и persistent WAL main/book даже без sidecars (SQLite header read/write versions), выходящие за frozen root paths и изменения во время capture. Ready row content даёт dated tokens; ready template identities/default bindings влияют на общий epoch даже вне requested range. Все остальные и новые native таблицы, side files, обязательный formula epoch и штатная business date (`Asia/Yekaterinburg`) консервативно меняют всю историю. Старый отсутствующий revision baseline не используется как доказательство no-change: локальный adapter проверяет фактическое содержимое, включая delete/re-date.

Это работающий **LOCAL frozen adapter**, не production invalidation hook. Его полный content proof остаётся отдельным строгим локальным контрактом. Live bridge ниже не использует полный hash operational DB или этот frozen shortcut.

## Этап 3: ограниченный live source bridge

`LiveNativeAdapter` читает native SQLite через существующий `mode=ro/query_only` context. До открытия проверяет SQLite header и family: rollback 1/1 разрешён, WAL 2/2 требует уже существующие читаемые WAL/SHM; неизвестный журнал, отсутствующие sidecars или unsupported header дают отказ без repair. Operational immutable, source copy и смена journal mode не используются. Authority manifest/path/inode проверяются при pin. Тела native revision triggers сверяются с producer schema в собственной in-memory SQLite.

Material counters служат alarm/fence, а не общим dirty epoch. Датированные proofs покрывают accepted ready binding/publication и temporal content, inventory captures/components, опубликованные cost rows, выигравшие V3/V4 parameters, потребляемый weekly-SKU Finance daily slice до 22 августа, active archival/first factual acceptance и breakglass operation/cells. Jul-1 source cost соответствует более ранним датам; effective parameter winner соответствует своему фактическому интервалу. Raw Finance не читается. Book использует официальные content-addressed version/blob identities; нужные новые blobs проверяются, прежние принятые identities переиспользуются. Current sources, supplier/book evidence и intraday bucket ограничены D/D-1 и фактической датой current planning; D-6 maturity задаётся отдельно по дню. Явные formula epoch и relevant policy/config/catalog изменения могут консервативно расширить всю историю.

Untyped lifecycle quality использует один fresh native resolver на закреплённую порцию: capture, canonical и natural blocks получают тот же callable. Native source loader и bulk identity/mapping proofs читаются один раз через существующий `_rows` byte/deadline accounting; В capture компактный cursor и прежние небольшие canonical mapper queries защищены SQL progress deadline. Перед compiler этот handler штатно снимается: новые bulk reads проверяют оставшийся cooperative portion deadline через `_rows`, а небольшие mapper SQL защищены общим hard process budget родителя, без отдельного per-query interrupt. Даты раньше свежего доказанного unresolved business date сохраняют native exact-empty skip. Owner-paused fallback имеет прежний приоритет, проецируется заново из тех же pinned policy/cache bytes и не запускает native backlog scan. Scope cache переиспользуется только после проверки fresh exact source/evidence/manifest/mapping/admission proofs; обычный общий status/cursor alarm не выбрасывает неизменные scopes. Callable отвергает вызов после закрытия своего pin. Исторический partial suffix всё ещё может получить resource refusal; production throughput этим не утверждается. Общий cap приватного proof cache — 128 MiB, source capture — 32 MiB/20 секунд; новые ready планы прогреваются по частям без compiler. Cache headers/proofs и собственные row-label objects очищаются до актуального набора; cache не является consumed watermark.

Общий inventory metadata запрос сохраняет каждую capture в `capture_sequence` порядке, полный roster и `source_digest`. Вместо повторной передачи всего `source_manifest_json` он читает только потребляемую классификацию `contract == bound_inventory_quantity_v1`: эти два потребителя иных полей manifest не используют. Последний root member `contract` через `json_each.rowid` сохраняет семантику `json.loads`, включая повторные и escaped keys; остальные типы/значения contract дают прежний untyped путь. Невалидный JSON или не-object root останавливает capture, а не исключает запись. Полные строки captures по-прежнему входят в датированные `_slice` proofs со всеми полями и native alarm; components proofs, source fence и byte/time caps не меняются. Большой совокупный manifest может прогреваться полными ограниченными днями, после чего компактный metadata запрос не переносит тот же payload повторно. Отдельный smoke на native producer проверяет aggregate больше 32 MiB, реальные cold/warm proofs, равенство legacy/reference и нового vector/catalog/всех 16 полей cells, а также semantic correction неиспользуемого поля.

Полные component proofs прогреваются по завершённым immutable captures в том же приватном cache. Новый progress считается только после всех упорядоченных строк, разбора каждого provenance, полного прежнего digest и записи готового proof/scopes entry; оборванная capture не кешируется. Byte-limit при неистёкшем capture deadline и таком новом progress даёт `live_components_bootstrap_pending`; полные header/day proofs до первой component capture сохраняют прежний dated bootstrap. Начальный `update_live_history` продолжает только подтверждённый progress в том же 180-секундном portion deadline. Cache persistence failure, истёкший deadline (включая одновременно byte-limit), SQLite interruption и отсутствие нового полного proof остаются terminal. Capture внутри compiler portion и fresh publication revalidation не повторяются. Одиночная oversized capture не получает частичного proof или бесконечного retry. Ни схема/ключи cache, ни полный hash компонентов, ни source alarm/fence/CAS, ни source/cache/store caps не меняются. Native smoke проверяет aggregate больше 32 MiB с ограниченными отдельными captures, равенство всех proof/vector/catalog/cell полей reference, и отдельную настоящую oversized capture. Это owned offline evidence, не гарантия production throughput.

`update_live_history` проверяет no-change до compiler. Один короткий pin на порцию (по умолчанию максимум 31 пересчёт) содержит повторный capture и все native compiler reads; перед публикацией pin закрывается и делается fresh capture. Успешная порция требует три capture, unchanged — один. Monotone native fence отдельно отвергает конкурентный ABA, не инвалидируя историю по общему counter. SQL capture progress handler снимается до compiler, включая error path. Prepared context собирается из bounded cached row labels/capture identities; полного `_build_period_snapshot` истории ради каталога нет. Pending refs переживают смену current proof при том же base/catalog/epoch и переиспользуются только по совпавшему day proof. Опубликованный vector изменяется атомарно со всей связанной редакцией.

Исчезновение ранее accepted ready binding обнаруживается, но пока не отличимо от source retention: bridge отказывает `accepted_ready_source_disappeared` и сохраняет last-good. Это явная граница producer intent для GC/delete, а не silently erased история. Numeric-loss guard этапа 2 сохранён. Все source caps/unknown bindings оставляют published edition прежней.

`apps/web_vitrina_history_candidate_build.py` переиспользует admission, нечётное окно 01/2:55–:59 Asia/Tbilisi и hard-kill process wrapper; оставшийся абсолютный budget проверяется снова перед child. Существующий finished-builder lock открывается read-only и держится EX/NB на время child; absent/busy означает skip. `--worker` — внутренний режим parent, не отдельная операторская команда: прямой запуск не предоставляет parent admission/hard-kill boundary. Worker повторно проверяет окно и cooperative deadline. Sleep-child тест доказывает existing wrapper, а не измеренную длительность полного live build.

Финальная проводка переиспользует существующий finished snapshot service и неизменённый timer. Reader и builder используют `/mnt/wb-core-extra100/web-vitrina-history/history`; builder хранит proofs рядом с history, начинает с 1 марта 2026 и фиксирует текущую business date в parent/child. `--runtime-contract` проверяет точную mount identity/flags и запас 8 GiB + 2 GiB store + 128 MiB proofs до создания файлов и повторно в child. Epoch остаётся digest принятого списка compiler/formula hashes из `web_vitrina_history_runtime.json`; изменения HTTP/UI или installed SHA его не меняют, несовпадение formula files останавливает сборку. Одна порция — максимум 31 день, last-good сохраняется, failed child даёт nonzero service exit. Проводку можно выпускать только после построения полного готового stable store и exact resume прежней maintenance baseline: unit drift под паузой недопустим. Эта документация и repo units не утверждают фактической production активации. Предел 366 дней и остальные storage/source caps сохраняются.

Явный `--manual` для разрешённой ручной пробы снимает только calendar gate. Parent сохраняет admission/shared lock и hard process budget не более 180 секунд; internal worker получает тот же flag и cooperative budget. Без flag scheduled default не изменён. Source/cache/store caps и read-only правила одинаковы; ручной режим не останавливает занятые jobs и не меняет расписания. Адресный CLI smoke проверяет manual propagation, 180-second cap, busy/shared-lock skip и прежний scheduled window.

Пять реальных закрытых inventory дат (15 марта, 15 апреля, 1 мая, 1 июля, 2 октября) прошли native capture verification/materialization → inventory planner/view-cell helper: все 16 полей существующих natural rows совпали с canonical, numeric loss нет. Новые общие facility/catalog rows отмечены отдельно. Это inventory-only доказательство, не полный page compiler. 1 сентября quality reader достиг лимита; 4 октября current/preliminary не принят как historical proof. Полная production история/throughput bootstrap, согласованный полный historical page parity и UI cutover ещё не измерены/не разрешены. Caller обязан передать accepted code/formula epoch; автоматический producer этого epoch не подключён.

## Проверки

```sh
python3 apps/web_vitrina_history_compiler_smoke.py
python3 apps/web_vitrina_history_store_smoke.py
python3 apps/web_vitrina_history_live_smoke.py
python3 apps/web_vitrina_history_inventory_metadata_smoke.py
python3 apps/web_vitrina_history_components_smoke.py
python3 apps/ff_pool_fbs_lifecycle_quality_reuse_smoke.py
python3 apps/web_vitrina_history_activation_smoke.py
python3 apps/sheet_vitrina_v1_inventory_planning_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py
```

Первый smoke: actual native compiler → saved dated objects → 1/3/14/31/180, независимый exact rowset, все16 полей, static catalog и selected-date search; исторические inactive SKU без raw row и смена facilities; no-change без construction; native semantic correction с прежним числом; config/default-template вне range; retro update/delete/re-date, business TZ rollover и numeric-loss refusal. Второй — framework interruption/resume, related atomic correction, unchanged refs, superseded/base CAS, previous pin/TTL expiry, quota и deadline. Callback проверки второго smoke не доказывают native source coverage.

Live smoke на owned native fixture: no-change без construction; actual current-only update ровно двух дней с неизменным epoch/context/catalog и refs закрытых дней; semantic ready correction, delete/re-date union, parameter suffix, Jul-1 backward cost, first duplicate Finance item, concurrent source ABA refusal, pending progress across current rollover, native quality floor, temporal overlay identity и WAL family refusal без создания файлов. Actual owned lifecycle fixture проверяет 5 cold / 0 warm native scope calls при свежих inputs на каждом pin, полный coverage/digest, native floor, closed-pin refusal, source byte/time и whole-cache caps, owner policy switch и настоящее last-good content update. Whole-cache serialization/check выполняется при allocation/persistence, не на каждой дате. Compiler fixture с untyped capture сравнивает все16 полей обоих injected blocks с native baseline; оба блока получают один callable. CLI window/lock assertions изолируют parent guards. Все три новых backend smoke PASS; действующий default web contract также PASS. Приватные evidence, реальные helper inputs/results и final hashes находятся вне Git в `ЕДИНАЯ_ИСТОРИЯ_ЭТАП3_2026-10-04`.

Локальный импорт ранее принятого saved31 отдельно измерил только serialization/read: 31 day objects и shared catalog заняли 63,61 MiB; summary79rows 1/3/14/31 — 0,128–0,156 с; первая128SKU31 — 0,162 с. Exact16/static/search parity, исходный SHA/stat и derived file family unchanged. Это не production benchmark и не доказательство независимого compiler на реальных данных. Приватные доказательства находятся вне Git в аудите `ЕДИНАЯ_ИСТОРИЯ_ЭТАП2_2026-10-04`.

Formula manifest/runtime contract и существующий service epoch обновляются вместе по reviewed file content. Новый epoch штатно прогревает source proofs заново и заменяет несовместимый PENDING; перенос старых proofs/refs вручную не предусмотрен. Partial/error portion сохраняет только полные scope entries; pruning происходит после всех потребителей порции, перед fresh publication fence. No-change preflight и ABA revalidation остаются прежними, новый generic retry отсутствует.
# Автоматическое окно 14 дней

Штатный history parent обновляет только изменённые дни от business-today−13
до business-today включительно. День определяется `business_time`
(Asia/Yekaterinburg); расписание таймера в Asia/Tbilisi не меняется.
Рассчитывается полный native день, без обещания вычисления отдельных метрик.

Редакция storage_version=2 сохраняет исходные object ID, catalog ID и
epoch/token каждого архивного дня. Общий каталог служит для чтения объединённого
набора строк, а контекст объекта проверяется по его собственному каталогу.
Старые ячейки не получают новую формулу или свежий proof. `CURRENT` меняется
атомарно после завершения нужных дней и свежей проверки источника; незаконченный
`PENDING` сохраняет предыдущую опубликованную редакцию.

`backfill_required` перечисляет известные изменения входных proofs вне окна;
они не блокируют свежие дни и не снимаются обычным обновлением.
`archive_not_reevaluated` отдельно обозначает архив, не проверенный под новым
контекстом, без утверждения об изменении всех его значений. Оба поля доступны
в результате parent и `history_snapshot.archive_status` читателя.
Архивный перерасчёт запускается только с явной парой `--backfill-from YYYY-MM-DD
--backfill-to YYYY-MM-DD` внутри объявленного source range, под прежними
admission, singleflight, source/fence и resource guards.

Отсутствующая в исходном дневном каталоге новая строка показывается как
`null/—`, «Не отслеживалась», без нулей. Для будущей новой метрики можно задать
фиксированный `metric_start_dates` в проверенном runtime contract: mapping
metric_key → ISO date. Дата не выводится из текущего времени reader.
Существующая метрика без объявленной даты начала сохраняет native missing
semantics; сохранённые наблюдения внутри окна не отбрасываются.
Несовместимые идентичности одинакового row ID или статические колонки дают
явный отказ вместо молчаливого смешивания.

## Однократный переход на блоки групп

Группы нового каталога используют `sku_groups.group_key` как идентичность,
`label` как подпись и `display_order, group_key` как порядок. Состав всех дат
берётся из текущей номенклатуры, включая сохранённые скрытые карточки; это не
восстановление исторического членства. Изменение подписи не меняет group ID.
Старый каталог и его TOTAL/SKU читаются до переключения без изменения ячеек.

GROUP использует нативный TOTAL evaluator на составе группы и окончательные
дневные SKU-операнды. Выкуп сохраняет зрелость D−6 и веса заказов; себестоимость,
Proxy3/4 и товарный капитал сохраняют нативные знаменатели и складские входы.
Отсутствующие обязательные операнды не заменяются нулями. `fin_storage_fee_total`
не имеет принятого распределения по SKU: GROUP пустой с качеством `unallocated`.
Пустая группа присутствует в справочнике, но не создаёт фиктивные числовые строки.

Обычный штатный parent по-прежнему использует rolling14. Полная история разрешена
только явным `--full-history-group-migration` в подтверждённой паузе с `--manual`
и точным `--maintenance-window-id`. `group_migration_root` в runtime contract
отдельный от обслуживаемого `candidate_root`; все mount/reserve/hash/source/lock
проверки остаются. Лимиты прежние: manual parent 180 секунд, максимум31 дневной
compute, capture32MiB/20 секунд, proof cache128MiB, store2GiB. Пока новый GROUP
кандидат строится, старый CURRENT остаётся доступен. Старые proofs/refs вручную
не переносятся и не переименовываются под новый epoch.

Пример операционной последовательности (root после exact release/CI, не запуск
из документа):

1. Прочитать CURRENT обслуживаемого root, выполнить штатные maintenance preflight,
   pause один раз и дождаться `held`, quiet и свободной admission.
2. Взять epoch из проверенного
   `artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json`.
   Вызывать **parent** с `--runtime-dir /opt/wb-core-runtime/state`,
   `--candidate-root /mnt/wb-core-extra100/web-vitrina-history-groups-candidate`,
   `--runtime-contract artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json`,
   `--formula-epoch <проверенный epoch>`, `--date-from 2026-03-01`,
   `--date-to business-today`, `--manual --maintenance-window-id <точный ID>`,
   `--full-history-group-migration --budget-seconds 180 --max-recomputes 31`.
   Конечные порции продолжаются только после known-complete результата и новых
   полных proofs или валидных дневных refs; нулевой прогресс/неизвестный исход — stop.
3. После CURRENT кандидата сохранить точные `expected-current` обслуживаемого root
   и `expected-candidate`. Те же аргументы parent плюс `--group-candidate-preview
   --expected-current <old> --expected-candidate <new>` выполняют ограниченную
   read-only проверку всех дневных объектов/16 полей/контекстов/digests и полного
   покрытия старых дат. Получить `preview_token`; источники кандидата должны быть
   актуальны, иначе вернуть его к обычной ограниченной порции.
4. Отправить один parent с теми же идентичностями и
   `--group-candidate-publish-token <preview_token>` вместо preview-флага.
   Публикация проверяет fresh native vector/fence до и после копирования
   неизменяемых производных объектов и атомарно меняет CURRENT через CAS.
   При неоднозначном ответе читать результат этой операции, не повторять submit.
5. Проверить CURRENT и pinned старую edition, TOTAL/group/SKU и одинаковую edition
   во всех выбранных страницах, затем выполнить точный штатный resume. Сервис и
   календарь не меняются; дальнейший parent без migration-флага обновляет только
   изменившиеся дни rolling14. Кандидат не является поводом для скрытого полного
   пересчёта в следующих штатных циклах.

Источник данных сайта после переключения тот же абсолютный root: специального
редактирования env/ручного CURRENT не требуется. Старые настройки метрик не
перезаписываются при чтении. HTTP `scope=catalog` возвращает только метаданные;
`total`, `group` и `sku` независимы, последние два требуют edition. Все SKU и
групповые страницы остаются ограниченными размером ответа и количеством строк.
