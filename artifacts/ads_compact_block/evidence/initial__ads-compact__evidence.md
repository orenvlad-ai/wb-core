# Initial Evidence

- Legacy samples собраны для `normal-case` и `empty-case`.
- Target samples собраны для `normal-case` и `empty-case`.
- Legacy-source зафиксирован как official source chain + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Local live-source preflight на этом Mac пока не подтверждён и не считается authoritative.
- После paused-state повторно выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-ads-compact-smoke-20260411` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/ads_compact_block_http_smoke.py`, `packages/adapters/ads_compact_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/ads_compact_block.py`, `packages/contracts/ads_compact_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-ads-compact-smoke-20260411`, при этом актуальный `WB_TOKEN` брался только из `/opt/wb-ai/.env` в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `normal: count -> 2`, `http-smoke-check passed`.

Вывод: `ads_compact_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set; paused-состояние разморожено без дополнительных правок кода.
