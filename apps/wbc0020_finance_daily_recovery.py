#!/usr/bin/env python3
"""One-submit orchestration for WBC0020 exact daily Finance recovery."""

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
PARITY_DATES = ("2026-08-24", "2026-08-25")
RECOVERY_DATES = ("2026-08-26", "2026-08-27")
EXPECTED_CELLS = 171


class Wbc0020Error(RuntimeError):
    pass


def _state_root() -> Path:
    runner_temp = str(os.environ.get("RUNNER_TEMP") or "").strip()
    if not runner_temp:
        raise Wbc0020Error("RUNNER_TEMP is required for private operation evidence")
    root = Path(runner_temp).resolve() / "wbc0020-finance-daily"
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
        raise Wbc0020Error(f"required private evidence is missing: {path.name}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise Wbc0020Error(f"private evidence is not an object: {path.name}")
    return payload


def _run_hosted(arguments: list[str], *, allow_failure: bool = False) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(HOSTED_RUNNER), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=1800.0,
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
        raise Wbc0020Error(
            "canonical hosted action failed: "
            f"return_code={result.returncode}; stdout={_text_digest(result.stdout)}; "
            f"stderr={_text_digest(result.stderr)}"
        )
    return payload


def _text_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _nested_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("result")
    if not isinstance(value, dict):
        raise Wbc0020Error("canonical hosted result payload is missing")
    return dict(value)


def _parity_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(plan.get("source") or {})
    return {
        "target_date": plan.get("target_date"),
        "status": plan.get("parity_status"),
        "changed_cells": int(plan.get("changed_cells") or 0),
        "target_cells": int(plan.get("expected_target_cells") or 0),
        "source_digest": source.get("source_digest"),
        "pages": source.get("pages"),
        "terminal_cursor": source.get("terminal_cursor"),
        "coverage": source.get("coverage"),
        "ready_plan_digest": plan.get("before_plan_digest"),
    }


def dry_run() -> dict[str, Any]:
    root = _state_root()
    summaries: list[dict[str, Any]] = []
    deployed_sha = ""
    for target_date in PARITY_DATES:
        payload = _run_hosted(["finance-daily-parity", "--target-date", target_date])
        plan = _nested_result(payload)
        summary = _parity_summary(plan)
        summaries.append(summary)
        deployed_sha = str(plan.get("deployed_sha") or deployed_sha)
    ready = all(
        item["status"] == "exact"
        and item["changed_cells"] == 0
        and item["target_cells"] == EXPECTED_CELLS
        and item["coverage"] == "33/33"
        for item in summaries
    )
    evidence = {
        "contract_name": "wbc0020_finance_daily_outer_operation_v1",
        "status": "ready" if ready else "blocked",
        "deployed_sha": deployed_sha,
        "parity": summaries,
        "recovery_dates": list(RECOVERY_DATES),
        "expected_cells_per_date": EXPECTED_CELLS,
        "production_mutation_count": 0,
        "private_evidence_root": str(root),
    }
    _write_private(root / "dry-run.json", evidence)
    if not ready:
        raise Wbc0020Error("24-25 exact 171-cell Finance parity is blocked")
    return evidence


def _plan_path(root: Path, target_date: str) -> Path:
    return root / f"plan-{target_date}.json"


def _date_state_path(root: Path, target_date: str) -> Path:
    return root / f"state-{target_date}.json"


def _readback_date(root: Path, target_date: str) -> dict[str, Any]:
    state = _read_object(_date_state_path(root, target_date))
    operation_id = str(state.get("operation_id") or "")
    if not operation_id:
        raise Wbc0020Error(f"{target_date} operation identity is missing")
    payload = _run_hosted(
        ["finance-daily-recovery-readback", "--operation-id", operation_id]
    )
    result = _nested_result(payload)
    if (
        result.get("status") != "complete"
        or result.get("target_date") != target_date
        or result.get("accepted_cells") != "171/171"
        or result.get("coverage") != "33/33"
        or result.get("query_only") is not True
        or not all(dict(result.get("checks") or {}).values())
    ):
        raise Wbc0020Error(f"{target_date} query-only readback is incomplete")
    updated = {**state, "phase": "complete", "readback": result}
    _write_private(_date_state_path(root, target_date), updated)
    return result


def _apply_date(root: Path, *, target_date: str, deployed_sha: str) -> dict[str, Any]:
    state_path = _date_state_path(root, target_date)
    if state_path.is_file():
        state = _read_object(state_path)
        if state.get("submit_attempted") is True:
            return _readback_date(root, target_date)

    plan_path = _plan_path(root, target_date)
    plan_payload = _run_hosted(
        [
            "finance-daily-recovery-plan",
            "--target-date",
            target_date,
            "--output",
            str(plan_path),
        ]
    )
    plan = _read_object(plan_path)
    source = dict(plan.get("source") or {})
    if (
        plan.get("contract_name") != "finance_daily_historical_recovery"
        or plan.get("mode") != "recovery"
        or plan.get("target_date") != target_date
        or plan.get("deployed_sha") != deployed_sha
        or plan.get("apply_allowed") is not True
        or int(plan.get("expected_target_cells") or 0) != EXPECTED_CELLS
        or source.get("coverage") != "33/33"
        or int(source.get("terminal_status") or 0) != 204
        or source.get("complete") is not True
    ):
        raise Wbc0020Error(f"{target_date} recovery plan is not exact/applyable")
    state = {
        "target_date": target_date,
        "phase": "planned",
        "plan_path": str(plan_path),
        "plan_fingerprint": plan.get("fingerprint"),
        "operation_id": plan.get("operation_id"),
        "source_digest": source.get("source_digest"),
        "pages": source.get("pages"),
        "terminal_cursor": source.get("terminal_cursor"),
        "before_plan_digest": plan.get("before_plan_digest"),
        "after_plan_digest": plan.get("after_plan_digest"),
        "non_target_digest": plan.get("non_target_digest"),
        "changed_cells": plan.get("changed_cells"),
        "submit_attempted": False,
    }
    _write_private(state_path, state)
    state["submit_attempted"] = True
    state["phase"] = "submit_started"
    _write_private(state_path, state)
    apply_payload = _run_hosted(
        [
            "finance-daily-recovery-apply",
            "--target-date",
            target_date,
            "--plan-file",
            str(plan_path),
            "--fingerprint",
            str(plan.get("fingerprint") or ""),
            "--approval-reference",
            "WBC0020 owner accepted exact Finance recovery 2026-08-26 and 2026-08-27",
            "--actor",
            "production-apply-runner",
        ],
        allow_failure=True,
    )
    state["apply_transport"] = {
        key: value
        for key, value in apply_payload.items()
        if key in {"status", "return_code", "stdout_sha256", "stderr_sha256"}
    }
    state["phase"] = "submit_finished_or_ambiguous"
    _write_private(state_path, state)
    # Never submit again: both a normal response and ambiguous transport end at
    # the exact same query-only operation readback.
    return _readback_date(root, target_date)


def apply() -> dict[str, Any]:
    root = _state_root()
    preflight = _read_object(root / "dry-run.json")
    if preflight.get("status") != "ready":
        raise Wbc0020Error("WBC0020 parity preflight is not ready")
    deployed_sha = str(preflight.get("deployed_sha") or "")
    results = [
        _apply_date(root, target_date=target_date, deployed_sha=deployed_sha)
        for target_date in RECOVERY_DATES
    ]
    final = {
        "contract_name": "wbc0020_finance_daily_outer_operation_v1",
        "status": "complete",
        "deployed_sha": deployed_sha,
        "dates": results,
        "operation_count": 2,
        "apply_count": 2,
        "accepted_cells": "342/342",
        "non_target_invariant": "unchanged",
        "producer_state": "not_paused; shared rate gate and ready-plan CAS used",
    }
    _write_private(root / "result.json", final)
    return final


def readback(*, reconcile: bool) -> dict[str, Any]:
    root = _state_root()
    results = [_readback_date(root, target_date) for target_date in RECOVERY_DATES]
    complete = all(item.get("status") == "complete" for item in results)
    payload = {
        "contract_name": "wbc0020_finance_daily_outer_operation_v1",
        "status": "complete" if complete else "blocked",
        "query_only": True,
        "reconciled": bool(reconcile and complete),
        "dates": results,
        "accepted_cells": "342/342" if complete else "incomplete",
        "operation_count": 2,
        "production_mutation_count": 0,
        "non_target_invariant": "unchanged" if complete else "unproven",
        "producer_state": "unchanged/not paused",
    }
    if not complete:
        raise Wbc0020Error("WBC0020 outer query-only reconciliation is incomplete")
    return payload


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
            result = readback(reconcile=args.action == "reconcile")
    except Wbc0020Error as exc:
        print(
            json.dumps(
                {"status": "blocked", "reason": str(exc), "apply_count": 0},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
