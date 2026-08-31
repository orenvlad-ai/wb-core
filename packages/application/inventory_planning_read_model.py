"""Query-only planning inventory read model for WB and facility-level FBS.

This module deliberately composes existing accounting/source evidence without
posting movements or changing the canonical six-stage warehouse projection.
Exact incident and seller-warehouse observations are additive, append-only
evidence contracts.  Until matching evidence exists the affected value stays
unavailable instead of being reconstructed or treated as zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from packages.application.ff_pool_cutover import MANIFESTS_TABLE
from packages.application.ff_pool_fbs_lifecycle import (
    CURRENT_TABLE,
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    fbs_lifecycle_group_blocked,
    fbs_lifecycle_quality_coverage,
)
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.ff_pool_fbs_applicability import (
    current_business_date,
    fbs_physical_component,
    stock_managed_nomenclature,
)
from packages.application.wb_fbs_orders import (
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)
from packages.application.wb_incident_policy import canonical_seller_id


CONTRACT_NAME = "inventory_planning_read_model"
CONTRACT_VERSION = 2
FORMULA_VERSION = "inventory_planning_v1"
INCIDENT_MANIFESTS_TABLE = "sheet_vitrina_v1_wb_incident_quantity_evidence"
INCIDENT_LINES_TABLE = "sheet_vitrina_v1_wb_incident_quantity_evidence_lines"
SELLER_STOCK_READBACKS_TABLE = "sheet_vitrina_v1_fbs_seller_stock_readbacks"
SELLER_STOCK_LINES_TABLE = "sheet_vitrina_v1_fbs_seller_stock_readback_lines"
FUNCTIONAL_ACTIVE_TABLE = "sheet_vitrina_v1_warehouse_functional_active"
WB_SNAPSHOTS_TABLE = "sheet_vitrina_v1_warehouse_wb_snapshots"
INCIDENT_POLICY_TABLE = "sheet_vitrina_v1_wb_incident_policy_revisions"


def ensure_inventory_planning_schema(conn: sqlite3.Connection) -> None:
    """Create empty evidence tables only; never seed or activate evidence."""

    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {INCIDENT_MANIFESTS_TABLE}(
            evidence_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            policy_revision INTEGER NOT NULL CHECK(policy_revision>0),
            wb_snapshot_id TEXT NOT NULL,
            wb_snapshot_digest TEXT NOT NULL,
            evidence_date TEXT NOT NULL
                CHECK(length(evidence_date)=10 AND date(evidence_date)=evidence_date),
            sku_scope_digest TEXT NOT NULL,
            evidence_digest TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            captured_at TEXT NOT NULL
                CHECK(substr(captured_at,-1,1)='Z' AND julianday(captured_at) IS NOT NULL),
            metadata_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(metadata_json)),
            UNIQUE(seller_id,policy_revision,wb_snapshot_id,evidence_digest)
        );
        CREATE INDEX IF NOT EXISTS wb_incident_quantity_evidence_current
        ON {INCIDENT_MANIFESTS_TABLE}(
            seller_id,policy_revision,wb_snapshot_id,captured_at DESC,evidence_id DESC
        );
        CREATE TABLE IF NOT EXISTS {INCIDENT_LINES_TABLE}(
            evidence_id TEXT NOT NULL REFERENCES {INCIDENT_MANIFESTS_TABLE}(evidence_id),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            incident_quantity INTEGER NOT NULL
                CHECK(typeof(incident_quantity)='integer' AND incident_quantity>=0),
            evidence_digest TEXT NOT NULL,
            PRIMARY KEY(evidence_id,nm_id)
        );
        CREATE TRIGGER IF NOT EXISTS wb_incident_quantity_evidence_no_update
        BEFORE UPDATE ON {INCIDENT_MANIFESTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB incident quantity evidence is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_incident_quantity_evidence_no_delete
        BEFORE DELETE ON {INCIDENT_MANIFESTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB incident quantity evidence is append-only'); END;
        CREATE TRIGGER IF NOT EXISTS wb_incident_quantity_lines_no_update
        BEFORE UPDATE ON {INCIDENT_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB incident quantity evidence lines are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_incident_quantity_lines_no_delete
        BEFORE DELETE ON {INCIDENT_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB incident quantity evidence lines are append-only'); END;

        CREATE TABLE IF NOT EXISTS {SELLER_STOCK_READBACKS_TABLE}(
            readback_id TEXT PRIMARY KEY,
            seller_id TEXT NOT NULL,
            captured_at TEXT NOT NULL
                CHECK(substr(captured_at,-1,1)='Z' AND julianday(captured_at) IS NOT NULL),
            source TEXT NOT NULL,
            source_digest TEXT NOT NULL UNIQUE,
            complete INTEGER NOT NULL CHECK(complete IN (0,1)),
            metadata_json TEXT NOT NULL DEFAULT '{{}}' CHECK(json_valid(metadata_json))
        );
        CREATE INDEX IF NOT EXISTS fbs_seller_stock_readbacks_current
        ON {SELLER_STOCK_READBACKS_TABLE}(seller_id,captured_at DESC,readback_id DESC);
        CREATE TABLE IF NOT EXISTS {SELLER_STOCK_LINES_TABLE}(
            readback_id TEXT NOT NULL REFERENCES {SELLER_STOCK_READBACKS_TABLE}(readback_id),
            seller_warehouse_id INTEGER NOT NULL
                CHECK(typeof(seller_warehouse_id)='integer' AND seller_warehouse_id>0),
            nm_id INTEGER NOT NULL CHECK(typeof(nm_id)='integer' AND nm_id>0),
            quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>=0),
            line_digest TEXT NOT NULL,
            PRIMARY KEY(readback_id,seller_warehouse_id,nm_id)
        );
        CREATE TRIGGER IF NOT EXISTS fbs_seller_stock_readbacks_no_update
        BEFORE UPDATE ON {SELLER_STOCK_READBACKS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS seller stock readback is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS fbs_seller_stock_readbacks_no_delete
        BEFORE DELETE ON {SELLER_STOCK_READBACKS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS seller stock readbacks are append-only'); END;
        CREATE TRIGGER IF NOT EXISTS fbs_seller_stock_lines_no_update
        BEFORE UPDATE ON {SELLER_STOCK_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS seller stock readback lines are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS fbs_seller_stock_lines_no_delete
        BEFORE DELETE ON {SELLER_STOCK_LINES_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS seller stock readback lines are append-only'); END;
        """
    )


class InventoryPlanningReadModel:
    """Compose current WB and FBS planning metrics from persisted evidence."""

    def __init__(self, *, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def current_fbs_facilities(
        self,
        *,
        requested_nm_ids: list[int],
    ) -> dict[str, Any]:
        """Read facility-level FBS truth without requiring any WB stock snapshot."""

        normalized_nm_ids = sorted(
            {int(nm_id) for nm_id in requested_nm_ids if int(nm_id) > 0}
        )
        with _connect_readonly(self.db_path) as conn:
            tables = _tables(conn)
            missing = sorted(_fbs_required_tables() - tables)
            if missing:
                return _etagged(
                    {
                        "contract_name": CONTRACT_NAME,
                        "contract_version": CONTRACT_VERSION,
                        "surface": "selected_facility_fbs",
                        "status": "schema_absent",
                        "missing_tables": missing,
                        "facilities": [],
                    }
                )
            fbs = _fbs_facilities(
                conn,
                seller_id=canonical_seller_id(),
                requested_nm_ids=normalized_nm_ids,
                include_seller_stock_reconciliation=False,
            )
        facilities: list[dict[str, Any]] = []
        for raw_facility in fbs["facilities"]:
            facility = {
                key: value
                for key, value in dict(raw_facility).items()
                if key != "seller_stock"
            }
            facility["sku_values"] = [
                {
                    key: value
                    for key, value in dict(raw_sku).items()
                    if key != "seller_stock"
                }
                for raw_sku in raw_facility.get("sku_values") or []
            ]
            facilities.append(facility)
        return _etagged(
            {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "surface": "selected_facility_fbs",
                "status": "ready",
                "requested_nm_ids": normalized_nm_ids,
                "facilities": facilities,
                "formula_epoch": dict(fbs["formula_epoch"]),
                "global_quality": str(fbs["quality"]),
                "global_reason_ru": str(fbs["reason_ru"]),
                "selected_facility_may_be_ready_when_global_is_unavailable": True,
                "wb_stock_operand_present": False,
            }
        )

    def current(self) -> dict[str, Any]:
        with _connect_readonly(self.db_path) as conn:
            tables = _tables(conn)
            missing = sorted(_required_tables() - tables)
            if missing:
                return _etagged(
                    {
                        "contract_name": CONTRACT_NAME,
                        "contract_version": CONTRACT_VERSION,
                        "status": "schema_absent",
                        "missing_tables": missing,
                        "metrics": [],
                        "facilities": [],
                    }
                )
            snapshot = _active_wb_snapshot(conn)
            if snapshot is None:
                return _etagged(
                    {
                        "contract_name": CONTRACT_NAME,
                        "contract_version": CONTRACT_VERSION,
                        "status": "wb_snapshot_unavailable",
                        "metrics": [],
                        "facilities": [],
                        "quality": {
                            "state": "unavailable",
                            "reason_ru": "Нет активного официального снимка остатков WB.",
                        },
                    }
                )
            wb_items = _wb_items(snapshot)
            wb_total = sum(item["quantity"] for item in wb_items)
            aggregate_only = _has_exact_aggregate_sentinel(snapshot)
            seller_id = canonical_seller_id()
            incident = _incident_deduction(
                conn,
                seller_id=seller_id,
                snapshot=snapshot,
                wb_items=wb_items,
            )
            fbs = _fbs_facilities(
                conn,
                seller_id=seller_id,
                requested_nm_ids=[item["nm_id"] for item in wb_items],
            )
            fbs_total = fbs["available_total"]
            wb_effective = (
                None
                if incident["quantity"] is None
                else wb_total - int(incident["quantity"])
            )
            total = None if fbs_total is None else wb_total + int(fbs_total)
            effective_total = (
                None
                if fbs_total is None or wb_effective is None
                else wb_effective + int(fbs_total)
            )
            metrics = [
                _metric("wb_total", "Остаток WB: всего", wb_total, "exact"),
                _metric(
                    "wb_effective_total",
                    "Остаток WB без инц.: всего",
                    wb_effective,
                    str(incident["quality"]),
                    reason_ru=str(incident["reason_ru"]),
                ),
                _metric(
                    "fbs_total",
                    "Остаток FBS: всего",
                    fbs_total,
                    str(fbs["quality"]),
                    reason_ru=str(fbs["reason_ru"]),
                ),
            ]
            metrics.extend(
                _metric(
                    f"fbs_facility:{item['facility_id']}",
                    f"Остаток FBS: {item['name']}",
                    item["available"],
                    str(item.get("state") or "missing"),
                    reason_ru=(
                        ""
                        if item["available"] is not None or not item.get("applicable")
                        else "Отсутствует exact physical FBS component."
                    ),
                )
                for item in fbs["facilities"]
            )
            metrics.extend(
                (
                    _metric(
                        "effective_total",
                        "Остаток без инц.: всего",
                        effective_total,
                        "exact" if effective_total is not None else "unavailable",
                        reason_ru=str(incident["reason_ru"] if effective_total is None else ""),
                    ),
                    _metric(
                        "total",
                        "Остаток: всего",
                        total,
                        "partial" if str(fbs["quality"]) == "partial" else "exact",
                        reason_ru=str(fbs["reason_ru"]),
                    ),
                )
            )
            sku_rows = _sku_planning_rows(
                wb_items=wb_items,
                incident=incident,
                fbs=fbs,
            )
            epoch = fbs["formula_epoch"]
            payload = {
                "contract_name": CONTRACT_NAME,
                "contract_version": CONTRACT_VERSION,
                "status": "ready",
                "formula": {
                    "version": FORMULA_VERSION,
                    "effective_from": epoch.get("effective_from"),
                    "feature_epoch": epoch.get("feature_epoch"),
                    "source_cutover_id": epoch.get("cutover_id"),
                    "history_rule": (
                        "История до границы не переписывается; новые формулы применяются "
                        "только к current planning/read metrics после указанной границы."
                    ),
                    "stock_total": "Остаток WB: всего + Остаток FBS: всего",
                    "effective_total": (
                        "Остаток WB: всего − exact incident quantity + Остаток FBS: всего"
                    ),
                    "fbs_available": "physical − reserved; отрицательное значение сохраняется",
                    "six_stage_total_changed": False,
                    "accounting_operand_added": False,
                },
                "metrics": metrics,
                "skus": sku_rows,
                "wb": {
                    "raw_total": wb_total,
                    "incident_quantity": incident["quantity"],
                    "effective_total": wb_effective,
                    "snapshot_id": str(snapshot["snapshot_id"]),
                    "snapshot_digest": str(snapshot["raw_rows_digest"]),
                    "snapshot_date": str(snapshot["snapshot_date"]),
                    "fetched_at": str(snapshot["fetched_at"]),
                    "pagination_complete": bool(snapshot["pagination_complete"]),
                    "aggregate_only": aggregate_only,
                    "districts": {
                        "available": not aggregate_only,
                        "reason_ru": (
                            ""
                            if not aggregate_only
                            else "Недоступно: WB временно не передаёт распределение"
                        ),
                        "historical_values_preserved": True,
                    },
                    "incident_evidence": incident,
                },
                "fbs": {
                    "physical": fbs["physical_total"],
                    "reserved": fbs["reserved_total"],
                    "available": fbs_total,
                    "facilities": fbs["facilities"],
                    "missing_components": fbs["missing_components"],
                    "inactive_facility_count": fbs["inactive_facility_count"],
                    "current_total_uses_active_facilities_only": True,
                    "applicability_rule": (
                        "every active facility x stock-managed active SKU is applicable "
                        "unless a dated explicit inapplicable event exists"
                    ),
                    "inactive_history_rewritten": False,
                    "seller_stock_role": "timestamped_reconciliation_only",
                    "seller_stock_reconciliation": fbs["seller_stock_reconciliation"],
                },
                "freshness": {
                    "wb_fetched_at": str(snapshot["fetched_at"]),
                    "fbs_updated_at": fbs["updated_at"],
                    "seller_stock_captured_at": fbs["seller_stock_captured_at"],
                },
                "quality": {
                    "wb": "aggregate_only" if aggregate_only else "warehouse_granular",
                    "incident": incident["quality"],
                    "fbs": fbs["quality"],
                },
                "privacy": {
                    "contains_pii": False,
                    "contains_raw_wb_payload": False,
                },
            }
        return _etagged(payload)


def _required_tables() -> set[str]:
    return {
        FUNCTIONAL_ACTIVE_TABLE,
        WB_SNAPSHOTS_TABLE,
        INCIDENT_POLICY_TABLE,
        INCIDENT_MANIFESTS_TABLE,
        INCIDENT_LINES_TABLE,
        FACILITIES_TABLE,
        FACILITY_PROFILES_TABLE,
        FEATURE_EPOCHS_TABLE,
        BALANCES_TABLE,
        MANIFESTS_TABLE,
        CURRENT_TABLE,
        WAREHOUSE_MAPPINGS_TABLE,
        SELLER_STOCK_READBACKS_TABLE,
        SELLER_STOCK_LINES_TABLE,
    }


def _fbs_required_tables() -> set[str]:
    return {
        FACILITIES_TABLE,
        FACILITY_PROFILES_TABLE,
        FEATURE_EPOCHS_TABLE,
        BALANCES_TABLE,
        MANIFESTS_TABLE,
        CURRENT_TABLE,
        IDENTITY_PENDING_TABLE,
        IDENTITY_PENDING_RESOLUTIONS_TABLE,
        OBSERVATIONS_TABLE,
        STATUS_OBSERVATIONS_TABLE,
        IDENTITY_EVIDENCE_TABLE,
        IDENTITY_MAPPINGS_TABLE,
        WAREHOUSE_MAPPINGS_TABLE,
    }


def _active_wb_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        f"""SELECT snapshot.*
            FROM {FUNCTIONAL_ACTIVE_TABLE} active
            JOIN {WB_SNAPSHOTS_TABLE} snapshot ON snapshot.version_id=active.version_id
            WHERE active.slot=1
            ORDER BY snapshot.created_at DESC,snapshot.snapshot_id DESC LIMIT 1"""
    ).fetchone()


def _wb_items(snapshot: Mapping[str, Any]) -> list[dict[str, int]]:
    raw = _loads(snapshot["items_json"], [])
    result: list[dict[str, int]] = []
    seen: set[int] = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        nm_id = _positive_int(item.get("nm_id"))
        quantity = _integer(item.get("quantity"))
        if nm_id is None or quantity is None or nm_id in seen:
            continue
        seen.add(nm_id)
        result.append({"nm_id": nm_id, "quantity": quantity})
    return sorted(result, key=lambda item: item["nm_id"])


def _has_exact_aggregate_sentinel(snapshot: Mapping[str, Any]) -> bool:
    raw = _loads(snapshot["raw_rows_json"], [])
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        warehouse_id = item.get("warehouseId", item.get("warehouse_id"))
        warehouse_name = item.get("warehouseName", item.get("warehouse_name"))
        region_name = item.get("regionName", item.get("region_name"))
        if (
            type(warehouse_id) is int
            and warehouse_id == -999999
            and warehouse_name == "Склад WB"
            and region_name == "Склад WB"
        ):
            return True
    return False


def _incident_deduction(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    snapshot: Mapping[str, Any],
    wb_items: list[dict[str, int]],
) -> dict[str, Any]:
    policy = conn.execute(
        f"""SELECT * FROM {INCIDENT_POLICY_TABLE}
            WHERE seller_id=? ORDER BY revision DESC LIMIT 1""",
        (seller_id,),
    ).fetchone()
    snapshot_date = str(snapshot["snapshot_date"])
    if policy is None or not _policy_is_active(policy, snapshot_date=snapshot_date):
        return {
            "quantity": 0,
            "quantity_by_nm_id": {item["nm_id"]: 0 for item in wb_items},
            "quality": "exact_no_active_incident",
            "reason_ru": "Активных инцидентов на дату снимка нет.",
            "policy_revision": int(policy["revision"]) if policy is not None else 0,
            "evidence_id": "",
            "fail_closed": False,
        }
    expected_scope_digest = _fingerprint([item["nm_id"] for item in wb_items])
    evidence = conn.execute(
        f"""SELECT * FROM {INCIDENT_MANIFESTS_TABLE}
            WHERE seller_id=? AND policy_revision=? AND wb_snapshot_id=?
              AND wb_snapshot_digest=? AND evidence_date=?
            ORDER BY captured_at DESC,evidence_id DESC LIMIT 1""",
        (
            seller_id,
            int(policy["revision"]),
            str(snapshot["snapshot_id"]),
            str(snapshot["raw_rows_digest"]),
            snapshot_date,
        ),
    ).fetchone()
    if evidence is None or str(evidence["sku_scope_digest"]) != expected_scope_digest:
        return {
            "quantity": None,
            "quantity_by_nm_id": {},
            "quality": "unavailable_exact_incident_evidence_missing",
            "reason_ru": (
                "Недоступно: для активного инцидента нет exact persisted quantity "
                "evidence по полному SKU-срезу текущего снимка WB."
            ),
            "policy_revision": int(policy["revision"]),
            "evidence_id": str(evidence["evidence_id"]) if evidence is not None else "",
            "fail_closed": True,
        }
    lines = conn.execute(
        f"SELECT nm_id,incident_quantity FROM {INCIDENT_LINES_TABLE} WHERE evidence_id=?",
        (str(evidence["evidence_id"]),),
    ).fetchall()
    allowed = {item["nm_id"] for item in wb_items}
    persisted_lines = sorted(
        (
            {
                "nm_id": int(row["nm_id"]),
                "incident_quantity": int(row["incident_quantity"]),
            }
            for row in lines
        ),
        key=lambda row: row["nm_id"],
    )
    exact_digest = _fingerprint(
        {
            "seller_id": seller_id,
            "policy_revision": int(policy["revision"]),
            "wb_snapshot_id": str(snapshot["snapshot_id"]),
            "wb_snapshot_digest": str(snapshot["raw_rows_digest"]),
            "evidence_date": snapshot_date,
            "sku_scope_digest": expected_scope_digest,
            "lines": persisted_lines,
        }
    )
    if (
        {row["nm_id"] for row in persisted_lines} != allowed
        or str(evidence["evidence_digest"]) != exact_digest
    ):
        return {
            "quantity": None,
            "quantity_by_nm_id": {},
            "quality": "unavailable_exact_incident_evidence_invalid",
            "reason_ru": (
                "Недоступно: incident evidence не покрывает exact полный SKU-срез "
                "или не проходит проверку digest."
            ),
            "policy_revision": int(policy["revision"]),
            "evidence_id": str(evidence["evidence_id"]),
            "fail_closed": True,
        }
    return {
        "quantity": sum(row["incident_quantity"] for row in persisted_lines),
        "quantity_by_nm_id": {
            row["nm_id"]: row["incident_quantity"] for row in persisted_lines
        },
        "quality": "exact_persisted_incident_evidence",
        "reason_ru": "",
        "policy_revision": int(policy["revision"]),
        "evidence_id": str(evidence["evidence_id"]),
        "evidence_digest": str(evidence["evidence_digest"]),
        "source": str(evidence["source"]),
        "captured_at": str(evidence["captured_at"]),
        "fail_closed": False,
        "synthetic_cap_applied": False,
    }


def _policy_is_active(policy: Mapping[str, Any], *, snapshot_date: str) -> bool:
    if not bool(policy["active"]):
        return False
    if str(policy["policy_status"] or "") not in {"active", "monitoring"}:
        return False
    start = str(policy["effective_from"] or "")
    end = str(policy["effective_to"] or "")
    try:
        target = date.fromisoformat(snapshot_date)
        if start and target < date.fromisoformat(start):
            return False
        if end and target > date.fromisoformat(end):
            return False
    except ValueError:
        return False
    return True


def _fbs_facilities(
    conn: sqlite3.Connection,
    *,
    seller_id: str,
    requested_nm_ids: list[int],
    include_seller_stock_reconciliation: bool = True,
) -> dict[str, Any]:
    canonical_as_of_date = current_business_date()
    manifest = conn.execute(
        f"""SELECT cutover_id,business_date,feature_epoch,cutover_at,
                   manifest_digest,observation_watermark_digest
            FROM {MANIFESTS_TABLE} ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1"""
    ).fetchone()
    feature = conn.execute(
        f"""SELECT epoch,writer_enabled,reader_enabled,created_at
            FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1"""
    ).fetchone()
    epoch_ready = bool(
        manifest is not None
        and feature is not None
        and bool(feature["reader_enabled"])
        and int(feature["epoch"]) == int(manifest["feature_epoch"])
    )
    facilities = conn.execute(
        f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                   COALESCE(profile.city,'') AS city
            FROM {FACILITIES_TABLE} facility
            LEFT JOIN {FACILITY_PROFILES_TABLE} profile
              ON profile.facility_id=facility.facility_id
            ORDER BY facility.active DESC,facility.code,facility.facility_id"""
    ).fetchall()
    readback = (
        conn.execute(
            f"""SELECT * FROM {SELLER_STOCK_READBACKS_TABLE}
                WHERE seller_id=? AND complete=1
                ORDER BY captured_at DESC,readback_id DESC LIMIT 1""",
            (seller_id,),
        ).fetchone()
        if include_seller_stock_reconciliation
        else None
    )
    readback_by_facility: dict[str, int] = {}
    readback_by_facility_nm_id: dict[tuple[str, int], int] = {}
    mapped_ids_by_facility: dict[str, list[int]] = {}
    unmatched_readback_ids: list[int] = []
    ambiguous_readback_ids: list[int] = []
    if readback is not None:
        for mapping_quality in conn.execute(
            f"""SELECT line.seller_warehouse_id,
                       COUNT(DISTINCT mapping.facility_id) AS target_count
                FROM (
                    SELECT DISTINCT seller_warehouse_id
                    FROM {SELLER_STOCK_LINES_TABLE}
                    WHERE readback_id=?
                ) line
                LEFT JOIN {WAREHOUSE_MAPPINGS_TABLE} mapping
                  ON mapping.seller_warehouse_id=line.seller_warehouse_id
                 AND mapping.active=1
                GROUP BY line.seller_warehouse_id""",
            (str(readback["readback_id"]),),
        ).fetchall():
            target_count = int(mapping_quality["target_count"] or 0)
            seller_warehouse_id = int(mapping_quality["seller_warehouse_id"])
            if target_count == 0:
                unmatched_readback_ids.append(seller_warehouse_id)
            elif target_count > 1:
                ambiguous_readback_ids.append(seller_warehouse_id)
        for row in conn.execute(
            f"""WITH exact_mapping AS (
                    SELECT seller_warehouse_id,MIN(facility_id) AS facility_id
                    FROM {WAREHOUSE_MAPPINGS_TABLE}
                    WHERE active=1
                    GROUP BY seller_warehouse_id
                    HAVING COUNT(DISTINCT facility_id)=1
                )
                SELECT mapping.facility_id,line.seller_warehouse_id,line.nm_id,
                       SUM(line.quantity) quantity
                FROM {SELLER_STOCK_LINES_TABLE} line
                JOIN exact_mapping mapping
                  ON mapping.seller_warehouse_id=line.seller_warehouse_id
                WHERE line.readback_id=?
                GROUP BY mapping.facility_id,line.seller_warehouse_id,line.nm_id""",
            (str(readback["readback_id"]),),
        ).fetchall():
            facility_id = str(row["facility_id"])
            readback_by_facility[facility_id] = (
                readback_by_facility.get(facility_id, 0) + int(row["quantity"])
            )
            readback_key = (facility_id, int(row["nm_id"]))
            readback_by_facility_nm_id[readback_key] = (
                readback_by_facility_nm_id.get(readback_key, 0) + int(row["quantity"])
            )
            mapped_ids = mapped_ids_by_facility.setdefault(facility_id, [])
            seller_warehouse_id = int(row["seller_warehouse_id"])
            if seller_warehouse_id not in mapped_ids:
                mapped_ids.append(seller_warehouse_id)
    balance_by_facility_nm_id: dict[tuple[str, int], dict[str, Any]] = {}
    reservation_by_facility_nm_id: dict[tuple[str, int], dict[str, Any]] = {}
    sku_scope = {int(nm_id) for nm_id in requested_nm_ids if int(nm_id) > 0}
    active_stock_nm_ids = {
        int(item["nm_id"]) for item in stock_managed_nomenclature(conn)
    }
    coverage_nm_ids = active_stock_nm_ids | sku_scope
    lifecycle_quality = fbs_lifecycle_quality_coverage(
        conn,
        as_of_date=canonical_as_of_date,
        requested_nm_ids=coverage_nm_ids,
    )
    if epoch_ready:
        for balance in conn.execute(
            f"""SELECT facility_id,nm_id,SUM(quantity) quantity,
                       MAX(updated_at) updated_at,MAX(source_watermark) source_watermark
                FROM {BALANCES_TABLE}
                WHERE pool='FBS' AND projection_epoch=?
                GROUP BY facility_id,nm_id""",
            (int(manifest["feature_epoch"]),),
        ).fetchall():
            key = (str(balance["facility_id"]), int(balance["nm_id"]))
            balance_by_facility_nm_id[key] = {
                "quantity": int(balance["quantity"]),
                "updated_at": str(balance["updated_at"] or ""),
                "source_watermark": str(balance["source_watermark"] or ""),
            }
            sku_scope.add(int(balance["nm_id"]))
        for reservation in conn.execute(
            f"""SELECT facility_id,nm_id,SUM(quantity) quantity,MAX(updated_at) updated_at
                FROM {CURRENT_TABLE}
                WHERE cutover_id=? AND pool='FBS' AND state='reserved'
                GROUP BY facility_id,nm_id""",
            (str(manifest["cutover_id"]),),
        ).fetchall():
            key = (str(reservation["facility_id"]), int(reservation["nm_id"]))
            reservation_by_facility_nm_id[key] = {
                "quantity": int(reservation["quantity"]),
                "updated_at": str(reservation["updated_at"] or ""),
            }
            sku_scope.add(int(reservation["nm_id"]))

    rows: list[dict[str, Any]] = []
    physical_total = 0
    reserved_total = 0
    latest_updates: list[str] = []
    active_count = 0
    applicable_count = 0
    missing_facilities: list[str] = []
    for facility in facilities:
        facility_id = str(facility["facility_id"])
        physical = reserved = None
        updated_at = ""
        sku_values: list[dict[str, Any]] = []
        typed_values: list[dict[str, Any]] = []
        facility_balances: dict[int, dict[str, Any]] = {}
        facility_reservations: dict[int, dict[str, Any]] = {}
        if epoch_ready:
            facility_balances = {
                nm_id: value
                for (row_facility_id, nm_id), value in balance_by_facility_nm_id.items()
                if row_facility_id == facility_id
            }
            facility_reservations = {
                nm_id: value
                for (row_facility_id, nm_id), value in reservation_by_facility_nm_id.items()
                if row_facility_id == facility_id
            }
            applicable = bool(facility["active"])
            for nm_id in sorted(coverage_nm_ids):
                component = fbs_physical_component(
                    conn,
                    facility_id=facility_id,
                    nm_id=nm_id,
                    as_of_date=canonical_as_of_date,
                    projection_epoch=int(manifest["feature_epoch"]),
                    facility_active=bool(facility["active"]),
                    sku_active=nm_id in active_stock_nm_ids,
                )
                reservation_value = facility_reservations.get(nm_id)
                lifecycle_blocked = fbs_lifecycle_group_blocked(
                    lifecycle_quality,
                    facility_id=facility_id,
                    nm_id=nm_id,
                )
                sku_physical = None if lifecycle_blocked else component["quantity"]
                sku_reserved = (
                    None
                    if lifecycle_blocked
                    else int(reservation_value["quantity"])
                    if reservation_value
                    else 0
                )
                sku_available = (
                    None
                    if lifecycle_blocked
                    or component["state"] in {"missing", "inapplicable"}
                    else int(sku_physical) - int(sku_reserved)
                )
                official_sku = readback_by_facility_nm_id.get((facility_id, nm_id))
                typed = {
                    "nm_id": nm_id,
                    "physical": sku_physical,
                    "reserved": sku_reserved,
                    "available": sku_available,
                    "available_is_signed": True,
                    # exact_zero describes the physical row, never a positive
                    # physical quantity fully offset by reservations.
                    "state": (
                        "missing" if lifecycle_blocked else str(component["state"])
                    ),
                    "quality": (
                        "partial_lifecycle_identity_coverage"
                        if lifecycle_blocked
                        else
                        "inapplicable"
                        if component["state"] == "inapplicable"
                        else "missing"
                        if component["state"] == "missing"
                        else "exact_ledger"
                    ),
                    "reason": (
                        "lifecycle_identity_coverage_pending"
                        if lifecycle_blocked
                        else str(component["reason"])
                    ),
                    "reason_ru": (
                        ""
                        if sku_available is not None or component["state"] == "inapplicable"
                        else (
                            "Неполная lifecycle/identity coverage FBS для SKU."
                            if lifecycle_blocked
                            else "Отсутствует exact physical FBS component для SKU."
                        )
                    ),
                    "provenance": {
                        **dict(component["provenance"]),
                        "lifecycle_quality_digest": str(
                            lifecycle_quality.get("digest") or ""
                        ),
                        "lifecycle_quality_status": str(
                            lifecycle_quality.get("status") or ""
                        ),
                    },
                    "seller_stock": {
                        "quantity": official_sku,
                        "delta_to_ledger_physical": (
                            None
                            if official_sku is None or sku_physical is None
                            else official_sku - int(sku_physical)
                        ),
                        "role": "reconciliation_only",
                    },
                }
                if nm_id in sku_scope:
                    sku_values.append(typed)
                typed_values.append(typed)
            applicable_values = [
                item for item in typed_values if item["state"] != "inapplicable"
            ]
            missing_values = [
                item for item in applicable_values if item["state"] == "missing"
            ]
            if applicable and not missing_values:
                physical = sum(int(item["physical"] or 0) for item in applicable_values)
                reserved = sum(int(item["reserved"] or 0) for item in applicable_values)
            else:
                physical = None
                reserved = None
            updated_at = max(
                [
                    str(value["updated_at"] or "")
                    for value in (*facility_balances.values(), *facility_reservations.values())
                ],
                default="",
            )
        else:
            applicable = False
        available = None if physical is None or reserved is None else physical - reserved
        official = readback_by_facility.get(facility_id) if readback is not None else None
        if bool(facility["active"]):
            active_count += 1
        if applicable:
            applicable_count += 1
            if physical is None:
                missing_facilities.append(str(facility["name"] or facility_id))
            else:
                physical_total += physical
                reserved_total += int(reserved or 0)
            if updated_at:
                latest_updates.append(updated_at)
        rows.append(
            {
                "facility_id": facility_id,
                "code": str(facility["code"]),
                "name": str(facility["name"]),
                "city": str(facility["city"] or ""),
                "active": bool(facility["active"]),
                "applicable": applicable,
                "state": (
                    "inapplicable"
                    if not applicable
                    else "exact_zero"
                    if physical == 0
                    else "exact"
                    if available is not None
                    else "missing"
                ),
                "reason": (
                    "facility_inactive"
                    if not applicable
                    else "applicable_physical_components_missing"
                    if physical is None
                    else "complete_applicable_physical_components"
                ),
                "provenance": {
                    "contract_name": "ff_pool_fbs_facility_component_v1",
                    "projection_epoch": (
                        int(manifest["feature_epoch"]) if epoch_ready else None
                    ),
                    "applicable_nm_ids": [
                        int(item["nm_id"])
                        for item in typed_values
                        if item["state"] != "inapplicable"
                    ],
                    "missing_nm_ids": [
                        int(item["nm_id"])
                        for item in typed_values
                        if item["state"] == "missing"
                    ],
                },
                "physical": physical,
                "reserved": reserved,
                "available": available,
                "available_is_signed": True,
                "sku_values": sku_values,
                "seller_stock": {
                    "quantity": official,
                    "captured_at": str(readback["captured_at"]) if readback is not None else "",
                    "mapped_seller_warehouse_ids": sorted(mapped_ids_by_facility.get(facility_id, [])),
                    "mapping_method": "exact_sellerWarehouseId",
                    "mapping_quality": "exact" if official is not None else "not_mapped",
                    "delta_to_ledger_physical": (
                        None if official is None or physical is None else official - physical
                    ),
                    "role": "reconciliation_only",
                },
                "fbs_orders_filter": {"facility_id": facility_id},
                "updated_at": updated_at,
                "source_revision": str(manifest["cutover_id"]) if manifest else "",
                "source_digest": str(manifest["manifest_digest"]) if manifest else "",
                "source_watermark": max(
                    (
                        *(
                            str(value.get("source_watermark") or "")
                            for value in facility_balances.values()
                        ),
                        *(
                            str(value.get("updated_at") or "")
                            for value in facility_reservations.values()
                        ),
                        str(manifest["observation_watermark_digest"])
                        if manifest
                        else "",
                    ),
                    default="",
                ),
            }
        )
    totals_complete = not missing_facilities
    available_total = (
        physical_total - reserved_total if totals_complete else None
    )
    totals_quality = "partial" if missing_facilities else "exact_ledger"
    applicable_rows = [row for row in rows if row["applicable"]]
    sku_values: list[dict[str, Any]] = []
    for nm_id in sorted(sku_scope):
        facility_values = [
            next(
                (
                    value
                    for value in row["sku_values"]
                    if int(value["nm_id"]) == nm_id
                ),
                None,
            )
            for row in applicable_rows
        ]
        applicable_values = [
            value
            for value in facility_values
            if value is not None and value["state"] != "inapplicable"
        ]
        missing = [
            str(row["name"] or row["facility_id"])
            for row, value in zip(applicable_rows, facility_values)
            if value is not None
            and value["state"] != "inapplicable"
            and value["available"] is None
        ]
        exact = bool(applicable_values) and all(
            value["available"] is not None for value in applicable_values
        )
        known_values = [
            value
            for value in applicable_values
            if value["available"] is not None
        ]
        sku_values.append(
            {
                "nm_id": nm_id,
                "physical": (
                    sum(int(value["physical"]) for value in known_values)
                    if exact
                    else None
                ),
                "reserved": (
                    sum(int(value["reserved"]) for value in known_values)
                    if exact
                    else None
                ),
                "available": (
                    sum(int(value["available"]) for value in known_values)
                    if exact
                    else None
                ),
                "available_is_signed": True,
                "quality": (
                    "inapplicable"
                    if not applicable_values
                    else "exact_ledger"
                    if exact
                    else "partial"
                ),
                "missing_components": missing,
                "reason_ru": (
                    ""
                    if exact
                    else "Частичные данные: отсутствуют компоненты FBS: " + ", ".join(missing)
                ),
            }
        )
    return {
        "facilities": rows,
        "physical_total": physical_total if totals_complete else None,
        "reserved_total": reserved_total if totals_complete else None,
        "available_total": available_total,
        "sku_values": sku_values,
        "quality": totals_quality,
        "missing_components": missing_facilities,
        "reason_ru": (
            ""
            if not missing_facilities
            else "Частичные данные: отсутствуют компоненты FBS: "
            + ", ".join(missing_facilities)
        ),
        "inactive_facility_count": sum(1 for row in facilities if not bool(row["active"])),
        "active_facility_count": active_count,
        "applicable_facility_count": applicable_count,
        "updated_at": max(latest_updates, default=""),
        "seller_stock_captured_at": str(readback["captured_at"]) if readback is not None else "",
        "seller_stock_reconciliation": {
            "readback_id": str(readback["readback_id"]) if readback is not None else "",
            "captured_at": str(readback["captured_at"]) if readback is not None else "",
            "source": str(readback["source"]) if readback is not None else "",
            "source_digest": str(readback["source_digest"]) if readback is not None else "",
            "complete": bool(readback["complete"]) if readback is not None else False,
            "mapping_quality": (
                "ambiguous"
                if ambiguous_readback_ids
                else "unmatched"
                if unmatched_readback_ids
                else "exact"
                if readback is not None
                else "unavailable"
            ),
            "ambiguous_seller_warehouse_ids": sorted(ambiguous_readback_ids),
            "unmatched_seller_warehouse_ids": sorted(unmatched_readback_ids),
            "role": "reconciliation_only",
        },
        "formula_epoch": {
            "cutover_id": str(manifest["cutover_id"]) if manifest is not None else "",
            "feature_epoch": int(manifest["feature_epoch"]) if manifest is not None else None,
            "effective_from": str(manifest["business_date"]) if manifest is not None else None,
        },
        "lifecycle_quality": lifecycle_quality,
    }


def _sku_planning_rows(
    *,
    wb_items: list[dict[str, int]],
    incident: Mapping[str, Any],
    fbs: Mapping[str, Any],
) -> list[dict[str, Any]]:
    wb_by_nm_id = {int(item["nm_id"]): int(item["quantity"]) for item in wb_items}
    incident_by_nm_id = {
        int(nm_id): int(quantity)
        for nm_id, quantity in dict(incident.get("quantity_by_nm_id") or {}).items()
    }
    fbs_by_nm_id = {
        int(item["nm_id"]): dict(item)
        for item in list(fbs.get("sku_values") or [])
    }
    facility_by_nm_id: dict[int, list[dict[str, Any]]] = {}
    for facility in list(fbs.get("facilities") or []):
        for item in list(facility.get("sku_values") or []):
            nm_id = int(item["nm_id"])
            facility_by_nm_id.setdefault(nm_id, []).append(
                {
                    "facility_id": str(facility["facility_id"]),
                    "name": str(facility["name"]),
                    "active": bool(facility.get("active")),
                    "applicable": bool(facility.get("applicable")),
                    "state": str(item.get("state") or "missing"),
                    "physical": item.get("physical"),
                    "reserved": item.get("reserved"),
                    "available": item.get("available"),
                    "available_is_signed": True,
                    "quality": str(item.get("quality") or "unavailable"),
                    "reason_ru": str(item.get("reason_ru") or ""),
                    "seller_stock": dict(item.get("seller_stock") or {}),
                }
            )

    nm_ids = sorted(set(wb_by_nm_id) | set(fbs_by_nm_id) | set(facility_by_nm_id))
    result: list[dict[str, Any]] = []
    for nm_id in nm_ids:
        wb_total = wb_by_nm_id.get(nm_id)
        incident_quantity = incident_by_nm_id.get(nm_id)
        incident_available = incident.get("quantity") is not None
        wb_effective = (
            wb_total - incident_quantity
            if wb_total is not None and incident_available and incident_quantity is not None
            else None
        )
        fbs_row = fbs_by_nm_id.get(nm_id) or {}
        fbs_available = fbs_row.get("available")
        total = (
            wb_total + int(fbs_available)
            if wb_total is not None and fbs_available is not None
            else None
        )
        effective_total = (
            wb_effective + int(fbs_available)
            if wb_effective is not None and fbs_available is not None
            else None
        )
        wb_reason = (
            ""
            if wb_total is not None
            else "Недоступно: официальный WB aggregate не содержит exact SKU quantity."
        )
        incident_reason = str(incident.get("reason_ru") or "")
        fbs_reason = str(fbs_row.get("reason_ru") or fbs.get("reason_ru") or "")
        result.append(
            {
                "nm_id": nm_id,
                "wb_total": wb_total,
                "wb_effective_total": wb_effective,
                "incident_quantity": incident_quantity if incident_available else None,
                "fbs_total": fbs_available,
                "fbs_physical": fbs_row.get("physical"),
                "fbs_reserved": fbs_row.get("reserved"),
                "fbs_facilities": sorted(
                    facility_by_nm_id.get(nm_id, []),
                    key=lambda item: (item["name"], item["facility_id"]),
                ),
                "total": total,
                "effective_total": effective_total,
                "quality": {
                    "missing_components": list(fbs_row.get("missing_components") or []),
                    "wb_total": "exact" if wb_total is not None else "unavailable",
                    "wb_total_reason_ru": wb_reason,
                    "wb_effective_total": (
                        str(incident.get("quality") or "unavailable")
                        if wb_effective is not None
                        else "unavailable"
                    ),
                    "wb_effective_total_reason_ru": wb_reason or incident_reason,
                    "fbs_total": str(fbs_row.get("quality") or "unavailable"),
                    "fbs_total_reason_ru": fbs_reason,
                    "total": (
                        "partial"
                        if total is not None and str(fbs_row.get("quality")) == "partial"
                        else "exact"
                        if total is not None
                        else "unavailable"
                    ),
                    "total_reason_ru": wb_reason or fbs_reason,
                    "effective_total": (
                        "exact" if effective_total is not None else "unavailable"
                    ),
                    "effective_total_reason_ru": wb_reason or incident_reason or fbs_reason,
                },
            }
        )
    return result


def _metric(
    key: str,
    label_ru: str,
    value: int | None,
    quality: str,
    *,
    reason_ru: str = "",
) -> dict[str, Any]:
    return {
        "metric_key": key,
        "label_ru": label_ru,
        "value": value,
        "unit": "шт",
        "quality": quality,
        "available": value is not None,
        "reason_ru": reason_ru,
        "independently_hideable": True,
    }


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = Path(db_path).resolve()
    if not resolved.is_file():
        raise RuntimeError("inventory planning runtime store is missing")
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def _loads(value: Any, fallback: Any) -> Any:
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _positive_int(value: Any) -> int | None:
    result = _integer(value)
    return result if result is not None and result > 0 else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    return int(number)


def _fingerprint(value: Any) -> str:
    material = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _etagged(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result["etag"] = '"' + _fingerprint(result) + '"'
    return result
