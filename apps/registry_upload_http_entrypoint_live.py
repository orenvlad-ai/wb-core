"""Локальный live runner для HTTP entrypoint registry upload."""

import os
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    build_registry_upload_http_server,
    load_registry_upload_http_entrypoint_config,
    RegistryUploadHttpServer,
)
from packages.adapters.search_cluster_cleaner_http import handles as cleaner_handles
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.storage_registry import StoreRegistry
from packages.application.search_cluster_cleaner_web import STAGE_E_CONFIG_PATH


class CleanerWorkerSupervisor:
    """Keep the readback-only recovery worker alive with bounded restarts."""
    def __init__(self,runtime_dir:Path):
        self.runtime_dir=runtime_dir
        self.process=None
        self.stopping=threading.Event()
        self.thread=threading.Thread(target=self._run,name='cleaner-worker-supervisor',daemon=True)
        self.last_exit=None
        self.last_start_at=None

    def start(self):
        self._spawn()
        self.thread.start()

    def _spawn(self):
        self.process=subprocess.Popen([sys.executable,str(ROOT/'apps/search_cluster_cleaner_self_service_worker.py'),
            '--runtime-dir',str(self.runtime_dir)],stdin=subprocess.DEVNULL,close_fds=True)
        self.last_start_at=time.time()

    def _run(self):
        backoff=1.0
        while not self.stopping.wait(2):
            process=self.process
            if process is None:continue
            result=process.poll()
            if result is None:
                if time.time()-(self.last_start_at or 0)>30:backoff=1.0
                continue
            self.last_exit=result
            if self.stopping.wait(backoff):break
            try:self._spawn()
            except OSError:pass
            backoff=min(30.0,backoff*2)

    def alive(self) -> bool:
        return bool(self.process and self.process.poll() is None)

    def status(self) -> str:
        process=self.process
        if not process or process.poll() is not None:return 'down'
        try:
            value=json.loads(Path('/var/lib/wb-core/search-cluster-cleaner-admission/self-service-worker-health.json').read_text(encoding='utf-8'))
            if value.get('pid')==process.pid:
                state=str(value.get('state') or 'down')
                # A WB request can exceed the heartbeat freshness window.
                # The child PID is still alive, and a saved busy phase cannot
                # be mistaken for readiness or an abandoned worker.
                if state=='busy':return state
                if time.time()-float(value.get('updated_at') or 0)<10:return state
        except (OSError,ValueError,TypeError):pass
        return 'starting'

    def close(self):
        self.stopping.set()
        process=self.process
        if process and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
        self.thread.join(timeout=5)


def _cleaner_contour_configured(entrypoint, runtime_dir:Path) -> bool:
    """Opt in only for the private, package-bound current manual account."""
    web=getattr(entrypoint,'cleaner_web',None)
    cleaner=getattr(web,'cleaner',None)
    if cleaner is None:return False
    try:
        from apps.search_cluster_cleaner_stage_e import _package
        config=json.loads(STAGE_E_CONFIG_PATH.read_text(encoding='utf-8'))
        if set(config)!={'seller_id','account_scope','generation','owner_username','approved_package_path'}:
            return False
        if (config['seller_id']!=cleaner.account.seller_id or config['account_scope']!=cleaner.account.account_scope
                or config['generation']!=web.generation or config['owner_username']!=cleaner.owner_username):
            return False
        package_path=Path(config['approved_package_path'])
        if not package_path.is_absolute():return False
        _package(package_path,cleaner.account,web.generation)
        with cleaner.store.read() as c:
            settings=cleaner._settings(c)
            return settings['generation']==web.generation and bool(settings['baseline_ready'])
    except (OSError,ValueError,KeyError,TypeError):return False
    except Exception as exc:
        from packages.contracts.search_cluster_cleaner import CleanerError
        if isinstance(exc,CleanerError):return False
        raise


def start_cleaner_contour(server,entrypoint,runtime_dir:Path):
    """Return no listener/worker on unrelated or unconfigured registry hosts."""
    if not _cleaner_contour_configured(entrypoint,runtime_dir):return None,None,None
    primary_handler=server.RequestHandlerClass
    class CleanerOnlyHandler(primary_handler):
        def do_GET(self):  # noqa: N802
            if not cleaner_handles(urlsplit(self.path).path):self.send_error(404);return
            super().do_GET()
        def do_POST(self):  # noqa: N802
            if not cleaner_handles(urlsplit(self.path).path):self.send_error(404);return
            super().do_POST()
    cleaner_server=RegistryUploadHttpServer(('127.0.0.1',8776),CleanerOnlyHandler)
    cleaner_thread=threading.Thread(target=cleaner_server.serve_forever,name='cleaner-http',daemon=True)
    cleaner_thread.start()
    try:
        cleaner_worker=CleanerWorkerSupervisor(runtime_dir)
        cleaner_worker.start()
    except BaseException:
        cleaner_server.shutdown();cleaner_server.server_close();cleaner_thread.join(timeout=5)
        raise
    entrypoint.cleaner_web.worker_alive=cleaner_worker.alive
    entrypoint.cleaner_web.worker_status=cleaner_worker.status
    return cleaner_server,cleaner_thread,cleaner_worker


class FinanceCanonicalStoreBindings:
    """Keep process-visible query-only handles on both canonical stores."""

    def __init__(self, runtime_dir: Path) -> None:
        registry = StoreRegistry(Path(runtime_dir))
        manifest = registry.load(require_files=True)
        paths = tuple(
            dict.fromkeys(
                (
                    registry.resolve("finance_raw", manifest=manifest),
                    registry.resolve("operational", manifest=manifest),
                )
            )
        )
        connections: list[sqlite3.Connection] = []
        try:
            for path in paths:
                connection = sqlite3.connect(
                    f"file:{path.resolve()}?mode=ro",
                    uri=True,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA query_only=ON")
                if int(connection.execute("PRAGMA query_only").fetchone()[0]) != 1:
                    raise RuntimeError(
                        "canonical Finance store binding is not query-only"
                    )
                connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master"
                ).fetchone()
                connections.append(connection)
        except Exception:
            for connection in reversed(connections):
                connection.close()
            raise
        self.paths = tuple(path.resolve() for path in paths)
        self._connections = connections

    def close(self) -> None:
        for connection in reversed(self._connections):
            connection.close()
        self._connections.clear()


def main() -> None:
    config = load_registry_upload_http_entrypoint_config()
    activated_at_override = os.environ.get("REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE", "").strip()
    bindings: FinanceCanonicalStoreBindings | None = None
    server = None
    cleaner_server = None
    cleaner_thread = None
    cleaner_worker = None
    try:
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=config.runtime_dir,
            activated_at_factory=(
                (lambda: activated_at_override)
                if activated_at_override
                else None
            ),
        )
        bindings = FinanceCanonicalStoreBindings(config.runtime_dir)
        server = build_registry_upload_http_server(
            config,
            entrypoint=entrypoint,
        )
        # The legacy 8765 listener remains independent of this opt-in contour.
        cleaner_server,cleaner_thread,cleaner_worker=start_cleaner_contour(server,entrypoint,config.runtime_dir)
        host, port = server.server_address
        print(f"registry upload http entrypoint: http://{host}:{port}{config.upload_path}")
        print(f"cost price upload endpoint: http://{host}:{port}{config.cost_price_upload_path}")
        print(f"sheet vitrina plan endpoint: http://{host}:{port}{config.sheet_plan_path}")
        print(f"sheet vitrina refresh endpoint: http://{host}:{port}{config.sheet_refresh_path}")
        print(f"sheet vitrina load endpoint: http://{host}:{port}{DEFAULT_SHEET_LOAD_PATH}")
        print(f"sheet vitrina status endpoint: http://{host}:{port}{config.sheet_status_path}")
        print(f"sheet vitrina job endpoint: http://{host}:{port}{DEFAULT_SHEET_JOB_PATH}")
        print(f"sheet vitrina operator page: http://{host}:{port}{config.sheet_operator_ui_path}")
        print(f"runtime dir: {config.runtime_dir}")
        print(
            "finance canonical store bindings: "
            + ", ".join(str(path) for path in bindings.paths)
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if cleaner_worker is not None:
            cleaner_worker.close()
        if cleaner_server is not None:
            cleaner_server.shutdown()
            cleaner_server.server_close()
        if cleaner_thread is not None:cleaner_thread.join(timeout=5)
        if server is not None:
            server.server_close()
        if bindings is not None:
            bindings.close()


if __name__ == "__main__":
    main()
