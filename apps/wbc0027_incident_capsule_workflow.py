#!/usr/bin/env python3
"""Trusted GitHub-side bindings for the WBC0027 incident capsule workflow."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.github_release_runner import GitHubClient  # noqa: E402
from apps import registry_upload_http_entrypoint_hosted_runtime as hosted_runtime  # noqa: E402
from apps.production_apply_runner import (  # noqa: E402
    ApplyError,
    collect_exact_release_binding,
)
from apps import wbc0027_incident_recovery_capsule as capsule_module  # noqa: E402
from packages.application.fbs_lifecycle_manifests import (  # noqa: E402
    canonical_bytes,
    digest,
    read_json,
)


SSH_HOST_ALIAS = "wbc0027-capsule-canonical"


def materialize_ssh_transport(
    *,
    target_file: Path,
    output_directory: Path,
    private_key: str,
    known_hosts: str,
) -> dict[str, Any]:
    """Resolve the trusted target before materializing an isolated SSH contour."""

    target_path = Path(target_file).expanduser().resolve()
    if not target_path.is_file():
        raise ApplyError(f"capsule canonical target file is missing: {target_path}")
    try:
        target = hosted_runtime.load_hosted_runtime_target(target_path)
        hosted_runtime._validate_production_target_identity(
            target,
            action="WBC0027 incident capsule transport",
        )
        hosted_runtime._ensure_active_hosted_runtime_target(
            target,
            action="WBC0027 incident capsule transport",
        )
    except (OSError, ValueError) as exc:
        raise ApplyError(f"capsule canonical target is invalid: {exc}") from exc
    host_name = str(target.host_ip or "").strip()
    if not host_name:
        raise ApplyError("capsule canonical target host_ip is missing")
    if host_name not in hosted_runtime.ACTIVE_HOSTED_RUNTIME_PUBLIC_HOSTS:
        raise ApplyError(f"capsule canonical target host_ip is foreign: {host_name}")
    if target.target_id != capsule_module.CANONICAL_TARGET_ID:
        raise ApplyError("capsule canonical target_id is foreign")
    if target.ssh_destination != hosted_runtime.ACTIVE_HOSTED_RUNTIME_SSH_DESTINATION:
        raise ApplyError("capsule canonical ssh_destination is foreign")
    key = str(private_key or "")
    hosts = str(known_hosts or "")
    if not key.strip() or not hosts.strip():
        raise ApplyError("capsule hosted SSH credentials are missing")

    directory = Path(output_directory).expanduser()
    if directory.is_symlink():
        raise ApplyError("capsule SSH directory cannot be a symlink")
    directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    directory = directory.resolve()
    os.chmod(directory, 0o700)
    identity_path = directory / "identity"
    known_hosts_path = directory / "known_hosts"
    config_path = directory / "config"
    _write_secret(identity_path, key)
    _write_secret(known_hosts_path, hosts)
    config = "\n".join(
        (
            f"Host {SSH_HOST_ALIAS}",
            f"    HostName {host_name}",
            "    User root",
            f'    IdentityFile "{_ssh_config_path(identity_path)}"',
            "    IdentitiesOnly yes",
            "    BatchMode yes",
            "    StrictHostKeyChecking yes",
            f'    UserKnownHostsFile "{_ssh_config_path(known_hosts_path)}"',
            "    GlobalKnownHostsFile /dev/null",
            f"    HostKeyAlias {host_name}",
            "    CheckHostIP yes",
            "",
        )
    )
    _write_secret(config_path, config)
    return {
        "contract": "wbc0027_incident_capsule_ssh_binding/v1",
        "target_id": target.target_id,
        "target_file": str(target_path),
        "target_file_sha256": "sha256:" + hashlib.sha256(target_path.read_bytes()).hexdigest(),
        "source_ssh_destination": target.ssh_destination,
        "ssh_host_alias": SSH_HOST_ALIAS,
        "host_name": host_name,
        "user": "root",
        "host_key_alias": host_name,
        "ssh_config": str(config_path),
        "identity_file": str(identity_path),
        "known_hosts_file": str(known_hosts_path),
    }


def _ssh_config_path(path: Path) -> str:
    value = str(Path(path).resolve())
    if any(character in value for character in ('\n', '\r', '"')):
        raise ApplyError("capsule SSH path cannot be represented safely")
    return value.replace("\\", "\\\\")


def _write_secret(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def collect_release(*, repository: str, pr: int, operation: str) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ApplyError("GITHUB_TOKEN is required")
    client = GitHubClient(repository, token)
    exact = collect_exact_release_binding(
        client,
        pr=int(pr),
        release_operation=str(operation),
        expected_kind="live_runtime",
        expected_state="done",
        expected_manifest=None,
    )
    receipt = dict(exact["receipt"])
    return {
        "contract": capsule_module.RELEASE_BINDING_CONTRACT,
        "repository": repository,
        "pull_request": int(pr),
        "release_operation_id": str(operation),
        "release_kind": "live_runtime",
        "state": "done",
        "base_sha": str(receipt["base_sha"]),
        "head_sha": str(receipt["head_sha"]),
        "merge_sha": str(receipt["merge_sha"]),
        "deployed_sha": str(receipt["deployed_sha"]),
        "plan_hash": str(receipt["plan_hash"]),
        "gate_workflow_run_id": int(exact["gate_run_id"]),
        "release_receipt_digest": str(exact["artifact_file_sha256"]),
    }


def validate_authorization(
    *,
    repository: str,
    comment_id: int,
    release_binding: Mapping[str, Any],
    manifest: Mapping[str, Any],
    qualification: Mapping[str, Any],
) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise ApplyError("GITHUB_TOKEN is required")
    client = GitHubClient(repository, token)
    comment = client.get(f"/issues/comments/{int(comment_id)}")
    if (
        not isinstance(comment, Mapping)
        or int(comment.get("id") or 0) != int(comment_id)
        or str(comment.get("author_association") or "") not in {"OWNER", "MEMBER"}
    ):
        raise ApplyError("capsule authorization comment is not OWNER/MEMBER")
    reviewed = capsule_module._parse_capsule_manifest(
        manifest,
        deployed_sha=str(release_binding["deployed_sha"]),
    )
    qualified = capsule_module._parse_qualification(
        qualification,
        manifest=reviewed,
    )
    expected = capsule_module._authorization_body(
        release=release_binding,
        operation_id=str(reviewed["operation_id"]),
        manifest_digest=str(reviewed["manifest_digest"]),
        qualification_digest=str(qualified["qualification_digest"]),
    )
    body = str(comment.get("body") or "").strip()
    if body != expected:
        raise ApplyError("capsule authorization body is not exact")
    issue_url = str(comment.get("issue_url") or "")
    expected_issue = f"/repos/{repository}/issues/{int(release_binding['pull_request'])}"
    if not issue_url.endswith(expected_issue):
        raise ApplyError("capsule authorization belongs to another PR")
    return {
        "comment_id": int(comment_id),
        "author_association": str(comment["author_association"]),
        "body": body,
        "body_digest": digest(body),
        "operation_id": str(reviewed["operation_id"]),
        "manifest_digest": str(reviewed["manifest_digest"]),
        "qualification_digest": str(qualified["qualification_digest"]),
    }


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_bytes(payload) + b"\n"
    with output.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _github_outputs(path: str, payload: Mapping[str, Any]) -> None:
    if not path:
        return
    output = Path(path)
    with output.open("a", encoding="utf-8") as handle:
        for key, value in payload.items():
            text = str(value)
            if "\n" in text or "\r" in text:
                raise ApplyError("multiline GitHub output is forbidden")
            handle.write(f"{key}={text}\n")


def run(args: argparse.Namespace) -> int:
    if args.command == "materialize-ssh":
        payload = materialize_ssh_transport(
            target_file=Path(args.target_file),
            output_directory=Path(args.output_directory),
            private_key=os.environ.get("WB_CORE_DEPLOY_SSH_KEY", ""),
            known_hosts=os.environ.get("WB_CORE_DEPLOY_KNOWN_HOSTS", ""),
        )
        _write(Path(args.output), payload)
        _github_outputs(
            str(args.github_output or ""),
            {
                "ssh_config": payload["ssh_config"],
                "ssh_host_alias": payload["ssh_host_alias"],
                "host_name": payload["host_name"],
                "target_file_sha256": payload["target_file_sha256"],
            },
        )
    elif args.command == "release-binding":
        payload = collect_release(
            repository=str(args.repository),
            pr=int(args.pr),
            operation=str(args.release_operation_id),
        )
        _write(Path(args.output), payload)
        _github_outputs(
            str(args.github_output or ""),
            {
                "deployed_sha": payload["deployed_sha"],
                "operation_id": (
                    "wbc0027-incident-capsule-"
                    + str(payload["deployed_sha"])[:16]
                    + "-run-"
                    + str(args.workflow_run_id)
                ),
                "release_receipt_digest": payload["release_receipt_digest"],
            },
        )
    else:
        release = read_json(Path(args.release_binding_file))
        manifest = read_json(Path(args.manifest_file))
        qualification = read_json(Path(args.qualification_file))
        payload = validate_authorization(
            repository=str(args.repository),
            comment_id=int(args.authorization_comment_id),
            release_binding=release,
            manifest=manifest,
            qualification=qualification,
        )
        _write(Path(args.output), payload)
        _github_outputs(
            str(args.github_output or ""),
            {
                "operation_id": payload["operation_id"],
                "manifest_digest": payload["manifest_digest"],
                "qualification_digest": payload["qualification_digest"],
            },
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    transport = sub.add_parser("materialize-ssh")
    transport.add_argument("--target-file", required=True)
    transport.add_argument("--output-directory", required=True)
    transport.add_argument("--output", required=True)
    transport.add_argument("--github-output", default="")
    release = sub.add_parser("release-binding")
    release.add_argument("--repository", default="orenvlad-ai/wb-core")
    release.add_argument("--pr", required=True, type=int)
    release.add_argument("--release-operation-id", required=True)
    release.add_argument("--workflow-run-id", required=True, type=int)
    release.add_argument("--output", required=True)
    release.add_argument("--github-output", default="")
    apply = sub.add_parser("validate-authorization")
    apply.add_argument("--repository", default="orenvlad-ai/wb-core")
    apply.add_argument("--authorization-comment-id", required=True, type=int)
    apply.add_argument("--release-binding-file", required=True)
    apply.add_argument("--manifest-file", required=True)
    apply.add_argument("--qualification-file", required=True)
    apply.add_argument("--output", required=True)
    apply.add_argument("--github-output", default="")
    return parser


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
