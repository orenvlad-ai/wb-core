# Codex Execution Protocol

## Модель Выполнения

Codex может выполнять длинные, но bounded execution chains внутри `wb-core`, если задача конкретна и следующий безопасный шаг выводим из контекста репозитория.

Bounded означает:
- явный scope;
- явные stop conditions;
- без silent repo-wide redesign;
- без скрытых side effects вне текущей задачи.

## Stop Conditions

Codex должна остановиться и вынести задачу на review, если:
- задача требует менять migration scope;
- architecture docs конфликтуют с наблюдаемыми фактами репозитория;
- reference evidence недостаточен для заявленного контракта;
- запрошенное изменение вводит business-логику на foundation-этапе;
- требуется runtime-only behavior, который нельзя воспроизвести из repo artifacts;
- задача потребовала бы менять reference-репозитории.

## Verification Matrix

Для каждой существенной задачи Codex должна проверять, где применимо:

| Проверка | Минимальное ожидание |
| --- | --- |
| Scope | Изменены только запрошенные файлы и прямо необходимые support files |
| Contract | Заявленные контракты совпадают с repository evidence или помечены как inference |
| Boundaries | В foundation work не добавлена business-логика |
| Diff hygiene | Нет случайных правок в reference-репозиториях |
| Documentation sync | При значимом техническом изменении обновлена каноническая документация в той же задаче |
| Local validation | Базовые структурные проверки проходят |
| Reviewability | Результат понятен ручному reviewer без runtime-археологии |

## Documentation Sync Rule

Если в рамках задачи Codex меняет:
- код;
- структуру каталогов и файлов;
- smoke-path или критерии проверки;
- артефакты;
- runtime helpers;
- другой значимый технический результат,

она должна в той же задаче обновить и соответствующую документацию, если такая документация существует или по смыслу уже должна существовать как часть source of truth проекта.

Задача не считается полностью завершённой, если:
- технический результат уже изменён;
- документация по смыслу должна была быть обновлена;
- но фактически не обновлена.

Для `wb-core` частное обязательное правило такое:
- канонический source of truth для модульной документации живёт в `docs/modules/`;
- если модуль новый, Codex обязана:
  - создать новый файл модуля в `docs/modules/`;
  - обновить `docs/modules/00_INDEX__MODULES.md`;
- если модуль изменён, Codex обязана:
  - обновить соответствующий файл модуля в `docs/modules/`;
  - и при необходимости обновить `docs/modules/00_INDEX__MODULES.md`.

## Когда Codex Может Действовать Самостоятельно

Codex может действовать самостоятельно, если:
- задача находится внутри `wb-core`;
- изменение укладывается в уже зафиксированные migration principles;
- разумные assumptions низкорисковые и явно помечены;
- repository policy не требует внешнего approval.

## Когда Нужен Reasoning-Review

Reasoning-review нужен, если:
- контракт неоднозначен;
- остаются две валидные архитектурные опции;
- изменение может зафиксировать долгосрочную границу;
- предлагаемый шаг влияет на cutover sequencing или ownership source of truth.

## Что Codex Нельзя Делать Без Явного Разрешения

Codex не должна без явного разрешения:
- менять reference-репозитории;
- добавлять business или production runtime code во время foundation-only work;
- создавать CI/CD workflows;
- создавать миграции БД;
- создавать deploy scripts;
- создавать или заменять operator table;
- делать commit, push или открывать PR;
- считать runtime snapshots authoritative без зафиксированного reconcile evidence.
