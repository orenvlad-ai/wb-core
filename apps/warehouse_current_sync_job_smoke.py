#!/usr/bin/env python3
"""Targeted smoke for the observable current-source warehouse UI job."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import threading
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import (
    RegistryUploadHttpEntrypoint,
)
from packages.application.warehouse_sync_lock import WarehouseSyncBusyError


def _wait_terminal(
    entrypoint: RegistryUploadHttpEntrypoint,
    run_id: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        payload = entrypoint.handle_warehouse_manual_sync_status_request(run_id)
        if payload["status"] not in {"running", "busy"}:
            return payload
        time.sleep(0.01)
    raise AssertionError("warehouse background job did not finish")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="warehouse-current-sync-job-") as raw:
        entrypoint = RegistryUploadHttpEntrypoint(runtime_dir=Path(raw))
        release = threading.Event()

        def changed_sync() -> dict[str, Any]:
            if not release.wait(timeout=5):
                raise RuntimeError("synthetic wait timed out")
            return {
                "plan_fingerprint": "sha256:changed",
                "diff": {
                    "changed_line_count": 2,
                    "lines": [
                        {"warehouse_key": "ff", "nm_id": 1},
                        {"warehouse_key": "wb", "nm_id": 1},
                    ],
                },
                "active_version": {
                    "version_id": "whfv_changed",
                    "business_date": "2026-07-23",
                },
                "official_supply_sync": {"changed_rows": 2},
                "functional_economics_publication": {"database_written": True},
            }

        entrypoint.handle_warehouse_manual_sync_request = changed_sync  # type: ignore[method-assign]
        started = entrypoint.handle_warehouse_manual_sync_start_request()
        assert started["status"] == "running"
        assert started["run_id"]
        duplicate = entrypoint.handle_warehouse_manual_sync_start_request()
        assert duplicate["status"] == "busy"
        assert duplicate["run_id"] == started["run_id"]
        release.set()
        changed = _wait_terminal(entrypoint, started["run_id"])
        assert changed["user_status"] == "Готово: все 6 складов и себестоимости обновлены"
        assert changed["changed_warehouses"] == 2
        assert changed["changed_skus"] == 1
        assert changed["functional_version_id"] == "whfv_changed"
        assert (
            entrypoint.handle_warehouse_manual_sync_status_request()["run_id"]
            == started["run_id"]
        ), "status must remain readable after page reload"

        entrypoint.handle_warehouse_manual_sync_request = lambda: {  # type: ignore[method-assign]
            "plan_fingerprint": "sha256:no-change",
            "diff": {"changed_line_count": 0, "lines": []},
            "active_version": {
                "version_id": "whfv_same",
                "business_date": "2026-07-23",
            },
            "official_supply_sync": {"changed_rows": 0},
            "functional_economics_publication": {"database_written": False},
        }
        no_change_start = entrypoint.handle_warehouse_manual_sync_start_request()
        no_change = _wait_terminal(entrypoint, no_change_start["run_id"])
        assert no_change["user_status"] == "Без изменений: данные уже актуальны"

        def blocked_sync() -> dict[str, Any]:
            raise WarehouseSyncBusyError("warehouse lock is held")

        entrypoint.handle_warehouse_manual_sync_request = blocked_sync  # type: ignore[method-assign]
        blocked_start = entrypoint.handle_warehouse_manual_sync_start_request()
        blocked = _wait_terminal(entrypoint, blocked_start["run_id"])
        assert blocked["status"] == "error"
        assert "warehouse lock is held" in blocked["user_status"]
        assert any("Ошибка:" in line for line in blocked["short_log"])

    print("warehouse current sync job smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
