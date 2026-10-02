"""Keep Vitrina control GETs read-only beside a pinned DELETE-journal reader."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
from tempfile import TemporaryDirectory
from time import perf_counter
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer, _reserve_free_port,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
    DEFAULT_UPLOAD_PATH,
    RegistryUploadHttpEntrypointConfig,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application import registry_upload_db_backed_runtime as runtime_module  # noqa: E402


def _get_json(base_url: str, path: str) -> tuple[int, dict, float]:
    started = perf_counter()
    try:
        response = urlopen(base_url + path, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response), perf_counter() - started


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_concurrent_delete_reader() -> None:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, advertise_window_v3=True)
    with fixture as base_url:
        db_path = fixture.entrypoint.runtime.db_path
        with sqlite3.connect(db_path) as conn:
            mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise AssertionError(f"expected DELETE journal, got {mode}")
        before = _sha256(db_path)
        reader = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        reader.execute("SELECT bundle_version FROM registry_upload_current_state WHERE slot=1").fetchone()

        old_keys = set(runtime_module._SCHEMA_READY_KEYS)
        old_bootstrap = runtime_module._ensure_schema_uncached

        def forbid_bootstrap(*_args, **_kwargs):
            raise AssertionError("control GET attempted operational schema bootstrap")

        runtime_module._SCHEMA_READY_KEYS.clear()
        runtime_module._ensure_schema_uncached = forbid_bootstrap
        try:
            cases = (
                (DEFAULT_SHEET_STATUS_PATH + "?as_of_date=2026-04-20", "server_context"),
                (DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH, "contract_name"),
                (DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH, "config_key"),
            )
            for path, field in cases:
                status, payload, seconds = _get_json(base_url, path)
                if status != HTTPStatus.OK or field not in payload or seconds >= 1:
                    raise AssertionError(f"control GET failed beside pinned reader: {path} {status} {seconds:.3f}s {payload}")
                print(f"window_v3_control_http: {path.split('?')[0]} {status} {seconds:.3f}s")

            status, payload, seconds = _get_json(
                base_url, DEFAULT_SHEET_STATUS_PATH + "?as_of_date=2026-04-13",
            )
            if status != HTTPStatus.UNPROCESSABLE_ENTITY or not all(
                key in payload for key in ("server_context", "manual_context", "load_context")
            ) or seconds >= 1:
                raise AssertionError(f"status error contexts escaped pinned read: {status} {seconds:.3f}s {payload}")
        finally:
            runtime_module._ensure_schema_uncached = old_bootstrap
            runtime_module._SCHEMA_READY_KEYS.clear()
            runtime_module._SCHEMA_READY_KEYS.update(old_keys)
            reader.execute("ROLLBACK")
            reader.close()
        if _sha256(db_path) != before:
            raise AssertionError("control GET changed the operational SQLite file")


def check_first_run_user_config() -> None:
    with TemporaryDirectory(prefix="window-v3-first-run-control-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=runtime_dir)
        db_path = entrypoint.runtime.db_path
        # Entrypoint construction may prime an empty operational store. Reset
        # this disposable fixture to exercise the route's true absent-DB path.
        if db_path.is_file():
            db_path.unlink()
        if os.path.lexists(db_path):
            raise AssertionError("first-run precondition lost: operational DB already exists")
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1", port=_reserve_free_port(), upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime_dir,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            status, payload, _seconds = _get_json(
                f"http://127.0.0.1:{config.port}", DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
            )
            if status != HTTPStatus.OK or payload.get("status") != "missing" or not db_path.is_file():
                raise AssertionError(f"first-run user config changed: {status} {payload}")
            with sqlite3.connect(db_path) as conn:
                conn.execute("DROP TABLE registry_upload_current_state")
            damaged_before = _sha256(db_path)
            status, _payload, _seconds = _get_json(
                f"http://127.0.0.1:{config.port}", DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
            )
            if status != HTTPStatus.INTERNAL_SERVER_ERROR or _sha256(db_path) != damaged_before:
                raise AssertionError("existing damaged store fell back to schema bootstrap")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def main() -> None:
    check_concurrent_delete_reader()
    check_first_run_user_config()
    print("window_v3_control_http: DELETE reader and first-run parity ok")


if __name__ == "__main__":
    main()
