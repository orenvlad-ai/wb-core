#!/usr/bin/env python3
"""Default-off one-shot production apply with exact owner and manifest binding."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_runner import (  # noqa: E402
    GitHubClient,
    RECEIPT_MARKER,
    canonical_json_bytes,
    exact_sha,
    is_actions_bot_comment,
    list_comments,
)
from apps.release_protocol import CANONICAL_REPOSITORY, validate_production_manifest  # noqa: E402


APPLY_RECEIPT_SCHEMA = "wb-core.production-apply-receipt/v2"
APPLY_MARKER = "wb-core-production-apply-receipt"
AUTH_RE = re.compile(
    r"^/wb-core apply-v2 pr (?P<pr>[1-9][0-9]*) merge (?P<merge>[0-9a-f]{40}) "
    r"deployed (?P<deployed>[0-9a-f]{40}) manifest sha256:(?P<manifest>[0-9a-f]{64}) "
    r"operation (?P<operation>[A-Za-z0-9._:-]{1,160})$"
)


class ApplyError(RuntimeError):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def marker(operation: str) -> str:
    return f"<!-- {APPLY_MARKER} operation={operation} -->"


def parse_release_receipt(
    comments: list[Mapping[str, Any]],
    *,
    merge_sha: str,
    manifest_sha: str,
    operation: str,
) -> dict[str, Any]:
    matches = []
    for comment in comments:
        body = str(comment.get("body") or "")
        if (
            f"<!-- {RECEIPT_MARKER} " not in body
            or "```json" not in body
            or not is_actions_bot_comment(comment)
        ):
            continue
        try:
            payload_text = body.split("```json", 1)[1].split("```", 1)[0]
            payload = json.loads(payload_text)
        except (IndexError, json.JSONDecodeError):
            continue
        manifest = payload.get("manifest")
        if (
            payload.get("state") == "awaiting_apply"
            and payload.get("operation_id") == operation
            and payload.get("merge_sha") == merge_sha
            and isinstance(manifest, Mapping)
            and manifest.get("sha256") == manifest_sha
            and manifest.get("operation_id") == operation
        ):
            matches.append(payload)
    if len(matches) != 1:
        raise ApplyError("awaiting-apply receipt is missing or ambiguous")
    return matches[0]


def validate_authorization(
    comment: Mapping[str, Any],
    *,
    pr: int,
    merge_sha: str,
    deployed_sha: str,
    manifest_sha: str,
    operation: str,
) -> None:
    association = str(comment.get("author_association") or "").upper()
    if association not in {"OWNER", "MEMBER"}:
        raise ApplyError("apply authorization association is not OWNER or MEMBER")
    match = AUTH_RE.fullmatch(str(comment.get("body") or "").strip())
    if match is None:
        raise ApplyError("apply authorization body is not exact protocol-v2 syntax")
    expected = {
        "pr": str(pr),
        "merge": merge_sha,
        "deployed": deployed_sha,
        "manifest": manifest_sha,
        "operation": operation,
    }
    if match.groupdict() != expected:
        raise ApplyError("apply authorization binding mismatch")


def command_evidence(command: list[str]) -> dict[str, Any]:
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    return {
        "command_sha256": digest(canonical_json_bytes(command)),
        "return_code": result.returncode,
        "stdout_sha256": digest(result.stdout.encode("utf-8")),
        "stderr_sha256": digest(result.stderr.encode("utf-8")),
    }


def run_commands(manifest: Mapping[str, Any]) -> dict[str, Any]:
    commands = manifest["commands"]
    dry_run = command_evidence(commands["dry_run"])
    if dry_run["return_code"] != 0:
        return {"state": "blocked", "apply_count": 0, "dry_run": dry_run}
    apply = command_evidence(commands["apply"])
    readback = command_evidence(commands["readback"])
    reconcile = command_evidence(commands["reconcile"])
    complete = all(item["return_code"] == 0 for item in (apply, readback, reconcile))
    return {
        "state": "done" if complete else "blocked",
        "apply_count": 1,
        "dry_run": dry_run,
        "apply": apply,
        "readback": readback,
        "reconcile": reconcile,
    }


def load_manifest(path: Path, expected_sha: str) -> dict[str, Any]:
    raw = path.read_bytes()
    if digest(raw) != expected_sha:
        raise ApplyError("manifest digest mismatch")
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict) or validate_production_manifest(manifest)["valid"] is not True:
        raise ApplyError("manifest contract invalid")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=CANONICAL_REPOSITORY)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--merge-sha", required=True)
    parser.add_argument("--deployed-sha", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--authorization-comment-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    merge_sha = exact_sha(args.merge_sha, "merge")
    deployed_sha = exact_sha(args.deployed_sha, "deployed")
    if deployed_sha != merge_sha:
        raise ApplyError("deployed SHA must equal exact merge SHA")
    if re.fullmatch(r"[0-9a-f]{64}", args.manifest_sha256) is None:
        raise ApplyError("manifest SHA-256 is invalid")
    client = GitHubClient(args.repository, token)
    pr = client.get(f"/pulls/{args.pr}")
    if pr.get("merged") is not True or exact_sha(pr.get("merge_commit_sha"), "pr-merge") != merge_sha:
        raise ApplyError("PR merge binding mismatch")
    comments = list_comments(client, args.pr)
    prior = [
        item
        for item in comments
        if marker(args.operation_id) in str(item.get("body") or "")
        and is_actions_bot_comment(item)
    ]
    if prior:
        if len(prior) != 1:
            raise ApplyError("duplicate or ambiguous durable apply receipt")
        receipt = {"schema": APPLY_RECEIPT_SCHEMA, "state": "already_terminal", "operation_id": args.operation_id}
        args.output.write_bytes(canonical_json_bytes(receipt) + b"\n")
        print(json.dumps(receipt, sort_keys=True))
        return 0
    release_receipt = parse_release_receipt(
        comments,
        merge_sha=merge_sha,
        manifest_sha=args.manifest_sha256,
        operation=args.operation_id,
    )
    authorization = client.get(f"/issues/comments/{args.authorization_comment_id}")
    validate_authorization(
        authorization,
        pr=args.pr,
        merge_sha=merge_sha,
        deployed_sha=deployed_sha,
        manifest_sha=args.manifest_sha256,
        operation=args.operation_id,
    )
    subprocess.run(["git", "fetch", "--no-tags", "origin", merge_sha], cwd=ROOT, check=True)
    subprocess.run(["git", "checkout", "--detach", merge_sha], cwd=ROOT, check=True)
    binding = release_receipt["manifest"]
    manifest_path = (ROOT / str(binding["path"])).resolve()
    if ROOT not in manifest_path.parents:
        raise ApplyError("manifest path escapes repository")
    manifest = load_manifest(manifest_path, args.manifest_sha256)
    if manifest.get("operation_id") != args.operation_id:
        raise ApplyError("manifest operation id mismatch")
    result = run_commands(manifest)
    receipt = {
        "schema": APPLY_RECEIPT_SCHEMA,
        "state": result["state"],
        "operation_id": args.operation_id,
        "repository": args.repository,
        "pull_request": args.pr,
        "merge_sha": merge_sha,
        "deployed_sha": deployed_sha,
        "manifest_sha256": args.manifest_sha256,
        "authorization_comment_id": args.authorization_comment_id,
        "authorization_body_sha256": digest(
            str(authorization.get("body") or "").strip().encode("utf-8")
        ),
        "apply_count": result["apply_count"],
        "evidence": result,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(receipt) + b"\n")
    body = marker(args.operation_id) + "\nProtocol-v2 one-shot production apply receipt:\n```json\n" + json.dumps(receipt, sort_keys=True, indent=2) + "\n```"
    client.post(f"/issues/{args.pr}/comments", {"body": body})
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
