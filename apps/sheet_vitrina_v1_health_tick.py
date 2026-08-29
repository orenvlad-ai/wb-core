#!/usr/bin/env python3
"""Bounded 06:30 candidate / 07:30 confirmation for Web Vitrina health."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_auto_refresh_tick import (  # noqa: E402
    DEFAULT_ENV_FILE,
    DEFAULT_HOST,
    DEFAULT_JOB_PATH,
    DEFAULT_PORT,
    DEFAULT_REFRESH_PATH,
    DEFAULT_RUNTIME_DIR,
    _build_web_auth_cookie,
    _poll_job,
    _post_json,
    _read_env_file,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_health import (  # noqa: E402
    evaluate_web_vitrina_health,
    persist_web_vitrina_health_evaluation,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402


GROUP_REFRESH_PATH = "/v1/sheet-vitrina-v1/web-vitrina/group-refresh"
CANDIDATE_TRIGGER = "web_vitrina_health_candidate"
TERMINAL_JOB_STATES = {"success", "warning", "error", "failed", "skipped", "action_required"}
MAX_CONFIRMATION_RECOVERY_GROUPS = 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("candidate", "confirmation", "shadow"), required=True)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    env = _read_env_file(Path(args.env_file))
    os.environ.update({key: value for key, value in env.items() if key not in os.environ})
    runtime_dir = Path(
        args.runtime_dir
        or os.environ.get("REGISTRY_UPLOAD_RUNTIME_DIR")
        or str(DEFAULT_RUNTIME_DIR)
    )
    base_url = (
        args.base_url
        or os.environ.get("SHEET_VITRINA_AUTO_REFRESH_BASE_URL")
        or f"http://{os.environ.get('REGISTRY_UPLOAD_HTTP_HOST', DEFAULT_HOST)}:{os.environ.get('REGISTRY_UPLOAD_HTTP_PORT', DEFAULT_PORT)}"
    ).rstrip("/")
    runtime = RegistryUploadDbBackedRuntime(
        runtime_dir,
        store_registry=StoreRegistry(runtime_dir),
    )
    cookie = _build_web_auth_cookie(os.environ, required=args.phase != "shadow")
    launched: list[dict[str, Any]] = []

    if args.phase == "candidate" and not args.dry_run:
        candidate = _post_json(
            base_url + DEFAULT_REFRESH_PATH,
            {
                "async": True,
                "auto_refresh": True,
                "trigger_source": CANDIDATE_TRIGGER,
            },
            cookie=cookie,
            timeout=60,
        )
        launched.append(_wait_job(candidate, base_url=base_url, cookie=cookie, args=args))

    evaluation = evaluate_web_vitrina_health(runtime=runtime)
    if args.phase == "confirmation" and not args.dry_run:
        evaluation, recovery_jobs = _run_confirmation_recovery(
            evaluation=evaluation,
            runtime=runtime,
            base_url=base_url,
            cookie=cookie,
            args=args,
        )
        launched.extend(recovery_jobs)

    observed_at = _utc_timestamp()
    receipt = (
        {
            "status": "dry_run",
            "business_date": evaluation["business_date"],
            "payload_fingerprint": evaluation["fingerprint"],
        }
        if args.dry_run
        else persist_web_vitrina_health_evaluation(
            runtime=runtime,
            evaluation=evaluation,
            phase=args.phase,
            observed_at=observed_at,
        )
    )
    print(
        json.dumps(
            {
                "status": "complete",
                "phase": args.phase,
                "observed_at": observed_at,
                "signals": evaluation["signals"],
                "recovery_preview": evaluation["recovery_preview"],
                "bot_gap_count": evaluation["bot_date_observations"]["gap_count"],
                "jobs": launched,
                "receipt": receipt,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_confirmation_recovery(
    *,
    evaluation: Mapping[str, Any],
    runtime: RegistryUploadDbBackedRuntime,
    base_url: str,
    cookie: str,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if str((evaluation.get("signals") or {}).get("yesterday_closed", {}).get("state") or "") == "ok":
        return dict(evaluation), []
    jobs: list[dict[str, Any]] = []
    actions = [
        dict(item)
        for item in (evaluation.get("recovery_preview") or {}).get("actions") or []
        if bool(item.get("apply_allowed")) and item.get("hook") == "group_refresh"
    ][:MAX_CONFIRMATION_RECOVERY_GROUPS]
    current = dict(evaluation)
    for action in actions:
        launched = _post_json(
            base_url + GROUP_REFRESH_PATH,
            {
                "source_group_id": action["source_group_id"],
                "as_of_date": action["target_date"],
            },
            cookie=cookie,
            timeout=60,
        )
        terminal = _wait_job(launched, base_url=base_url, cookie=cookie, args=args)
        terminal["planned_action_fingerprint"] = action["action_fingerprint"]
        terminal["requested_source_group_id"] = action["source_group_id"]
        jobs.append(terminal)
        current = evaluate_web_vitrina_health(runtime=runtime)
        if str(current["signals"]["yesterday_closed"]["state"]) == "ok":
            break
    return current, jobs


def _wait_job(
    payload: Mapping[str, Any],
    *,
    base_url: str,
    cookie: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        state = str(payload.get("status") or "")
        if state not in TERMINAL_JOB_STATES:
            raise RuntimeError(f"health tick launch returned no job_id: {dict(payload)}")
        return {"job_id": "", "status": state, "single_flight": bool(payload.get("single_flight"))}
    terminal = _poll_job(
        base_url=base_url,
        job_path=DEFAULT_JOB_PATH,
        job_id=job_id,
        cookie=cookie,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    state = str(terminal.get("status") or "").lower()
    if state in {"error", "failed"}:
        raise RuntimeError(f"Web Vitrina health job failed: job_id={job_id}; status={state}")
    return {
        "job_id": job_id,
        "status": state,
        "operation": str(terminal.get("operation") or payload.get("operation") or ""),
        "single_flight": bool(payload.get("single_flight")),
    }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
