"""Append-only official-evidence versions for legacy FBS warehouse mappings.

The original mapping row remains immutable.  A version supplies only the
official WB evidence fields that did not exist when legacy mappings were
created.  It never creates a facility, inventory document, movement, balance,
cost, lifecycle event, or WB-side mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable, Mapping

from packages.application.ff_pool_foundation import FACILITIES_TABLE
from packages.application.wb_fbs_orders import WAREHOUSE_MAPPINGS_TABLE


MAPPING_EVIDENCE_VERSIONS_TABLE = (
    "sheet_vitrina_v1_wb_fbs_mapping_official_evidence_versions"
)
REGISTRY_RUNS_TABLE = "sheet_vitrina_v1_wb_fbs_warehouse_registry_runs"
REGISTRY_ROWS_TABLE = "sheet_vitrina_v1_wb_fbs_warehouse_registry_rows"
UPGRADE_MODE = "upgrade_official_evidence"
RESTORE_MODE = "restore_before_image"
MAX_REGISTRY_EVIDENCE_AGE_SECONDS = 30 * 60
OPERATION_RE = re.compile(r"[a-z0-9][a-z0-9._-]{7,127}")
IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}")
OFFICIAL_FIELDS = (
    "official_office_id",
    "official_warehouse_name",
    "official_office_name",
    "official_office_city",
    "official_evidence_digest",
)


class WbFbsMappingEvidenceError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def ensure_wb_fbs_mapping_evidence_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS {MAPPING_EVIDENCE_VERSIONS_TABLE}(
            version_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            version_id TEXT NOT NULL UNIQUE,
            operation_id TEXT NOT NULL UNIQUE,
            mapping_id TEXT NOT NULL REFERENCES {WAREHOUSE_MAPPINGS_TABLE}(mapping_id),
            seller_warehouse_id INTEGER NOT NULL CHECK(seller_warehouse_id>0),
            facility_id TEXT NOT NULL REFERENCES {FACILITIES_TABLE}(facility_id),
            mapping_digest TEXT NOT NULL,
            version_kind TEXT NOT NULL
                CHECK(version_kind IN ('official_evidence_upgrade','before_image_restore')),
            supersedes_version_id TEXT
                REFERENCES {MAPPING_EVIDENCE_VERSIONS_TABLE}(version_id),
            source_registry_run_id TEXT NOT NULL REFERENCES {REGISTRY_RUNS_TABLE}(run_id),
            live_registry_digest TEXT NOT NULL,
            official_office_id INTEGER NOT NULL CHECK(official_office_id>=0),
            official_warehouse_name TEXT NOT NULL,
            official_office_name TEXT NOT NULL,
            official_office_city TEXT NOT NULL,
            official_evidence_digest TEXT NOT NULL,
            before_image_json TEXT NOT NULL CHECK(json_valid(before_image_json)),
            before_image_digest TEXT NOT NULL,
            candidate_digest TEXT NOT NULL,
            actor TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK(
                version_kind='before_image_restore'
                OR (official_office_id>0 AND length(official_evidence_digest)>0)
            )
        );
        CREATE INDEX IF NOT EXISTS wb_fbs_mapping_evidence_versions_by_mapping
        ON {MAPPING_EVIDENCE_VERSIONS_TABLE}(mapping_id,version_sequence DESC);
        CREATE TRIGGER IF NOT EXISTS wb_fbs_mapping_evidence_versions_no_update
        BEFORE UPDATE ON {MAPPING_EVIDENCE_VERSIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS mapping evidence versions are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS wb_fbs_mapping_evidence_versions_no_delete
        BEFORE DELETE ON {MAPPING_EVIDENCE_VERSIONS_TABLE}
        BEGIN SELECT RAISE(ABORT,'FBS mapping evidence versions are append-only'); END;
        """
    )


def latest_mapping_evidence_version(
    conn: sqlite3.Connection, mapping_id: str
) -> dict[str, Any] | None:
    if MAPPING_EVIDENCE_VERSIONS_TABLE not in _table_names(conn):
        return None
    row = conn.execute(
        f"""SELECT * FROM {MAPPING_EVIDENCE_VERSIONS_TABLE}
             WHERE mapping_id=? ORDER BY version_sequence DESC LIMIT 1""",
        (str(mapping_id),),
    ).fetchone()
    return dict(row) if row is not None else None


def effective_mapping_official_evidence(
    mapping: Mapping[str, Any], version: Mapping[str, Any] | None
) -> dict[str, Any]:
    result = dict(mapping)
    if version is not None:
        for field in OFFICIAL_FIELDS:
            result[field] = version[field]
        result["official_evidence_version_id"] = str(version["version_id"])
        result["official_evidence_version_kind"] = str(version["version_kind"])
        result["official_evidence_version_digest"] = str(version["candidate_digest"])
    else:
        result["official_evidence_version_id"] = ""
        result["official_evidence_version_kind"] = "legacy_mapping_row"
        result["official_evidence_version_digest"] = ""
    return result


class WbFbsMappingEvidenceUpgrade:
    """Preview, append one exact version, and query-only read it back."""

    def __init__(
        self,
        *,
        db_path: Path,
        storage_identity: Mapping[str, Any],
        source: Any | None = None,
        timestamp_factory: Callable[[], str] | None = None,
        actor: str = "github-production-apply",
    ) -> None:
        self.db_path = Path(db_path).resolve()
        self.storage_identity = {
            "generation_id": str(storage_identity.get("generation_id") or ""),
            "generation_epoch": str(storage_identity.get("generation_epoch") or ""),
            "manifest_sha256": str(storage_identity.get("manifest_sha256") or ""),
        }
        self.source = source
        self._now = timestamp_factory or _utc_now
        self.actor = _actor(actor)

    def preview(
        self, request: Mapping[str, Any], operation_id: str
    ) -> dict[str, Any]:
        plan = self._build_plan(request, operation_id)
        return {
            "operation_id": plan["operation_id"],
            "target": f"operational:{self.storage_identity['generation_id']}",
            "scope": plan["scope"],
            "prestate_sha256": plan["prestate_sha256"],
            "candidate_sha256": plan["candidate_sha256"],
            "recovery": plan["recovery"],
            "evidence": plan["evidence"],
            "effect": {
                "append_mapping_evidence_version_count": 1,
                "update_or_delete_mapping_count": 0,
                "facility_create_count": 0,
                "inventory_or_movement_count": 0,
                "cost_or_lifecycle_event_count": 0,
                "wb_write_count": 0,
            },
        }

    def apply(
        self,
        request: Mapping[str, Any],
        operation_id: str,
        *,
        expected_prestate: str,
        expected_candidate: str,
    ) -> dict[str, Any]:
        prior = self.readback(request, operation_id)
        if prior.get("state") == "applied":
            return {
                "operation_id": str(prior["operation_id"]),
                "disposition": "already_applied",
                "version_id": str(prior["version_id"]),
                "row_insert_count": 0,
            }
        plan = self._build_plan(request, operation_id)
        if (
            plan["prestate_sha256"] != str(expected_prestate or "")
            or plan["candidate_sha256"] != str(expected_candidate or "")
        ):
            raise WbFbsMappingEvidenceError(
                "candidate_or_prestate_drift",
                "Mapping evidence candidate or prestate changed before apply",
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if MAPPING_EVIDENCE_VERSIONS_TABLE not in _table_names(conn):
                raise WbFbsMappingEvidenceError(
                    "mapping_evidence_schema_absent",
                    "Mapping evidence version schema is not deployed",
                )
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                f"SELECT * FROM {MAPPING_EVIDENCE_VERSIONS_TABLE} WHERE operation_id=?",
                (plan["operation_id"],),
            ).fetchone()
            if existing is not None:
                conn.rollback()
                if not _version_matches_request(
                    existing, plan["request"], expected_version_id=plan["version_id"]
                ):
                    raise WbFbsMappingEvidenceError(
                        "operation_id_identity_conflict",
                        "Operation ID belongs to another mapping evidence version",
                    )
                return {
                    "operation_id": plan["operation_id"],
                    "disposition": "already_applied",
                    "version_id": str(existing["version_id"]),
                    "row_insert_count": 0,
                }
            locked_plan = self._plan_with_connection(
                conn, plan["request"], plan["operation_id"], plan["live_evidence"]
            )
            if (
                locked_plan["prestate_sha256"] != plan["prestate_sha256"]
                or locked_plan["candidate_sha256"] != plan["candidate_sha256"]
            ):
                conn.rollback()
                raise WbFbsMappingEvidenceError(
                    "locked_prestate_drift",
                    "Mapping evidence state changed before the single insert",
                )
            row = locked_plan["candidate_row"]
            conn.execute(
                f"""INSERT INTO {MAPPING_EVIDENCE_VERSIONS_TABLE}(
                       version_id,operation_id,mapping_id,seller_warehouse_id,
                       facility_id,mapping_digest,version_kind,supersedes_version_id,
                       source_registry_run_id,live_registry_digest,
                       official_office_id,official_warehouse_name,
                       official_office_name,official_office_city,
                       official_evidence_digest,before_image_json,
                       before_image_digest,candidate_digest,actor,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    locked_plan["version_id"],
                    locked_plan["operation_id"],
                    row["mapping_id"],
                    row["seller_warehouse_id"],
                    row["facility_id"],
                    row["mapping_digest"],
                    row["version_kind"],
                    row["supersedes_version_id"],
                    row["source_registry_run_id"],
                    row["live_registry_digest"],
                    row["official_office_id"],
                    row["official_warehouse_name"],
                    row["official_office_name"],
                    row["official_office_city"],
                    row["official_evidence_digest"],
                    _json(row["before_image"]),
                    row["before_image_digest"],
                    locked_plan["candidate_sha256"],
                    self.actor,
                    self._now(),
                ),
            )
            conn.commit()
        return {
            "operation_id": plan["operation_id"],
            "disposition": "submitted",
            "version_id": plan["version_id"],
            "row_insert_count": 1,
        }

    def readback(
        self, request: Mapping[str, Any], operation_id: str
    ) -> dict[str, Any]:
        normalized = _normalize_request(request)
        selected_operation = _operation_id(operation_id)
        if not self.db_path.is_file():
            return {
                "operation_id": selected_operation,
                "state": "failed",
                "reason": "operational_store_missing",
            }
        with _connect_readonly(self.db_path) as conn:
            if MAPPING_EVIDENCE_VERSIONS_TABLE not in _table_names(conn):
                return {
                    "operation_id": selected_operation,
                    "state": "not_submitted",
                    "reason": "mapping_evidence_schema_absent",
                }
            row = conn.execute(
                f"SELECT * FROM {MAPPING_EVIDENCE_VERSIONS_TABLE} WHERE operation_id=?",
                (selected_operation,),
            ).fetchone()
            if row is None:
                return {
                    "operation_id": selected_operation,
                    "state": "not_submitted",
                    "mapping_id": normalized["mapping_id"],
                }
            latest = latest_mapping_evidence_version(conn, normalized["mapping_id"])
            identity_matches = bool(
                str(row["mapping_id"]) == normalized["mapping_id"]
                and int(row["seller_warehouse_id"])
                == normalized["seller_warehouse_id"]
                and str(row["facility_id"]) == normalized["facility_id"]
                and str(row["mapping_digest"])
                == normalized["expected_mapping_digest"]
            )
            latest_matches = bool(
                latest is not None
                and str(latest["version_id"]) == str(row["version_id"])
            )
            operation_matches = _version_matches_request(row, normalized)
            state = (
                "applied"
                if identity_matches and operation_matches and latest_matches
                else "failed"
            )
            return {
                "operation_id": selected_operation,
                "state": state,
                "version_id": str(row["version_id"]),
                "mapping_id": str(row["mapping_id"]),
                "seller_warehouse_id": int(row["seller_warehouse_id"]),
                "facility_id": str(row["facility_id"]),
                "official_office_id": int(row["official_office_id"]),
                "official_evidence_digest": str(row["official_evidence_digest"]),
                "candidate_sha256": str(row["candidate_digest"]),
                "latest_effective": latest_matches,
                "row_insert_count": 1,
                "reason": "" if state == "applied" else "version_identity_or_head_drift",
            }

    def _build_plan(
        self, request: Mapping[str, Any], operation_id: str
    ) -> dict[str, Any]:
        if self.source is None:
            raise WbFbsMappingEvidenceError(
                "official_source_missing",
                "Official WB source is required for preview and apply",
            )
        normalized = _normalize_request(request)
        selected_operation = _operation_id(operation_id)
        live = _stable_official_evidence(
            self.source,
            seller_warehouse_id=normalized["seller_warehouse_id"],
            expected_office_id=normalized["expected_office_id"],
        )
        with _connect_readonly(self.db_path) as conn:
            plan = self._plan_with_connection(
                conn, normalized, selected_operation, live
            )
        plan["live_evidence"] = live
        return plan

    def _plan_with_connection(
        self,
        conn: sqlite3.Connection,
        request: Mapping[str, Any],
        operation_id: str,
        live: Mapping[str, Any],
    ) -> dict[str, Any]:
        required_tables = {
            WAREHOUSE_MAPPINGS_TABLE,
            FACILITIES_TABLE,
            REGISTRY_RUNS_TABLE,
            REGISTRY_ROWS_TABLE,
            MAPPING_EVIDENCE_VERSIONS_TABLE,
        }
        if required_tables - _table_names(conn):
            raise WbFbsMappingEvidenceError(
                "mapping_evidence_schema_absent",
                "Required FBS mapping evidence schema is not deployed",
            )
        mapping = conn.execute(
            f"SELECT * FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE mapping_id=?",
            (request["mapping_id"],),
        ).fetchone()
        if mapping is None:
            raise WbFbsMappingEvidenceError(
                "mapping_not_found", "Exact legacy mapping was not found"
            )
        mapping_dict = dict(mapping)
        if not bool(mapping_dict.get("active")):
            raise WbFbsMappingEvidenceError(
                "mapping_inactive", "Exact legacy mapping is not active"
            )
        if (
            int(mapping_dict.get("seller_warehouse_id") or 0)
            != request["seller_warehouse_id"]
            or str(mapping_dict.get("facility_id") or "") != request["facility_id"]
            or str(mapping_dict.get("mapping_digest") or "")
            != request["expected_mapping_digest"]
        ):
            raise WbFbsMappingEvidenceError(
                "mapping_identity_drift", "Exact legacy mapping identity changed"
            )
        seller_rows = conn.execute(
            f"SELECT mapping_id FROM {WAREHOUSE_MAPPINGS_TABLE} "
            "WHERE active=1 AND seller_warehouse_id=? ORDER BY mapping_id",
            (request["seller_warehouse_id"],),
        ).fetchall()
        facility_rows = conn.execute(
            f"SELECT mapping_id FROM {WAREHOUSE_MAPPINGS_TABLE} "
            "WHERE active=1 AND facility_id=? ORDER BY mapping_id",
            (request["facility_id"],),
        ).fetchall()
        if (
            [str(row[0]) for row in seller_rows] != [request["mapping_id"]]
            or [str(row[0]) for row in facility_rows] != [request["mapping_id"]]
        ):
            raise WbFbsMappingEvidenceError(
                "active_mapping_cardinality_conflict",
                "Seller warehouse and facility are not one-to-one",
            )
        facility = conn.execute(
            f"SELECT facility_id,name,active,updated_at FROM {FACILITIES_TABLE} "
            "WHERE facility_id=?",
            (request["facility_id"],),
        ).fetchone()
        if facility is None or not bool(facility["active"]):
            raise WbFbsMappingEvidenceError(
                "facility_missing_or_inactive", "Exact facility is missing or inactive"
            )
        latest = latest_mapping_evidence_version(conn, request["mapping_id"])
        raw_official = _official_image(mapping_dict)
        effective_before = effective_mapping_official_evidence(mapping_dict, latest)
        before_image = _official_image(effective_before)
        if request["mode"] == UPGRADE_MODE:
            if latest is not None:
                raise WbFbsMappingEvidenceError(
                    "mapping_already_versioned",
                    "Legacy mapping already has an official-evidence version",
                )
            if raw_official != {
                "official_office_id": 0,
                "official_warehouse_name": "",
                "official_office_name": "",
                "official_office_city": "",
                "official_evidence_digest": "",
            }:
                raise WbFbsMappingEvidenceError(
                    "mapping_not_exact_legacy_shape",
                    "Mapping is not the exact pre-evidence legacy shape",
                )
            version_kind = "official_evidence_upgrade"
            supersedes_version_id = None
            official_after = {
                "official_office_id": int(live["target"]["office_id"]),
                "official_warehouse_name": str(live["target"]["warehouse_name"]),
                "official_office_name": str(live["target"]["office_name"]),
                "official_office_city": str(live["target"]["office_city"]),
                "official_evidence_digest": str(live["target"]["evidence_digest"]),
            }
        else:
            if (
                latest is None
                or str(latest["version_id"])
                != str(request["restore_from_version_id"])
                or str(latest["version_kind"]) != "official_evidence_upgrade"
            ):
                raise WbFbsMappingEvidenceError(
                    "restore_source_not_latest_upgrade",
                    "Recovery source must be the latest official-evidence upgrade",
                )
            try:
                official_after = _official_image(
                    json.loads(str(latest["before_image_json"]))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise WbFbsMappingEvidenceError(
                    "restore_before_image_invalid",
                    "Stored mapping before-image is invalid",
                ) from exc
            version_kind = "before_image_restore"
            supersedes_version_id = str(latest["version_id"])
        persisted = conn.execute(
            f"""WITH latest_run AS (
                       SELECT * FROM {REGISTRY_RUNS_TABLE}
                        WHERE status IN ('success','partial')
                        ORDER BY run_sequence DESC LIMIT 1
                   )
                   SELECT registry.*,latest_run.completed_at,
                          latest_run.status AS run_status,
                          latest_run.warehouse_count,latest_run.office_count
                     FROM latest_run
                     JOIN {REGISTRY_ROWS_TABLE} registry
                       ON registry.run_id=latest_run.run_id
                    WHERE registry.seller_warehouse_id=?""",
            (request["seller_warehouse_id"],),
        ).fetchone()
        if persisted is None:
            raise WbFbsMappingEvidenceError(
                "official_registry_evidence_missing",
                "Persisted official registry evidence is missing",
            )
        if (
            int(persisted["office_id"]) != request["expected_office_id"]
            or str(persisted["evidence_digest"])
            != str(live["target"]["evidence_digest"])
        ):
            raise WbFbsMappingEvidenceError(
                "persisted_and_live_official_evidence_drift",
                "Persisted and live official warehouse evidence differ",
            )
        registry_row_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM {REGISTRY_ROWS_TABLE} WHERE run_id=?",
                (str(persisted["run_id"]),),
            ).fetchone()[0]
        )
        if (
            registry_row_count != int(persisted["warehouse_count"])
            or registry_row_count != int(live["warehouse_count"])
            or int(persisted["office_count"]) != int(live["office_count"])
        ):
            raise WbFbsMappingEvidenceError(
                "persisted_official_registry_cardinality_drift",
                "Persisted and live official registry cardinality differs",
            )
        observed_now = _parse_timestamp(self._now())
        persisted_at = _parse_timestamp(str(persisted["completed_at"]))
        age_seconds = (observed_now - persisted_at).total_seconds()
        if age_seconds < 0 or age_seconds > MAX_REGISTRY_EVIDENCE_AGE_SECONDS:
            raise WbFbsMappingEvidenceError(
                "official_registry_evidence_stale",
                "Persisted official warehouse evidence is not current",
            )
        prestate = {
            "storage": self.storage_identity,
            "mapping": {
                "mapping_id": request["mapping_id"],
                "seller_warehouse_id": request["seller_warehouse_id"],
                "facility_id": request["facility_id"],
                "mapping_digest": request["expected_mapping_digest"],
                "active": True,
                "raw_official_evidence": raw_official,
                "effective_official_evidence": before_image,
                "latest_version_id": str(latest["version_id"]) if latest else "",
            },
            "facility": {
                "facility_id": str(facility["facility_id"]),
                "name": str(facility["name"]),
                "active": bool(facility["active"]),
                "updated_at": str(facility["updated_at"]),
            },
            "one_to_one": {
                "seller_active_mapping_count": len(seller_rows),
                "facility_active_mapping_count": len(facility_rows),
            },
            "source_registry_run_id": str(persisted["run_id"]),
            "source_official_evidence_digest": str(persisted["evidence_digest"]),
        }
        before_image_digest = _fingerprint(before_image)
        candidate_row = {
            "mapping_id": request["mapping_id"],
            "seller_warehouse_id": request["seller_warehouse_id"],
            "facility_id": request["facility_id"],
            "mapping_digest": request["expected_mapping_digest"],
            "version_kind": version_kind,
            "supersedes_version_id": supersedes_version_id,
            "source_registry_run_id": str(persisted["run_id"]),
            "live_registry_digest": str(live["registry_digest"]),
            **official_after,
            "before_image": before_image,
            "before_image_digest": before_image_digest,
        }
        candidate_sha256 = _fingerprint(candidate_row)
        version_id = "fbsmapver_" + hashlib.sha256(
            f"{operation_id}:{candidate_sha256}".encode("utf-8")
        ).hexdigest()[:30]
        recovery = {
            "kind": "append_only_before_image_reversion",
            "mapping_id": request["mapping_id"],
            "source_version_id": version_id,
            "before_image_sha256": before_image_digest,
            "adapter": "wb_fbs_mapping_evidence_v1",
            "requires_separate_authorization": True,
            "update_or_delete_required": False,
        }
        return {
            "operation_id": operation_id,
            "request": dict(request),
            "version_id": version_id,
            "scope": {
                "mode": request["mode"],
                "mapping_id": request["mapping_id"],
                "seller_warehouse_id": request["seller_warehouse_id"],
                "facility_id": request["facility_id"],
                "official_office_id": int(official_after["official_office_id"]),
                "mapping_row_count": 1,
            },
            "prestate_sha256": _fingerprint(prestate),
            "candidate_sha256": candidate_sha256,
            "candidate_row": candidate_row,
            "recovery": recovery,
            "evidence": {
                "source_registry_run_id": str(persisted["run_id"]),
                "persisted_at": str(persisted["completed_at"]),
                "persisted_age_seconds": int(age_seconds),
                "official_registry_digest": str(live["registry_digest"]),
                "official_evidence_digest": str(
                    live["target"]["evidence_digest"]
                ),
                "official_registry_stable": True,
            },
        }


def _normalize_request(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise WbFbsMappingEvidenceError(
            "request_invalid", "Mapping evidence request must be an object"
        )
    allowed = {
        "mode",
        "mapping_id",
        "seller_warehouse_id",
        "facility_id",
        "expected_mapping_digest",
        "expected_office_id",
        "restore_from_version_id",
    }
    if set(value) - allowed:
        raise WbFbsMappingEvidenceError(
            "request_fields_invalid", "Mapping evidence request has unknown fields"
        )
    mode = str(value.get("mode") or UPGRADE_MODE).strip()
    if mode not in {UPGRADE_MODE, RESTORE_MODE}:
        raise WbFbsMappingEvidenceError(
            "mode_invalid", "Unsupported mapping evidence operation mode"
        )
    request = {
        "mode": mode,
        "mapping_id": _identity(value.get("mapping_id"), "mapping_id"),
        "seller_warehouse_id": _positive_int(
            value.get("seller_warehouse_id"), "seller_warehouse_id"
        ),
        "facility_id": _identity(value.get("facility_id"), "facility_id"),
        "expected_mapping_digest": _digest(
            value.get("expected_mapping_digest"), "expected_mapping_digest"
        ),
        "expected_office_id": _positive_int(
            value.get("expected_office_id"), "expected_office_id"
        ),
        "restore_from_version_id": str(
            value.get("restore_from_version_id") or ""
        ).strip(),
    }
    if mode == RESTORE_MODE:
        request["restore_from_version_id"] = _identity(
            request["restore_from_version_id"], "restore_from_version_id"
        )
    elif request["restore_from_version_id"]:
        raise WbFbsMappingEvidenceError(
            "restore_source_unexpected",
            "Upgrade request must not include a restore source",
        )
    return request


def _stable_official_evidence(
    source: Any, *, seller_warehouse_id: int, expected_office_id: int
) -> dict[str, Any]:
    first = _normalize_official_registry(
        source.list_seller_warehouses(), source.list_offices()
    )
    second = _normalize_official_registry(
        source.list_seller_warehouses(), source.list_offices()
    )
    if not first["complete"] or not second["complete"]:
        raise WbFbsMappingEvidenceError(
            "official_registry_incomplete", "Official WB registry is incomplete"
        )
    if first["registry_digest"] != second["registry_digest"]:
        raise WbFbsMappingEvidenceError(
            "official_registry_unstable", "Official WB registry changed during preflight"
        )
    targets = [
        row
        for row in second["rows"]
        if int(row["seller_warehouse_id"]) == int(seller_warehouse_id)
    ]
    if len(targets) != 1:
        raise WbFbsMappingEvidenceError(
            "official_target_cardinality_invalid",
            "Official WB seller warehouse is missing or duplicated",
        )
    target = targets[0]
    if (
        int(target["office_id"]) != int(expected_office_id)
        or bool(target["is_deleting"])
        or bool(target["is_processing"])
    ):
        raise WbFbsMappingEvidenceError(
            "official_target_identity_invalid",
            "Official WB seller warehouse office or active state differs",
        )
    return {
        "registry_digest": second["registry_digest"],
        "warehouse_count": len(second["rows"]),
        "office_count": second["office_count"],
        "target": target,
    }


def _normalize_official_registry(
    warehouses: list[Any], offices: list[Any]
) -> dict[str, Any]:
    offices_by_id: dict[int, Any] = {}
    duplicate_office_ids: set[int] = set()
    for office in offices:
        office_id = int(office.office_id)
        if office_id in offices_by_id:
            duplicate_office_ids.add(office_id)
        offices_by_id[office_id] = office
    warehouse_ids: set[int] = set()
    rows: list[dict[str, Any]] = []
    complete = not duplicate_office_ids
    for warehouse in warehouses:
        warehouse_id = int(warehouse.warehouse_id)
        if warehouse_id in warehouse_ids:
            complete = False
        warehouse_ids.add(warehouse_id)
        office = offices_by_id.get(int(warehouse.office_id))
        if office is None:
            complete = False
        row = {
            "seller_warehouse_id": warehouse_id,
            "office_id": int(warehouse.office_id),
            "warehouse_name": str(warehouse.name),
            "office_name": str(office.name) if office else "",
            "office_city": str(office.city) if office else "",
            "office_federal_district": str(office.federal_district) if office else "",
            "cargo_type": warehouse.cargo_type,
            "delivery_type": warehouse.delivery_type,
            "is_deleting": bool(warehouse.is_deleting),
            "is_processing": bool(warehouse.is_processing),
        }
        row["evidence_digest"] = _fingerprint(row)
        rows.append(row)
    rows.sort(key=lambda row: int(row["seller_warehouse_id"]))
    material = {
        "warehouses": rows,
        "offices": [
            {
                "office_id": office_id,
                "name": str(offices_by_id[office_id].name),
                "city": str(offices_by_id[office_id].city),
                "federal_district": str(
                    offices_by_id[office_id].federal_district
                ),
            }
            for office_id in sorted(offices_by_id)
        ],
    }
    return {
        "rows": rows,
        "registry_digest": _fingerprint(material),
        "office_count": len(offices_by_id),
        "complete": complete,
    }


def _version_matches_request(
    row: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    expected_version_id: str | None = None,
) -> bool:
    expected_kind = (
        "official_evidence_upgrade"
        if request["mode"] == UPGRADE_MODE
        else "before_image_restore"
    )
    mode_matches = str(row["version_kind"]) == expected_kind
    if request["mode"] == UPGRADE_MODE:
        mode_matches = bool(
            mode_matches
            and not str(row["supersedes_version_id"] or "")
            and int(row["official_office_id"]) == request["expected_office_id"]
        )
    else:
        mode_matches = bool(
            mode_matches
            and str(row["supersedes_version_id"] or "")
            == request["restore_from_version_id"]
        )
    return bool(
        mode_matches
        and str(row["mapping_id"]) == request["mapping_id"]
        and int(row["seller_warehouse_id"]) == request["seller_warehouse_id"]
        and str(row["facility_id"]) == request["facility_id"]
        and str(row["mapping_digest"]) == request["expected_mapping_digest"]
        and (
            expected_version_id is None
            or str(row["version_id"]) == expected_version_id
        )
    )


def _official_image(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "official_office_id": int(value.get("official_office_id") or 0),
        "official_warehouse_name": str(value.get("official_warehouse_name") or ""),
        "official_office_name": str(value.get("official_office_name") or ""),
        "official_office_city": str(value.get("official_office_city") or ""),
        "official_evidence_digest": str(value.get("official_evidence_digest") or ""),
    }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not Path(path).is_file():
        raise WbFbsMappingEvidenceError(
            "operational_store_missing", "Operational store is missing"
        )
    conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _operation_id(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if OPERATION_RE.fullmatch(normalized) is None:
        raise WbFbsMappingEvidenceError(
            "operation_id_invalid", "Operation ID is invalid"
        )
    return normalized


def _identity(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if IDENTITY_RE.fullmatch(normalized) is None:
        raise WbFbsMappingEvidenceError(
            f"{field}_invalid", f"{field} is invalid"
        )
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise WbFbsMappingEvidenceError(f"{field}_invalid", f"{field} is invalid")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WbFbsMappingEvidenceError(
            f"{field}_invalid", f"{field} is invalid"
        ) from exc
    if result <= 0:
        raise WbFbsMappingEvidenceError(f"{field}_invalid", f"{field} is invalid")
    return result


def _digest(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) is None:
        raise WbFbsMappingEvidenceError(f"{field}_invalid", f"{field} is invalid")
    return normalized


def _actor(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    if not normalized or len(normalized) > 160:
        raise WbFbsMappingEvidenceError("actor_invalid", "Actor is invalid")
    return normalized


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise WbFbsMappingEvidenceError(
            "timestamp_invalid", "Official evidence timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None:
        raise WbFbsMappingEvidenceError(
            "timestamp_invalid", "Official evidence timestamp has no timezone"
        )
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
