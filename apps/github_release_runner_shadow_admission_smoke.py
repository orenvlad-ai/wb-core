#!/usr/bin/env python3
"""Deterministic smoke matrix for the shadow repo-only admission component."""

from __future__ import annotations

import ast
import copy
import json
import sys
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import github_release_runner_shadow_admission as admission


HEAD_SHA = "b" * 40
BASE_SHA = "a" * 40


def golden_snapshot() -> dict[str, Any]:
    return {
        "schema": admission.SNAPSHOT_SCHEMA,
        "repository": "orenvlad-ai/wb-core",
        "pr_number": 1042,
        "state": "open",
        "draft": False,
        "base_repository": "orenvlad-ai/wb-core",
        "base_ref": "main",
        "base_sha": BASE_SHA,
        "head_repository": "orenvlad-ai/wb-core",
        "head_ref": "codex/shadow-release-runner-admission",
        "head_sha": HEAD_SHA,
        "expected_head_sha": HEAD_SHA,
        "labels": ["task:standard", "scope:repo-only", "release:ready"],
        "required_check": {
            "id": 7654321,
            "name": "baseline",
            "app_slug": "github-actions",
            "head_sha": HEAD_SHA,
            "status": "completed",
            "conclusion": "success",
        },
        "mergeable": True,
    }


def changed(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    snapshot = copy.deepcopy(golden_snapshot())
    mutator(snapshot)
    return snapshot


def assert_reasons(
    name: str, snapshot: dict[str, Any], expected: list[str]
) -> None:
    receipt = admission.admission_receipt(snapshot)
    assert receipt["reason_codes"] == expected, (
        name,
        receipt["reason_codes"],
        expected,
    )
    assert receipt["decision"] == ("eligible" if not expected else "blocked")


def test_golden_eligible() -> None:
    receipt = admission.admission_receipt(golden_snapshot())
    assert receipt == {
        "schema": admission.RECEIPT_SCHEMA,
        "decision": "eligible",
        "reason_codes": [],
        "snapshot_sha256": "1c6db9dc3ff293638be1de2f01cd6976d240597c23ae0de8b21a3d6605165a5b",
        "bindings": {
            "repository": "orenvlad-ai/wb-core",
            "pr_number": 1042,
            "base": {
                "repository": "orenvlad-ai/wb-core",
                "ref": "main",
                "sha": BASE_SHA,
            },
            "head": {
                "repository": "orenvlad-ai/wb-core",
                "ref": "codex/shadow-release-runner-admission",
                "sha": HEAD_SHA,
            },
            "expected_head_sha": HEAD_SHA,
            "required_check": {
                "id": 7654321,
                "name": "baseline",
                "app_slug": "github-actions",
                "head_sha": HEAD_SHA,
                "status": "completed",
                "conclusion": "success",
            },
            "task": ["task:standard"],
            "scope": ["scope:repo-only"],
        },
    }


def test_key_label_order_and_digest_stability() -> None:
    original = golden_snapshot()
    reordered = OrderedDict(reversed(list(original.items())))
    reordered["required_check"] = OrderedDict(
        reversed(list(original["required_check"].items()))
    )
    reordered["labels"] = [
        " RELEASE:READY ",
        "scope:repo-only",
        "TASK:STANDARD",
        "task:standard",
    ]
    reordered["base_sha"] = BASE_SHA.upper()
    reordered["head_sha"] = HEAD_SHA.upper()
    reordered["expected_head_sha"] = HEAD_SHA.upper()
    reordered["required_check"]["head_sha"] = HEAD_SHA.upper()
    first = admission.admission_receipt(original)
    second = admission.admission_receipt(reordered)
    assert first == second
    assert admission.canonical_snapshot_bytes(original) == (
        admission.canonical_snapshot_bytes(reordered)
    )


def test_reason_matrix() -> None:
    cases: list[tuple[str, dict[str, Any], list[str]]] = [
        (
            "schema",
            changed(lambda item: item.__setitem__("schema", "future/v2")),
            ["snapshot-schema-unsupported"],
        ),
        (
            "shape",
            changed(lambda item: item.pop("mergeable")),
            ["snapshot-shape-invalid", "mergeability-unknown"],
        ),
        (
            "repository-and-pr",
            changed(
                lambda item: item.update(
                    repository="someone/wb-core", pr_number=0
                )
            ),
            [
                "repository-not-canonical",
                "pr-number-invalid",
                "base-repository-mismatch",
                "head-repository-mismatch",
            ],
        ),
        (
            "closed-draft-base-fork",
            changed(
                lambda item: item.update(
                    state="closed",
                    draft=True,
                    base_repository="fork/wb-core",
                    base_ref="develop",
                    head_repository="fork/wb-core",
                )
            ),
            [
                "pr-not-open",
                "pr-draft",
                "base-repository-mismatch",
                "base-not-main",
                "head-repository-mismatch",
            ],
        ),
        (
            "missing-ref-and-invalid-shas",
            changed(
                lambda item: (
                    item.update(
                        base_sha="",
                        head_ref="",
                        head_sha="not-a-sha",
                        expected_head_sha="",
                    ),
                    item["required_check"].update(head_sha=""),
                )
            ),
            [
                "base-sha-invalid",
                "head-ref-missing",
                "head-sha-invalid",
                "expected-head-sha-invalid",
                "required-check-head-mismatch",
            ],
        ),
        (
            "head-drift",
            changed(lambda item: item.update(expected_head_sha="c" * 40)),
            ["head-not-expected"],
        ),
        (
            "task-scope-release-conflicts",
            changed(
                lambda item: item.update(
                    labels=[
                        "task:other",
                        "scope:live-runtime",
                        "release:blocked",
                        "release:running",
                    ]
                )
            ),
            [
                "task-label-not-standard",
                "scope-label-not-repo-only",
                "release-ready-missing",
                "release-state-conflict",
            ],
        ),
        (
            "dcp",
            changed(lambda item: item.update(head_ref="ao/wb-core-7/root")),
            ["dcp-handoff-unsupported"],
        ),
        (
            "finance",
            changed(
                lambda item: item["labels"].append(
                    "finance:migration-deploy-lease"
                )
            ),
            ["finance-lease-unsupported"],
        ),
        (
            "legacy",
            changed(lambda item: item["labels"].append("legacy:readmission")),
            ["legacy-contour-unsupported"],
        ),
        (
            "check",
            changed(
                lambda item: item["required_check"].update(
                    id=0,
                    name="other",
                    app_slug="external-ci",
                    head_sha="c" * 40,
                    status="in_progress",
                    conclusion="failure",
                )
            ),
            [
                "required-check-id-invalid",
                "required-check-name-mismatch",
                "required-check-source-mismatch",
                "required-check-head-mismatch",
                "required-check-not-completed",
                "required-check-not-successful",
            ],
        ),
        (
            "mergeability-null",
            changed(lambda item: item.update(mergeable=None)),
            ["mergeability-unknown"],
        ),
        (
            "mergeability-false",
            changed(lambda item: item.update(mergeable=False)),
            ["mergeability-false"],
        ),
    ]
    for name, snapshot, expected in cases:
        assert_reasons(name, snapshot, expected)


def test_full_reason_order() -> None:
    snapshot = changed(
        lambda item: (
            item.update(
                schema="future/v2",
                repository="other/repo",
                pr_number=0,
                state="closed",
                draft=True,
                base_repository="base/fork",
                base_ref="develop",
                base_sha="bad",
                head_repository="head/fork",
                head_ref="ao/wb-core-3/root",
                head_sha="bad",
                expected_head_sha="bad",
                labels=[
                    "task:loop",
                    "scope:production-mutation",
                    "release:halted",
                    "finance:migration-deploy-lease",
                    "legacy:recovery",
                ],
                mergeable=None,
            ),
            item["required_check"].update(
                id=0,
                name="other",
                app_slug="external",
                head_sha="bad",
                status="queued",
                conclusion="failure",
            ),
        )
    )
    expected = [
        code
        for code in admission.REASON_CODES
        if code
        not in {
            "snapshot-shape-invalid",
            "head-ref-missing",
            "head-not-expected",
            "mergeability-false",
        }
    ]
    assert_reasons("full-order", snapshot, expected)


def test_byte_stable_json() -> None:
    first = admission.receipt_json_bytes(golden_snapshot())
    second = admission.receipt_json_bytes(golden_snapshot())
    assert first == second
    assert first == admission.canonical_json_bytes(json.loads(first))
    assert not first.endswith(b"\n")


def test_admission_has_no_forbidden_adapters() -> None:
    source_path = Path(admission.__file__).resolve()
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    forbidden_import_roots = {
        "argparse",
        "aiohttp",
        "asyncio",
        "http",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "subprocess",
        "urllib",
    }
    imported: set[str] = set()
    full_imports: set[str] = set()
    called_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            full_imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
            full_imports.add(node.module)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            called_names.add(node.func.id)
    assert imported.issubset(
        {"__future__", "collections", "hashlib", "json", "re", "typing"}
    )
    assert not imported.intersection(forbidden_import_roots)
    assert not any(
        module.endswith(
            ("github_release_train", "github_release_train_spec")
        )
        for module in full_imports
    )
    assert not called_names.intersection({"open", "exec", "eval", "compile"})
    assert "__main__" not in source_path.read_text(encoding="utf-8")


def main() -> int:
    test_golden_eligible()
    test_key_label_order_and_digest_stability()
    test_reason_matrix()
    test_full_reason_order()
    test_byte_stable_json()
    test_admission_has_no_forbidden_adapters()
    print("github_release_runner_shadow_admission_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
