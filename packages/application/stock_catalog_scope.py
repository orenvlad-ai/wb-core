"""Query-only SKU scope shared by official WB/FBS stock readers.

Visibility and the reporting bundle do not retire inventory. Hidden products
remain requested until an independent retirement policy can prove no remaining
stock or operations. This module never activates SKUs or writes accounting data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


NOMENCLATURE_TABLE = "sheet_vitrina_v1_nomenclature_items"
SCOPE_POLICY = "nomenclature_main_and_retained_hidden_v1"


class StockCatalogScopeError(ValueError):
    pass


def read_stock_catalog_scope(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read the current catalog afresh; absent/ambiguous identities stay explicit."""
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (NOMENCLATURE_TABLE,),
    ).fetchone() is None:
        raise StockCatalogScopeError("stock_nomenclature_unavailable")
    cursor = conn.execute(
        f"SELECT * FROM {NOMENCLATURE_TABLE} "
        "WHERE (is_active=1 AND is_hidden=0) OR is_hidden=1 ORDER BY item_id"
    )
    columns = [column[0] for column in cursor.description]
    items = [dict(zip(columns, row)) for row in cursor]
    identities = []
    invalid = []
    by_nm: dict[int, list[str]] = {}
    for item in items:
        nm = item.get("nm_id")
        if not isinstance(nm, int) or isinstance(nm, bool) or nm <= 0:
            invalid.append(str(item["item_id"]))
        else:
            by_nm.setdefault(nm, []).append(str(item["item_id"]))
        identities.append({
            "item_id": str(item["item_id"]), "nm_id": nm,
            "is_active": bool(item["is_active"]), "is_hidden": bool(item["is_hidden"]),
            "updated_at": str(item.get("updated_at") or ""),
            "reason": "retained_hidden" if item["is_hidden"] else "main_nomenclature",
        })
    duplicates = sorted(nm for nm, owners in by_nm.items() if len(owners) != 1)
    material = {"policy": SCOPE_POLICY, "items": identities}
    digest = "sha256:" + hashlib.sha256(json.dumps(
        material, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()
    return {
        "policy": SCOPE_POLICY, "complete": bool(items) and not invalid and not duplicates,
        "nm_ids": sorted(by_nm), "items": items, "identities": identities,
        "scope_digest": digest,
        "main_count": sum(not row["is_hidden"] for row in identities),
        "retained_hidden_count": sum(row["is_hidden"] for row in identities),
        "invalid_identity_item_ids": invalid, "duplicate_nm_ids": duplicates,
    }


def load_stock_catalog_scope(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        conn.execute("PRAGMA query_only=ON")
        return read_stock_catalog_scope(conn)
    finally:
        conn.close()


def require_stock_catalog_scope(db_path: Path) -> dict[str, Any]:
    scope = load_stock_catalog_scope(db_path)
    if not scope["complete"]:
        raise StockCatalogScopeError(
            "stock_nomenclature_scope_incomplete: "
            f"invalid_items={scope['invalid_identity_item_ids']}; "
            f"duplicate_nm_ids={scope['duplicate_nm_ids']}"
        )
    return scope
