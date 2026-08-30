#!/usr/bin/env python3
"""One-submit recovery for the exact WBC0010 interrupted provider boundary."""

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
HOSTED_RUNNER = ROOT / "apps" / "registry_upload_http_entrypoint_hosted_runtime.py"
INCIDENT_PROCESSING_KEY = "RClVU3wNw5XaLZ0xkoej|1|1.4.2"
INCIDENT_FEEDBACK_ID = "RClVU3wNw5XaLZ0xkoej"
INCIDENT_PROVIDER_STARTED_AT = "2026-08-27T15:16:46.773938Z"
MAXIMUM_UNCERTAINTY_USD = "0.10000000"


class Wbc0010AutoanswersRecoveryError(RuntimeError):
    pass


def _state_root() -> Path:
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp:
        raise Wbc0010AutoanswersRecoveryError(
            "RUNNER_TEMP is required for private operation evidence"
        )
    root = Path(runner_temp).resolve() / "wbc0010-autoanswers-budget-recovery"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def _write_private(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + f".tmp.{os.getpid()}")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Wbc0010AutoanswersRecoveryError(
            f"required private evidence is missing: {path.name}"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise Wbc0010AutoanswersRecoveryError(
            f"private evidence is not an object: {path.name}"
        )
    return payload


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _run_hosted(arguments: list[str], *, allow_failure: bool = False) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(HOSTED_RUNNER), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=900.0,
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
                "stdout_sha256": _digest(result.stdout),
                "stderr_sha256": _digest(result.stderr),
            }
        raise Wbc0010AutoanswersRecoveryError(
            "canonical hosted action failed: "
            f"return_code={result.returncode}; stdout={_digest(result.stdout)}; "
            f"stderr={_digest(result.stderr)}"
        )
    return payload


def _nested_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    if not isinstance(result, dict):
        raise Wbc0010AutoanswersRecoveryError(
            "canonical hosted result payload is missing"
        )
    return dict(result)


def _lifecycle_status() -> dict[str, Any]:
    result = _nested_result(_run_hosted(["autoanswers-lifecycle", "status"]))
    lifecycle = dict(result.get("lifecycle") or {})
    settings = dict(result.get("settings") or {})
    master = dict(result.get("master_policy") or {})
    if (
        lifecycle.get("business_mode") != "auto_all"
        or lifecycle.get("desired") is not True
        or lifecycle.get("suspended_by_master") is not False
        or settings.get("mode") != "auto_all"
        or settings.get("master_enabled") is not True
        or master.get("master_desired") is not True
    ):
        raise Wbc0010AutoanswersRecoveryError(
            "existing Autoanswers owner intent is not the accepted auto_all contour"
        )
    return {
        "business_mode": lifecycle.get("business_mode"),
        "desired": lifecycle.get("desired"),
        "actual": lifecycle.get("actual"),
        "lifecycle_state": lifecycle.get("lifecycle_state"),
        "drift_status": lifecycle.get("drift_status"),
        "stop_reason": lifecycle.get("stop_reason"),
        "policy_epoch": settings.get("policy_epoch"),
        "master_enabled": settings.get("master_enabled"),
        "master_desired": master.get("master_desired"),
        "transition_run_id": lifecycle.get("transition_run_id"),
    }


def _exact_plan() -> dict[str, Any]:
    plan = _nested_result(
        _run_hosted(["autoanswers-budget-reconciliation", "dry-run"])
    )
    holds = list(plan.get("holds") or [])
    expected = dict(plan.get("expected_affected_records") or {})
    invariants = dict(plan.get("non_target_invariants") or {})
    run_cap = dict(plan.get("active_run_cap") or {})
    if (
        plan.get("contract") != "wb_autoanswers_budget_reconciliation_v1"
        or int(plan.get("candidate_count") or 0) != 1
        or len(holds) != 1
        or str(dict(plan.get("runtime") or {}).get("stop_reason") or "")
        != "budget_state_unknown"
        or not str(plan.get("settings_revision") or "").startswith("sha256:")
        or not run_cap.get("transition_run_id")
        or (
            run_cap.get("run_max_usd") in {None, ""}
            and run_cap.get("run_max_paid_reviews") in {None, ""}
        )
    ):
        raise Wbc0010AutoanswersRecoveryError(
            "exact one unresolved budget_state_unknown boundary is not proven"
        )
    hold = dict(holds[0])
    if (
        hold.get("processing_key") != INCIDENT_PROCESSING_KEY
        or hold.get("feedback_id") != INCIDENT_FEEDBACK_ID
        or int(hold.get("content_version") or 0) != 1
        or hold.get("provider_call_started_at") != INCIDENT_PROVIDER_STARTED_AT
        or hold.get("reservation_status") != "released"
        or hold.get("released_reason") != "stale_or_orphaned"
        or float(hold.get("reservation_actual_cost_usd") or 0) != 0
        or hold.get("job_state") != "processing"
        or hold.get("last_error_code") is not None
        or hold.get("completed_at") is not None
        or not hold.get("lease_owner")
        or not hold.get("lease_until")
        or hold.get("recovery_action")
        != "append_hold_and_terminalize_interrupted_processing"
        or hold.get("upper_bound_usd") != MAXIMUM_UNCERTAINTY_USD
    ):
        raise Wbc0010AutoanswersRecoveryError(
            "the unresolved provider boundary differs from accepted incident evidence"
        )
    if expected != {
        "uncertainty_holds_inserted": 1,
        "processing_jobs_terminalized": 1,
        "audit_events_appended": 2,
        "runtime_state_rows_updated": 1,
        "provider_calls_created": 0,
        "cost_events_created": 0,
        "wb_writes_created": 0,
    } or not invariants or not all(invariants.values()):
        raise Wbc0010AutoanswersRecoveryError(
            "budget reconciliation affected-record or non-target contract changed"
        )
    return plan


def dry_run() -> dict[str, Any]:
    root = _state_root()
    plan = _exact_plan()
    lifecycle = _lifecycle_status()
    run_cap = dict(plan.get("active_run_cap") or {})
    if lifecycle.get("transition_run_id") != run_cap.get("transition_run_id"):
        raise Wbc0010AutoanswersRecoveryError(
            "owner lifecycle and immutable transition run identity differ"
        )
    evidence = {
        "contract": "wbc0010_autoanswers_budget_recovery_v1",
        "status": "ready",
        "query_only": True,
        "processing_key": INCIDENT_PROCESSING_KEY,
        "plan_fingerprint": plan["plan_fingerprint"],
        "pre_change_digest": plan["pre_change_digest"],
        "maximum_uncertainty_usd": MAXIMUM_UNCERTAINTY_USD,
        "expected_affected_records": plan["expected_affected_records"],
        "non_target_invariants": plan["non_target_invariants"],
        "settings_revision": plan["settings_revision"],
        "active_run_cap": run_cap,
        "lifecycle_before": lifecycle,
        "production_mutation_count": 0,
    }
    _write_private(root / "dry-run.json", evidence)
    return evidence


def _readback(*, reconciled: bool) -> dict[str, Any]:
    root = _state_root()
    preflight = _read_object(root / "dry-run.json")
    fingerprint = str(preflight.get("plan_fingerprint") or "")
    lifecycle_before = dict(preflight.get("lifecycle_before") or {})
    result = _nested_result(
        _run_hosted(["autoanswers-budget-reconciliation", "readback"])
    )
    status = dict(result.get("readback") or {})
    lifecycle = _lifecycle_status()
    interruptions = [
        dict(item)
        for item in list(status.get("terminalized_interruptions") or [])
        if isinstance(item, Mapping)
        and item.get("processing_key") == INCIDENT_PROCESSING_KEY
    ]
    if len(interruptions) != 1:
        raise Wbc0010AutoanswersRecoveryError(
            "exact interrupted processing job terminalization is not proven"
        )
    target = interruptions[0]
    details = dict(target.get("details") or {})
    if (
        result.get("status") != "confirmed"
        or status.get("confirmed") is not True
        or int(status.get("unresolved_count") or 0) != 0
        or status.get("stop_reason") == "budget_state_unknown"
        or target.get("job_state") != "terminal_error"
        or target.get("last_error_code")
        != "provider_boundary_interrupted_unknown_result"
        or int(target.get("attempts") or 0) != 1
        or target.get("lease_owner") is not None
        or target.get("lease_until") is not None
        or not target.get("completed_at")
        or float(target.get("job_actual_cost_usd") or 0) != 0
        or target.get("reservation_status") != "released"
        or float(target.get("reservation_actual_cost_usd") or 0) != 0
        or target.get("released_reason") != "stale_or_orphaned"
        or target.get("provider_call_started_at") != INCIDENT_PROVIDER_STARTED_AT
        or int(target.get("hold_count") or 0) != 1
        or float(target.get("hold_upper_bound_usd") or 0) != 0.1
        or int(target.get("cost_event_count") or 0) != 0
        or int(target.get("failed_cost_event_count") or 0) != 0
        or int(target.get("publication_job_count") or 0) != 0
        or details.get("plan_fingerprint") != fingerprint
        or details.get("provider_call_replayed") is not False
        or details.get("wb_post_created") is not False
        or details.get("actual_cost_asserted") is not False
        or lifecycle.get("business_mode") != "auto_all"
        or lifecycle.get("desired") is not True
        or lifecycle.get("drift_status") != "matched"
        or lifecycle.get("stop_reason") == "budget_state_unknown"
        or lifecycle.get("policy_epoch") != lifecycle_before.get("policy_epoch")
        or lifecycle.get("transition_run_id")
        != lifecycle_before.get("transition_run_id")
    ):
        raise Wbc0010AutoanswersRecoveryError(
            "query-only recovery readback does not prove the accepted outcome"
        )
    evidence = {
        "contract": "wbc0010_autoanswers_budget_recovery_v1",
        "status": "complete",
        "query_only": True,
        "reconciled": reconciled,
        "processing_key": INCIDENT_PROCESSING_KEY,
        "plan_fingerprint": fingerprint,
        "target": target,
        "budget": result.get("budget"),
        "lifecycle": lifecycle,
        "unresolved_count": 0,
        "provider_calls_created": 0,
        "wb_writes_created": 0,
        "production_mutation_count": 0,
    }
    _write_private(root / ("reconcile.json" if reconciled else "readback.json"), evidence)
    return evidence


def apply() -> dict[str, Any]:
    root = _state_root()
    preflight = _read_object(root / "dry-run.json")
    if preflight.get("status") != "ready":
        raise Wbc0010AutoanswersRecoveryError("recovery dry-run is not ready")
    state_path = root / "apply-state.json"
    if state_path.is_file():
        state = _read_object(state_path)
        if state.get("submit_attempted") is True:
            return _readback(reconciled=False)
    state = {
        "contract": "wbc0010_autoanswers_budget_recovery_v1",
        "processing_key": INCIDENT_PROCESSING_KEY,
        "plan_fingerprint": preflight["plan_fingerprint"],
        "submit_attempted": True,
        "phase": "submit_started",
    }
    _write_private(state_path, state)
    response = _run_hosted(
        [
            "autoanswers-budget-reconciliation",
            "apply",
            "--fingerprint",
            str(preflight["plan_fingerprint"]),
        ],
        allow_failure=True,
    )
    state["phase"] = "submit_finished_or_ambiguous"
    state["transport"] = {
        key: value
        for key, value in response.items()
        if key in {"status", "return_code", "stdout_sha256", "stderr_sha256"}
    }
    _write_private(state_path, state)
    readback = _readback(reconciled=False)
    result = {
        **readback,
        "query_only": False,
        "production_mutation_count": 1,
        "apply_count": 1,
        "submit_attempted": True,
    }
    _write_private(root / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("dry-run", "apply", "readback", "reconcile"))
    args = parser.parse_args()
    try:
        if args.action == "dry-run":
            result = dry_run()
        elif args.action == "apply":
            result = apply()
        else:
            result = _readback(reconciled=args.action == "reconcile")
    except Wbc0010AutoanswersRecoveryError as exc:
        print(
            json.dumps(
                {
                    "contract": "wbc0010_autoanswers_budget_recovery_v1",
                    "status": "blocked",
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
