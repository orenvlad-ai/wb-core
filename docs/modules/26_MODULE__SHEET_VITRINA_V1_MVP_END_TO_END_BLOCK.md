# Module 26 — Web Vitrina end to end

## Назначение

Модуль связывает server-owned источники и подготовленные снимки с основной Web
Vitrina. Архивный обратный путь записи в Google Sheets не является рабочим
контуром.

## Поток

```text
источники → нормализация → registry/runtime storage → ready snapshot
→ read API → Web Vitrina и пользовательские выгрузки
```

## Инварианты

- интерфейс читает один опубликованный ready snapshot;
- кандидат строится до переключения указателя на него;
- неготовый кандидат не заменяет last-good;
- дата и качество показателя сохраняются до ячейки;
- `missing`, `partial`, `stale`, `unconfirmed` и точный ноль различаются;
- ручное обновление не создаёт отдельную версию бизнес-логики;
- старый Google Sheets write bridge не используется как fallback.

## Кодовые границы

- `packages/application/sheet_vitrina_v1_*` — сборка и публикация;
- `packages/contracts/sheet_vitrina_v1_*` — форматы;
- `apps/registry_upload_http_entrypoint*` — server entrypoint и read API;
- `packages/adapters/templates/sheet_vitrina_v1_operator.html` — основной UI.

Предметные строки и формулы описаны в документах конкретных модулей. Этот
документ не дублирует их.
