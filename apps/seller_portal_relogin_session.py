"""Server-side seller portal relogin session with temporary localhost-only noVNC access."""

from __future__ import annotations

import argparse
import base64
from dataclasses import asdict, dataclass
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
import tempfile
import time
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from packages.adapters.web_source_current_sync import _SELLER_PORTAL_SESSION_PROBE_SCRIPT


UTC = timezone.utc
DEFAULT_STATE_DIR = Path("/opt/wb-core-runtime/state/seller_portal_relogin")
DEFAULT_STORAGE_STATE_PATH = Path("/opt/wb-web-bot/storage_state.json")
DEFAULT_WB_BOT_PYTHON = Path("/opt/wb-web-bot/venv/bin/python")
DEFAULT_DISPLAY = ":99"
DEFAULT_VNC_PORT = 45901
DEFAULT_WEB_PORT = 46080
DEFAULT_TIMEOUT_SEC = 1800
DEFAULT_POLL_SEC = 5.0
DEFAULT_REFRESH_TIMEOUT_SEC = 1800
DEFAULT_REFRESH_URL = "http://127.0.0.1:8765/v1/sheet-vitrina-v1/refresh"
DEFAULT_JOB_URL = "http://127.0.0.1:8765/v1/sheet-vitrina-v1/job"
DEFAULT_STATUS_URL = "http://127.0.0.1:8765/v1/sheet-vitrina-v1/status"
DEFAULT_PAGE_URL = "http://127.0.0.1:8765/v1/sheet-vitrina-v1/web-vitrina?surface=page_composition"
DEFAULT_SELLER_URL = "https://seller.wildberries.ru"
DEFAULT_NOVNC_WEB_DIR = Path("/usr/share/novnc")
DEFAULT_CANONICAL_SUPPLIER_ID = ""
DEFAULT_CANONICAL_SUPPLIER_LABEL = ""
DEFAULT_VISUAL_READY_TIMEOUT_SEC = 20.0
DEFAULT_VISUAL_READY_POLL_SEC = 0.5


@dataclass(frozen=True)
class ReloginSessionConfig:
    state_dir: Path = DEFAULT_STATE_DIR
    storage_state_path: Path = DEFAULT_STORAGE_STATE_PATH
    wb_bot_python: Path = DEFAULT_WB_BOT_PYTHON
    display: str = DEFAULT_DISPLAY
    vnc_port: int = DEFAULT_VNC_PORT
    web_port: int = DEFAULT_WEB_PORT
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    poll_sec: float = DEFAULT_POLL_SEC
    refresh_timeout_sec: int = DEFAULT_REFRESH_TIMEOUT_SEC
    refresh_url: str = DEFAULT_REFRESH_URL
    job_url: str = DEFAULT_JOB_URL
    status_url: str = DEFAULT_STATUS_URL
    page_composition_url: str = DEFAULT_PAGE_URL
    seller_url: str = DEFAULT_SELLER_URL
    ssh_destination: str = "selleros-root"
    novnc_web_dir: Path = DEFAULT_NOVNC_WEB_DIR
    canonical_supplier_id: str = DEFAULT_CANONICAL_SUPPLIER_ID
    canonical_supplier_label: str = DEFAULT_CANONICAL_SUPPLIER_LABEL

    @property
    def status_path(self) -> Path:
        return self.state_dir / "session_status.json"

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "supervisor.pid"

    @property
    def candidate_state_path(self) -> Path:
        return self.state_dir / "candidate_storage_state.json"

    @property
    def backup_state_path(self) -> Path:
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        return self.state_dir / f"storage_state.backup.{timestamp}.json"

    @property
    def xvfb_log_path(self) -> Path:
        return self.state_dir / "xvfb.log"

    @property
    def openbox_log_path(self) -> Path:
        return self.state_dir / "openbox.log"

    @property
    def x11vnc_log_path(self) -> Path:
        return self.state_dir / "x11vnc.log"

    @property
    def websockify_log_path(self) -> Path:
        return self.state_dir / "websockify.log"

    @property
    def supervisor_log_path(self) -> Path:
        return self.state_dir / "supervisor.log"

    @property
    def novnc_url(self) -> str:
        query = urllib_parse.urlencode(
            {
                "autoconnect": "1",
                "resize": "remote",
                "path": "websockify",
                "reconnect": "1",
            }
        )
        return f"http://127.0.0.1:{self.web_port}/vnc.html?{query}"

    @property
    def ssh_tunnel_command(self) -> str:
        return f"ssh -L {self.web_port}:127.0.0.1:{self.web_port} {self.ssh_destination}"

    @property
    def canonical_supplier_configured(self) -> bool:
        return bool(str(self.canonical_supplier_id or "").strip())


def main() -> None:
    default_config = load_relogin_session_config_from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command_name in ("start", "status", "stop", "supervise"):
        sub = subparsers.add_parser(command_name)
        _add_common_args(sub, default_config=default_config)
        if command_name == "start":
            sub.add_argument("--replace", action="store_true", help="Stop an existing relogin session before starting a new one.")
        if command_name == "status":
            sub.add_argument("--probe", action="store_true", help="Run a fresh seller-session probe against the current storage state.")

    args = parser.parse_args()
    config = _config_from_args(args)

    if args.command == "start":
        payload = start_relogin_session(config, replace=args.replace)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.command == "status":
        payload = read_session_status(config, with_probe=args.probe)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.command == "stop":
        payload = stop_relogin_session(config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if args.command == "supervise":
        exit_code = supervise_relogin_session(config)
        raise SystemExit(exit_code)
    raise SystemExit(f"unsupported command: {args.command}")


def start_relogin_session(config: ReloginSessionConfig, *, replace: bool = False) -> dict[str, Any]:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    current = read_session_status(config, with_probe=False)
    if current.get("running"):
        if not replace:
            return current
        stop_relogin_session(config)

    _write_status(
        config,
        {
            "status": "starting",
            "message": "server-side seller relogin session is starting",
            "started_at": _iso_now(),
            "novnc_url": config.novnc_url,
            "ssh_tunnel_command": config.ssh_tunnel_command,
            "human_step": _build_macos_human_step(config),
            "storage_state_path": str(config.storage_state_path),
            "state_dir": str(config.state_dir),
        },
    )

    with config.supervisor_log_path.open("ab", buffering=0) as log_file:
        process = subprocess.Popen(
            _build_supervisor_command(config),
            cwd=str(ROOT),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    config.pid_path.write_text(str(process.pid), encoding="utf-8")

    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = read_session_status(config, with_probe=False)
        if status.get("status") in {"starting_visual_session", "awaiting_login", "auth_confirmed", "success", "timeout", "error", "refresh_failed"}:
            return status
        time.sleep(0.5)
    return read_session_status(config, with_probe=False)


def stop_relogin_session(config: ReloginSessionConfig) -> dict[str, Any]:
    pid = _read_pid(config.pid_path)
    if pid is not None and _pid_is_running(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _pid_is_running(pid):
            time.sleep(0.2)
        if _pid_is_running(pid):
            os.killpg(pid, signal.SIGKILL)
    if config.pid_path.exists():
        config.pid_path.unlink()
    payload = read_session_status(config, with_probe=False)
    payload["status"] = "stopped"
    payload["running"] = False
    payload["message"] = "seller relogin session stopped"
    _write_status(config, payload)
    return payload


def read_session_status(config: ReloginSessionConfig, *, with_probe: bool) -> dict[str, Any]:
    payload = _read_status(config.status_path)
    pid = _read_pid(config.pid_path)
    payload.setdefault("storage_state_path", str(config.storage_state_path))
    payload.setdefault("state_dir", str(config.state_dir))
    payload.setdefault("novnc_url", config.novnc_url)
    payload.setdefault("ssh_tunnel_command", config.ssh_tunnel_command)
    payload.setdefault("human_step", _build_macos_human_step(config))
    payload.setdefault("canonical_supplier_id", str(config.canonical_supplier_id or ""))
    payload.setdefault("canonical_supplier_label", str(config.canonical_supplier_label or ""))
    payload.setdefault("canonical_supplier_configured", config.canonical_supplier_configured)
    payload.setdefault("supplier_context", read_storage_state_supplier_context(config.storage_state_path))
    payload["supervisor_pid"] = pid
    payload["running"] = bool(pid and _pid_is_running(pid))
    if with_probe:
        payload["current_storage_probe"] = probe_storage_state(config.storage_state_path, wb_bot_python=config.wb_bot_python)
    return payload


def supervise_relogin_session(config: ReloginSessionConfig) -> int:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.pid_path.write_text(str(os.getpid()), encoding="utf-8")
    _ensure_required_commands(config)

    processes: list[subprocess.Popen[Any]] = []
    try:
        _write_status(
            config,
            {
                "status": "starting",
                "message": "starting virtual display and temporary localhost-only noVNC access",
                "started_at": _iso_now(),
                "deadline_at": _iso_at(_utc_now() + timedelta(seconds=config.timeout_sec)),
                "novnc_url": config.novnc_url,
                "ssh_tunnel_command": config.ssh_tunnel_command,
                "human_step": _build_macos_human_step(config),
                "storage_state_path": str(config.storage_state_path),
                "state_dir": str(config.state_dir),
                "supervisor_pid": os.getpid(),
            },
        )

        xvfb = _spawn(
            [
                "Xvfb",
                config.display,
                "-screen",
                "0",
                "1600x900x24",
                "-nolisten",
                "tcp",
            ],
            log_path=config.xvfb_log_path,
        )
        processes.append(xvfb)
        _wait_for_display_socket(config.display)

        openbox_path = _command_path("openbox")
        if openbox_path:
            openbox = _spawn(
                [openbox_path],
                log_path=config.openbox_log_path,
                env={"DISPLAY": config.display},
            )
            processes.append(openbox)

        x11vnc = _spawn(
            [
                "x11vnc",
                "-display",
                config.display,
                "-localhost",
                "-shared",
                "-forever",
                "-nopw",
                "-noxdamage",
                "-rfbport",
                str(config.vnc_port),
            ],
            log_path=config.x11vnc_log_path,
        )
        processes.append(x11vnc)
        _wait_for_port("127.0.0.1", config.vnc_port)

        websockify = _spawn(
            [
                "websockify",
                f"127.0.0.1:{config.web_port}",
                f"127.0.0.1:{config.vnc_port}",
                "--web",
                str(config.novnc_web_dir),
            ],
            log_path=config.websockify_log_path,
        )
        processes.append(websockify)
        _wait_for_port("127.0.0.1", config.web_port)

        _write_status(
            config,
            {
                "status": "starting_visual_session",
                "message": "starting headed Chromium in the temporary localhost-only noVNC session",
                "started_at": _iso_now(),
                "deadline_at": _iso_at(_utc_now() + timedelta(seconds=config.timeout_sec)),
                "novnc_url": config.novnc_url,
                "ssh_tunnel_command": config.ssh_tunnel_command,
                "human_step": _build_macos_human_step(config),
                "storage_state_path": str(config.storage_state_path),
                "state_dir": str(config.state_dir),
                "supervisor_pid": os.getpid(),
            },
        )

        capture_result = run_login_capture(config)
        if capture_result["status"] != "auth_confirmed":
            _write_status(config, capture_result)
            return 1
        _write_status(config, capture_result)

        refresh_result = trigger_refresh_and_wait(config)
        final_status = "success" if refresh_result["status"] == "success" else "refresh_failed"
        _write_status(
            config,
            {
                **capture_result,
                "status": final_status,
                "message": refresh_result["message"],
                "refresh_result": refresh_result,
                "finished_at": _iso_now(),
            },
        )
        return 0 if final_status == "success" else 1
    except Exception as exc:
        _write_status(
            config,
            {
                "status": "error",
                "message": str(exc),
                "finished_at": _iso_now(),
                "supervisor_pid": os.getpid(),
                "novnc_url": config.novnc_url,
                "ssh_tunnel_command": config.ssh_tunnel_command,
                "human_step": _build_macos_human_step(config),
                "storage_state_path": str(config.storage_state_path),
                "state_dir": str(config.state_dir),
            },
        )
        return 1
    finally:
        for process in reversed(processes):
            _terminate_process(process)
        if config.pid_path.exists():
            config.pid_path.unlink()


def run_login_capture(
    config: ReloginSessionConfig,
    *,
    probe_fn: Callable[[Path], dict[str, Any]] | None = None,
    playwright_factory: Callable[[], Any] = sync_playwright,
    sleep_fn: Callable[[float], None] = time.sleep,
    monotonic_fn: Callable[[], float] = time.monotonic,
    visual_ready_fn: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    probe = probe_fn or (lambda path: probe_storage_state(path, wb_bot_python=config.wb_bot_python))
    visual_ready = visual_ready_fn or _display_has_visible_content
    deadline = monotonic_fn() + config.timeout_sec
    profile_dir = Path(tempfile.mkdtemp(prefix="seller-relogin-profile-", dir=str(config.state_dir)))
    previous_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = config.display
    try:
        with playwright_factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--start-maximized"],
            )
            try:
                _hydrate_persistent_context_from_storage_state(context, config.storage_state_path)
                page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
                page.goto(config.seller_url, wait_until="domcontentloaded", timeout=60000)
                page.bring_to_front()
                _wait_for_visual_materialization(
                    config.display,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                    visual_ready_fn=visual_ready,
                )
                while monotonic_fn() < deadline:
                    context.storage_state(path=str(config.candidate_state_path))
                    organization_switch_applied = False
                    if config.canonical_supplier_configured:
                        supplier_context = read_storage_state_supplier_context(config.candidate_state_path)
                        if not _supplier_context_matches_canonical_supplier(supplier_context, config):
                            _rewrite_storage_state_supplier(
                                config.candidate_state_path,
                                supplier_id=str(config.canonical_supplier_id or "").strip(),
                            )
                            organization_switch_applied = True
                    probe_payload = probe(config.candidate_state_path)
                    if bool(probe_payload.get("ok")):
                        organization_confirmed = _probe_matches_canonical_supplier(probe_payload, config)
                        if config.canonical_supplier_configured and not organization_confirmed:
                            return {
                                "status": "wrong_organization",
                                "message": _wrong_organization_message(config, probe_payload),
                                "authenticated_at": _iso_now(),
                                "storage_state_path": str(config.storage_state_path),
                                "last_probe": probe_payload,
                                "supplier_context": read_storage_state_supplier_context(config.candidate_state_path),
                                "organization_confirmed": False,
                                "organization_switch_applied": organization_switch_applied,
                                "canonical_supplier_id": str(config.canonical_supplier_id or ""),
                                "canonical_supplier_label": str(config.canonical_supplier_label or ""),
                                "novnc_url": config.novnc_url,
                                "ssh_tunnel_command": config.ssh_tunnel_command,
                                "human_step": _build_macos_human_step(config),
                            }
                        if config.storage_state_path.exists():
                            config.storage_state_path.replace(config.backup_state_path)
                        context.storage_state(path=str(config.storage_state_path))
                        if organization_switch_applied:
                            _rewrite_storage_state_supplier(
                                config.storage_state_path,
                                supplier_id=str(config.canonical_supplier_id or "").strip(),
                            )
                        validated_payload = probe(config.storage_state_path)
                        if not bool(validated_payload.get("ok")):
                            raise RuntimeError(
                                "saved seller storage_state.json did not pass validation after relogin"
                            )
                        organization_confirmed = _probe_matches_canonical_supplier(validated_payload, config)
                        if config.canonical_supplier_configured and not organization_confirmed:
                            return {
                                "status": "wrong_organization",
                                "message": _wrong_organization_message(config, validated_payload),
                                "authenticated_at": _iso_now(),
                                "storage_state_path": str(config.storage_state_path),
                                "last_probe": validated_payload,
                                "supplier_context": read_storage_state_supplier_context(config.storage_state_path),
                                "organization_confirmed": False,
                                "organization_switch_applied": organization_switch_applied,
                                "canonical_supplier_id": str(config.canonical_supplier_id or ""),
                                "canonical_supplier_label": str(config.canonical_supplier_label or ""),
                                "novnc_url": config.novnc_url,
                                "ssh_tunnel_command": config.ssh_tunnel_command,
                                "human_step": _build_macos_human_step(config),
                            }
                        return {
                            "status": "auth_confirmed",
                            "message": (
                                "seller portal session updated, canonical organization confirmed, and refresh is starting"
                                if config.canonical_supplier_configured
                                else "seller portal session updated and validated; refresh is starting"
                            ),
                            "authenticated_at": _iso_now(),
                            "storage_state_path": str(config.storage_state_path),
                            "last_probe": validated_payload,
                            "supplier_context": read_storage_state_supplier_context(config.storage_state_path),
                            "organization_confirmed": organization_confirmed,
                            "organization_switch_applied": organization_switch_applied,
                            "canonical_supplier_id": str(config.canonical_supplier_id or ""),
                            "canonical_supplier_label": str(config.canonical_supplier_label or ""),
                            "novnc_url": config.novnc_url,
                            "ssh_tunnel_command": config.ssh_tunnel_command,
                            "human_step": _build_macos_human_step(config),
                        }
                    _write_status(
                        config,
                        {
                            "status": "awaiting_login",
                            "message": "log in to seller portal in the temporary noVNC session; storage_state.json will be saved automatically",
                            "started_at": _iso_now(),
                            "deadline_at": _iso_at(_utc_now() + timedelta(seconds=max(0, int(deadline - monotonic_fn())))),
                            "last_probe": probe_payload,
                            "novnc_url": config.novnc_url,
                            "ssh_tunnel_command": config.ssh_tunnel_command,
                            "human_step": _build_macos_human_step(config),
                            "storage_state_path": str(config.storage_state_path),
                        },
                    )
                    sleep_fn(config.poll_sec)
            finally:
                context.close()
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
        if previous_display is None:
            os.environ.pop("DISPLAY", None)
        else:
            os.environ["DISPLAY"] = previous_display

    return {
        "status": "timeout",
        "message": "seller portal relogin timed out before authentication was confirmed",
        "finished_at": _iso_now(),
        "novnc_url": config.novnc_url,
        "ssh_tunnel_command": config.ssh_tunnel_command,
        "human_step": _build_macos_human_step(config),
        "storage_state_path": str(config.storage_state_path),
    }


def probe_storage_state(storage_state_path: Path, *, wb_bot_python: Path) -> dict[str, Any]:
    if not storage_state_path.exists():
        return {
            "ok": False,
            "status": "seller_portal_session_missing",
            "message": "storage_state.json is missing",
        }
    probe = subprocess.run(
        [
            str(wb_bot_python),
            "-c",
            _SELLER_PORTAL_SESSION_PROBE_SCRIPT,
            str(storage_state_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    stdout = str(probe.stdout or "").strip()
    stderr = str(probe.stderr or "").strip()
    payload: dict[str, Any]
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        payload = {
            "ok": False,
            "status": "seller_portal_session_probe_failed",
            "message": "probe did not return valid JSON",
        }
    payload["returncode"] = probe.returncode
    if stderr:
        payload["stderr_tail"] = stderr[-1000:]
    payload.setdefault("supplier_context", read_storage_state_supplier_context(storage_state_path))
    return payload


def load_relogin_session_config_from_env() -> ReloginSessionConfig:
    return ReloginSessionConfig(
        state_dir=Path(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_STATE_DIR", DEFAULT_STATE_DIR)).strip()
            or str(DEFAULT_STATE_DIR)
        ).expanduser(),
        storage_state_path=Path(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_STORAGE_STATE_PATH", DEFAULT_STORAGE_STATE_PATH)).strip()
            or str(DEFAULT_STORAGE_STATE_PATH)
        ).expanduser(),
        wb_bot_python=Path(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_WB_BOT_PYTHON", DEFAULT_WB_BOT_PYTHON)).strip()
            or str(DEFAULT_WB_BOT_PYTHON)
        ).expanduser(),
        display=str(os.environ.get("SELLER_PORTAL_RELOGIN_DISPLAY", DEFAULT_DISPLAY)).strip() or DEFAULT_DISPLAY,
        vnc_port=int(str(os.environ.get("SELLER_PORTAL_RELOGIN_VNC_PORT", DEFAULT_VNC_PORT)).strip() or DEFAULT_VNC_PORT),
        web_port=int(str(os.environ.get("SELLER_PORTAL_RELOGIN_WEB_PORT", DEFAULT_WEB_PORT)).strip() or DEFAULT_WEB_PORT),
        timeout_sec=int(str(os.environ.get("SELLER_PORTAL_RELOGIN_TIMEOUT_SEC", DEFAULT_TIMEOUT_SEC)).strip() or DEFAULT_TIMEOUT_SEC),
        poll_sec=float(str(os.environ.get("SELLER_PORTAL_RELOGIN_POLL_SEC", DEFAULT_POLL_SEC)).strip() or DEFAULT_POLL_SEC),
        refresh_timeout_sec=int(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_REFRESH_TIMEOUT_SEC", DEFAULT_REFRESH_TIMEOUT_SEC)).strip()
            or DEFAULT_REFRESH_TIMEOUT_SEC
        ),
        refresh_url=str(os.environ.get("SELLER_PORTAL_RELOGIN_REFRESH_URL", DEFAULT_REFRESH_URL)).strip() or DEFAULT_REFRESH_URL,
        job_url=str(os.environ.get("SELLER_PORTAL_RELOGIN_JOB_URL", DEFAULT_JOB_URL)).strip() or DEFAULT_JOB_URL,
        status_url=str(os.environ.get("SELLER_PORTAL_RELOGIN_STATUS_URL", DEFAULT_STATUS_URL)).strip() or DEFAULT_STATUS_URL,
        page_composition_url=(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_PAGE_COMPOSITION_URL", DEFAULT_PAGE_URL)).strip()
            or DEFAULT_PAGE_URL
        ),
        seller_url=str(os.environ.get("SELLER_PORTAL_RELOGIN_SELLER_URL", DEFAULT_SELLER_URL)).strip() or DEFAULT_SELLER_URL,
        ssh_destination=(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_SSH_DESTINATION", "selleros-root")).strip()
            or "selleros-root"
        ),
        novnc_web_dir=Path(
            str(os.environ.get("SELLER_PORTAL_RELOGIN_NOVNC_WEB_DIR", DEFAULT_NOVNC_WEB_DIR)).strip()
            or str(DEFAULT_NOVNC_WEB_DIR)
        ).expanduser(),
        canonical_supplier_id=(
            str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_ID", DEFAULT_CANONICAL_SUPPLIER_ID)).strip()
        ),
        canonical_supplier_label=(
            str(os.environ.get("SELLER_PORTAL_CANONICAL_SUPPLIER_LABEL", DEFAULT_CANONICAL_SUPPLIER_LABEL)).strip()
        ),
    )


def build_macos_launcher_archive(
    config: ReloginSessionConfig,
    *,
    public_status_url: str,
    public_operator_url: str,
) -> tuple[bytes, str]:
    script_body = _build_macos_launcher_script(
        config,
        public_status_url=public_status_url,
        public_operator_url=public_operator_url,
    )
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo("seller-portal-relogin.command")
        info.external_attr = 0o755 << 16
        archive.writestr(info, script_body)
    return archive_buffer.getvalue(), "seller-portal-relogin-macos.zip"


def _hydrate_persistent_context_from_storage_state(context: Any, storage_state_path: Path) -> None:
    if not storage_state_path.exists():
        return
    try:
        payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return

    cookies = [item for item in (payload.get("cookies") or []) if isinstance(item, dict)]
    if cookies:
        try:
            context.add_cookies(cookies)
        except Exception:
            pass

    origins = [item for item in (payload.get("origins") or []) if isinstance(item, dict)]
    if not origins:
        return

    page = context.pages[0] if getattr(context, "pages", None) else context.new_page()
    for origin_payload in origins:
        origin = str(origin_payload.get("origin") or "").strip()
        if not origin:
            continue
        local_storage = []
        for item in origin_payload.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            local_storage.append(
                {
                    "name": name,
                    "value": str(item.get("value") or ""),
                }
            )
        if not local_storage:
            continue
        try:
            page.goto(origin, wait_until="domcontentloaded", timeout=30000)
            page.evaluate(
                """(items) => {
                    for (const item of items) {
                        window.localStorage.setItem(item.name, item.value);
                    }
                }""",
                local_storage,
            )
        except Exception:
            continue


def _wait_for_visual_materialization(
    display: str,
    *,
    sleep_fn: Callable[[float], None],
    monotonic_fn: Callable[[], float],
    visual_ready_fn: Callable[[str], bool],
    timeout_sec: float = DEFAULT_VISUAL_READY_TIMEOUT_SEC,
) -> None:
    deadline = monotonic_fn() + timeout_sec
    while monotonic_fn() < deadline:
        if visual_ready_fn(display):
            return
        sleep_fn(DEFAULT_VISUAL_READY_POLL_SEC)
    raise RuntimeError("headed Chromium did not materialize a visible X11 window in time")


def _display_has_visible_content(display: str) -> bool:
    try:
        from PIL import ImageGrab
    except Exception:
        return True
    image = ImageGrab.grab(xdisplay=display)
    extrema = image.convert("L").getextrema()
    return bool(extrema) and int(extrema[1]) > 0


def trigger_refresh_and_wait(config: ReloginSessionConfig) -> dict[str, Any]:
    refresh_request = urllib_request.Request(
        config.refresh_url,
        data=json.dumps({"async": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(refresh_request, timeout=30) as response:
            refresh_payload = json.load(response)
    except urllib_error.HTTPError as exc:
        return {
            "status": "error",
            "message": f"refresh trigger failed: HTTP {exc.code}",
        }

    job_id = str(refresh_payload.get("job_id") or "").strip()
    if not job_id:
        return {
            "status": "error",
            "message": "refresh trigger returned no job_id",
            "payload": refresh_payload,
        }

    deadline = time.monotonic() + config.refresh_timeout_sec
    last_job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with urllib_request.urlopen(f"{config.job_url}?{urllib_parse.urlencode({'job_id': job_id})}", timeout=30) as response:
            last_job = json.load(response)
        job_status = str(last_job.get("status") or "").strip().lower()
        if job_status in {"completed", "failed"}:
            break
        time.sleep(2)

    post_status: dict[str, Any] = {}
    post_page: dict[str, Any] = {}
    for url, target in ((config.status_url, post_status), (config.page_composition_url, post_page)):
        try:
            with urllib_request.urlopen(url, timeout=30) as response:
                payload = json.load(response)
            target.update(payload)
        except Exception:
            continue

    job_status = str(last_job.get("status") or "").strip().lower()
    if job_status != "completed":
        return {
            "status": "error",
            "message": "seller session updated, but post-login refresh did not complete successfully",
            "job_id": job_id,
            "job": last_job,
        }
    return {
        "status": "success",
        "message": "seller session updated, live refresh completed, and current surfaces were reread",
        "job_id": job_id,
        "job": last_job,
        "status_payload_excerpt": _compact_status_payload(post_status),
        "page_update_items_excerpt": _compact_page_payload(post_page),
    }


def _config_from_args(args: argparse.Namespace) -> ReloginSessionConfig:
    return ReloginSessionConfig(
        state_dir=Path(args.state_dir).expanduser(),
        storage_state_path=Path(args.storage_state_path).expanduser(),
        wb_bot_python=Path(args.wb_bot_python).expanduser(),
        display=str(args.display).strip(),
        vnc_port=int(args.vnc_port),
        web_port=int(args.web_port),
        timeout_sec=int(args.timeout_sec),
        poll_sec=float(args.poll_sec),
        refresh_timeout_sec=int(args.refresh_timeout_sec),
        refresh_url=str(args.refresh_url).strip(),
        job_url=str(args.job_url).strip(),
        status_url=str(args.status_url).strip(),
        page_composition_url=str(args.page_composition_url).strip(),
        seller_url=str(args.seller_url).strip(),
        ssh_destination=str(args.ssh_destination).strip(),
        novnc_web_dir=Path(args.novnc_web_dir).expanduser(),
        canonical_supplier_id=str(args.canonical_supplier_id).strip(),
        canonical_supplier_label=str(args.canonical_supplier_label).strip(),
    )


def _add_common_args(
    parser: argparse.ArgumentParser,
    *,
    default_config: ReloginSessionConfig,
) -> None:
    parser.add_argument("--state-dir", default=str(default_config.state_dir))
    parser.add_argument("--storage-state-path", default=str(default_config.storage_state_path))
    parser.add_argument("--wb-bot-python", default=str(default_config.wb_bot_python))
    parser.add_argument("--display", default=default_config.display)
    parser.add_argument("--vnc-port", default=str(default_config.vnc_port))
    parser.add_argument("--web-port", default=str(default_config.web_port))
    parser.add_argument("--timeout-sec", default=str(default_config.timeout_sec))
    parser.add_argument("--poll-sec", default=str(default_config.poll_sec))
    parser.add_argument("--refresh-timeout-sec", default=str(default_config.refresh_timeout_sec))
    parser.add_argument("--refresh-url", default=default_config.refresh_url)
    parser.add_argument("--job-url", default=default_config.job_url)
    parser.add_argument("--status-url", default=default_config.status_url)
    parser.add_argument("--page-composition-url", default=default_config.page_composition_url)
    parser.add_argument("--seller-url", default=default_config.seller_url)
    parser.add_argument("--ssh-destination", default=default_config.ssh_destination)
    parser.add_argument("--novnc-web-dir", default=str(default_config.novnc_web_dir))
    parser.add_argument("--canonical-supplier-id", default=default_config.canonical_supplier_id)
    parser.add_argument("--canonical-supplier-label", default=default_config.canonical_supplier_label)


def _build_supervisor_command(config: ReloginSessionConfig) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "supervise",
        "--state-dir",
        str(config.state_dir),
        "--storage-state-path",
        str(config.storage_state_path),
        "--wb-bot-python",
        str(config.wb_bot_python),
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
        "--refresh-timeout-sec",
        str(config.refresh_timeout_sec),
        "--refresh-url",
        config.refresh_url,
        "--job-url",
        config.job_url,
        "--status-url",
        config.status_url,
        "--page-composition-url",
        config.page_composition_url,
        "--seller-url",
        config.seller_url,
        "--ssh-destination",
        config.ssh_destination,
        "--novnc-web-dir",
        str(config.novnc_web_dir),
        "--canonical-supplier-id",
        str(config.canonical_supplier_id or ""),
        "--canonical-supplier-label",
        str(config.canonical_supplier_label or ""),
    ]


def _compact_status_payload(payload: dict[str, Any]) -> dict[str, Any]:
    source_outcomes = payload.get("source_outcomes") or []
    excerpt = []
    for item in source_outcomes:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_key") or "") not in {"seller_funnel_snapshot", "web_source_snapshot"}:
            continue
        excerpt.append(
            {
                "source_key": item.get("source_key"),
                "status": item.get("status"),
                "reason": item.get("reason"),
            }
        )
    return {
        "semantic_status": payload.get("semantic_status"),
        "semantic_reason": payload.get("semantic_reason"),
        "snapshot_id": payload.get("snapshot_id"),
        "source_outcomes": excerpt,
    }


def _compact_page_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    surface = payload.get("activity_surface") or {}
    update = surface.get("update_summary") or {}
    items = update.get("items") or []
    compact = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("source_key") or "") not in {"seller_funnel_snapshot", "web_source_snapshot"}:
            continue
        compact.append(
            {
                "source_key": item.get("source_key"),
                "tone": item.get("tone"),
                "reason_ru": item.get("reason_ru"),
            }
        )
    return compact


def _write_status(config: ReloginSessionConfig, payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload.setdefault("novnc_url", config.novnc_url)
    payload.setdefault("ssh_tunnel_command", config.ssh_tunnel_command)
    payload.setdefault("human_step", _build_macos_human_step(config))
    payload.setdefault("storage_state_path", str(config.storage_state_path))
    payload.setdefault("state_dir", str(config.state_dir))
    payload.setdefault("canonical_supplier_id", str(config.canonical_supplier_id or ""))
    payload.setdefault("canonical_supplier_label", str(config.canonical_supplier_label or ""))
    payload.setdefault("canonical_supplier_configured", config.canonical_supplier_configured)
    payload.setdefault("supplier_context", read_storage_state_supplier_context(config.storage_state_path))
    payload.setdefault("updated_at", _iso_now())
    temp_path = config.status_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(config.status_path)


def _read_status(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "error", "message": "status file is not valid JSON"}
    return payload if isinstance(payload, dict) else {}


def _spawn(args: list[str], *, log_path: Path, env: dict[str, str] | None = None) -> subprocess.Popen[Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("ab", buffering=0)
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.Popen(args, stdout=log_file, stderr=subprocess.STDOUT, env=merged_env)


def _ensure_required_commands(config: ReloginSessionConfig) -> None:
    missing = []
    for command in ("Xvfb", "x11vnc", "websockify"):
        if not _command_path(command):
            missing.append(command)
    if missing:
        raise RuntimeError(f"missing host recovery packages: {', '.join(missing)}")
    if not config.novnc_web_dir.exists():
        raise RuntimeError(f"noVNC web dir not found: {config.novnc_web_dir}")
    if not config.wb_bot_python.exists():
        raise RuntimeError(f"wb-web-bot python not found: {config.wb_bot_python}")


def _command_path(command: str) -> str:
    return str(shutil_which(command) or "")


def shutil_which(command: str) -> str | None:
    for raw_path in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(raw_path) / command
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _wait_for_display_socket(display: str, *, timeout_sec: float = 15.0) -> None:
    display_number = display.replace(":", "", 1)
    socket_path = Path("/tmp/.X11-unix") / f"X{display_number}"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        time.sleep(0.2)
    raise RuntimeError(f"X display did not start in time: {display}")


def _wait_for_port(host: str, port: int, *, timeout_sec: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError(f"port did not become ready: {host}:{port}")


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _build_macos_human_step(config: ReloginSessionConfig) -> str:
    return (
        f"ssh -L {config.web_port}:127.0.0.1:{config.web_port} {config.ssh_destination} -N >/tmp/seller-portal-relogin-ssh.log 2>&1 & "
        f"for i in $(seq 1 20); do curl -fsS --max-time 2 '{config.novnc_url}' >/dev/null 2>&1 && break; sleep 1; done; "
        f"open '{config.novnc_url}'"
    )


def _build_macos_launcher_script(
    config: ReloginSessionConfig,
    *,
    public_status_url: str,
    public_operator_url: str,
) -> str:
    final_statuses = "success refresh_failed wrong_organization timeout error stopped"
    return "\n".join(
        [
            "#!/bin/bash",
            "set -euo pipefail",
            f"WEB_PORT={config.web_port}",
            f"SSH_DESTINATION={shlex.quote(config.ssh_destination)}",
            f"NOVNC_URL={shlex.quote(config.novnc_url)}",
            f"STATUS_URL={shlex.quote(public_status_url)}",
            f"OPERATOR_URL={shlex.quote(public_operator_url)}",
            'SSH_LOG="${TMPDIR:-/tmp}/seller-portal-relogin-ssh.log"',
            "",
            "cleanup() {",
            '  if [[ -n "${SSH_PID:-}" ]]; then',
            '    kill "${SSH_PID}" >/dev/null 2>&1 || true',
            "  fi",
            "}",
            "trap cleanup EXIT",
            "",
            'echo "Поднимаем SSH tunnel к seller recovery session..."',
            'ssh -o ExitOnForwardFailure=yes -L "${WEB_PORT}:127.0.0.1:${WEB_PORT}" "${SSH_DESTINATION}" -N >"${SSH_LOG}" 2>&1 &',
            "SSH_PID=$!",
            "",
            "for attempt in $(seq 1 20); do",
            '  if curl -fsS --max-time 2 "${NOVNC_URL}" >/dev/null 2>&1; then',
            "    break",
            "  fi",
            "  sleep 1",
            "done",
            'open "${NOVNC_URL}"',
            'echo "Окно noVNC открыто. Войдите в seller portal и дождитесь завершения восстановления."',
            "",
            "while true; do",
            '  STATUS_JSON="$(curl -fsS "${STATUS_URL}" 2>/dev/null || true)"',
            "  STATUS=\"$(printf '%s' \"${STATUS_JSON}\" | python3 -c 'import json, sys; raw = sys.stdin.read().strip(); "
            "print((json.loads(raw).get(\"status\", \"\") if raw else \"\"), end=\"\")' 2>/dev/null || true)\"",
            f'  if [[ " {final_statuses} " == *" ${{STATUS}} "* ]]; then',
            '    echo "Seller recovery завершился со статусом: ${STATUS:-unknown}"',
            '    open "${OPERATOR_URL}" >/dev/null 2>&1 || true',
            "    break",
            "  fi",
            "  sleep 3",
            "done",
            "",
        ]
    )


def read_storage_state_supplier_context(storage_state_path: Path) -> dict[str, Any]:
    if not storage_state_path.exists():
        return {}
    try:
        payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    cookies = payload.get("cookies") or []
    origins = payload.get("origins") or []
    current_supplier_id = ""
    current_supplier_external_id = ""
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip()
        if name == "x-supplier-id":
            current_supplier_id = str(cookie.get("value") or "").strip()
        if name == "x-supplier-id-external":
            current_supplier_external_id = str(cookie.get("value") or "").strip()
    analytics_supplier_id = ""
    analytics_user_id = ""
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if str(origin.get("origin") or "").strip() != "https://seller.wildberries.ru":
            continue
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() != "analytics-external-data":
                continue
            raw_value = str(item.get("value") or "")
            try:
                decoded = base64.b64decode(raw_value).decode("utf-8")
                decoded_payload = json.loads(decoded)
            except Exception:
                decoded_payload = {}
            analytics_supplier_id = str(decoded_payload.get("idSupplier") or "").strip()
            analytics_user_id = str(decoded_payload.get("idUser") or "").strip()
            break
    unique_supplier_ids = sorted(
        {
            value
            for value in (
                current_supplier_id,
                current_supplier_external_id,
                analytics_supplier_id,
            )
            if value
        }
    )
    return {
        "current_supplier_id": current_supplier_id,
        "current_supplier_external_id": current_supplier_external_id,
        "analytics_supplier_id": analytics_supplier_id,
        "analytics_user_id": analytics_user_id,
        "unique_supplier_ids": unique_supplier_ids,
    }


def _rewrite_storage_state_supplier(storage_state_path: Path, *, supplier_id: str) -> None:
    payload = json.loads(storage_state_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("storage_state.json must contain a JSON object")
    supplier_id = str(supplier_id or "").strip()
    if not supplier_id:
        raise RuntimeError("canonical supplier id is empty")
    cookies = payload.get("cookies") or []
    cookie_names = {"x-supplier-id", "x-supplier-id-external"}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        if str(cookie.get("name") or "").strip() in cookie_names:
            cookie["value"] = supplier_id
    origins = payload.get("origins") or []
    for origin in origins:
        if not isinstance(origin, dict):
            continue
        if str(origin.get("origin") or "").strip() != "https://seller.wildberries.ru":
            continue
        local_storage = origin.get("localStorage") or []
        analytics_item: dict[str, Any] | None = None
        for item in local_storage:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "").strip() != "analytics-external-data":
                continue
            analytics_item = item
            decoded_payload: dict[str, Any]
            try:
                decoded_payload = json.loads(base64.b64decode(str(item.get("value") or "")).decode("utf-8"))
            except Exception:
                decoded_payload = {}
            decoded_payload["idSupplier"] = supplier_id
            item["value"] = base64.b64encode(
                json.dumps(decoded_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).decode("utf-8")
            break
        if analytics_item is None:
            local_storage.append(
                {
                    "name": "analytics-external-data",
                    "value": base64.b64encode(
                        json.dumps({"idSupplier": supplier_id}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    ).decode("utf-8"),
                }
            )
    storage_state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _supplier_context_matches_canonical_supplier(
    supplier_context: dict[str, Any] | None,
    config: ReloginSessionConfig,
) -> bool:
    if not config.canonical_supplier_configured:
        return True
    supplier_context = supplier_context or {}
    expected = str(config.canonical_supplier_id or "").strip()
    unique_ids = {
        str(value or "").strip()
        for value in (
            supplier_context.get("current_supplier_id"),
            supplier_context.get("current_supplier_external_id"),
            supplier_context.get("analytics_supplier_id"),
        )
        if str(value or "").strip()
    }
    return bool(unique_ids) and unique_ids == {expected}


def _probe_matches_canonical_supplier(
    probe_payload: dict[str, Any] | None,
    config: ReloginSessionConfig,
) -> bool:
    if not config.canonical_supplier_configured:
        return True
    payload = probe_payload or {}
    supplier_context = payload.get("supplier_context")
    if isinstance(supplier_context, dict):
        return _supplier_context_matches_canonical_supplier(supplier_context, config)
    return _supplier_context_matches_canonical_supplier(read_storage_state_supplier_context(config.storage_state_path), config)


def _wrong_organization_message(config: ReloginSessionConfig, probe_payload: dict[str, Any] | None) -> str:
    supplier_context = {}
    if isinstance(probe_payload, dict) and isinstance(probe_payload.get("supplier_context"), dict):
        supplier_context = dict(probe_payload.get("supplier_context") or {})
    current_supplier_id = (
        str(supplier_context.get("current_supplier_id") or "").strip()
        or str(supplier_context.get("analytics_supplier_id") or "").strip()
        or "unknown"
    )
    expected_supplier_id = str(config.canonical_supplier_id or "").strip() or "unknown"
    expected_label = str(config.canonical_supplier_label or "").strip()
    if expected_label:
        return (
            "seller portal session is authenticated, but canonical supplier was not confirmed: "
            f"expected_supplier_id={expected_supplier_id}; expected_supplier_label={expected_label}; "
            f"current_supplier_id={current_supplier_id}"
        )
    return (
        "seller portal session is authenticated, but canonical supplier was not confirmed: "
        f"expected_supplier_id={expected_supplier_id}; current_supplier_id={current_supplier_id}"
    )


def _read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iso_now() -> str:
    return _iso_at(_utc_now())


def _iso_at(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
