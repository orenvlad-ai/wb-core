#!/usr/bin/env python3
"""One-submit Release/Apply orchestration for WBC0027 recovery."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
HOSTED = ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py"
TARGET = ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "hosted_runtime_target__europe_api.json"
MANIFEST = ROOT / "release" / "production-mutations" / "wbc0027_capital_recovery.json"


class Wbc0027ReleaseError(RuntimeError):
    pass


def _root() -> Path:
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp:
        raise Wbc0027ReleaseError("RUNNER_TEMP is required for private reviewed evidence")
    path = Path(runner_temp).resolve() / "wbc0027-capital-recovery"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise Wbc0027ReleaseError(f"private evidence is not an object: {path.name}")
    return payload


def _run(arguments: list[str], *, allow_failure: bool = False) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(HOSTED), "--target-file", str(TARGET), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = None
    if result.returncode != 0 or not isinstance(payload, dict):
        if allow_failure:
            return {
                "status": "transport_ambiguous",
                "return_code": result.returncode,
                "stdout_sha256": _text_digest(result.stdout),
                "stderr_sha256": _text_digest(result.stderr),
            }
        raise Wbc0027ReleaseError(
            f"canonical hosted action failed: return_code={result.returncode}; "
            f"stdout={_text_digest(result.stdout)}; stderr={_text_digest(result.stderr)}"
        )
    nested = payload.get("result")
    if not isinstance(nested, dict):
        raise Wbc0027ReleaseError("canonical hosted result payload is missing")
    return dict(nested)


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _payload_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_digest(encoded)


def _validate(plan: Mapping[str, Any]) -> None:
    product = dict((plan.get("product_capital") or {}).get("counts") or {})
    economics = dict(plan.get("functional_economics") or {})
    if not (
        plan.get("contract_name") == "wbc0027_capital_recovery_v1"
        and plan.get("status") == "ready"
        and plan.get("production_mutation_count") == 0
        and product.get("primary_row_count") == 936
        and product.get("primary_cell_count") == 19656
        and product.get("primary_mismatch_count") == 7655
        and product.get("event_path_mismatch_count") == 7639
        and product.get("separate_20260821_mismatch_count") == 16
        and product.get("secondary_mismatch_count") == 1791
        and economics.get("logical_repair_count") == 298
        and economics.get("persisted_repair_count") == 472
        and len(economics.get("evidence_blocked") or []) == 12
        and str(plan.get("product_operation_id") or "").startswith("recovery_")
        and str(plan.get("economics_operation_id") or "").startswith("recovery_")
        and plan.get("product_operation_id") != plan.get("economics_operation_id")
        and isinstance(plan.get("storage_generation"), Mapping)
        and str((plan.get("storage_generation") or {}).get("generation_id") or "")
        and str((plan.get("storage_generation") or {}).get("manifest_sha256") or "").startswith("sha256:")
    ):
        raise Wbc0027ReleaseError("query-only qualification differs from exact accepted scope")
    manifest = _read(MANIFEST)
    if manifest.get("operation_id") != "wbc0027-product-capital-and-qualified-economics-v2":
        raise Wbc0027ReleaseError("WBC0027 manifest operation is not the fresh v2 generation")
    pre_change_digest = _payload_digest(
        {
            "economics_before_digest": plan["functional_economics"]["before_digest"],
            "product_before_target_digest": plan["product_capital"]["before_target_digest"],
        }
    )
    if manifest.get("pre_change_digest_value") != pre_change_digest:
        raise Wbc0027ReleaseError("manifest pre-change digest differs from live exact target")


def dry_run() -> dict[str, Any]:
    root = _root()
    plan_path = root / "reviewed-plan.json"
    plan = _run(["wbc0027-capital-recovery-plan", "--output", str(plan_path)])
    persisted = _read(plan_path)
    if persisted != plan:
        raise Wbc0027ReleaseError("persisted reviewed plan differs from hosted result")
    _validate(plan)
    state = {
        "status": "ready",
        "deployed_sha": plan["deployed_sha"],
        "storage_generation": plan["storage_generation"],
        "manifest_operation_id": _read(MANIFEST)["operation_id"],
        "manifest_sha256": _file_digest(MANIFEST),
        "plan_path": str(plan_path),
        "plan_fingerprint": plan["plan_fingerprint"],
        "product_operation_id": plan["product_operation_id"],
        "economics_operation_id": plan["economics_operation_id"],
        "product_counts": plan["product_capital"]["counts"],
        "economics_counts": {
            "logical_repair_count": plan["functional_economics"]["logical_repair_count"],
            "persisted_repair_count": plan["functional_economics"]["persisted_repair_count"],
            "evidence_blocked_count": len(plan["functional_economics"]["evidence_blocked"]),
        },
        "production_mutation_count": 0,
        "pre_change_digest": _payload_digest(
            {
                "economics_before_digest": plan["functional_economics"]["before_digest"],
                "product_before_target_digest": plan["product_capital"]["before_target_digest"],
            }
        ),
        "product_non_target_digest": plan["product_capital"]["non_target_digest"],
        "economics_non_target_digest": plan["functional_economics"]["non_target_digest"],
        "ready_snapshot_digest": plan["ready_snapshot_digest"],
        "outbox_digest": plan["outbox_digest"],
        "product_submit_attempted": False,
        "economics_submit_attempted": False,
    }
    _write(root / "state.json", state)
    return state


def _query_only_readback(root: Path, *, require_final: bool) -> dict[str, Any]:
    state = _read(root / "state.json")
    result = _run(
        [
            "wbc0027-capital-recovery-readback",
            "--plan-file",
            str(state["plan_path"]),
        ]
    )
    if result.get("query_only") is not True:
        raise Wbc0027ReleaseError("WBC0027 readback is not query-only")
    if require_final and result.get("status") != "reconciled":
        raise Wbc0027ReleaseError("WBC0027 query-only readback is not exact")
    _write(root / "readback.json", result)
    return result


def apply() -> dict[str, Any]:
    root = _root()
    state_path = root / "state.json"
    state = _read(state_path)
    approval = "WBC0027 exact-manifest OWNER authorization validated by Production Apply Runner"
    if state.get("product_submit_attempted") is not True:
        state["product_submit_attempted"] = True
        state["phase"] = "product_exact_submit_started"
        _write(state_path, state)
        result = _run(
            [
                "wbc0027-capital-recovery-apply",
                "--plan-file",
                str(state["plan_path"]),
                "--fingerprint",
                str(state["plan_fingerprint"]),
                "--approval-reference",
                approval,
                "--phase",
                "product",
            ],
            allow_failure=True,
        )
        state["product_transport"] = {
            key: value
            for key, value in result.items()
            if key in {"status", "return_code", "stdout_sha256", "stderr_sha256"}
        }
        _write(state_path, state)
    product_readback = _query_only_readback(root, require_final=False)
    if (
        product_readback.get("product_exact") is not True
        or product_readback.get("product_recovery_lifecycle") != "retained"
    ):
        raise Wbc0027ReleaseError(
            "product recovery submit is not exactly reconciled; blind retry is prohibited"
        )
    if state.get("economics_submit_attempted") is not True:
        state["economics_submit_attempted"] = True
        state["phase"] = "economics_exact_submit_started"
        _write(state_path, state)
        result = _run(
            [
                "wbc0027-capital-recovery-apply",
                "--plan-file",
                str(state["plan_path"]),
                "--fingerprint",
                str(state["plan_fingerprint"]),
                "--approval-reference",
                approval,
                "--phase",
                "economics",
            ],
            allow_failure=True,
        )
        state["economics_transport"] = {
            key: value
            for key, value in result.items()
            if key in {"status", "return_code", "stdout_sha256", "stderr_sha256"}
        }
        _write(state_path, state)
    # Each phase crosses its submit boundary at most once. Any transport
    # ambiguity converges only through the two durable recovery identities.
    return _query_only_readback(root, require_final=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dry-run", "apply", "readback", "reconcile"))
    args = parser.parse_args()
    try:
        if args.command == "dry-run":
            result = dry_run()
        elif args.command == "apply":
            result = apply()
        else:
            result = _query_only_readback(_root(), require_final=True)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
