"""Unconnected, owned staging of an exact historical receipt candidate.

This module never changes accounting_current, ready, functional stages, History,
Finance or an operator completion. The candidate still needs the dated derived
stage applicability/publication adapter. Staging is not a publication receipt.
Schema bootstrap is explicit; ordinary writers and closed guards are unchanged.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from packages.application import fbs_accounting_runtime as accounting
from packages.application import ready_publication as ready
from packages.application.fbs_accounting_historical_revision import (
    HEADER_FIELDS, validate_historical_revision_plan,
)
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, REQUESTS_TABLE
from packages.application.warehouse_functional_lock import (
    require_warehouse_job_owner, warehouse_functional_write_lock,
)

CONTRACT = "fbs_accounting_historical_revision_staging_v1"
TABLE = "sheet_vitrina_v1_fbs_accounting_historical_revision_staging"
MAX_MANIFEST_BYTES = 160 * 1024**2
MAX_REVISIONS = 10000
MAX_LINEAGE_BYTES = 32 * 1024**2
MAX_NATIVE_BYTES = 32 * 1024**2
UNRESOLVED_OBLIGATIONS = ("native_dated_stage_applicability", "native_rollover_wb_retained_authority",
                          "ready_inventory_history_finance_receipts")
# Capture semantics, rather than unrelated Finance/ready refresh timestamps.
SOURCE_TABLES = tuple(dict.fromkeys((*ready.MATERIAL_TABLES, REQUESTS_TABLE)))
REQUEST_PROOF_FIELDS = (
    "request_id", "request_identity", "client_request_id", "document_kind",
    "source_system", "source_type", "source_id", "source_revision",
    "idempotency_epoch", "actor", "business_date", "source_filename",
    "source_content_type", "source_sha256", "template_fingerprint",
    "request_payload_json", "accepted_at", "posted_document_id",
    "posted_manifest_sha256", "posted_at",
)


class HistoricalRevisionStageError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise HistoricalRevisionStageError(reason)


def _clock(now):
    now = now or datetime.now(timezone.utc)
    _require(isinstance(now, datetime) and now.utcoffset() is not None, "stage_aware_clock_required")
    return now


def _trigger_sql():
    immutable = ("operation_id", "attempt_id", "owner_generation", "manifest_digest",
                 "manifest_json", "expected_book", "after_book", "book_operation_id", "created_at")
    different = " OR ".join(f"old.{key} IS NOT new.{key}" for key in immutable)
    result = {
        "historical_stage_immutable": f"""CREATE TRIGGER IF NOT EXISTS historical_stage_immutable
            BEFORE UPDATE ON {TABLE} WHEN {different} OR old.state<>'prepared' OR new.state<>'staged'
            BEGIN SELECT RAISE(ABORT,'historical staging identity is immutable'); END""",
        "historical_stage_no_delete": f"""CREATE TRIGGER IF NOT EXISTS historical_stage_no_delete
            BEFORE DELETE ON {TABLE}
            BEGIN SELECT RAISE(ABORT,'historical staging evidence is immutable'); END""",
    }
    different = " OR ".join(f"old.{key} IS NOT new.{key}" for key in REQUEST_PROOF_FIELDS)
    for event in ("INSERT", "UPDATE", "DELETE"):
        when = " WHEN " + different if event == "UPDATE" else ""
        result[f"historical_stage_request_{event.lower()}"] = f"""CREATE TRIGGER IF NOT EXISTS historical_stage_request_{event.lower()}
            AFTER {event} ON {REQUESTS_TABLE}{when}
            BEGIN UPDATE {ready.REVISIONS} SET revision=revision+1
            WHERE source_table='{REQUESTS_TABLE}'; END"""
    return result


def ensure_staging_schema(conn):
    """Explicit additive bootstrap; not called by a read, preparation or apply."""
    _require(not conn.in_transaction, "stage_bootstrap_requires_no_owner_transaction")
    ready.ensure_material_revisions(conn)
    with conn:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
            operation_id TEXT NOT NULL,attempt_id TEXT NOT NULL,owner_generation TEXT NOT NULL,
            manifest_digest TEXT NOT NULL,manifest_json TEXT NOT NULL,
            expected_book TEXT NOT NULL,after_book TEXT NOT NULL,book_operation_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('prepared','staged')),
            staged_book_version TEXT,created_at TEXT NOT NULL,staged_at TEXT,
            PRIMARY KEY(operation_id,attempt_id),
            CHECK((state='prepared' AND staged_book_version IS NULL AND staged_at IS NULL)
               OR (state='staged' AND staged_book_version=after_book AND staged_at IS NOT NULL)))""")
        # Native request lifecycle/updated_at changes are observation metadata.
        # Source/actor/accepted authority changes (including ABA) are material.
        conn.execute(f"INSERT OR IGNORE INTO {ready.REVISIONS} VALUES(?,0)", (REQUESTS_TABLE,))
        for sql in _trigger_sql().values():
            conn.execute(sql)
        _schema_ready(conn)


def _schema_ready(conn):
    _require(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone(),
             "historical_stage_bootstrap_required")
    _require(ready.material_revisions_schema_ready(conn), "stage_material_revision_bootstrap_required")
    def normalize(sql):
        return "".join(sql.lower().split()).replace("ifnotexists", "")
    for name, expected in _trigger_sql().items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
        _require(row is not None and normalize(row[0]) == normalize(expected), "stage_trigger_authority_mismatch:" + name)


def _confirmation(conn, capture, receipt_ids):
    """Exact native accepted-and-posted request authority, including actor."""
    proofs = {}
    documents = {v["document_id"]: v for v in capture["documents"]}
    for identity in receipt_ids:
        doc = dict(conn.execute(f"SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?", (identity,)).fetchone())
        request_size = conn.execute(f"SELECT length(request_payload_json) FROM {REQUESTS_TABLE} WHERE request_id=?", (doc["request_id"],)).fetchone()
        _require(request_size and request_size[0] <= MAX_NATIVE_BYTES, "stage_request_source_size_limit")
        request_row = conn.execute(f"SELECT {','.join(REQUEST_PROOF_FIELDS)},state FROM {REQUESTS_TABLE} WHERE request_id=?", (doc["request_id"],)).fetchone()
        _require(request_row is not None, "stage_native_request_missing")
        request = dict(request_row)
        posted = json.loads(capture["posted_manifest_json_by_id"][identity])
        _require(request["state"] in {"posted", "replay", "complete"}, "stage_native_request_not_posted")
        _require(request["actor"].strip() and doc["actor"] == request["actor"], "stage_actor_authority_mismatch")
        _require(request["accepted_at"] and request["posted_at"]
                 and datetime.fromisoformat(request["accepted_at"].replace("Z", "+00:00"))
                 <= datetime.fromisoformat(doc["posted_at"].replace("Z", "+00:00"))
                 and request["posted_at"] == doc["posted_at"], "stage_native_confirmation_frontier_mismatch")
        fields = ("source_system", "source_type", "source_id", "source_revision", "idempotency_epoch", "business_date",
                  "source_filename", "source_content_type", "source_sha256", "template_fingerprint")
        _require(all(request[key] == doc[key] for key in fields), "stage_native_request_source_mismatch")
        _require(request["posted_document_id"] == posted["primary_document_id"], "stage_native_request_primary_mismatch")
        original = {key: value for key, value in posted.items() if key not in {"document_id", "document_role", "lines"}}
        original["document_kind"] = request["document_kind"]
        _require(request["posted_manifest_sha256"] == fingerprint(original), "stage_native_request_manifest_mismatch")
        _require(doc["posted_manifest_sha256"] == documents[identity]["posted_manifest_sha256"], "stage_native_document_changed")
        # A2 proves receipt ordered posted lines, not arbitrary NEW expense rows.
        _require(not documents[identity]["cost_document"]["expense_lines"], "stage_new_expense_authority_requires_cohort_adapter")
        proofs[identity] = {"document": {key: doc[key] for key in (*HEADER_FIELDS, "request_id", "actor")},
                            "request": {key: request[key] for key in REQUEST_PROOF_FIELDS}}
    return proofs


def _capture(conn, db, plan, now, book):
    count, size = conn.execute(f"SELECT count(*),coalesce(sum(length(posted_manifest_json)),0) FROM {DOCUMENTS_TABLE}").fetchone()
    _require(count <= 10000 and size <= MAX_NATIVE_BYTES, "stage_native_source_size_limit")
    for table, text_fields in (
        ("ff_pool_document_lines", ("metadata_json", "capital_rub", "expense_rub")),
        ("ff_pool_document_expense_lines", ("metadata_json", "amount_rub", "basis", "source_file_sha256")),
        ("ff_pool_movement_lines", ("metadata_json", "quantity_delta", "capital_delta_rub")),
        ("ff_pool_document_relations", ("parent_document_id", "child_document_id", "relation_type")),
        ("wb_fbs_stock_snapshot_rows", ("provenance",)),
    ):
        expression = "+".join(f"coalesce(length({field}),0)" for field in text_fields)
        count, added = conn.execute(f"SELECT count(*),coalesce(sum({expression}),0) FROM sheet_vitrina_v1_{table}").fetchone()
        size += added
        _require(count <= 100000 and size <= MAX_NATIVE_BYTES, "stage_native_source_size_limit")
    capture = capture_current(db, now=now, include_baseline=False, connection=conn)
    capture["posted_manifest_json_by_id"] = {row[0]: row[1] for row in conn.execute(
        f"SELECT document_id,posted_manifest_json FROM {DOCUMENTS_TABLE} ORDER BY document_id")}
    if max(plan["candidate_book"]["state"]["periods"]) > max(book["state"]["periods"]):
        # Staging records the pure planner's declared exact new-day inputs.
        # Only C's dated adapter can establish their native publication authority.
        day = plan["scope"]["date_to"]
        capture["current_dated_inputs"] = {"wb_capture": plan["candidate_book"]["wb_days"][day],
                                          "retained_capture": plan["candidate_book"]["retained_days"][day]}
    return capture


def _lineage(conn, *, excluding_operation=None):
    hashed = hashlib.sha256()
    count = size = 0
    for row in conn.execute("SELECT version,operation_id,payload,previous_version FROM accounting_revisions ORDER BY version"):
        if row[1] == excluding_operation:
            continue
        count += 1
        encoded = canonical(list(row)).encode() + b"\n"
        size += len(encoded)
        _require(count <= MAX_REVISIONS and size <= MAX_LINEAGE_BYTES, "stage_accounting_lineage_limit")
        hashed.update(encoded)
    return "sha256:" + hashed.hexdigest()


def _code_authority():
    root = Path(__file__).resolve().parents[1]
    names = ("application/fbs_accounting_historical_revision_writer.py",
             "application/fbs_accounting_historical_revision.py", "application/fbs_snapshot_cost.py",
             "application/fbs_snapshot_cost_sources.py", "application/fbs_document_cost.py",
             "application/fbs_inventory_presentation.py", "application/shared_sku_cost.py",
             "application/fbs_accounting_runtime.py", "application/official_fbs_stock_read.py",
             "application/ready_publication.py", "application/ff_pool_documents.py",
             "application/storage_registry.py", "application/fbs_current_snapshot_policy.py",
             "application/stock_catalog_scope.py", "application/wb_fbs_warehouse_registry.py", "business_time.py")
    return {name: "sha256:" + hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def _file_authority(runtime_dir, operational_path):
    result = {}
    for key, path in (("operational", Path(operational_path)), ("accounting", accounting.path(runtime_dir))):
        stat = path.stat()
        result[key] = [stat.st_dev, stat.st_ino]
    return result


def _book_frontier(runtime_dir, *, excluding_operation=None):
    with closing(sqlite3.connect(accounting.path(runtime_dir).as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        accounting.admit(conn)
        current = conn.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()
        _require(current is not None, "stage_active_book_missing")
        book, version = accounting.load(runtime_dir, connection=conn)
        _require(version == current[0], "stage_book_frontier_changed")
        return book, version, _lineage(conn, excluding_operation=excluding_operation)


def prepare_historical_revision_stage(plan, *, runtime_dir, owner_generation, operation_id, attempt_id, now=None):
    """Read-only preparation; later observations cannot replace business material."""
    now = _clock(now)
    for value in (owner_generation, operation_id, attempt_id):
        _require(type(value) is str and 0 < len(value) <= 240 and value.strip() == value, "stage_identity_invalid")
    book_operation_id = "historical-stage:" + fingerprint([operation_id, attempt_id])[7:]
    authority = ready.capture_authority(runtime_dir)
    db = Path(authority["path"])
    files = _file_authority(runtime_dir, db)
    book, expected, lineage = _book_frontier(runtime_dir, excluding_operation=book_operation_id)
    with ready.readonly(db) as conn:
        _schema_ready(conn)
        ready.check_pinned_authority(conn, authority)
        capture = _capture(conn, db, plan, now, book)
        validated = validate_historical_revision_plan(plan, book=book, capture=capture)
        confirmation = _confirmation(conn, capture, validated["receipt_document_ids"])
        material = ready.capture_material(conn, tables=SOURCE_TABLES)
    manifest = {"contract": CONTRACT, "operation_id": operation_id, "attempt_id": attempt_id,
        "owner_generation": owner_generation, "book_operation_id": book_operation_id,
        "authority": authority, "file_authority": files, "code_authority": _code_authority(),
        "expected_book": expected, "before_lineage_digest": lineage,
        "after_book": validated["after_book_digest"], "plan": validated,
        "source_confirmation": confirmation, "source_revisions": material,
        "created_at": validated["observation"]["captured_at"],
        "publication_status": "not_published", "activation_allowed": False,
        "unresolved_obligations": list(UNRESOLVED_OBLIGATIONS)}
    _require(len(canonical(manifest).encode()) <= MAX_MANIFEST_BYTES, "stage_manifest_size_limit")
    manifest["manifest_digest"] = fingerprint(manifest)
    _require(_file_authority(runtime_dir, db) == files, "stage_source_file_replaced")
    return manifest


def _verify_manifest(manifest):
    _require(manifest.get("contract") == CONTRACT and manifest.get("activation_allowed") is False
             and manifest.get("publication_status") == "not_published"
             and manifest.get("unresolved_obligations") == list(UNRESOLVED_OBLIGATIONS), "stage_manifest_contract_mismatch")
    _require(len(canonical(manifest).encode()) <= MAX_MANIFEST_BYTES, "stage_manifest_size_limit")
    _require(manifest.get("manifest_digest") == fingerprint({k: v for k, v in manifest.items() if k != "manifest_digest"}),
             "stage_manifest_digest_mismatch")
    for key in ("owner_generation", "operation_id", "attempt_id"):
        value = manifest.get(key)
        _require(type(value) is str and 0 < len(value) <= 240 and value.strip() == value, "stage_identity_invalid")
    _require(manifest["book_operation_id"] == "historical-stage:" + fingerprint([
        manifest["operation_id"], manifest["attempt_id"]])[7:], "stage_book_operation_identity_mismatch")
    _require(manifest["expected_book"] == manifest["plan"]["before_book_digest"]
             and manifest["after_book"] == manifest["plan"]["after_book_digest"]
             and manifest["created_at"] == manifest["plan"]["observation"]["captured_at"], "stage_plan_identity_mismatch")


def _row(conn, manifest):
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=? AND attempt_id=?",
                       (manifest["operation_id"], manifest["attempt_id"])).fetchone()
    if row:
        _require(row["manifest_digest"] == manifest["manifest_digest"]
                 and row["manifest_json"] == canonical(manifest)
                 and row["owner_generation"] == manifest["owner_generation"], "stage_intent_identity_conflict")
    return row


def _revalidate(conn, manifest, runtime_dir, now):
    _schema_ready(conn)
    _require(_code_authority() == manifest["code_authority"], "stage_code_authority_changed")
    _require(_file_authority(runtime_dir, manifest["authority"]["path"]) == manifest["file_authority"], "stage_source_file_replaced")
    ready.check_pinned_authority(conn, manifest["authority"])
    ready.check_material(conn, manifest["source_revisions"])
    book, expected, lineage = _book_frontier(runtime_dir, excluding_operation=manifest["book_operation_id"])
    _require(expected == manifest["expected_book"] and lineage == manifest["before_lineage_digest"], "stage_book_compare_and_swap_failed")
    capture = _capture(conn, Path(manifest["authority"]["path"]), manifest["plan"], now, book)
    plan = validate_historical_revision_plan(manifest["plan"], book=book, capture=capture)
    _require(_confirmation(conn, capture, plan["receipt_document_ids"]) == manifest["source_confirmation"],
             "stage_confirmation_changed")
    _require(plan["after_book_digest"] == manifest["after_book"], "stage_rebuilt_candidate_mismatch")
    _require(_file_authority(runtime_dir, manifest["authority"]["path"]) == manifest["file_authority"], "stage_source_file_replaced")
    return plan["candidate_book"]


def _append_candidate(runtime_dir, manifest, candidate):
    with closing(sqlite3.connect(accounting.path(runtime_dir).as_uri() + "?mode=rw", uri=True, timeout=0)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        accounting.admit(conn)
        current = conn.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()
        _require(current is not None and current[0] == manifest["expected_book"], "stage_book_compare_and_swap_failed")
        _require(_lineage(conn, excluding_operation=manifest["book_operation_id"]) == manifest["before_lineage_digest"],
                 "stage_book_lineage_changed")
        prior = conn.execute("SELECT version,payload,previous_version FROM accounting_revisions WHERE operation_id=?",
                             (manifest["book_operation_id"],)).fetchone()
        if prior:
            _require(prior[0] == manifest["after_book"] and prior[2] == manifest["expected_book"]
                     and accounting.unpack(conn, prior[1]) == candidate, "stage_existing_revision_identity_mismatch")
            return
        _require(not conn.execute("SELECT 1 FROM accounting_revisions WHERE version=?", (manifest["after_book"],)).fetchone(),
                 "stage_candidate_revision_owned_by_other_operation")
        payload = accounting.pack(conn, candidate)
        _require(accounting.unpack(conn, payload) == candidate, "stage_packed_candidate_corrupt")
        conn.execute("INSERT INTO accounting_revisions VALUES(?,?,?,?)", (
            manifest["after_book"], manifest["book_operation_id"], payload, manifest["expected_book"]))
        # No accounting_current UPDATE here, including retry/recovery.


def stage_historical_revision(manifest, *, runtime_dir, now=None, fault_injector=None):
    """Same-identity retry of staging under an existing live owned cycle."""
    _verify_manifest(manifest)
    _require(Path(runtime_dir).resolve() == Path(manifest["authority"]["runtime_dir"]), "stage_runtime_authority_mismatch")
    now = _clock(now)
    require_warehouse_job_owner(Path(runtime_dir))
    def inject(boundary):
        if fault_injector is not None:
            fault_injector(boundary)
    with warehouse_functional_write_lock(Path(runtime_dir), timeout_seconds=5), accounting.writer_lock(runtime_dir):
        ready.check_authority(runtime_dir, (Path(manifest["authority"]["path"]), manifest["authority"]["manifest"]))
        with closing(sqlite3.connect(Path(manifest["authority"]["path"]).as_uri() + "?mode=rw", uri=True, timeout=0)) as conn:
            conn.row_factory = sqlite3.Row
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                _revalidate(conn, manifest, runtime_dir, now)
                existing = _row(conn, manifest)
                if existing is None:
                    inject("before_intent")
                    conn.execute(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?,?, 'prepared',NULL,?,NULL)", (
                        manifest["operation_id"], manifest["attempt_id"], manifest["owner_generation"],
                        manifest["manifest_digest"], canonical(manifest), manifest["expected_book"],
                        manifest["after_book"], manifest["book_operation_id"], manifest["created_at"]))
            inject("after_intent")
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                candidate = _revalidate(conn, manifest, runtime_dir, now)
                existing = _row(conn, manifest)
                _require(existing is not None, "stage_intent_missing")
                _append_candidate(runtime_dir, manifest, candidate)
                inject("after_revision")
                if existing["state"] == "prepared":
                    changed = conn.execute(f"""UPDATE {TABLE} SET state='staged',staged_book_version=?,staged_at=?
                        WHERE operation_id=? AND attempt_id=? AND state='prepared' AND manifest_digest=?""", (
                        manifest["after_book"], now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                        manifest["operation_id"], manifest["attempt_id"], manifest["manifest_digest"])).rowcount
                    _require(changed == 1, "stage_intent_compare_and_swap_failed")
            inject("after_stage")
    return read_historical_revision_stage(runtime_dir=runtime_dir, operation_id=manifest["operation_id"], attempt_id=manifest["attempt_id"])


def read_historical_revision_stage(*, runtime_dir, operation_id, attempt_id):
    """Read exact staged outcome; this is never an active/publication receipt."""
    authority = ready.capture_authority(runtime_dir)
    with ready.readonly(Path(authority["path"])) as conn:
        ready.check_pinned_authority(conn, authority)
        _schema_ready(conn)
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=? AND attempt_id=?", (operation_id, attempt_id)).fetchone()
        if row is None:
            return None
        record = dict(row)
    manifest = json.loads(record["manifest_json"])
    _verify_manifest(manifest)
    _require(record["manifest_digest"] == manifest["manifest_digest"], "stage_stored_manifest_corrupt")
    _require(manifest["authority"] == authority, "stage_stored_authority_changed")
    _require(_file_authority(runtime_dir, authority["path"]) == manifest["file_authority"], "stage_source_file_replaced")
    candidate = None
    with closing(sqlite3.connect(accounting.path(runtime_dir).as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        accounting.admit(conn)
        row = conn.execute("SELECT version,payload,previous_version FROM accounting_revisions WHERE operation_id=?",
                           (manifest["book_operation_id"],)).fetchone()
        current = conn.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()
        if row:
            candidate = accounting.unpack(conn, row[1])
            _require(row[0] == manifest["after_book"] == fingerprint(candidate)
                     and row[2] == manifest["expected_book"] and candidate == manifest["plan"]["candidate_book"],
                     "stage_readback_revision_mismatch")
    _require(record["state"] != "staged" or candidate is not None, "stage_receipt_revision_missing")
    return {"contract": CONTRACT, "operation_id": operation_id, "attempt_id": attempt_id,
        "owner_generation": manifest["owner_generation"], "manifest_digest": manifest["manifest_digest"],
        "state": record["state"], "revision_present": candidate is not None,
        "staged_book_version": manifest["after_book"] if candidate is not None else None,
        "expected_book": manifest["expected_book"], "current_book_version": current[0] if current else None,
        "current_matches_expected": bool(current and current[0] == manifest["expected_book"]),
        "publication_status": "not_published", "activation_allowed": False,
        "operator_completion": False, "unresolved_obligations": manifest["unresolved_obligations"]}
