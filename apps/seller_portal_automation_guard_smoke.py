"""Local smoke checks for shared Seller Portal automation guard."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_automation_guard import (  # noqa: E402
    SellerPortalAutomationBusy,
    SellerPortalStorageStatePolicyError,
    acquire_seller_portal_automation_lock,
    current_lock_status,
    seller_portal_automation_lock_path,
    validate_storage_state_path_for_runtime,
)


def main() -> None:
    _assert_acquire_release_and_busy()
    _assert_stale_cleanup_requires_process_check()
    _assert_live_storage_state_policy()
    print("seller_portal_automation_guard_smoke: OK")


def _assert_acquire_release_and_busy() -> None:
    with TemporaryDirectory(prefix="seller-automation-lock-") as tmp:
        runtime_dir = Path(tmp)
        lock = acquire_seller_portal_automation_lock(
            runtime_dir=runtime_dir,
            owner="smoke-owner",
            purpose="smoke",
            run_id="run-1",
            expected_max_seconds=60,
        )
        try:
            status = current_lock_status(runtime_dir)
            if not status.get("busy") or status.get("owner") != "smoke-owner":
                raise AssertionError(f"active lock must be visible: {status}")
            reentrant = acquire_seller_portal_automation_lock(
                runtime_dir=runtime_dir,
                owner="smoke-owner-2",
                purpose="smoke-reentrant",
                run_id="run-2",
                expected_max_seconds=60,
            )
            if not reentrant.reentrant:
                raise AssertionError(f"same process must be reentrant: {reentrant}")
        finally:
            lock.release()
        lock_path = seller_portal_automation_lock_path(runtime_dir)
        lock_path.write_text(
            json.dumps(
                {
                    "owner": "active-other-host",
                    "purpose": "active",
                    "run_id": "busy-run",
                    "started_at": "2026-05-07T00:00:00Z",
                    "heartbeat_at": "2999-01-01T00:00:00Z",
                    "expected_max_seconds": 60,
                    "pid": 99999999,
                    "host": "another-host",
                    "command": "active",
                    "lock_id": "active-lock",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        try:
            acquire_seller_portal_automation_lock(
                runtime_dir=runtime_dir,
                owner="blocked-owner",
                purpose="blocked",
                run_id="blocked",
                expected_max_seconds=60,
            )
        except SellerPortalAutomationBusy:
            pass
        else:
            raise AssertionError("active other-host lock must block a second automation job")


def _assert_stale_cleanup_requires_process_check() -> None:
    with TemporaryDirectory(prefix="seller-automation-stale-") as tmp:
        runtime_dir = Path(tmp)
        lock_path = seller_portal_automation_lock_path(runtime_dir)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(
            json.dumps(
                {
                    "owner": "old",
                    "purpose": "old",
                    "run_id": "old-run",
                    "started_at": "2020-01-01T00:00:00Z",
                    "heartbeat_at": "2020-01-01T00:00:00Z",
                    "expected_max_seconds": 60,
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "command": "old",
                    "lock_id": "old-lock",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        lock = acquire_seller_portal_automation_lock(
            runtime_dir=runtime_dir,
            owner="new",
            purpose="new",
            run_id="new-run",
            expected_max_seconds=60,
        )
        try:
            if not lock.stale_lock or lock.stale_lock.get("run_id") != "old-run":
                raise AssertionError(f"stale lock evidence must be reported: {lock.public_payload()}")
        finally:
            lock.release()


def _assert_live_storage_state_policy() -> None:
    previous = os.environ.get("SELLER_PORTAL_STORAGE_STATE_PATH")
    try:
        os.environ["SELLER_PORTAL_STORAGE_STATE_PATH"] = "/opt/wb-web-bot/storage_state.json"
        validate_storage_state_path_for_runtime(Path("/opt/wb-web-bot/storage_state.json"), Path("/opt/wb-core-runtime/state"))
        try:
            validate_storage_state_path_for_runtime(Path("/Users/operator/storage_state.json"), Path("/opt/wb-core-runtime/state"))
        except SellerPortalStorageStatePolicyError:
            pass
        else:
            raise AssertionError("EU live runtime must reject local storage_state fallback")
    finally:
        if previous is None:
            os.environ.pop("SELLER_PORTAL_STORAGE_STATE_PATH", None)
        else:
            os.environ["SELLER_PORTAL_STORAGE_STATE_PATH"] = previous


if __name__ == "__main__":
    main()
