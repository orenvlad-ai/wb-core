"""Targeted smoke-check for seller portal relogin auto-capture flow."""

from __future__ import annotations

import base64
import io
from importlib import util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as urllib_parse
import zipfile


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "apps" / "seller_portal_relogin_session.py"
SPEC = util.spec_from_file_location("seller_portal_relogin_session", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"unable to load module spec from {MODULE_PATH}")
MODULE = util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class _FakePage:
    def goto(self, *_args, **_kwargs) -> None:
        return

    def bring_to_front(self) -> None:
        return

    def evaluate(self, *_args, **_kwargs) -> None:
        return


class _FakeContext:
    def __init__(self) -> None:
        self.storage_state_calls = 0
        self.cookies: list[dict[str, object]] = []
        self.pages = [_FakePage()]
        self.closed = False

    def new_page(self) -> _FakePage:
        page = _FakePage()
        self.pages.append(page)
        return page

    def add_cookies(self, cookies: list[dict[str, object]]) -> None:
        self.cookies.extend(cookies)

    def storage_state(self, *, path: str) -> None:
        self.storage_state_calls += 1
        wrong_supplier_id = "wrong-supplier-id"
        payload = {
            "cookies": [
                {
                    "name": "x-supplier-id",
                    "value": wrong_supplier_id,
                    "domain": "seller.wildberries.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
                {
                    "name": "x-supplier-id-external",
                    "value": wrong_supplier_id,
                    "domain": ".wildberries.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": False,
                    "sameSite": "Lax",
                },
            ],
            "origins": [
                {
                    "origin": "https://seller.wildberries.ru",
                    "localStorage": [
                        {
                            "name": "analytics-external-data",
                            "value": base64.b64encode(
                                json.dumps(
                                    {"idSupplier": wrong_supplier_id, "idUser": 51178567},
                                    ensure_ascii=False,
                                ).encode("utf-8")
                            ).decode("utf-8"),
                        }
                    ],
                }
            ],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.context = _FakeContext()
        self.closed = False

    def launch_persistent_context(self, *_args, **_kwargs) -> _FakeContext:
        return self.context

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self) -> None:
        self.browser = _FakeBrowser()

    def __enter__(self) -> "_FakePlaywright":
        return self

    def __exit__(self, *_args) -> None:
        return

    @property
    def chromium(self) -> "_FakePlaywright":
        return self

    def launch_persistent_context(self, *_args, **_kwargs) -> _FakeContext:
        return self.browser.launch_persistent_context(*_args, **_kwargs)


def main() -> None:
    with tempfile.TemporaryDirectory() as temp_dir_raw:
        temp_dir = Path(temp_dir_raw)
        config = MODULE.ReloginSessionConfig(
            state_dir=temp_dir,
            storage_state_path=temp_dir / "storage_state.json",
            wb_bot_python=Path(sys.executable),
            timeout_sec=30,
            poll_sec=0.0,
            ssh_destination="wb-core-eu-root",
            canonical_supplier_id="canonical-supplier-id",
            canonical_supplier_label="ИП Сагитов В. Р.",
        )
        config.state_dir.mkdir(parents=True, exist_ok=True)
        config.storage_state_path.write_text(
            json.dumps(
                {
                    "cookies": [
                        {
                            "name": "existing-session-cookie",
                            "value": "seed",
                            "domain": "seller.wildberries.ru",
                            "path": "/",
                        }
                    ],
                    "origins": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fake_playwright = _FakePlaywright()
        probe_calls = {"count": 0}

        def fake_probe(_path: Path) -> dict[str, object]:
            probe_calls["count"] += 1
            if probe_calls["count"] == 1:
                return {"ok": False, "status": "seller_portal_session_invalid"}
            supplier_context = MODULE.read_storage_state_supplier_context(_path)
            return {
                "ok": True,
                "status": "ok",
                "final_url": "https://seller.wildberries.ru/search-analytics/my-search-queries",
                "supplier_context": supplier_context,
            }

        result = MODULE.run_login_capture(
            config,
            probe_fn=fake_probe,
            playwright_factory=lambda: fake_playwright,
            sleep_fn=lambda _seconds: None,
            visual_ready_fn=lambda _display: True,
        )

        if result.get("status") != "capture_completed":
            raise AssertionError(f"expected capture_completed, got {result}")
        if not config.storage_state_path.exists():
            raise AssertionError("storage_state.json must be written after auth is confirmed")
        if probe_calls["count"] < 2:
            raise AssertionError(f"probe must be retried until auth succeeds, got {probe_calls}")
        if not fake_playwright.browser.context.cookies:
            raise AssertionError("persistent context must receive cookies from existing storage state when available")
        if not fake_playwright.browser.context.closed:
            raise AssertionError("persistent context must be closed after auto-capture")
        if result.get("organization_confirmed") is not True:
            raise AssertionError(f"canonical supplier must be confirmed, got {result}")
        if result.get("organization_switch_applied") is not True:
            raise AssertionError(f"run_login_capture must auto-switch to canonical supplier, got {result}")
        final_supplier_context = MODULE.read_storage_state_supplier_context(config.storage_state_path)
        if final_supplier_context.get("current_supplier_id") != "canonical-supplier-id":
            raise AssertionError(f"storage_state.json must be rewritten to canonical supplier, got {final_supplier_context}")
        status_payload = MODULE._read_status(config.status_path)  # type: ignore[attr-defined]
        if status_payload.get("status") != "checking_canonical_supplier":
            raise AssertionError(f"intermediate checking_canonical_supplier status must be persisted, got {status_payload}")
        MODULE._write_status(  # type: ignore[attr-defined]
            config,
            {
                "run_id": "seller-recovery-test-run",
                "status": "awaiting_login",
                "message": "temporary noVNC session is ready for login",
                "started_at": "2026-04-23T00:00:00Z",
            },
        )
        config.pid_path.write_text("4242", encoding="utf-8")
        original_pid_is_running = MODULE._pid_is_running  # type: ignore[attr-defined]
        MODULE._pid_is_running = lambda _pid: True  # type: ignore[assignment]
        archive_bytes, archive_name = MODULE.build_macos_launcher_archive(
            config,
            public_status_url="http://89.191.226.88/v1/sheet-vitrina-v1/seller-portal-recovery/status",
            public_operator_url="http://89.191.226.88/sheet-vitrina-v1/operator",
        )
        MODULE._pid_is_running = original_pid_is_running  # type: ignore[assignment]
        if archive_name != "seller-portal-relogin-macos.zip":
            raise AssertionError(f"unexpected launcher archive name: {archive_name}")
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            names = archive.namelist()
            if names != ["seller-portal-relogin.command"]:
                raise AssertionError(f"unexpected launcher archive entries: {names}")
            launcher_text = archive.read("seller-portal-relogin.command").decode("utf-8")
        required_fragments = [
            "wb-core-eu-root",
            "RUN_ID=seller-recovery-test-run",
            "${STATUS}",
            "python3 -c",
            'json.loads(raw).get("status", "")',
            'json.loads(raw).get("summary", "")',
            "http://89.191.226.88/v1/sheet-vitrina-v1/seller-portal-recovery/status",
            "run_id=seller-recovery-test-run",
            "http://89.191.226.88/sheet-vitrina-v1/operator",
            "/vnc.html?autoconnect=1&resize=remote&path=websockify&reconnect=1",
            "Восстановление завершено: ${STATUS:-unknown}",
        ]
        missing_fragments = [item for item in required_fragments if item not in launcher_text]
        if missing_fragments:
            raise AssertionError(f"launcher script is missing required fragments: {missing_fragments}")
        if "sed -n 's/.*\"status\"" in launcher_text or 'sed -n \'s/.*"status"' in launcher_text:
            raise AssertionError("launcher script must not parse nested JSON status fields via greedy sed")

        MODULE._write_status(  # type: ignore[attr-defined]
            config,
            {
                "run_id": "seller-recovery-stop-run",
                "status": "awaiting_login",
                "message": "temporary noVNC session is ready for login",
                "started_at": "2026-04-23T00:00:00Z",
            },
        )
        config.pid_path.write_text("4242", encoding="utf-8")
        running_state = {"running": True}
        kill_calls: list[tuple[int, signal.Signals]] = []
        original_pid_is_running = MODULE._pid_is_running  # type: ignore[attr-defined]
        original_killpg = os.killpg
        original_sleep = MODULE.time.sleep  # type: ignore[attr-defined]

        def fake_killpg(pid: int, sig: signal.Signals) -> None:
            kill_calls.append((pid, sig))
            running_state["running"] = False

        try:
            MODULE._pid_is_running = lambda _pid: running_state["running"]  # type: ignore[assignment]
            os.killpg = fake_killpg  # type: ignore[assignment]
            MODULE.time.sleep = lambda _seconds: None  # type: ignore[assignment]
            stop_payload = MODULE.stop_relogin_session(config)
        finally:
            MODULE._pid_is_running = original_pid_is_running  # type: ignore[assignment]
            os.killpg = original_killpg  # type: ignore[assignment]
            MODULE.time.sleep = original_sleep  # type: ignore[assignment]

        if stop_payload.get("status") != "stopped" or stop_payload.get("running") is not False:
            raise AssertionError(f"stop must report stopped for an operator-requested active run, got {stop_payload}")
        if kill_calls != [(4242, signal.SIGTERM)]:
            raise AssertionError(f"stop must terminate the active supervisor process group once, got {kill_calls}")
        if config.pid_path.exists():
            raise AssertionError("stop must remove supervisor.pid after cleanup")

        auth_env_file = temp_dir / "web-auth.env"
        auth_env_file.write_text(
            "\n".join(
                [
                    "WB_CORE_WEB_AUTH_USERNAME=owner",
                    "WB_CORE_WEB_AUTH_SESSION_SECRET=relogin-smoke-secret",
                ]
            ),
            encoding="utf-8",
        )
        refresh_server, refresh_hits = _start_refresh_server()
        refresh_thread = threading.Thread(target=refresh_server.serve_forever, daemon=True)
        refresh_thread.start()
        try:
            base_url = f"http://127.0.0.1:{refresh_server.server_port}"
            refresh_config = MODULE.ReloginSessionConfig(
                state_dir=temp_dir,
                storage_state_path=temp_dir / "storage_state.json",
                wb_bot_python=Path(sys.executable),
                refresh_url=base_url + "/refresh",
                job_url=base_url + "/job",
                status_url=base_url + "/status",
                page_composition_url=base_url + "/page",
                web_auth_env_file=auth_env_file,
                canonical_supplier_id="canonical-supplier-id",
                canonical_supplier_label="ИП Сагитов В. Р.",
            )
            refresh_result = MODULE.trigger_refresh_and_wait(refresh_config)
        finally:
            refresh_server.shutdown()
            refresh_server.server_close()
            refresh_thread.join(timeout=5)
        if refresh_result.get("status") != "success":
            raise AssertionError(f"post-login refresh must accept app auth and success job status, got {refresh_result}")
        if refresh_hits.get("refresh_cookie") != 1 or refresh_hits.get("job_cookie") != 1:
            raise AssertionError(f"refresh/job requests must include sanitized app session cookie, got {refresh_hits}")

        print("seller_portal_relogin_session_capture: ok -> capture_completed after browser login")
        print("seller_portal_relogin_session_supplier_switch: ok -> canonical supplier enforced before final save")
        print("seller_portal_relogin_session_launcher: ok -> archive contains reusable Mac launcher script")
        print("seller_portal_relogin_session_stop: ok -> operator stop reports stopped, not unexpected_exit")
        print("seller_portal_relogin_session_post_login_refresh_auth: ok -> WebCore auth cookie and success job status accepted")
        print("smoke-check passed")


def _start_refresh_server() -> tuple[ThreadingHTTPServer, dict[str, int]]:
    hits = {
        "refresh_cookie": 0,
        "job_cookie": 0,
        "status_cookie": 0,
        "page_cookie": 0,
    }

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/refresh":
                self.send_error(404)
                return
            if not _has_auth_cookie(self):
                self._write_json(401, {"error": "authentication_required"})
                return
            hits["refresh_cookie"] += 1
            self._write_json(202, {"job_id": "refresh-job-1"})

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib_parse.urlparse(self.path)
            if not _has_auth_cookie(self):
                self._write_json(401, {"error": "authentication_required"})
                return
            if parsed.path == "/job":
                hits["job_cookie"] += 1
                self._write_json(200, {"job_id": "refresh-job-1", "status": "success", "result": {"semantic_status": "success"}})
                return
            if parsed.path == "/status":
                hits["status_cookie"] += 1
                self._write_json(200, {"semantic_status": "success", "semantic_reason": "ok", "source_outcomes": []})
                return
            if parsed.path == "/page":
                hits["page_cookie"] += 1
                self._write_json(200, {"activity_surface": {"loading_table": {"rows": []}}})
                return
            self.send_error(404)

        def log_message(self, *_args: object) -> None:
            return

        def _write_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    return server, hits


def _has_auth_cookie(handler: BaseHTTPRequestHandler) -> bool:
    return "wb_core_web_session=" in str(handler.headers.get("Cookie") or "")


if __name__ == "__main__":
    main()
