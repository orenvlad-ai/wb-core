"""Persistent native WBC UI. Launch only through the OS-isolated supervisor."""
import argparse
from contextlib import ExitStack
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlsplit

from .sandbox import ROOT, launch, probe
from .state import LabState, atomic_json, read_json, stamp

DEFAULT_STATE = ROOT / ".runtime" / "local-cycle-lab"
SCENARIO = Path(__file__).with_name("scenario.json")
RESET_EXIT = 75


def code_digest():
    digest = hashlib.sha256()
    for directory in (ROOT / "apps", ROOT / "packages"):
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.suffix in {".py", ".html", ".js"}:
                digest.update(str(path.relative_to(ROOT)).encode())
                digest.update(path.read_bytes())
    return digest.hexdigest()


def isolation_checks(sentinel, probe_port):
    checks = []

    def denied(label, action):
        try:
            action()
        except OSError as exc:
            if exc.errno not in (errno.EPERM, errno.EACCES):
                raise RuntimeError(f"{label}: отказ не доказывает изоляцию: {exc}") from exc
            checks.append(label)
        else:
            raise RuntimeError("Песочница пропустила: " + label)

    denied("read_outside", lambda: Path(sentinel).read_bytes())
    denied("write_outside", lambda: Path(str(sentinel) + ".write").write_text("probe"))
    with socket.socket() as connection:
        denied("tcp_loopback_outbound", lambda: connection.connect(("127.0.0.1", probe_port)))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        denied("udp_outbound", lambda: connection.sendto(b"probe", ("127.0.0.1", probe_port)))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        denied("unix_outbound", lambda: connection.connect(str(Path(sentinel).with_name("canary.sock"))))
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as connection:
        denied("ipv6_outbound", lambda: connection.connect(("::1", probe_port)))
    import ctypes
    library = ctypes.CDLL(None)
    bootstrap = ctypes.c_uint.in_dll(library, "bootstrap_port").value
    service = ctypes.c_uint()
    if library.bootstrap_look_up(bootstrap, b"com.apple.cfprefsd.agent", ctypes.byref(service)) != 1100:
        raise RuntimeError("Mach lookup не был запрещён песочницей")
    checks.append("mach_lookup")
    return checks


def probe_main(args, child=False):
    state = LabState(args.state)
    result = dict(parent=isolation_checks(args.sentinel, args.probe_port))
    # Fork/exec the actual permitted interpreter, without inheriting open FDs.
    if not child:
        command = [sys.executable, "-s", "-B", "-m", "apps.local_cycle_lab.local", "_probe_leaf",
                   "--state", str(state.root), "--sentinel", args.sentinel,
                   "--probe-port", str(args.probe_port)]
        completed = subprocess.run(command, close_fds=True, capture_output=True, text=True, timeout=10)
        if completed.returncode:
            raise RuntimeError("Дочерний probe: " + completed.stderr[-3000:])
        result["child"] = json.loads(completed.stdout)["parent"]
    print(json.dumps(result))


def supervise(args):
    state = LabState(args.state)
    identity = state.open(args.scenario, args.variant)
    with state.writer() as lock_fd:
        state.recover_reset()
        if args.action == "reset":
            archive = state.reset(args.confirm)
            print("Исходные данные будут подготовлены при следующем запуске. Архив: " + str(archive))
            return
        isolation = probe(state, args.port)
        atomic_json(state.root / "control" / "isolation.json", isolation)
        session = dict(access=secrets.token_urlsafe(32), secret=secrets.token_urlsafe(48),
                       password=secrets.token_urlsafe(32), username="local_operator", port=args.port)
        atomic_json(state.root / "control" / "session.json", session)
        url = f"http://127.0.0.1:{args.port}/?access={session['access']}"
        atomic_json(state.root / "open.json", dict(url=url, id=identity["id"], port=args.port))
        print("Тестовая WBC: " + url, flush=True)
        def terminate(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, terminate)
        while True:
            child = launch(state, args.port, ["_serve", "--state", str(state.root), "--port", str(args.port)], lock_fd=lock_fd)
            try:
                code = child.wait()
            except KeyboardInterrupt:
                code = 130
            finally:
                # Children cannot escape their session under the default-deny profile.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=10)
            if code != RESET_EXIT:
                if code not in (0, 130):
                    raise SystemExit(code)
                return
            request = read_json(state.root / "control" / "reset.json")
            identity = state.identity()
            if request != {"id": identity["id"], "generation": identity["generation"]}:
                raise RuntimeError("Запрос сброса не соответствует текущему стенду")
            state.reset(identity["id"])
            (state.root / "control" / "reset.json").unlink()


def prepare_runtime(state, scenario, replay):
    from unittest.mock import patch
    from .__main__ import run
    from .checks import verify
    from .http import HttpTape
    identity = state.identity()
    marker = state.runtime / "wbc-local-scenario.json"
    expected = dict(fixture=identity["fixture"], variant=identity["variant"])
    if state.runtime.exists():
        if read_json(marker) != expected:
            raise ValueError("Тестовая runtime не соответствует сценарию; используйте явный сброс")
        return
    with patch("urllib.request.urlopen", HttpTape(replay).open):
        result = run(state.runtime, scenario, replay, True,
                     state.root / "tmp" / ("initial-gate-" + secrets.token_hex(12)))
    verify(result, scenario, identity["variant"])
    # Fixture configuration enables only source acceptance. No fabricated stock
    # balance, physical posting, accounting book or completed receipt is seeded.
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from packages.application.ff_pool_foundation import FEATURE_EPOCHS_TABLE
    from packages.application.ff_pool_surfaces import FfPoolSurface
    runtime = RegistryUploadDbBackedRuntime(state.runtime)
    runtime.list_ff_stock_operations(limit=1)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute(f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(1,1,0,'local-lab',?,'{{}}')",
                     (scenario["today"] + "T08:00:00Z",))
    surface = FfPoolSurface(db_path=runtime.db_path, runtime_dir=state.runtime,
                           timestamp_factory=lambda: scenario["today"] + "T08:00:00Z")
    surface.create_facility(dict(request_id="lab:facility", name="Тестовый FF", city="Москва",
        active=True, display_timezone="Asia/Yekaterinburg"), actor="Локальный оператор")
    atomic_json(marker, expected)


class StageController:
    """Durable attempts for the currently implemented source/Ready stage."""
    def __init__(self, state, scenario, code_version):
        self.state, self.scenario = state, scenario
        self.code_version = code_version
        self.lock = threading.Lock()
        self.thread = None
        # A restart only records interruption; it never restarts a writer.
        for path in (state.root / "runs").glob("*/attempt.json"):
            item = read_json(path)
            if item["status"] == "running":
                item.update(status="interrupted", finished_at=stamp())
                atomic_json(path, item)

    def status(self):
        attempts = [read_json(path) for path in sorted((self.state.root / "runs").glob("*/attempt.json"))]
        attempts.sort(key=lambda item: (item.get("started_at", ""), item["run_id"]))
        return dict(stage="sources_ready", label="Источники и витрина", attempts=attempts,
                    busy=self.lock.locked())

    def start(self):
        if code_digest() != self.code_version:
            raise ValueError("Код изменился. Перезапустите стенд: документы и результаты сохранятся.")
        if not self.lock.acquire(blocking=False):
            raise ValueError("Этап уже выполняется")
        run_id = secrets.token_hex(12)
        folder = self.state.root / "runs" / run_id
        try:
            folder.mkdir(mode=0o700)
            self.thread = threading.Thread(target=self._run, args=(folder,), daemon=True)
            self.thread.start()
        except BaseException:
            self.lock.release()
            raise
        return dict(run_id=run_id, accepted=True)

    def _run(self, folder):
        from .__main__ import run
        from .checks import verify
        from .http import HttpTape
        from .sources import Replay
        from unittest.mock import patch
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        item = dict(run_id=folder.name, stage="sources_ready", status="running", started_at=stamp())
        try:
            identity = self.state.identity()
            item.update(business_date=self.scenario["today"], fixture=identity["fixture"],
                        generation=identity["generation"], code_sha256=self.code_version,
                        scope=[r["nm_id"] for r in self.scenario["catalog"] if r["enabled"]])
            atomic_json(folder / "attempt.json", item)
            runtime = RegistryUploadDbBackedRuntime(self.state.runtime)
            item["input_db_sha256"] = self.state.checkpoint(folder, runtime.db_path)
            atomic_json(folder / "scenario.json", self.scenario)
            atomic_json(folder / "attempt.json", item)
            replay = Replay(self.scenario, self.state.identity()["variant"])
            with patch("urllib.request.urlopen", HttpTape(replay).open):
                result = run(self.state.runtime, self.scenario, replay, False, folder / "rate-gate")
            result["verification"] = verify(result, self.scenario, replay.variant)
            if code_digest() != self.code_version:
                raise ValueError("Код изменился во время этапа; перезапустите стенд и повторите проверку")
            atomic_json(folder / "result.json", result)
            item.update(status="passed", result_sha256=hashlib.sha256((folder / "result.json").read_bytes()).hexdigest())
        except Exception as exc:
            item.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            item["finished_at"] = stamp()
            atomic_json(folder / "attempt.json", item)
            self.lock.release()


def serve(args):
    # Requiring the actual macOS sandbox profile prevents accidental direct
    # invocation of this internal command as an unisolated app.
    import ctypes
    sandbox = ctypes.CDLL("/usr/lib/libsandbox.dylib")
    if sandbox.sandbox_check(os.getpid(), b"network-outbound", 0) != 1:
        raise RuntimeError("Внутренний server разрешён только в OS sandbox")
    state = LabState(args.state)
    import fcntl
    lock_fd = int(os.environ["WBC_LAB_LOCK_FD"])
    lock_info = os.fstat(lock_fd)
    expected_lock = (state.root / ".writer.lock").stat()
    if (lock_info.st_dev, lock_info.st_ino) != (expected_lock.st_dev, expected_lock.st_ino):
        raise RuntimeError("Не унаследована точная блокировка стенда")
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    parent_pid = int(os.environ["WBC_LAB_SUPERVISOR_PID"])
    def watch_parent():
        timer = threading.Event()
        while not timer.wait(0.25):
            if os.getppid() != parent_pid:
                os._exit(143)
    threading.Thread(target=watch_parent, daemon=True).start()
    code_version = code_digest()
    identity = state.identity()
    scenario = read_json(state.root / "scenario.json")
    session = read_json(state.root / "control" / "session.json")
    # Set native auth before importing any native application module.
    from .sources import Replay
    from .http import HttpTape
    from unittest.mock import patch
    from .__main__ import build_sources, ReplayClock
    replay = Replay(scenario, identity["variant"])
    with ExitStack() as stack:
        stack.enter_context(patch("urllib.request.urlopen", HttpTape(replay).open))
        prepare_runtime(state, scenario, replay)
        from packages.adapters import registry_upload_http_entrypoint as web
        os.environ.update(WB_CORE_WEB_AUTH_REQUIRED="1", WB_CORE_WEB_AUTH_USERNAME=session["username"],
            WB_CORE_WEB_AUTH_PASSWORD_HASH=web._hash_pbkdf2_password(session["password"]),
            WB_CORE_WEB_AUTH_SESSION_SECRET=session["secret"])
        from datetime import datetime
        from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
        from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint, SheetVitrinaV1OperatorJobStore
        from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig
        from packages.adapters.wb_finance_api import WbFinanceApiClient
        now = datetime.fromisoformat(scenario["today"] + "T08:00:00+00:00")
        timestamp = lambda: scenario["today"] + "T08:00:00Z"
        runtime = RegistryUploadDbBackedRuntime(state.runtime)
        # Replace the scheduling hook only. Native document acceptance, guards,
        # readback and pending receipts retain their original implementation.
        stack.enter_context(patch.object(SheetVitrinaV1OperatorJobStore, "resume_warehouse_pending", return_value=None))
        def disabled_job(*args, **kwargs):
            raise ValueError("Полный цикл стенда ещё не подключён; используйте проверку источников и витрины")
        stack.enter_context(patch.object(SheetVitrinaV1OperatorJobStore, "start", disabled_job))
        stack.enter_context(patch.object(SheetVitrinaV1OperatorJobStore, "start_warehouse_if_idle", disabled_job))
        entry = RegistryUploadHttpEntrypoint(runtime_dir=state.runtime, runtime=runtime,
            now_factory=lambda: now, activated_at_factory=timestamp, refreshed_at_factory=timestamp)
        clock = ReplayClock(now)
        finance = WbFinanceApiClient("lab-only", request=replay.finance_request,
            rate_gate_root=state.root / "tmp" / "ui-gate", wall_time=clock.time, monotonic=clock.time, sleep=clock.advance)
        entry.sheet_plan_block = build_sources(runtime, replay, now, finance)
        controller = StageController(state, scenario, code_version)
        config = RegistryUploadHttpEntrypointConfig(host="127.0.0.1", port=args.port,
            upload_path=web.DEFAULT_UPLOAD_PATH, sheet_plan_path=web.DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh", sheet_status_path=web.DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=web.DEFAULT_SHEET_OPERATOR_UI_PATH, runtime_dir=state.runtime)
        server = web.build_registry_upload_http_server(config, entrypoint=entry, advertise_window_v3=False)
        original = server.RequestHandlerClass
        html_writer = web._write_html_response
        json_writer = web._write_json_response

        def write_json(handler, status, payload, **kwargs):
            return json_writer(handler, web.HTTPStatus(status), payload, **kwargs)
        stack.enter_context(patch.object(web, "_write_json_response", write_json))

        def write_html(handler, status, html, **kwargs):
            banner = '<div style="padding:8px;background:#fef08a;color:#713f12;font:600 14px sans-serif">ТЕСТ · Локальная WBC · Данные ' + scenario["today"] + '</div>'
            script = '<script>const labGeneration=' + json.dumps(identity["generation"]) + ';const nativeFetch=window.fetch;window.fetch=(url,options={})=>{const target=new URL(typeof url==="string"?url:url.url,location.href);if(target.origin===location.origin){options.headers=new Headers(options.headers||(url instanceof Request?url.headers:{}));options.headers.set("X-WBC-Lab-Generation",labGeneration);}return nativeFetch(url,options);};</script>'
            html = html.replace("</head>", script + "</head>", 1)
            index = html.find(">", html.find("<body")) + 1
            html = html[:index] + banner + html[index:]
            return html_writer(handler, status, html, **kwargs)
        stack.enter_context(patch.object(web, "_write_html_response", write_html))

        class Handler(original):
            def log_message(self, *arguments):
                pass  # Access capabilities must not appear in HTTP logs.

            def end_headers(self):
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
                self.send_header("Referrer-Policy", "no-referrer")
                super().end_headers()

            def authenticated(self):
                if not web._authenticated_web_user(self, web._web_auth_config()):
                    web._write_json_response(self, 401, {"error": "Войдите в локальную WBC"})
                    return False
                return True

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/" and secrets.compare_digest(parse_qs(parsed.query).get("access", [""])[0], session["access"]):
                    cookie = web._build_session_cookie(self, session["username"], web._web_auth_config(), role="admin", display_name="Локальный оператор")
                    web._write_redirect_response(self, web.HTTPStatus.SEE_OTHER, "/", headers={"Set-Cookie": cookie})
                    return
                if parsed.path in {"/", "/lab/status"}:
                    if not self.authenticated():
                        return
                    if parsed.path == "/lab/status":
                        web._write_json_response(self, 200, controller.status())
                    else:
                        html = Path(__file__).with_name("local.html").read_text().replace("__WBC_UI__", web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH)
                        web._write_html_response(self, web.HTTPStatus.OK, html)
                    return
                super().do_GET()

            def do_POST(self):
                parsed = urlsplit(self.path)
                if not self.mutation_allowed():
                    return
                if parsed.path in {"/lab/run", "/lab/reset"}:
                    if not self.authenticated():
                        return
                    try:
                        if parsed.path == "/lab/run":
                            if entry.operator_jobs.maintenance_live_jobs():
                                raise ValueError("Сначала дождитесь завершения фоновой задачи WBC")
                            result = controller.start()
                        else:
                            if controller.status()["busy"]:
                                raise ValueError("Дождитесь завершения этапа")
                            atomic_json(state.root / "control" / "reset.json", {"id": identity["id"], "generation": identity["generation"]})
                            web._write_json_response(self, 202, {"resetting": True})
                            self.wfile.flush()
                            os._exit(RESET_EXIT)
                        web._write_json_response(self, 202, result)
                    except ValueError as exc:
                        web._write_json_response(self, 409, {"error": str(exc)})
                    return
                # Explicitly allow only the native source-acceptance workflow
                # verified in this version. No hidden async launcher is exposed.
                allowed = parsed.path in {web.DEFAULT_FF_POOL_OVERHEAD_PREVIEW_PATH,
                    web.DEFAULT_SHEET_WEB_VITRINA_PERFORMANCE_PATH, web.DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
                    "/login"}
                prefix = web.DEFAULT_FF_POOL_REQUESTS_PATH + "/"
                if parsed.path.startswith(prefix) and parsed.path.endswith("/confirm"):
                    request_id = parsed.path[len(prefix):-len("/confirm")]
                    status = entry.ff_pool_surface.request_status(request_id)
                    allowed = status.get("document_kind") == "pool_overhead"
                if not allowed:
                    web._write_json_response(self, 409, {"error": "В этой версии стенда доступны ручные накладные расходы и проверка источников/витрины. Остальные операции ещё не подключены."})
                    return
                super().do_POST()

            def mutation_allowed(self):
                if urlsplit(self.path).path == "/login":
                    return True
                if (self.headers.get("X-WBC-Lab-Generation") != identity["generation"]
                        or self.headers.get("Origin", "") != f"http://127.0.0.1:{args.port}"):
                    web._write_json_response(self, 409, {"error": "Перезагрузите страницу тестового стенда"})
                    return False
                if controller.status()["busy"]:
                    web._write_json_response(self, 409, {"error": "Дождитесь завершения этапа; сейчас доступен просмотр"})
                    return False
                return True

            def do_PATCH(self):
                if self.mutation_allowed():
                    web._write_json_response(self, 409, {"error": "Этот вид изменения ещё не подключён к стенду"})

            def do_DELETE(self):
                if self.mutation_allowed():
                    web._write_json_response(self, 409, {"error": "Этот вид изменения ещё не подключён к стенду"})

            def do_PUT(self):
                if self.mutation_allowed():
                    self.send_error(405)

        server.RequestHandlerClass = Handler
        server.service_actions = lambda: None  # No automatic maintenance/cycle wakeup.
        atomic_json(state.root / "control" / "ready.json", dict(pid=os.getpid(), generation=identity["generation"], port=args.port))
        print(f"Локальный интерфейс готов: http://127.0.0.1:{args.port}/", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve", "reset", "_serve", "_probe", "_probe_leaf"))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--scenario", type=Path, default=SCENARIO)
    parser.add_argument("--variant", choices=("normal", "zero", "missing"), default="normal")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--sentinel")
    parser.add_argument("--probe-port", type=int)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Нужен отдельный непривилегированный порт 1024..65535")
    if args.action in {"_probe", "_probe_leaf"}:
        probe_main(args, child=args.action == "_probe_leaf")
    elif args.action == "_serve":
        serve(args)
    else:
        supervise(args)


if __name__ == "__main__":
    main()
