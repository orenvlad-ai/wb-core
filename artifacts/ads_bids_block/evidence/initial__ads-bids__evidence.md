# Initial Evidence

- Legacy samples собраны для `normal-case` и `empty-case`.
- Target samples собраны для `normal-case` и `empty-case`.
- Legacy-source зафиксирован как official source chain + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Local live-source preflight для promotion API на этом Mac не считается authoritative.
- После implementation выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-ads-bids-smoke` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/ads_bids_block_http_smoke.py`, `packages/adapters/ads_bids_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/ads_bids_block.py`, `packages/contracts/ads_bids_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-ads-bids-smoke`, при этом `WB_TOKEN` передавался только в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `normal: count -> 2`, `http-smoke-check passed`.

Вывод: `ads_bids_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set; empty-case остаётся честным block-level empty после фильтрации active bid rows по `nmId`.
