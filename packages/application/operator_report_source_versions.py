"""Immutable acknowledgements for the two native report-file source stores.

These are source versions, not warehouse jobs or evidence that a report was
calculated. The native source mutation and its version share one transaction.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

TABLE = "sheet_vitrina_v1_operator_report_source_versions"
DOMAINS = frozenset({"plan_report_baseline", "factory_order_dataset"})
IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
PUBLIC_COLUMNS = "operation_id,domain,source_id,action,actor,accepted_at,input_digest,after_json,source_revision"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def ensure_schema(conn):
    """Called by explicit native schema bootstrap, never by a journal read."""
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY, domain TEXT NOT NULL, source_id TEXT NOT NULL,
        action TEXT NOT NULL, actor TEXT NOT NULL, accepted_at TEXT NOT NULL,
        input_digest TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        before_file BLOB, after_file BLOB, source_revision TEXT NOT NULL,
        CHECK(domain IN ('plan_report_baseline','factory_order_dataset')),
        CHECK(action IN ('upload','delete'))
    )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS operator_report_source_accepted ON {TABLE}(domain,accepted_at DESC,operation_id)")
    for event in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS operator_report_source_no_{event.lower()}
            BEFORE {event} ON {TABLE}
            BEGIN SELECT RAISE(ABORT,'report source version is immutable'); END""")


def request(*, domain, source_id, action, actor, operands, operation_id=None):
    if domain not in DOMAINS or action not in {"upload", "delete"}:
        raise ValueError("invalid_report_source_action")
    actor = str(actor or "").strip()
    if not actor or len(actor) > 240 or not source_id:
        raise ValueError("report_source_actor_and_identity_required")
    operation_id = operation_id or "ors_" + uuid4().hex
    if not isinstance(operation_id, str) or not operation_id.startswith("ors_") or IDENTITY.fullmatch(operation_id) is None:
        raise ValueError("invalid_report_operation_identity")
    return {"operation_id": operation_id, "domain": domain, "source_id": source_id,
            "action": action, "actor": actor, "input_digest": digest(operands)}


def existing(conn, intent):
    row = conn.execute(f"SELECT {PUBLIC_COLUMNS} FROM {TABLE} WHERE operation_id=?", (intent["operation_id"],)).fetchone()
    if row is None:
        return None
    row = dict(row)
    if any(row[key] != value for key, value in intent.items()):
        raise ValueError("report_operation_identity_conflict")
    return public(row)


def record(conn, intent, *, accepted_at, before, after, before_file=None, after_file=None):
    if not conn.in_transaction:
        raise RuntimeError("report_source_version_requires_native_transaction")
    before_json, after_json = canonical(before), canonical(after)
    revision = digest({"domain": intent["domain"], "source_id": intent["source_id"],
                       "operation_id": intent["operation_id"], "after": after,
                       "file_sha256": hashlib.sha256(after_file).hexdigest() if after_file is not None else None})
    conn.execute(f"""INSERT INTO {TABLE}(operation_id,domain,source_id,action,actor,accepted_at,
        input_digest,before_json,after_json,before_file,after_file,source_revision)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
        intent["operation_id"], intent["domain"], intent["source_id"], intent["action"],
        intent["actor"], accepted_at, intent["input_digest"], before_json, after_json,
        before_file, after_file, revision))
    return public(dict(conn.execute(f"SELECT {PUBLIC_COLUMNS} FROM {TABLE} WHERE operation_id=?", (intent["operation_id"],)).fetchone()))


def public(row):
    domain = row["domain"]
    after = json.loads(row["after_json"])
    if domain == "plan_report_baseline":
        title = "Исходные данные отчёта «Выполнение плана»"
        fields = [{"label": "Месяцы", "value": ", ".join(item["month"] for item in after)}]
        if after:
            fields.append({"label": "Файл", "value": after[0].get("uploaded_filename") or "—"})
    else:
        labels = {"stock_ff": "Остатки ФФ", "inbound_factory_to_ff": "Товары в пути от фабрики",
                  "inbound_ff_to_wb": "Товары в пути от ФФ на WB"}
        title = labels.get(row["source_id"], "Исходные данные планирования")
        fields = [{"label": "Действие", "value": "Файл удалён" if row["action"] == "delete" else "Файл загружен"}]
        if after is not None:
            fields.append({"label": "Строк", "value": after["row_count"]})
            fields.append({"label": "Файл", "value": after["uploaded_filename"]})
    return {"contract_name": "operator_operations_v1", "operation_id": row["operation_id"],
            "domain": domain, "source_ref": {"domain": domain, "entity_id": row["source_id"],
                "revision": row["source_revision"], "action": row["action"]},
            "accepted_at": row["accepted_at"], "actor": row["actor"], "durable_saved": True,
            "primary_effect": "source_saved", "state": "completed", "native_state": "source_saved",
            "title_ru": title, "fields": fields, "reason_code": "source_saved",
            "reason_ru": "Изменение источника сохранено. Расчёт отчёта выполняется отдельно.",
            "calculation_completed": False,
            "detail_path": "/v1/sheet-vitrina-v1/operations/" + row["operation_id"],
            "journal_path": "/sheet-vitrina-v1/operations?operation_id=" + row["operation_id"]}


def read(db_path, operation_id, *, allowed_domains):
    """Permission-filtered exact read, including legacy databases without schema."""
    allowed = DOMAINS.intersection(allowed_domains)
    if not allowed:
        return None
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return None
        row = conn.execute(f"SELECT {PUBLIC_COLUMNS} FROM {TABLE} WHERE operation_id=? AND domain IN ({','.join('?' for _ in allowed)})",
                           (operation_id, *sorted(allowed))).fetchone()
        return public(dict(row)) if row is not None else None
