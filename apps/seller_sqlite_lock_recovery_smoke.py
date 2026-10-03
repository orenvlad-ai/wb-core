"""Local DELETE-journal regression for Seller-related SQLite contention."""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta, timezone
import importlib
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.calculation_parameters import CalculationParametersBlock  # noqa: E402
from packages.application.calculation_parameters_v4 import ProxyV4ParametersBlock  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint  # noqa: E402
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402


def _constructor_does_not_write_when_schema_is_ready() -> None:
    with TemporaryDirectory(prefix="seller-lock-schema-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        CalculationParametersBlock(runtime=runtime)
        ProxyV4ParametersBlock(runtime=runtime)
        reader = sqlite3.connect(runtime.db_path, timeout=0.05)
        try:
            assert reader.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
            reader.execute("BEGIN")
            reader.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            started = time.monotonic()
            CalculationParametersBlock(runtime=runtime)
            ProxyV4ParametersBlock(runtime=runtime)
            assert time.monotonic() - started < 2, "ready constructors waited for a writer lock"
        finally:
            reader.rollback()
            reader.close()


def _failed_admission_never_starts_wb_fetch() -> None:
    class Runtime:
        def __init__(self, runtime_dir: Path) -> None:
            self.runtime_dir = runtime_dir
            self.rows: dict[str, dict] = {}
            self.create_calls = 0

        def load_active_wb_supply_transit_cost_enrichment_run(self):
            return None

        def create_wb_supply_transit_cost_enrichment_run(self, **fields):
            self.create_calls += 1
            raise sqlite3.OperationalError("database is locked")

        def load_wb_supply_transit_cost_enrichment_run(self, run_id):
            return self.rows.get(run_id)

    with TemporaryDirectory(prefix="seller-lock-admission-") as tmp:
        runtime = Runtime(Path(tmp))
        block = object.__new__(WbSuppliesBlock)
        block.runtime = runtime
        block.timestamp_factory = lambda: "2026-10-03T10:00:00Z"
        block._transit_cost_run_lock = threading.Lock()
        block._transit_cost_threads = {}
        block._transit_cost_uncertain_runs = {}
        block._select_transit_cost_enrichment_candidates = lambda _request: [{"supply_id": "123"}]
        called = []
        block._run_transit_cost_enrichment_guarded = lambda *_args: called.append("fetch")
        try:
            block.start_transit_cost_enrichment({"limit": 1})
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
        else:
            raise AssertionError("failed DB admission was accepted")
        assert runtime.create_calls == 1 and not called and not block._transit_cost_threads

        # A lost response after the same run_id was committed is read back;
        # it must not cause a second INSERT or a second worker.
        def committed_but_lost(**fields):
            runtime.create_calls += 1
            runtime.rows[fields["run_id"]] = dict(fields)
            raise sqlite3.OperationalError("database is locked")

        runtime.create_wb_supply_transit_cost_enrichment_run = committed_but_lost
        accepted = block.start_transit_cost_enrichment({"limit": 1})
        for thread in list(block._transit_cost_threads.values()):
            thread.join(timeout=2)
        assert accepted["status"] == "running"
        assert runtime.create_calls == 2 and called == ["fetch"]


def _probe_survives_health_cache_lock() -> None:
    class Recovery:
        def check_session(self, **_kwargs):
            return {
                "status": "session_valid_canonical",
                "status_tone": "success",
                "organization_confirmed": True,
                "expected_supplier_id": "123",
                "current_supplier_id": "123",
                "current_storage_probe": {"ok": True, "checked_at": "2026-10-03T10:00:00Z"},
            }

    class Runtime:
        def save_source_health_status(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def load_source_health_status(self, _source):
            return {"checked_at": "2026-10-02T10:00:00Z", "session_status": "probe_error"}

    entry = object.__new__(RegistryUploadHttpEntrypoint)
    entry.seller_portal_recovery = Recovery()
    entry.runtime = Runtime()
    entry.activated_at_factory = lambda: "2026-10-03T10:00:00Z"
    checked = entry.handle_seller_portal_session_check_request(launcher_download_path="/fixture")
    assert checked["status"] == "session_valid_canonical"
    assert checked["health_persistence_status"] == "unconfirmed"
    assert checked["health_persistence_warning"]

    class SameProbeRuntime(Runtime):
        def load_source_health_status(self, _source):
            return {
                "checked_at": "2026-10-03T10:00:00Z",
                "session_status": "session_valid_canonical",
                "session_status_label": "",
                "organization_confirmed": True,
                "expected_supplier_label": "",
                "expected_supplier_id": "123",
                "current_supplier_id": "123",
                "reason": "",
            }

    entry.runtime = SameProbeRuntime()
    confirmed = entry.handle_seller_portal_session_check_request(launcher_download_path="/fixture")
    assert confirmed["health_persistence_status"] == "saved_observed"
    assert "health_persistence_warning" not in confirmed


def _orphaned_admission_is_not_reported_running_after_restart() -> None:
    class Runtime:
        runtime_dir = Path("/tmp/seller-lock-fixture-does-not-exist")

        def load_wb_supply_transit_cost_enrichment_run(self, _run_id):
            return {
                "run_id": "lost-worker", "status": "running", "phase": "worker_admitted",
                "started_at": "2026-10-03T10:00:00Z", "updated_at": "2026-10-03T10:00:00Z",
            }

    block = object.__new__(WbSuppliesBlock)
    block.runtime = Runtime()
    block.timestamp_factory = lambda: "2026-10-03T10:06:00Z"
    block._transit_cost_threads = {}
    block._transit_cost_uncertain_runs = {}
    block.transit_cost_coverage = lambda: {}
    observed = block.get_transit_cost_enrichment_status({"run_id": "lost-worker"})["run"]
    assert observed["status"] == "unknown" and observed["durable_status"] == "running"
    assert observed["phase"] == "collector_outcome_unknown"

    class OtherProcessFetching(Runtime):
        def load_wb_supply_transit_cost_enrichment_run(self, _run_id):
            return {
                "run_id": "other-worker", "status": "running", "phase": "browser_network_json",
                "started_at": "2026-10-03T10:00:00Z", "updated_at": "2026-10-03T10:00:00Z",
            }

    block.runtime = OtherProcessFetching()
    other_worker = block.get_transit_cost_enrichment_status({"run_id": "other-worker"})["run"]
    assert other_worker["status"] == "running", "another process may still be fetching at six minutes"

    class QueuedRuntime(Runtime):
        def load_active_wb_supply_transit_cost_enrichment_run(self):
            return {
                "run_id": "legacy-queued", "status": "queued", "phase": "queued",
                "started_at": "2026-10-03T10:00:00Z", "updated_at": "2026-10-03T10:00:00Z",
            }

    block.runtime = QueuedRuntime()
    block._transit_cost_run_lock = threading.Lock()
    block._select_transit_cost_enrichment_candidates = lambda _request: (_ for _ in ()).throw(
        AssertionError("uncertain legacy run must not select or fetch candidates")
    )
    legacy = block.start_transit_cost_enrichment({"limit": 1})
    assert legacy["status"] == "unknown" and legacy["accepted"] is False
    assert legacy["run_id"] == "legacy-queued"


def _no_candidates_is_terminal_in_one_insert() -> None:
    with TemporaryDirectory(prefix="seller-lock-empty-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        block = WbSuppliesBlock(runtime=runtime)
        block._select_transit_cost_enrichment_candidates = lambda _request: []
        runtime.update_wb_supply_transit_cost_enrichment_run = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("empty run must not need a second status write")
        )
        accepted = block.start_transit_cost_enrichment({"limit": 1})
        durable = runtime.load_wb_supply_transit_cost_enrichment_run(accepted["run_id"])
        assert accepted["status"] == "success"
        assert durable["status"] == "success" and durable["phase"] == "no_candidates"
        assert durable["completed_at"] == durable["started_at"]


def _durable_probe_prevents_old_health_cache_rollback() -> None:
    with TemporaryDirectory(prefix="seller-lock-history-") as tmp:
        state = Path(tmp)
        storage = state / "storage_state.json"
        storage.write_text('{"cookies":[],"origins":[]}', encoding="utf-8")
        history = state / "session_probe_history.jsonl"
        tool = importlib.import_module("apps.seller_portal_relogin_session")
        now = datetime.now(timezone.utc)
        checked_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        history.write_text(json.dumps({
            "checked_at": checked_at, "ok": True,
            "status": "seller_portal_session_valid",
            "supplier_context": {"current_supplier_id": "123"},
            "storage_state_meta": tool._public_storage_state_metadata(storage),
        }) + "\n", encoding="utf-8")

        class Recovery:
            def _config(self):
                return SimpleNamespace(probe_history_path=history, storage_state_path=storage)

            def _tool(self):
                return tool

        class Runtime:
            def load_source_health_status(self, source_key):
                if source_key == "seller_portal_auth":
                    return {"session_status": "session_invalid", "organization_confirmed": False,
                            "checked_at": "2026-10-02T10:00:00Z"}
                return {}

            def load_sheet_vitrina_refresh_status(self):
                raise ValueError("fixture")

        entry = object.__new__(RegistryUploadHttpEntrypoint)
        entry.seller_portal_recovery = Recovery()
        entry.runtime = Runtime()
        entry.activated_at_factory = lambda: checked_at
        entry.handle_seller_portal_recovery_status_request = lambda **_kw: {"expected_supplier_id": "123"}
        entry.handle_wb_buyer_session_recovery_status_request = lambda **_kw: {}
        entry.wb_supplies_block = SimpleNamespace(get_transit_cost_enrichment_status=lambda _params: {"coverage": {}})
        first = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert first["session_status"] == "session_valid_canonical"
        assert first["organization_confirmed"] is True
        assert first["health_persistence_status"] == "probe_history"

        class SameSecondOldFailure(Runtime):
            def load_source_health_status(self, source_key):
                result = super().load_source_health_status(source_key)
                if source_key == "seller_portal_auth":
                    result["checked_at"] = checked_at
                return result

        entry.runtime = SameSecondOldFailure()
        same_second = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert same_second["session_status"] == "session_valid_canonical"
        assert same_second["health_persistence_status"] == "probe_history"

        class SameSecondSaved(SameSecondOldFailure):
            def load_source_health_status(self, source_key):
                result = super().load_source_health_status(source_key)
                if source_key == "seller_portal_auth":
                    result.update({"session_status": "session_valid_canonical", "organization_confirmed": True})
                return result

        entry.runtime = SameSecondSaved()
        saved = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert saved["session_status"] == "session_valid_canonical"
        assert "health_persistence_warning" not in saved
        entry.runtime = Runtime()

        # A rotated storage state cannot inherit the earlier positive proof.
        storage.write_text('{"cookies":[{"name":"changed"}],"origins":[]}', encoding="utf-8")
        rotated = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert rotated["session_status"] != "session_valid_canonical"
        assert rotated["organization_confirmed"] is False

        history.write_text(json.dumps({
            "checked_at": checked_at, "ok": True,
            "status": "seller_portal_session_valid",
            "supplier_context": "malformed",
            "storage_state_meta": tool._public_storage_state_metadata(storage),
        }) + "\n", encoding="utf-8")
        malformed = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert malformed["organization_confirmed"] is False
        assert malformed["session_status"] != "session_valid_canonical"

        history.write_text(json.dumps({
            "checked_at": checked_at, "ok": "false",
            "status": "seller_portal_session_valid",
            "supplier_context": {"current_supplier_id": "123"},
            "storage_state_meta": tool._public_storage_state_metadata(storage),
        }) + "\n", encoding="utf-8")
        bad_boolean = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert bad_boolean["session_status"] != "session_valid_canonical"

        history.write_text(json.dumps({
            "checked_at": (now - timedelta(minutes=6)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "ok": True, "status": "seller_portal_session_valid",
            "supplier_context": {"current_supplier_id": "123"},
            "storage_state_meta": tool._public_storage_state_metadata(storage),
        }) + "\n", encoding="utf-8")
        stale = entry.handle_sources_sessions_status_request(
            seller_launcher_download_path="/fixture", buyer_launcher_download_path="/fixture",
        )["seller_portal"]["authorization"]
        assert stale["health_persistence_status"] == "stale_probe_history"
        assert stale["session_status_label"] == "Нужна повторная проверка"


def main() -> None:
    _constructor_does_not_write_when_schema_is_ready()
    _failed_admission_never_starts_wb_fetch()
    _probe_survives_health_cache_lock()
    _orphaned_admission_is_not_reported_running_after_restart()
    _no_candidates_is_terminal_in_one_insert()
    _durable_probe_prevents_old_health_cache_rollback()
    print("seller_sqlite_lock_recovery_smoke: OK")


if __name__ == "__main__":
    main()
