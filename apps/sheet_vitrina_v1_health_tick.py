#!/usr/bin/env python3
"""Bounded 06:30 candidate / 07:30 confirmation for Web Vitrina health."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
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
    JobPollDeadlineError,
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
    mark_web_vitrina_health_cycle_incomplete,
    persist_web_vitrina_health_evaluation,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402


GROUP_REFRESH_PATH = "/v1/sheet-vitrina-v1/web-vitrina/group-refresh"
CANDIDATE_TRIGGER = "web_vitrina_health_candidate"
TERMINAL_JOB_STATES = {"success", "warning", "error", "failed", "skipped", "action_required"}
MAX_CONFIRMATION_RECOVERY_GROUPS = 3
DEFAULT_POLL_DEADLINE_SECONDS = 1500
HEALTH_TICK_RECEIPT_CONTRACT = "sheet_vitrina_v1_health_tick_receipt/v2"


class HealthTickIncomplete(RuntimeError):
    def __init__(
        self,
        *,
        failure_code: str,
        reason: str,
        job: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(reason)
        self.failure_code = str(failure_code or "runner_failed")
        self.reason = str(reason or "morning health runner did not complete")
        self.job = dict(job or {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("candidate", "confirmation", "shadow"), required=True)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_POLL_DEADLINE_SECONDS)
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
    return _execute_health_tick(
        runtime=runtime,
        base_url=base_url,
        cookie=cookie,
        args=args,
    )


def _execute_health_tick(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    base_url: str,
    cookie: str,
    args: argparse.Namespace,
    post_json: Any = _post_json,
    poll_job: Any = _poll_job,
) -> int:
    launched: list[dict[str, Any]] = []
    failure: HealthTickIncomplete | None = None
    evaluation: dict[str, Any] | None = None
    application_deadline = time.monotonic() + max(1, int(args.timeout_seconds))
    try:
        if args.phase == "candidate" and not args.dry_run:
            candidate = post_json(
                base_url + DEFAULT_REFRESH_PATH,
                {
                    "async": True,
                    "auto_refresh": True,
                    "trigger_source": CANDIDATE_TRIGGER,
                },
                cookie=cookie,
                timeout=60,
            )
            launch = _job_identity(candidate)
            launched.append(launch)
            _emit_receipt(
                {
                    "event": "launch",
                    "status": "accepted",
                    "phase": args.phase,
                    "observed_at": _utc_timestamp(),
                    "job": launch,
                }
            )
            launched[-1] = _wait_job(
                candidate,
                base_url=base_url,
                cookie=cookie,
                args=args,
                poll_job=poll_job,
                application_deadline=application_deadline,
            )

        evaluation = evaluate_web_vitrina_health(runtime=runtime)
        if args.phase == "confirmation" and not args.dry_run:
            evaluation, recovery_jobs = _run_confirmation_recovery(
                evaluation=evaluation,
                runtime=runtime,
                base_url=base_url,
                cookie=cookie,
                args=args,
                post_json=post_json,
                poll_job=poll_job,
                application_deadline=application_deadline,
            )
            launched.extend(recovery_jobs)
    except HealthTickIncomplete as exc:
        failure = exc
        if exc.job and not any(
            str(item.get("job_id") or "") == str(exc.job.get("job_id") or "")
            for item in launched
        ):
            launched.append(dict(exc.job))
    except Exception as exc:  # fail closed with a durable observation whenever evaluation remains available
        failure = HealthTickIncomplete(
            failure_code="runner_failed",
            reason=f"Утренний контур завершился технической ошибкой ({type(exc).__name__}).",
        )

    if evaluation is None:
        try:
            evaluation = evaluate_web_vitrina_health(runtime=runtime)
        except Exception as exc:
            _emit_receipt(
                {
                    "event": "terminal",
                    "status": "error",
                    "phase": args.phase,
                    "observed_at": _utc_timestamp(),
                    "failure_code": "evaluation_unavailable",
                    "reason": f"Health evaluation is unavailable ({type(exc).__name__}).",
                    "jobs": launched,
                    "receipt": None,
                }
            )
            return 1

    observed_at = _utc_timestamp()
    if failure is not None:
        evaluation = mark_web_vitrina_health_cycle_incomplete(
            evaluation,
            phase=args.phase,
            failure_code=failure.failure_code,
            reason=failure.reason,
            observed_at=observed_at,
            job=failure.job,
        )
    try:
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
    except Exception as exc:
        _emit_receipt(
            {
                "event": "terminal",
                "status": "error",
                "phase": args.phase,
                "observed_at": observed_at,
                "failure_code": "observation_persist_failed",
                "reason": f"Health observation persistence failed ({type(exc).__name__}).",
                "jobs": launched,
                "receipt": None,
            }
        )
        return 1

    _emit_receipt(
        {
            "event": "terminal",
            "status": "incomplete" if failure is not None else ("dry_run" if args.dry_run else "complete"),
            "phase": args.phase,
            "observed_at": observed_at,
            "failure_code": failure.failure_code if failure is not None else "",
            "reason": failure.reason if failure is not None else "",
            "signals": evaluation["signals"],
            "recovery_preview": evaluation["recovery_preview"],
            "bot_gap_count": evaluation["bot_date_observations"]["gap_count"],
            "jobs": launched,
            "receipt": receipt,
        }
    )
    return 1 if failure is not None else 0


def _run_confirmation_recovery(
    *,
    evaluation: Mapping[str, Any],
    runtime: RegistryUploadDbBackedRuntime,
    base_url: str,
    cookie: str,
    args: argparse.Namespace,
    post_json: Any = _post_json,
    poll_job: Any = _poll_job,
    application_deadline: float | None = None,
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
        launched = post_json(
            base_url + GROUP_REFRESH_PATH,
            {
                "source_group_id": action["source_group_id"],
                "as_of_date": action["target_date"],
            },
            cookie=cookie,
            timeout=60,
        )
        launch = _job_identity(launched)
        _emit_receipt(
            {
                "event": "launch",
                "status": "accepted",
                "phase": "confirmation",
                "observed_at": _utc_timestamp(),
                "planned_action_fingerprint": action["action_fingerprint"],
                "requested_source_group_id": action["source_group_id"],
                "job": launch,
            }
        )
        terminal = _wait_job(
            launched,
            base_url=base_url,
            cookie=cookie,
            args=args,
            poll_job=poll_job,
            application_deadline=application_deadline,
        )
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
    poll_job: Any = _poll_job,
    application_deadline: float | None = None,
) -> dict[str, Any]:
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        state = str(payload.get("status") or "")
        if state not in TERMINAL_JOB_STATES:
            raise RuntimeError(f"health tick launch returned no job_id: {dict(payload)}")
        return {"job_id": "", "status": state, "single_flight": bool(payload.get("single_flight"))}
    state = str(payload.get("status") or "").lower()
    launch = _job_identity(payload)
    if bool(payload.get("single_flight")) and state in {"", "queued", "running"}:
        raise HealthTickIncomplete(
            failure_code="active_single_flight",
            reason=(
                "Утренний контур обнаружил уже активную загрузку Витрины; "
                "повторный job не создан и ожидание не продолжалось."
            ),
            job=launch,
        )
    timeout_seconds = max(1, int(args.timeout_seconds))
    if application_deadline is not None:
        remaining = int(application_deadline - time.monotonic())
        if remaining <= 1:
            raise HealthTickIncomplete(
                failure_code="poll_deadline_exceeded",
                reason=(
                    "Общий дедлайн утреннего контура истёк до завершения загрузки; "
                    "наблюдение сохранено как неполное без повторного запуска."
                ),
                job=launch,
            )
        timeout_seconds = min(timeout_seconds, remaining)
    try:
        terminal = poll_job(
            base_url=base_url,
            job_path=DEFAULT_JOB_PATH,
            job_id=job_id,
            cookie=cookie,
            timeout_seconds=timeout_seconds,
            poll_seconds=args.poll_seconds,
        )
    except JobPollDeadlineError as exc:
        raise HealthTickIncomplete(
            failure_code="poll_deadline_exceeded",
            reason=(
                "Загрузка Витрины не завершилась до внутреннего дедлайна; "
                "наблюдение сохранено как неполное без повторного запуска."
            ),
            job=launch,
        ) from exc
    state = str(terminal.get("status") or "").lower()
    if state in {"error", "failed"}:
        raise HealthTickIncomplete(
            failure_code="job_failed",
            reason="Загрузка Витрины завершилась ошибкой; успешное закрытие дня не подтверждено.",
            job={**launch, "status": state},
        )
    return {
        "job_id": job_id,
        "status": state,
        "operation": str(terminal.get("operation") or payload.get("operation") or ""),
        "single_flight": bool(payload.get("single_flight")),
    }


def _job_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(payload.get("job_id") or payload.get("already_running_job_id") or ""),
        "status": str(payload.get("status") or ""),
        "operation": str(payload.get("operation") or ""),
        "single_flight": bool(payload.get("single_flight")),
    }


def _emit_receipt(payload: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            {
                "contract": HEALTH_TICK_RECEIPT_CONTRACT,
                **dict(payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    raise SystemExit(main())
