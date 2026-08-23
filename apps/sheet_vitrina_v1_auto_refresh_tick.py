#!/usr/bin/env python3
"""Authenticated due-schedule tick for sheet_vitrina_v1 web-vitrina auto refresh."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import random
import shlex
import sys
import time
from typing import Any, Mapping
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.sheet_vitrina_v1_auto_refresh import (  # noqa: E402
    SheetVitrinaV1AutoRefreshSchedulesBlock,
)
from packages.application.sheet_vitrina_v1_night_refresh_experiment import (  # noqa: E402
    NightRefreshExperimentRunner,
    TARGET_DATE as NIGHT_EXPERIMENT_TARGET_DATE,
    TRIGGER_SOURCE as NIGHT_EXPERIMENT_TRIGGER_SOURCE,
)
from packages.application.sheet_vitrina_v1_control_refresh_canary import (  # noqa: E402
    ControlRefreshCanaryRunner,
    SystemdTimerCoordinator,
    arm_control_canary_manifest,
    arm_night_refresh_plan_manifest,
    control_canary_status,
    finalize_night_refresh_plans,
    night_refresh_plan_status,
    rebind_night_refresh_plan_manifest,
)
from packages.application.business_data_write_barrier import barrier_status  # noqa: E402
from packages.application.storage_registry import StoreRegistry  # noqa: E402


HOSTED_RUNTIME_APP_DIR = Path("/opt/wb-core-runtime/app")
HOSTED_RUNTIME_STATE_DIR = Path("/opt/wb-core-runtime/state")
DEFAULT_RUNTIME_DIR = HOSTED_RUNTIME_STATE_DIR if ROOT == HOSTED_RUNTIME_APP_DIR else ROOT / ".runtime" / "registry_upload"
DEFAULT_ENV_FILE = Path("/opt/wb-ai/.env")
DEFAULT_REFRESH_PATH = "/v1/sheet-vitrina-v1/refresh"
DEFAULT_JOB_PATH = "/v1/sheet-vitrina-v1/job"
DEFAULT_WEB_VITRINA_PATH = "/v1/sheet-vitrina-v1/web-vitrina"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = "8765"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--runtime-dir", default="")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--refresh-path", default="")
    parser.add_argument("--job-path", default="")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--experiment-status", action="store_true")
    parser.add_argument("--control-canary-status", action="store_true")
    parser.add_argument("--arm-control-canary", action="store_true")
    parser.add_argument("--night-refresh-plan-status", action="store_true")
    parser.add_argument("--arm-night-refresh-plan", action="store_true")
    parser.add_argument("--rebind-night-refresh-plan", action="store_true")
    parser.add_argument("--night-refresh-plan-id", default="")
    parser.add_argument("--previous-night-refresh-plan-id", default="")
    parser.add_argument("--control-canary-id", default="")
    parser.add_argument("--control-canary-due-at", default="")
    parser.add_argument("--control-canary-deadline", default="")
    parser.add_argument("--expected-deployed-sha", default="")
    parser.add_argument("--pause-unit", action="append", default=[])
    parser.add_argument("--restore-expired-control-canary-pauses", action="store_true")
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
    refresh_path = args.refresh_path or os.environ.get("SHEET_VITRINA_REFRESH_HTTP_PATH") or DEFAULT_REFRESH_PATH
    job_path = args.job_path or os.environ.get("SHEET_VITRINA_JOB_HTTP_PATH") or DEFAULT_JOB_PATH
    timer_coordinator = SystemdTimerCoordinator()
    if args.rebind_night_refresh_plan:
        deployed_sha = _read_deployed_sha()
        if not args.expected_deployed_sha or deployed_sha != str(args.expected_deployed_sha).strip().lower():
            raise RuntimeError(
                "night refresh plan rebind exact deployed SHA mismatch: "
                f"expected={str(args.expected_deployed_sha).strip().lower()} actual={deployed_sha}"
            )
        _print(
            rebind_night_refresh_plan_manifest(
                runtime_dir=runtime_dir,
                current_experiment_id=args.previous_night_refresh_plan_id,
                replacement_experiment_id=args.night_refresh_plan_id,
                expected_deployed_sha=deployed_sha,
                pause_units=args.pause_unit,
                now=datetime.now(timezone.utc),
            )
        )
        return 0
    if args.arm_night_refresh_plan:
        deployed_sha = _read_deployed_sha()
        if not args.expected_deployed_sha or deployed_sha != str(args.expected_deployed_sha).strip().lower():
            raise RuntimeError(
                "night refresh plan exact deployed SHA mismatch: "
                f"expected={str(args.expected_deployed_sha).strip().lower()} actual={deployed_sha}"
            )
        _print(
            arm_night_refresh_plan_manifest(
                runtime_dir=runtime_dir,
                experiment_id=args.night_refresh_plan_id,
                expected_deployed_sha=deployed_sha,
                pause_units=args.pause_unit,
                now=datetime.now(timezone.utc),
            )
        )
        return 0
    if args.arm_control_canary:
        deployed_sha = _read_deployed_sha()
        if not args.expected_deployed_sha or deployed_sha != str(args.expected_deployed_sha).strip().lower():
            raise RuntimeError(
                "control canary exact deployed SHA mismatch: "
                f"expected={str(args.expected_deployed_sha).strip().lower()} actual={deployed_sha}"
            )
        _print(
            arm_control_canary_manifest(
                runtime_dir=runtime_dir,
                experiment_id=args.control_canary_id,
                due_at=args.control_canary_due_at,
                deadline=args.control_canary_deadline,
                expected_deployed_sha=deployed_sha,
                pause_units=args.pause_unit,
                now=datetime.now(timezone.utc),
            )
        )
        return 0
    if args.restore_expired_control_canary_pauses:
        _print(
            {
                "status": "restore_watchdog_complete",
                "restored": timer_coordinator.restore_orphans(
                    control_root=runtime_dir / "experiments" / "sheet-vitrina-control-canaries",
                    now=datetime.now(timezone.utc),
                ),
            }
        )
        return 0
    if args.control_canary_status:
        _print(control_canary_status(runtime_dir=runtime_dir, now=datetime.now(timezone.utc)))
        return 0
    if args.night_refresh_plan_status:
        _print(night_refresh_plan_status(runtime_dir=runtime_dir, now=datetime.now(timezone.utc)))
        return 0
    block = SheetVitrinaV1AutoRefreshSchedulesBlock(runtime_dir=runtime_dir)
    due = sorted(block.due_schedules(), key=lambda item: str(item[1] or ""))
    missed_due, selected_due = _select_due_for_tick(due)
    cookie = _build_web_auth_cookie(os.environ, required=False)
    experiment = _build_night_experiment_runner(
        runtime_dir=runtime_dir,
        base_url=base_url,
        refresh_path=refresh_path,
        job_path=job_path,
        cookie=cookie,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
    )
    control_canary = _build_control_canary_runner(
        runtime_dir=runtime_dir,
        base_url=base_url,
        refresh_path=refresh_path,
        job_path=job_path,
        cookie=cookie,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        timer_coordinator=timer_coordinator,
    )
    if args.experiment_status:
        _print(experiment.status())
        return 0
    if args.dry_run:
        _print(
            {
                "status": "dry_run",
                "runtime_dir": str(runtime_dir),
                "base_url": base_url,
                "due_count": len(due),
                "selected_due_count": len(selected_due),
                "missed_due_count": len(missed_due),
                "due_schedules": [_public_due(item) for item in due],
                "selected_due_schedules": [_public_due(item) for item in selected_due],
                "missed_due_schedules": [_public_due(item) for item in missed_due],
                "night_experiment": experiment.status(),
                "control_canary": control_canary_status(
                    runtime_dir=runtime_dir,
                    now=datetime.now(timezone.utc),
                ),
                "night_refresh_plan": night_refresh_plan_status(
                    runtime_dir=runtime_dir,
                    now=datetime.now(timezone.utc),
                ),
            }
        )
        return 0
    experiment_result = experiment.tick()
    control_canary_result = control_canary.tick()
    finalized_night_plans = finalize_night_refresh_plans(
        runtime_dir=runtime_dir,
        now=datetime.now(timezone.utc),
    )
    night_plan_result = night_refresh_plan_status(
        runtime_dir=runtime_dir,
        now=datetime.now(timezone.utc),
    )
    canary_blocks_ordinary = str(control_canary_result.get("status") or "") not in {
        "no_due_canary",
        "armed",
        "terminal",
    }
    if canary_blocks_ordinary:
        _print(
            {
                "status": "control_canary_owned_tick",
                "runtime_dir": str(runtime_dir),
                "base_url": base_url,
                "due_count": len(due),
                "ordinary_due_launch_suppressed": True,
                "night_experiment": experiment_result,
                "control_canary": control_canary_result,
                "night_refresh_plan": night_plan_result,
                "finalized_night_plans": finalized_night_plans,
            }
        )
        return 0 if _control_canary_tick_exit_success(control_canary_result) else 1
    if not due:
        failed = str((experiment_result.get("tick_result") or {}).get("status") or "") == "failed"
        _print({
            "status": "error" if failed else "no_due_schedules",
            "runtime_dir": str(runtime_dir),
            "base_url": base_url,
            "due_count": 0,
            "night_experiment": experiment_result,
            "control_canary": control_canary_result,
            "night_refresh_plan": night_plan_result,
            "finalized_night_plans": finalized_night_plans,
        })
        return 1 if failed else 0
    if not cookie:
        cookie = _build_web_auth_cookie(os.environ, required=True)
    results: list[dict[str, Any]] = []
    exit_code = 0
    missed_due_marked = False
    for schedule, due_at in selected_due:
        schedule_id = str(schedule.get("id") or "")
        started_at = _utc_now()
        accepted_attempt = False
        try:
            refresh_result = _post_json(
                base_url + refresh_path,
                {
                    "async": True,
                    "auto_refresh": True,
                    "trigger_source": "scheduled",
                    "schedule_id": schedule_id,
                    "due_at": due_at,
                },
                cookie=cookie,
                timeout=min(args.timeout_seconds, 60),
            )
            job_id = str(refresh_result.get("job_id") or refresh_result.get("id") or "")
            if job_id:
                accepted_attempt = True
                block.mark_run_started(
                    schedule_id,
                    started_at=started_at,
                    due_at=due_at,
                    run_id=job_id,
                    trigger_source="scheduled",
                )
                terminal = _poll_job(
                    base_url=base_url,
                    job_path=job_path,
                    job_id=job_id,
                    cookie=cookie,
                    timeout_seconds=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                )
            else:
                terminal = refresh_result
                accepted_attempt = not _is_active_job_skip(terminal)
                if accepted_attempt:
                    block.mark_run_started(
                        schedule_id,
                        started_at=started_at,
                        due_at=due_at,
                        trigger_source="scheduled",
                    )
            if accepted_attempt and not missed_due_marked:
                _mark_missed_due_slots(block, missed_due)
                missed_due_marked = True
            status = str(terminal.get("status") or "").lower()
            error = str(terminal.get("error") or "")
            if accepted_attempt and not _terminal_contains_server_schedule_update(terminal):
                block.mark_run_finished(
                    schedule_id,
                    finished_at=_utc_now(),
                    result_payload=terminal,
                    error=error if status == "error" else "",
                )
            if status == "error" or _is_stale_active_job_skip(terminal):
                exit_code = 1
            results.append({"schedule_id": schedule_id, "due_at": due_at, "job_id": job_id, "status": status or terminal.get("semantic_status") or "success"})
        except Exception as exc:
            exit_code = 1
            if accepted_attempt and not missed_due_marked:
                _mark_missed_due_slots(block, missed_due)
                missed_due_marked = True
            if accepted_attempt:
                block.mark_run_finished(
                    schedule_id,
                    finished_at=_utc_now(),
                    error=str(exc),
                )
            results.append({"schedule_id": schedule_id, "due_at": due_at, "status": "error", "error": str(exc)})
    _print(
        {
            "status": "completed" if exit_code == 0 else "error",
            "runtime_dir": str(runtime_dir),
            "base_url": base_url,
            "due_count": len(due),
            "selected_due_count": len(selected_due),
            "missed_due_count": len(missed_due),
            "results": results,
            "night_experiment": experiment_result,
            "control_canary": control_canary_result,
            "night_refresh_plan": night_plan_result,
            "finalized_night_plans": finalized_night_plans,
        }
    )
    return exit_code


def _select_due_for_tick(
    due: list[tuple[dict[str, Any], str]]
) -> tuple[list[tuple[dict[str, Any], str]], list[tuple[dict[str, Any], str]]]:
    if not due:
        return [], []
    ordered = sorted(due, key=lambda item: str(item[1] or ""))
    return ordered[:-1], ordered[-1:]


def _mark_missed_due_slots(
    block: SheetVitrinaV1AutoRefreshSchedulesBlock,
    missed_due: list[tuple[dict[str, Any], str]],
) -> None:
    for missed_schedule, missed_due_at in missed_due:
        schedule_id = str(missed_schedule.get("id") or "")
        if not schedule_id:
            continue
        block.mark_due_skipped(
            schedule_id,
            due_at=str(missed_due_at or ""),
            reason="Слот расписания пропущен: выбран более поздний накопившийся due slot.",
            trigger_source="auto_refresh_tick_missed_due",
        )


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        try:
            parsed = shlex.split(value, posix=True)
        except ValueError:
            parsed = []
        if parsed:
            values[key] = parsed[0]
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            values[key] = value[1:-1]
        else:
            values[key] = value
    return values


def _build_web_auth_cookie(env: Mapping[str, str], *, required: bool = True) -> str:
    username = str(env.get("WB_CORE_WEB_AUTH_USERNAME") or "").strip()
    secret = str(env.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "").strip()
    if not username or not secret:
        if required:
            raise RuntimeError("WB_CORE_WEB_AUTH_USERNAME/WB_CORE_WEB_AUTH_SESSION_SECRET are required for auto refresh tick")
        return ""
    payload = _b64(json.dumps({"u": username, "exp": int(time.time()) + 3600}, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return f"wb_core_web_session={payload}.{signature}"


def _build_night_experiment_runner(
    *,
    runtime_dir: Path,
    base_url: str,
    refresh_path: str,
    job_path: str,
    cookie: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> NightRefreshExperimentRunner:
    from urllib.parse import urlencode

    def start_refresh(slot: object, wrapper_run_id: str) -> Mapping[str, Any]:
        return _post_json(
            base_url + refresh_path,
            {
                "async": True,
                "auto_refresh": True,
                "as_of_date": NIGHT_EXPERIMENT_TARGET_DATE,
                "trigger_source": NIGHT_EXPERIMENT_TRIGGER_SOURCE,
                "experiment_slot_id": getattr(slot, "slot_id", ""),
                "experiment_wrapper_run_id": wrapper_run_id,
            },
            cookie=cookie,
            timeout=min(timeout_seconds, 60),
        )

    def poll_job(job_id: str) -> Mapping[str, Any]:
        return _poll_job(
            base_url=base_url,
            job_path=job_path,
            job_id=job_id,
            cookie=cookie,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
        )

    def fetch_contract() -> Mapping[str, Any]:
        query = urlencode({"as_of_date": NIGHT_EXPERIMENT_TARGET_DATE})
        return _get_json(f"{base_url}{DEFAULT_WEB_VITRINA_PATH}?{query}", cookie=cookie, timeout=60)

    def fetch_source_status() -> Mapping[str, Any]:
        query = urlencode(
            {
                "surface": "page_composition",
                "as_of_date": NIGHT_EXPERIMENT_TARGET_DATE,
                "include_source_status": "1",
                "include_table_data": "0",
            }
        )
        return _get_json(f"{base_url}{DEFAULT_WEB_VITRINA_PATH}?{query}", cookie=cookie, timeout=60)

    return NightRefreshExperimentRunner(
        runtime_dir=runtime_dir,
        start_refresh=start_refresh,
        poll_job=poll_job,
        fetch_contract=fetch_contract,
        fetch_source_status=fetch_source_status,
    )


def _build_control_canary_runner(
    *,
    runtime_dir: Path,
    base_url: str,
    refresh_path: str,
    job_path: str,
    cookie: str,
    timeout_seconds: int,
    poll_seconds: float,
    timer_coordinator: SystemdTimerCoordinator,
) -> ControlRefreshCanaryRunner:
    from urllib.parse import urlencode

    def start_refresh(manifest: object, attempt_id: str) -> Mapping[str, Any]:
        return _post_json(
            base_url + refresh_path,
            {
                "async": True,
                "auto_refresh": True,
                "as_of_date": getattr(manifest, "target_date", ""),
                "trigger_source": NIGHT_EXPERIMENT_TRIGGER_SOURCE,
                "experiment_id": getattr(manifest, "experiment_id", ""),
                "experiment_slot_id": getattr(manifest, "slot_id", ""),
                "experiment_wrapper_run_id": attempt_id,
            },
            cookie=cookie,
            timeout=min(timeout_seconds, 60),
        )

    def poll_job(job_id: str, deadline: datetime) -> Mapping[str, Any]:
        remaining = max(1, int((deadline - datetime.now(timezone.utc)).total_seconds()))
        return _poll_job(
            base_url=base_url,
            job_path=job_path,
            job_id=job_id,
            cookie=cookie,
            timeout_seconds=min(timeout_seconds, remaining),
            poll_seconds=poll_seconds,
        )

    def fetch_contract(target_date: str) -> Mapping[str, Any]:
        query = urlencode({"as_of_date": target_date})
        return _get_json(f"{base_url}{DEFAULT_WEB_VITRINA_PATH}?{query}", cookie=cookie, timeout=60)

    def fetch_source_status(target_date: str) -> Mapping[str, Any]:
        query = urlencode(
            {
                "surface": "page_composition",
                "as_of_date": target_date,
                "include_source_status": "1",
                "include_table_data": "0",
            }
        )
        return _get_json(f"{base_url}{DEFAULT_WEB_VITRINA_PATH}?{query}", cookie=cookie, timeout=60)

    def fetch_ready_snapshot(target_date: str) -> Mapping[str, Any]:
        registry = StoreRegistry(runtime_dir)
        with registry.session(
            "operational",
            mode="ro",
            operation="sheet_vitrina_control_canary_ready_snapshot_readback",
            timeout_ms=10_000,
        ) as conn:
            row = conn.execute(
                """SELECT as_of_date,snapshot_id,refreshed_at
                   FROM sheet_vitrina_v1_ready_snapshots
                   WHERE as_of_date=?""",
                (target_date,),
            ).fetchone()
        return dict(row) if row is not None else {}

    return ControlRefreshCanaryRunner(
        runtime_dir=runtime_dir,
        start_refresh=start_refresh,
        poll_job=poll_job,
        fetch_contract=fetch_contract,
        fetch_source_status=fetch_source_status,
        fetch_ready_snapshot=fetch_ready_snapshot,
        timer_coordinator=timer_coordinator,
        read_deployed_sha=_read_deployed_sha,
        read_business_data_barrier=lambda: barrier_status(runtime_dir),
    )


def _post_json(url: str, payload: Mapping[str, Any], *, cookie: str, timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json", "Cookie": cookie},
        method="POST",
    )
    return _open_json(request, timeout=timeout)


def _get_json(url: str, *, cookie: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cookie": cookie})
    return _open_json(request, timeout=timeout)


class HTTPJSONError(RuntimeError):
    def __init__(
        self,
        *,
        status: int,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = int(status)
        self.payload = dict(payload)
        self.headers = {str(key): str(value) for key, value in dict(headers or {}).items()}
        super().__init__(f"HTTP {self.status}: {self.payload.get('error') or self.payload}")


class JobPollDeadlineError(RuntimeError):
    retryable = True


def _open_json(request: urllib.request.Request, *, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
        headers = dict(exc.headers.items()) if exc.headers is not None else {}
    else:
        headers = dict(response.headers.items()) if response.headers is not None else {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        if status >= 400:
            raise HTTPJSONError(
                status=status,
                payload={"error": "non_json_response"},
                headers=headers,
            ) from exc
        raise RuntimeError(f"HTTP {status}: non-JSON response") from exc
    if status >= 400:
        raise HTTPJSONError(status=status, payload=payload, headers=headers)
    if isinstance(payload, dict):
        return payload
    raise RuntimeError("JSON response must be an object")


def _poll_job(
    *,
    base_url: str,
    job_path: str,
    job_id: str,
    cookie: str,
    timeout_seconds: int,
    poll_seconds: float,
    get_json: Any = None,
    monotonic_factory: Any = None,
    sleep: Any = None,
    jitter_factory: Any = None,
) -> dict[str, Any]:
    getter = get_json or _get_json
    monotonic = monotonic_factory or time.monotonic
    sleeper = sleep or time.sleep
    jitter = jitter_factory or (lambda upper: random.uniform(0.0, upper))
    deadline = monotonic() + max(1, timeout_seconds)
    url = f"{base_url}{job_path}?job_id={urllib_parse_quote(job_id)}"
    last_payload: dict[str, Any] = {}
    last_retry_reason = "job remained queued/running"
    retry_count = 0
    while monotonic() < deadline:
        try:
            last_payload = getter(url, cookie=cookie, timeout=30)
        except HTTPJSONError as exc:
            if not _retryable_poll_http_error(exc):
                raise
            retry_count += 1
            last_retry_reason = f"HTTP {exc.status} retryable"
            delay = _poll_retry_delay(exc, retry_count=retry_count, poll_seconds=poll_seconds)
            remaining = max(0.0, deadline - monotonic())
            if remaining <= 0:
                break
            sleeper(min(delay + jitter(min(0.5, delay * 0.1)), remaining))
            continue
        except (TimeoutError, urllib.error.URLError) as exc:
            retry_count += 1
            last_retry_reason = f"transport timeout/retry: {type(exc).__name__}"
            delay = min(30.0, max(0.1, poll_seconds) * (2 ** min(retry_count - 1, 4)))
            remaining = max(0.0, deadline - monotonic())
            if remaining <= 0:
                break
            sleeper(min(delay + jitter(min(0.5, delay * 0.1)), remaining))
            continue
        status = str(last_payload.get("status") or "").lower()
        if status not in {"", "queued", "running"}:
            return last_payload
        remaining = max(0.0, deadline - monotonic())
        sleeper(min(max(0.1, poll_seconds), remaining))
    raise JobPollDeadlineError(
        f"auto refresh job observation deadline elapsed: {job_id}; last={last_retry_reason}"
    )


def _retryable_poll_http_error(error: HTTPJSONError) -> bool:
    payload = error.payload
    typed_contention = (
        error.status == 503
        and str(payload.get("contract_name") or "") == "wb_core_sqlite_contention_v1"
        and str(payload.get("code") or "") == "sqlite_write_busy"
        and payload.get("retryable") is True
    )
    return typed_contention or error.status in {429, 502, 503, 504}


def _poll_retry_delay(error: HTTPJSONError, *, retry_count: int, poll_seconds: float) -> float:
    payload_delay = error.payload.get("retry_after_ms")
    try:
        if payload_delay is not None:
            base = max(0.1, float(payload_delay) / 1000.0)
        else:
            raise ValueError
    except (TypeError, ValueError):
        retry_after = next(
            (value for key, value in error.headers.items() if key.lower() == "retry-after"),
            "",
        )
        base = _retry_after_header_seconds(retry_after)
        if base is None:
            base = max(0.1, poll_seconds)
    return min(30.0, base * (2 ** min(max(0, retry_count - 1), 4)))


def _retry_after_header_seconds(value: str) -> float | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return max(0.0, float(normalized))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(normalized)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _read_deployed_sha() -> str:
    path = HOSTED_RUNTIME_APP_DIR / ".wb-core-runtime-sha"
    try:
        value = path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise RuntimeError(f"cannot read canonical deployed SHA: {path}") from exc
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError(f"invalid canonical deployed SHA marker: {path}")
    return value


def urllib_parse_quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _public_due(item: tuple[Mapping[str, Any], str]) -> dict[str, Any]:
    schedule, due_at = item
    return {"schedule_id": schedule.get("id") or "", "local_time_hhmm": schedule.get("local_time_hhmm") or "", "due_at": due_at}


def _terminal_contains_server_schedule_update(payload: Mapping[str, Any]) -> bool:
    result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
    return isinstance(result.get("auto_schedule"), Mapping) or isinstance(payload.get("auto_schedule"), Mapping)


def _is_active_job_skip(payload: Mapping[str, Any]) -> bool:
    return str(payload.get("status") or "").lower() == "skipped" and bool(payload.get("already_running_job_id"))


def _is_stale_active_job_skip(payload: Mapping[str, Any]) -> bool:
    return _is_active_job_skip(payload) and bool(payload.get("active_job_stale"))


def _control_canary_tick_exit_success(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("status") or "") in {"accepted", "accepted_with_warning"}:
        return True
    artifact = payload.get("artifact") if isinstance(payload.get("artifact"), Mapping) else {}
    if not artifact.get("parent_plan_id"):
        return False
    checks = artifact.get("acceptance_checks") if isinstance(artifact.get("acceptance_checks"), Mapping) else {}
    failed_checks = {str(key) for key, value in checks.items() if value is not True}
    fingerprints = artifact.get("fingerprints") if isinstance(artifact.get("fingerprints"), Mapping) else {}
    return bool(
        str(artifact.get("technical_status") or "").lower() == "success"
        and str(artifact.get("semantic_status") or "").lower() in {"success", "warning"}
        and failed_checks == {"fresh_exact_date_fingerprint_match"}
        and fingerprints.get("known_volatile_only_difference") is True
        and fingerprints.get("fresh_readback_diff_paths")
        == ["meta.generated_at", "status_summary.business_now"]
    )


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
