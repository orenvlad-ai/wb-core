"""Live-oriented smoke for repeated Seller Portal recovery readiness attempts."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
import os
from pathlib import Path
import ssl
import sys
import time
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.registry_upload_http_entrypoint_hosted_runtime import (  # noqa: E402
    DEFAULT_TARGET_FILE,
    _build_probe_auth_cookie,
    load_hosted_runtime_target,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH,
    DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH,
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-file", type=Path, default=DEFAULT_TARGET_FILE)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--attempts", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--poll-seconds", type=float, default=1.5)
    parser.add_argument("--start-route", choices=["web", "direct"], default="web")
    parser.add_argument("--ca-file", default=os.environ.get("SSL_CERT_FILE") or _default_ca_file())
    parser.add_argument("--keep-last-open", action="store_true")
    args = parser.parse_args()

    if args.attempts < 1:
        raise SystemExit("--attempts must be >= 1")
    target = load_hosted_runtime_target(args.target_file)
    base_url = (args.base_url or target.public_base_url).rstrip("/")
    auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=args.timeout_seconds)
    ssl_context = _build_ssl_context(args.ca_file)

    outcomes: list[dict[str, Any]] = []
    try:
        for attempt in range(1, args.attempts + 1):
            start_payload = _post_json(
                f"{base_url}{_start_path(args.start_route)}",
                {"replace": True, "async": args.start_route == "web"},
                auth_cookie=auth_cookie,
                ssl_context=ssl_context,
                timeout=args.timeout_seconds,
            )
            job_id = str(start_payload.get("job_id") or "").strip()
            if job_id:
                job_payload = _poll_job(
                    base_url,
                    job_id=job_id,
                    auth_cookie=auth_cookie,
                    ssl_context=ssl_context,
                    timeout=args.timeout_seconds,
                    poll_seconds=args.poll_seconds,
                )
                if str(job_payload.get("status") or "") != "success":
                    raise RuntimeError(f"start job did not finish cleanly on attempt {attempt}: {job_payload.get('status')}")
                start_result = dict(job_payload.get("result") or {})
            else:
                start_result = start_payload
            run_id = str(start_result.get("run_id") or "").strip()
            readiness = _poll_recovery_readiness(
                base_url,
                run_id=run_id,
                auth_cookie=auth_cookie,
                ssl_context=ssl_context,
                timeout=args.timeout_seconds,
                poll_seconds=args.poll_seconds,
            )
            launcher_probe = _probe_launcher(base_url, auth_cookie=auth_cookie, ssl_context=ssl_context, timeout=args.timeout_seconds)
            outcome = {
                "attempt": attempt,
                "run_id": readiness.get("run_id") or run_id,
                "run_status": readiness.get("run_status") or readiness.get("status"),
                "launcher_ready": bool(readiness.get("launcher_ready") or readiness.get("can_download_launcher")),
                "can_download_launcher": bool(readiness.get("can_download_launcher")),
                "final_marker": str(readiness.get("final_marker") or ""),
                "launcher_http_status": launcher_probe["http_status"],
                "launcher_status": launcher_probe.get("launcher_status", ""),
            }
            outcomes.append(outcome)
            print(json.dumps(outcome, ensure_ascii=False, separators=(",", ":")), flush=True)
            if not (args.keep_last_open and attempt == args.attempts):
                _post_json(
                    f"{base_url}{DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH}",
                    {},
                    auth_cookie=auth_cookie,
                    ssl_context=ssl_context,
                    timeout=args.timeout_seconds,
                )
    finally:
        if not args.keep_last_open:
            try:
                _post_json(
                    f"{base_url}{DEFAULT_SELLER_PORTAL_RECOVERY_STOP_PATH}",
                    {},
                    auth_cookie=auth_cookie,
                    ssl_context=ssl_context,
                    timeout=args.timeout_seconds,
                )
            except Exception:
                pass

    ready_count = sum(
        1
        for item in outcomes
        if item["launcher_ready"] or item["run_status"] == "not_needed"
    )
    if ready_count != args.attempts:
        raise SystemExit(f"seller recovery readiness attempts failed: {ready_count}/{args.attempts}")
    print(f"seller_portal_recovery_live_readiness: ok -> {ready_count}/{args.attempts}", flush=True)


def _start_path(kind: str) -> str:
    return (
        DEFAULT_SHEET_WEB_VITRINA_SELLER_RECOVERY_START_PATH
        if kind == "web"
        else DEFAULT_SELLER_PORTAL_RECOVERY_START_PATH
    )


def _default_ca_file() -> str:
    fallback = Path("/etc/ssl/cert.pem")
    return str(fallback) if fallback.exists() else ""


def _build_ssl_context(ca_file: str) -> ssl.SSLContext | None:
    if not ca_file:
        return None
    return ssl.create_default_context(cafile=ca_file)


def _poll_job(
    base_url: str,
    *,
    job_id: str,
    auth_cookie: str | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = _get_json(
            f"{base_url}{DEFAULT_SHEET_JOB_PATH}?job_id={job_id}",
            auth_cookie=auth_cookie,
            ssl_context=ssl_context,
            timeout=timeout,
        )
        if str(payload.get("status") or "") != "running":
            return payload
        time.sleep(poll_seconds)
    raise TimeoutError(f"job {job_id} did not finish within {timeout:g}s")


def _poll_recovery_readiness(
    base_url: str,
    *,
    run_id: str,
    auth_cookie: str | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
    poll_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    query = f"?run_id={run_id}" if run_id else ""
    last_payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = _get_json(
            f"{base_url}{DEFAULT_SELLER_PORTAL_RECOVERY_STATUS_PATH}{query}",
            auth_cookie=auth_cookie,
            ssl_context=ssl_context,
            timeout=timeout,
        )
        last_payload = payload
        if bool(payload.get("launcher_ready") or payload.get("can_download_launcher")):
            return payload
        run_status = str(payload.get("run_status") or payload.get("status") or "")
        if run_status in {"not_needed", "completed", "error", "timeout", "stopped"}:
            return payload
        time.sleep(poll_seconds)
    raise TimeoutError(
        "seller recovery did not reach launcher readiness within "
        f"{timeout:g}s; last_status={last_payload.get('run_status') or last_payload.get('status')}"
    )


def _probe_launcher(
    base_url: str,
    *,
    auth_cookie: str | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
) -> dict[str, Any]:
    url = f"{base_url}{DEFAULT_SELLER_PORTAL_RECOVERY_LAUNCHER_PATH}"
    request = urllib_request.Request(url, method="GET", headers={"Accept": "application/zip, application/json"})
    if auth_cookie:
        request.add_header("Cookie", auth_cookie)
    try:
        with urllib_request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            response.read(1024)
            return {"http_status": int(response.status), "launcher_status": "zip_ready"}
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body) if body.strip() else {}
        except json.JSONDecodeError:
            payload = {}
        return {
            "http_status": int(exc.code),
            "launcher_status": str(payload.get("launcher_status") or payload.get("status") or ""),
        }


def _get_json(
    url: str,
    *,
    auth_cookie: str | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
) -> dict[str, Any]:
    request = urllib_request.Request(url, method="GET", headers={"Accept": "application/json"})
    if auth_cookie:
        request.add_header("Cookie", auth_cookie)
    with urllib_request.urlopen(request, timeout=timeout, context=ssl_context) as response:
        return json.loads(response.read().decode("utf-8"))


def _post_json(
    url: str,
    payload: dict[str, Any],
    *,
    auth_cookie: str | None,
    ssl_context: ssl.SSLContext | None,
    timeout: float,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json; charset=utf-8"},
    )
    if auth_cookie:
        request.add_header("Cookie", auth_cookie)
    try:
        with urllib_request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            return json.loads(response.read().decode("utf-8") or "{}")
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == HTTPStatus.CONFLICT.value:
            return json.loads(body or "{}")
        raise RuntimeError(f"POST {url} failed with HTTP {exc.code}: {body[:300]}") from exc


if __name__ == "__main__":
    main()
