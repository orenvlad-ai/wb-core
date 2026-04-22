"""Server-side seller portal relogin session with temporary localhost-only noVNC access."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request


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
        return f"http://127.0.0.1:{self.web_port}/vnc.html?autoconnect=1&resize=remote"

    @property
    def ssh_tunnel_command(self) -> str:
        return f"ssh -L {self.web_port}:127.0.0.1:{self.web_port} {self.ssh_destination}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command_name in ("start", "status", "stop", "supervise"):
        sub = subparsers.add_parser(command_name)
        _add_common_args(sub)
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
        if status.get("status") in {"awaiting_login", "auth_confirmed", "success", "timeout", "error", "refresh_failed"}:
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
                "status": "awaiting_login",
                "message": "open the temporary localhost-only noVNC session and log in to seller portal; storage_state.json will be saved automatically",
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
) -> dict[str, Any]:
    probe = probe_fn or (lambda path: probe_storage_state(path, wb_bot_python=config.wb_bot_python))
    deadline = monotonic_fn() + config.timeout_sec
    context_kwargs: dict[str, Any] = {}
    if config.storage_state_path.exists():
        context_kwargs["storage_state"] = str(config.storage_state_path)
    previous_display = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = config.display
    try:
        with playwright_factory() as playwright:
            browser = playwright.chromium.launch(headless=False)
            try:
                context = browser.new_context(**context_kwargs)
                page = context.new_page()
                page.goto(config.seller_url, wait_until="domcontentloaded", timeout=60000)
                while monotonic_fn() < deadline:
                    context.storage_state(path=str(config.candidate_state_path))
                    probe_payload = probe(config.candidate_state_path)
                    if bool(probe_payload.get("ok")):
                        if config.storage_state_path.exists():
                            config.storage_state_path.replace(config.backup_state_path)
                        context.storage_state(path=str(config.storage_state_path))
                        validated_payload = probe(config.storage_state_path)
                        if not bool(validated_payload.get("ok")):
                            raise RuntimeError(
                                "saved seller storage_state.json did not pass validation after relogin"
                            )
                        return {
                            "status": "auth_confirmed",
                            "message": "seller portal session updated and validated; refresh is starting",
                            "authenticated_at": _iso_now(),
                            "storage_state_path": str(config.storage_state_path),
                            "last_probe": validated_payload,
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
                browser.close()
    finally:
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
    return payload


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
    )


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--storage-state-path", default=str(DEFAULT_STORAGE_STATE_PATH))
    parser.add_argument("--wb-bot-python", default=str(DEFAULT_WB_BOT_PYTHON))
    parser.add_argument("--display", default=DEFAULT_DISPLAY)
    parser.add_argument("--vnc-port", default=str(DEFAULT_VNC_PORT))
    parser.add_argument("--web-port", default=str(DEFAULT_WEB_PORT))
    parser.add_argument("--timeout-sec", default=str(DEFAULT_TIMEOUT_SEC))
    parser.add_argument("--poll-sec", default=str(DEFAULT_POLL_SEC))
    parser.add_argument("--refresh-timeout-sec", default=str(DEFAULT_REFRESH_TIMEOUT_SEC))
    parser.add_argument("--refresh-url", default=DEFAULT_REFRESH_URL)
    parser.add_argument("--job-url", default=DEFAULT_JOB_URL)
    parser.add_argument("--status-url", default=DEFAULT_STATUS_URL)
    parser.add_argument("--page-composition-url", default=DEFAULT_PAGE_URL)
    parser.add_argument("--seller-url", default=DEFAULT_SELLER_URL)
    parser.add_argument("--ssh-destination", default="selleros-root")
    parser.add_argument("--novnc-web-dir", default=str(DEFAULT_NOVNC_WEB_DIR))


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
        f"sleep 2; open '{config.novnc_url}'"
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
