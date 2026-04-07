# Initial Evidence

- Legacy samples собраны для `normal-case` и `empty-case`.
- Target samples собраны для `normal-case` и `empty-case`.
- Legacy-source зафиксирован как official source chain + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Local live-source preflight на этом Mac пока не подтверждён и не считается authoritative.
- Authoritative server-side smoke должен быть выполнен отдельно без deploy и без изменений в `/opt/...`.

Вывод: checkpoint готов к code-skeleton, transformation и live-source smoke для подтверждения реально рабочего состояния.
