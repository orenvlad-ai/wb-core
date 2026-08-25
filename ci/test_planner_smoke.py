#!/usr/bin/env python3
"""Deterministic smoke coverage for registry union and impact selection."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci import test_planner as planner


BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40


def registry() -> dict:
    return {
        "schema": planner.REGISTRY_SCHEMA,
        "protocol": {
            "version": 2,
            "cutover_epoch": "4f0333ad7b500967fe4175aa6e53359043832360",
            "full_regression_suites": ["core", "finance", "warehouse"],
        },
        "suites": {
            "core": {"group": "core", "depends_on": [], "commands": [["python3", "core.py"]]},
            "warehouse": {"group": "domain", "depends_on": ["core"], "commands": [["python3", "warehouse.py"]]},
            "finance": {"group": "domain", "depends_on": ["warehouse"], "commands": [["python3", "finance.py"]]},
        },
        "rules": [
            {"id": "core", "paths": ["ci/**"], "suites": ["core"], "release": "repo_only", "force_full": True},
            {"id": "finance", "paths": ["apps/finance_*"], "suites": ["finance"], "release": "live_runtime"},
        ],
    }


def build(base: dict | None, head: dict, changes: list[dict[str, str]]) -> dict:
    return planner.build_plan(
        pr_number=1041,
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        base_registry=base,
        head_registry=head,
        changes=changes,
    )


def main() -> None:
    base = registry()
    head = copy.deepcopy(base)
    head["suites"]["core"]["commands"] = [["python3", "new_core.py"]]
    union, reasons = planner.union_registries(base, head)
    assert reasons == []
    assert union["suites"]["core"]["commands"] == [
        ["python3", "core.py"],
        ["python3", "new_core.py"],
    ]

    finance = build(base, head, [{"status": "M", "path": "apps/finance_storage.py"}])
    planner.verify_plan(finance)
    assert finance["selected_suites"] == ["core", "finance", "warehouse"]
    assert finance["release_plan"]["kind"] == "live_runtime"
    assert "transitive-domain-dependency" in finance["reason_codes"]

    unknown = build(base, head, [{"status": "A", "path": "new-domain/file.txt"}])
    planner.verify_plan(unknown)
    assert unknown["selected_suites"] == ["core", "finance", "warehouse"]
    assert unknown["unknown_paths"] == ["new-domain/file.txt"]
    assert unknown["release_plan"]["kind"] == "live_runtime"
    assert "unknown-path-full-regression" in unknown["reason_codes"]

    core = build(base, head, [{"status": "M", "path": "ci/test_registry.json"}])
    assert core["selected_suites"] == ["core", "finance", "warehouse"]
    encoded = planner.canonical_json_bytes(core)
    reordered = json.loads(json.dumps(core, sort_keys=False))
    assert encoded == planner.canonical_json_bytes(reordered)
    planner.verify_plan(reordered)

    missing_base = build(None, head, [{"status": "M", "path": "apps/finance_storage.py"}])
    assert "base-registry-missing" in missing_base["reason_codes"]

    real_registry = json.loads((ROOT / planner.REGISTRY_PATH).read_text(encoding="utf-8"))
    planner.validate_registry(real_registry, "repository registry")
    assert planner._record_paths(
        [{"status": "R100", "old_path": "русский путь/до.md", "path": "русский путь/после.md"}]
    ) == ["русский путь/до.md", "русский путь/после.md"]
    print("test_planner_smoke: ok")


if __name__ == "__main__":
    main()
