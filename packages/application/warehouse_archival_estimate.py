"""Versioned owner-approved archival WB cost basis and guarded source correction.

The immutable functional opening map stays untouched.  This module adds one
bounded, data-backed overlay for the exact manifest approved by the owner and
owns the only mutation path for publishing it.  Physical quantities, source
documents and warehouse movements are never manufactured by this correction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable, Mapping

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.sqlite_contention import connect_sqlite
from packages.application.warehouse_functional_lock import warehouse_functional_write_lock


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST_PATH = (
    ROOT
    / "migration"
    / "data"
    / "warehouse_business_approved_archival_estimate_20260701.json"
)
FUNCTIONAL_CUTOVER_ID = "warehouse_functional_cutover_v1"
CONTRACT_NAME = "warehouse_business_approved_archival_estimate"
CONTRACT_VERSION = "v2"
QUALITY = "business_approved_archival_estimate"
LEGACY_ESTIMATE_DAILY_QUALITIES = frozenset(
    {
        "fallback_average",
        "periodic_snapshot_wac_closed",
        "periodic_snapshot_wac_provisional",
    }
)
MIN_BACKUP_HEADROOM_BYTES = 512 * 1024 * 1024
ZERO = Decimal("0")


class WarehouseArchivalEstimateError(RuntimeError):
    """Fail-closed error for the archival estimate publication."""


def ensure_archival_estimate_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_archival_estimate_versions(
            version_id TEXT PRIMARY KEY,effective_date TEXT NOT NULL,
            unit_cost_rub TEXT NOT NULL,quality TEXT NOT NULL,
            owner_approval_reference TEXT NOT NULL,manifest_digest TEXT NOT NULL,
            production_dry_run_plan_sha256 TEXT NOT NULL,
            source_digest TEXT NOT NULL,plan_fingerprint TEXT NOT NULL UNIQUE,
            supersedes_version_id TEXT,backup_json TEXT NOT NULL,
            before_daily_rows_json TEXT NOT NULL,after_daily_rows_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_archival_estimate_rows(
            version_id TEXT NOT NULL,nm_id INTEGER NOT NULL,vendor_code TEXT NOT NULL,
            item_name TEXT NOT NULL,unit_cost_rub TEXT NOT NULL,quality TEXT NOT NULL,
            previous_unit_cost_rub TEXT NOT NULL,previous_quality TEXT NOT NULL,
            lineage_json TEXT NOT NULL,row_fingerprint TEXT NOT NULL,
            PRIMARY KEY(version_id,nm_id)
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_archival_estimate_active(
            slot INTEGER PRIMARY KEY CHECK(slot=1),version_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sheet_vitrina_v1_warehouse_archival_estimate_rollbacks(
            rollback_id TEXT PRIMARY KEY,version_id TEXT NOT NULL UNIQUE,
            plan_fingerprint TEXT NOT NULL,reason TEXT NOT NULL,
            backup_json TEXT NOT NULL,created_at TEXT NOT NULL
        );
        """
    )


def load_archival_estimate_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise WarehouseArchivalEstimateError("archival estimate manifest must be an object")
    targets = list(payload.get("targets") or [])
    active = [int(value) for value in payload.get("active_vitrina_nm_ids") or []]
    target_ids = [int(item.get("nm_id") or 0) for item in targets]
    if (
        payload.get("schema_version")
        != "warehouse_business_approved_archival_estimate_manifest_v2"
        or payload.get("effective_date") != "2026-07-01"
        or _decimal(payload.get("unit_cost_rub")) != Decimal("100.00")
        or payload.get("quality") != QUALITY
        or len(target_ids) != 18
        or len(set(target_ids)) != 18
        or min(target_ids, default=0) <= 0
        or len(active) != 33
        or len(set(active)) != 33
        or set(target_ids) & set(active)
    ):
        raise WarehouseArchivalEstimateError("archival estimate manifest contract mismatch")
    if any(
        not str(item.get("vendor_code") or "").strip()
        or not str(item.get("canonical_nomenclature_name") or "").strip()
        or not str(item.get("name") or "").strip()
        for item in targets
    ):
        raise WarehouseArchivalEstimateError("archival target identity is incomplete")
    return payload


def archival_estimate_manifest_digest(
    path: Path = DEFAULT_MANIFEST_PATH,
) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def active_archival_estimates(
    conn: sqlite3.Connection,
    *,
    as_of_date: str | None = None,
    nm_ids: Iterable[int] | None = None,
) -> dict[int, dict[str, Any]]:
    """Return the persisted active overlay without creating schema or data."""

    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    required = {
        "sheet_vitrina_v1_warehouse_archival_estimate_versions",
        "sheet_vitrina_v1_warehouse_archival_estimate_rows",
        "sheet_vitrina_v1_warehouse_archival_estimate_active",
    }
    if not required.issubset(tables):
        return {}
    date_filter = str(as_of_date or "").strip()
    selected_nm_ids = sorted({int(value) for value in nm_ids or [] if int(value) > 0})
    nm_filter = (
        " AND row.nm_id IN (" + ",".join("?" for _ in selected_nm_ids) + ")"
        if selected_nm_ids
        else ""
    )
    rows = conn.execute(
        f"""SELECT version.version_id,version.effective_date,version.unit_cost_rub,
                  version.quality,version.owner_approval_reference,
                  version.manifest_digest,version.production_dry_run_plan_sha256,
                  version.source_digest,version.plan_fingerprint,
                  row.nm_id,row.vendor_code,row.item_name,row.previous_unit_cost_rub,
                  row.previous_quality,row.lineage_json,row.row_fingerprint
           FROM sheet_vitrina_v1_warehouse_archival_estimate_active active
           JOIN sheet_vitrina_v1_warehouse_archival_estimate_versions version
             ON version.version_id=active.version_id
           JOIN sheet_vitrina_v1_warehouse_archival_estimate_rows row
             ON row.version_id=version.version_id
           WHERE active.slot=1{nm_filter} ORDER BY row.nm_id""",
        tuple(selected_nm_ids),
    ).fetchall()
    if not rows:
        return {}
    first_factual_date: dict[int, str] = {}
    if date_filter and "sheet_vitrina_v1_warehouse_functional_events" in tables:
        active_nm_ids = sorted({int(row["nm_id"]) for row in rows})
        placeholders = ",".join("?" for _ in active_nm_ids)
        first_factual_date = {
            int(row["nm_id"]): str(row["first_business_date"])
            for row in conn.execute(
                f"""SELECT nm_id,MIN(business_date) AS first_business_date
                   FROM sheet_vitrina_v1_warehouse_functional_events
                   WHERE event_type='wb_final_acceptance'
                     AND nm_id IN ({placeholders})
                     AND business_date IS NOT NULL AND business_date!=''
                     AND (CAST(quantity AS REAL)!=0 OR CAST(capital_rub AS REAL)!=0)
                   GROUP BY nm_id""",
                tuple(active_nm_ids),
            ).fetchall()
        }
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        if date_filter and date_filter < str(row["effective_date"]):
            continue
        if date_filter and date_filter >= first_factual_date.get(int(row["nm_id"]), "9999-12-31"):
            continue
        item = dict(row)
        item["lineage"] = _loads(item.pop("lineage_json"), {})
        result[int(item["nm_id"])] = item
    return result


def archival_estimate_for_nm_id(
    conn: sqlite3.Connection,
    *,
    nm_id: int | str,
    as_of_date: str,
) -> dict[str, Any] | None:
    normalized_nm_id = int(nm_id)
    return active_archival_estimates(
        conn,
        as_of_date=as_of_date,
        nm_ids=[normalized_nm_id],
    ).get(normalized_nm_id)


def overlay_opening_cost_rows(
    conn: sqlite3.Connection,
    rows: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay only WB opening WAC; retain immutable FF/source evidence."""

    estimates = active_archival_estimates(conn)
    result: list[dict[str, Any]] = []
    for raw in rows:
        item = dict(raw)
        nm_id = int(item.get("nm_id") or 0)
        estimate = estimates.get(nm_id)
        if estimate is None:
            result.append(item)
            continue
        original_provenance = dict(item.get("provenance") or {})
        provenance = {
            "source": QUALITY,
            "owner_approval_reference": estimate["owner_approval_reference"],
            "effective_date": estimate["effective_date"],
            "unit_cost_rub": estimate["unit_cost_rub"],
            "manifest_digest": estimate["manifest_digest"],
            "production_dry_run_plan_sha256": estimate[
                "production_dry_run_plan_sha256"
            ],
            "source_digest": estimate["source_digest"],
            "calculation_fingerprint": estimate["plan_fingerprint"],
            "row_fingerprint": estimate["row_fingerprint"],
            "supersedes": {
                "quality": item.get("quality"),
                "wb_unit_cost_rub": item.get("wb_unit_cost_rub"),
                "opening_fingerprint": item.get("fingerprint"),
                "provenance": original_provenance,
            },
            "no_quantity_or_capital_created": True,
        }
        item.update(
            {
                "wb_unit_cost_rub": str(estimate["unit_cost_rub"]),
                "quality": QUALITY,
                "provenance": provenance,
            }
        )
        item["fingerprint"] = "sha256:" + _hash(
            {
                "nm_id": nm_id,
                "ff_unit_cost_rub": item.get("ff_unit_cost_rub"),
                "wb_unit_cost_rub": item["wb_unit_cost_rub"],
                "quality": QUALITY,
                "provenance": provenance,
            }
        )
        result.append(item)
    return result


def build_archival_estimate_plan(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Build one deterministic read-only exact-target correction plan."""

    manifest = load_archival_estimate_manifest(manifest_path)
    owns_connection = _connection is None
    conn = _connection or _connect(runtime.db_path, readonly=True)
    try:
        if owns_connection:
            # Python's sqlite3 driver does not start a transaction for SELECT.
            # Pin every row and digest in this plan to one coherent WAL snapshot.
            conn.execute("BEGIN")
        target_ids = sorted(int(item["nm_id"]) for item in manifest["targets"])
        active_ids = sorted(int(value) for value in manifest["active_vitrina_nm_ids"])
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {
            "sheet_vitrina_v1_warehouse_functional_cutovers",
            "sheet_vitrina_v1_warehouse_opening_cost_map",
            "sheet_vitrina_v1_warehouse_wb_daily_cost",
            "sheet_vitrina_v1_warehouse_functional_events",
            "sheet_vitrina_v1_nomenclature_items",
            "sheet_vitrina_v1_warehouse_archival_estimate_versions",
            "sheet_vitrina_v1_warehouse_archival_estimate_rows",
            "sheet_vitrina_v1_warehouse_archival_estimate_active",
            "sheet_vitrina_v1_warehouse_archival_estimate_rollbacks",
        }
        blockers: list[dict[str, Any]] = []
        if missing := sorted(required - tables):
            blockers.append({"code": "required_schema_missing", "tables": missing})
            plan = _blocked_plan(manifest, manifest_path, blockers)
            return plan
        cutover = conn.execute(
            """SELECT status,plan_fingerprint FROM sheet_vitrina_v1_warehouse_functional_cutovers
               WHERE cutover_id=?""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        if cutover is None or str(cutover["status"] or "") != "posted":
            blockers.append({"code": "functional_cutover_not_posted"})

        placeholders = ",".join("?" for _ in target_ids)
        opening_rows = _rows(
            conn,
            f"""SELECT nm_id,ff_unit_cost_rub,wb_unit_cost_rub,quality,
                       provenance_json,fingerprint,created_at
                FROM sheet_vitrina_v1_warehouse_opening_cost_map
                WHERE cutover_id=? AND nm_id IN ({placeholders}) ORDER BY nm_id""",
            (FUNCTIONAL_CUTOVER_ID, *target_ids),
        )
        opening_by_nm = {int(row["nm_id"]): row for row in opening_rows}
        if set(opening_by_nm) != set(target_ids):
            blockers.append(
                {
                    "code": "opening_fallback_target_mismatch",
                    "missing_nm_ids": sorted(set(target_ids) - set(opening_by_nm)),
                }
            )
        expected_previous = _decimal(manifest["supersedes_unit_cost_rub"])
        for nm_id, row in opening_by_nm.items():
            provenance = _loads(row["provenance_json"], {})
            if (
                str(row["quality"]) != str(manifest["supersedes_quality"])
                or _decimal(row["wb_unit_cost_rub"]) != expected_previous
                or not bool(provenance.get("missing_purchase_price"))
            ):
                blockers.append(
                    {
                        "code": "opening_fallback_source_drift",
                        "nm_id": nm_id,
                        "quality": str(row["quality"]),
                        "wb_unit_cost_rub": str(row["wb_unit_cost_rub"]),
                    }
                )

        nomenclature_rows = _rows(
            conn,
            f"""SELECT nm_id,vendor_code,nomenclature_name,purchase_price_yuan
                FROM sheet_vitrina_v1_nomenclature_items
                WHERE nm_id IN ({placeholders}) ORDER BY nm_id""",
            tuple(target_ids),
        )
        nomenclature_by_nm: dict[int, list[dict[str, Any]]] = {}
        for row in nomenclature_rows:
            nomenclature_by_nm.setdefault(int(row["nm_id"]), []).append(row)
        manifest_by_nm = {int(item["nm_id"]): item for item in manifest["targets"]}
        nomenclature_identity_proof: list[dict[str, Any]] = []
        for nm_id in target_ids:
            sources = nomenclature_by_nm.get(nm_id) or []
            wanted = manifest_by_nm[nm_id]
            if not sources:
                blockers.append({"code": "nomenclature_target_missing", "nm_id": nm_id})
                continue
            if len(sources) != 1:
                blockers.append(
                    {
                        "code": "nomenclature_target_ambiguous",
                        "nm_id": nm_id,
                        "row_count": len(sources),
                    }
                )
            for source in sources:
                actual_vendor_code = str(source.get("vendor_code") or "").strip()
                actual_nomenclature_name = str(
                    source.get("nomenclature_name") or ""
                ).strip()
                expected_vendor_code = str(wanted["vendor_code"]).strip()
                expected_nomenclature_name = str(
                    wanted["canonical_nomenclature_name"]
                ).strip()
                identity_matches = (
                    actual_vendor_code == expected_vendor_code
                    and actual_nomenclature_name == expected_nomenclature_name
                )
                nomenclature_identity_proof.append(
                    {
                        "nm_id": nm_id,
                        "expected_vendor_code": expected_vendor_code,
                        "actual_vendor_code": actual_vendor_code,
                        "expected_nomenclature_name": expected_nomenclature_name,
                        "actual_nomenclature_name": actual_nomenclature_name,
                        "descriptive_name": str(wanted["name"]).strip(),
                        "purchase_price_yuan": source.get("purchase_price_yuan"),
                        "matches": identity_matches,
                    }
                )
                if not identity_matches:
                    blockers.append(
                        {
                            "code": "nomenclature_identity_drift",
                            "nm_id": nm_id,
                            "expected_vendor_code": expected_vendor_code,
                            "actual_vendor_code": actual_vendor_code,
                            "expected_nomenclature_name": expected_nomenclature_name,
                            "actual_nomenclature_name": actual_nomenclature_name,
                        }
                    )
                if source.get("purchase_price_yuan") not in (None, ""):
                    blockers.append(
                        {
                            "code": "target_now_has_factual_purchase_price",
                            "nm_id": nm_id,
                            "purchase_price_yuan": str(source["purchase_price_yuan"]),
                        }
                    )

        active_rows = _rows(
            conn,
            """SELECT nm_id,quantity,wac_rub,capital_rub,quality,fingerprint
               FROM sheet_vitrina_v1_warehouse_wb_daily_cost
               WHERE cutover_id=? AND as_of_date='2026-07-01' ORDER BY nm_id""",
            (FUNCTIONAL_CUTOVER_ID,),
        )
        actual_active_ids = sorted(
            int(row["nm_id"])
            for row in active_rows
            if int(row["nm_id"]) not in set(target_ids)
        )
        if actual_active_ids != active_ids:
            blockers.append(
                {
                    "code": "active_vitrina_manifest_drift",
                    "expected_nm_ids": active_ids,
                    "actual_nm_ids": actual_active_ids,
                }
            )

        factual_events = _rows(
            conn,
            f"""SELECT event_id,business_date,nm_id,quantity,capital_rub,source_id,
                       source_fingerprint,provenance_json
                FROM sheet_vitrina_v1_warehouse_functional_events
                WHERE event_type='wb_final_acceptance' AND nm_id IN ({placeholders})
                  AND business_date>=? ORDER BY business_date,event_id""",
            (*target_ids, manifest["effective_date"]),
        )
        if factual_events:
            blockers.append(
                {
                    "code": "target_has_factual_post_effective_cost_basis",
                    "event_ids": [str(row["event_id"]) for row in factual_events],
                }
            )

        active = conn.execute(
            """SELECT version.version_id,version.plan_fingerprint,version.manifest_digest
               FROM sheet_vitrina_v1_warehouse_archival_estimate_active active
               JOIN sheet_vitrina_v1_warehouse_archival_estimate_versions version
                 ON version.version_id=active.version_id WHERE active.slot=1"""
        ).fetchone()
        daily_rows = _rows(
            conn,
            f"""SELECT cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                       quality,provenance_json,fingerprint,created_at
                FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                WHERE cutover_id=? AND nm_id IN ({placeholders})
                  AND as_of_date>=? ORDER BY as_of_date,nm_id""",
            (FUNCTIONAL_CUTOVER_ID, *target_ids, manifest["effective_date"]),
        )
        if active is None:
            factual_daily_rows = []
            for row in daily_rows:
                reasons = _daily_factual_cost_reasons(row)
                if _decimal(row["wac_rub"]) != expected_previous:
                    reasons.append("wac_differs_from_approved_superseded_fallback")
                if str(row["quality"]) not in LEGACY_ESTIMATE_DAILY_QUALITIES:
                    reasons.append("quality_is_not_legacy_estimate_projection")
                if reasons:
                    factual_daily_rows.append(
                        {
                            "as_of_date": str(row["as_of_date"]),
                            "nm_id": int(row["nm_id"]),
                            "quality": str(row["quality"]),
                            "wac_rub": str(row["wac_rub"]),
                            "reasons": sorted(set(reasons)),
                        }
                    )
            if factual_daily_rows:
                blockers.append(
                    {
                        "code": "target_daily_rows_have_factual_cost_basis",
                        "rows": factual_daily_rows,
                    }
                )
        desired_daily_rows = [
            _corrected_daily_row(row, manifest=manifest, manifest_digest=archival_estimate_manifest_digest(manifest_path))
            for row in daily_rows
        ]
        manifest_digest = archival_estimate_manifest_digest(manifest_path)
        already_active = bool(
            active is not None and str(active["manifest_digest"]) == manifest_digest
        )
        source_digest = _primary_source_digest(conn)
        target_before_digest = "sha256:" + _hash(daily_rows)
        target_after_digest = "sha256:" + _hash(desired_daily_rows)
        non_target_digest = _non_target_digest(conn, target_ids=target_ids)
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "blocked" if blockers else ("no_op" if already_active else "ready"),
            "dry_run": True,
            "runtime_mutation": False,
            "apply_allowed": not blockers and not already_active,
            "effective_date": manifest["effective_date"],
            "unit_cost_rub": manifest["unit_cost_rub"],
            "quality": QUALITY,
            "owner_approval_reference": manifest["owner_approval_reference"],
            "production_dry_run_plan_sha256": manifest[
                "production_dry_run_plan_sha256"
            ],
            "manifest_digest": manifest_digest,
            "target_nm_ids": target_ids,
            "target_count": len(target_ids),
            "targets": manifest["targets"],
            "nomenclature_identity_proof": nomenclature_identity_proof,
            "active_vitrina_nm_ids": active_ids,
            "active_vitrina_count": len(active_ids),
            "target_active_intersection": sorted(set(target_ids) & set(active_ids)),
            "previous_fallback": {
                "quality": manifest["supersedes_quality"],
                "unit_cost_rub": manifest["supersedes_unit_cost_rub"],
                "rows": opening_rows,
            },
            "factual_post_effective_events": factual_events,
            "target_daily_rows_before": daily_rows,
            "target_daily_rows_after": desired_daily_rows,
            "target_daily_row_count": len(daily_rows),
            "source_digest": source_digest,
            "opening_map_digest": _table_digest(
                conn,
                "sheet_vitrina_v1_warehouse_opening_cost_map",
                "cutover_id,nm_id",
            ),
            "target_before_digest": target_before_digest,
            "target_after_digest": target_after_digest,
            "non_target_digest": non_target_digest,
            "active_vitrina_before_digest": "sha256:" + _hash(active_rows),
            "supersedes_version_id": str(active["version_id"]) if active else None,
            "already_active": already_active,
            "blockers": blockers,
            "write_set": {
                "estimate_version_rows": 0 if already_active else 1,
                "estimate_manifest_rows": 0 if already_active else len(target_ids),
                "derived_daily_rows": sum(
                    1 for before, after in zip(daily_rows, desired_daily_rows)
                    if _daily_business_identity(before) != _daily_business_identity(after)
                ),
                "primary_source_rows": 0,
                "opening_map_rows": 0,
                "warehouse_quantity_or_movement_rows": 0,
            },
            "backup_plan": {
                "method": "sqlite_online_backup",
                "integrity_check": "required_ok",
                "sha256": "required",
                "mode": "0600",
                "free_space_check": "database_size_plus_512MiB",
            },
            "apply_plan": {
                "requires_exact_fingerprint": True,
                "requires_owner_approval_reference": True,
                "single_immediate_transaction": True,
                "optimistic_source_recheck": True,
                "repeat_apply_noop": True,
            },
            "rollback_plan": {
                "runner": "warehouse-archival-estimate-rollback",
                "restores_exact_target_daily_rows": True,
                "keeps_version_and_rollback_audit": True,
                "primary_sources_mutated": False,
            },
            "invariants": {
                "exact_target_manifest": set(target_ids) == set(manifest_by_nm),
                "target_disjoint_from_active_33": not (set(target_ids) & set(active_ids)),
                "opening_map_immutable": True,
                "primary_sources_immutable": True,
                "quantities_preserved": all(
                    str(before["quantity"]) == str(after["quantity"])
                    for before, after in zip(daily_rows, desired_daily_rows)
                ),
                "capital_matches_quantity_times_wac": all(
                    _decimal(after["capital_rub"])
                    == _decimal(after["quantity"]) * _decimal(after["wac_rub"])
                    for after in desired_daily_rows
                ),
                "no_fake_movements": not factual_events,
            },
        }
        candidate_fingerprint = _plan_fingerprint(plan)
        rolled_back = conn.execute(
            """SELECT rollback.rollback_id
               FROM sheet_vitrina_v1_warehouse_archival_estimate_versions version
               JOIN sheet_vitrina_v1_warehouse_archival_estimate_rollbacks rollback
                 ON rollback.version_id=version.version_id
               WHERE version.plan_fingerprint=?""",
            (candidate_fingerprint,),
        ).fetchone()
        if rolled_back is not None:
            blockers.append(
                {
                    "code": "plan_fingerprint_previously_rolled_back",
                    "plan_fingerprint": candidate_fingerprint,
                    "rollback_id": str(rolled_back["rollback_id"]),
                }
            )
            plan["status"] = "blocked"
            plan["apply_allowed"] = False
            plan["write_set"] = {
                **dict(plan["write_set"]),
                "estimate_version_rows": 0,
                "estimate_manifest_rows": 0,
                "derived_daily_rows": 0,
            }
        plan["plan_fingerprint"] = _plan_fingerprint(plan)
        return plan
    finally:
        if owns_connection:
            if conn.in_transaction:
                conn.rollback()
            conn.close()


def apply_archival_estimate_plan(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    approval_reference: str,
    backup_dir: Path,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Apply through the common serialized warehouse writer boundary."""

    with warehouse_functional_write_lock(runtime.runtime_dir):
        return _apply_archival_estimate_plan_locked(
            runtime,
            plan,
            confirm_fingerprint=confirm_fingerprint,
            approval_reference=approval_reference,
            backup_dir=backup_dir,
            manifest_path=manifest_path,
        )


def _apply_archival_estimate_plan_locked(
    runtime: RegistryUploadDbBackedRuntime,
    plan: Mapping[str, Any],
    *,
    confirm_fingerprint: str,
    approval_reference: str,
    backup_dir: Path,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
) -> dict[str, Any]:
    """Apply one exact plan atomically after a coherent verified backup."""

    normalized = json.loads(json.dumps(dict(plan), ensure_ascii=False))
    fingerprint = str(normalized.get("plan_fingerprint") or "")
    if (
        normalized.get("contract_name") != CONTRACT_NAME
        or fingerprint != str(confirm_fingerprint or "")
        or fingerprint != _plan_fingerprint(
            {key: value for key, value in normalized.items() if key != "plan_fingerprint"}
        )
    ):
        raise WarehouseArchivalEstimateError("exact archival estimate plan fingerprint is required")
    if not str(approval_reference or "").strip():
        raise WarehouseArchivalEstimateError("owner approval reference is required")
    if normalized.get("status") == "no_op" and normalized.get("already_active") is True:
        fresh_no_op = build_archival_estimate_plan(runtime, manifest_path=manifest_path)
        if str(fresh_no_op["plan_fingerprint"]) != fingerprint:
            raise WarehouseArchivalEstimateError("archival estimate sources drifted after dry-run")
        readback = readback_archival_estimate(runtime)
        if not readback["invariants_ok"]:
            raise WarehouseArchivalEstimateError(
                "active archival estimate readback is blocked"
            )
        return {
            **readback,
            "status": "no_op_already_active",
            "idempotent": True,
            "database_written": False,
            "backup": None,
        }
    if normalized.get("status") != "ready" or normalized.get("apply_allowed") is not True:
        raise WarehouseArchivalEstimateError("archival estimate plan is not applicable")
    with _connect(runtime.db_path) as conn:
        ensure_archival_estimate_schema(conn)
        active = conn.execute(
            """SELECT version.version_id,version.plan_fingerprint,version.source_digest
               FROM sheet_vitrina_v1_warehouse_archival_estimate_active active
               JOIN sheet_vitrina_v1_warehouse_archival_estimate_versions version
                 ON version.version_id=active.version_id WHERE active.slot=1"""
        ).fetchone()
        if active is not None and str(active["plan_fingerprint"]) == fingerprint:
            readback = readback_archival_estimate(runtime)
            if not readback["invariants_ok"]:
                raise WarehouseArchivalEstimateError(
                    "active archival estimate readback is blocked"
                )
            return {
                **readback,
                "status": "no_op_already_applied",
                "idempotent": True,
                "database_written": False,
                "backup": None,
            }
    fresh = build_archival_estimate_plan(runtime, manifest_path=manifest_path)
    if str(fresh["plan_fingerprint"]) != fingerprint:
        raise WarehouseArchivalEstimateError("archival estimate sources drifted after dry-run")
    backup_root = Path(backup_dir)
    if not backup_root.is_absolute():
        raise WarehouseArchivalEstimateError("absolute backup_dir is required")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup, free_before, database_size = _create_verified_backup(
        runtime,
        root=backup_root,
        fingerprint=fingerprint,
    )
    committed = False
    try:
        with _connect(runtime.db_path) as conn:
            ensure_archival_estimate_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                locked = build_archival_estimate_plan(
                    runtime,
                    manifest_path=manifest_path,
                    _connection=conn,
                )
                if str(locked["plan_fingerprint"]) != fingerprint:
                    raise WarehouseArchivalEstimateError(
                        "archival estimate sources drifted before atomic apply"
                    )
                now = _now()
                version_id = "wbae_" + fingerprint.removeprefix("sha256:")[:24]
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_archival_estimate_versions(
                           version_id,effective_date,unit_cost_rub,quality,
                           owner_approval_reference,manifest_digest,
                           production_dry_run_plan_sha256,source_digest,plan_fingerprint,
                           supersedes_version_id,backup_json,before_daily_rows_json,
                           after_daily_rows_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        version_id,
                        locked["effective_date"],
                        locked["unit_cost_rub"],
                        locked["quality"],
                        str(approval_reference).strip(),
                        locked["manifest_digest"],
                        locked["production_dry_run_plan_sha256"],
                        locked["source_digest"],
                        fingerprint,
                        locked.get("supersedes_version_id"),
                        _json(backup),
                        _json(locked["target_daily_rows_before"]),
                        _json(locked["target_daily_rows_after"]),
                        now,
                    ),
                )
                manifest_by_nm = {
                    int(item["nm_id"]): item for item in locked["targets"]
                }
                opening_by_nm = {
                    int(item["nm_id"]): item
                    for item in locked["previous_fallback"]["rows"]
                }
                for nm_id in locked["target_nm_ids"]:
                    identity = manifest_by_nm[int(nm_id)]
                    opening = opening_by_nm[int(nm_id)]
                    lineage = {
                        "owner_approval_reference": str(approval_reference).strip(),
                        "effective_date": locked["effective_date"],
                        "unit_cost_rub": locked["unit_cost_rub"],
                        "target_manifest_digest": locked["manifest_digest"],
                        "production_dry_run_plan_sha256": locked[
                            "production_dry_run_plan_sha256"
                        ],
                        "source_digest": locked["source_digest"],
                        "calculation_fingerprint": fingerprint,
                        "supersedes_opening_fingerprint": opening["fingerprint"],
                        "canonical_nomenclature_name": identity[
                            "canonical_nomenclature_name"
                        ],
                        "descriptive_name": identity["name"],
                        "no_quantity_or_capital_created": True,
                    }
                    row_fingerprint = "sha256:" + _hash(
                        {
                            "version_id": version_id,
                            "nm_id": int(nm_id),
                            "vendor_code": identity["vendor_code"],
                            "canonical_nomenclature_name": identity[
                                "canonical_nomenclature_name"
                            ],
                            "name": identity["name"],
                            "unit_cost_rub": locked["unit_cost_rub"],
                            "quality": QUALITY,
                            "previous_unit_cost_rub": opening["wb_unit_cost_rub"],
                            "previous_quality": opening["quality"],
                            "lineage": lineage,
                        }
                    )
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_archival_estimate_rows(
                               version_id,nm_id,vendor_code,item_name,unit_cost_rub,quality,
                               previous_unit_cost_rub,previous_quality,lineage_json,row_fingerprint
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            version_id,
                            int(nm_id),
                            identity["vendor_code"],
                            identity["name"],
                            locked["unit_cost_rub"],
                            QUALITY,
                            opening["wb_unit_cost_rub"],
                            opening["quality"],
                            _json(lineage),
                            row_fingerprint,
                        ),
                    )
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_archival_estimate_active(
                           slot,version_id,updated_at) VALUES(1,?,?)
                       ON CONFLICT(slot) DO UPDATE SET version_id=excluded.version_id,
                                                        updated_at=excluded.updated_at""",
                    (version_id, now),
                )
                for row in locked["target_daily_rows_after"]:
                    conn.execute(
                        """UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
                           SET wac_rub=?,capital_rub=?,quality=?,provenance_json=?,
                               fingerprint=?,created_at=?
                           WHERE cutover_id=? AND as_of_date=? AND nm_id=?""",
                        (
                            row["wac_rub"],
                            row["capital_rub"],
                            row["quality"],
                            row["provenance_json"],
                            row["fingerprint"],
                            row["created_at"],
                            row["cutover_id"],
                            row["as_of_date"],
                            int(row["nm_id"]),
                        ),
                    )
                if _primary_source_digest(conn) != locked["source_digest"]:
                    raise WarehouseArchivalEstimateError(
                        "primary source digest changed during atomic apply"
                    )
                if _non_target_digest(
                    conn, target_ids=locked["target_nm_ids"]
                ) != locked["non_target_digest"]:
                    raise WarehouseArchivalEstimateError(
                        "non-target warehouse state changed during atomic apply"
                    )
                if _table_digest(
                    conn,
                    "sheet_vitrina_v1_warehouse_opening_cost_map",
                    "cutover_id,nm_id",
                ) != locked["opening_map_digest"]:
                    raise WarehouseArchivalEstimateError(
                        "immutable opening map changed during atomic apply"
                    )
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                raise
    except Exception:
        if not committed:
            _discard_uncommitted_backup(backup)
        raise
    readback = readback_archival_estimate(runtime)
    if readback["target_count"] != 18 or not readback["invariants_ok"]:
        raise WarehouseArchivalEstimateError("archival estimate post-apply readback failed")
    return {
        **readback,
        "status": "applied",
        "idempotent": False,
        "database_written": True,
        "applied_plan_fingerprint": fingerprint,
        "backup": backup,
        "free_space_bytes_before_backup": free_before,
        "database_size_bytes": database_size,
        "primary_source_digest_before": normalized["source_digest"],
        "primary_source_digest_after": readback["primary_source_digest"],
    }


def readback_archival_estimate(
    runtime: RegistryUploadDbBackedRuntime,
) -> dict[str, Any]:
    manifest = load_archival_estimate_manifest()
    target_ids = sorted(int(item["nm_id"]) for item in manifest["targets"])
    with _connect(runtime.db_path, readonly=True) as conn:
        cutover = conn.execute(
            """SELECT status FROM sheet_vitrina_v1_warehouse_functional_cutovers
               WHERE cutover_id=?""",
            (FUNCTIONAL_CUTOVER_ID,),
        ).fetchone()
        estimates = active_archival_estimates(conn, as_of_date=manifest["effective_date"])
        rows = [estimates[nm_id] for nm_id in sorted(estimates)]
        non_target = _non_target_digest(conn, target_ids=target_ids)
        primary = _primary_source_digest(conn)
        opening = _table_digest(
            conn,
            "sheet_vitrina_v1_warehouse_opening_cost_map",
            "cutover_id,nm_id",
        )
        daily = _rows(
            conn,
            """SELECT as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,fingerprint
               FROM sheet_vitrina_v1_warehouse_wb_daily_cost
               WHERE nm_id IN ({}) ORDER BY as_of_date,nm_id""".format(
                ",".join("?" for _ in target_ids)
            ),
            tuple(target_ids),
        )
        first_factual_dates = {
            int(row["nm_id"]): str(row["first_business_date"])
            for row in conn.execute(
                """SELECT nm_id,MIN(business_date) AS first_business_date
                   FROM sheet_vitrina_v1_warehouse_functional_events
                   WHERE event_type='wb_final_acceptance'
                     AND nm_id IN ({}) AND business_date IS NOT NULL AND business_date!=''
                     AND (CAST(quantity AS REAL)!=0 OR CAST(capital_rub AS REAL)!=0)
                   GROUP BY nm_id""".format(",".join("?" for _ in target_ids)),
                tuple(target_ids),
            ).fetchall()
        }
    pending_factual_replay = [
        {
            "as_of_date": str(item["as_of_date"]),
            "nm_id": int(item["nm_id"]),
            "quality": str(item["quality"]),
            "first_factual_date": first_factual_dates[int(item["nm_id"])],
        }
        for item in daily
        if int(item["nm_id"]) in first_factual_dates
        and str(item["as_of_date"]) >= first_factual_dates[int(item["nm_id"])]
        and str(item["quality"]) == QUALITY
    ]
    invariant_rows = bool(
        cutover is not None
        and str(cutover["status"] or "") == "posted"
        and set(estimates) == set(target_ids)
        and all(_decimal(item["unit_cost_rub"]) == Decimal("100.00") for item in rows)
        and all(str(item["quality"]) == QUALITY for item in rows)
        and all(
            _decimal(item["capital_rub"])
            == _decimal(item["quantity"]) * _decimal(item["wac_rub"])
            for item in daily
        )
        and all(
            (
                _decimal(item["wac_rub"]) == Decimal("100.00")
                and str(item["quality"]) == QUALITY
            )
            if str(item["as_of_date"])
            < first_factual_dates.get(int(item["nm_id"]), "9999-12-31")
            else str(item["quality"]) != QUALITY
            for item in daily
            if str(item["as_of_date"]) >= str(manifest["effective_date"])
        )
        and not pending_factual_replay
    )
    return {
        "contract_name": CONTRACT_NAME,
        "status": "ready" if invariant_rows else "blocked",
        "target_count": len(rows),
        "target_nm_ids": sorted(estimates),
        "unit_cost_rub": "100.00",
        "quality": QUALITY,
        "functional_cutover_status": str(cutover["status"] or "") if cutover else "missing",
        "rows": rows,
        "target_daily_rows": daily,
        "target_daily_digest": "sha256:" + _hash(daily),
        "pending_factual_replay_rows": pending_factual_replay,
        "primary_source_digest": primary,
        "non_target_digest": non_target,
        "opening_map_digest": opening,
        "invariants_ok": invariant_rows,
    }


def rollback_archival_estimate(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    plan_fingerprint: str,
    reason: str,
    backup_dir: Path,
) -> dict[str, Any]:
    """Rollback through the common serialized warehouse writer boundary."""

    with warehouse_functional_write_lock(runtime.runtime_dir):
        return _rollback_archival_estimate_locked(
            runtime,
            plan_fingerprint=plan_fingerprint,
            reason=reason,
            backup_dir=backup_dir,
        )


def _rollback_archival_estimate_locked(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    plan_fingerprint: str,
    reason: str,
    backup_dir: Path,
) -> dict[str, Any]:
    """Restore the exact pre-apply derived rows and keep append-only audit."""

    selected = str(plan_fingerprint or "").strip()
    rollback_reason = str(reason or "").strip()
    if not selected or not rollback_reason:
        raise WarehouseArchivalEstimateError(
            "rollback requires an exact plan fingerprint and reason"
        )
    with _connect(runtime.db_path, readonly=True) as conn:
        version = conn.execute(
            """SELECT * FROM sheet_vitrina_v1_warehouse_archival_estimate_versions
               WHERE plan_fingerprint=?""",
            (selected,),
        ).fetchone()
        active = conn.execute(
            """SELECT version_id FROM sheet_vitrina_v1_warehouse_archival_estimate_active
               WHERE slot=1"""
        ).fetchone()
        rolled_back = conn.execute(
            """SELECT rollback_id FROM sheet_vitrina_v1_warehouse_archival_estimate_rollbacks
               WHERE version_id=(SELECT version_id
                                 FROM sheet_vitrina_v1_warehouse_archival_estimate_versions
                                 WHERE plan_fingerprint=?)""",
            (selected,),
        ).fetchone()
        if version is None:
            raise WarehouseArchivalEstimateError("rollback version does not exist")
        if rolled_back is not None:
            return {
                "status": "no_op_already_rolled_back",
                "idempotent": True,
                "rollback_id": str(rolled_back["rollback_id"]),
            }
        if active is None or str(active["version_id"]) != str(version["version_id"]):
            raise WarehouseArchivalEstimateError(
                "rollback target is not the active archival estimate version"
            )
        before_rows = _loads(version["before_daily_rows_json"], [])
        after_rows = _loads(version["after_daily_rows_json"], [])
        target_ids = [
            int(row["nm_id"])
            for row in conn.execute(
                """SELECT nm_id FROM sheet_vitrina_v1_warehouse_archival_estimate_rows
                   WHERE version_id=? ORDER BY nm_id""",
                (version["version_id"],),
            ).fetchall()
        ]
        source_before = _primary_source_digest(conn)
        factual_after_apply = conn.execute(
            """SELECT event_id FROM sheet_vitrina_v1_warehouse_functional_events
               WHERE event_type='wb_final_acceptance' AND nm_id IN ({})
                 AND business_date>=? LIMIT 1""".format(
                ",".join("?" for _ in target_ids)
            ),
            (*target_ids, str(version["effective_date"])),
        ).fetchone()
        if factual_after_apply is not None:
            raise WarehouseArchivalEstimateError(
                "target gained factual cost evidence; rollback requires a new recovery plan"
            )
        current_rows = _rows(
            conn,
            """SELECT cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                      quality,provenance_json,fingerprint,created_at
               FROM sheet_vitrina_v1_warehouse_wb_daily_cost
               WHERE nm_id IN ({}) AND as_of_date>=?
               ORDER BY as_of_date,nm_id""".format(
                ",".join("?" for _ in target_ids)
            ),
            (*target_ids, str(version["effective_date"])),
        )
        if [_daily_business_identity(row) for row in current_rows] != [
            _daily_business_identity(row) for row in after_rows
        ]:
            raise WarehouseArchivalEstimateError(
                "target daily state drifted after apply; rollback requires a new recovery plan"
            )
    backup_root = Path(backup_dir)
    if not backup_root.is_absolute():
        raise WarehouseArchivalEstimateError("absolute backup_dir is required")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup, free_before, database_size = _create_verified_backup(
        runtime,
        root=backup_root,
        fingerprint=selected,
    )
    placeholders = ",".join("?" for _ in target_ids)
    committed = False
    try:
        with _connect(runtime.db_path) as conn:
            ensure_archival_estimate_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                locked_active = conn.execute(
                    """SELECT version_id FROM sheet_vitrina_v1_warehouse_archival_estimate_active
                       WHERE slot=1"""
                ).fetchone()
                if locked_active is None or str(locked_active["version_id"]) != str(
                    version["version_id"]
                ):
                    raise WarehouseArchivalEstimateError(
                        "active archival estimate drifted before rollback"
                    )
                if _primary_source_digest(conn) != source_before:
                    raise WarehouseArchivalEstimateError(
                        "primary sources drifted before rollback"
                    )
                conn.execute(
                    f"""DELETE FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                        WHERE cutover_id=? AND nm_id IN ({placeholders})
                          AND as_of_date>=?""",
                    (FUNCTIONAL_CUTOVER_ID, *target_ids, str(version["effective_date"])),
                )
                for row in before_rows:
                    conn.execute(
                        """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost(
                               cutover_id,as_of_date,nm_id,quantity,wac_rub,capital_rub,
                               quality,provenance_json,fingerprint,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            row["cutover_id"],
                            row["as_of_date"],
                            int(row["nm_id"]),
                            row["quantity"],
                            row["wac_rub"],
                            row["capital_rub"],
                            row["quality"],
                            row["provenance_json"],
                            row["fingerprint"],
                            row["created_at"],
                        ),
                    )
                supersedes = str(version["supersedes_version_id"] or "")
                if supersedes:
                    conn.execute(
                        """UPDATE sheet_vitrina_v1_warehouse_archival_estimate_active
                           SET version_id=?,updated_at=? WHERE slot=1""",
                        (supersedes, _now()),
                    )
                else:
                    conn.execute(
                        "DELETE FROM sheet_vitrina_v1_warehouse_archival_estimate_active WHERE slot=1"
                    )
                rollback_id = "wbaerb_" + _hash(
                    {"version_id": version["version_id"], "reason": rollback_reason}
                )[:24]
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_warehouse_archival_estimate_rollbacks(
                           rollback_id,version_id,plan_fingerprint,reason,backup_json,created_at
                       ) VALUES(?,?,?,?,?,?)""",
                    (
                        rollback_id,
                        version["version_id"],
                        selected,
                        rollback_reason,
                        _json(backup),
                        _now(),
                    ),
                )
                if _primary_source_digest(conn) != source_before:
                    raise WarehouseArchivalEstimateError(
                        "primary sources changed during rollback"
                    )
                conn.commit()
                committed = True
            except Exception:
                conn.rollback()
                raise
    except Exception:
        if not committed:
            _discard_uncommitted_backup(backup)
        raise
    return {
        "status": "rolled_back",
        "idempotent": False,
        "rollback_id": rollback_id,
        "version_id": str(version["version_id"]),
        "restored_daily_row_count": len(before_rows),
        "backup": backup,
        "free_space_bytes_before_backup": free_before,
        "database_size_bytes": database_size,
        "primary_source_digest_before": source_before,
        "primary_source_digest_after": source_before,
    }


def _corrected_daily_row(
    row: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_digest: str,
) -> dict[str, Any]:
    result = dict(row)
    quantity = _decimal(result["quantity"])
    previous_provenance = _loads(result.get("provenance_json"), {})
    provenance = {
        "source": QUALITY,
        "owner_approval_reference": manifest["owner_approval_reference"],
        "effective_date": manifest["effective_date"],
        "unit_cost_rub": manifest["unit_cost_rub"],
        "target_manifest_digest": manifest_digest,
        "production_dry_run_plan_sha256": manifest[
            "production_dry_run_plan_sha256"
        ],
        "supersedes": {
            "wac_rub": str(result["wac_rub"]),
            "quality": str(result["quality"]),
            "fingerprint": str(result["fingerprint"]),
            "provenance": previous_provenance,
        },
        "quantity_preserved": str(result["quantity"]),
        "no_quantity_or_movement_created": True,
        "last_valid_wac_retained": quantity == ZERO,
    }
    result.update(
        {
            "wac_rub": str(manifest["unit_cost_rub"]),
            "capital_rub": _text(quantity * _decimal(manifest["unit_cost_rub"])),
            "quality": QUALITY,
            "provenance_json": _json(provenance),
        }
    )
    result["fingerprint"] = "sha256:" + _hash(
        {
            "as_of_date": result["as_of_date"],
            "nm_id": int(result["nm_id"]),
            "quantity": str(result["quantity"]),
            "wac_rub": result["wac_rub"],
            "capital_rub": result["capital_rub"],
            "quality": QUALITY,
            "provenance": provenance,
        }
    )
    return result


def _daily_business_identity(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "cutover_id",
            "as_of_date",
            "nm_id",
            "quantity",
            "wac_rub",
            "capital_rub",
            "quality",
            "provenance_json",
            "fingerprint",
        )
    }


def _daily_factual_cost_reasons(row: Mapping[str, Any]) -> list[str]:
    provenance = _loads(row.get("provenance_json"), {})
    if not isinstance(provenance, Mapping):
        return ["invalid_daily_provenance"]
    reasons: list[str] = []
    for key in ("inbound_quantity", "accepted_quantity_delta", "accepted_capital_delta_rub"):
        if _decimal(provenance.get(key)) != ZERO:
            reasons.append(f"nonzero_{key}")
    for key in ("inbound_supply_ids", "accepted_event_ids"):
        value = provenance.get(key)
        if isinstance(value, list) and any(str(item or "").strip() for item in value):
            reasons.append(f"nonempty_{key}")
    return reasons


def _blocked_plan(
    manifest: Mapping[str, Any],
    path: Path,
    blockers: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = {
        "contract_name": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "status": "blocked",
        "dry_run": True,
        "runtime_mutation": False,
        "apply_allowed": False,
        "effective_date": manifest["effective_date"],
        "unit_cost_rub": manifest["unit_cost_rub"],
        "quality": QUALITY,
        "manifest_digest": archival_estimate_manifest_digest(path),
        "target_nm_ids": sorted(int(item["nm_id"]) for item in manifest["targets"]),
        "blockers": blockers,
    }
    plan["plan_fingerprint"] = _plan_fingerprint(plan)
    return plan


PRIMARY_SOURCE_TABLES = (
    "wb_finance_weekly_raw_rows",
    "temporal_source_slot_snapshots",
    "sheet_vitrina_v1_supplier_shipments",
    "sheet_vitrina_v1_supplier_shipment_lines",
    "sheet_vitrina_v1_cny_ledger_operations",
    "sheet_vitrina_v1_supplier_financial_documents",
    "sheet_vitrina_v1_supplier_financial_expense_lines",
    "sheet_vitrina_v1_ff_stock_operations",
    "sheet_vitrina_v1_ff_stock_operation_lines",
    "sheet_vitrina_v1_wb_supplies",
    "sheet_vitrina_v1_warehouse_wb_snapshots",
)


def _primary_source_digest(conn: sqlite3.Connection) -> str:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return "sha256:" + _hash(
        {
            table: _table_digest(conn, table, "rowid")
            for table in PRIMARY_SOURCE_TABLES
            if table in tables
        }
    )


def _non_target_digest(
    conn: sqlite3.Connection,
    *,
    target_ids: Iterable[int],
) -> str:
    target = sorted({int(value) for value in target_ids})
    placeholders = ",".join("?" for _ in target)
    manifest = {
        "active": _rows(
            conn,
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_active ORDER BY slot",
        ),
        "versions": _rows(
            conn,
            "SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions ORDER BY version_id",
        ),
        "balances": _rows(
            conn,
            f"""SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                WHERE nm_id NOT IN ({placeholders}) ORDER BY version_id,warehouse_key,nm_id""",
            tuple(target),
        ),
        "daily": _rows(
            conn,
            f"""SELECT * FROM sheet_vitrina_v1_warehouse_wb_daily_cost
                WHERE nm_id NOT IN ({placeholders}) ORDER BY as_of_date,nm_id""",
            tuple(target),
        ),
        "events": _rows(
            conn,
            f"""SELECT * FROM sheet_vitrina_v1_warehouse_functional_events
                WHERE nm_id NOT IN ({placeholders}) ORDER BY event_id""",
            tuple(target),
        ),
    }
    return "sha256:" + _hash(manifest)


def _table_digest(conn: sqlite3.Connection, table: str, order_by: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"[")
    first = True
    for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}"):
        if not first:
            digest.update(b",")
        first = False
        digest.update(_json(dict(row)).encode("utf-8"))
    digest.update(b"]")
    return "sha256:" + digest.hexdigest()


def _create_verified_backup(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    root: Path,
    fingerprint: str,
) -> tuple[dict[str, Any], int, int]:
    database_size = runtime.db_path.stat().st_size
    free_before = shutil.disk_usage(root).free
    if free_before < database_size + MIN_BACKUP_HEADROOM_BYTES:
        raise WarehouseArchivalEstimateError(
            "insufficient free space for coherent archival estimate backup"
        )
    digest = fingerprint.removeprefix("sha256:")
    destination = root / f"warehouse-archival-estimate-{digest[:24]}.sqlite3"
    if destination.exists():
        destination = root / (
            f"warehouse-archival-estimate-{digest[:24]}-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        )
    backup = runtime.backup_database(destination)
    destination.chmod(0o600)
    if str(backup.get("integrity_check") or "").lower() != "ok":
        _discard_uncommitted_backup(backup)
        raise WarehouseArchivalEstimateError("archival estimate backup integrity_check failed")
    return backup, free_before, database_size


def _discard_uncommitted_backup(backup: Mapping[str, Any] | None) -> None:
    path_value = str((backup or {}).get("path") or "")
    if not path_value:
        return
    path = Path(path_value)
    if not path.is_absolute():
        return
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        candidate.unlink(missing_ok=True)


def _rows(
    conn: sqlite3.Connection,
    query: str,
    parameters: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, parameters).fetchall()]


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=60)
    else:
        conn = connect_sqlite(path, priority="background")
    conn.row_factory = sqlite3.Row
    if not readonly:
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise WarehouseArchivalEstimateError(f"invalid Decimal value: {value!r}") from exc
    if not result.is_finite():
        raise WarehouseArchivalEstimateError(f"non-finite Decimal value: {value!r}")
    return result


def _text(value: Decimal) -> str:
    return format(value, "f")


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback


def _plan_fingerprint(plan: Mapping[str, Any]) -> str:
    normalized = {key: value for key, value in plan.items() if key != "plan_fingerprint"}
    return "sha256:" + _hash(normalized)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
