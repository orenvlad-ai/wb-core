"""Apply an exact, previously rehearsed native hourly plan during a held pause.

Preview/readback use query-only connections. Native apply retains its ordinary
source/date CAS, serialized writer and T2 recovery; no snapshot or balance is
fabricated and maintenance is never released by this adapter.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from apps.production_apply_contract import AdapterError, AmbiguousSubmit
from packages.application.fbs_accounting_apply import target
from packages.application.fbs_historical_fbo_action import _held
from packages.application.storage_registry import StoreRegistry
from packages.application import warehouse_functional as native
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock
from packages.business_time import current_business_date_iso

CONTRACT = "warehouse_reviewed_hourly_sync_v1"
P = "sheet_vitrina_v1_warehouse_"


def require(value, reason):
    if not value:
        raise AdapterError("reviewed_sync_" + reason)


def readonly(path):
    conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def plan_for(request):
    path = Path(request["plan_path"])
    require(path.is_absolute() and not path.is_symlink() and path.is_file(), "regular_plan_required")
    require(path.stat().st_size <= 32 * 1024**2, "plan_size")
    data = path.read_bytes()
    require(hashlib.sha256(data).hexdigest() == request["plan_sha256"], "plan_file_changed")
    plan = json.loads(data)
    require(plan.get("kind") == "hourly_wb_sync", "hourly_plan_only")
    require(plan.get("plan_fingerprint") == native._fingerprint(
        {k: v for k, v in plan.items() if k != "plan_fingerprint"}), "plan_fingerprint")
    require(not plan.get("targeted_recalc_requests"), "separate_targeted_work_required")
    require(not plan.get("new_events"), "separate_business_events_required")
    require(not (plan.get("historical_correction") or {}).get("required")
            and not (plan.get("historical_correction") or {}).get("row_count"),
            "separate_historical_correction_required")
    return plan


def committed(conn, plan):
    row = conn.execute(
        f"SELECT * FROM {P}functional_versions WHERE plan_fingerprint=?",
        (plan["plan_fingerprint"],)).fetchone()
    if row is None:
        return None
    version = dict(row)
    require(version["status"] == "good" and version["business_effective_date"] == plan["effective_date"],
            "committed_version_conflict")
    snapshot = conn.execute(
        f"SELECT snapshot_id,snapshot_date FROM {P}wb_snapshots WHERE version_id=?",
        (version["version_id"],)).fetchone()
    require(snapshot is not None and snapshot["snapshot_id"] == plan["wb_snapshot"]["snapshot_id"]
            and snapshot["snapshot_date"] == plan["effective_date"], "committed_snapshot_conflict")
    return version


def capture(conn, plan):
    require(plan["effective_date"] == current_business_date_iso(), "current_business_date_required")
    require(plan["wb_snapshot"]["snapshot_date"] == plan["effective_date"], "snapshot_day_conflict")
    require(native._current_snapshot_effective_date(
        captured_at=plan["captured_at"], snapshot_date=plan["wb_snapshot"]["snapshot_date"])
        == plan["effective_date"], "captured_day_conflict")
    active = conn.execute(f"SELECT version_id FROM {P}functional_active WHERE slot=1").fetchone()
    require(active is not None and active[0] == plan["base_active_version_id"], "active_version_drift")
    source = native._functional_local_source_view(native._source_rows(
        conn, recovery_end_date=plan["effective_date"]))
    source_digest = "sha256:" + native._hash(native._guarded_local_sources(source))
    require(source_digest == plan["local_source_digest"], "local_source_drift")
    supply_digest = "sha256:" + native._hash(native._current_wb_supply_revisions(conn))
    require(supply_digest == plan["wb_supply_source_digest"], "supply_source_drift")
    return dict(active_version=active[0], local_source_digest=source_digest,
                wb_supply_source_digest=supply_digest)


class WarehouseReviewedSyncAdapter:
    def preview(self, request, operation_id):
        root = target(request)
        plan = plan_for(request)
        db = StoreRegistry(root).resolve("operational")
        with _held(root, request), closing(readonly(db)) as conn:
            conn.execute("BEGIN")
            existing = committed(conn, plan)
            before = (dict(active_version=plan["base_active_version_id"],
                local_source_digest=plan["local_source_digest"],
                wb_supply_source_digest=plan["wb_supply_source_digest"])
                if existing else capture(conn, plan))
        return dict(operation_id=operation_id, target=str(root),
            scope=dict(contract=CONTRACT, action="native_hourly_sync",
                business_date=plan["effective_date"], snapshot_id=plan["wb_snapshot"]["snapshot_id"],
                no_new_business_events=True, maintenance_remains_held=True),
            prestate_sha256=native._fingerprint(before),
            candidate_sha256=plan["plan_fingerprint"],
            recovery=dict(kind="native_hourly_warehouse_sync_T2",
                plan_fingerprint=plan["plan_fingerprint"]), before=before)

    def apply(self, request, operation_id, preview):
        root = target(request)
        plan = plan_for(request)
        require(preview["candidate_sha256"] == plan["plan_fingerprint"], "reviewed_candidate_drift")
        db = StoreRegistry(root).resolve("operational")
        with _held(root, request), warehouse_functional_job_lock(root):
            with closing(readonly(db)) as conn:
                conn.execute("BEGIN")
                if committed(conn, plan):
                    return dict(operation_id=operation_id, disposition="already_applied")
                require(native._fingerprint(capture(conn, plan)) == preview["prestate_sha256"],
                        "prestate_drift")
            from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
            runtime = RegistryUploadDbBackedRuntime(runtime_dir=root, store_registry=StoreRegistry(root))
            block = native.WarehouseFunctionalBlock(runtime=runtime)
            try:
                block.apply_plan(plan, confirm_fingerprint=plan["plan_fingerprint"])
            except Exception as exc:
                # Once native apply starts, inspect this fingerprint, never
                # turn an uncertain result into a new submission identity.
                raise AmbiguousSubmit("inspect_native_hourly_plan_receipt") from exc
        return dict(operation_id=operation_id, disposition="submitted")

    def readback(self, request, operation_id):
        root = target(request)
        plan = plan_for(request)
        with closing(readonly(StoreRegistry(root).resolve("operational"))) as conn:
            conn.execute("BEGIN")
            existing = committed(conn, plan)
            recovery = conn.execute(
                "SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations "
                "WHERE operation_kind='hourly_warehouse_sync' AND plan_fingerprint=?",
                (plan["plan_fingerprint"],)).fetchone()
            if existing is None:
                return dict(operation_id=operation_id,
                    state="ambiguous" if recovery else "not_submitted")
            active = conn.execute(f"SELECT version_id FROM {P}functional_active WHERE slot=1").fetchone()
            lifecycle = str(recovery[0]) if recovery else "missing"
            return dict(operation_id=operation_id, state="applied",
                version_id=existing["version_id"], business_date=existing["business_effective_date"],
                current_active=bool(active and active[0] == existing["version_id"]),
                recovery_lifecycle=lifecycle,
                recovery_pending=lifecycle not in {"retained", "released"})
