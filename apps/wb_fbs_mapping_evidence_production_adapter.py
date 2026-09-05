"""Trusted SSH adapter for append-only FBS mapping evidence versions."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
from typing import Any, Mapping

from apps.github_release_runner import RunnerError, configure_ssh, trusted_main_sha
from apps.production_apply_contract import AdapterError, AmbiguousSubmit


ROOT = Path(__file__).resolve().parents[1]
TARGET_PATH = (
    ROOT
    / "artifacts/registry_upload_http_entrypoint/input/hosted_runtime_target__europe_api.json"
)
REMOTE_APP = "/opt/wb-core-runtime/app/apps/wb_fbs_mapping_evidence.py"
REMOTE_RUNTIME_DIR = "/opt/wb-core-runtime/state"
REMOTE_ENV_FILE = "/opt/wb-ai/.env"


class WbFbsMappingEvidenceProductionAdapter:
    def preview(
        self, request: dict[str, Any], operation_id: str
    ) -> dict[str, Any]:
        return self._invoke(
            action="preview", request=request, operation_id=operation_id
        )

    def apply(
        self,
        request: dict[str, Any],
        operation_id: str,
        preview: dict[str, Any],
    ) -> dict[str, Any]:
        return self._invoke(
            action="apply",
            request=request,
            operation_id=operation_id,
            expected_prestate=str(preview.get("prestate_sha256") or ""),
            expected_candidate=str(preview.get("candidate_sha256") or ""),
        )

    def readback(
        self, request: dict[str, Any], operation_id: str
    ) -> dict[str, Any]:
        return self._invoke(
            action="readback", request=request, operation_id=operation_id
        )

    def _invoke(
        self,
        *,
        action: str,
        request: Mapping[str, Any],
        operation_id: str,
        expected_prestate: str = "",
        expected_candidate: str = "",
    ) -> dict[str, Any]:
        target = json.loads(TARGET_PATH.read_text(encoding="utf-8"))
        destination = str(target.get("ssh_destination") or "").strip()
        if (
            target.get("target_status") != "active"
            or target.get("target_role") != "primary_live"
            or target.get("target_lifecycle") != "current_live"
            or destination != "wb-core-eu-root"
        ):
            raise AdapterError("production-target-identity-invalid")
        envelope = {
            "action": action,
            "operation_id": operation_id,
            "request": dict(request),
            "expected_prestate": expected_prestate,
            "expected_candidate": expected_candidate,
            "expected_runtime_sha": trusted_main_sha(),
            "actor": "github-actions:" + str(os.environ.get("GITHUB_ACTOR") or "unknown")[:120],
        }
        with tempfile.TemporaryDirectory(prefix="wb-fbs-mapping-evidence-") as raw:
            try:
                configure_ssh(Path(raw))
            except RunnerError as exc:
                raise AdapterError(exc.reason) from exc
            identity = os.environ.get(
                "WB_CORE_HOSTED_RUNTIME_SSH_IDENTITY_FILE", ""
            ).strip()
            options = os.environ.get(
                "WB_CORE_HOSTED_RUNTIME_SSH_OPTIONS", ""
            ).strip()
            command = [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "ServerAliveInterval=10",
                "-o",
                "ServerAliveCountMax=2",
                "-i",
                identity,
                *shlex.split(options),
                destination,
                "python3",
                REMOTE_APP,
                "--runtime-dir",
                REMOTE_RUNTIME_DIR,
                "--env-file",
                REMOTE_ENV_FILE,
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=json.dumps(
                        envelope,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    text=True,
                    capture_output=True,
                    timeout=150,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                if action == "apply":
                    raise AmbiguousSubmit("mapping-evidence-submit-timeout") from exc
                raise AdapterError("mapping-evidence-transport-timeout") from exc
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            if action == "apply":
                raise AmbiguousSubmit("mapping-evidence-submit-output-ambiguous") from exc
            raise AdapterError("mapping-evidence-response-invalid") from exc
        if not isinstance(payload, dict):
            if action == "apply":
                raise AmbiguousSubmit("mapping-evidence-submit-output-ambiguous")
            raise AdapterError("mapping-evidence-response-invalid")
        if completed.returncode != 0 or payload.get("status") == "blocked":
            detail = payload.get("error") if isinstance(payload, Mapping) else None
            code = (
                str(detail.get("code") or "")
                if isinstance(detail, Mapping)
                else ""
            )
            if action == "apply" and payload.get("status") != "blocked":
                raise AmbiguousSubmit("mapping-evidence-submit-transport-ambiguous")
            raise AdapterError(code or "mapping-evidence-remote-blocked")
        return payload
