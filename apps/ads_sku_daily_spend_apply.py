"""Receipt-bound spend verification through the existing one-submit production launcher."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import socket
import sqlite3

from apps.production_apply_contract import AmbiguousSubmit
from apps.web_vitrina_management_history import WebVitrinaManagementHistoryAdapter, readonly
from packages.application.ads_spend_verification import TABLE, accepted, digest, encoded, ensure_schema, has_evidence, preview
from packages.application.ads_historical_recovery import canonical_digest
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock, warehouse_functional_write_lock
from packages.application.wb_incident_policy import canonical_seller_id

OPERATIONS = "ads_sku_daily_spend_imports"


def evidence_file(runtime, value):
    path = Path(value).resolve(strict=True)
    if not path.is_relative_to(runtime / "evidence"):
        raise ValueError("WB export and capture receipt must be in runtime evidence")
    return path


class DailySpendAdapter:
    def target(self, request):
        if request["hostname"] != socket.gethostname():
            raise ValueError("unexpected target host")
        return WebVitrinaManagementHistoryAdapter().target(request)

    def candidate(self, conn, request, runtime, db, operation_id):
        raw = evidence_file(runtime, request["workbook_path"]).read_bytes()
        receipt = json.loads(evidence_file(runtime, request["receipt_path"]).read_text())
        seller = canonical_seller_id()
        nm_id = request.get("nm_id")
        if nm_id is not None and not re.fullmatch(r"[1-9][0-9]*", str(nm_id)):
            raise ValueError("invalid product scope")
        if request["seller_id"] != seller or request["day"] != receipt["date_from"]:
            raise ValueError("requested account/day differs from capture")
        result, inserts = preview(conn, raw, receipt, seller, datetime.now(timezone.utc),
                                  nm_id=None if nm_id is None else str(nm_id))
        summary = dict(operation_id=operation_id, target=str(db),
                       scope=dict(seller_id=seller, day=result["day"], nm_id=nm_id,
                                  receipt_hash=digest(encoded(receipt).encode()),
                                  decisions=result["decisions"]),
                       prestate_sha256="sha256:" + result["preview_hash"],
                       candidate_sha256=canonical_digest(inserts),
                       recovery=dict(kind="exact_inserted_evidence_rows", journal=OPERATIONS))
        return summary, inserts

    def preview(self, request, operation_id):
        runtime, db = self.target(request)
        with readonly(db) as conn:
            conn.execute("BEGIN")
            op = self.operation(conn, request, operation_id)
            if op:
                return op["preview"]
            return self.candidate(conn, request, runtime, db, operation_id)[0]

    @staticmethod
    def operation(conn, request, operation_id):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (OPERATIONS,)).fetchone():
            return None
        row = conn.execute(f"SELECT record FROM {OPERATIONS} WHERE id=?", (operation_id,)).fetchone()
        if not row:
            return None
        record = json.loads(row[0])
        if record["request_hash"] != canonical_digest(request):
            raise ValueError("operation ID already used for another request")
        return record

    def apply(self, request, operation_id, expected):
        runtime, db = self.target(request)
        with (warehouse_functional_job_lock(runtime, blocking=False),
              warehouse_functional_write_lock(runtime, blocking=False),
              closing(sqlite3.connect(db.as_uri() + "?mode=rw", uri=True)) as conn):
            conn.execute("BEGIN IMMEDIATE")
            if self.target(request) != (runtime, db) or self.operation(conn, request, operation_id):
                raise ValueError("target or operation changed")
            current, inserts = self.candidate(conn, request, runtime, db, operation_id)
            if current != expected:
                raise ValueError("spend evidence changed since preview")
            ensure_schema(conn)
            conn.execute(f"CREATE TABLE IF NOT EXISTS {OPERATIONS}(id TEXT PRIMARY KEY, record TEXT NOT NULL)")
            conn.executemany(f"INSERT INTO {TABLE} VALUES(?,?,?,?,?,?,?)", inserts)
            receipt_hash = current["scope"]["receipt_hash"]
            snapshot_hash, _ = accepted(conn, request["day"])
            expected_rows = [tuple(row) for row in conn.execute(
                f"SELECT * FROM {TABLE} WHERE seller_id=? AND day=? AND snapshot_hash=? AND receipt_hash=? "
                "ORDER BY nm_id", (request["seller_id"], request["day"], snapshot_hash, receipt_hash))
                if row[3] in current["scope"]["decisions"]]
            image = dict(request_hash=canonical_digest(request), preview=current,
                         expected_rows=expected_rows, inserts=inserts)
            conn.execute(f"INSERT INTO {OPERATIONS} VALUES(?,?)", (operation_id, encoded(image)))
            try:
                conn.commit()
            except sqlite3.Error as exc:
                raise AmbiguousSubmit("check the operation receipt") from exc
        return {"operation_id": operation_id, "disposition": "submitted" if inserts else "no_change"}

    def readback(self, request, operation_id):
        _, db = self.target(request)
        with readonly(db) as conn:
            conn.execute("BEGIN")
            record = self.operation(conn, request, operation_id)
            state = "not_submitted"
            if record:
                if record.get("rolled_back"):
                    return dict(operation_id=operation_id, state="failed", reason="rolled_back")
                matches = has_evidence(conn) and all(self.present(conn, r) for r in record["expected_rows"])
                state = ("applied" if record["inserts"] else "no_change") if matches else "ambiguous"
        return dict(operation_id=operation_id, state=state)

    @staticmethod
    def present(conn, row):
        return conn.execute(f"SELECT 1 FROM {TABLE} WHERE seller_id=? AND day=? AND snapshot_hash=? "
                            "AND nm_id=? AND receipt_hash=? AND receipt_json=? AND campaign_amounts=?",
                            row).fetchone() is not None

    def rollback(self, request, operation_id):
        runtime, db = self.target(request)
        with (warehouse_functional_job_lock(runtime, blocking=False),
              warehouse_functional_write_lock(runtime, blocking=False),
              closing(sqlite3.connect(db.as_uri() + "?mode=rw", uri=True)) as conn):
            conn.execute("BEGIN IMMEDIATE")
            if self.target(request) != (runtime, db):
                raise ValueError("storage target changed")
            op = self.operation(conn, request, operation_id)
            if not op or op.get("rolled_back") or not all(self.present(conn, r) for r in op["inserts"]):
                raise ValueError("verification after-image has changed")
            for row in op["inserts"]:
                conn.execute(f"DELETE FROM {TABLE} WHERE seller_id=? AND day=? AND snapshot_hash=? "
                             "AND nm_id=? AND receipt_hash=?", row[:5])
            op["rolled_back"] = True
            conn.execute(f"UPDATE {OPERATIONS} SET record=? WHERE id=?", (encoded(op), operation_id))
            conn.commit()
        return dict(operation_id=operation_id, disposition="rolled_back")
