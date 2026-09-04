#!/usr/bin/env python3
"""Build a small, base-owned check plan for one exact pull request diff."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
MAP_PATH = ROOT / "ci/checks.json"
PLAN_SCHEMA = "wb-core.check-plan/v1"
SHA_RE = re.compile(r"[0-9a-f]{40}")
KNOWN_ROOTS = {".github", "apps", "artifacts", "ci", "docs", "gas", "packages", "registry"}
KNOWN_ROOT_FILES = {".clasp.json", ".gitignore", "AGENTS.md", "README.md"}
KNOWN_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".py", ".toml", ".yaml", ".yml"}
REPO_ONLY_PREFIXES = (".github/", "ci/", "docs/")
REPO_ONLY_FILES = {".clasp.json", ".gitignore", "AGENTS.md", "README.md"}


class PlanError(RuntimeError):
    pass


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def exact_sha(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if SHA_RE.fullmatch(normalized) is None:
        raise PlanError(f"{label} is not a full commit SHA")
    return normalized


def safe_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    if not normalized or normalized.startswith("/") or ".." in Path(normalized).parts:
        raise PlanError(f"unsafe changed path: {path!r}")
    root = normalized.split("/", 1)[0]
    if normalized not in KNOWN_ROOT_FILES and root not in KNOWN_ROOTS:
        raise PlanError(f"unclassified top-level path: {normalized}")
    suffix = Path(normalized).suffix.lower()
    if suffix not in KNOWN_SUFFIXES:
        raise PlanError(f"unclassified file type: {normalized}")
    return normalized


def load_map() -> tuple[dict[str, Any], str]:
    raw = MAP_PATH.read_bytes()
    payload = json.loads(raw)
    if payload.get("schema") != "wb-core.check-map/v1" or not isinstance(payload.get("groups"), dict):
        raise PlanError("invalid check map")
    return payload, digest(raw)


def changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "--find-renames", f"{base}...{head}"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    paths: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        for value in fields[1:]:
            paths.append(safe_path(value))
    return sorted(set(paths))


def git_file_exists(head: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{head}:{path}"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def build_plan_from_paths(
    *,
    pull_request: int,
    base: str,
    head: str,
    paths: list[str],
    file_exists: Callable[[str, str], bool],
) -> dict[str, Any]:
    mapping, mapping_sha = load_map()
    safe_paths = [safe_path(path) for path in paths]
    groups: list[str] = []
    commands: list[list[str]] = []
    pip: list[str] = []

    for group_name, group in mapping["groups"].items():
        patterns = group.get("patterns") or []
        if any(fnmatch.fnmatch(path, pattern) for path in safe_paths for pattern in patterns):
            groups.append(group_name)
            commands.extend(group.get("commands") or [])
            pip.extend(group.get("pip") or [])

    python_paths = sorted(path for path in safe_paths if path.endswith(".py") and file_exists(head, path))
    if python_paths:
        commands.insert(0, ["python3", "-m", "py_compile", *python_paths])

    for path in python_paths:
        if path.endswith("_smoke.py"):
            commands.append(["python3", path])
            continue
        sibling = path[:-3] + "_smoke.py"
        if file_exists(head, sibling):
            commands.append(["python3", sibling])

    unique_commands: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for command in commands:
        normalized = tuple(str(part) for part in command)
        if not normalized or normalized in seen:
            continue
        if normalized[0] not in {"python3", "node"}:
            raise PlanError(f"unsupported check command: {normalized[0]}")
        seen.add(normalized)
        unique_commands.append(list(normalized))

    release_kind = (
        "repo_only"
        if safe_paths and all(path in REPO_ONLY_FILES or path.startswith(REPO_ONLY_PREFIXES) for path in safe_paths)
        else "live_runtime"
    )
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "pull_request": pull_request,
        "base_sha": exact_sha(base, "base"),
        "head_sha": exact_sha(head, "head"),
        "changed_paths": sorted(set(safe_paths)),
        "groups": sorted(groups),
        "commands": unique_commands,
        "pip": sorted(set(pip)),
        "release_kind": release_kind,
        "check_map_sha256": mapping_sha,
    }
    plan["plan_sha256"] = digest(canonical_bytes(plan))
    return plan


def verify_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema") != PLAN_SCHEMA:
        raise PlanError("unsupported plan schema")
    expected = dict(plan)
    supplied = expected.pop("plan_sha256", None)
    if supplied != digest(canonical_bytes(expected)):
        raise PlanError("plan digest mismatch")
    exact_sha(str(plan.get("base_sha") or ""), "base")
    exact_sha(str(plan.get("head_sha") or ""), "head")
    if plan.get("release_kind") not in {"repo_only", "live_runtime"}:
        raise PlanError("invalid release kind")
    if not isinstance(plan.get("commands"), list) or not isinstance(plan.get("changed_paths"), list):
        raise PlanError("invalid plan shape")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    base = exact_sha(args.base, "base")
    head = exact_sha(args.head, "head")
    plan = build_plan_from_paths(
        pull_request=args.pr,
        base=base,
        head=head,
        paths=changed_paths(base, head),
        file_exists=git_file_exists,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_bytes(plan) + b"\n")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"release_kind={plan['release_kind']}\n")
            handle.write(f"plan_sha256={plan['plan_sha256']}\n")
            handle.write(f"has_checks={'true' if plan['commands'] else 'false'}\n")
    print(json.dumps(plan, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
