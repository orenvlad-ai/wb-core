#!/usr/bin/env python3
"""Playwright smoke for the shared warehouse UI and legacy FF transition."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.warehouse_stocks_smoke import _block, _seed_runtime  # noqa: E402
from apps.warehouse_stocks_production_ui_flow import run_warehouse_ui_flow  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_PLAN_PATH,
    DEFAULT_SHEET_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_UPLOAD_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-browser-smoke-") as temp_dir:
        root = Path(temp_dir)
        runtime = _seed_runtime(root / "runtime")
        block = _block(runtime)
        plan = block.build_opening_plan()
        block.apply_opening_plan(
            plan,
            confirm_fingerprint=plan["plan_fingerprint"],
            backup_dir=root / "backups",
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1",
            port=_reserve_free_port(),
            upload_path=DEFAULT_UPLOAD_PATH,
            sheet_plan_path=DEFAULT_SHEET_PLAN_PATH,
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path=DEFAULT_SHEET_STATUS_PATH,
            sheet_operator_ui_path=DEFAULT_SHEET_OPERATOR_UI_PATH,
            runtime_dir=runtime.runtime_dir,
        )
        with _patched_env({"WB_CORE_WEB_AUTH_REQUIRED": "0"}):
            server = build_registry_upload_http_server(
                config,
                entrypoint=RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = run_warehouse_ui_flow(
                    base_url=f"http://127.0.0.1:{config.port}",
                    auth_cookie=None,
                    expected_readback=block.readback(),
                    evidence_dir=root / "ui-evidence",
                    allowed_server_error_paths=(
                        "/v1/sheet-vitrina-v1/supply/wb-supplies/overlay-options",
                    ),
                    allowed_console_error_messages=(
                        "Failed to load resource: the server responded with a status of 422 (Unprocessable Content)",
                        "Failed to load resource: the server responded with a status of 500 (Internal Server Error)",
                    ),
                )
                _assert(result.get("status") == "ok", "browser flow status")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
    print("warehouse stocks browser smoke: ok")


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _patched_env(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    main()
