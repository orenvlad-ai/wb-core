"""Owner-scoped WB Buyer login in ordinary, sandboxed Chrome on the VPS.

Only a boolean visible-login result crosses the private DevTools pipe.  This
module never reads prices or writes the canonical Playwright buyer profile.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import pwd
import secrets
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping
from urllib import parse as urllib_parse
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wb_buyer_chrome_runtime as runtime  # noqa: E402
from apps import wb_buyer_session_recovery as legacy  # noqa: E402


UTC = timezone.utc
DISPLAY = ":98"
VNC_PORT = 45911
WEB_PORT = 46090
NOVNC_DIR = Path("/usr/share/novnc")
LOGIN_URL = "https://www.wildberries.ru/lk"
ACTIVE = {"starting", "awaiting_human", "validating_session", "stopping"}
FINAL = {"completed", "stopped", "timeout", "error"}
RUN_PREFIX = "buyer-recovery-chrome-"

# Chromium reads DevTools JSON from FD 3 and writes NUL-terminated replies to
# FD 4.  The browser is launched directly with this one extra switch; no
# Playwright launch arguments, TCP debugger, or browser extension are used.
SURFACE_EXPRESSION = """(() => {
  if (document.visibilityState !== "visible") return "hidden";
  const host = location.hostname.toLowerCase();
  const path = location.pathname.toLowerCase().replace(/\\/$/, "");
  const body = (document.body?.innerText || "").toLowerCase();
  if (["подозрительная активность", "подтвердите, что вы не робот", "captcha-support@rwb.ru", "почти готово", "suspicious activity", "verify you are human"].some(x => body.includes(x))) return "challenge";
  if (["код из смс", "введите код", "код подтверждения", "отправили код", "enter code", "verification code", "sent code"].some(x => body.includes(x))) return "sms";
  if (["введите номер телефона", "номер телефона", "получить код", "phone number", "receive code", "sign in with wb id"].some(x => body.includes(x))) return "phone";
  const onAccount = (host === "wildberries.ru" || host.endsWith(".wildberries.ru")) && (path === "/lk" || path.startsWith("/lk/"));
  // A cross-origin WB challenge can cover an already rendered /lk. Its text
  // is not available in body.innerText; a large visible frame blocks proof.
  const coveringFrame = [...document.querySelectorAll("iframe")].some(frame => {
    const box = frame.getBoundingClientRect();
    const style = getComputedStyle(frame);
    return style.display !== "none" && style.visibility === "visible" && Number(style.opacity) > 0 &&
      box.width >= 220 && box.height >= 180 && box.width * box.height >= 100000;
  });
  if (onAccount && coveringFrame) return "challenge";
  const account = ["мои заказы", "мои покупки", "история покупок", "личные данные", "мои адреса", "способы оплаты", "выйти из аккаунта"];
  const markers = account.filter(x => body.includes(x));
  return onAccount && document.readyState === "complete" && (markers.length >= 2 || body.includes("выйти из аккаунта")) ? "account" : "unknown";
})()"""


class BuyerChromeBusyError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _status_path() -> Path:
    return runtime.STATE / "recovery_status.json"


def _read() -> dict[str, Any]:
    try:
        value = json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"run_id": "", "status": "idle", "reason": "", "session": {}, "price": {"status": "not_checked"}}
    return dict(value) if isinstance(value, Mapping) else {"run_id": "", "status": "idle"}


@contextmanager
def _status_lock() -> Iterator[None]:
    state = runtime.STATE
    if not state.is_dir() or state.is_symlink():
        raise RuntimeError("Chrome auth state unavailable")
    descriptor = os.open(state / "status.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        if os.geteuid() == 0:
            user = pwd.getpwnam(runtime.USER)
            os.fchown(descriptor, user.pw_uid, user.pw_gid)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _write_unlocked(payload: Mapping[str, Any]) -> None:
    state = runtime.STATE
    if not state.is_dir() or state.is_symlink():
        raise RuntimeError("Chrome auth state unavailable")
    safe = {key: value for key, value in payload.items() if key in {
        "run_id", "status", "reason", "started_at", "deadline_at", "finished_at",
        "viewer_owner", "session", "price", "unit", "invocation_id", "login_confirmed",
    }}
    name = state / f".recovery-status-{uuid4().hex}.tmp"
    name.write_text(json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    os.chmod(name, 0o600)
    if os.geteuid() == 0:
        user = pwd.getpwnam(runtime.USER)
        os.chown(name, user.pw_uid, user.pw_gid)
    name.replace(_status_path())


def _write(payload: Mapping[str, Any]) -> None:
    with _status_lock():
        _write_unlocked(payload)


@contextmanager
def _shared_start_lock() -> Iterator[None]:
    normal = legacy.load_recovery_config_from_env()
    normal.session.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with normal.start_lock_path.open("a+") as handle:
        os.chmod(normal.start_lock_path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _systemd_properties(unit: str) -> dict[str, str]:
    command = ["systemctl", "show", unit, "-p", "ActiveState", "-p", "InvocationID", "--no-pager"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=4, check=False)
    if result.returncode:
        return {}
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _is_running(payload: Mapping[str, Any]) -> bool:
    unit = str(payload.get("unit") or "")
    run_id = str(payload.get("run_id") or "")
    if not run_id.startswith(RUN_PREFIX) or unit != f"wbc-{run_id}.service":
        return False
    props = _systemd_properties(unit)
    if props.get("ActiveState") not in {"active", "activating", "deactivating"}:
        return False
    recorded = str(payload.get("invocation_id") or "")
    return not recorded or recorded == props.get("InvocationID")


def raw_status(*, requested_run_id: str | None = None) -> dict[str, Any]:
    payload = _read()
    payload["running"] = _is_running(payload)
    if payload.get("status") in ACTIVE and not payload["running"]:
        if payload.get("status") == "starting" and _seconds_since(payload.get("started_at")) < 8:
            pass
        elif payload.get("status") == "stopping":
            payload.update(status="stopped", reason="buyer_recovery_stopped", finished_at=_now().isoformat())
            _write(payload)
        else:
            payload.update(status="error", reason="buyer_chrome_unexpected_exit", finished_at=_now().isoformat())
            _write(payload)
    if requested_run_id and requested_run_id != str(payload.get("run_id") or ""):
        return {"run_id": str(payload.get("run_id") or ""), "status": "error", "reason": "buyer_recovery_run_not_current", "running": payload["running"], "session": {}, "price": {"status": "not_checked"}}
    return payload


def _seconds_since(raw: Any) -> float:
    try:
        value = datetime.fromisoformat(str(raw or ""))
        return max(0.0, (_now() - value.astimezone(UTC)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def start(*, replace: bool = False, viewer_owner: str = "", viewer_expires_at: int | None = None) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RuntimeError("Chrome auth start requires root")
    if not viewer_owner or not viewer_expires_at:
        raise ValueError("viewer owner and expiry required")
    with _shared_start_lock():
        normal_config = legacy.load_recovery_config_from_env()
        if legacy.read_recovery_status(normal_config, with_probe=False).get("running"):
            raise BuyerChromeBusyError("buyer viewer already in use")
        current = raw_status()
        if current.get("running"):
            if str(current.get("viewer_owner") or "") != viewer_owner:
                raise BuyerChromeBusyError("buyer viewer controlled by another operator")
            if replace:
                raise BuyerChromeBusyError("wait for current Chrome login to stop")
            return current
        if not runtime.PACKAGE.is_file():
            raise RuntimeError("pinned Chrome package is not installed")
        if runtime._available(Path("/")) < runtime._root_reserve():
            raise RuntimeError("root storage reserve unavailable")
        user = pwd.getpwnam(runtime.USER)
        if runtime.PROFILE.stat().st_uid != user.pw_uid or runtime.PROFILE.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError("Chrome profile ownership invalid")
        if runtime.STATE.stat().st_uid != user.pw_uid or runtime.STATE.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError("Chrome auth state ownership invalid")
        if not NOVNC_DIR.is_dir() or any(shutil.which(name) is None for name in ("Xvfb", "xauth", "x11vnc", "websockify", "openbox", "systemd-run", "setpriv")):
            raise RuntimeError("Chrome viewer dependencies unavailable")
        if any(_port_open(port) for port in (VNC_PORT, WEB_PORT)):
            raise BuyerChromeBusyError("buyer viewer port busy")
        deadline = min(_now() + timedelta(minutes=30), datetime.fromtimestamp(viewer_expires_at, UTC))
        if deadline <= _now() + timedelta(seconds=30):
            raise ValueError("viewer session expires too soon")
        run_id = f"{RUN_PREFIX}{_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        unit = f"wbc-{run_id}.service"
        payload: dict[str, Any] = {
            "run_id": run_id, "status": "starting", "reason": "buyer_chrome_starting",
            "started_at": _now().isoformat(), "deadline_at": deadline.isoformat(),
            "viewer_owner": viewer_owner, "unit": unit, "invocation_id": "",
            "session": {"status": "recovery_running", "valid": False, "account_confirmed": False, "login_confirmed": False},
            "price": {"status": "not_checked"}, "login_confirmed": False,
        }
        _write(payload)
        duration = max(30, int((deadline - _now()).total_seconds()))
        command = [
            "systemd-run", f"--unit={unit.removesuffix('.service')}",
            "--property=KillMode=mixed", "--property=TimeoutStopSec=100s",
            f"--property=RuntimeMaxSec={duration}s", "--property=PrivateTmp=yes",
            "--property=NoNewPrivileges=no", "--property=StandardOutput=journal", "--property=StandardError=journal",
            "/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "PYTHONUNBUFFERED=1",
            sys.executable, str(Path(__file__).resolve()), "prepare-supervise", "--run-id", run_id,
        ]
        try:
            subprocess.run(command, capture_output=True, timeout=15, check=True)
        except Exception:
            _write({**payload, "status": "error", "reason": "buyer_chrome_start_failed", "finished_at": _now().isoformat()})
            raise
        props = _systemd_properties(unit)
        if not props.get("InvocationID"):
            raise RuntimeError("Chrome unit invocation unavailable")
        _write({**_read(), "invocation_id": props["InvocationID"]})
        return raw_status()


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def stop(*, requested_run_id: str | None = None) -> dict[str, Any]:
    with _shared_start_lock():
        payload = raw_status()
        run_id = str(payload.get("run_id") or "")
        if requested_run_id and requested_run_id != run_id:
            return {**payload, "status": "error", "reason": "buyer_recovery_run_not_current"}
        if payload.get("running"):
            should_stop = False
            with _status_lock():
                latest = _read()
                if latest.get("run_id") != run_id:
                    return {**payload, "status": "error", "reason": "buyer_recovery_run_not_current"}
                if latest.get("status") not in FINAL:
                    _write_unlocked({**latest, "status": "stopping", "reason": "buyer_chrome_stopping"})
                    should_stop = True
            if should_stop:
                subprocess.run(["systemctl", "stop", "--no-block", str(payload["unit"])], check=True, capture_output=True, timeout=8)
        elif payload.get("status") not in FINAL:
            _write({**payload, "status": "stopped", "reason": "buyer_recovery_stopped", "finished_at": _now().isoformat()})
        return raw_status()


class ChromePipe:
    def __init__(self, directory: Path) -> None:
        if not directory.is_dir() or directory.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError("Chrome pipe directory invalid")
        self.directory = directory
        self.input = directory / "in"
        self.output = directory / "out"
        os.mkfifo(self.input, 0o600)
        os.mkfifo(self.output, 0o600)
        self._read_fd = os.open(self.output, os.O_RDONLY | os.O_NONBLOCK)
        self._write_fd = -1
        self._buffer = bytearray()
        self._next_id = 1

    def connect(self) -> None:
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                self._write_fd = os.open(self.input, os.O_WRONLY | os.O_NONBLOCK)
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("Chrome private pipe unavailable")

    def close(self) -> None:
        os.close(self._read_fd)
        if self._write_fd >= 0:
            os.close(self._write_fd)
        shutil.rmtree(self.directory)

    def call(self, method: str, params: Mapping[str, Any] | None = None, *, session: str = "", timeout: float = 6) -> dict[str, Any]:
        identifier = self._next_id
        self._next_id += 1
        command: dict[str, Any] = {"id": identifier, "method": method}
        if params is not None:
            command["params"] = dict(params)
        if session:
            command["sessionId"] = session
        os.write(self._write_fd, json.dumps(command, separators=(",", ":")).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([self._read_fd], [], [], max(0, min(1, deadline - time.monotonic())))[0]:
                self._buffer.extend(os.read(self._read_fd, 65_536))
            if len(self._buffer) > 2_000_000:
                raise RuntimeError("Chrome pipe response too large")
            while b"\0" in self._buffer:
                raw, _, tail = self._buffer.partition(b"\0")
                self._buffer = bytearray(tail)
                response = json.loads(raw)
                if response.get("id") == identifier:
                    if "error" in response:
                        raise RuntimeError("Chrome pipe method failed")
                    return dict(response.get("result") or {})
        raise TimeoutError("Chrome pipe did not respond")

    def visible_surface(self) -> str:
        targets = self.call("Target.getTargets").get("targetInfos") or []
        visible: list[str] = []
        for target in targets[:30]:
            parsed = urllib_parse.urlparse(str(target.get("url") or ""))
            if target.get("type") != "page" or parsed.hostname not in {"wildberries.ru", "www.wildberries.ru", "id.wb.ru"}:
                continue
            session = str(self.call("Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}).get("sessionId") or "")
            if not session:
                continue
            try:
                result = self.call("Runtime.evaluate", {"expression": SURFACE_EXPRESSION, "returnByValue": True, "awaitPromise": False}, session=session)
                if result.get("exceptionDetails"):
                    continue
                value = result.get("result") if isinstance(result.get("result"), Mapping) else {}
                surface = str(value.get("value") or "unknown")
                if surface != "hidden":
                    visible.append(surface)
            finally:
                self.call("Target.detachFromTarget", {"sessionId": session})
        for surface in ("challenge", "sms", "phone"):
            if surface in visible:
                return surface
        if visible == ["account"]:
            return "account"
        return "unknown"


def _spawn(command: list[str], log_name: str, *, env: Mapping[str, str], capture: bool = True) -> subprocess.Popen[Any]:
    if not capture:
        # Chrome may print a navigation URL carrying one-time OAuth state.
        return subprocess.Popen(command, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log = (runtime.STATE / log_name).open("ab", buffering=0)
    os.chmod(runtime.STATE / log_name, 0o600)
    try:
        return subprocess.Popen(command, env=dict(env), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()


def _wait_display(process: subprocess.Popen[Any]) -> None:
    path = Path("/tmp/.X11-unix/X98")
    for _ in range(80):
        if path.is_socket() and process.poll() is None:
            return
        time.sleep(0.1)
    raise RuntimeError("Chrome display unavailable")


def _wait_port(port: int, process: subprocess.Popen[Any]) -> None:
    for _ in range(80):
        if _port_open(port) and process.poll() is None:
            return
        time.sleep(0.1)
    raise RuntimeError("Chrome viewer unavailable")


def _stop_process(process: subprocess.Popen[Any] | None, *, grace: float = 5) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _launch_chrome(chrome: Path, env: Mapping[str, str], *, run_id: str, generation: int) -> tuple[subprocess.Popen[Any], ChromePipe]:
    pipe = ChromePipe(Path(tempfile.mkdtemp(prefix=f".{run_id}-{generation}-", dir=runtime.STATE)))
    # tempfile creates 0700; Chrome inherits only FD 3 and FD 4 through exec.
    command = [
        "/bin/bash", "-c",
        'exec 3<>"$1"; exec 4<>"$2"; exec "$3" "--user-data-dir=$4" --remote-debugging-pipe "$5"',
        "chrome-pipe", str(pipe.input), str(pipe.output), str(chrome), str(runtime.PROFILE), LOGIN_URL,
    ]
    process = _spawn(command, "chrome.log", env=env, capture=False)
    try:
        pipe.connect()
        pipe.call("Browser.getVersion", timeout=15)
        return process, pipe
    except Exception:
        try:
            _stop_chrome(process)
        finally:
            pipe.close()
        raise


def _safe_status_update(run_id: str, **fields: Any) -> None:
    with _status_lock():
        current = _read()
        if str(current.get("run_id") or "") != run_id or current.get("status") in FINAL:
            return
        # Stop/logout owns this latch. No late browser observation may reopen
        # viewer access or publish a completed login after cancellation.
        if current.get("status") == "stopping":
            if fields.get("status") == "error":
                _write_unlocked({**current, "status": "error", "reason": fields.get("reason", "buyer_chrome_cleanup_failed"), "finished_at": fields.get("finished_at", _now().isoformat())})
            return
        _write_unlocked({**current, **fields})


def _stop_chrome(process: subprocess.Popen[Any] | None) -> None:
    # The Chrome main process handles SIGTERM as an orderly browser shutdown.
    _stop_process(process, grace=35)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if not _owned_chrome_pids():
            return
        time.sleep(0.2)
    raise RuntimeError("Chrome descendants did not exit")


def _owned_chrome_pids(proc_root: Path = Path("/proc")) -> list[int]:
    """Only Chrome executables in this supervisor's transient unit count as live."""
    own_cgroup = (proc_root / "self" / "cgroup").read_bytes()
    if not own_cgroup:
        raise RuntimeError("Chrome unit cgroup unavailable")
    chrome_stat = runtime.CHROME.stat()
    chrome_identity = (chrome_stat.st_dev, chrome_stat.st_ino)
    matches: list[int] = []
    for item in proc_root.iterdir():
        if not item.name.isdigit() or int(item.name) == os.getpid():
            continue
        try:
            if item.stat().st_uid != os.geteuid() or (item / "cgroup").read_bytes() != own_cgroup:
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue  # The process exited during the scan.
        try:
            executable = (item / "exe").stat()
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError as error:
            # The setuid sandbox makes its live children non-dumpable on Linux,
            # so /proc/PID/exe denies even the owner. Within this exact unit,
            # count known Chrome process names as still active. An unfamiliar
            # inaccessible process remains an error, never proof of cleanup.
            try:
                comm = (item / "comm").read_text(encoding="ascii").strip()
            except (FileNotFoundError, ProcessLookupError):
                continue  # The sandbox child exited after the exe check.
            except OSError:
                raise RuntimeError("Chrome process identity unavailable") from error
            if comm not in {"chrome", "chrome-sandbox"}:
                raise RuntimeError("Chrome process identity unavailable") from error
            matches.append(int(item.name))
            continue
        if (executable.st_dev, executable.st_ino) == chrome_identity:
            matches.append(int(item.name))
    return matches


def _report_failure(stage: str, error: Exception) -> None:
    # Never log exception text: browser errors can contain OAuth URLs or state.
    errno = getattr(error, "errno", None)
    print(
        f"buyer_chrome_diagnostic stage={stage} class={type(error).__name__} "
        f"errno={errno if isinstance(errno, int) else 'none'}",
        file=sys.stderr,
        flush=True,
    )


def supervise(run_id: str, chrome_path: Path) -> int:
    if os.geteuid() == 0 or not run_id.startswith(RUN_PREFIX):
        return 1
    initial = _read()
    if initial.get("run_id") != run_id or chrome_path != runtime.CHROME:
        return 1
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda _sig, _frame: stop_event.set())
    deadline = datetime.fromisoformat(str(initial["deadline_at"]))
    env = {
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "DISPLAY": DISPLAY,
        "XAUTHORITY": str(runtime.STATE / "display.Xauthority"),
        "HOME": str(runtime.PROFILE), "XDG_CONFIG_HOME": str(runtime.PROFILE), "XDG_CACHE_HOME": str(runtime.PROFILE),
    }
    xvfb: subprocess.Popen[Any] | None = None
    openbox: subprocess.Popen[Any] | None = None
    vnc: subprocess.Popen[Any] | None = None
    web: subprocess.Popen[Any] | None = None
    chrome: subprocess.Popen[Any] | None = None
    pipe: ChromePipe | None = None
    restarted = False
    restart_at = 0.0
    final_status = "error"
    final_reason = "buyer_chrome_runtime_error"
    try:
        xauth = runtime.STATE / "display.Xauthority"
        xauth.touch(mode=0o600)
        os.chmod(xauth, 0o600)
        subprocess.run(["xauth", "-f", str(xauth), "add", DISPLAY, "MIT-MAGIC-COOKIE-1", secrets.token_hex(16)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        xvfb = _spawn(["Xvfb", DISPLAY, "-screen", "0", "1600x900x24", "-nolisten", "tcp", "-auth", str(xauth)], "xvfb.log", env=env)
        _wait_display(xvfb)
        openbox = _spawn(["openbox", "--sm-disable"], "openbox.log", env=env)
        chrome, pipe = _launch_chrome(chrome_path, env, run_id=run_id, generation=1)
        vnc = _spawn(["x11vnc", "-display", DISPLAY, "-auth", str(xauth), "-localhost", "-forever", "-nopw", "-noxdamage", "-rfbport", str(VNC_PORT)], "x11vnc.log", env=env)
        _wait_port(VNC_PORT, vnc)
        web = _spawn(["websockify", f"127.0.0.1:{WEB_PORT}", f"127.0.0.1:{VNC_PORT}", "--web", str(NOVNC_DIR)], "websockify.log", env=env)
        _wait_port(WEB_PORT, web)
        _safe_status_update(run_id, status="awaiting_human", reason="buyer_chrome_login_window_ready")
        while not stop_event.wait(2):
            if _now() >= deadline:
                final_status, final_reason = "timeout", "buyer_login_timeout"
                break
            if runtime._available(Path("/")) < runtime._root_reserve():
                final_status, final_reason = "error", "buyer_chrome_storage_reserve"
                break
            if chrome.poll() is not None or xvfb.poll() is not None or (web is not None and web.poll() is not None) or (vnc is not None and vnc.poll() is not None):
                raise RuntimeError("Chrome login process exited")
            try:
                surface = pipe.visible_surface()
            except (OSError, TimeoutError, RuntimeError, ValueError):
                continue
            if stop_event.is_set() or _read().get("status") == "stopping":
                final_status, final_reason = "stopped", "buyer_recovery_stopped"
                break
            if surface != "account":
                if restarted and web is None and (surface in {"phone", "sms", "challenge"} or time.monotonic() - restart_at >= 20):
                    vnc = _spawn(["x11vnc", "-display", DISPLAY, "-auth", str(xauth), "-localhost", "-forever", "-nopw", "-noxdamage", "-rfbport", str(VNC_PORT)], "x11vnc.log", env=env)
                    _wait_port(VNC_PORT, vnc)
                    web = _spawn(["websockify", f"127.0.0.1:{WEB_PORT}", f"127.0.0.1:{VNC_PORT}", "--web", str(NOVNC_DIR)], "websockify.log", env=env)
                    _wait_port(WEB_PORT, web)
                reason = {"phone": "buyer_phone_required", "sms": "buyer_sms_required", "challenge": "buyer_security_challenge"}.get(surface, "buyer_chrome_login_window_ready")
                _safe_status_update(run_id, status="awaiting_human" if web is not None else "validating_session", reason=reason)
                continue
            if not restarted:
                _safe_status_update(run_id, status="validating_session", reason="buyer_chrome_restarting")
                _stop_process(web)
                _stop_process(vnc)
                web = vnc = None
                _stop_chrome(chrome)
                pipe.close()
                pipe = None
                if stop_event.is_set() or _read().get("status") == "stopping":
                    final_status, final_reason = "stopped", "buyer_recovery_stopped"
                    break
                chrome, pipe = _launch_chrome(chrome_path, env, run_id=run_id, generation=2)
                restarted = True
                restart_at = time.monotonic()
                continue
            final_status, final_reason = "completed", "buyer_chrome_login_confirmed"
            break
        else:
            final_status, final_reason = "stopped", "buyer_recovery_stopped"
    except Exception as error:
        _report_failure("supervise", error)
        final_status, final_reason = "error", "buyer_chrome_runtime_error"
    finally:
        # Revoke the live viewer first, but keep X alive while Chrome flushes.
        cleanup_ok = True
        try:
            _stop_process(web)
        except Exception as error:
            _report_failure("viewer_stop", error)
            cleanup_ok = False
        try:
            _stop_chrome(chrome)
        except Exception as error:
            _report_failure("chrome_stop", error)
            cleanup_ok = False
        if pipe is not None:
            try:
                pipe.close()
            except Exception as error:
                _report_failure("pipe_close", error)
                cleanup_ok = False
        for stage, process in (("vnc_stop", vnc), ("openbox_stop", openbox), ("xvfb_stop", xvfb)):
            try:
                _stop_process(process)
            except Exception as error:
                _report_failure(stage, error)
                cleanup_ok = False
        if not cleanup_ok:
            final_status, final_reason = "error", "buyer_chrome_cleanup_failed"
        if final_status == "completed":
            # This is a confirmed login surface after a full browser restart,
            # not a match to the canonical expected account fingerprint.
            _safe_status_update(
                run_id, status="completed", reason=final_reason, finished_at=_now().isoformat(), login_confirmed=True,
                session={"status": "authenticated_surface", "valid": False, "login_confirmed": True, "account_confirmed": False, "checked_at": _now().isoformat()},
                price={"status": "not_checked"},
            )
        else:
            _safe_status_update(run_id, status=final_status, reason=final_reason, finished_at=_now().isoformat())
    return 0 if final_status in {"completed", "stopped"} else 1


def prepare_supervise(run_id: str) -> None:
    """Cold extraction occurs in the transient unit, never in the HTTP lane."""
    if os.geteuid() != 0 or _read().get("run_id") != run_id:
        raise RuntimeError("Chrome preparation identity invalid")
    chrome = runtime.ensure_runtime()
    user = pwd.getpwnam(runtime.USER)
    command = [
        "setpriv", "--reuid", str(user.pw_uid), "--regid", str(user.pw_gid), "--clear-groups",
        sys.executable, str(Path(__file__).resolve()), "supervise", "--run-id", run_id, "--chrome", str(chrome),
    ]
    os.execvpe("setpriv", command, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("supervise", "prepare-supervise"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--chrome", default="")
    args = parser.parse_args()
    if args.command == "prepare-supervise":
        prepare_supervise(args.run_id)
    raise SystemExit(supervise(args.run_id, Path(args.chrome)))


if __name__ == "__main__":
    main()
