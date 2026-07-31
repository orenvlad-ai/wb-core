"""Локальный live runner для HTTP entrypoint registry upload."""

import os
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_LOAD_PATH,
    build_registry_upload_http_server,
    load_registry_upload_http_entrypoint_config,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.storage_registry import StoreRegistry


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
        if server is not None:
            server.server_close()
        if bindings is not None:
            bindings.close()


if __name__ == "__main__":
    main()
