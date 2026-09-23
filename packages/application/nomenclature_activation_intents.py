"""Source-owned staging for the existing dense FBS activation consumer.

Only new explicit catalog writes create demand. The per-item revision also
records later inactive decisions, including an ABA with the same timestamp.
Planning remains in DenseFbsService because it needs the current roster/epoch.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

TABLE = "sheet_vitrina_v1_nomenclature_activation_intents"
CATALOG = "sheet_vitrina_v1_nomenclature_items"


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    # No executescript: source and obligation must share the caller's commit.
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
        item_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
        desired_fingerprint TEXT NOT NULL, staged_fingerprint TEXT NOT NULL,
        nm_id INTEGER NOT NULL, source_updated_at TEXT NOT NULL,
        batch_key TEXT NOT NULL, status TEXT NOT NULL
            CHECK(status IN ('pending','active','inactive','cancelled','blocked')),
        error TEXT NOT NULL DEFAULT '', requested_at TEXT NOT NULL,
        dense_intent_id TEXT NOT NULL DEFAULT '', last_attempt_at TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS nomenclature_activation_pending ON {TABLE}(status,batch_key)")


def record_source_write(
    conn: sqlite3.Connection,
    *,
    desired_items: Sequence[Mapping[str, Any]],
    staged_item_ids: set[str],
) -> None:
    """Called after the catalog upsert, inside that same source transaction."""
    ensure_schema(conn)
    new_pending: list[dict[str, Any]] = []
    for desired in desired_items:
        item_id = str(desired["item_id"])
        existing = conn.execute(f"SELECT * FROM {TABLE} WHERE item_id=?", (item_id,)).fetchone()
        row = conn.execute(f"SELECT * FROM {CATALOG} WHERE item_id=?", (item_id,)).fetchone()
        staged_fingerprint = _fingerprint(dict(row))
        desired_fingerprint = _fingerprint({**dict(row), "is_active": int(desired["is_active"])})
        if (existing is not None
                and existing["desired_fingerprint"] == desired_fingerprint
                and existing["status"] in {"pending", "active", "inactive"}):
            # A retry keeps the original cohort and exact materialization identity.
            continue
        revision = int(existing["revision"] if existing is not None else 0) + 1
        status = "pending" if item_id in staged_item_ids else ("active" if desired["is_active"] else "inactive")
        conn.execute(f"""INSERT INTO {TABLE}(
            item_id,revision,desired_fingerprint,staged_fingerprint,nm_id,
            source_updated_at,batch_key,status,requested_at
        ) VALUES(?,?,?,?,?,?,'',?,?) ON CONFLICT(item_id) DO UPDATE SET
            revision=excluded.revision,desired_fingerprint=excluded.desired_fingerprint,
            staged_fingerprint=excluded.staged_fingerprint,nm_id=excluded.nm_id,
            source_updated_at=excluded.source_updated_at,batch_key='',status=excluded.status,
            requested_at=excluded.requested_at,error='',dense_intent_id='',last_attempt_at=''""", (
                item_id, revision, desired_fingerprint, staged_fingerprint,
                int(desired["nm_id"] or 0), str(desired["updated_at"]), status,
                str(desired["updated_at"]),
            ))
        if status == "pending":
            new_pending.append({"item_id": item_id, "revision": revision, "fingerprint": desired_fingerprint})
    if new_pending:
        batch_key = _fingerprint(sorted(new_pending, key=lambda item: item["item_id"]))
        for item in new_pending:
            conn.execute(f"UPDATE {TABLE} SET batch_key=? WHERE item_id=? AND revision=?",
                         (batch_key, item["item_id"], item["revision"]))


def cancel_source_activation(conn: sqlite3.Connection, item_id: str) -> None:
    ensure_schema(conn)
    # Called after the catalog update in the same transaction. A legacy Dense
    # plan has no source row yet, so deletion must INSERT its tombstone too.
    row = conn.execute(f"SELECT * FROM {CATALOG} WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("cancelled nomenclature item is missing")
    fingerprint = _fingerprint(dict(row))
    conn.execute(f"""INSERT INTO {TABLE}(
        item_id,revision,desired_fingerprint,staged_fingerprint,nm_id,
        source_updated_at,batch_key,status,error,requested_at
    ) VALUES(?,1,?,?,?,?,?,'cancelled','catalog_item_deleted',?)
    ON CONFLICT(item_id) DO UPDATE SET revision={TABLE}.revision+1,
        desired_fingerprint=excluded.desired_fingerprint,staged_fingerprint=excluded.staged_fingerprint,
        nm_id=excluded.nm_id,source_updated_at=excluded.source_updated_at,batch_key='',status='cancelled',
        error='catalog_item_deleted',requested_at=excluded.requested_at,dense_intent_id='',last_attempt_at=''""",
        (item_id, fingerprint, fingerprint, int(row["nm_id"] or 0), str(row["updated_at"]), "", str(row["updated_at"])))


def source_statuses(conn: sqlite3.Connection, item_ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return {}
    where = f" WHERE item_id IN ({','.join('?' for _ in item_ids)})" if item_ids is not None else ""
    return {row["item_id"]: {"activation_status": row["status"],
                             "activation_source_revision": row["revision"],
                             "activation_error": row["error"]}
            for row in conn.execute(f"SELECT item_id,revision,status,error FROM {TABLE}{where}", tuple(item_ids or []))}


def require_current_source(
    conn: sqlite3.Connection, staged_items: Sequence[Mapping[str, Any]], *, active: bool = False,
) -> None:
    from packages.application.ff_pool_dense_fbs import DenseFbsError

    has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    for item in staged_items:
        source = conn.execute(f"SELECT * FROM {TABLE} WHERE item_id=?", (item["item_id"],)).fetchone() if has_table else None
        token = item.get("source_revision")
        if source is None and token is None:
            # Legacy explicit staging remains resumable only while the exact
            # catalog state still matches. This also guards direct ready posts.
            row = conn.execute(f"SELECT is_active,is_hidden,nm_id,updated_at FROM {CATALOG} WHERE item_id=?",
                               (item["item_id"],)).fetchone()
            if (row is not None and bool(row[0]) == active and not bool(row[1])
                    and int(row[2] or 0) == int(item["nm_id"]) and str(row[3]) == str(item["updated_at"])):
                continue
            raise DenseFbsError("sku_activation_source_superseded", "Legacy staging no longer matches the catalog",
                                details={"item_id": item["item_id"]})
        row = conn.execute(f"SELECT * FROM {CATALOG} WHERE item_id=?", (item["item_id"],)).fetchone()
        values = dict(row) if row is not None else {}
        if active and values:
            values["is_active"] = 0
        if (source is None or token != source["revision"]
                or source["status"] != ("active" if active else "pending")
                or _fingerprint(values) != source["staged_fingerprint"]):
            raise DenseFbsError("sku_activation_source_superseded",
                                "Nomenclature activation no longer owns this source revision",
                                details={"item_id": item["item_id"], "source_revision": token})


def acknowledge_source(
    conn: sqlite3.Connection, staged_items: Sequence[Mapping[str, Any]], *, intent_id: str,
) -> None:
    """Exact per-item acknowledgment shares the active publication transaction."""
    from packages.application.ff_pool_dense_fbs import DenseFbsError

    for item in staged_items:
        if "source_revision" not in item:
            continue
        changed = conn.execute(f"""UPDATE {TABLE} SET status='active',dense_intent_id=?,error=''
            WHERE item_id=? AND revision=? AND status='pending'""",
            (intent_id, item["item_id"], item["source_revision"])).rowcount
        if changed != 1:
            raise DenseFbsError("sku_activation_source_ack_drift", "Activation acknowledgment lost its source revision")


def drain_nomenclature_activation_intents(
    runtime: Any, *, item_ids: Sequence[str] | None = None,
    service: Any = None, raise_errors: bool = False, batch_limit: int = 100,
) -> dict[str, Any]:
    """Existing save/warehouse continuation; no inactive catalog/history scan."""
    from packages.application.ff_pool_dense_fbs import DenseFbsService
    from packages.application.registry_upload_db_backed_runtime import _connect
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock

    results: list[dict[str, Any]] = []
    first_error: Exception | None = None
    with warehouse_functional_write_lock(runtime.runtime_dir):
        with _connect(runtime.db_path) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
                return {"status": "no_op", "batches": []}
            where = "status='pending'"
            params: list[Any] = []
            if item_ids is not None:
                if not item_ids:
                    return {"status": "no_op", "batches": []}
                where += f" AND item_id IN ({','.join('?' for _ in item_ids)})"
                params.extend(item_ids)
            batches = [row[0] for row in conn.execute(
                f"SELECT batch_key FROM {TABLE} WHERE {where} GROUP BY batch_key ORDER BY MIN(last_attempt_at),batch_key LIMIT ?",
                (*params, max(1, int(batch_limit))))]
        consumer = service or DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir)
        for batch_key in batches:
            with _connect(runtime.db_path) as conn:
                pending = conn.execute(f"SELECT * FROM {TABLE} WHERE batch_key=? AND status='pending' ORDER BY nm_id,item_id", (batch_key,)).fetchall()
                items = [{"item_id": row["item_id"], "nm_id": row["nm_id"],
                          "updated_at": row["source_updated_at"], "source_revision": row["revision"]} for row in pending]
                # Bounded drains rotate attempted cohorts so an early blocked
                # plan cannot starve later, independently recoverable sources.
                conn.execute(f"UPDATE {TABLE} SET last_attempt_at=? WHERE batch_key=? AND status='pending'",
                             (datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), batch_key))
                conn.commit()
            if not items:
                continue
            identity = _fingerprint(items)
            try:
                result = consumer.activate_staged_skus(
                    staged_items=items, orchestration_key="sku-source-activation:" + identity,
                    request_identity=identity, actor="registry_nomenclature_write")
                results.append({"batch_key": batch_key, **result})
            except Exception as exc:
                # Retain exact pending scope; DenseFbsService decides whether its
                # canonical request is resumable. Terminal plans are not reposted.
                error = str(getattr(exc, "code", type(exc).__name__))
                with _connect(runtime.db_path) as conn:
                    current = [conn.execute(f"SELECT revision,status FROM {TABLE} WHERE item_id=?",
                                            (item["item_id"],)).fetchone() for item in items]
                    if all(row is not None and row["revision"] == item["source_revision"] and row["status"] == "active"
                           for item, row in zip(items, current)):
                        results.append({"batch_key": batch_key, "state": "active", "transport_reconciled": True})
                        continue
                    for item in items:
                        conn.execute(f"UPDATE {TABLE} SET error=? WHERE item_id=? AND revision=? AND status='pending'",
                                     (error, item["item_id"], item["source_revision"]))
                    conn.commit()
                results.append({"batch_key": batch_key, "state": "pending", "error": error})
                first_error = first_error or exc
    if raise_errors and first_error is not None:
        # A failed continuation is not an unsaved catalog operation. Keep the
        # existing domain code while exposing its committed source boundary.
        first_error.args = ("Nomenclature saved; activation pending: " + str(first_error),)
        if hasattr(first_error, "details"):
            first_error.details = {"catalog_saved": True, "activation_status": "pending",
                                   "cause": first_error.details}
        raise first_error
    return {"status": "pending" if first_error else ("ok" if results else "no_op"), "batches": results}
