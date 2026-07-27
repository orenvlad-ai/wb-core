#!/usr/bin/env python3
"""Regression checks for the short-window manual HTTP write barrier."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.business_data_write_barrier import (  # noqa: E402
    BusinessDataWriteBarrierError,
    STATE_FILENAME,
    acquire_barrier,
    audit_blocked_request,
    barrier_status,
    confirm_barrier_hold,
    mark_barrier_restoring,
    release_barrier,
)


PLAN = "sha256:" + ("1" * 64)
WINDOW = "snapshot-window-001"


def _maintenance_hold() -> dict[str, object]:
    return {
        "schema_version": "business_data_maintenance_v1",
        "phase": "held",
        "held_at": "2026-07-27T00:00:00Z",
        "hold_readback": {
            "quiet": True,
            "auto_updates": {"revision": 19},
        },
    }


def _assert_lifecycle_survives_process_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="write-barrier-smoke-") as raw:
        runtime_dir = Path(raw)
        assert barrier_status(runtime_dir)["active"] is False
        acquired = acquire_barrier(
            runtime_dir,
            window_id=WINDOW,
            window_kind="snapshot",
            plan_fingerprint=PLAN,
            approval_reference="approval-comment-001",
            actor="smoke",
            reason="coherent snapshot",
        )
        assert acquired["active"] is True
        assert acquired["phase"] == "acquiring"
        state_path = runtime_dir / STATE_FILENAME
        assert state_path.stat().st_mode & 0o777 == 0o600

        # A fresh read has no in-memory dependency and models an HTTP/runner
        # restart while the durable barrier must remain fail-closed.
        restarted = barrier_status(Path(str(runtime_dir)))
        assert restarted["active"] is True
        assert restarted["window_id"] == WINDOW
        assert acquire_barrier(
            runtime_dir,
            window_id=WINDOW,
            window_kind="snapshot",
            plan_fingerprint=PLAN,
            approval_reference="approval-comment-001",
            actor="smoke",
            reason="coherent snapshot",
        )["idempotent"] is True
        try:
            acquire_barrier(
                runtime_dir,
                window_id="different-window-002",
                window_kind="snapshot",
                plan_fingerprint="sha256:" + ("2" * 64),
                approval_reference="approval-comment-002",
                actor="smoke",
                reason="must conflict",
            )
        except BusinessDataWriteBarrierError as exc:
            assert "different maintenance" in str(exc)
        else:
            raise AssertionError("a second active barrier must fail closed")

        confirmed = confirm_barrier_hold(
            runtime_dir,
            window_id=WINDOW,
            plan_fingerprint=PLAN,
            maintenance_state=_maintenance_hold(),
        )
        assert confirmed["phase"] == "held"
        audit = audit_blocked_request(
            runtime_dir,
            method="POST",
            path="/v1/sheet-vitrina-v1/web-vitrina/user-config",
            actor="webcore_user_smoke",
            request_id="request-smoke-001",
            remote_address="127.0.0.1",
        )
        assert audit["audited"] is True
        mark_barrier_restoring(
            runtime_dir,
            window_id=WINDOW,
            plan_fingerprint=PLAN,
        )
        try:
            release_barrier(
                runtime_dir,
                window_id=WINDOW,
                plan_fingerprint=PLAN,
                actor="smoke",
                reason="unsafe restore",
                restore_readback={
                    "status": "restored",
                    "exact_prior_state_restored": False,
                },
            )
        except BusinessDataWriteBarrierError as exc:
            assert "exact maintenance restore" in str(exc)
        else:
            raise AssertionError("barrier release without exact restore must fail")
        assert barrier_status(runtime_dir)["active"] is True
        released = release_barrier(
            runtime_dir,
            window_id=WINDOW,
            plan_fingerprint=PLAN,
            actor="smoke",
            reason="exact restore read back",
            restore_readback={
                "status": "restored",
                "captured_at": "2026-07-27T00:00:01Z",
                "exact_prior_state_restored": True,
                "control_signature": "sha256:" + ("3" * 64),
                "auto_updates": {
                    "revision": 20,
                    "master_desired": True,
                },
            },
        )
        assert released["active"] is False
        assert released["phase"] == "released"


def _assert_corrupt_state_fails_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="write-barrier-corrupt-") as raw:
        runtime_dir = Path(raw)
        state_path = runtime_dir / STATE_FILENAME
        state_path.write_text("{broken", encoding="utf-8")
        state_path.chmod(0o600)
        status = barrier_status(runtime_dir)
        assert status["active"] is True
        assert status["status"] == "invalid_fail_closed"
        assert "unreadable" in status["error"]

        state_path.write_text(
            json.dumps({"schema_version": "unknown", "phase": "released"}),
            encoding="utf-8",
        )
        state_path.chmod(0o644)
        permissions = barrier_status(runtime_dir)
        assert permissions["active"] is True
        assert permissions["status"] == "invalid_fail_closed"
        assert "private mode 0600" in permissions["error"]


def main() -> int:
    _assert_lifecycle_survives_process_restart()
    _assert_corrupt_state_fails_closed()
    print("business_data_write_barrier_smoke: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
