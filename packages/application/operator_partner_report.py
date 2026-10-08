"""Exact acknowledgements for native Partner Report settings versions."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import uuid4

TABLE = "partner_report_settings_requests"
DOMAIN = "partner_report_settings"


def ensure_schema(conn):
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        operation_id TEXT PRIMARY KEY, seller_id TEXT NOT NULL, actor TEXT NOT NULL,
        input_digest TEXT NOT NULL, settings_version_id TEXT NOT NULL,
        nm_id TEXT NOT NULL, product_name TEXT NOT NULL, fingerprint TEXT NOT NULL,
        accepted_at TEXT NOT NULL,
        FOREIGN KEY(settings_version_id) REFERENCES partner_report_settings_versions(settings_version_id)
    )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS partner_settings_requests_time ON {TABLE}(accepted_at DESC,operation_id)")
    for action in ("UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS partner_settings_request_no_{action.lower()}
            BEFORE {action} ON {TABLE}
            BEGIN SELECT RAISE(ABORT,'partner settings receipt is immutable'); END""")


def intent(*, operation_id, seller_id, actor, parameters):
    identity = operation_id or "ops_partner_" + uuid4().hex
    if not isinstance(identity, str) or not re.fullmatch(r"ops_partner_[A-Za-z0-9_-]{1,120}", identity):
        raise ValueError("invalid_partner_settings_operation_identity")
    actor = str(actor or "").strip()
    if not actor:
        raise ValueError("partner_settings_actor_required")
    digest = "sha256:" + hashlib.sha256(json.dumps(parameters, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return dict(operation_id=identity, seller_id=seller_id, actor=actor, input_digest=digest)


def existing(conn, request):
    row = conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (request["operation_id"],)).fetchone()
    if row is None:
        return None
    if any(row[key] != value for key, value in request.items()):
        raise ValueError("partner_settings_operation_identity_conflict")
    version = conn.execute("SELECT * FROM partner_report_settings_versions WHERE settings_version_id=?",
        (row["settings_version_id"],)).fetchone()
    if version is None or version["fingerprint"] != row["fingerprint"]:
        raise RuntimeError("partner_settings_version_evidence_changed")
    return version, public(conn, row)


def record(conn, request, version, *, accepted_at):
    if not conn.in_transaction:
        raise RuntimeError("partner_settings_receipt_requires_native_transaction")
    conn.execute(f"""INSERT INTO {TABLE}(operation_id,seller_id,actor,input_digest,
        settings_version_id,nm_id,product_name,fingerprint,accepted_at) VALUES(?,?,?,?,?,?,?,?,?)""",
        (request["operation_id"], request["seller_id"], request["actor"], request["input_digest"],
         version["settings_version_id"], version["nm_id"], version["product_name"], version["fingerprint"], accepted_at))
    return public(conn, conn.execute(f"SELECT * FROM {TABLE} WHERE operation_id=?", (request["operation_id"],)).fetchone())


def public(conn, row):
    return {"contract_name": "operator_operations_v1", "operation_id": row["operation_id"],
        "domain": DOMAIN, "accepted_at": row["accepted_at"], "actor": row["actor"],
        "source_ref": {"domain": DOMAIN, "entity_id": row["nm_id"], "revision": row["settings_version_id"],
            "fingerprint": row["fingerprint"], "action": "settings_saved"},
        "durable_saved": True, "primary_effect": "source_saved", "state": "completed",
        "native_state": "settings_saved", "calculation_completed": False,
        "title_ru": "Настройки партнёрского отчёта", "reason_code": "source_saved",
        "reason_ru": "Настройки сохранены. Новый расчёт отчёта можно открыть отдельно.",
        "fields": [{"label": "Товар", "value": row["product_name"]}, {"label": "Артикул WB", "value": row["nm_id"]}],
        "journal_path": "/sheet-vitrina-v1/operations?operation_id=" + row["operation_id"],
        "detail_path": "/v1/sheet-vitrina-v1/operations/" + row["operation_id"]}
