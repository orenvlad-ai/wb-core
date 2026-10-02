"""Loopback HTTP ownership and live-scope checks for every V3 operation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import Event
from time import monotonic, sleep
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters import registry_upload_http_entrypoint as web  # noqa: E402
from packages.application.web_vitrina_window_v3 import WindowV3Service  # noqa: E402


def read(base_url: str, operation: str, principal: str, **params: str) -> tuple[int, dict]:
    query = {"surface": "page_composition", "window_format": "window_v3", "window_op": operation, **params}
    request = Request(base_url + web.DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + urlencode(query),
                      headers={"X-Test-Principal": principal, "Accept": "application/json"})
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.loads(response.read())


def ready(base_url: str, operation: str, principal: str, **params: str) -> dict:
    status, payload = read(base_url, operation, principal, **params)
    deadline = monotonic() + 45
    while status == 202 and monotonic() < deadline:
        sleep(0.05)
        status, payload = read(base_url, "job", principal, job_id=payload["job_id"])
    if status != 200:
        raise AssertionError(f"{operation} failed for {principal}: {status} {payload}")
    return payload


def assert_denied(base_url: str, operation: str, principal: str, expected_status: int,
                  forbidden_values: tuple[str, ...], **params: str) -> None:
    status, payload = read(base_url, operation, principal, **params)
    encoded = json.dumps(payload, ensure_ascii=False)
    if status != expected_status or any(value and value in encoded for value in forbidden_values):
        raise AssertionError(f"{operation} leaked to {principal}: {status} {payload}")


def main() -> None:
    principals = {
        "alice": {"username": "alice", "role": "operator", "allowed_sections": ["vitrina", "reports"]},
        "bob": {"username": "bob", "role": "operator", "allowed_sections": ["vitrina"]},
    }

    def authenticated(handler, _config):
        return principals.get(handler.headers.get("X-Test-Principal", ""))

    auth_config = {"configured": True, "enabled": True}
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True, advertise_window_v3=True) as base_url, \
         patch.object(web, "_web_auth_config", return_value=auth_config), \
         patch.object(web, "_authenticated_web_user", side_effect=authenticated):
        manifest_params = {"date_from": "2026-04-20", "date_to": "2026-04-21"}
        manifest = ready(base_url, "manifest", "alice", **manifest_params)
        session = manifest["session_id"]
        token = manifest["content_token"]
        session_params = {"session_id": session, "content_token": token}
        seek_params = {**session_params, "date_index": "0", "row_start": "0", "row_count": "1"}
        seek = ready(base_url, "seek", "alice", **seek_params)
        chunk_params = {**session_params, "cursor": seek["cursor"]}
        ready(base_url, "chunk", "alice", **chunk_params)
        ready(base_url, "global", "alice", **session_params, search="", sort="")
        operation_params = {
            "manifest": manifest_params,
            "job": {"job_id": "missing-job"},
            "seek": seek_params,
            "chunk": chunk_params,
            "global": {**session_params, "search": "", "sort": ""},
            "cancel": {"session_id": session},
        }
        secrets = (session, token, seek["cursor"])
        for operation, params in operation_params.items():
            assert_denied(base_url, operation, "nobody", 401, secrets, **params)
            if operation != "manifest":
                assert_denied(base_url, operation, "bob", 404, secrets, **params)

        # Keep one global worker pending while the same user's Vitrina grant is
        # revoked, then test again when its result has been prepared.
        worker_started = Event()
        release_worker = Event()
        original_build_global = WindowV3Service._build_global

        def delayed_global(self, session_state, arguments, cancelled):
            worker_started.set()
            if not release_worker.wait(10):
                raise AssertionError("test worker was not released")
            return original_build_global(self, session_state, arguments, cancelled)

        with patch.object(WindowV3Service, "_build_global", new=delayed_global):
            status, pending = read(base_url, "global", "alice", **session_params, search="view", sort="")
            if status != 202 or not worker_started.wait(5):
                raise AssertionError(f"global did not remain pending: {status} {pending}")
            job_id = pending["job_id"]
            secrets = (session, token, seek["cursor"], job_id)
            assert_denied(base_url, "job", "bob", 404, secrets, job_id=job_id)
            assert_denied(base_url, "cancel", "bob", 404, secrets, job_id=job_id)
            principals["alice"]["allowed_sections"] = ["reports"]
            for operation, params in {**operation_params, "job": {"job_id": job_id}}.items():
                assert_denied(base_url, operation, "alice", 403, secrets, **params)
            release_worker.set()
        # A still-valid login with changed grants is a different owner key even
        # if Vitrina is re-granted; old job/cursor/session remain unavailable.
        principals["alice"]["allowed_sections"] = ["vitrina"]
        for operation, params in {
            "job": {"job_id": job_id}, "seek": seek_params, "chunk": chunk_params,
            "global": {**session_params, "search": "", "sort": ""},
            "cancel": {"session_id": session},
        }.items():
            assert_denied(base_url, operation, "alice", 404, secrets, **params)
        principals["alice"]["allowed_sections"] = ["vitrina", "reports"]
        deadline = monotonic() + 45
        while monotonic() < deadline:
            status, result = read(base_url, "job", "alice", job_id=job_id)
            if status == 200:
                break
            if status != 202:
                raise AssertionError(f"owner job changed unexpectedly: {status} {result}")
            sleep(0.05)
        else:
            raise AssertionError("owner job did not finish")
        if result.get("kind") != "global":
            raise AssertionError("owner did not recover its own global result")
        assert_denied(base_url, "seek", "bob", 404, secrets,
                      **{**seek_params, "ack_job_ids": job_id})
        repeated_status, repeated = read(base_url, "job", "alice", job_id=job_id)
        if repeated_status != 200 or repeated.get("kind") != "global":
            raise AssertionError("foreign piggyback ACK released another owner's job")
        status, cancellation = read(base_url, "cancel", "alice", session_id=session)
        if status != 200 or cancellation.get("cancelled") is not True:
            raise AssertionError(f"owner could not cancel its own session: {status} {cancellation}")
        print("window_v3_auth_http: per-operation auth, owner isolation and pending-job scope revocation ok")


if __name__ == "__main__":
    main()
