# Квитанции и журнал Cash TEST

В existing Finance UI журнал появляется только при `store_mode=isolated_test`
и подтверждённом native `store_id`. Он открывается в самом Finance UI. Данные
не присоединяются к operational SQLite и не читаются через secret bridge.
Новые маршруты только GET: `/v1/finance/operator-operations` и exact
`/v1/finance/operator-operations/<operation_id>`.

Existing Finance authentication, hierarchy `finance`/`finance_operate`/
`finance_admin` и отдельное право видеть остаток Владислава сохраняются. Без
`finance` нет чтения, counts, поиска и detail. Неадминистратор видит только
свои native operations; admin scope совпадает с existing `get_operation`.
Фильтры actor и whitelist кассовых scope применяются до count/search/page/detail.
Directory operations и личные настройки сюда не входят.

Источник — immutable `finance_liquidity_operations`: exact operation ID,
receipt ID, actor, request digest и effect-root document. Чтение выполняется
через existing native `FinanceCashService._connect(write=False)` в read-only
transaction с `query_only`. Sealed ledger проверяется тем же native validator,
который используется обычным `get_operation`. Constructor/bootstrap не
вызываются. Неподтверждённый TEST binding отклоняется до открытия источника.

В receipt нет raw `result_json`, сумм, остатков, comments и приватных путей.
Для сверки Владислава без balance grant скрыт также matched/discrepancy result.
Черновик показывает «Черновик сохранён», без green accepted receipt. Source-only
сверка показывает сохранение, без изменения денег. Ledger completion требует
native immutable receipt и integrity proof. Action-required остаётся вопросом
к оператору и не считается проведением.

Existing native POST/atomic ledger/once-only operation recovery не изменены.
UI только добавляет GET той же exact identity после результата cash operation
или existing uncertain-response readback. Операции справочников не запускают
этот GET. Журнал загружается при открытии; projection failure не заменяет
native ответ и не вызывает повторный POST. Receipt использует общий компонент
`OperatorAcceptance`, не меняя native writeflow и не открывая live Finance.

Проверка: `apps/operator_cash_operations_smoke.py` выполняет actual native TEST
bootstrap/ledger лишь в temporary fixture, then readonly API/Chromium requests.
Проверяются grants до totals/search/detail, foreign actor, TEST binding до read,
failed native ledger proof, draft, protected reconciliation и неизменные DB bytes.
Дополнительно native cash/auth/HTTP/browser smokes. Нужны existing APSW и
Playwright Chromium; CI mapping/release принадлежат root WBC0137.
