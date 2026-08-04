"""Deterministic validation and rollout planning for the C1 curator workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "packages" / "contracts" / "codex_curator_workspace_v1.json"
CANONICAL_REPOSITORY = "orenvlad-ai/wb-core"
SCHEMA = "wb-core-codex-curator-workspace/v1"


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _git(repository: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _origin_slug(origin: str) -> str:
    value = origin.strip().removesuffix(".git")
    for prefix in (
        "https://github.com/",
        "http://github.com/",
        "ssh://git@github.com/",
        "git@github.com:",
    ):
        if value.startswith(prefix):
            return value[len(prefix) :]
    raise ValueError("origin must be a supported GitHub HTTPS/SSH URL")


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    contract = json.loads(path.read_text(encoding="utf-8"))
    if contract.get("schema") != SCHEMA:
        raise ValueError("unexpected curator workspace schema")
    if contract.get("repository") != CANONICAL_REPOSITORY:
        raise ValueError("curator workspace must target the canonical repository")
    return contract


def build_plan(repository: Path, source_ref: str = "origin/main") -> dict[str, Any]:
    repository = repository.resolve()
    root = Path(_git(repository, "rev-parse", "--show-toplevel")).resolve()
    if root != repository:
        raise ValueError("repository path must be the Git root")
    origin = _git(repository, "remote", "get-url", "origin")
    if _origin_slug(origin) != CANONICAL_REPOSITORY:
        raise ValueError("repository origin does not match orenvlad-ai/wb-core")

    contract = load_contract()
    source_sha = _git(repository, "rev-parse", source_ref)
    checkout_head = _git(repository, "rev-parse", "HEAD")
    if checkout_head != source_sha:
        raise ValueError("rollout checkout must match the exact trusted source ref")
    if _git(repository, "status", "--porcelain"):
        raise ValueError("rollout checkout must be clean")
    project = contract["project"]
    discovery = contract["instruction_discovery"]
    role_path = str(discovery["role_delta"])
    config_path = str(discovery["project_config"])
    primary_path = repository / str(project["primary_relative_path"])

    sources: dict[str, dict[str, str]] = {}
    for relative in ("AGENTS.md", role_path, config_path):
        content = subprocess.run(
            ["git", "-C", str(repository), "show", f"{source_ref}:{relative}"],
            check=True,
            capture_output=True,
        ).stdout
        sources[relative] = {"digest": _digest(content), "source_ref": source_ref}

    return {
        "schema": "wb-core-codex-curator-workspace-plan/v1",
        "repository": CANONICAL_REPOSITORY,
        "repository_root": str(repository),
        "source_ref": source_ref,
        "source_sha": source_sha,
        "checkout_head": checkout_head,
        "checkout_matches_source": True,
        "checkout_clean": True,
        "project_label": project["label"],
        "primary_path": str(primary_path),
        "primary_relative_path": project["primary_relative_path"],
        "environment": project["environment"],
        "expected_git_repository": project["expected_git_repository"],
        "instruction_order": discovery["expected_order"],
        "sources": sources,
    }


def validate_checkout(repository: Path = ROOT) -> dict[str, Any]:
    contract = load_contract()
    project = contract["project"]
    discovery = contract["instruction_discovery"]
    role_path = repository / str(discovery["role_delta"])
    config_path = repository / str(discovery["project_config"])
    primary_path = repository / str(project["primary_relative_path"])

    if primary_path != role_path.parent or primary_path != config_path.parent.parent:
        raise ValueError("role/config files must live under the exact primary folder")
    if not role_path.is_file() or not config_path.is_file():
        raise ValueError("curator role/config files are missing")

    role_text = role_path.read_text(encoding="utf-8")
    root_text = (repository / "AGENTS.md").read_text(encoding="utf-8")
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    if config != {
        "model": contract["curator"]["model"],
        "model_reasoning_effort": contract["curator"]["reasoning_effort"],
        "project_doc_max_bytes": discovery["project_doc_max_bytes"],
    }:
        raise ValueError("project config does not match the curator model contract")
    if len(role_text.encode("utf-8")) >= len(root_text.encode("utf-8")) // 4:
        raise ValueError("curator role delta is too large and risks becoming a second protocol")
    for forbidden_heading in (
        "## Источники истины",
        "## Execution-контуры",
        "### `LOOP`",
        "## Production UI-проверки",
    ):
        if forbidden_heading in role_text:
            raise ValueError("curator role delta copied a root protocol section")

    return {
        "schema": "wb-core-codex-curator-workspace-validation/v1",
        "status": "ok",
        "project_label": project["label"],
        "primary_relative_path": project["primary_relative_path"],
        "role_delta_digest": _digest(role_text.encode("utf-8")),
        "project_config_digest": _digest(config_path.read_bytes()),
        "root_protocol_digest": _digest(root_text.encode("utf-8")),
    }


def validate_project_readback(
    readback: Mapping[str, Any], contract: Mapping[str, Any], repository: Path
) -> dict[str, Any]:
    projects = readback.get("projects")
    if not isinstance(projects, list):
        raise ValueError("project readback must contain a projects list")
    project_contract = contract["project"]
    expected_path = str((repository / project_contract["primary_relative_path"]).resolve())
    matches = [
        item
        for item in projects
        if isinstance(item, Mapping) and item.get("label") == project_contract["label"]
    ]
    if len(matches) != 1:
        raise ValueError("readback must contain exactly one curator project")
    project = matches[0]
    if project.get("path") != expected_path:
        raise ValueError("Desktop normalized the curator primary cwd")
    if project.get("hostId") != project_contract["host_id"]:
        raise ValueError("curator project is on the wrong host")
    if project.get("isGitRepository") is not project_contract["expected_git_repository"]:
        raise ValueError("curator project Git classification changed")
    project_id = project.get("projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ValueError("curator project readback is missing an exact project ID")
    return {
        "schema": "wb-core-codex-curator-project-readback/v1",
        "status": "ok",
        "project_id": project_id,
        "label": project.get("label"),
        "path": project.get("path"),
        "host_id": project.get("hostId"),
        "is_git_repository": project.get("isGitRepository"),
        "evidence_digest": _digest(
            json.dumps(project, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate checked-in workspace contract")

    plan = subparsers.add_parser("plan", help="render a trusted-ref rollout plan")
    plan.add_argument("--repository", type=Path, default=ROOT)
    plan.add_argument("--source-ref", default="origin/main")

    readback = subparsers.add_parser(
        "verify-project-readback", help="validate a Codex list_projects JSON readback"
    )
    readback.add_argument("--repository", type=Path, default=ROOT)
    readback.add_argument("--readback-json", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "validate":
        result = validate_checkout()
    elif args.command == "plan":
        result = build_plan(args.repository, args.source_ref)
    else:
        result = validate_project_readback(
            json.loads(args.readback_json.read_text(encoding="utf-8")),
            load_contract(),
            args.repository.resolve(),
        )
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
