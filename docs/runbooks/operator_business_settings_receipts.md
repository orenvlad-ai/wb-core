# Native квитанции бизнес-настроек SKU

`save_sheet_vitrina_user_config` сохраняет текущую native конфигурацию и,
для explicit `operator_command`, immutable business version в одной native
read-CAS-write transaction. Current row и history proof откатываются вместе.
Перед чтением revision writer берёт write reservation (`BEGIN IMMEDIATE`,
если native connection ещё не находится в своей transaction).

Поддержаны только source-owned `sku_management.forecast` и
`sku_inventory_balance.calculation`. Личные `table` preferences не входят в
version JSON и не создают operator document. Source identity имеет namespace
`business-settings:<id>`; trusted HTTP передаёт actual actor, native user key,
canonical seller и expected revision. Request digest включает всю нормализованную
native mutation/schema/scope, поэтому тот же ID с другим payload/actor/account
или schema конфликтует. Duplicate проверяется перед CAS и не сохраняет повторно.
Отдельные прежние версии, включая ABA, неизменны. SQL triggers запрещают update
и delete версий. Ordinary legacy saves без ID совместимы и не получают fictitious
immutable receipt. Explicit invalid ID отклоняется перед source write.

`business_settings` в общем журнале читает только native таблицу через existing
operational RO/query_only surface; GET не создаёт таблиц и не вызывает owner
repository. Native endpoint grants определяют разрешённые config families.
Actor/user/seller/config filters применяются до count/search/page/detail.
Personal prefs/auth не становятся searchable journal data. `source_saved` и
`calculation_completed=false` означают «Сохранено»: запуск/расчёт этим proof не
подтверждаются. Новый executor/queue отсутствует.

Actual settings forms сохраняют browser fence с exact source ID до одного POST.
Unknown response/reload допускают только GET той же source version через общий
журнал. Receipt обязан совпасть с ID/config/revision и переданными business
operands. Повторной отправки нет. Definite precommit 4xx снимает fence; сохранённый
source proof позволяет следующий новый запрос. Preferences autosave не проходит
сквозь pending business command и сохраняет прежние saved business fields.
Web Lock по exact fence key удерживает get/create/POST/read/clear между вкладками.
Без Web Locks отправка отклоняется до POST. Очистка сверяет собственный identity,
поэтому другая retained ссылка не удаляется. Two-tab native HTTP test удерживает
ответ после actual source commit: вторая вкладка ожидает lock, затем читает тот же
ID после lost response первой, без второго POST и CAS-конфликта.

Forecast general UI остаётся `hidden inert` согласно reviewed SKU07: новая
квитанция не возвращает retired экран. Его compatibility API/function получают
такой же native version. Balance settings остаются в текущей форме. Existing
Balance settings endpoint POST-only; recovery не требует вымышленного GET туда.

Проверки: `operator_business_settings_smoke.py` — actual native CAS, concurrent
winner, rollback, immutable ABA history, negative scopes до count/search/detail,
DB bytes unchanged при чтении, actual native HTTP и actual form functions с
потерянными POST/GET responses и reload, две вкладки и отсутствие Web Locks.
`sku_inventory_balance_browser_smoke.py`
использует real native CAS/proof fixture для settings и fake unrelated external
operations; это не fabricated accepted JSON. Native SKU/Balance core и common
receipt/journal/feedback regressions сохраняются. Legacy
`sku_management_browser_smoke.py` уже на frozen38714c6a падает на initial hidden
general rows; продукт/этот тест данным patch не раскрываются и не меняются.
