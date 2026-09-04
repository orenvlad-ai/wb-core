#!/usr/bin/env python3
"""Offline checks for the compact Release Runner."""

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps import github_release_runner as runner
from ci.select_checks import canonical_bytes


def plan() -> dict:
    value = {
        "schema": "wb-core.check-plan/v1",
        "pull_request": 7,
        "base_sha": "1" * 40,
        "head_sha": "2" * 40,
        "changed_paths": ["docs/example.md"],
        "groups": [],
        "commands": [],
        "pip": [],
        "release_kind": "repo_only",
        "check_map_sha256": "3" * 64,
    }
    value["plan_sha256"] = runner.sha256(canonical_bytes(value))
    return value


def main() -> None:
    value = plan()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("check-plan.json", json.dumps(value))
    assert runner._extract_plan(stream.getvalue()) == value
    operation = runner.operation_id(10, 7, "1" * 40, "2" * 40, value["plan_sha256"])
    assert operation.startswith("release-v3-")
    data = runner.receipt(
        state="done", run_id=10, pr=7, base="1" * 40, head="2" * 40, plan=value, merge="4" * 40
    )
    assert data["deployed_sha"] is None
    assert data["release_kind"] == "repo_only"
    try:
        runner.exact_sha("short", "test")
    except runner.RunnerError:
        pass
    else:
        raise AssertionError("short SHA accepted")
    print("github_release_runner_smoke: ok")


if __name__ == "__main__":
    main()
