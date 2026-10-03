# WBC0069K16: локальный backend единой истории

Кандидат этапа 2 не подключён к HTTP, UI, production reader, builder или таймерам. Действующий готовый режим 14/31 и процедуры источников сохраняются. Следующий шаг после Draft PR — отдельное решение о production producer contract и проверка реальной исторической совместимости.

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

Это работающий **LOCAL frozen adapter**, не production invalidation hook. Пока не приняты: дешёвый exact impact по Finance/book/effective parameter old/new intervals, intraday clock/quality dependencies, code/policy epoch producer, live multi-source consistency и дешёвый catalog identity feed. Большой source proof либо неизвестная coverage завершаются failclosed. Generic token/callback не является доказательством production coverage. Jul-1 cost retropropagation и D-6 требуют целевых native producer regression при подключении; здесь unknown impact консервативно расширяется на все даты, а не объявляется готовым журналом изменений.

## Проверки

```sh
python3 apps/web_vitrina_history_compiler_smoke.py
python3 apps/web_vitrina_history_store_smoke.py
python3 apps/sheet_vitrina_v1_inventory_planning_smoke.py
python3 apps/sheet_vitrina_v1_web_vitrina_contract_smoke.py
```

Первый smoke: actual native compiler → saved dated objects → 1/3/14/31/180, независимый exact rowset, все16 полей, static catalog и selected-date search; исторические inactive SKU без raw row и смена facilities; no-change без construction; native semantic correction с прежним числом; config/default-template вне range; retro update/delete/re-date, business TZ rollover и numeric-loss refusal. Второй — framework interruption/resume, related atomic correction, unchanged refs, superseded/base CAS, previous pin/TTL expiry, quota и deadline. Callback проверки второго smoke не доказывают native source coverage.

Локальный импорт ранее принятого saved31 отдельно измерил только serialization/read: 31 day objects и shared catalog заняли 63,61 MiB; summary79rows 1/3/14/31 — 0,128–0,156 с; первая128SKU31 — 0,162 с. Exact16/static/search parity, исходный SHA/stat и derived file family unchanged. Это не production benchmark и не доказательство независимого compiler на реальных данных. Приватные доказательства находятся вне Git в аудите `ЕДИНАЯ_ИСТОРИЯ_ЭТАП2_2026-10-04`.
