# Parity Matrix Блока Web-Source Snapshot

## Цель Parity-Проверки

Parity-проверка нужна, чтобы доказать: новый `web-source snapshot block` воспроизводит legacy-контракт по смыслу до начала реализации следующего слоя.

Она должна доказать:
- сохранён downstream-значимый snapshot contract;
- не потеряны identity, period semantics и failure-mode;
- возможный cutover этого блока не сломает consumers по форме и смыслу данных.

## Что Сравниваем

Сравниваем:
- legacy-сторону как reference snapshot contract;
- новый блок как target snapshot contract в `wb-core`.

Уровень сравнения:
- contract level;
- payload level;
- semantic level;
- failure-mode level.

## Обязательные Точки Сравнения

| Точка | Что должно совпадать |
| --- | --- |
| Snapshot period | Логика `snapshot_date` или `date_from/date_to`, включая смысл среза |
| `nm_id` | `nm_id` остаётся главным item identity без потери смысла |
| Состав `items` | Список items отражает тот же набор сущностей в том же semantic scope |
| Числовые поля | `views_current`, `ctr_current`, `orders_current`, `position_avg` и аналогичные поля сохраняют свой смысл |
| Payload shape | Обязательные поля snapshot-а и items присутствуют и стабильны для downstream |
| `not found` | Отсутствие snapshot-а остаётся отдельным non-fatal режимом, а не смешивается с hard failure |
| Downstream stability | Контракт остаётся пригодным для downstream-consumer без скрытой semantic drift |

## Что Считается Успехом

Успех:
- совпадает смысл snapshot period;
- совпадает роль `nm_id`;
- совпадает обязательный состав item-level данных;
- числовые поля не теряют semantics;
- downstream-обязательная payload shape сохранена;
- `not found` и hard failure различаются так же, как в legacy contract.

Технические отличия допустимы только если:
- не меняют contract semantics;
- не ломают downstream interpretation;
- явно задокументированы как harmless transform.

Неприемлемое расхождение:
- потеря или переопределение `nm_id`;
- смена смысла snapshot period;
- исчезновение обязательных item fields;
- неявная смена смысла числовых полей;
- смешение `not found` с общей ошибкой;
- unstable payload shape для downstream.

## Какие Evidence Нужны

Нужны:
- legacy sample или recorded fixture;
- target contract sample;
- поле-в-поле parity table;
- отдельная проверка failure-mode;
- список downstream assumptions;
- явная фиксация допустимых и недопустимых расхождений.

До следующего этапа должно быть показано:
- какой legacy sample взят как reference;
- какие поля обязательны для parity;
- какие поля можно считать optional вне первого scope;
- что downstream contract не теряет критичный смысл.

## Что Не Входит В Parity Этого Блока

Не входит:
- parity browser automation;
- parity acquisition runtime;
- parity API/job implementation;
- parity table apply-layer;
- parity downstream business logic;
- performance и production hardening.

## Следующий Шаг После Parity Matrix

Следующий шаг:
- создать в `wb-core` короткий evidence checklist для `web-source snapshot block`, который перечисляет какие fixtures, sample payloads и failure-case примеры должны быть собраны до начала минимальной реализации.
