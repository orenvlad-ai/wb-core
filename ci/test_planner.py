#!/usr/bin/env python3
"""Build the canonical protocol-v2 test and release plan for one PR head."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REGISTRY_PATH = "ci/test_registry.json"
PR_GATE_WORKFLOW_PATH = ".github/workflows/pr-gate.yml"
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


def fast_core_workflow_commands() -> list[tuple[str, ...]]:
    """Extract unconditional exact commands from multiline runs in PR Gate core."""

    workflow_path = ROOT / PR_GATE_WORKFLOW_PATH
    if not workflow_path.is_file():
        raise PlanError(f"Fast core workflow is missing: {PR_GATE_WORKFLOW_PATH}")
    lines = workflow_path.read_text(encoding="utf-8").splitlines()
    try:
        core_start = lines.index("  core:")
    except ValueError as exc:
        raise PlanError("PR Gate workflow lacks the core job") from exc
    core_end = next(
        (
            index
            for index in range(core_start + 1, len(lines))
            if re.fullmatch(r"  [A-Za-z0-9_-]+:", lines[index])
        ),
        len(lines),
    )
    core_lines = lines[core_start:core_end]
    if any(line.startswith("    if:") for line in core_lines):
        raise PlanError("PR Gate core job must be unconditional")

    commands: list[tuple[str, ...]] = []
    step_start = 0
    for index, line in enumerate(core_lines):
        if line.startswith("      - "):
            step_start = index
        if line != "        run: |":
            continue
        step_end = next(
            (
                cursor
                for cursor in range(index + 1, len(core_lines))
                if core_lines[cursor].startswith("      - ")
            ),
            len(core_lines),
        )
        if any(
            candidate.startswith("        if:")
            for candidate in core_lines[step_start:step_end]
        ):
            continue
        for command_line in core_lines[index + 1 : step_end]:
            if not command_line.startswith("          "):
                continue
            stripped = command_line.strip()
            if not stripped or stripped.startswith(("#", "set ")):
                continue
            try:
                command = tuple(shlex.split(stripped))
            except ValueError as exc:
                raise PlanError("PR Gate Fast core contains an invalid command line") from exc
            if command:
                commands.append(command)
    return commands


def validate_core_only_commands(
    entries: Sequence[Mapping[str, Any]], source: str
) -> None:
    workflow_commands = fast_core_workflow_commands() if entries else []
    for index, entry in enumerate(entries):
        command = tuple(entry["command"])
        path = _repo_command_path(command)
        if path is None:
            raise PlanError(
                f"{source}.core_only_commands[{index}] lacks a repo script path"
            )
        if not (ROOT / path).is_file():
            raise PlanError(
                f"{source}.core_only_commands[{index}] path does not exist: {path}"
            )
        if workflow_commands.count(command) != 1:
            raise PlanError(
                f"{source}.core_only_commands[{index}] must occur exactly once in unconditional PR Gate Fast core: {list(command)}"
            )


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
    command_self_coverage = protocol.get("command_self_coverage", False)
    if not isinstance(command_self_coverage, bool):
        raise PlanError(f"{source}.command_self_coverage must be boolean")
    core_only = protocol.get("core_only_commands", [])
    if not isinstance(core_only, list):
        raise PlanError(f"{source}.core_only_commands must be an array")
    normalized_core_only: set[tuple[str, ...]] = set()
    for index, entry in enumerate(core_only):
        if not isinstance(entry, Mapping):
            raise PlanError(f"{source}.core_only_commands[{index}] is invalid")
        command = entry.get("command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part for part in command)
        ):
            raise PlanError(f"{source}.core_only_commands[{index}].command is invalid")
        justification = entry.get("justification")
        if not isinstance(justification, str) or not justification.strip():
            raise PlanError(f"{source}.core_only_commands[{index}].justification is required")
        normalized = tuple(command)
        if normalized in normalized_core_only:
            raise PlanError(f"{source}.core_only_commands contains a duplicate command")
        normalized_core_only.add(normalized)
    validate_core_only_commands(core_only, source)
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
    missing_dependencies = sorted(
        {
            dependency
            for suite in suites.values()
            for dependency in suite.get("depends_on", [])
            if dependency not in suites
        }
    )
    if missing_dependencies:
        raise PlanError(f"{source} has unresolved suite dependencies: {missing_dependencies}")
    if not isinstance(rules, list) or not rules:
        raise PlanError(f"{source}.rules must be a non-empty array")
    for index, rule in enumerate(rules):
        if not isinstance(rule, Mapping):
            raise PlanError(f"{source}.rules[{index}] is invalid")
        _string_list(rule.get("paths"), f"{source}.rules[{index}].paths")
        exclude_paths = rule.get("exclude_paths", [])
        if not isinstance(exclude_paths, list) or any(
            not isinstance(item, str) or not item for item in exclude_paths
        ):
            raise PlanError(f"{source}.rules[{index}].exclude_paths is invalid")
        selected = _string_list(rule.get("suites"), f"{source}.rules[{index}].suites")
        if sorted(set(selected) - set(suites)):
            raise PlanError(f"{source}.rules[{index}] names an unknown suite")
        if rule.get("release") not in RELEASE_ORDER:
            raise PlanError(f"{source}.rules[{index}].release is invalid")
    if command_self_coverage:
        coverage = registered_command_coverage(registry)
        if coverage["gaps"]:
            raise PlanError(
                f"{source} registered command self-coverage gaps: {coverage['gaps']}"
            )


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
                "exclude_paths": sorted(set(raw_rule.get("exclude_paths", []))),
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
    core_only_values: dict[bytes, dict[str, Any]] = {}
    for registry in registries:
        for entry in registry["protocol"].get("core_only_commands", []):
            normalized = {
                "command": list(entry["command"]),
                "justification": str(entry["justification"]).strip(),
            }
            core_only_values[canonical_json_bytes(normalized)] = normalized
    union = {
        "schema": REGISTRY_SCHEMA,
        "protocol": {
            "version": 2,
            "cutover_epoch": epochs.pop(),
            "command_self_coverage": any(
                bool(registry["protocol"].get("command_self_coverage", False))
                for registry in registries
            ),
            "core_only_commands": [
                core_only_values[key] for key in sorted(core_only_values)
            ],
            "full_regression_suites": full,
        },
        "suites": {key: suites[key] for key in sorted(suites)},
        "rules": [rule_values[key] for key in sorted(rule_values)],
    }
    validate_registry(union, "registry union")
    return union, reason_codes


def registry_union_for_plan(
    base: Mapping[str, Any] | None,
    head: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str], bool]:
    """Return an executable union, falling back to full coverage on invalid input.

    A valid counterpart may supply the exact commands needed to execute a full
    regression, but the resulting release plan remains invalid so the PR cannot
    pass the aggregate gate.
    """

    base_error = False
    head_error = False
    if base is not None:
        try:
            validate_registry(base, "base registry")
        except PlanError:
            base_error = True
    try:
        validate_registry(head, "head registry")
    except PlanError:
        head_error = True

    if head_error:
        if base is None or base_error:
            raise PlanError("both registry inputs are invalid; no executable full regression exists")
        fallback, _ = union_registries(base, base)
        return fallback, ["head-registry-invalid-full-regression"], False
    if base_error:
        fallback, _ = union_registries(head, head)
        return fallback, ["base-registry-invalid-full-regression"], False
    try:
        union, reasons = union_registries(base, head)
    except PlanError:
        fallback, _ = union_registries(head, head)
        return fallback, ["registry-union-invalid-full-regression"], False
    return union, reasons, True


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


def _rule_matches(path: str, rule: Mapping[str, Any]) -> bool:
    return _matches(path, rule["paths"]) and not _matches(
        path, rule.get("exclude_paths", [])
    )


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


def _repo_command_path(command: Sequence[str]) -> str | None:
    for part in command[1:]:
        if (
            not part.startswith("-")
            and not Path(part).is_absolute()
            and part.endswith((".py", ".mjs", ".sh"))
        ):
            return part
    return None


def registered_command_coverage(registry: Mapping[str, Any]) -> dict[str, Any]:
    suites = registry["suites"]
    rules = registry["rules"]
    core_only = {
        tuple(entry["command"])
        for entry in registry["protocol"].get("core_only_commands", [])
    }
    commands = {
        tuple(command)
        for suite in suites.values()
        for command in suite["commands"]
    }
    gaps: list[dict[str, Any]] = []
    for command in sorted(commands):
        path = _repo_command_path(command)
        if path is None:
            if command not in core_only:
                gaps.append({"command": list(command), "reason": "repo-command-path-unresolved"})
            continue
        matched = [rule for rule in rules if _rule_matches(path, rule)]
        selected = {
            suite_id for rule in matched for suite_id in rule["suites"]
        }
        selected, _dependency_added = _dependency_closure(selected, suites)
        selected_commands = {
            tuple(candidate)
            for suite_id in selected
            for candidate in suites[suite_id]["commands"]
        }
        if command not in selected_commands and command not in core_only:
            gaps.append(
                {
                    "command": list(command),
                    "path": path,
                    "selected_suites": sorted(selected),
                    "reason": "own-path-does-not-execute-command",
                }
            )
    return {
        "valid": not gaps,
        "registered_command_count": len(commands),
        "core_only_command_count": len(core_only),
        "gaps": gaps,
    }


def _deduplicated_execution(
    selected_ids: Sequence[str], suites: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], int]:
    seen: set[tuple[str, ...]] = set()
    duplicate_count = 0
    execution: dict[str, Any] = {}
    for suite_id in selected_ids:
        commands: list[list[str]] = []
        for command in suites[suite_id]["commands"]:
            normalized = tuple(command)
            if normalized in seen:
                duplicate_count += 1
                continue
            seen.add(normalized)
            commands.append(list(command))
        execution[suite_id] = {
            "group": suites[suite_id]["group"],
            "requires_browser": suites[suite_id]["requires_browser"],
            "commands": commands,
        }
    return execution, duplicate_count


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
    registry_error_codes: Sequence[str] = (),
) -> dict[str, Any]:
    if pr_number <= 0:
        raise PlanError("pull request number must be positive")
    union, reason_codes, registry_valid = registry_union_for_plan(
        base_registry, head_registry
    )
    if registry_error_codes:
        registry_valid = False
        reason_codes.extend(registry_error_codes)
    cutover_bootstrap = (
        base_registry is None and base_sha == union["protocol"]["cutover_epoch"]
    )
    if base_registry is None and not cutover_bootstrap:
        registry_valid = False
        reason_codes.append("base-registry-missing-full-regression")
    paths = _record_paths(changes)
    suites = union["suites"]
    selected: set[str] = set()
    release_kind = "repo_only"
    unknown_paths: list[str] = []
    force_full = not registry_valid

    for path in paths:
        matches = [rule for rule in union["rules"] if _rule_matches(path, rule)]
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
    plan_errors: list[str] = [] if registry_valid else ["registry-invalid"]
    if release_kind == "production_mutation":
        manifest, manifest_errors = _manifest_binding(head_sha, paths)
        plan_errors.extend(manifest_errors)

    selected_ids = sorted(selected)
    execution, duplicate_command_count = _deduplicated_execution(selected_ids, suites)
    if duplicate_command_count:
        reason_codes.append("duplicate-command-deduplicated")
    groups = sorted(
        {suite["group"] for suite in execution.values() if suite["commands"]}
    )
    browser_groups = sorted(
        {
            suite["group"]
            for suite in execution.values()
            if suite["requires_browser"] is True and suite["commands"]
        }
    )
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
            "command_self_coverage": registered_command_coverage(union),
            "deduplicated_command_count": duplicate_command_count,
            "valid": registry_valid,
        },
        "changed_paths": list(changes),
        "changed_paths_digest": sha256(canonical_json_bytes(list(changes))),
        "unknown_paths": unknown_paths,
        "selected_suites": selected_ids,
        "groups": groups,
        "browser_groups": browser_groups,
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
    execution = plan.get("execution")
    if not isinstance(execution, Mapping):
        raise PlanError("test plan execution is invalid")
    commands: list[tuple[str, ...]] = []
    for suite in execution.values():
        if not isinstance(suite, Mapping) or not isinstance(suite.get("commands"), list):
            raise PlanError("test plan suite execution is invalid")
        commands.extend(tuple(command) for command in suite["commands"])
    if len(commands) != len(set(commands)):
        raise PlanError("test plan executes a duplicate command")


def write_github_output(path: Path, plan: Mapping[str, Any]) -> None:
    values = {
        "groups": json.dumps(plan["groups"], separators=(",", ":")),
        "browser_groups": json.dumps(plan["browser_groups"], separators=(",", ":")),
        "plan_hash": plan["plan_hash"],
        "release_kind": plan["release_plan"]["kind"],
        "plan_valid": str(bool(plan["release_plan"]["valid"])).lower(),
        "execution_valid": "true",
        "release_valid": str(bool(plan["release_plan"]["valid"])).lower(),
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
    registry_error_codes: list[str] = []
    try:
        base_registry, base_digest = load_registry_at(base_sha)
    except PlanError:
        base_registry, base_digest = None, None
        registry_error_codes.append("base-registry-json-invalid-full-regression")
    try:
        head_registry, head_digest = load_registry_at(head_sha)
    except PlanError:
        head_registry, head_digest = None, None
        registry_error_codes.append("head-registry-json-invalid-full-regression")
    if head_registry is None:
        if base_registry is None:
            raise PlanError("head and base lack an executable test registry")
        head_registry = base_registry
        registry_error_codes.append("head-registry-missing-full-regression")
    plan = build_plan(
        pr_number=args.pr,
        base_sha=base_sha,
        head_sha=head_sha,
        base_registry=base_registry,
        head_registry=head_registry,
        base_registry_blob_sha256=base_digest,
        head_registry_blob_sha256=head_digest,
        changes=changed_paths(base_sha, head_sha),
        registry_error_codes=registry_error_codes,
    )
    verify_plan(plan)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(plan) + b"\n")
    if args.github_output:
        write_github_output(args.github_output, plan)
    print(json.dumps({"plan_hash": plan["plan_hash"], "selected_suites": plan["selected_suites"], "release_plan": plan["release_plan"], "reason_codes": plan["reason_codes"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
