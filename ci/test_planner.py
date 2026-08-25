#!/usr/bin/env python3
"""Build the canonical protocol-v2 test and release plan for one PR head."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REGISTRY_PATH = "ci/test_registry.json"
REGISTRY_SCHEMA = "wb-core.test-registry/v2"
PLAN_SCHEMA = "wb-core.test-plan/v2"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_ORDER = {"repo_only": 0, "live_runtime": 1, "production_mutation": 2}


class PlanError(ValueError):
    """The requested plan cannot be constructed without guessing."""


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=check,
    )


def _exact_sha(value: str, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if SHA_RE.fullmatch(normalized) is None:
        raise PlanError(f"{field} must be an exact 40-character commit SHA")
    if _git("cat-file", "-e", f"{normalized}^{{commit}}", check=False).returncode != 0:
        raise PlanError(f"{field} commit is unavailable: {normalized}")
    return normalized


def _load_json_bytes(raw: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise PlanError(f"{source} must contain a JSON object")
    return value


def load_registry_at(commit: str) -> tuple[dict[str, Any] | None, str | None]:
    result = _git("show", f"{commit}:{REGISTRY_PATH}", check=False)
    if result.returncode != 0:
        return None, None
    raw = result.stdout.encode("utf-8")
    return _load_json_bytes(raw, f"{commit}:{REGISTRY_PATH}"), sha256(raw)


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise PlanError(f"{field} must be a non-empty-string array")
    return list(value)


def validate_registry(registry: Mapping[str, Any], source: str) -> None:
    if registry.get("schema") != REGISTRY_SCHEMA:
        raise PlanError(f"unsupported registry schema in {source}")
    protocol = registry.get("protocol")
    suites = registry.get("suites")
    rules = registry.get("rules")
    if not isinstance(protocol, Mapping) or protocol.get("version") != 2:
        raise PlanError(f"{source} must declare protocol version 2")
    if SHA_RE.fullmatch(str(protocol.get("cutover_epoch") or "")) is None:
        raise PlanError(f"{source} has an invalid cutover epoch")
    full = _string_list(protocol.get("full_regression_suites"), f"{source}.full_regression_suites")
    if not isinstance(suites, Mapping) or not suites:
        raise PlanError(f"{source}.suites must be a non-empty object")
    for suite_id, suite in suites.items():
        if not isinstance(suite_id, str) or not suite_id or not isinstance(suite, Mapping):
            raise PlanError(f"{source} contains an invalid suite")
        if not isinstance(suite.get("group"), str) or not suite.get("group"):
            raise PlanError(f"{source}.{suite_id}.group is invalid")
        dependencies = suite.get("depends_on", [])
        if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
            raise PlanError(f"{source}.{suite_id}.depends_on is invalid")
        commands = suite.get("commands")
        if not isinstance(commands, list) or not commands:
            raise PlanError(f"{source}.{suite_id}.commands must not be empty")
        for command in commands:
            if not isinstance(command, list) or not command or any(not isinstance(part, str) or not part for part in command):
                raise PlanError(f"{source}.{suite_id} contains an invalid command")
    missing_full = sorted(set(full) - set(suites))
    if missing_full:
        raise PlanError(f"{source} full regression names unknown suites: {missing_full}")
    if not isinstance(rules, list) or not rules:
        raise PlanError(f"{source}.rules must be a non-empty array")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise PlanError(f"{source}.rules[{index}] is invalid")
        _string_list(rule.get("paths"), f"{source}.rules[{index}].paths")
        selected = _string_list(rule.get("suites"), f"{source}.rules[{index}].suites")
        if sorted(set(selected) - set(suites)):
            raise PlanError(f"{source}.rules[{index}] names an unknown suite")
        if rule.get("release") not in RELEASE_ORDER:
            raise PlanError(f"{source}.rules[{index}].release is invalid")


def _normalized_suite(value: Mapping[str, Any]) -> dict[str, Any]:
    commands = sorted({tuple(command) for command in value.get("commands", [])})
    return {
        "group": str(value["group"]),
        "depends_on": sorted(set(value.get("depends_on", []))),
        "requires_browser": bool(value.get("requires_browser", False)),
        "commands": [list(command) for command in commands],
    }


def union_registries(
    base: Mapping[str, Any] | None,
    head: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Union base and head so a branch cannot remove its own coverage."""

    validate_registry(head, "head registry")
    registries = [head]
    reason_codes: list[str] = []
    if base is None:
        reason_codes.append("base-registry-missing")
    else:
        validate_registry(base, "base registry")
        registries.insert(0, base)

    epochs = {str(item["protocol"]["cutover_epoch"]) for item in registries}
    if len(epochs) != 1:
        raise PlanError("base and head registry cutover epochs conflict")

    suites: dict[str, dict[str, Any]] = {}
    for registry in registries:
        for suite_id, raw_suite in registry["suites"].items():
            suite = _normalized_suite(raw_suite)
            if suite_id not in suites:
                suites[suite_id] = suite
                continue
            current = suites[suite_id]
            if current["group"] != suite["group"]:
                raise PlanError(f"suite group changed ambiguously for {suite_id}")
            current["depends_on"] = sorted(set(current["depends_on"]) | set(suite["depends_on"]))
            current["requires_browser"] = current["requires_browser"] or suite["requires_browser"]
            current["commands"] = [
                list(command)
                for command in sorted(
                    {tuple(command) for command in current["commands"] + suite["commands"]}
                )
            ]

    rule_values: dict[bytes, dict[str, Any]] = {}
    for registry in registries:
        for raw_rule in registry["rules"]:
            rule = {
                "id": str(raw_rule.get("id") or "").strip(),
                "paths": sorted(set(raw_rule["paths"])),
                "suites": sorted(set(raw_rule["suites"])),
                "release": raw_rule["release"],
                "force_full": bool(raw_rule.get("force_full", False)),
            }
            rule_values[canonical_json_bytes(rule)] = rule

    full = sorted(
        {
            suite
            for registry in registries
            for suite in registry["protocol"]["full_regression_suites"]
        }
    )
    union = {
        "schema": REGISTRY_SCHEMA,
        "protocol": {
            "version": 2,
            "cutover_epoch": epochs.pop(),
            "full_regression_suites": full,
        },
        "suites": {key: suites[key] for key in sorted(suites)},
        "rules": [rule_values[key] for key in sorted(rule_values)],
    }
    validate_registry(union, "registry union")
    return union, reason_codes


def changed_paths(base_sha: str, head_sha: str) -> list[dict[str, str]]:
    output = _git(
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        base_sha,
        head_sha,
        "--",
    ).stdout
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    records: list[dict[str, str]] = []
    index = 0
    while index < len(fields):
        status = fields[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(fields):
                raise PlanError("truncated rename/copy diff record")
            records.append(
                {"status": status, "old_path": fields[index], "path": fields[index + 1]}
            )
            index += 2
        else:
            if index >= len(fields):
                raise PlanError("truncated diff record")
            records.append({"status": status, "path": fields[index]})
            index += 1
    return sorted(records, key=lambda item: (item.get("path", ""), item.get("old_path", ""), item["status"]))


def _record_paths(records: Sequence[Mapping[str, str]]) -> list[str]:
    return sorted({path for record in records for key in ("old_path", "path") if (path := record.get(key))})


def _matches(path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _dependency_closure(selected: set[str], suites: Mapping[str, Mapping[str, Any]]) -> tuple[set[str], bool]:
    expanded = set(selected)
    changed = False
    while True:
        additions = {
            dependency
            for suite_id in expanded
            for dependency in suites[suite_id].get("depends_on", [])
            if dependency not in expanded
        }
        if not additions:
            return expanded, changed
        unknown = additions - set(suites)
        if unknown:
            raise PlanError(f"unresolved suite dependencies: {sorted(unknown)}")
        expanded.update(additions)
        changed = True


def _release_kind(current: str, candidate: str) -> str:
    return candidate if RELEASE_ORDER[candidate] > RELEASE_ORDER[current] else current


def _manifest_binding(head_sha: str, paths: Sequence[str]) -> tuple[dict[str, Any] | None, list[str]]:
    manifest_paths = sorted(path for path in paths if fnmatch.fnmatchcase(path, "release/production-mutations/*.json"))
    if len(manifest_paths) != 1:
        return None, ["production-manifest-count-invalid"]
    path = manifest_paths[0]
    result = _git("show", f"{head_sha}:{path}", check=False)
    if result.returncode != 0:
        return None, ["production-manifest-unreadable"]
    raw = result.stdout.encode("utf-8")
    value = _load_json_bytes(raw, f"{head_sha}:{path}")
    from apps.release_protocol import validate_production_manifest

    if validate_production_manifest(value)["valid"] is not True:
        return {"path": path, "sha256": sha256(raw)}, ["production-manifest-contract-invalid"]
    return {"path": path, "sha256": sha256(raw)}, []


def build_plan(
    *,
    pr_number: int,
    base_sha: str,
    head_sha: str,
    base_registry: Mapping[str, Any] | None,
    head_registry: Mapping[str, Any],
    base_registry_blob_sha256: str | None = None,
    head_registry_blob_sha256: str | None = None,
    changes: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    if pr_number <= 0:
        raise PlanError("pull request number must be positive")
    union, reason_codes = union_registries(base_registry, head_registry)
    cutover_bootstrap = (
        base_registry is None and base_sha == union["protocol"]["cutover_epoch"]
    )
    paths = _record_paths(changes)
    suites = union["suites"]
    selected: set[str] = set()
    release_kind = "repo_only"
    unknown_paths: list[str] = []
    force_full = False

    for path in paths:
        matches = [rule for rule in union["rules"] if _matches(path, rule["paths"])]
        if not matches:
            unknown_paths.append(path)
            release_kind = _release_kind(release_kind, "live_runtime")
            continue
        for rule in matches:
            selected.update(rule["suites"])
            release_kind = _release_kind(release_kind, rule["release"])
            force_full = force_full or bool(rule.get("force_full"))

    if unknown_paths:
        reason_codes.append("unknown-path-full-regression")
        force_full = True
    if force_full:
        reason_codes.append("registry-or-core-full-regression")
        selected.update(union["protocol"]["full_regression_suites"])
    if not paths:
        reason_codes.append("empty-diff-full-regression")
        selected.update(union["protocol"]["full_regression_suites"])
    selected, dependency_added = _dependency_closure(selected, suites)
    if dependency_added:
        reason_codes.append("transitive-domain-dependency")
    if not selected:
        raise PlanError("test selection resolved to no suites")

    if cutover_bootstrap:
        release_kind = "repo_only"
        reason_codes.append("cutover-bootstrap-no-deploy")

    manifest = None
    plan_errors: list[str] = []
    if release_kind == "production_mutation":
        manifest, plan_errors = _manifest_binding(head_sha, paths)

    selected_ids = sorted(selected)
    execution = {
        suite_id: {
            "group": suites[suite_id]["group"],
            "requires_browser": suites[suite_id]["requires_browser"],
            "commands": suites[suite_id]["commands"],
        }
        for suite_id in selected_ids
    }
    groups = sorted({suite["group"] for suite in execution.values()})
    union_bytes = canonical_json_bytes(union)
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "protocol_version": 2,
        "cutover_epoch": union["protocol"]["cutover_epoch"],
        "pull_request": pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "registry": {
            "path": REGISTRY_PATH,
            "base_blob_sha256": base_registry_blob_sha256,
            "head_blob_sha256": head_registry_blob_sha256,
            "union_sha256": sha256(union_bytes),
        },
        "changed_paths": list(changes),
        "changed_paths_digest": sha256(canonical_json_bytes(list(changes))),
        "unknown_paths": unknown_paths,
        "selected_suites": selected_ids,
        "groups": groups,
        "execution": execution,
        "release_plan": {
            "kind": release_kind,
            "deploy_required": release_kind in {"live_runtime", "production_mutation"},
            "production_apply_required": release_kind == "production_mutation",
            "manifest": manifest,
            "valid": not plan_errors,
        },
        "reason_codes": sorted(set(reason_codes + plan_errors)),
    }
    plan["plan_hash"] = sha256(canonical_json_bytes(plan))
    return plan


def verify_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("schema") != PLAN_SCHEMA:
        raise PlanError("test plan schema is unsupported")
    supplied = str(plan.get("plan_hash") or "")
    unsigned = dict(plan)
    unsigned.pop("plan_hash", None)
    expected = sha256(canonical_json_bytes(unsigned))
    if supplied != expected:
        raise PlanError("test plan hash mismatch")


def write_github_output(path: Path, plan: Mapping[str, Any]) -> None:
    values = {
        "groups": json.dumps(plan["groups"], separators=(",", ":")),
        "plan_hash": plan["plan_hash"],
        "release_kind": plan["release_plan"]["kind"],
        "plan_valid": str(bool(plan["release_plan"]["valid"])).lower(),
    }
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    base_sha = _exact_sha(args.base, "base")
    head_sha = _exact_sha(args.head, "head")
    base_registry, base_digest = load_registry_at(base_sha)
    head_registry, head_digest = load_registry_at(head_sha)
    if head_registry is None or head_digest is None:
        raise PlanError("head does not contain ci/test_registry.json")
    plan = build_plan(
        pr_number=args.pr,
        base_sha=base_sha,
        head_sha=head_sha,
        base_registry=base_registry,
        head_registry=head_registry,
        base_registry_blob_sha256=base_digest,
        head_registry_blob_sha256=head_digest,
        changes=changed_paths(base_sha, head_sha),
    )
    verify_plan(plan)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(plan) + b"\n")
    if args.github_output:
        write_github_output(args.github_output, plan)
    print(json.dumps({"plan_hash": plan["plan_hash"], "selected_suites": plan["selected_suites"], "release_plan": plan["release_plan"], "reason_codes": plan["reason_codes"]}, sort_keys=True))
    return 0 if plan["release_plan"]["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
