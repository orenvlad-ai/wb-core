"""Deterministic smoke coverage for the canonical local C1 workspace."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.codex_curator_workspace import (  # noqa: E402
    load_contract,
    validate_checkout,
    validate_project_readback,
)


def main() -> None:
    contract = load_contract()
    validation = validate_checkout()
    assert validation["status"] == "ok"
    assert contract["project"]["label"] == "WB Core · Кураторы"
    assert contract["curator"] == {
        "role": "C1",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "task_surface": "discussion-only",
    }
    assert contract["instruction_discovery"]["project_doc_max_bytes"] == 65536
    assert contract["instruction_discovery"]["current_role_source"].startswith(
        "origin/main:workspaces/"
    )
    instruction_bytes = sum(
        (ROOT / relative).stat().st_size
        for relative in contract["instruction_discovery"]["expected_order"]
    )
    assert instruction_bytes < contract["instruction_discovery"]["project_doc_max_bytes"]

    lifecycle = contract["lifecycle"]
    assert lifecycle == {
        "common_contract_source": "origin/main:AGENTS.md",
        "common_contract_sections": [
            "Discussion → отдельная Codex-задача",
            "Глобальный Watcher и арбитр",
        ],
        "inherit_without_override": True,
        "role_entry_state": "discussion-only",
        "role_specific_obligations": [
            "before-action-current-origin-main-readback",
            "c2-outside-curator-primary-folder",
        ],
    }

    root_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    for common_lifecycle_rule in (
        "`DISPATCH_REQUEST`",
        "выдать ровно один короткий dispatch summary и завершить текущий turn",
        "steady state — завершённый turn/idle без model turn",
        "не запускает циклы `wait_threads`/`read_thread`",
        "Нормальные wake sources — новое сообщение пользователя или exact attention",
        "Задача принята",
    ):
        assert common_lifecycle_rule in root_text

    watcher = contract["watcher"]
    assert watcher == {
        "common_contract_source": "origin/main:AGENTS.md",
        "inherit_without_override": True,
        "workspace_adds_watcher_or_heartbeat": False,
    }
    assert contract["executor"]["inherits_curator_delta"] is False
    assert contract["canary"]["requires_service_boilerplate"] is False

    migration = contract["migration"]
    assert migration["legacy_project"] == "wb_core_3"
    assert migration["automatic_archive"] is False
    for required in (
        "no-active-pre-migration-tasks",
        "initiating-curator-owner-accepted",
        "registry-integrity-ok",
        "exactly-one-active-watcher",
        "new-front-door-proven",
    ):
        assert required in migration["legacy_archive_requires"]

    readback = {
        "schemaVersion": 2,
        "projects": [
            {
                "projectId": "local-curators",
                "projectKind": "local",
                "label": contract["project"]["label"],
                "path": str(
                    (ROOT / contract["project"]["primary_relative_path"]).resolve()
                ),
                "hostId": "local",
                "isGitRepository": True,
            }
        ],
    }
    project = validate_project_readback(readback, contract, ROOT)
    assert project["status"] == "ok"
    assert project["project_id"] == "local-curators"

    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "projects.json"
        path.write_text(json.dumps(readback, ensure_ascii=False), encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert validate_project_readback(loaded, contract, ROOT)["status"] == "ok"

    bad_path = json.loads(json.dumps(readback))
    bad_path["projects"][0]["path"] = str(ROOT)
    try:
        validate_project_readback(bad_path, contract, ROOT)
    except ValueError as error:
        assert "normalized" in str(error)
    else:
        raise AssertionError("normalized Git-root cwd must fail closed")

    missing_project_id = json.loads(json.dumps(readback))
    missing_project_id["projects"][0]["projectId"] = ""
    try:
        validate_project_readback(missing_project_id, contract, ROOT)
    except ValueError as error:
        assert "project ID" in str(error)
    else:
        raise AssertionError("missing Desktop project ID must fail closed")

    sources = "\n".join(
        (ROOT / relative).read_text(encoding="utf-8")
        for relative in (
            "workspaces/WB Core · Кураторы/AGENTS.override.md",
            "workspaces/WB Core · Кураторы/README.md",
            "docs/architecture/13_codex_curator_workspace.md",
        )
    )
    for required in (
        "discussion-only",
        "inherit_without_override",
        "before-action",
        "origin/main:workspaces/",
        "wb_core_3",
    ):
        assert required in sources
    role_text = (
        ROOT / "workspaces/WB Core · Кураторы/AGENTS.override.md"
    ).read_text(encoding="utf-8")
    for common_rule in (
        "`DISPATCH_REQUEST`",
        "`wait_threads`",
        "Нормальные wake sources",
        "Задача принята",
    ):
        assert common_rule not in role_text
    for forbidden in (
        "watcher-g1",
        "watcher-g2",
        "watcher-g3",
        "watcher-g4",
        "watcher-g5",
        "watcher-g6",
    ):
        assert forbidden not in sources.casefold()

    print("codex_curator_workspace_smoke: ok")


if __name__ == "__main__":
    main()
