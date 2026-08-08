# Archived Codex Curator Workspace v1

Canonical curator workspace, registry-bound dispatch, pin/readback lifecycle и
Desktop callback больше не являются частью correctness path `wb-core`.

Исторический contract доступен в Git history на anchor
`e44f548982900e286a2c1a73fdf439d0c8a49843`. Current UX — куратор запускает
отдельного исполнителя, получает короткий GitHub/deploy отчёт и вручную принимает
результат. Актуальный naming/pinning/owner-acceptance contract находится только
в [Codex Execution Protocol](07_codex_execution_protocol.md#видимый-жизненный-цикл-codex-задач).
Пользовательские чаты не удаляются и не архивируются автоматически.
