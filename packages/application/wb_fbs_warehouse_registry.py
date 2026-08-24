"""Official WB seller-warehouse discovery, stock readback and exact binding.

This contour is deliberately independent from the five-minute order collector.
It writes only append-only observation/workflow evidence.  WB-declared stock is
reconciliation evidence and never creates a facility, inventory document,
movement, physical quantity, capital, WAC or zero assumption.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable, Mapping

from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
from packages.application.ff_pool_foundation import BALANCES_TABLE, FACILITIES_TABLE
from packages.application.wb_fbs_orders import (
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    ensure_wb_fbs_orders_schema,
)


CONTRACT_NAME = "wb_fbs_warehouse_registry_readback_v1"
REGISTRY_RUNS_TABLE = "sheet_vitrina_v1_wb_fbs_warehouse_registry_runs"
REGISTRY_ROWS_TABLE = "sheet_vitrina_v1_wb_fbs_warehouse_registry_rows"
STOCK_RUNS_TABLE = "sheet_vitrina_v1_wb_fbs_stock_snapshot_runs"
STOCK_ROWS_TABLE = "sheet_vitrina_v1_wb_fbs_stock_snapshot_rows"
BINDING_REQUESTS_TABLE = "sheet_vitrina_v1_wb_fbs_binding_requests"
BINDING_CONFIRMATIONS_TABLE = "sheet_vitrina_v1_wb_fbs_binding_confirmations"
MAX_STOCK_CHUNK = 1000


class WbFbsWarehouseRegistryError(ValueError):
    def __init__(
        self, code: str, message: str, *, details: Any = None, http_status: int = 422
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = details
        self.http_status = int(http_status)


def ensure_wb_fbs_warehouse_registry_schema(conn: sqlite3.Connection) -> None:
    ensure_wb_fbs_orders_schema(conn)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {REGISTRY_RUNS_TABLE}(
            run_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('success','partial','failed')),
            complete INTEGER NOT NULL CHECK(complete IN (0,1)),
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            warehouse_count INTEGER NOT NULL CHECK(warehouse_count>=0),
            office_count INTEGER NOT NULL CHECK(office_count>=0),
            source_digest TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '' CHECK(length(error)<=1000)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_registry_runs_recent
        ON {REGISTRY_RUNS_TABLE}(run_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_registry_runs_no_update
        BEFORE UPDATE ON {REGISTRY_RUNS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS registry runs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_registry_runs_no_delete
        BEFORE DELETE ON {REGISTRY_RUNS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS registry runs are append-only'); END;

        CREATE TABLE IF NOT EXISTS {REGISTRY_ROWS_TABLE}(
            run_id TEXT NOT NULL REFERENCES {REGISTRY_RUNS_TABLE}(run_id),
            seller_warehouse_id INTEGER NOT NULL CHECK(seller_warehouse_id>0),
            office_id INTEGER NOT NULL CHECK(office_id>0),
            warehouse_name TEXT NOT NULL,
            office_name TEXT NOT NULL DEFAULT '',
            office_city TEXT NOT NULL DEFAULT '',
            office_federal_district TEXT NOT NULL DEFAULT '',
            cargo_type INTEGER,
            delivery_type INTEGER,
            is_deleting INTEGER NOT NULL CHECK(is_deleting IN (0,1)),
            is_processing INTEGER NOT NULL CHECK(is_processing IN (0,1)),
            evidence_digest TEXT NOT NULL,
            PRIMARY KEY(run_id,seller_warehouse_id)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_registry_rows_by_warehouse
        ON {REGISTRY_ROWS_TABLE}(seller_warehouse_id,run_id);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_registry_rows_no_update
        BEFORE UPDATE ON {REGISTRY_ROWS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS registry rows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_registry_rows_no_delete
        BEFORE DELETE ON {REGISTRY_ROWS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS registry rows are append-only'); END;

        CREATE TABLE IF NOT EXISTS {STOCK_RUNS_TABLE}(
            run_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL UNIQUE,
            registry_run_id TEXT NOT NULL REFERENCES {REGISTRY_RUNS_TABLE}(run_id),
            seller_warehouse_id INTEGER NOT NULL CHECK(seller_warehouse_id>0),
            status TEXT NOT NULL CHECK(status IN ('success','partial','failed','no_scope')),
            complete INTEGER NOT NULL CHECK(complete IN (0,1)),
            snapshot_at TEXT NOT NULL,
            requested_chrt_count INTEGER NOT NULL CHECK(requested_chrt_count>=0),
            returned_chrt_count INTEGER NOT NULL CHECK(returned_chrt_count>=0),
            identity_scope_json TEXT NOT NULL CHECK(json_valid(identity_scope_json)),
            source_digest TEXT NOT NULL,
            error TEXT NOT NULL DEFAULT '' CHECK(length(error)<=1000),
            UNIQUE(registry_run_id,seller_warehouse_id)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_stock_runs_recent
        ON {STOCK_RUNS_TABLE}(seller_warehouse_id,run_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_stock_runs_no_update
        BEFORE UPDATE ON {STOCK_RUNS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS stock runs are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_stock_runs_no_delete
        BEFORE DELETE ON {STOCK_RUNS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS stock runs are append-only'); END;

        CREATE TABLE IF NOT EXISTS {STOCK_ROWS_TABLE}(
            run_id TEXT NOT NULL REFERENCES {STOCK_RUNS_TABLE}(run_id),
            seller_warehouse_id INTEGER NOT NULL CHECK(seller_warehouse_id>0),
            chrt_id INTEGER NOT NULL CHECK(chrt_id>0),
            nm_id INTEGER NOT NULL CHECK(nm_id>0),
            amount INTEGER NOT NULL CHECK(amount>=0),
            evidence_digest TEXT NOT NULL,
            PRIMARY KEY(run_id,chrt_id)
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_stock_rows_by_warehouse_nm
        ON {STOCK_ROWS_TABLE}(seller_warehouse_id,nm_id,run_id);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_stock_rows_no_update
        BEFORE UPDATE ON {STOCK_ROWS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS stock rows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_stock_rows_no_delete
        BEFORE DELETE ON {STOCK_ROWS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS stock rows are append-only'); END;

        CREATE TABLE IF NOT EXISTS {BINDING_REQUESTS_TABLE}(
            request_id TEXT PRIMARY KEY,
            request_digest TEXT NOT NULL UNIQUE,
            seller_warehouse_id INTEGER NOT NULL CHECK(seller_warehouse_id>0),
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            registry_run_id TEXT NOT NULL REFERENCES {REGISTRY_RUNS_TABLE}(run_id),
            official_evidence_digest TEXT NOT NULL,
            expected_facility_updated_at TEXT NOT NULL,
            preview_json TEXT NOT NULL CHECK(json_valid(preview_json)),
            preview_fingerprint TEXT NOT NULL,
            actor TEXT NOT NULL,
            previewed_at TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS wb_fbs_binding_requests_no_update
        BEFORE UPDATE ON {BINDING_REQUESTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS binding requests are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_binding_requests_no_delete
        BEFORE DELETE ON {BINDING_REQUESTS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS binding requests are append-only'); END;

        CREATE TABLE IF NOT EXISTS {BINDING_CONFIRMATIONS_TABLE}(
            confirmation_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL UNIQUE REFERENCES {BINDING_REQUESTS_TABLE}(request_id),
            mapping_id TEXT NOT NULL UNIQUE REFERENCES {WAREHOUSE_MAPPINGS_TABLE}(mapping_id),
            actor TEXT NOT NULL,
            confirmed_at TEXT NOT NULL,
            result_digest TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS wb_fbs_binding_confirmations_no_update
        BEFORE UPDATE ON {BINDING_CONFIRMATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS binding confirmations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_binding_confirmations_no_delete
        BEFORE DELETE ON {BINDING_CONFIRMATIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'WB FBS binding confirmations are append-only'); END;
        """
    )


class WbFbsWarehouseRegistry:
    def __init__(
        self,
        *,
        db_path: Path,
        timestamp_factory: Any | None = None,
        source: Any | None = None,
        writer_enabled: bool | Callable[[], bool] = False,
    ) -> None:
        self.db_path = Path(db_path)
        self._now = timestamp_factory or _utc_now
        self.source = source or HttpBackedWbFbsOrdersSource()
        self._writer_enabled = (
            writer_enabled
            if callable(writer_enabled)
            else lambda: bool(writer_enabled)
        )

    def collect(self) -> dict[str, Any]:
        """Run official registry and stock reads before one short local write."""

        started_at = self._now()
        run_id = "fbsreg_" + hashlib.sha256(
            f"{started_at}:{self._now()}".encode("utf-8")
        ).hexdigest()[:28]
        try:
            warehouses = self.source.list_seller_warehouses()
            offices = self.source.list_offices()
        except Exception as exc:
            completed_at = self._now()
            self._persist(
                registry={
                    "run_id": run_id,
                    "status": "failed",
                    "complete": False,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "warehouses": [],
                    "office_count": 0,
                    "source_digest": _fingerprint({"run_id": run_id, "status": "failed"}),
                    "error": _safe_error(exc),
                },
                stock_runs=[],
            )
            return self.read_model()
        offices_by_id = {int(item.office_id): item for item in offices}
        registry_rows: list[dict[str, Any]] = []
        complete = True
        for warehouse in warehouses:
            office = offices_by_id.get(int(warehouse.office_id))
            if office is None:
                complete = False
            row = {
                "seller_warehouse_id": int(warehouse.warehouse_id),
                "office_id": int(warehouse.office_id),
                "warehouse_name": str(warehouse.name),
                "office_name": str(office.name) if office else "",
                "office_city": str(office.city) if office else "",
                "office_federal_district": (
                    str(office.federal_district) if office else ""
                ),
                "cargo_type": warehouse.cargo_type,
                "delivery_type": warehouse.delivery_type,
                "is_deleting": bool(warehouse.is_deleting),
                "is_processing": bool(warehouse.is_processing),
            }
            row["evidence_digest"] = _fingerprint(row)
            registry_rows.append(row)
        registry_source_digest = _fingerprint(
            {"warehouses": registry_rows, "office_ids": sorted(offices_by_id)}
        )
        chrt_to_nm, identity_scope = self._exact_chrt_to_nm()
        stock_runs: list[dict[str, Any]] = []
        chrt_ids = sorted(chrt_to_nm)
        for warehouse in registry_rows:
            stock_runs.append(
                self._read_warehouse_stocks(
                    registry_run_id=run_id,
                    seller_warehouse_id=int(warehouse["seller_warehouse_id"]),
                    snapshot_at=self._now(),
                    chrt_ids=chrt_ids,
                    chrt_to_nm=chrt_to_nm,
                    identity_scope=identity_scope,
                )
            )
        completed_at = self._now()
        self._persist(
            registry={
                "run_id": run_id,
                "status": "success" if complete else "partial",
                "complete": complete,
                "started_at": started_at,
                "completed_at": completed_at,
                "warehouses": registry_rows,
                "office_count": len(offices_by_id),
                "source_digest": registry_source_digest,
                "error": "" if complete else "official office evidence incomplete",
            },
            stock_runs=stock_runs,
        )
        return self.read_model()

    def _exact_chrt_to_nm(self) -> tuple[dict[int, int], dict[str, Any]]:
        if not self.db_path.exists():
            return {}, {
                "status": "unavailable",
                "active_nm_id_count": 0,
                "mapped_nm_id_count": 0,
                "complete": False,
            }
        with _connect_readonly(self.db_path) as conn:
            tables = _table_names(conn)
            candidates: dict[int, set[int]] = {}
            if OBSERVATIONS_TABLE in tables:
                for row in conn.execute(
                    f"""SELECT chrt_id,nm_id FROM {OBSERVATIONS_TABLE}
                         WHERE chrt_id IS NOT NULL AND chrt_id>0 AND nm_id>0
                         GROUP BY chrt_id,nm_id ORDER BY chrt_id,nm_id"""
                ):
                    candidates.setdefault(int(row[0]), set()).add(int(row[1]))
            if IDENTITY_MAPPINGS_TABLE in tables:
                for row in conn.execute(
                    f"""SELECT source_chrt_id,target_nm_id
                           FROM {IDENTITY_MAPPINGS_TABLE}
                          WHERE active=1 AND source_chrt_id>0 AND target_nm_id>0
                          GROUP BY source_chrt_id,target_nm_id
                          ORDER BY source_chrt_id,target_nm_id"""
                ):
                    candidates.setdefault(int(row[0]), set()).add(int(row[1]))
            exact = {
                chrt_id: next(iter(nm_ids))
                for chrt_id, nm_ids in candidates.items()
                if len(nm_ids) == 1
            }
            active_nm_ids: set[int] | None = None
            if "sheet_vitrina_v1_nomenclature_items" in tables:
                active_nm_ids = {
                    int(row[0])
                    for row in conn.execute(
                        """SELECT DISTINCT nm_id
                             FROM sheet_vitrina_v1_nomenclature_items
                            WHERE is_active=1 AND nm_id>0 ORDER BY nm_id"""
                    )
                }
            mapped_nm_ids = set(exact.values())
            active_nm_coverage_complete = (
                active_nm_ids is not None and active_nm_ids <= mapped_nm_ids
            )
            # Orders and exact identity mappings prove only identities already
            # observed by wb-core.  They do not prove that every official WB
            # size/chrtId for an active nmId is present, so this source can
            # never claim a complete stock-request universe by itself.
            complete = False
            scope = {
                "status": (
                    "observed_identity_scope_only"
                    if active_nm_coverage_complete
                    else "partial"
                    if active_nm_ids is not None
                    else "catalog_unavailable"
                ),
                "active_nm_id_count": (
                    len(active_nm_ids) if active_nm_ids is not None else 0
                ),
                "mapped_nm_id_count": len(mapped_nm_ids),
                "unmapped_active_nm_id_count": (
                    len(active_nm_ids - mapped_nm_ids)
                    if active_nm_ids is not None
                    else None
                ),
                "ambiguous_chrt_id_count": sum(
                    len(nm_ids) != 1 for nm_ids in candidates.values()
                ),
                "complete": complete,
                "active_nm_coverage_complete": active_nm_coverage_complete,
                "full_official_chrt_catalog_available": False,
                "sources": [
                    name
                    for name in (OBSERVATIONS_TABLE, IDENTITY_MAPPINGS_TABLE)
                    if name in tables
                ],
            }
            return exact, scope

    def _read_warehouse_stocks(
        self,
        *,
        registry_run_id: str,
        seller_warehouse_id: int,
        snapshot_at: str,
        chrt_ids: list[int],
        chrt_to_nm: Mapping[int, int],
        identity_scope: Mapping[str, Any],
    ) -> dict[str, Any]:
        run_id = "fbsstock_" + hashlib.sha256(
            f"{registry_run_id}:{seller_warehouse_id}".encode("utf-8")
        ).hexdigest()[:26]
        if not chrt_ids:
            scope_complete = bool(identity_scope.get("complete"))
            return {
                "run_id": run_id,
                "registry_run_id": registry_run_id,
                "seller_warehouse_id": seller_warehouse_id,
                "status": "no_scope" if scope_complete else "partial",
                "complete": scope_complete,
                "snapshot_at": snapshot_at,
                "requested_chrt_count": 0,
                "rows": [],
                "identity_scope": dict(identity_scope),
                "source_digest": _fingerprint(
                    {"requested": [], "identity_scope": dict(identity_scope)}
                ),
                "error": "" if scope_complete else "exact chrtId scope incomplete",
            }
        returned: dict[int, int] = {}
        try:
            for offset in range(0, len(chrt_ids), MAX_STOCK_CHUNK):
                chunk = chrt_ids[offset : offset + MAX_STOCK_CHUNK]
                for item in self.source.list_stocks(
                    warehouse_id=seller_warehouse_id, chrt_ids=chunk
                ):
                    if int(item.chrt_id) in returned:
                        raise ValueError("duplicate chrtId across stock chunks")
                    returned[int(item.chrt_id)] = int(item.amount)
        except Exception as exc:
            return {
                "run_id": run_id,
                "registry_run_id": registry_run_id,
                "seller_warehouse_id": seller_warehouse_id,
                "status": "failed",
                "complete": False,
                "snapshot_at": snapshot_at,
                "requested_chrt_count": len(chrt_ids),
                "rows": [],
                "identity_scope": dict(identity_scope),
                "source_digest": _fingerprint(
                    {
                        "requested": chrt_ids,
                        "status": "failed",
                        "identity_scope": dict(identity_scope),
                    }
                ),
                "error": _safe_error(exc),
            }
        rows = [
            {
                "seller_warehouse_id": seller_warehouse_id,
                "chrt_id": chrt_id,
                "nm_id": int(chrt_to_nm[chrt_id]),
                "amount": amount,
            }
            for chrt_id, amount in sorted(returned.items())
        ]
        for row in rows:
            row["evidence_digest"] = _fingerprint(row)
        request_complete = set(returned) == set(chrt_ids)
        complete = request_complete and bool(identity_scope.get("complete"))
        return {
            "run_id": run_id,
            "registry_run_id": registry_run_id,
            "seller_warehouse_id": seller_warehouse_id,
            "status": "success" if complete else "partial",
            "complete": complete,
            "snapshot_at": snapshot_at,
            "requested_chrt_count": len(chrt_ids),
            "rows": rows,
            "identity_scope": dict(identity_scope),
            "source_digest": _fingerprint(
                {
                    "requested": chrt_ids,
                    "returned": rows,
                    "identity_scope": dict(identity_scope),
                }
            ),
            "error": (
                ""
                if complete
                else "one or more requested chrtIds were absent"
                if not request_complete
                else "exact chrtId scope incomplete"
            ),
        }

    def _persist(
        self, *, registry: Mapping[str, Any], stock_runs: list[Mapping[str, Any]]
    ) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_wb_fbs_warehouse_registry_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"""INSERT OR IGNORE INTO {REGISTRY_RUNS_TABLE}(
                       run_id,status,complete,started_at,completed_at,warehouse_count,
                       office_count,source_digest,error
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    registry["run_id"], registry["status"], int(bool(registry["complete"])),
                    registry["started_at"], registry["completed_at"],
                    len(registry.get("warehouses") or []), registry["office_count"],
                    registry["source_digest"], registry.get("error") or "",
                ),
            )
            for row in registry.get("warehouses") or []:
                conn.execute(
                    f"""INSERT OR IGNORE INTO {REGISTRY_ROWS_TABLE}(
                           run_id,seller_warehouse_id,office_id,warehouse_name,
                           office_name,office_city,office_federal_district,cargo_type,
                           delivery_type,is_deleting,is_processing,evidence_digest
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        registry["run_id"], row["seller_warehouse_id"], row["office_id"],
                        row["warehouse_name"], row["office_name"], row["office_city"],
                        row["office_federal_district"], row["cargo_type"],
                        row["delivery_type"], int(row["is_deleting"]),
                        int(row["is_processing"]), row["evidence_digest"],
                    ),
                )
            for stock in stock_runs:
                conn.execute(
                    f"""INSERT OR IGNORE INTO {STOCK_RUNS_TABLE}(
                           run_id,registry_run_id,seller_warehouse_id,status,complete,
                           snapshot_at,requested_chrt_count,returned_chrt_count,
                           identity_scope_json,source_digest,error
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        stock["run_id"], stock["registry_run_id"],
                        stock["seller_warehouse_id"], stock["status"],
                        int(bool(stock["complete"])), stock["snapshot_at"],
                        stock["requested_chrt_count"], len(stock.get("rows") or []),
                        _json(stock.get("identity_scope") or {}),
                        stock["source_digest"], stock.get("error") or "",
                    ),
                )
                for row in stock.get("rows") or []:
                    conn.execute(
                        f"""INSERT OR IGNORE INTO {STOCK_ROWS_TABLE}(
                               run_id,seller_warehouse_id,chrt_id,nm_id,amount,evidence_digest
                           ) VALUES(?,?,?,?,?,?)""",
                        (
                            stock["run_id"], row["seller_warehouse_id"], row["chrt_id"],
                            row["nm_id"], row["amount"], row["evidence_digest"],
                        ),
                    )
            conn.commit()

    def read_model(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return _empty_read_model("schema_absent")
        observed_at = self._now()
        with _connect_readonly(self.db_path) as conn:
            required = {
                REGISTRY_RUNS_TABLE, REGISTRY_ROWS_TABLE, STOCK_RUNS_TABLE,
                STOCK_ROWS_TABLE, WAREHOUSE_MAPPINGS_TABLE, FACILITIES_TABLE,
                BALANCES_TABLE,
            }
            if not required <= _table_names(conn):
                return _empty_read_model("schema_absent")
            registry_run = conn.execute(
                f"SELECT * FROM {REGISTRY_RUNS_TABLE} "
                "WHERE status IN ('success','partial') ORDER BY run_sequence DESC LIMIT 1"
            ).fetchone()
            latest_attempt = conn.execute(
                f"SELECT * FROM {REGISTRY_RUNS_TABLE} ORDER BY run_sequence DESC LIMIT 1"
            ).fetchone()
            if registry_run is None:
                return {
                    **_empty_read_model("unavailable"),
                    "latest_attempt": dict(latest_attempt) if latest_attempt else None,
                }
            warehouses: list[dict[str, Any]] = []
            for official in conn.execute(
                f"SELECT * FROM {REGISTRY_ROWS_TABLE} WHERE run_id=? "
                "ORDER BY warehouse_name,seller_warehouse_id",
                (registry_run["run_id"],),
            ):
                mapping = conn.execute(
                    f"""SELECT mapping.*,facility.name AS facility_name,
                                facility.active AS facility_active,
                                facility.updated_at AS facility_updated_at
                           FROM {WAREHOUSE_MAPPINGS_TABLE} AS mapping
                           JOIN {FACILITIES_TABLE} AS facility
                             ON facility.facility_id=mapping.facility_id
                          WHERE mapping.seller_warehouse_id=? AND mapping.active=1
                          ORDER BY mapping.created_at DESC,mapping.mapping_id DESC LIMIT 1""",
                    (official["seller_warehouse_id"],),
                ).fetchone()
                stock_run = conn.execute(
                    f"SELECT * FROM {STOCK_RUNS_TABLE} WHERE seller_warehouse_id=? "
                    "ORDER BY run_sequence DESC LIMIT 1",
                    (official["seller_warehouse_id"],),
                ).fetchone()
                stock_rows: list[dict[str, Any]] = []
                if stock_run is not None:
                    declared_by_nm: dict[int, int] = {}
                    for item in conn.execute(
                        f"SELECT nm_id,SUM(amount) AS amount FROM {STOCK_ROWS_TABLE} "
                        "WHERE run_id=? GROUP BY nm_id ORDER BY nm_id",
                        (stock_run["run_id"],),
                    ):
                        declared_by_nm[int(item[0])] = int(item[1])
                    nm_ids = set(declared_by_nm)
                    physical_by_nm: dict[int, tuple[int, str]] = {}
                    if mapping is not None:
                        for item in conn.execute(
                            f"""SELECT nm_id,quantity,capital_rub FROM {BALANCES_TABLE}
                                 WHERE facility_id=? AND pool='FBS' ORDER BY nm_id""",
                            (mapping["facility_id"],),
                        ):
                            physical_by_nm[int(item[0])] = (
                                int(item[1]), str(item[2])
                            )
                        nm_ids.update(physical_by_nm)
                    for nm_id in sorted(nm_ids):
                        declared = declared_by_nm.get(nm_id)
                        physical = physical_by_nm.get(nm_id)
                        stock_rows.append(
                            {
                                "nm_id": nm_id,
                                "internal_physical_quantity": (
                                    physical[0] if physical is not None else None
                                ),
                                "internal_capital_rub": (
                                    physical[1] if physical is not None else None
                                ),
                                "wb_declared_quantity": declared,
                                "delta_quantity": (
                                    declared - physical[0]
                                    if declared is not None and physical is not None
                                    else None
                                ),
                            }
                        )
                warehouses.append(
                    {
                        "seller_warehouse_id": int(official["seller_warehouse_id"]),
                        "name": str(official["warehouse_name"]),
                        "office": {
                            "id": int(official["office_id"]),
                            "name": str(official["office_name"]),
                            "city": str(official["office_city"]),
                            "federal_district": str(
                                official["office_federal_district"]
                            ),
                        },
                        "official_evidence_digest": str(official["evidence_digest"]),
                        "binding_status": "Привязан" if mapping else "Не привязан",
                        "facility": (
                            {
                                "facility_id": str(mapping["facility_id"]),
                                "name": str(mapping["facility_name"]),
                                "active": bool(mapping["facility_active"]),
                            }
                            if mapping
                            else None
                        ),
                        "stock_readback": (
                            {
                                "status": str(stock_run["status"]),
                                "complete": bool(stock_run["complete"]),
                                "snapshot_at": str(stock_run["snapshot_at"]),
                                "freshness": _freshness(
                                    str(stock_run["snapshot_at"]), observed_at
                                ),
                                "source_digest": str(stock_run["source_digest"]),
                                "identity_scope": json.loads(
                                    str(stock_run["identity_scope_json"] or "{}")
                                ),
                                "requested_chrt_count": int(
                                    stock_run["requested_chrt_count"]
                                ),
                                "returned_chrt_count": int(
                                    stock_run["returned_chrt_count"]
                                ),
                                "rows": stock_rows,
                            }
                            if stock_run
                            else {
                                "status": "unavailable",
                                "complete": False,
                                "snapshot_at": "",
                                "freshness": "unavailable",
                                "source_digest": "",
                                "identity_scope": {
                                    "status": "unavailable",
                                    "complete": False,
                                },
                                "requested_chrt_count": 0,
                                "returned_chrt_count": 0,
                                "rows": [],
                            }
                        ),
                    }
                )
            bound_facilities = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT facility_id FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1"
                )
            }
            waiting_facilities = [
                {
                    "facility_id": str(row["facility_id"]),
                    "name": str(row["name"]),
                    "active": bool(row["active"]),
                    "binding_status": "Ожидает привязки к WB",
                    "updated_at": str(row["updated_at"]),
                }
                for row in conn.execute(
                    f"SELECT * FROM {FACILITIES_TABLE} ORDER BY name,facility_id"
                )
                if str(row["facility_id"]) not in bound_facilities
            ]
            return {
                "contract": CONTRACT_NAME,
                "status": "ready",
                "registry": {
                    "run_id": str(registry_run["run_id"]),
                    "captured_at": str(registry_run["completed_at"]),
                    "complete": bool(registry_run["complete"]),
                    "source_digest": str(registry_run["source_digest"]),
                    "warehouse_count": int(registry_run["warehouse_count"]),
                },
                "latest_attempt": dict(latest_attempt) if latest_attempt else None,
                "warehouses": warehouses,
                "waiting_facilities": waiting_facilities,
                "policy": {
                    "binding": "exact_official_id_only",
                    "cardinality": "one_active_wb_warehouse_to_one_fbs_facility",
                    "wb_stock_role": "reconciliation_only",
                    "missing_row_is_zero": False,
                    "order_collector_dependency": False,
                },
            }

    def preview_binding(
        self, payload: Mapping[str, Any], *, actor: str
    ) -> dict[str, Any]:
        request_id = _request_id(payload.get("request_id"))
        seller_warehouse_id = _positive_int(
            payload.get("seller_warehouse_id"), "seller_warehouse_id"
        )
        facility_id = _identity(payload.get("facility_id"), "facility_id")
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_wb_fbs_warehouse_registry_schema(conn)
            official = conn.execute(
                f"""SELECT registry.*,run.run_sequence FROM {REGISTRY_ROWS_TABLE} registry
                     JOIN {REGISTRY_RUNS_TABLE} run ON run.run_id=registry.run_id
                     WHERE registry.seller_warehouse_id=?
                       AND run.status IN ('success','partial')
                     ORDER BY run.run_sequence DESC LIMIT 1""",
                (seller_warehouse_id,),
            ).fetchone()
            facility = conn.execute(
                f"SELECT * FROM {FACILITIES_TABLE} WHERE facility_id=?", (facility_id,)
            ).fetchone()
            if official is None:
                raise WbFbsWarehouseRegistryError(
                    "official_warehouse_not_discovered",
                    "The exact official WB seller warehouse was not discovered",
                    http_status=409,
                )
            if facility is None:
                raise WbFbsWarehouseRegistryError(
                    "facility_not_found", "Internal facility was not found", http_status=404
                )
            conflict = conn.execute(
                f"""SELECT seller_warehouse_id,facility_id FROM {WAREHOUSE_MAPPINGS_TABLE}
                     WHERE active=1 AND (seller_warehouse_id=? OR facility_id=?)
                     ORDER BY created_at,mapping_id LIMIT 1""",
                (seller_warehouse_id, facility_id),
            ).fetchone()
            if conflict is not None:
                raise WbFbsWarehouseRegistryError(
                    "active_binding_conflict",
                    "WB warehouse or internal facility already has an active binding",
                    details=dict(conflict),
                    http_status=409,
                )
            preview = {
                "seller_warehouse": {
                    "id": seller_warehouse_id,
                    "name": str(official["warehouse_name"]),
                    "office_id": int(official["office_id"]),
                    "office_name": str(official["office_name"]),
                    "office_city": str(official["office_city"]),
                },
                "facility": {
                    "facility_id": facility_id,
                    "name": str(facility["name"]),
                    "active": bool(facility["active"]),
                },
                "effect": {
                    "create_exact_binding": True,
                    "create_facility": False,
                    "create_inventory_or_movement": False,
                    "wb_mutation": False,
                    "bounded_recovery_scope": {
                        "seller_warehouse_id": seller_warehouse_id,
                        "only_unresolved_identities": True,
                        "global_backlog_replay": False,
                    },
                },
            }
            request_digest = _fingerprint(
                {
                    "request_id": request_id,
                    "seller_warehouse_id": seller_warehouse_id,
                    "facility_id": facility_id,
                    "official_evidence_digest": str(official["evidence_digest"]),
                    "facility_updated_at": str(facility["updated_at"]),
                }
            )
            preview_fingerprint = _fingerprint(preview)
            existing = conn.execute(
                f"SELECT * FROM {BINDING_REQUESTS_TABLE} WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise WbFbsWarehouseRegistryError(
                        "request_id_identity_conflict",
                        "request_id was already used for another binding preview",
                        http_status=409,
                    )
            else:
                conn.execute(
                    f"""INSERT INTO {BINDING_REQUESTS_TABLE}(
                           request_id,request_digest,seller_warehouse_id,facility_id,
                           registry_run_id,official_evidence_digest,
                           expected_facility_updated_at,preview_json,preview_fingerprint,
                           actor,previewed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request_id, request_digest, seller_warehouse_id, facility_id,
                        str(official["run_id"]), str(official["evidence_digest"]),
                        str(facility["updated_at"]), _json(preview), preview_fingerprint,
                        _actor(actor), self._now(),
                    ),
                )
                conn.commit()
            return {
                "contract": "wb_fbs_exact_binding_preview_v1",
                "request_id": request_id,
                "preview_fingerprint": preview_fingerprint,
                "confirm_required": True,
                "preview": preview,
            }

    def confirm_binding(
        self,
        request_id: str,
        *,
        preview_fingerprint: str,
        actor: str,
    ) -> dict[str, Any]:
        if not bool(self._writer_enabled()):
            raise WbFbsWarehouseRegistryError(
                "binding_writer_disabled", "Binding writer is disabled", http_status=409
            )
        selected = _request_id(request_id)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_wb_fbs_warehouse_registry_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            request = conn.execute(
                f"SELECT * FROM {BINDING_REQUESTS_TABLE} WHERE request_id=?", (selected,)
            ).fetchone()
            if request is None:
                raise WbFbsWarehouseRegistryError(
                    "binding_preview_not_found", "Binding preview was not found", http_status=404
                )
            if str(request["preview_fingerprint"]) != str(preview_fingerprint or ""):
                raise WbFbsWarehouseRegistryError(
                    "binding_preview_fingerprint_mismatch",
                    "Binding preview fingerprint does not match",
                    http_status=409,
                )
            existing = conn.execute(
                f"SELECT * FROM {BINDING_CONFIRMATIONS_TABLE} WHERE request_id=?",
                (selected,),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                return self._binding_result(existing, idempotent=True)
            official = conn.execute(
                f"""SELECT registry.* FROM {REGISTRY_ROWS_TABLE} registry
                     JOIN {REGISTRY_RUNS_TABLE} run ON run.run_id=registry.run_id
                     WHERE registry.seller_warehouse_id=?
                       AND run.status IN ('success','partial')
                     ORDER BY run.run_sequence DESC LIMIT 1""",
                (request["seller_warehouse_id"],),
            ).fetchone()
            facility = conn.execute(
                f"SELECT * FROM {FACILITIES_TABLE} WHERE facility_id=?",
                (request["facility_id"],),
            ).fetchone()
            if (
                official is None
                or facility is None
                or str(official["evidence_digest"])
                != str(request["official_evidence_digest"])
                or str(facility["updated_at"])
                != str(request["expected_facility_updated_at"])
            ):
                raise WbFbsWarehouseRegistryError(
                    "binding_source_changed",
                    "Official warehouse or facility changed after preview",
                    http_status=409,
                )
            conflict = conn.execute(
                f"""SELECT seller_warehouse_id,facility_id
                       FROM {WAREHOUSE_MAPPINGS_TABLE}
                      WHERE active=1
                        AND (seller_warehouse_id=? OR facility_id=?)
                      ORDER BY created_at,mapping_id LIMIT 1""",
                (request["seller_warehouse_id"], request["facility_id"]),
            ).fetchone()
            if conflict is not None:
                raise WbFbsWarehouseRegistryError(
                    "active_binding_conflict",
                    "WB warehouse or internal facility already has an active binding",
                    details=dict(conflict),
                    http_status=409,
                )
            mapping_material = {
                "seller_warehouse_id": int(request["seller_warehouse_id"]),
                "facility_id": str(request["facility_id"]),
                "official_evidence_digest": str(official["evidence_digest"]),
                "request_id": selected,
            }
            mapping_digest = _fingerprint(mapping_material)
            mapping_id = "fbs_wh_" + mapping_digest.removeprefix("sha256:")[:32]
            now = self._now()
            try:
                conn.execute(
                    f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
                           mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,
                           created_at,created_by,official_office_id,
                           official_warehouse_name,official_office_name,
                           official_office_city,official_evidence_digest
                       ) VALUES(?,?,?,?,1,?,?,?,?,?,?,?)""",
                    (
                        mapping_id, request["seller_warehouse_id"], request["facility_id"],
                        mapping_digest, now, _actor(actor), official["office_id"],
                        official["warehouse_name"], official["office_name"],
                        official["office_city"], official["evidence_digest"],
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise WbFbsWarehouseRegistryError(
                    "active_binding_conflict",
                    "WB warehouse or internal facility already has an active binding",
                    http_status=409,
                ) from exc
            result = {
                "mapping_id": mapping_id,
                "seller_warehouse_id": int(request["seller_warehouse_id"]),
                "facility_id": str(request["facility_id"]),
                "mapping_digest": mapping_digest,
                "bounded_recovery_scope": {
                    "seller_warehouse_id": int(request["seller_warehouse_id"]),
                    "only_unresolved_identities": True,
                    "global_backlog_replay": False,
                    "automatic_replay_started": False,
                },
            }
            confirmation_id = "fbsbind_" + hashlib.sha256(
                selected.encode("utf-8")
            ).hexdigest()[:28]
            conn.execute(
                f"""INSERT INTO {BINDING_CONFIRMATIONS_TABLE}(
                       confirmation_id,request_id,mapping_id,actor,confirmed_at,result_digest
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    confirmation_id, selected, mapping_id, _actor(actor), now,
                    _fingerprint(result),
                ),
            )
            conn.commit()
            return {"contract": "wb_fbs_exact_binding_result_v1", **result, "idempotent": False}

    def _binding_result(
        self, confirmation: Mapping[str, Any], *, idempotent: bool
    ) -> dict[str, Any]:
        with _connect_readonly(self.db_path) as conn:
            mapping = conn.execute(
                f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE mapping_id=?",
                (confirmation["mapping_id"],),
            ).fetchone()
        return {
            "contract": "wb_fbs_exact_binding_result_v1",
            "mapping_id": str(mapping["mapping_id"]),
            "seller_warehouse_id": int(mapping["seller_warehouse_id"]),
            "facility_id": str(mapping["facility_id"]),
            "mapping_digest": str(mapping["mapping_digest"]),
            "bounded_recovery_scope": {
                "seller_warehouse_id": int(mapping["seller_warehouse_id"]),
                "only_unresolved_identities": True,
                "global_backlog_replay": False,
                "automatic_replay_started": False,
            },
            "idempotent": idempotent,
        }


def _empty_read_model(status: str) -> dict[str, Any]:
    return {
        "contract": CONTRACT_NAME,
        "status": status,
        "registry": None,
        "latest_attempt": None,
        "warehouses": [],
        "waiting_facilities": [],
        "policy": {
            "binding": "exact_official_id_only",
            "cardinality": "one_active_wb_warehouse_to_one_fbs_facility",
            "wb_stock_role": "reconciliation_only",
            "missing_row_is_zero": False,
            "order_collector_dependency": False,
        },
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _request_id(value: Any) -> str:
    token = str(value or "").strip()
    if not (8 <= len(token) <= 120) or any(ord(char) < 32 for char in token):
        raise WbFbsWarehouseRegistryError(
            "invalid_request_id", "request_id must contain 8..120 safe characters"
        )
    return token


def _identity(value: Any, field: str) -> str:
    token = str(value or "").strip()
    if not token or len(token) > 160 or any(ord(char) < 32 for char in token):
        raise WbFbsWarehouseRegistryError(
            f"invalid_{field}", f"{field} is invalid"
        )
    return token


def _positive_int(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WbFbsWarehouseRegistryError(
            f"invalid_{field}", f"{field} must be a positive integer"
        ) from exc
    if result <= 0:
        raise WbFbsWarehouseRegistryError(
            f"invalid_{field}", f"{field} must be a positive integer"
        )
    return result


def _actor(value: Any) -> str:
    return _identity(value, "actor")


def _safe_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:1000]


def _freshness(snapshot_at: str, observed_at: str) -> str:
    try:
        snapshot = datetime.fromisoformat(str(snapshot_at).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError:
        return "unavailable"
    age = observed - snapshot
    if age < -timedelta(minutes=5):
        return "invalid_future_timestamp"
    return "fresh" if age <= timedelta(minutes=30) else "stale"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
