# Balance: сохранённый запрос и фактическая проверка WB

Нативный владелец — `SkuInventoryBalanceBlock`. Новый операторский запрос
сохраняет `balance-apply:<id>`, исходные calculation/selection/revision и
серверные actor/seller/account scope прямо в существующем `apply_jobs`, вместе
с неизменным manifest и строками целей одной транзакцией. Отдельной очереди нет.
`inventory_balance_jobs` в общем журнале — только проекция этих записей.

«Принято» означает, что задание и точные цели сохранены. `job.state=completed`,
item `succeeded` или `readback_status=matching` сами по себе не подтверждают WB.
Для завершения нужны все exact Change Registry children: собственные
actor/account/source/job/calculation/target/before/after/decision identities,
последняя confirmed (либо resolved/confirmed) попытка, `wb_readback`, digest и
точная native receipt reference. Частичный и неопределённый результат видны
отдельно. GET не запускает worker, resume или новую внешнюю отправку.

Форма сохраняет исходный body и ID под Web Lock точного actor scope/calculation.
Если ответ потерян или неизвестен, повтор/reload читают только
`GET .../inventory-balance/apply-jobs?request_id=<id>`. Обычный нативный
`GET .../apply-jobs/<job_id>` тоже читает без bootstrap и записи. Grant,
native actor, human actor, seller и account scope проверяются до detail,
поиска, counts и pagination; чужая запись не выдаётся через resume.

Перед **новым** ID форма читает existing calculation GET и сверяет выбранные
показанные bid/state operands, native decision IDs, override generations и
owner confirmation policy. Изменившаяся выбранная цель требует снова проверить
форму: POST не отправляется. Неизменные выбранные цели получают fresh native
revision для короткого CAS; количество/цена/selection не подменяются. Это
необходимо, поскольку существующая форма merge-ит save response только для
одной цели, сохраняя соседние edits. При retained ID этот prepare не выполняется.
Внутри native `BEGIN IMMEDIATE` ревизия проверяется ещё раз перед insert.
Каждое native override save меняет generation, включая ABA.

Форма снимает fence только по точной квитанции своего ID/body/job/manifest или
доказанному precommit rejection code. Неизвестные 4xx/5xx, повреждённый уже
сохранённый proof и отказ GET сохраняют ID. Отсутствие Web Locks/storage —
fail-before-submit. Exact maintenance HTTP423/code `business_data_maintenance`
остаётся доказанным отказом до mutation. Нет auto-resubmit на неизвестный WB
эффект; существующий явный native resume не заменён новым исполнителем.

Typed request/target proof append-only; старую legacy строку нельзя повысить
до typed authority через UPDATE, удалить её ID или переписать цели. Новое
задание ограничено 1000 целями, body 512 KiB и immutable proof 2 MiB;
превышение блокирует admission до worker/provider. Сохранённые ключи не
удаляются. Native outcome updates не ограничены новой terminal capacity.
Старые jobs без typed proof продолжают native flow, но не получают фиктивную
операторскую acceptance. Неподдержанный/unconfigured native account не даёт
green receipt. Native TEST fixtures выполняют реальный source TX/worker и
Change Registry на fake transport; реальные WB mutations запрещены.

Проверки: `apps/operator_balance_job_recovery_smoke.py` и существующие
`sku_inventory_balance_smoke.py`, `sku_inventory_balance_live_apply_smoke.py`,
`sku_inventory_balance_browser_smoke.py`. Зависимости прежние: APSW,
openpyxl, Playwright Chromium. Новых provider/API/runtime возможностей нет.

Bounded Balance composition uses the reviewed C1 wire scope unchanged:
`get_settings(user_key)` returns `operator_scope=user_key`; the form copies it
from the native settings projection. No settings receipt/save implementation or
future settings module is imported by this prerequisite. The compatibility
browser fixture uses a secure HTTPS origin so real Chromium Web Locks apply.
