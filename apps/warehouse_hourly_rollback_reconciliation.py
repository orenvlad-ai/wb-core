"""Native, receipt-bound reconciliation of an expired hourly plan's rollback.

This adapter changes only the recovery lifecycle. It never restores a domain
checkpoint, rewrites source documents, publishes a warehouse version or retries
the failed business transaction. Preview and readback are strictly query-only.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from apps.production_apply_contract import AdapterError, AmbiguousSubmit
from packages.business_time import business_date_from_timestamp
from packages.application.storage_registry import StoreRegistry
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
from packages.application.warehouse_recovery_policy import WarehouseRecoveryRegistry, _sha256_file

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "hourly_checkpoint_date_boundary_reconciliation_v1"
ERROR = "functional plan crossed the canonical business-date boundary while acquiring apply lock"
PREFIX = "sheet_vitrina_v1_warehouse_"
# These are publication operands, not mutable source/document queues or journals.
# Version rows are inserted unconditionally in the same SQLite transaction as
# every hourly output. Equality plus candidate absence proves no native commit.
CORE = (
    "functional_active", "functional_versions", "functional_cutovers",
    "wb_daily_cost", "wb_daily_cost_corrections", "opening_cost_map",
    "supplier_flows", "functional_events", "supplier_cost_states",
    "functional_ff_reservations", "functional_documents",
)
SCOPED = (
    "functional_balances", "functional_read_models", "wb_option_read_models",
)


def require(condition, reason):
    if not condition:
        raise AdapterError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=lambda value: {"bytes_hex": value.hex()} if isinstance(value, bytes) else str(value)).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(encoded(value)).hexdigest()


def readonly(path):
    conn = sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def timestamp(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def context(request, *, require_deployed_sha=True):
    require(set(request) == {"runtime_dir", "deployed_sha", "recovery_operation_id", "cycle_id", "warehouse_run_id"}, "hourly-reconciliation-request-scope")
    require(re.fullmatch(r"[0-9a-f]{40}", request["deployed_sha"]) is not None, "hourly-reconciliation-exact-sha-required")
    marker = ROOT / ".wb-core-runtime-sha"
    if require_deployed_sha:
        require(marker.is_file() and marker.read_text().strip() == request["deployed_sha"], "hourly-reconciliation-deployed-sha-drift")
    require(re.fullmatch(r"[0-9a-f]{32}", request["cycle_id"]) is not None, "hourly-reconciliation-cycle-id")
    root = Path(request["runtime_dir"]).resolve()
    db = StoreRegistry(root).resolve("operational")
    require(db.is_file(), "hourly-reconciliation-store-missing")
    return root, db


def table_proof(conn, name, version_ids=None):
    table = PREFIX + name
    schema = [tuple(row) for row in conn.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? ORDER BY type,name", (table,))]
    require(any(row[0] == "table" for row in schema), "hourly-reconciliation-table-missing:" + name)
    columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    where = " WHERE version_id IN (?,?)" if version_ids is not None else ""
    query = f'SELECT * FROM "{table}"' + where + " ORDER BY " + ",".join('"'+column+'"' for column in columns)
    sha = hashlib.sha256()
    count = 0
    for row in conn.execute(query, version_ids or ()):
        sha.update(encoded(tuple(row)) + b"\n")
        count += 1
    return {"schema_sha256": digest(schema), "rows": count, "rows_sha256": "sha256:" + sha.hexdigest()}


def capture(request, *, conn=None):
    root, db = context(request)
    if conn is None:
        with closing(readonly(db)) as reader:
            reader.execute("BEGIN")
            return capture(request, conn=reader)
    operation_id = request["recovery_operation_id"]
    row = conn.execute("SELECT * FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?", (operation_id,)).fetchone()
    require(row is not None, "hourly-reconciliation-operation-missing")
    operation = dict(row)
    scope = json.loads(operation["target_scope_json"])
    require(operation["operation_kind"] == "hourly_warehouse_sync" and operation["tier"] == "T2"
            and operation["lifecycle_state"] == "failed_recoverable" and operation["writer_state"] == "failed"
            and operation["next_action"] == "resume_or_rollback_hourly_wb_sync"
            and not operation["after_digest"]
            and set(scope) == {"base_active_version_id", "effective_date"}, "hourly-reconciliation-operation-not-exact")
    base = scope["base_active_version_id"]
    candidate = "whfv_" + operation["plan_fingerprint"].removeprefix("sha256:")[:24]
    require(conn.execute("SELECT version_id FROM " + PREFIX + "functional_active WHERE slot=1").fetchone()[0] == base,
            "hourly-reconciliation-active-version-drift")
    require(conn.execute("SELECT 1 FROM " + PREFIX + "functional_versions WHERE version_id=? OR plan_fingerprint=?",
                         (candidate, operation["plan_fingerprint"])).fetchone() is None, "hourly-reconciliation-candidate-already-published")
    transitions = [dict(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_recovery_transitions WHERE operation_id=? ORDER BY transition_id", (operation_id,))]
    verified = [row for row in transitions if row["to_state"] == "verified"]
    failed = [row for row in transitions if row["to_state"] == "failed_recoverable"]
    require(len(verified) == 1 and len(failed) == 1 and failed[0]["from_state"] == "mutation_running", "hourly-reconciliation-transition-history")
    run = conn.execute("SELECT * FROM " + PREFIX + "update_runs WHERE run_id=?", (request["warehouse_run_id"],)).fetchone()
    phases = {row["phase_key"]: dict(row) for row in conn.execute("SELECT * FROM " + PREFIX + "update_phases WHERE run_id=?", (request["warehouse_run_id"],))}
    require(run is not None and run["status"] == "failed" and run["trigger_source"] == "cycle"
            and run["last_error"] == "functional_publication: " + ERROR, "hourly-reconciliation-canonical-run-error")
    publication = phases.get("functional_publication", {})
    stocks = phases.get("official_complete_wb_stocks", {})
    require(publication.get("status") == "failed" and publication.get("last_error") == ERROR
            and stocks.get("status") == "success"
            and json.loads(stocks.get("details_json", "{}"))["plan_fingerprint"] == operation["plan_fingerprint"],
            "hourly-reconciliation-canonical-phase-plan")
    require(timestamp(publication["started_at"]) <= timestamp(operation["created_at"])
            <= timestamp(verified[0]["transitioned_at"]) <= timestamp(failed[0]["transitioned_at"])
            <= timestamp(publication["finished_at"]), "hourly-reconciliation-time-identity")
    require(business_date_from_timestamp(failed[0]["transitioned_at"]) != scope["effective_date"], "hourly-reconciliation-no-date-crossing")
    cycle_path = root / "sheet-vitrina-cycles" / (request["cycle_id"] + ".json")
    require(cycle_path.is_file() and cycle_path.stat().st_size <= 16 * 1024**2, "hourly-reconciliation-cycle-missing")
    cycle_bytes = cycle_path.read_bytes()
    cycle = json.loads(cycle_bytes)
    warehouse = [stage for stage in cycle["stages"] if stage["stage"] == "warehouse"]
    require(cycle["cycle_id"] == request["cycle_id"] and cycle["business_date"] == scope["effective_date"]
            and len(warehouse) == 1 and warehouse[0]["durable_ref"] == request["warehouse_run_id"]
            and warehouse[0]["status"] == "failed", "hourly-reconciliation-cycle-run-link")
    artifacts = [dict(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_recovery_artifacts WHERE operation_id=? ORDER BY artifact_id", (operation_id,))]
    checkpoint_rows = [row for row in artifacts if row["artifact_kind"] == "domain_checkpoint" and row["state"] == "verified"]
    require(len(checkpoint_rows) == 1, "hourly-reconciliation-checkpoint-missing")
    checkpoint = Path(checkpoint_rows[0]["path"])
    registry = WarehouseRecoveryRegistry(runtime_dir=root, db_path=db)
    require(checkpoint.is_file() and any(checkpoint.resolve().is_relative_to(path) for path in registry.recovery_roots)
            and not checkpoint.is_symlink(), "hourly-reconciliation-checkpoint-path")
    for artifact in artifacts:
        path = Path(artifact["path"])
        require(artifact["state"] == "verified" and path.is_file()
                and any(path.resolve().is_relative_to(allowed) for allowed in registry.recovery_roots)
                and not path.is_symlink() and _sha256_file(path) == artifact["digest"], "hourly-reconciliation-artifact-identity")
    require(checkpoint_rows[0]["digest"] == operation["checkpoint_digest"], "hourly-reconciliation-checkpoint-digest")
    proofs = {}
    with closing(readonly(checkpoint)) as source:
        metadata_rows = [dict(row) for row in source.execute("SELECT * FROM recovery_checkpoint_metadata")]
        require(len(metadata_rows) == 1, "hourly-reconciliation-checkpoint-metadata-count")
        metadata = metadata_rows[0]
        require(metadata["operation_id"] == operation_id
                and metadata["contract_name"] == "warehouse_recovery_policy_v1"
                and metadata["plan_fingerprint"] == operation["plan_fingerprint"]
                and metadata["source_digest"] == operation["source_digest"], "hourly-reconciliation-checkpoint-metadata-identity")
        watermarks = json.loads(metadata["source_watermarks_json"])
        require(watermarks.get("base_active_version_id") == base
                and watermarks.get("effective_date") == scope["effective_date"]
                and watermarks.get("local_source_digest") == operation["source_digest"]
                and metadata["schema_revision"] == "v2", "hourly-reconciliation-checkpoint-watermarks")
        for name in CORE + SCOPED:
            versions = (base, candidate) if name in SCOPED else None
            before = table_proof(source, name, versions)
            require(before == table_proof(conn, name, versions), "hourly-reconciliation-publication-drift:" + name)
            proofs[name] = before
    require(conn.total_changes == 0, "hourly-reconciliation-preview-mutated")
    return {
        "contract": CONTRACT, "db_path": str(db), "deployed_sha": request["deployed_sha"],
        "cycle_id": request["cycle_id"], "warehouse_run_id": request["warehouse_run_id"],
        "operation": operation, "transitions_sha256": digest(transitions),
        "run_sha256": digest(dict(run)), "phases_sha256": digest(phases),
        "cycle_sha256": "sha256:" + hashlib.sha256(cycle_bytes).hexdigest(),
        "artifacts": artifacts, "publication_tables": proofs,
        "checkpoint_metadata": metadata,
        "candidate_version_id": candidate, "base_active_version_id": base,
    }


class WarehouseHourlyRollbackReconciliationAdapter:
    def preview(self, request, operation_id):
        proof = capture(request)
        return {"operation_id": operation_id, "target": proof["db_path"],
                "scope": {"recovery_operation_id": request["recovery_operation_id"], "cycle_id": request["cycle_id"],
                          "warehouse_run_id": request["warehouse_run_id"], "action": "terminalize_proven_rollback"},
                "prestate_sha256": digest(proof),
                "candidate_sha256": digest({"recovery_operation_id": request["recovery_operation_id"], "proof_sha256": digest(proof), "lifecycle": "rolled_back"}),
                "recovery": {"method": "existing_verified_checkpoint_preserved_until_original_expiry", "artifacts": proof["artifacts"]}}

    def apply(self, request, operation_id, preview):
        root, db = context(request)
        # Match the native job -> writer lock ordering and exclude a live cycle.
        with warehouse_functional_job_lock(root), warehouse_functional_write_lock(root):
            proof = capture(request)
            require(digest(proof) == preview["prestate_sha256"], "hourly-reconciliation-reviewed-prestate-drift")
            candidate = {"recovery_operation_id": request["recovery_operation_id"], "proof_sha256": digest(proof), "lifecycle": "rolled_back"}
            require(digest(candidate) == preview["candidate_sha256"], "hourly-reconciliation-reviewed-candidate-drift")
            registry = WarehouseRecoveryRegistry(runtime_dir=root, db_path=db)
            def validate(conn):
                require(digest(capture(request, conn=conn)) == preview["prestate_sha256"], "hourly-reconciliation-CAS-proof-drift")
            try:
                result = registry.complete_hourly_transaction_rollback(
                    request["recovery_operation_id"], expected_state_version=proof["operation"]["state_version"],
                    plan_fingerprint=proof["operation"]["plan_fingerprint"], base_active_version_id=proof["base_active_version_id"],
                    evidence={"contract": CONTRACT, "sqlite_rollback_completed": True,
                              "launcher_operation_id": operation_id, "proof": proof}, validate=validate)
            except Exception as exc:
                raise AmbiguousSubmit("hourly-reconciliation-same-operation-readback-required") from exc
        return {"operation_id": operation_id, "disposition": "submitted", "recovery_lifecycle": result["lifecycle"]}

    def readback(self, request, operation_id):
        root, db = context(request, require_deployed_sha=False)
        with closing(readonly(db)) as conn:
            row = conn.execute("SELECT lifecycle_state FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?", (request["recovery_operation_id"],)).fetchone()
            transitions = [json.loads(row[0]) for row in conn.execute("SELECT detail_json FROM sheet_vitrina_v1_recovery_transitions WHERE operation_id=? AND to_state='rolled_back' ORDER BY transition_id", (request["recovery_operation_id"],))]
        exact = [proof for proof in transitions if proof.get("contract") == CONTRACT and proof.get("launcher_operation_id") == operation_id
                 and proof.get("operation_id") == request["recovery_operation_id"]
                 and proof.get("proof", {}).get("cycle_id") == request["cycle_id"]
                 and proof.get("proof", {}).get("warehouse_run_id") == request["warehouse_run_id"]
                 and proof.get("proof", {}).get("deployed_sha") == request["deployed_sha"]]
        if row is not None and row[0] in {"rolled_back", "released"} and len(exact) == 1:
            return {"operation_id": operation_id, "state": "applied", "recovery_operation_id": request["recovery_operation_id"], "proof_sha256": digest(exact[0]), "business_publication_completed": False}
        return {"operation_id": operation_id, "state": "not_submitted" if not transitions and row is not None and row[0] == "failed_recoverable" else "ambiguous"}
