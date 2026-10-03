"""Fixture-only run identity and cancellation checks for Seller relogin."""

from __future__ import annotations

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import seller_portal_relogin_session as recovery  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="seller-relogin-race-") as raw:
        root = Path(raw)
        canonical = root / "storage_state.json"
        canonical.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        before = canonical.read_bytes()
        config = recovery.ReloginSessionConfig(
            state_dir=root, storage_state_path=canonical,
            canonical_supplier_id="fixture-supplier", canonical_supplier_label="Fixture seller",
        )
        old_run = "seller-recovery-20261003T090000Z-aaaaaaaa"
        owner = "a" * 64
        recovery._write_status(config, {"run_id": old_run, "status": "awaiting_login", "viewer_owner": owner,
                                        "viewer_expires_at": 2_000_000_000})
        config.pid_path.write_text("4242", encoding="utf-8")
        active = {"value": True}

        def running(_pid: int) -> bool:
            return active["value"]

        def kill(_pid: int, _signal: int) -> None:
            active["value"] = False

        with patch.object(recovery, "_pid_is_running", side_effect=running), patch.object(recovery.os, "killpg", side_effect=kill), patch.object(recovery.time, "sleep", return_value=None):
            with patch.object(recovery, "probe_storage_state", return_value={"ok": True, "status": "ok"}), patch.object(recovery, "_probe_matches_canonical_supplier", return_value=True):
                try:
                    recovery.start_relogin_session(config, replace=False, viewer_owner="b" * 64)
                    raise AssertionError("foreign owner joined an active Seller login")
                except PermissionError:
                    pass
                queued = recovery.request_login_finish(config, requested_run_id=old_run)
                assert queued.get("finish_requested") is True
                assert canonical.read_bytes() == before, "click must not save candidate or canonical state"
                assert config.finish_request_path.exists()
                stale_stop = recovery.stop_relogin_session(config, requested_run_id="foreign-run")
                assert stale_stop.get("run_failure_code") == "run_replaced"
                assert config.finish_request_path.exists(), "stale cancel must not erase current finish request"
                stopped = recovery.stop_relogin_session(config, requested_run_id=old_run)
                assert stopped.get("status") == "stopped"
                assert not config.finish_request_path.exists()
                next_run = recovery.start_relogin_session(config, replace=False, viewer_owner=owner, viewer_expires_at=2_000_000_000)
                assert next_run.get("status") == "not_needed" and next_run.get("run_id") != old_run
                stale_finish = recovery.request_login_finish(config, requested_run_id=old_run)
                assert stale_finish.get("run_failure_code") == "run_replaced"
                assert not config.finish_request_path.exists()
                assert canonical.read_bytes() == before
                # Supervisor status updates must retain the request nonce so
                # a lost start response can still cancel the active browser.
                active["value"] = True
                config.pid_path.write_text("4242", encoding="utf-8")
                active_run = "seller-recovery-20261003T090001Z-bbbbbbbb"
                active_request_id = "3" * 32
                recovery._write_status(config, {"run_id": active_run, "status": "starting", "viewer_owner": owner,
                                                "request_id": active_request_id})
                recovery._write_status(config, {"status": "awaiting_login"})
                assert recovery.read_session_status(config, with_probe=False)["request_id"] == active_request_id
                cancelled = recovery.request_login_cancel(config, request_id=active_request_id, viewer_owner=owner)
                assert cancelled["status"] == "stopped" and not config.pid_path.exists()
                assert canonical.read_bytes() == before

    # Start can spend up to a minute probing before it has a supervisor PID.
    # Its run must still be visible and cancellable during that interval.
    with TemporaryDirectory(prefix="seller-relogin-pending-start-") as raw:
        root = Path(raw)
        canonical = root / "storage_state.json"
        canonical.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        before = canonical.read_bytes()
        config = recovery.ReloginSessionConfig(
            state_dir=root, storage_state_path=canonical,
            canonical_supplier_id="fixture-supplier", canonical_supplier_label="Fixture seller",
        )
        probe_entered = threading.Event()
        release_probe = threading.Event()
        start_result: list[dict] = []
        stop_result: list[dict] = []
        request_id = "1" * 32

        def slow_probe(*_args, **_kwargs):
            probe_entered.set()
            assert release_probe.wait(timeout=5), "fixture probe was never released"
            return {"ok": False, "status": "needs_login"}

        with patch.object(recovery, "probe_storage_state", side_effect=slow_probe), patch.object(recovery.subprocess, "Popen", side_effect=AssertionError("cancelled run opened a browser")):
            starter = threading.Thread(target=lambda: start_result.append(recovery.start_relogin_session(config, viewer_owner=owner, viewer_expires_at=2_000_000_000, request_id=request_id)))
            starter.start()
            assert probe_entered.wait(timeout=2)
            pending = recovery.read_session_status(config, with_probe=False)
            assert pending["status"] == "starting" and pending["running"] and pending["start_pending"]
            assert pending["viewer_owner"] == owner and pending["run_id"]
            assert pending["request_id"] == request_id
            stopper = threading.Thread(target=lambda: stop_result.append(recovery.request_login_cancel(config, request_id=request_id, viewer_owner=owner)))
            stopper.start()
            time.sleep(0.05)
            assert stopper.is_alive(), "cancel must wait for the in-flight start lock"
            release_probe.set()
            starter.join(timeout=5)
            stopper.join(timeout=5)
            assert not starter.is_alive() and not stopper.is_alive()
            assert start_result and start_result[0]["status"] == "stopped"
            assert stop_result and stop_result[0]["status"] == "stopped"
            assert not config.pid_path.exists() and not config.cancel_request_path(request_id, owner).exists()
            assert recovery.read_session_status(config, with_probe=False)["running"] is False
            assert canonical.read_bytes() == before
        with patch.object(recovery, "probe_storage_state", side_effect=AssertionError("duplicate start probed")):
            replay = recovery.start_relogin_session(config, viewer_owner=owner, request_id=request_id)
        assert replay["status"] == "stopped" and replay["run_id"] == pending["run_id"]

    # Exercise the real supervisor status sequence with fixture processes:
    # each write must retain the nonce through a capture failure.
    with TemporaryDirectory(prefix="seller-relogin-supervisor-nonce-") as raw:
        root = Path(raw)
        canonical = root / "storage_state.json"
        canonical.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        config = recovery.ReloginSessionConfig(
            state_dir=root, storage_state_path=canonical,
            canonical_supplier_id="fixture-supplier", canonical_supplier_label="Fixture seller",
        )
        request_id = "5" * 32
        recovery._write_status(config, {"run_id": "seller-recovery-20261003T090002Z-cccccccc",
                                        "status": "starting", "viewer_owner": owner, "request_id": request_id})
        class FakeLock:
            def release(self):
                pass
        with patch.object(recovery, "_ensure_required_commands", return_value=None), patch.object(recovery, "acquire_seller_portal_automation_lock", return_value=FakeLock()), patch.object(recovery, "_spawn", return_value=object()), patch.object(recovery, "_command_path", return_value=None), patch.object(recovery, "_wait_for_display_socket", return_value=None), patch.object(recovery, "_wait_for_port", return_value=None), patch.object(recovery, "_terminate_process", return_value=None), patch.object(recovery, "run_login_capture", return_value={"status": "error", "run_failure_code": "fixture_capture_failed", "message": "fixture"}):
            assert recovery.supervise_relogin_session(config) == 1
        final = recovery.read_session_status(config, with_probe=False)
        assert final["status"] == "error" and final["request_id"] == request_id
        assert final["viewer_owner"] == owner and not config.pid_path.exists()

        # Cancellation can arrive before the start handler acquires its lock.
        earlier_request_id = "2" * 32
        result = recovery.request_login_cancel(config, request_id=earlier_request_id, viewer_owner=owner)
        assert result["run_failure_code"] == "cancel_pending_start"
        with patch.object(recovery, "probe_storage_state", side_effect=AssertionError("cancelled request probed")):
            result = recovery.start_relogin_session(config, viewer_owner=owner, request_id=earlier_request_id)
        assert result["status"] == "stopped" and result["run_failure_code"] == "cancelled_before_start"
        assert not config.cancel_request_path(earlier_request_id, owner).exists()
        assert canonical.read_bytes() == before

    # There is another small window after the preflight but before Popen/PID.
    # The second starting status must still be visible as pending there.
    with TemporaryDirectory(prefix="seller-relogin-before-popen-") as raw:
        root = Path(raw)
        canonical = root / "storage_state.json"
        canonical.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        before = canonical.read_bytes()
        config = recovery.ReloginSessionConfig(
            state_dir=root, storage_state_path=canonical,
            canonical_supplier_id="fixture-supplier", canonical_supplier_label="Fixture seller",
        )
        before_popen = threading.Event()
        release_popen = threading.Event()
        process_alive = {"value": True}
        killed: list[int] = []
        started: list[dict] = []
        cancelled: list[dict] = []
        request_id = "4" * 32
        original_write = recovery._write_status

        def pause_after_second_status(cfg, payload):
            original_write(cfg, payload)
            if payload.get("message") == "server-side seller relogin session is starting":
                before_popen.set()
                assert release_popen.wait(timeout=5), "fixture Popen was never released"

        class FakeProcess:
            pid = 4244

        def fake_kill(pid: int, _signal: int) -> None:
            killed.append(pid)
            process_alive["value"] = False

        with patch.object(recovery, "probe_storage_state", return_value={"ok": False, "status": "needs_login"}), patch.object(recovery, "_write_status", side_effect=pause_after_second_status), patch.object(recovery.subprocess, "Popen", return_value=FakeProcess()), patch.object(recovery, "_pid_is_running", side_effect=lambda _pid: process_alive["value"]), patch.object(recovery.os, "killpg", side_effect=fake_kill):
            starter = threading.Thread(target=lambda: started.append(recovery.start_relogin_session(config, viewer_owner=owner, request_id=request_id)))
            starter.start()
            assert before_popen.wait(timeout=2)
            pending = recovery.read_session_status(config, with_probe=False)
            assert pending["start_pending"] and pending["running"] and pending["request_id"] == request_id
            stopper = threading.Thread(target=lambda: cancelled.append(recovery.request_login_cancel(config, request_id=request_id, viewer_owner=owner)))
            stopper.start()
            time.sleep(0.05)
            assert stopper.is_alive()
            release_popen.set()
            starter.join(timeout=5)
            stopper.join(timeout=5)
            assert not starter.is_alive() and not stopper.is_alive()
            assert started and cancelled and cancelled[0]["status"] == "stopped"
            assert killed == [FakeProcess.pid] and not config.pid_path.exists()
            assert recovery.read_session_status(config, with_probe=False)["running"] is False
            assert canonical.read_bytes() == before
    print("seller_portal_relogin_run_race_smoke: OK")


if __name__ == "__main__":
    main()
