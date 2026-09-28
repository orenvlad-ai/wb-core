"""Offline contracts for the ordinary Chrome login and its fast HTTP start."""

from __future__ import annotations

from contextlib import nullcontext, redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
import os
import pwd
import re
import sys
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_chrome_auth as auth  # noqa: E402
from apps import wb_buyer_chrome_price_smoke as price_smoke  # noqa: E402
from apps import wb_buyer_chrome_runtime as runtime  # noqa: E402
from apps.wb_buyer_network_diagnostic import MAX_EVENTS, NetworkDiagnostic, endpoint_category  # noqa: E402
from packages.application.wb_buyer_session import _public_recovery_payload  # noqa: E402


def _fast_start_and_private_boundary() -> None:
    with TemporaryDirectory() as directory:
        base = Path(directory)
        package, profile, state, novnc = (base / name for name in ("chrome.deb", "profile", "state", "novnc"))
        package.touch()
        for path in (profile, state, novnc):
            path.mkdir(mode=0o700)
        commands: list[list[str]] = []

        def run(command, **_kwargs):
            commands.append(list(command))
            return SimpleNamespace(returncode=0)

        with (
            patch.object(runtime, "PACKAGE", package),
            patch.object(runtime, "PROFILE", profile),
            patch.object(runtime, "STATE", state),
            patch.object(auth, "DIAGNOSTIC_DIR", base / "diagnostic-run"),
            patch.object(runtime, "USER", pwd.getpwuid(__import__("os").getuid()).pw_name),
            patch.object(runtime, "_available", return_value=30 * 1024**3),
            patch.object(runtime, "_root_reserve", return_value=25 * 1024**3),
            patch.object(runtime, "ensure_runtime", side_effect=AssertionError("HTTP start unpacked Chrome")),
            patch.object(runtime, "ensure_runner_idle"),
            patch.object(runtime, "_ensure_user", return_value=SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())),
            patch.object(auth, "NOVNC_DIR", novnc),
            patch.object(auth, "_shared_start_lock", side_effect=lambda: nullcontext()),
            patch.object(auth, "_systemd_properties", return_value={"ActiveState": "active", "InvocationID": "fixture-invocation"}),
            patch.object(auth, "_port_open", return_value=False),
            patch.object(auth.shutil, "which", return_value="/usr/bin/fixture"),
            patch.object(auth.legacy, "load_recovery_config_from_env", return_value=object()),
            patch.object(auth.legacy, "read_recovery_status", return_value={"running": False}),
            patch.object(auth.subprocess, "run", side_effect=run),
            patch.object(auth.os, "geteuid", return_value=0),
        ):
            payload = auth.start(viewer_owner="owner-hash", viewer_expires_at=int((datetime.now(timezone.utc) + timedelta(minutes=20)).timestamp()))
            assert payload["status"] == "starting" and payload["running"]
            assert payload["price"]["status"] == "not_checked"
            assert len(commands) == 2 and all(command[0] == "systemd-run" for command in commands)
            assert "expire-diagnostic" in commands[0] and "--on-active=2h" in commands[0]
            assert "prepare-supervise" in commands[1]
            assert not any(part.startswith("--property=User=") or part.startswith("--property=Group=") for part in commands[1])
            assert "--remote-debugging-port" not in " ".join(commands[1])
            public = _public_recovery_payload({**payload, "status": "completed", "running": False, "login_confirmed": True,
                "session": {"status": "authenticated_surface", "login_confirmed": True, "valid": False, "account_confirmed": False}}, launcher_download_path="")
            assert public["login_confirmed"] and not public["session"]["valid"]
            assert not public["session"]["account_confirmed"] and public["price"]["status"] == "not_checked"


def _english_login_and_account_surface() -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.route("https://id.wb.ru/**", lambda route: route.fulfill(body="<title>WB ID</title><body>Sign in with WB ID <input placeholder='+7'> <button>Receive Code</button></body>", content_type="text/html; charset=utf-8"))
        page.goto("https://id.wb.ru/login/")
        assert page.evaluate(auth.SURFACE_EXPRESSION) == "phone"
        page.route("https://www.wildberries.ru/**", lambda route: route.fulfill(body="<body>Подозрительная активность. Пожалуйста, подождите.</body>", content_type="text/html; charset=utf-8"))
        page.goto("https://www.wildberries.ru/lk")
        assert page.evaluate(auth.SURFACE_EXPRESSION) == "challenge"
        page.unroute_all()
        page.route("https://www.wildberries.ru/**", lambda route: route.fulfill(body="<body>Мои заказы <button>Выйти из аккаунта</button></body>", content_type="text/html; charset=utf-8"))
        page.goto("https://www.wildberries.ru/lk")
        assert page.evaluate(auth.SURFACE_EXPRESSION) == "account"
        page.route("https://id.wb.ru/**", lambda route: route.fulfill(body="<body>Challenge frame</body>", content_type="text/html; charset=utf-8"))
        page.locator("body").evaluate("node => node.insertAdjacentHTML('beforeend', '<iframe src=\"https://id.wb.ru/challenge\" style=\"position:fixed;inset:0;width:800px;height:600px\"></iframe>')")
        assert page.evaluate(auth.SURFACE_EXPRESSION) == "challenge"
        browser.close()


def _foreground_target_is_not_ambiguous() -> None:
    pipe = object.__new__(auth.ChromePipe)
    surfaces = {"account": "account", "login": "phone"}
    def call(method, params=None, *, session="", timeout=6):
        del timeout
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "page", "targetId": target, "url": "https://www.wildberries.ru/lk"} for target in surfaces]}
        if method == "Target.attachToTarget":
            return {"sessionId": params["targetId"]}
        if method == "Runtime.evaluate":
            return {"result": {"value": surfaces[session]}}
        return {}
    pipe.call = call
    assert pipe.visible_surface() == "phone"
    surfaces["login"] = "unknown"
    assert pipe.visible_surface() == "unknown"
    surfaces["login"] = "hidden"
    assert pipe.visible_surface() == "account"


def _pinned_package_and_deploy_contract() -> None:
    from apps.registry_upload_http_entrypoint_hosted_runtime import _build_buyer_chrome_install_command

    metadata = "Package: google-chrome-stable\nVersion: 154.0.8037.57-1\nArchitecture: amd64\n"
    with patch.object(runtime.subprocess, "run", return_value=SimpleNamespace(stdout=metadata)):
        assert runtime._package_identity(Path("/tmp/fixture.deb")) == ("google-chrome-stable", runtime.VERSION)
    command = _build_buyer_chrome_install_command(SimpleNamespace(target_dir="/opt/wb-core-runtime/app", ssh_destination="fixture-host"))
    assert "python3 apps/wb_buyer_chrome_runtime.py" in command[-1]
    assert command[-2] == "fixture-host"


def _manual_chrome_and_finish_are_owner_latched() -> None:
    with TemporaryDirectory() as directory:
        state = Path(directory)
        run_id = "buyer-recovery-chrome-20260928T120000Z-aabbccdd"
        with (
            patch.object(runtime, "STATE", state),
            patch.object(runtime, "PROFILE", state / "profile"),
            patch.object(runtime, "USER", pwd.getpwuid(os.getuid()).pw_name),
            patch.object(auth, "_shared_start_lock", side_effect=lambda: nullcontext()),
            patch.object(auth, "_systemd_properties", return_value={"ActiveState": "active", "InvocationID": "fixture"}),
        ):
            auth._write({"run_id": run_id, "unit": f"wbc-{run_id}.service", "invocation_id": "fixture",
                         "status": "awaiting_human", "viewer_owner": "owner", "session": {"valid": False}})
            calls: list[list[str]] = []
            with patch.object(auth, "_spawn", side_effect=lambda command, *_args, **_kwargs: calls.append(list(command)) or object()):
                auth._launch_manual_chrome(Path("/fixture/chrome"), {"PATH": "/usr/bin"})
            assert calls and calls[0][0] == "/fixture/chrome"
            assert not any("debugging" in part or "playwright" in part.lower() for part in calls[0])
            assert auth.finish(requested_run_id="foreign")["reason"] == "buyer_recovery_run_not_current"
            first = auth.finish(requested_run_id=run_id)
            second = auth.finish(requested_run_id=run_id)
            assert first["status"] == second["status"] == "validating_session"
            assert auth._read()["reason"] == "buyer_chrome_manual_finish_requested"
            auth._write({**auth._read(), "status": "stopping"})
            assert auth.finish(requested_run_id=run_id)["reason"] == "buyer_chrome_finish_not_ready"


def _orphan_runner_blocks_new_login() -> None:
    with TemporaryDirectory() as directory:
        proc = Path(directory)
        (proc / "1234").mkdir()
        with patch.object(runtime, "_ensure_user", return_value=SimpleNamespace(pw_uid=os.getuid())):
            try:
                runtime.ensure_runner_idle(proc)
            except RuntimeError as error:
                assert "runner still has processes" in str(error)
            else:
                raise AssertionError("orphan runner was accepted")
            (proc / "1234").rmdir()
            runtime.ensure_runner_idle(proc)


def _stop_latches_against_late_proof() -> None:
    with TemporaryDirectory() as directory:
        state = Path(directory)
        run_id = "buyer-recovery-chrome-20260927T120000Z-aabbccdd"
        entered = threading.Event()
        release = threading.Event()
        errors: list[BaseException] = []
        with patch.object(runtime, "STATE", state), patch.object(runtime, "USER", pwd.getpwuid(os.getuid()).pw_name):
            auth._write({"run_id": run_id, "unit": f"wbc-{run_id}.service", "status": "awaiting_human", "viewer_owner": "owner", "login_confirmed": False})
            original_write = auth._write_unlocked

            def gated_write(payload):
                if payload.get("status") == "stopping":
                    entered.set()
                    assert release.wait(timeout=3)
                original_write(payload)

            def stop_run():
                try:
                    auth.stop(requested_run_id=run_id)
                except BaseException as exc:
                    errors.append(exc)

            def late_proof():
                try:
                    auth._safe_status_update(run_id, status="completed", login_confirmed=True,
                        session={"status": "authenticated_surface", "login_confirmed": True})
                except BaseException as exc:
                    errors.append(exc)

            with (
                patch.object(auth, "_shared_start_lock", side_effect=lambda: nullcontext()),
                patch.object(auth, "_systemd_properties", return_value={"ActiveState": "active", "InvocationID": "fixture"}),
                patch.object(auth.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as unit_command,
                patch.object(auth, "_write_unlocked", side_effect=gated_write),
            ):
                first = threading.Thread(target=stop_run)
                second = threading.Thread(target=late_proof)
                first.start()
                assert entered.wait(timeout=3)
                second.start()
                release.set()
                first.join(timeout=3)
                second.join(timeout=3)
                assert not first.is_alive() and not second.is_alive() and not errors, errors
                status = auth._read()
                assert status["status"] == "stopping" and not status.get("login_confirmed")
                auth._safe_status_update(run_id, status="awaiting_human")
                assert auth._read()["status"] == "stopping"
                auth._write({"run_id": run_id, "unit": f"wbc-{run_id}.service", "status": "completed", "login_confirmed": True})
                unit_command.reset_mock()
                late_stop = auth.stop(requested_run_id=run_id)
                assert late_stop["status"] == "completed" and late_stop["login_confirmed"]
                unit_command.assert_not_called()


def _chrome_cleanup_counts_only_owned_executables() -> None:
    with TemporaryDirectory() as directory:
        base = Path(directory)
        proc = base / "proc"
        proc.mkdir()
        chrome = base / "chrome"
        python = base / "python3"
        other_chrome = base / "other-chrome"
        for executable in (chrome, python, other_chrome):
            executable.touch()
        (proc / "self").mkdir()
        own_cgroup = b"0::/system.slice/wbc-owned-run.service\n"
        (proc / "self" / "cgroup").write_bytes(own_cgroup)

        def process(pid: int, executable: Path, cgroup: bytes, command: bytes, comm: str = "chrome") -> None:
            folder = proc / str(pid)
            folder.mkdir()
            (folder / "exe").symlink_to(executable)
            (folder / "cgroup").write_bytes(cgroup)
            (folder / "cmdline").write_bytes(command)
            (folder / "comm").write_text(comm)

        # The supervisor carries --chrome PATH in argv, but executes Python.
        process(100, python, own_cgroup, b"python3\0supervise\0--chrome\0" + os.fsencode(chrome), "python3")
        process(101, chrome, own_cgroup, os.fsencode(chrome) + b"\0--type=renderer")
        process(102, chrome, b"0::/system.slice/another-run.service\n", os.fsencode(chrome))
        process(103, other_chrome, own_cgroup, os.fsencode(other_chrome))
        with patch.object(runtime, "CHROME", chrome):
            assert auth._owned_chrome_pids(proc) == [101]
            (proc / "101" / "exe").unlink()
            assert auth._owned_chrome_pids(proc) == []
            process(104, chrome, own_cgroup, b"chrome renderer", "chrome")
            process(105, other_chrome, own_cgroup, b"chrome-sandbox", "chrome-sandbox")
            process(107, chrome, own_cgroup, b"chrome exited", "chrome")
            (proc / "107" / "comm").unlink()
            denied = {proc / str(pid) / "exe" for pid in (104, 105, 106, 107)}
            original_stat = Path.stat

            def sandboxed_stat(path, *args, **kwargs):
                if path in denied:
                    raise PermissionError(13, "sandboxed executable")
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", new=sandboxed_stat):
                assert auth._owned_chrome_pids(proc) == [104, 105]
                process(106, other_chrome, own_cgroup, b"other", "other")
                try:
                    auth._owned_chrome_pids(proc)
                except RuntimeError as error:
                    assert str(error) == "Chrome process identity unavailable"
                else:
                    raise AssertionError("An ambiguous owned process was declared clean")
        with patch.object(auth, "_owned_chrome_pids", return_value=[101]), patch.object(auth.time, "monotonic", side_effect=[0, 16]):
            try:
                auth._stop_chrome(None)
            except RuntimeError as error:
                assert str(error) == "Chrome descendants did not exit"
            else:
                raise AssertionError("An owned Chrome process did not block cleanup")
        diagnostic = StringIO()
        with redirect_stderr(diagnostic):
            auth._report_failure("chrome_stop", PermissionError(13, "private OAuth state"))
        assert diagnostic.getvalue().strip() == "buyer_chrome_diagnostic stage=chrome_stop class=PermissionError errno=13"


def _network_diagnostic_is_bounded_and_redacted() -> None:
    secret = "phone79998887766-otp123456-stateoauthsecret"
    assert endpoint_category(f"https://id.wb.ru/login/{secret}?state={secret}") == "wbid_login"
    assert endpoint_category(f"https://www.wildberries.ru/wb-id/callback?code={secret}") == "marketplace_callback"
    assert endpoint_category(f"https://evil.example/login/{secret}") == ""
    with TemporaryDirectory() as directory:
        path = Path(directory) / "events.jsonl"
        with path.open("wb") as output:
            diagnostic = NetworkDiagnostic(output_fd=output.fileno())
            diagnostic.enable_session("owned-session")
            request = lambda request_id, url, method="POST": {
                "sessionId": "owned-session", "method": "Network.requestWillBeSent",
                "params": {"requestId": request_id, "type": "XHR", "request": {"url": url, "method": method, "postData": secret}},
            }
            response = lambda request_id, status: {
                "sessionId": "owned-session", "method": "Network.responseReceived",
                "params": {"requestId": request_id, "response": {"status": status, "url": f"https://id.wb.ru/{secret}?state={secret}", "headers": {"Cookie": secret}}},
            }
            diagnostic.consume(request("1", f"https://id.wb.ru/login/{secret}?state={secret}"))
            diagnostic.consume(response("foreign-id", 500))
            diagnostic.consume(response("1", 502))
            diagnostic.consume(request("2", f"https://id.wb.ru/api/{secret}?phone={secret}"))
            diagnostic.consume({"sessionId": "owned-session", "method": "Network.loadingFailed",
                "params": {"requestId": "2", "errorText": f"net::ERR_FAILED?token={secret}"}})
            diagnostic.consume(request("3", f"https://id.wb.ru/api/{secret}?phone={secret}"))
            diagnostic.consume({"sessionId": "owned-session", "method": "Network.loadingFailed",
                "params": {"requestId": "3", "errorText": "net::ERR_CONNECTION_RESET"}})
            diagnostic.consume(request("4", f"https://evil.example/{secret}"))
            diagnostic.consume(response("4", 403))
            diagnostic.enable_session("second-owned-tab")
            diagnostic.consume(request("collision", "https://id.wb.ru/login/"))
            diagnostic.consume({"sessionId": "second-owned-tab", "method": "Network.requestWillBeSent",
                "params": {"requestId": "collision", "type": "XHR", "request": {"url": "https://www.wildberries.ru/api/check", "method": "POST"}}})
            diagnostic.consume(response("collision", 500))
            diagnostic.consume({"sessionId": "second-owned-tab", "method": "Network.responseReceived",
                "params": {"requestId": "collision", "response": {"status": 401}}})
            diagnostic.consume({"sessionId": "foreign-session", "method": "Network.requestWillBeSent",
                "params": {"requestId": "5", "type": "XHR", "request": {"url": f"https://id.wb.ru/api/{secret}", "method": "POST"}}})
            for index in range(MAX_EVENTS + 20):
                identifier = f"bounded-{index}"
                diagnostic.consume(request(identifier, "https://id.wb.ru/api/check"))
                diagnostic.consume(response(identifier, 429))
            assert diagnostic.events == MAX_EVENTS and len(diagnostic.requests) <= 256
        raw = path.read_text()
        assert secret not in raw and "Cookie" not in raw and "?" not in raw and "/login" not in raw
        records = [json.loads(line) for line in raw.splitlines()]
        assert len(records) == MAX_EVENTS
        assert records[:3] == [
            {**records[0], "endpoint": "wbid_login", "code": 502},
            {**records[1], "endpoint": "wbid_api", "code": "network_other"},
            {**records[2], "endpoint": "wbid_api", "code": "net::ERR_CONNECTION_RESET"},
        ]
        assert records[3]["endpoint"] == "wbid_login" and records[3]["code"] == 500
        assert records[4]["endpoint"] == "marketplace_api" and records[4]["code"] == 401
        assert all(set(record) == {"event", "at", "stage", "endpoint", "kind", "method", "outcome", "code"} for record in records)
        assert all(re.fullmatch(r"[a-z_]+", record["endpoint"]) for record in records)


def _private_pipe_interleaves_network_events_and_replies() -> None:
    to_chrome_read, to_chrome_write = os.pipe()
    from_chrome_read, from_chrome_write = os.pipe()
    with TemporaryDirectory() as directory:
        with (Path(directory) / "events.jsonl").open("wb") as output:
            pipe = object.__new__(auth.ChromePipe)
            pipe._read_fd, pipe._write_fd = from_chrome_read, to_chrome_write
            pipe._buffer, pipe._next_id = bytearray(), 1
            pipe._diagnostic = NetworkDiagnostic(output_fd=output.fileno())
            pipe._diagnostic.enable_session("owned")
            def reply():
                command = json.loads(os.read(to_chrome_read, 4096).rstrip(b"\0"))
                for event in (
                    {"sessionId": "owned", "method": "Network.requestWillBeSent", "params": {"requestId": "r", "type": "Document", "request": {"url": "https://id.wb.ru/login/", "method": "GET"}}},
                    {"sessionId": "owned", "method": "Network.responseReceived", "params": {"requestId": "r", "response": {"status": 503}}},
                    {"id": command["id"], "result": {"product": "Chrome"}},
                ):
                    os.write(from_chrome_write, json.dumps(event).encode() + b"\0")
            worker = threading.Thread(target=reply)
            worker.start()
            assert pipe.call("Browser.getVersion") == {"product": "Chrome"}
            worker.join(timeout=2)
            assert not worker.is_alive()
        assert json.loads((Path(directory) / "events.jsonl").read_text())["code"] == 503
    for fd in (to_chrome_read, to_chrome_write, from_chrome_read, from_chrome_write):
        os.close(fd)


def _network_is_armed_before_human_window() -> None:
    pipe = object.__new__(auth.ChromePipe)
    pipe._diagnostic = NetworkDiagnostic(output_fd=-1)
    pipe._network_sessions = {}
    seen: list[str] = []
    targets = [{"type": "page", "targetId": "initial", "url": "about:blank"}]
    fail_new_tab = False
    def call(method, params=None, *, session="", timeout=6):
        del timeout
        seen.append(method)
        if method == "Target.getTargets":
            return {"targetInfos": list(targets)}
        if method == "Target.attachToTarget":
            return {"sessionId": params["targetId"]}
        if method == "Network.enable" and fail_new_tab and session == "new-wbid-tab":
            raise RuntimeError("fixture target not armed")
        if method == "Runtime.evaluate":
            return {"result": {"value": "phone"}}
        return {}
    pipe.call = call
    pipe.enable_network()
    assert seen == ["Target.getTargets", "Target.attachToTarget", "Network.enable"]
    targets.append({"type": "page", "targetId": "new-wbid-tab", "url": "https://id.wb.ru/login/"})
    fail_new_tab = True
    assert pipe.visible_surface() == "unknown"
    assert "new-wbid-tab" not in pipe._network_sessions
    fail_new_tab = False
    assert pipe.visible_surface() == "phone"
    assert pipe._network_sessions == {"initial": "initial", "new-wbid-tab": "new-wbid-tab"}
    assert seen.count("Network.enable") == 3
    assert seen.index("Network.enable", 4) < seen.index("Runtime.evaluate")


def _diagnostic_expires_only_its_own_run() -> None:
    with TemporaryDirectory() as directory:
        state = Path(directory)
        path = state / "network_diagnostic.jsonl"
        run = "buyer-recovery-chrome-20260928T110909Z-ca76cc20"
        other = "buyer-recovery-chrome-20260928T111010Z-aabbccdd"
        with patch.object(auth, "DIAGNOSTIC_DIR", state), patch.object(auth.os, "geteuid", return_value=0), patch.object(auth, "_shared_start_lock", side_effect=lambda: nullcontext()):
            path.write_text(json.dumps({"event": "buyer_network_run", "run_id": other}) + "\n")
            os.utime(path, (100, 100))
            with patch.object(auth.time, "time", return_value=100 + 3 * 3600):
                auth.expire_diagnostic(run)
                assert path.exists()
                auth.expire_diagnostic(other)
                assert not path.exists()


def main() -> None:
    _fast_start_and_private_boundary()
    _english_login_and_account_surface()
    _foreground_target_is_not_ambiguous()
    _pinned_package_and_deploy_contract()
    _manual_chrome_and_finish_are_owner_latched()
    _orphan_runner_blocks_new_login()
    _stop_latches_against_late_proof()
    _chrome_cleanup_counts_only_owned_executables()
    _network_diagnostic_is_bounded_and_redacted()
    _private_pipe_interleaves_network_events_and_replies()
    _network_is_armed_before_human_window()
    _diagnostic_expires_only_its_own_run()
    # The current PR Gate selects its check map from base; keep the new
    # isolated reader fixture reachable through this established smoke.
    price_smoke.main()
    print("wb_buyer_chrome_auth_smoke: OK")


if __name__ == "__main__":
    main()
