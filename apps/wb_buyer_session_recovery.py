"""Temporary localhost-only noVNC recovery for the isolated WB buyer session."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Mapping
from urllib import parse as urllib_parse
from uuid import uuid4
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from packages.adapters.wb_buyer_session import (  # noqa: E402
    WbBuyerSessionAdapter,
    WbBuyerSessionConfig,
    load_wb_buyer_session_config_from_env,
)


UTC = timezone.utc
DEFAULT_DISPLAY = ":98"
DEFAULT_VNC_PORT = 45911
DEFAULT_WEB_PORT = 46090
DEFAULT_TIMEOUT_SEC = 1800
DEFAULT_POLL_SEC = 3.0
DEFAULT_SSH_DESTINATION = "wb-core-eu-root"
DEFAULT_NOVNC_WEB_DIR = Path("/usr/share/novnc")
ACTIVE_STATUSES = {"starting", "awaiting_login", "saving_session", "validating_session"}
FINAL_STATUSES = {"completed", "stopped", "timeout", "error"}


@dataclass(frozen=True)
class BuyerRecoveryConfig:
    session: WbBuyerSessionConfig
    display: str = DEFAULT_DISPLAY
    vnc_port: int = DEFAULT_VNC_PORT
    web_port: int = DEFAULT_WEB_PORT
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    poll_sec: float = DEFAULT_POLL_SEC
    ssh_destination: str = DEFAULT_SSH_DESTINATION
    novnc_web_dir: Path = DEFAULT_NOVNC_WEB_DIR

    @property
    def status_path(self) -> Path:
        return self.session.state_dir / "recovery_status.json"

    @property
    def pid_path(self) -> Path:
        return self.session.state_dir / "recovery_supervisor.pid"

    @property
    def candidate_path(self) -> Path:
        return self.session.state_dir / "candidate_storage_state.json"

    @property
    def supervisor_log_path(self) -> Path:
        return self.session.state_dir / "recovery_supervisor.log"

    @property
    def xvfb_log_path(self) -> Path:
        return self.session.state_dir / "recovery_xvfb.log"

    @property
    def openbox_log_path(self) -> Path:
        return self.session.state_dir / "recovery_openbox.log"

    @property
    def x11vnc_log_path(self) -> Path:
        return self.session.state_dir / "recovery_x11vnc.log"

    @property
    def websockify_log_path(self) -> Path:
        return self.session.state_dir / "recovery_websockify.log"

    @property
    def novnc_url(self) -> str:
        query = urllib_parse.urlencode({"autoconnect": "1", "resize": "remote", "path": "websockify", "reconnect": "1"})
        return f"http://127.0.0.1:{self.web_port}/vnc.html?{query}"


def load_recovery_config_from_env() -> BuyerRecoveryConfig:
    return BuyerRecoveryConfig(
        session=load_wb_buyer_session_config_from_env(),
        display=str(os.environ.get("WB_BUYER_RECOVERY_DISPLAY") or DEFAULT_DISPLAY),
        vnc_port=_env_int("WB_BUYER_RECOVERY_VNC_PORT", DEFAULT_VNC_PORT),
        web_port=_env_int("WB_BUYER_RECOVERY_WEB_PORT", DEFAULT_WEB_PORT),
        timeout_sec=_env_int("WB_BUYER_RECOVERY_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC),
        poll_sec=_env_float("WB_BUYER_RECOVERY_POLL_SEC", DEFAULT_POLL_SEC),
        ssh_destination=str(os.environ.get("WB_BUYER_RECOVERY_SSH_DESTINATION") or DEFAULT_SSH_DESTINATION),
        novnc_web_dir=Path(str(os.environ.get("WB_BUYER_RECOVERY_NOVNC_WEB_DIR") or DEFAULT_NOVNC_WEB_DIR)),
    )


def main() -> None:
    config = load_recovery_config_from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "stop", "supervise"):
        sub = subparsers.add_parser(name)
        _add_args(sub, config)
        if name == "start":
            sub.add_argument("--replace", action="store_true")
        if name == "status":
            sub.add_argument("--probe", action="store_true")
            sub.add_argument("--run-id", default="")
    args = parser.parse_args()
    config = _config_from_args(args)
    if args.command == "start":
        payload = start_recovery(config, replace=args.replace)
    elif args.command == "status":
        payload = read_recovery_status(config, with_probe=args.probe, requested_run_id=args.run_id or None)
    elif args.command == "stop":
        payload = stop_recovery(config)
    else:
        raise SystemExit(supervise_recovery(config))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def start_recovery(config: BuyerRecoveryConfig, *, replace: bool = False) -> dict[str, Any]:
    _ensure_state_dir(config)
    current = read_recovery_status(config, with_probe=False)
    if current.get("running"):
        if not replace:
            return current
        stop_recovery(config)
    run_id = f"buyer-recovery-{_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
    session = WbBuyerSessionAdapter(config=config.session).check_session()
    if session.get("status") == "valid":
        payload = {
            "run_id": run_id,
            "status": "completed",
            "reason": "buyer_session_already_valid",
            "started_at": _now_text(),
            "finished_at": _now_text(),
            "session": session,
        }
        _write_status(config, payload)
        return read_recovery_status(config, with_probe=False)
    _write_status(
        config,
        {
            "run_id": run_id,
            "status": "starting",
            "reason": "buyer_recovery_starting",
            "started_at": _now_text(),
            "deadline_at": (_now() + timedelta(seconds=config.timeout_sec)).isoformat(),
            "session": session,
        },
    )
    with _open_secure_log(config.supervisor_log_path) as log_file:
        process = subprocess.Popen(
            _supervisor_command(config),
            cwd=str(ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    config.pid_path.write_text(str(process.pid), encoding="utf-8")
    os.chmod(config.pid_path, 0o600)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = read_recovery_status(config, with_probe=False)
        if status.get("status") in ACTIVE_STATUSES | FINAL_STATUSES:
            return status
        time.sleep(0.25)
    return read_recovery_status(config, with_probe=False)


def stop_recovery(config: BuyerRecoveryConfig) -> dict[str, Any]:
    pid = _read_pid(config.pid_path)
    if pid and _pid_running(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_running(pid):
            time.sleep(0.2)
        if _pid_running(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    config.pid_path.unlink(missing_ok=True)
    config.candidate_path.unlink(missing_ok=True)
    payload = _read_status(config.status_path)
    if str(payload.get("status") or "") not in FINAL_STATUSES:
        payload.update({"status": "stopped", "reason": "buyer_recovery_stopped", "finished_at": _now_text()})
        _write_status(config, payload)
    return read_recovery_status(config, with_probe=True)


def read_recovery_status(
    config: BuyerRecoveryConfig,
    with_probe: bool,
    *,
    requested_run_id: str | None = None,
) -> dict[str, Any]:
    payload = _read_status(config.status_path)
    pid = _read_pid(config.pid_path)
    payload["running"] = bool(pid and _pid_running(pid))
    payload.setdefault("status", "idle")
    payload.setdefault("run_id", "")
    if not payload["running"] and payload["status"] in ACTIVE_STATUSES:
        payload.update({"status": "error", "reason": "buyer_recovery_unexpected_exit", "finished_at": _now_text()})
        _write_status(config, payload)
    requested = str(requested_run_id or "").strip()
    if requested and requested != str(payload.get("run_id") or ""):
        return {
            "run_id": str(payload.get("run_id") or ""),
            "status": "error",
            "reason": "buyer_recovery_run_not_current",
            "running": payload["running"],
            "session": {},
        }
    if with_probe and not payload["running"]:
        payload["session"] = WbBuyerSessionAdapter(config=config.session).check_session()
    return payload


def supervise_recovery(config: BuyerRecoveryConfig) -> int:
    _ensure_state_dir(config)
    config.pid_path.write_text(str(os.getpid()), encoding="utf-8")
    os.chmod(config.pid_path, 0o600)
    processes: list[subprocess.Popen[Any]] = []
    adapter = WbBuyerSessionAdapter(config=config.session)
    try:
        with adapter.session_lock(blocking=False):
            _ensure_commands(config)
            _write_status(config, {**_read_status(config.status_path), "status": "starting", "reason": "buyer_visual_session_starting"})
            xvfb = _spawn(["Xvfb", config.display, "-screen", "0", "1600x900x24", "-nolisten", "tcp"], config.xvfb_log_path)
            processes.append(xvfb)
            _wait_display(config.display)
            openbox_path = shutil.which("openbox")
            if openbox_path:
                processes.append(_spawn([openbox_path], config.openbox_log_path, env={"DISPLAY": config.display}))
            x11vnc = _spawn(
                ["x11vnc", "-display", config.display, "-localhost", "-shared", "-forever", "-nopw", "-noxdamage", "-rfbport", str(config.vnc_port)],
                config.x11vnc_log_path,
            )
            processes.append(x11vnc)
            _wait_port(config.vnc_port)
            websockify = _spawn(
                ["websockify", f"127.0.0.1:{config.web_port}", f"127.0.0.1:{config.vnc_port}", "--web", str(config.novnc_web_dir)],
                config.websockify_log_path,
            )
            processes.append(websockify)
            _wait_port(config.web_port)
            return _capture_login(config, adapter)
    except BlockingIOError:
        _write_status(config, {**_read_status(config.status_path), "status": "error", "reason": "buyer_session_lock_busy", "finished_at": _now_text()})
        return 1
    except Exception:
        _write_status(config, {**_read_status(config.status_path), "status": "error", "reason": "buyer_recovery_runtime_error", "finished_at": _now_text()})
        return 1
    finally:
        for process in reversed(processes):
            _terminate(process)
        config.candidate_path.unlink(missing_ok=True)
        config.pid_path.unlink(missing_ok=True)


def _capture_login(config: BuyerRecoveryConfig, adapter: WbBuyerSessionAdapter) -> int:
    deadline = time.monotonic() + config.timeout_sec
    env = os.environ.copy()
    env["DISPLAY"] = config.display
    old_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = config.display
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context_args: dict[str, Any] = {"locale": "ru-RU", "viewport": {"width": 1500, "height": 820}}
                if config.session.storage_state_path.exists():
                    context_args["storage_state"] = str(config.session.storage_state_path)
                context = browser.new_context(**context_args)
                page = context.new_page()
                page.goto(config.session.buyer_url, wait_until="domcontentloaded")
                _write_status(
                    config,
                    {
                        **_read_status(config.status_path),
                        "status": "awaiting_login",
                        "reason": "buyer_login_window_ready",
                        "session": {"status": "missing", "valid": False},
                    },
                )
                last_session: Mapping[str, Any] = {}
                while time.monotonic() < deadline:
                    context.storage_state(path=str(config.candidate_path))
                    os.chmod(config.candidate_path, 0o600)
                    candidate = _probe_recovery_candidate(config, adapter, page)
                    last_session = candidate
                    if candidate.get("status") == "valid":
                        _write_status(config, {**_read_status(config.status_path), "status": "saving_session", "reason": "buyer_session_saving", "session": candidate})
                        adapter.persist_storage_state_atomically(config.candidate_path)
                        _write_status(config, {**_read_status(config.status_path), "status": "validating_session", "reason": "buyer_session_validating", "session": candidate})
                        validated = adapter.check_session(persist_fingerprint=True, acquire_lock=False)
                        if validated.get("status") != "valid":
                            _write_status(
                                config,
                                {
                                    **_read_status(config.status_path),
                                    "status": "error",
                                    "reason": str(validated.get("reason") or "buyer_session_post_save_validation_failed"),
                                    "finished_at": _now_text(),
                                    "session": validated,
                                },
                            )
                            return 1
                        _write_status(
                            config,
                            {
                                **_read_status(config.status_path),
                                "status": "completed",
                                "reason": "buyer_session_saved_and_validated",
                                "finished_at": _now_text(),
                                "session": validated,
                            },
                        )
                        return 0
                    if candidate.get("status") == "wrong_account":
                        _write_status(
                            config,
                            {
                                **_read_status(config.status_path),
                                "status": "awaiting_login",
                                "reason": "buyer_account_fingerprint_mismatch",
                                "session": candidate,
                            },
                        )
                    else:
                        _write_status(
                            config,
                            {
                                **_read_status(config.status_path),
                                "status": "awaiting_login",
                                "reason": _candidate_wait_reason(candidate),
                                "session": candidate,
                            },
                        )
                    page.wait_for_timeout(max(500, int(config.poll_sec * 1000)))
                _write_status(
                    config,
                    {
                        **_read_status(config.status_path),
                        "status": "timeout",
                        "reason": "buyer_login_timeout",
                        "finished_at": _now_text(),
                        "session": last_session,
                    },
                )
                return 1
            finally:
                browser.close()
    finally:
        if old_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = old_display


def _probe_recovery_candidate(
    config: BuyerRecoveryConfig,
    adapter: WbBuyerSessionAdapter,
    page: Any,
    *,
    fresh_adapter_factory: Any = WbBuyerSessionAdapter,
) -> dict[str, Any]:
    candidate = adapter.check_session(
        storage_state_path=config.candidate_path,
        persist_fingerprint=False,
        acquire_lock=False,
    )
    status = str(candidate.get("status") or "probe_error")
    if status in {"valid", "wrong_account"} or not _visible_login_completed(page):
        return candidate

    # WB rotates browser-side auth material immediately after the /lk redirect.
    # Revalidate this same secure snapshot after a short settle interval instead
    # of overwriting it again first.
    page.wait_for_timeout(min(3_000, max(750, int(config.poll_sec * 1000))))
    return fresh_adapter_factory(config=config.session).check_session(
        storage_state_path=config.candidate_path,
        persist_fingerprint=False,
        acquire_lock=False,
    )


def _visible_login_completed(page: Any) -> bool:
    try:
        parsed = urllib_parse.urlparse(str(page.url or ""))
    except Exception:
        return False
    host = str(parsed.hostname or "").lower()
    path = str(parsed.path or "").rstrip("/").lower()
    return (host == "wildberries.ru" or host.endswith(".wildberries.ru")) and (
        path == "/lk" or path.startswith("/lk/")
    )


def _candidate_wait_reason(candidate: Mapping[str, Any]) -> str:
    status = str(candidate.get("status") or "probe_error")
    reasons = {
        "missing": "buyer_storage_state_missing",
        "expired": "buyer_login_required",
        "login_redirect": "buyer_login_redirect",
        "security_challenge": "buyer_security_challenge",
        "probe_error": "buyer_session_probe_failed",
        "recovery_running": "buyer_session_lock_busy",
    }
    return reasons.get(status, "buyer_session_not_ready")


def build_macos_launcher_archive(
    config: BuyerRecoveryConfig,
    *,
    public_status_url: str,
    public_operator_url: str,
) -> tuple[bytes, str]:
    status = read_recovery_status(config, with_probe=False)
    run_id = str(status.get("run_id") or "")
    if not run_id or not status.get("running") or status.get("status") not in {"starting", "awaiting_login"}:
        raise RuntimeError("buyer recovery launcher is not ready")
    body = _launcher_script(
        config,
        run_id=run_id,
        public_status_url=public_status_url,
        public_operator_url=public_operator_url,
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("Открыть вход Wildberries.command")
        info.external_attr = 0o755 << 16
        archive.writestr(info, body)
    return buffer.getvalue(), f"wb-buyer-session-{run_id}.zip"


def _launcher_script(
    config: BuyerRecoveryConfig,
    *,
    run_id: str,
    public_status_url: str,
    public_operator_url: str,
) -> str:
    return "\n".join(
        [
            "#!/bin/bash",
            "set -euo pipefail",
            f"WEB_PORT={config.web_port}",
            f"SSH_DESTINATION={shlex.quote(config.ssh_destination)}",
            f"NOVNC_URL={shlex.quote(config.novnc_url)}",
            f"RUN_ID={shlex.quote(run_id)}",
            f"OPERATOR_URL={shlex.quote(public_operator_url)}",
            'SSH_LOG="${TMPDIR:-/tmp}/wb-buyer-recovery-ssh.log"',
            f"MAX_POLLS={max(1, int(config.timeout_sec / 3) + 20)}",
            'cleanup() { if [[ -n "${SSH_PID:-}" ]]; then kill "${SSH_PID}" >/dev/null 2>&1 || true; fi; }',
            "trap cleanup EXIT",
            'ssh -o ExitOnForwardFailure=yes -L "${WEB_PORT}:127.0.0.1:${WEB_PORT}" "${SSH_DESTINATION}" -N >"${SSH_LOG}" 2>&1 &',
            "SSH_PID=$!",
            'for attempt in $(seq 1 20); do if curl -fsS --max-time 2 "${NOVNC_URL}" >/dev/null 2>&1; then break; fi; sleep 1; done',
            'open "${NOVNC_URL}"',
            'echo "Окно входа Wildberries открыто для запуска ${RUN_ID}."',
            'MISSING_POLLS=0',
            'for attempt in $(seq 1 "${MAX_POLLS}"); do',
            '  if ! kill -0 "${SSH_PID}" >/dev/null 2>&1; then break; fi',
            '  if curl -fsS --max-time 2 "${NOVNC_URL}" >/dev/null 2>&1; then MISSING_POLLS=0; else MISSING_POLLS=$((MISSING_POLLS + 1)); fi',
            '  if [[ "${MISSING_POLLS}" -ge 3 ]]; then break; fi',
            '  sleep 3',
            'done',
            'echo "Окно входа Wildberries закрыто или время запуска завершилось."',
            'open "${OPERATOR_URL}" >/dev/null 2>&1 || true',
            "",
        ]
    )


def _write_status(config: BuyerRecoveryConfig, payload: Mapping[str, Any]) -> None:
    safe = {
        key: value
        for key, value in payload.items()
        if key in {"run_id", "status", "reason", "started_at", "finished_at", "deadline_at", "session"}
    }
    staged = config.session.state_dir / f".recovery-status-{uuid4().hex}.tmp"
    staged.write_text(json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(staged, 0o600)
    staged.replace(config.status_path)
    os.chmod(config.status_path, 0o600)


def _read_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "idle", "run_id": "", "reason": ""}
    return dict(value) if isinstance(value, Mapping) else {"status": "idle", "run_id": "", "reason": ""}


def _ensure_state_dir(config: BuyerRecoveryConfig) -> None:
    config.session.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config.session.state_dir, 0o700)


def _ensure_commands(config: BuyerRecoveryConfig) -> None:
    missing = [name for name in ("Xvfb", "x11vnc", "websockify") if shutil.which(name) is None]
    if missing:
        raise RuntimeError("buyer recovery dependencies missing")
    if not config.novnc_web_dir.exists():
        raise RuntimeError("buyer recovery noVNC assets missing")


def _spawn(args: list[str], log_path: Path, *, env: Mapping[str, str] | None = None) -> subprocess.Popen[Any]:
    merged = os.environ.copy()
    merged.update(dict(env or {}))
    log = _open_secure_log(log_path)
    return subprocess.Popen(args, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=merged)


def _open_secure_log(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    handle = path.open("ab", buffering=0)
    os.chmod(path, 0o600)
    return handle


def _wait_display(display: str) -> None:
    number = display.lstrip(":").split(".", 1)[0]
    path = Path(f"/tmp/.X11-unix/X{number}")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.2)
    raise RuntimeError("buyer recovery display did not start")


def _wait_port(port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("buyer recovery localhost port did not start")


def _terminate(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_running(pid: int) -> bool:
    try:
        waited_pid, _status = os.waitpid(pid, os.WNOHANG)
        if waited_pid == pid:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _supervisor_command(config: BuyerRecoveryConfig) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "supervise",
        "--state-dir",
        str(config.session.state_dir),
        "--storage-state-path",
        str(config.session.storage_state_path),
        "--display",
        config.display,
        "--vnc-port",
        str(config.vnc_port),
        "--web-port",
        str(config.web_port),
        "--timeout-sec",
        str(config.timeout_sec),
        "--poll-sec",
        str(config.poll_sec),
        "--ssh-destination",
        config.ssh_destination,
        "--novnc-web-dir",
        str(config.novnc_web_dir),
    ]


def _add_args(parser: argparse.ArgumentParser, config: BuyerRecoveryConfig) -> None:
    parser.add_argument("--state-dir", default=str(config.session.state_dir))
    parser.add_argument("--storage-state-path", default=str(config.session.storage_state_path))
    parser.add_argument("--display", default=config.display)
    parser.add_argument("--vnc-port", type=int, default=config.vnc_port)
    parser.add_argument("--web-port", type=int, default=config.web_port)
    parser.add_argument("--timeout-sec", type=int, default=config.timeout_sec)
    parser.add_argument("--poll-sec", type=float, default=config.poll_sec)
    parser.add_argument("--ssh-destination", default=config.ssh_destination)
    parser.add_argument("--novnc-web-dir", default=str(config.novnc_web_dir))


def _config_from_args(args: argparse.Namespace) -> BuyerRecoveryConfig:
    session = load_wb_buyer_session_config_from_env()
    session = WbBuyerSessionConfig(
        state_dir=Path(args.state_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        buyer_url=session.buyer_url,
        product_url_template=session.product_url_template,
        navigation_timeout_ms=session.navigation_timeout_ms,
        settle_timeout_ms=session.settle_timeout_ms,
    )
    return BuyerRecoveryConfig(
        session=session,
        display=args.display,
        vnc_port=args.vnc_port,
        web_port=args.web_port,
        timeout_sec=args.timeout_sec,
        poll_sec=args.poll_sec,
        ssh_destination=args.ssh_destination,
        novnc_web_dir=Path(args.novnc_web_dir),
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _now_text() -> str:
    return _now().isoformat()


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name) or default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name) or default))
    except ValueError:
        return default


if __name__ == "__main__":
    main()
