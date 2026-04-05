# Initial Evidence

- Legacy samples собраны для `normal-case` и `empty-case`.
- Target samples собраны для `normal-case` и `empty-case`.
- Legacy-source зафиксирован как official source path + current RAW/APPLY semantics.
- Bootstrap `nmId` честно ограничен sample set из уже известных проекту SKU.
- Parity по смыслу подтверждена для `normal-case`.
- Parity по смыслу подтверждена для `empty-case`.
- Artifact-backed transformation может быть проверена локальным smoke-check.
- Local live-source preflight для statistics API на этом Mac не считается authoritative и повторно упирается в TLS handshake timeout.
- Upstream statistics sales endpoint ограничен `1 request / minute`, поэтому authoritative server-side live smoke для checkpoint фиксируется только по `normal-case`; empty-case доказывается artifact-backed sample и transformation.
- После local transport-failure выполнен authoritative server-side smoke на `root@178.72.152.177` во временной директории `/tmp/wb-core-spp-smoke-v2` без deploy и без изменений в `/opt/...`.
- Во временную server-side среду копировались только `apps/spp_block_http_smoke.py`, `packages/adapters/spp_block.py`, `packages/adapters/official_api_runtime.py`, `packages/application/spp_block.py`, `packages/contracts/spp_block.py`.
- Server-side live smoke запущен через `python3` с `PYTHONPATH=/tmp/wb-core-spp-smoke-v2`, при этом `WB_TOKEN` передавался только в runtime запуска и не сохранялся в repo-файлах.
- Server-side smoke дал authoritative результат: `normal: ok -> success`, `normal: count -> 2`, `http-smoke-check passed`.

Вывод: `spp_block` подтверждён как реально рабочий live-source checkpoint на bootstrap sample set; empty-case остаётся честным block-level empty после фильтрации по `nmId`, а live smoke ограничен одним normal-case из-за upstream rate limit.
