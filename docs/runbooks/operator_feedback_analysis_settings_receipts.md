# Инструкция и модель разбора отзывов

Эта квитанция подтверждает сохранение инструкции и модели legacy-разбора
отзывов. Она не подтверждает запуск анализа, расход OpenAI, отправку жалобы
или ответа WB. Automatic reply prompt bundle остаётся отдельным источником.

Нативный владелец — `JsonFileFeedbacksAiPromptStore`, источник —
`runtime/sheet_vitrina_v1_feedbacks_ai_prompt.json`. Браузер использует прежний
`GET/POST /v1/sheet-vitrina-v1/feedbacks/ai-prompt`. Новые команды содержат
`operation_id=analysis-settings:<uuid>` и `expected_source_revision` из GET.
Actor, seller/account и account scope определяет сервер, не тело запроса.

Внутри существующего файла атомарно сохраняются текущий источник, новая
generation и append-only `operator_source_receipts`. Квитанция связывает
исходный запрос с оригинальными before/after prompt, model, датой и версиями.
Все штатные записи этого владельца используют один thread/process lock;
legacy save сохраняет предыдущие квитанции и меняет generation даже при ABA.
Каталог моделей — метаданные отображения, он не меняет source revision.
GET текущего источника связывает показанные значения и revision из одного
снимка до model discovery. Exact receipt GET не вызывает discovery вообще.

Точный повтор ID возвращает исходную квитанцию до CAS/discovery, не меняя
текущую инструкцию. Другой запрос или actor с тем же ID отвергается. Старые
источники без квитанции не становятся операторскими документами задним числом.
Общий журнал читает фиксированный файл без создания владельца/lock/schema;
native path grant, account и actor ограничивают count, поиск, страницу и detail.

Перед единственным POST браузер удерживает Web Lock точного fence key и
сохраняет ID с запросом. Потерянный/неизвестный ответ означает только exact
`GET /v1/sheet-vitrina-v1/operations/<id>`, включая повтор и перезагрузку.
Generic 4xx/5xx и ошибка чтения после commit не разрешают новый POST. Fence
можно очистить по доказанной совпадающей квитанции либо перечисленному native
precommit refusal; очистка проверяет собственный ID. Без Web Lock POST запрещён.

Файл ограничен 32 MiB, retained ledger — 16 MiB/512 документов. Вместимость
проверяется до replace, ключи не вытесняются. Это настройка без внешнего effect:
после commit не существует отдельного результата provider, которому нужен
терминальный резерв. Запись использует уникальный exclusive temp file, fsync
файла, атомарный replace и fsync каталога; исходные/lock/temp symlinks запрещены.
Ошибка после replace сохраняет неопределённость для UI и восстанавливается
чтением исходного ID. Записи остаются в том же native source, новой очереди нет.

Synthetic проверки: `apps/operator_json_business_settings_smoke.py`
(APSW через native HTTP fixture, Playwright Chromium, stdlib multiprocessing/
flock). Реальные API/provider, production и SSH не используются.
