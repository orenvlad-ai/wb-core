#!/usr/bin/env python3
"""Authenticated due-schedule tick for sheet_vitrina_v1 web-vitrina auto refresh."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
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


DEFAULT_RUNTIME_DIR = ROOT / ".runtime" / "registry_upload"
DEFAULT_ENV_FILE = Path("/opt/wb-ai/.env")
DEFAULT_REFRESH_PATH = "/v1/sheet-vitrina-v1/refresh"
DEFAULT_JOB_PATH = "/v1/sheet-vitrina-v1/job"
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
    block = SheetVitrinaV1AutoRefreshSchedulesBlock(runtime_dir=runtime_dir)
    due = block.due_schedules()
    if args.dry_run:
        _print({"status": "dry_run", "due_count": len(due), "due_schedules": [_public_due(item) for item in due]})
        return 0
    if not due:
        _print({"status": "no_due_schedules", "due_count": 0})
        return 0
    cookie = _build_web_auth_cookie(os.environ)
    results: list[dict[str, Any]] = []
    exit_code = 0
    for schedule, due_at in due:
        schedule_id = str(schedule.get("id") or "")
        started_at = _utc_now()
        block.mark_run_started(
            schedule_id,
            started_at=started_at,
            due_at=due_at,
            trigger_source="scheduled",
        )
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
            status = str(terminal.get("status") or "").lower()
            error = str(terminal.get("error") or "")
            if not _terminal_contains_server_schedule_update(terminal):
                block.mark_run_finished(
                    schedule_id,
                    finished_at=_utc_now(),
                    result_payload=terminal,
                    error=error if status == "error" else "",
                )
            if status == "error":
                exit_code = 1
            results.append({"schedule_id": schedule_id, "due_at": due_at, "job_id": job_id, "status": status or terminal.get("semantic_status") or "success"})
        except Exception as exc:
            exit_code = 1
            block.mark_run_finished(
                schedule_id,
                finished_at=_utc_now(),
                error=str(exc),
            )
            results.append({"schedule_id": schedule_id, "due_at": due_at, "status": "error", "error": str(exc)})
    _print({"status": "completed" if exit_code == 0 else "error", "due_count": len(due), "results": results})
    return exit_code


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


def _build_web_auth_cookie(env: Mapping[str, str]) -> str:
    username = str(env.get("WB_CORE_WEB_AUTH_USERNAME") or "").strip()
    secret = str(env.get("WB_CORE_WEB_AUTH_SESSION_SECRET") or "").strip()
    if not username or not secret:
        raise RuntimeError("WB_CORE_WEB_AUTH_USERNAME/WB_CORE_WEB_AUTH_SESSION_SECRET are required for auto refresh tick")
    payload = _b64(json.dumps({"u": username, "exp": int(time.time()) + 3600}, separators=(",", ":")).encode("utf-8"))
    signature = _b64(hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return f"wb_core_web_session={payload}.{signature}"


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


def _open_json(request: urllib.request.Request, *, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"HTTP {status}: non-JSON response") from exc
    if status >= 400:
        raise RuntimeError(f"HTTP {status}: {payload.get('error') or payload}")
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
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1, timeout_seconds)
    url = f"{base_url}{job_path}?job_id={urllib_parse_quote(job_id)}"
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last_payload = _get_json(url, cookie=cookie, timeout=30)
        status = str(last_payload.get("status") or "").lower()
        if status not in {"", "queued", "running"}:
            return last_payload
        time.sleep(max(0.1, poll_seconds))
    raise RuntimeError(f"auto refresh job timed out: {job_id}")


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


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
