#!/usr/bin/env python3
"""Registered, one-submit adapter for exact WB Autoanswers enqueue recovery."""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_autoanswers_backlog_recovery as domain
from apps.production_apply_contract import AmbiguousSubmit
from packages.application.wb_autoanswers_control_lock import autoanswers_control_lock

TARGET = "wb-core-eu-root:/opt/wb-core-runtime/state/wb_autoanswers_runtime.sqlite3"


def _claim_path(runtime_dir: Path, operation_id: str) -> Path:
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{7,127}", operation_id) is None:
        raise ValueError("invalid-operation-id")
    return runtime_dir / "wb_autoanswers_recovery_operations" / (operation_id + ".json")


def _readback(runtime_dir: Path, operation_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
    path = _claim_path(runtime_dir, operation_id)
    result: dict[str, Any] = {"operation_id": operation_id, "state": "not_submitted"}
    if not path.exists():
        return result
    claim = json.loads(path.read_text(encoding="utf-8"))
    if claim["request_sha256"] != domain._fingerprint(request):
        raise ValueError("operation-request-mismatch")
    # A retained submit marker is never permission to run apply again. The
    # domain's atomic applied record proves this enqueue, even if workers have
    # advanced those jobs or other WB reviews arrived since submission.
    with closing(domain._open(runtime_dir, read_only=True)) as conn:
        row = conn.execute("""SELECT state,manifest_sha256,expected_feedback_count,applied_at
            FROM sheet_vitrina_v1_wb_autoanswers_backlog_recovery_runs WHERE plan_fingerprint=?""",
            (claim["candidate_sha256"],)).fetchone()
    applied = bool(row and row["state"] == "applied" and row["manifest_sha256"] == claim["manifest_sha256"]
                   and int(row["expected_feedback_count"]) == claim["target_count"])
    return {**result, "state": "applied" if applied else "ambiguous",
            "candidate_sha256": claim["candidate_sha256"], "target_count": claim["target_count"],
            "proof": "atomic_domain_enqueue_record" if applied else "retained_submit_without_applied_record",
            "applied_at": row["applied_at"] if applied else None,
            "wb_post_count": 0, "provider_call_count": 0}


def execute(envelope: Mapping[str, Any], *, runtime_dir: Path, env_file: Path,
            source: Any = None) -> dict[str, Any]:
    allowed = {"action", "operation_id", "request", "expected_prestate", "expected_candidate", "expected_runtime_sha", "actor"}
    if not isinstance(envelope, Mapping) or set(envelope) - allowed:
        raise ValueError("invalid-envelope")
    action = str(envelope.get("action") or "")
    if action not in {"preview", "apply", "readback"}:
        raise ValueError("invalid-action")
    runtime_dir = Path(runtime_dir).resolve()
    operation_id = str(envelope.get("operation_id") or "")
    path = _claim_path(runtime_dir, operation_id)
    deployed = domain._deployed_runtime_evidence(str(envelope.get("expected_runtime_sha") or ""))
    request = envelope.get("request")
    if not isinstance(request, dict) or set(request) - {"manifest", "approval_reference", "recovery_reference", "capture_only"}:
        raise ValueError("invalid-request")
    if action == "readback":
        return _readback(runtime_dir, operation_id, request)
    if source is None:
        if runtime_dir != Path("/opt/wb-core-runtime/state"):
            raise ValueError("canonical-runtime-target-required")
        domain._load_safe_env_file(Path(env_file).resolve())
        if str(os.environ.get(domain.EXTERNAL_GATE_ENV) or "").lower() not in {"1", "true", "yes", "on"}:
            raise ValueError("external-io-gate-off")
        source = domain.RecoveryPacedReadPort(domain.HttpBackedWbAutoanswersReadAdapter())
    if request.get("capture_only"):
        if action != "preview" or set(request) != {"capture_only"}:
            raise ValueError("capture-is-preview-only")
        manifest = domain.capture_t0_manifest(source)
        digest = domain._fingerprint(manifest)
        return {"operation_id": operation_id, "target": TARGET,
                "scope": {"manifest": manifest, "capture_only": True},
                "prestate_sha256": digest, "candidate_sha256": digest,
                "recovery": {"kind": "read_only_capture"}, "deployed_runtime": deployed}
    manifest = domain.validate_manifest(request.get("manifest"))
    if not str(request.get("approval_reference") or "").strip():
        raise ValueError("human-approval-reference-required")
    recovery_reference = Path(str(request.get("recovery_reference") or ""))
    if not recovery_reference.is_absolute() or not recovery_reference.is_file() or recovery_reference.stat().st_size == 0:
        raise ValueError("existing-recovery-backup-required")
    def preview_plan():
        remote, details = domain.fetch_remote_evidence(source, manifest)
        with closing(domain._open(runtime_dir, read_only=True)) as conn:
            plan = domain.build_plan(conn, runtime_dir=runtime_dir, manifest=manifest, remote=remote)
        if not plan["coverage_confirmed"]:
            raise ValueError("recovery-preconditions-failed")
        preview = {"operation_id": operation_id, "target": TARGET,
                   "scope": {"manifest_sha256": manifest["manifest_sha256"], "target_count": len(manifest["items"]),
                             "actions": plan["target_actions"], "action_counts": plan["action_counts"]},
                   "prestate_sha256": plan["pre_change_digest"], "candidate_sha256": plan["plan_fingerprint"],
                   "recovery": {"kind": "sqlite_backup_and_retained_job_history", "reference": str(recovery_reference)},
                   "deployed_runtime": deployed}
        return preview, remote, details
    if action == "preview":
        return preview_plan()[0]
    with autoanswers_control_lock(runtime_dir):
        if path.exists():
            readback = _readback(runtime_dir, operation_id, request)
            if readback["state"] == "applied":
                return {"operation_id": operation_id, "disposition": "already_applied"}
            raise AmbiguousSubmit("retained-submit-readback-only")
        preview, remote, details = preview_plan()
        if preview["prestate_sha256"] != envelope.get("expected_prestate") or preview["candidate_sha256"] != envelope.get("expected_candidate"):
            raise ValueError("candidate-or-prestate-drift")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        claim = {"operation_id": operation_id, "request_sha256": domain._fingerprint(request),
                 "target": TARGET, "deployed_runtime": deployed, "manifest_sha256": manifest["manifest_sha256"],
                 "candidate_sha256": preview["candidate_sha256"], "target_count": len(manifest["items"]),
                 "recovery_reference": str(recovery_reference)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(domain._canonical(claim)); handle.flush(); os.fsync(handle.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        try:
            domain.apply_plan(runtime_dir, manifest=manifest, remote=remote, details=details,
                              expected_fingerprint=preview["candidate_sha256"], actor=str(envelope.get("actor") or "github-production-apply"),
                              approval_reference=str(request["approval_reference"]))
        except Exception as exc:
            raise AmbiguousSubmit("recovery-submit-readback-only") from exc
        return {"operation_id": operation_id, "disposition": "submitted", "wb_post_count": 0, "provider_call_count": 0}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = execute(json.load(sys.stdin), runtime_dir=args.runtime_dir, env_file=args.env_file)
    except Exception as exc:
        print(json.dumps({"status": "ambiguous" if isinstance(exc, AmbiguousSubmit) else "blocked",
                          "error": {"code": type(exc).__name__, "message": str(exc)[:500]}}))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
