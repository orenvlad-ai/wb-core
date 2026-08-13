"""Owner-gated Stage 7A facility and FBS shadow production mutation.

The runner intentionally stops before any opening/cutover or physical stock
effect.  Plans are machine-derived from official IDs and current exact
nomenclature; apply accepts only the same reviewed fingerprint.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_CHANGES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_domain_write_guard import EVENTS_TABLE as DOMAIN_EPOCH_EVENTS_TABLE
from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
from packages.application.wb_fbs_orders import (
    BACKFILL_REVIEW_FROM,
    IDENTITY_EVIDENCE_TABLE,
    IDENTITY_MAPPINGS_TABLE,
    OBSERVATIONS_TABLE,
    STATE_TABLE,
    STATUS_OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
    WbFbsOrdersCollector,
    ensure_wb_fbs_orders_schema,
)


CONTRACT_NAME = "ff_stage_7a_production_mutation_v1"
CONTRACT_VERSION = 1
MOSCOW_NAME = "FF Москва"
ORENBURG_NAME = "FF Оренбург"
MOSCOW_CITY = "Москва"
ORENBURG_CITY = "Оренбург"
DISPLAY_TIMEZONE = "Asia/Yekaterinburg"
ENV_KEY = "WB_FBS_COLLECTOR_ENABLED"
ENV_VALUE = "true"
SCHEDULE_UNIT = "wb-core-warehouse-functional-sync.timer"
SCHEDULE_CALENDAR = "*-*-* *:17:00 Europe/Moscow"
SAFE_SHA_RE = re.compile(r"[0-9a-f]{40}")
ALLOWED_FACILITY_NAMES = (MOSCOW_NAME, ORENBURG_NAME)
MOSCOW_OFFICIAL_CITIES = frozenset({"Москва", "Москва_Восток"})
FACILITY_REQUEST_IDS = {
    MOSCOW_NAME: "ff_stage_7a_production_moscow_v1",
    ORENBURG_NAME: "ff_stage_7a_production_orenburg_v1",
}


class Stage7AProductionError(RuntimeError):
    pass


class FfStage7AProductionMutation:
    def __init__(
        self,
        *,
        runtime_dir: Path,
        env_file: Path,
        deployed_sha: str,
        timestamp_factory: Any | None = None,
        unix_time_factory: Any | None = None,
        source: HttpBackedWbFbsOrdersSource | None = None,
    ) -> None:
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir))
        self.env_file = Path(env_file)
        self.deployed_sha = str(deployed_sha).strip()
        if not SAFE_SHA_RE.fullmatch(self.deployed_sha):
            raise Stage7AProductionError("deployed_sha must be an exact 40-hex SHA")
        self.timestamp_factory = timestamp_factory or _utc_now
        self.unix_time_factory = unix_time_factory or _unix_now
        self.source = source or HttpBackedWbFbsOrdersSource()

    def build_plan(self, *, watermark_unix: int | None = None) -> dict[str, Any]:
        now = str(self.timestamp_factory())
        watermark = int(self.unix_time_factory()) if watermark_unix is None else int(watermark_unix)
        official_warehouses = self.source.list_seller_warehouses()
        official_offices = self.source.list_offices()
        official_orders = _fetch_official_orders(
            self.source,
            date_from=_review_start_unix(),
            date_to=watermark,
        )
        with _open_query_only(self.runtime.db_path) as conn:
            baseline = _readback(conn, env_file=self.env_file, now_unix=watermark)
            target_facilities = _facility_plan(conn)
            mappings = _mapping_plan(
                conn,
                official_warehouses=official_warehouses,
                official_offices=official_offices,
                official_orders=official_orders,
                target_facilities=target_facilities,
            )
            non_target = _non_target_snapshot(conn)
            source_digest = _source_digest(
                conn,
                official_warehouses=official_warehouses,
                official_offices=official_offices,
                official_orders=official_orders,
                env_file=self.env_file,
            )
        apply_allowed = not baseline["blockers"] and not mappings["blockers"]
        plan: dict[str, Any] = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "mode": "dry_run",
            "deployed_sha": self.deployed_sha,
            "generated_at": now,
            "review_range_from": BACKFILL_REVIEW_FROM,
            "watermark_unix": watermark,
            "watermark_at": datetime.fromtimestamp(watermark, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
            "facilities": target_facilities,
            "fixed_system_pools": ["FBS", "FBO"],
            "collector_configuration": {
                "environment_key": ENV_KEY,
                "environment_value": ENV_VALUE,
                "env_file": str(self.env_file),
                "schedule_unit": SCHEDULE_UNIT,
                "schedule_on_calendar": SCHEDULE_CALENDAR,
                "polling": True,
                "real_time": False,
            },
            "official_seller_warehouses": [
                {
                    "warehouse_id": item.warehouse_id,
                    "office_id": item.office_id,
                    "name": item.name,
                    "delivery_type": item.delivery_type,
                    "cargo_type": item.cargo_type,
                    "is_deleting": item.is_deleting,
                    "is_processing": item.is_processing,
                }
                for item in official_warehouses
            ],
            "official_offices": [
                {
                    "office_id": item.office_id,
                    "name": item.name,
                    "city": item.city,
                    "federal_district": item.federal_district,
                }
                for item in official_offices
                if item.office_id in {warehouse.office_id for warehouse in official_warehouses}
            ],
            "official_order_preview": {
                "order_count": len(official_orders),
                "earliest_created_at": min((str(item.get("createdAt") or "") for item in official_orders), default=""),
                "latest_created_at": max((str(item.get("createdAt") or "") for item in official_orders), default=""),
                "safe_identity_digest": _fingerprint([_safe_order_identity(item) for item in official_orders]),
            },
            "exact_mappings": mappings,
            "source_digest": source_digest,
            "pre_change": baseline,
            "non_target_invariants": non_target,
            "expected_effects": {
                "facility_insert_count": sum(1 for item in target_facilities if item["action"] == "insert"),
                "facility_noop_count": sum(1 for item in target_facilities if item["action"] == "noop"),
                "warehouse_mapping_insert_count": sum(
                    1 for item in mappings["warehouse"] if item["action"] == "insert"
                ),
                "identity_mapping_insert_count": sum(
                    1 for item in mappings["identity"] if item["action"] == "insert"
                ),
                "fbs_backfill_from": BACKFILL_REVIEW_FROM,
                "fbs_backfill_to_unix": watermark,
                "wb_writes": 0,
                "opening_cutover_writes": 0,
                "physical_stock_writes": 0,
            },
            "backup": {
                "required": True,
                "kind": "exact_target_before_image",
                "env_before_image_required": True,
                "recovery": "forward_reconcile_or_separately_authorized_config_restore",
                "immutable_official_observations_are_retained": True,
                "integrity_check": "sha256_required",
                "mode": "0600",
            },
            "apply_allowed": apply_allowed,
            "blockers": [*baseline["blockers"], *mappings["blockers"]],
        }
        plan["fingerprint"] = _fingerprint({key: value for key, value in plan.items() if key not in {"fingerprint", "generated_at"}})
        return plan

    def apply(
        self,
        reviewed_plan: Mapping[str, Any],
        *,
        fingerprint: str,
        approval_reference: str,
        actor: str,
        backup_dir: Path,
    ) -> dict[str, Any]:
        _validate_reviewed_plan(
            reviewed_plan,
            fingerprint=fingerprint,
            deployed_sha=self.deployed_sha,
            approval_reference=approval_reference,
            actor=actor,
        )
        backup_root = Path(backup_dir)
        if not backup_root.is_absolute():
            raise Stage7AProductionError("backup_dir must be absolute")
        backup_root.mkdir(parents=True, exist_ok=True)
        suffix = fingerprint.removeprefix("sha256:")[:16]
        backup_path = backup_root / f"ff-stage-7a-{suffix}.before.json"
        env_before_path = backup_root / f"ff-stage-7a-{suffix}.env-before"
        evidence_path = backup_root / f"ff-stage-7a-{suffix}.evidence.json"
        if evidence_path.is_file():
            prior = json.loads(evidence_path.read_text(encoding="utf-8"))
            if str(prior.get("fingerprint") or "") != fingerprint:
                raise Stage7AProductionError("existing Stage 7A evidence identity drifted")
            reconciliation = self.readback()
            _verify_reconciliation(
                reviewed_plan={
                    "watermark_unix": prior["catchup"]["watermark_unix"],
                    "non_target_invariants": prior["reconciliation"]["non_target_invariants"],
                    "exact_mappings": prior["reconciliation"]["expected_exact_mappings"],
                },
                reconciliation=reconciliation,
            )
            return {**prior, "idempotent": True, "evidence_path": str(evidence_path)}

        fresh = self.build_plan(watermark_unix=int(reviewed_plan["watermark_unix"]))
        resume = False
        if str(fresh["fingerprint"]) != str(fingerprint):
            resume = _resume_eligible(
                self.runtime.db_path,
                reviewed_plan=reviewed_plan,
                fresh_plan=fresh,
            )
            if not resume:
                raise Stage7AProductionError("Stage 7A production sources changed after dry-run")
        if not fresh["apply_allowed"] and not resume:
            raise Stage7AProductionError("Stage 7A production plan is not apply-eligible")
        with warehouse_functional_write_lock(self.runtime.runtime_dir, timeout_seconds=300):
            fresh_locked = self.build_plan(watermark_unix=int(reviewed_plan["watermark_unix"]))
            if fresh_locked["fingerprint"] != fingerprint:
                if not _resume_eligible(
                    self.runtime.db_path,
                    reviewed_plan=reviewed_plan,
                    fresh_plan=fresh_locked,
                ):
                    raise Stage7AProductionError("Stage 7A production sources changed under writer lock")
                fresh_locked = dict(reviewed_plan)
            backup = _backup_target_state(
                self.runtime.db_path,
                reviewed_plan=fresh_locked,
                destination=backup_path,
            )
            env_backup = _backup_env_file(self.env_file, env_before_path)
            _apply_facilities_and_mappings(
                self.runtime.db_path,
                reviewed_plan=fresh_locked,
                actor=actor,
                approval_reference=approval_reference,
            )
            collector = WbFbsOrdersCollector(
                db_path=self.runtime.db_path,
                timestamp_factory=self.timestamp_factory,
                source=self.source,
                enabled=True,
                unix_time_factory=self.unix_time_factory,
            )
            catchup = collector.collect_catchup(
                date_from=_review_start_unix(),
                date_to=int(fresh_locked["watermark_unix"]),
            )
            next_collection_probe = collector.collect_default_window()
            if (
                next_collection_probe.get("status") != "success"
                or not next_collection_probe.get("complete")
            ):
                raise Stage7AProductionError("next scheduled FBS collection path did not complete")
            env_result = _ensure_env_value(self.env_file, key=ENV_KEY, value=ENV_VALUE)

        reconciliation = self.readback(now_unix=int(self.unix_time_factory()))
        reconciliation["expected_exact_mappings"] = fresh_locked["exact_mappings"]
        _verify_reconciliation(
            reviewed_plan=fresh_locked,
            reconciliation=reconciliation,
        )
        evidence = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "complete",
            "deployed_sha": self.deployed_sha,
            "fingerprint": fingerprint,
            "approval_reference": approval_reference.strip(),
            "actor": actor.strip(),
            "backup": backup,
            "env_before_image": env_backup,
            "env_apply": env_result,
            "catchup": catchup,
            "next_collection_probe": next_collection_probe,
            "reconciliation": reconciliation,
            "completed_at": str(self.timestamp_factory()),
        }
        evidence["evidence_digest"] = _fingerprint({key: value for key, value in evidence.items() if key != "evidence_digest"})
        _write_private_json(evidence_path, evidence)
        return {**evidence, "evidence_path": str(evidence_path)}

    def readback(self, *, now_unix: int | None = None) -> dict[str, Any]:
        current_unix = int(self.unix_time_factory()) if now_unix is None else int(now_unix)
        with _open_query_only(self.runtime.db_path) as conn:
            payload = _readback(conn, env_file=self.env_file, now_unix=current_unix)
        blockers = list(payload["blockers"])
        if len(payload["facilities"]) != 2:
            blockers.append("target facility set is not activated")
        if not payload["collector_configuration"]["enabled"]:
            blockers.append("FBS collector configuration is not enabled")
        state = payload["collector_state"]
        if (
            state["last_status"] != "success"
            or not state["complete"]
            or state["next_cursor"] != 0
        ):
            blockers.append("FBS collector has no complete successful readback")
        payload = {**payload, "blockers": list(dict.fromkeys(blockers))}
        return {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "status": "ready" if not payload["blockers"] else "blocked",
            "deployed_sha": self.deployed_sha,
            **payload,
        }


def _facility_plan(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    specs = (
        (MOSCOW_NAME, MOSCOW_CITY, True),
        (ORENBURG_NAME, ORENBURG_CITY, False),
    )
    result: list[dict[str, Any]] = []
    for name, city, active in specs:
        request_id = FACILITY_REQUEST_IDS[name]
        row = conn.execute(
            f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                       facility.display_timezone,profile.city
                FROM {FACILITIES_TABLE} facility
                LEFT JOIN {FACILITY_PROFILES_TABLE} profile USING(facility_id)
                WHERE facility.name=? ORDER BY facility.facility_id""",
            (name,),
        ).fetchall()
        if len(row) > 1:
            raise Stage7AProductionError(f"duplicate facility display identity: {name}")
        if row:
            item = row[0]
            if (
                bool(item["active"]) != active
                or str(item["city"] or "") != city
                or str(item["display_timezone"]) != DISPLAY_TIMEZONE
            ):
                raise Stage7AProductionError(f"existing facility contract drift: {name}")
            result.append({
                "action": "noop",
                "request_id": request_id,
                "facility_id": str(item["facility_id"]),
                "code": str(item["code"]),
                "name": name,
                "city": city,
                "active": active,
                "display_timezone": DISPLAY_TIMEZONE,
            })
            continue
        identity = _facility_identity(request_id)
        result.append({
            "action": "insert",
            "request_id": request_id,
            "facility_id": identity["facility_id"],
            "code": identity["code"],
            "name": name,
            "city": city,
            "active": active,
            "display_timezone": DISPLAY_TIMEZONE,
        })
    return result


def _mapping_plan(
    conn: sqlite3.Connection,
    *,
    official_warehouses: list[Any],
    official_offices: list[Any],
    official_orders: list[Mapping[str, Any]],
    target_facilities: list[Mapping[str, Any]],
) -> dict[str, Any]:
    blockers: list[str] = []
    facility_by_name = {str(item["name"]): item for item in target_facilities}
    observed_ids: dict[int, int] = {}
    for order in official_orders:
        warehouse_id = int(order.get("warehouseId") or 0)
        if warehouse_id > 0:
            observed_ids[warehouse_id] = observed_ids.get(warehouse_id, 0) + 1
    official_by_id = {int(item.warehouse_id): item for item in official_warehouses}
    offices_by_id = {int(item.office_id): item for item in official_offices}
    warehouse_rows: list[dict[str, Any]] = []
    unrouted_observations = 0
    for warehouse_id in sorted(observed_ids):
        official = official_by_id.get(warehouse_id)
        if official is None or official.is_deleting or official.is_processing:
            unrouted_observations += observed_ids[warehouse_id]
            continue
        office = offices_by_id.get(int(official.office_id))
        if office is None or not _official_city_is_moscow(office):
            unrouted_observations += observed_ids[warehouse_id]
            continue
        facility = facility_by_name[MOSCOW_NAME]
        existing = conn.execute(
            f"""SELECT mapping_id,facility_id FROM {WAREHOUSE_MAPPINGS_TABLE}
                WHERE seller_warehouse_id=? AND active=1 ORDER BY mapping_id""",
            (warehouse_id,),
        ).fetchall()
        action = "insert"
        existing_mapping_id = ""
        if existing:
            targets = {str(row["facility_id"]) for row in existing}
            if len(existing) != 1 or targets != {str(facility["facility_id"])}:
                blockers.append(
                    f"active seller warehouse mapping conflicts for official ID {warehouse_id}"
                )
                continue
            action = "noop"
            existing_mapping_id = str(existing[0]["mapping_id"])
        warehouse_rows.append({
            "action": action,
            "existing_mapping_id": existing_mapping_id,
            "seller_warehouse_id": warehouse_id,
            "official_office_id": int(official.office_id),
            "official_name": str(official.name),
            "official_office_name": str(office.name),
            "official_office_city": str(office.city),
            "observed_order_count": observed_ids[warehouse_id],
            "facility_id": str(facility["facility_id"]),
            "facility_name": MOSCOW_NAME,
            "exact_official_id": True,
        })

    nomenclature = _nomenclature_exact_index(conn)
    identities: dict[tuple[int, int, str, tuple[str, ...]], int] = {}
    for order in official_orders:
        barcodes = tuple(
            dict.fromkeys(
                str(item).strip()
                for item in order.get("skus") or []
                if str(item).strip()
            )
        )
        key = (
            int(order.get("nmId") or 0),
            int(order.get("chrtId") or 0),
            str(order.get("article") or order.get("vendorCode") or "").strip(),
            barcodes,
        )
        identities[key] = identities.get(key, 0) + 1
    identity_rows: list[dict[str, Any]] = []
    unmatched = 0
    deferred = 0
    ambiguous = 0
    for (nm_id, chrt_id, sku, barcode_values), count in sorted(identities.items()):
        barcodes = list(barcode_values)
        barcode = str(barcodes[0]) if len(barcodes) == 1 else ""
        if not chrt_id or not barcode or not sku:
            deferred += count
            continue
        owners = nomenclature.get((nm_id, barcode, sku), [])
        if len(owners) == 1:
            existing = conn.execute(
                f"""SELECT mapping_id,target_nm_id FROM {IDENTITY_MAPPINGS_TABLE}
                    WHERE source_nm_id=? AND source_chrt_id=? AND source_barcode=?
                      AND source_sku=? AND active=1 ORDER BY mapping_id""",
                (nm_id, chrt_id, barcode, sku),
            ).fetchall()
            action = "insert"
            existing_mapping_id = ""
            if existing:
                targets = {int(row["target_nm_id"]) for row in existing}
                if len(existing) != 1 or targets != {int(owners[0]["nm_id"])}:
                    blockers.append(
                        "active SKU mapping conflicts for exact "
                        f"nmId/chrtId/barcode/SKU {nm_id}/{chrt_id}/{barcode}/{sku}"
                    )
                    ambiguous += count
                    continue
                action = "noop"
                existing_mapping_id = str(existing[0]["mapping_id"])
            identity_rows.append({
                "action": action,
                "existing_mapping_id": existing_mapping_id,
                "source_nm_id": nm_id,
                "source_chrt_id": chrt_id,
                "source_barcode": barcode,
                "source_sku": sku,
                "target_nm_id": int(owners[0]["nm_id"]),
                "nomenclature_item_id": str(owners[0]["item_id"]),
                "observation_count": count,
                "exact_identity": True,
            })
        elif len(owners) > 1:
            ambiguous += count
        else:
            unmatched += count
    orenburg_id = str(facility_by_name[ORENBURG_NAME]["facility_id"])
    orenburg_active_mapping_count = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE facility_id=? AND active=1",
            (orenburg_id,),
        ).fetchone()[0]
    )
    if orenburg_active_mapping_count:
        blockers.append("FF Оренбург already has an active seller warehouse mapping")
    return {
        "warehouse": warehouse_rows,
        "identity": identity_rows,
        "unrouted_warehouse_observation_count": unrouted_observations,
        "unmatched_identity_observation_count": unmatched,
        "deferred_identity_observation_count": deferred,
        "ambiguous_identity_observation_count": ambiguous,
        "unrouted_facility_names": [ORENBURG_NAME],
        "blockers": blockers,
    }


def _nomenclature_exact_index(conn: sqlite3.Connection) -> dict[tuple[int, str, str], list[dict[str, Any]]]:
    result: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    rows = conn.execute(
        """SELECT item_id,nm_id,barcode,barcodes_json,vendor_code
           FROM sheet_vitrina_v1_nomenclature_items
           WHERE is_active=1 AND is_hidden=0 AND nm_id IS NOT NULL
           ORDER BY item_id"""
    ).fetchall()
    for row in rows:
        nm_id = int(row["nm_id"])
        sku = str(row["vendor_code"] or "")
        barcodes = {str(row["barcode"] or "").strip(), *[str(item).strip() for item in _json_list(row["barcodes_json"])]}
        for barcode in sorted(item for item in barcodes if item):
            result.setdefault((nm_id, barcode, sku), []).append({"item_id": row["item_id"], "nm_id": nm_id})
    return result


def _apply_facilities_and_mappings(
    db_path: Path,
    *,
    reviewed_plan: Mapping[str, Any],
    actor: str,
    approval_reference: str,
) -> None:
    now = _utc_now()
    with closing(sqlite3.connect(db_path, timeout=120.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_wb_fbs_orders_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        for item in reviewed_plan["facilities"]:
            if item["action"] == "noop":
                continue
            existing = conn.execute(
                f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                           facility.display_timezone,profile.city
                    FROM {FACILITIES_TABLE} facility
                    LEFT JOIN {FACILITY_PROFILES_TABLE} profile USING(facility_id)
                    WHERE facility.facility_id=?""",
                (item["facility_id"],),
            ).fetchone()
            if existing is not None:
                actual = {
                    "facility_id": str(existing["facility_id"]),
                    "code": str(existing["code"]),
                    "name": str(existing["name"]),
                    "city": str(existing["city"] or ""),
                    "active": bool(existing["active"]),
                    "display_timezone": str(existing["display_timezone"]),
                }
                expected = {key: item[key] for key in actual}
                if actual != expected:
                    raise Stage7AProductionError(
                        f"existing facility resume identity drifted: {item['facility_id']}"
                    )
                continue
            conn.execute(
                f"""INSERT INTO {FACILITIES_TABLE}(
                       facility_id,code,name,active,display_timezone,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (item["facility_id"], item["code"], item["name"], int(item["active"]), item["display_timezone"], now, now),
            )
            conn.execute(
                f"""INSERT INTO {FACILITY_PROFILES_TABLE}(
                       facility_id,city,future_fields_json,created_at,updated_at
                   ) VALUES(?,?,'{{}}',?,?)""",
                (item["facility_id"], item["city"], now, now),
            )
            request_id = str(item["request_id"])
            request_identity = _fingerprint(
                {
                    "action": "create",
                    "name": item["name"],
                    "city": item["city"],
                    "display_timezone": item["display_timezone"],
                    "active": item["active"],
                }
            )
            change_id = "fffc_" + hashlib.sha256(f"{request_id}:created:{item['facility_id']}".encode()).hexdigest()[:28]
            conn.execute(
                f"""INSERT INTO {FACILITY_CHANGES_TABLE}(
                       change_id,request_id,request_identity,facility_id,action,actor,
                       previous_json,current_json,changed_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (change_id, request_id, request_identity, item["facility_id"], "created", actor.strip(), "{}", _json(dict(item)), now),
            )
        for item in reviewed_plan["exact_mappings"]["warehouse"]:
            if item.get("action") == "noop":
                continue
            digest = _fingerprint({
                "seller_warehouse_id": item["seller_warehouse_id"],
                "facility_id": item["facility_id"],
                "official_office_id": item["official_office_id"],
            })
            mapping_id = "fbs_wh_" + digest.removeprefix("sha256:")[:32]
            conn.execute(
                f"""INSERT OR IGNORE INTO {WAREHOUSE_MAPPINGS_TABLE}(
                       mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by
                   ) VALUES(?,?,?,?,1,?,?)""",
                (mapping_id, item["seller_warehouse_id"], item["facility_id"], digest, now, actor.strip()),
            )
        for item in reviewed_plan["exact_mappings"]["identity"]:
            if item.get("action") == "noop":
                continue
            digest = _fingerprint({key: item[key] for key in ("source_nm_id", "source_chrt_id", "source_barcode", "source_sku", "target_nm_id")})
            mapping_id = "fbs_sku_" + digest.removeprefix("sha256:")[:32]
            conn.execute(
                f"""INSERT OR IGNORE INTO {IDENTITY_MAPPINGS_TABLE}(
                       mapping_id,source_nm_id,source_chrt_id,source_barcode,source_sku,
                       target_nm_id,mapping_digest,active,created_at,created_by
                   ) VALUES(?,?,?,?,?,?,?,1,?,?)""",
                (mapping_id, item["source_nm_id"], item["source_chrt_id"], item["source_barcode"], item["source_sku"], item["target_nm_id"], digest, now, actor.strip()),
            )
        conn.commit()


def _readback(conn: sqlite3.Connection, *, env_file: Path, now_unix: int) -> dict[str, Any]:
    facilities = [dict(row) | {"active": bool(row["active"])} for row in conn.execute(
        f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                   facility.display_timezone,profile.city
            FROM {FACILITIES_TABLE} facility
            LEFT JOIN {FACILITY_PROFILES_TABLE} profile USING(facility_id)
            WHERE facility.name IN (?,?) ORDER BY facility.name""",
        ALLOWED_FACILITY_NAMES,
    )]
    state = conn.execute(f"SELECT * FROM {STATE_TABLE} WHERE state_id=1").fetchone()
    latest_to = int(state["window_date_to"] or 0) if state else 0
    latest_success = str(state["last_success_at"] or "") if state else ""
    latest_status = str(state["last_status"] or "") if state else ""
    last_error = str(state["last_error"] or "") if state else ""
    outcomes = {str(row["outcome"]): int(row["count"]) for row in conn.execute(
        f"SELECT outcome,COUNT(*) AS count FROM {IDENTITY_EVIDENCE_TABLE} GROUP BY outcome"
    )}
    earliest = conn.execute(f"SELECT MIN(source_created_at) FROM {OBSERVATIONS_TABLE} WHERE source_created_at<>''").fetchone()[0]
    latest = conn.execute(f"SELECT MAX(source_created_at) FROM {OBSERVATIONS_TABLE} WHERE source_created_at<>''").fetchone()[0]
    blockers: list[str] = []
    expected = {MOSCOW_NAME: (True, MOSCOW_CITY), ORENBURG_NAME: (False, ORENBURG_CITY)}
    if len(facilities) not in {0, 2}:
        blockers.append("target facility set is partial")
    for item in facilities:
        active, city = expected[str(item["name"])]
        if bool(item["active"]) != active or str(item["city"] or "") != city or str(item["display_timezone"]) != DISPLAY_TIMEZONE:
            blockers.append(f"facility contract mismatch: {item['name']}")
    if latest_status == "failed" or last_error:
        blockers.append("FBS collector state reports an error")
    return {
        "facilities": facilities,
        "collector_configuration": {
            "enabled": _env_value(env_file, ENV_KEY).casefold() in {"1", "true", "yes", "on"},
            "environment_key": ENV_KEY,
            "polling_schedule": SCHEDULE_CALENDAR,
            "period_seconds": 3600,
            "slo": "next successful hourly warehouse sync",
            "real_time": False,
        },
        "collector_state": {
            "last_status": latest_status,
            "last_success_at": latest_success,
            "last_error": last_error,
            "window_date_to": latest_to,
            "next_cursor": int(state["next_cursor"] or 0) if state else 0,
            "complete": bool(state["complete"]) if state else False,
            "latest_watermark_lag_seconds": max(0, now_unix - latest_to) if latest_to else None,
        },
        "official_orders": {
            "observation_count": _count(conn, OBSERVATIONS_TABLE),
            "status_observation_count": _count(conn, STATUS_OBSERVATIONS_TABLE),
            "earliest_official_order_date": str(earliest or "")[:10],
            "latest_official_order_at": str(latest or ""),
        },
        "mappings": {
            "warehouse_mapping_count": _count(conn, WAREHOUSE_MAPPINGS_TABLE),
            "identity_mapping_count": _count(conn, IDENTITY_MAPPINGS_TABLE),
            "active_warehouse": [dict(row) for row in conn.execute(
                f"""SELECT mapping_id,seller_warehouse_id,facility_id,mapping_digest
                    FROM {WAREHOUSE_MAPPINGS_TABLE} WHERE active=1
                    ORDER BY seller_warehouse_id,mapping_id"""
            )],
            "active_identity": [dict(row) for row in conn.execute(
                f"""SELECT mapping_id,source_nm_id,source_chrt_id,source_barcode,
                           source_sku,target_nm_id,mapping_digest
                    FROM {IDENTITY_MAPPINGS_TABLE} WHERE active=1
                    ORDER BY source_nm_id,source_chrt_id,source_barcode,source_sku,mapping_id"""
            )],
            "outcomes": outcomes,
            "unmatched_count": int(outcomes.get("unmatched_warehouse", 0)) + int(outcomes.get("unmatched_identity", 0)),
            "deferred_count": int(outcomes.get("deferred", 0)),
            "matched_count": int(outcomes.get("matched", 0)),
        },
        "non_target_invariants": _non_target_snapshot(conn),
        "blockers": blockers,
        "query_only": True,
    }


def _non_target_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    aggregate = {}
    if "sheet_vitrina_v1_warehouse_functional_active" in tables:
        active = conn.execute("SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active WHERE slot=1").fetchone()
        version_id = str(active[0]) if active else ""
        row = conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(CAST(quantity AS NUMERIC)),0),
                      COALESCE(SUM(CAST(capital_rub AS NUMERIC)),0)
               FROM sheet_vitrina_v1_warehouse_functional_balances
               WHERE version_id=? AND warehouse_key='ff'""",
            (version_id,),
        ).fetchone()
        aggregate_rows = [
            dict(item)
            for item in conn.execute(
                """SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances
                   WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id""",
                (version_id,),
            )
        ]
        aggregate = {
            "version_id": version_id,
            "row_count": int(row[0]),
            "quantity": str(row[1]),
            "capital_rub": str(row[2]),
            "row_digest": _fingerprint(aggregate_rows),
        }
    actual_acceptance = conn.execute(
        """SELECT COUNT(*) FROM sheet_vitrina_v1_supplier_shipments
           WHERE COALESCE(actual_ff_acceptance_date,'')<>''"""
    ).fetchone()[0]
    actual_acceptance_rows = [
        dict(row)
        for row in conn.execute(
            """SELECT shipment_id,actual_ff_acceptance_date
               FROM sheet_vitrina_v1_supplier_shipments
               WHERE COALESCE(actual_ff_acceptance_date,'')<>''
               ORDER BY shipment_id"""
        )
    ]
    return {
        "aggregate_ff": aggregate,
        "feature_epoch_count": _count(conn, FEATURE_EPOCHS_TABLE),
        "pool_balance_count": _count(conn, BALANCES_TABLE),
        "pool_operation_count": _count(conn, OPERATIONS_TABLE),
        "pool_movement_line_count": _count(conn, LINES_TABLE),
        "cutover_manifest_count": _count_if_present(conn, "sheet_vitrina_v1_ff_pool_cutover_manifests"),
        "cutover_checkpoint_count": _count_if_present(conn, "sheet_vitrina_v1_ff_pool_cutover_checkpoints"),
        "opening_reservation_count": _count_if_present(conn, "sheet_vitrina_v1_ff_pool_cutover_opening_reservations"),
        "ff_stock_operation_count": _count_if_present(conn, "sheet_vitrina_v1_ff_stock_operations"),
        "ff_stock_reservation_operation_count": _count_if_present(conn, "sheet_vitrina_v1_ff_stock_reservation_operations"),
        "actual_ff_acceptance_count": int(actual_acceptance),
        "actual_ff_acceptance_digest": _fingerprint(actual_acceptance_rows),
        "domain_epoch_event_count": _count_if_present(conn, DOMAIN_EPOCH_EVENTS_TABLE),
        "wb_mutation_count": 0,
    }


def _source_digest(
    conn: sqlite3.Connection,
    *,
    official_warehouses: list[Any],
    official_offices: list[Any],
    official_orders: list[Mapping[str, Any]],
    env_file: Path,
) -> str:
    return _fingerprint({
        "facilities": [dict(row) for row in conn.execute(f"SELECT * FROM {FACILITIES_TABLE} ORDER BY facility_id")],
        "profiles": [dict(row) for row in conn.execute(f"SELECT * FROM {FACILITY_PROFILES_TABLE} ORDER BY facility_id")],
        "observations": [dict(row) for row in conn.execute(f"SELECT order_id,source_revision,warehouse_id,nm_id,chrt_id,seller_sku,skus_json FROM {OBSERVATIONS_TABLE} ORDER BY observation_sequence")],
        "nomenclature": [dict(row) for row in conn.execute("SELECT item_id,is_active,is_hidden,nm_id,barcode,barcodes_json,vendor_code,updated_at FROM sheet_vitrina_v1_nomenclature_items ORDER BY item_id")],
        "official_warehouses": [item.__dict__ for item in official_warehouses],
        "official_offices": [item.__dict__ for item in official_offices],
        "official_orders": [_safe_order_identity(item) for item in official_orders],
        "env_enabled": _env_value(env_file, ENV_KEY),
    })


def _fetch_official_orders(
    source: HttpBackedWbFbsOrdersSource,
    *,
    date_from: int,
    date_to: int,
) -> list[Mapping[str, Any]]:
    cursor = 0
    seen = {0}
    rows: list[Mapping[str, Any]] = []
    for _ in range(50):
        page = source.list_orders(
            limit=1000,
            next_cursor=cursor,
            date_from=date_from,
            date_to=date_to,
        )
        rows.extend(
            item
            for item in page.orders
            if _collectable_official_order(item)
        )
        if page.next_cursor == 0:
            return rows
        if page.next_cursor in seen:
            raise Stage7AProductionError("official FBS dry-run cursor did not advance")
        cursor = int(page.next_cursor)
        seen.add(cursor)
    raise Stage7AProductionError("official FBS dry-run exceeded the 50-page bound")


def _safe_order_identity(order: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(order.get("id") or 0),
        "supply_id": str(order.get("supplyId") or order.get("supplyID") or ""),
        "created_at": str(order.get("createdAt") or ""),
        "warehouse_id": int(order.get("warehouseId") or 0),
        "office_id": int(order.get("officeId") or 0),
        "nm_id": int(order.get("nmId") or 0),
        "chrt_id": int(order.get("chrtId") or 0),
        "article": str(order.get("article") or order.get("vendorCode") or "").strip(),
        "skus": list(
            dict.fromkeys(
                str(item).strip()
                for item in order.get("skus") or []
                if str(item).strip()
            )
        ),
        "delivery_type": str(order.get("deliveryType") or ""),
        "cargo_type": order.get("cargoType"),
        "cross_border_type": order.get("crossBorderType"),
        "is_zero_order": order.get("isZeroOrder") is True,
    }


def _collectable_official_order(order: Mapping[str, Any]) -> bool:
    if (
        str(order.get("deliveryType") or order.get("delivery_type") or "")
        .strip()
        .casefold()
        != "fbs"
    ):
        return False
    try:
        return int(order.get("id") or 0) > 0 and int(order.get("nmId") or 0) > 0
    except (TypeError, ValueError):
        return False


def _official_city_is_moscow(office: Any) -> bool:
    return str(office.city or "").strip() in MOSCOW_OFFICIAL_CITIES


def _verify_reconciliation(*, reviewed_plan: Mapping[str, Any], reconciliation: Mapping[str, Any]) -> None:
    if reconciliation.get("status") != "ready":
        raise Stage7AProductionError("Stage 7A query-only reconciliation is blocked")
    facilities = {str(item["name"]): item for item in reconciliation["facilities"]}
    if set(facilities) != set(ALLOWED_FACILITY_NAMES):
        raise Stage7AProductionError("Stage 7A facility readback is incomplete")
    if not reconciliation["collector_configuration"]["enabled"]:
        raise Stage7AProductionError("FBS collector configuration did not read back enabled")
    state = reconciliation["collector_state"]
    if state["last_status"] != "success" or not state["complete"] or state["next_cursor"] != 0:
        raise Stage7AProductionError("FBS catch-up did not reconcile complete")
    if int(state["window_date_to"] or 0) < int(reviewed_plan["watermark_unix"]):
        raise Stage7AProductionError("FBS catch-up watermark is behind the reviewed plan")
    expected = reviewed_plan["exact_mappings"]
    actual_warehouse = reconciliation["mappings"]["active_warehouse"]
    for item in expected["warehouse"]:
        rows = [
            row
            for row in actual_warehouse
            if int(row["seller_warehouse_id"]) == int(item["seller_warehouse_id"])
        ]
        if len(rows) != 1 or str(rows[0]["facility_id"]) != str(item["facility_id"]):
            raise Stage7AProductionError("exact seller warehouse mapping did not reconcile")
    actual_identity = reconciliation["mappings"]["active_identity"]
    for item in expected["identity"]:
        rows = [
            row
            for row in actual_identity
            if (
                int(row["source_nm_id"]) == int(item["source_nm_id"])
                and int(row["source_chrt_id"]) == int(item["source_chrt_id"])
                and str(row["source_barcode"]) == str(item["source_barcode"])
                and str(row["source_sku"]) == str(item["source_sku"])
            )
        ]
        if len(rows) != 1 or int(rows[0]["target_nm_id"]) != int(item["target_nm_id"]):
            raise Stage7AProductionError("exact SKU identity mapping did not reconcile")
    if reconciliation["non_target_invariants"] != reviewed_plan["non_target_invariants"]:
        raise Stage7AProductionError("Stage 7A non-target invariants changed")


def _resume_eligible(
    db_path: Path,
    *,
    reviewed_plan: Mapping[str, Any],
    fresh_plan: Mapping[str, Any],
) -> bool:
    """Allow only the exact interrupted target cohort to resume idempotently."""

    if (
        fresh_plan.get("official_order_preview", {}).get("safe_identity_digest")
        != reviewed_plan.get("official_order_preview", {}).get("safe_identity_digest")
        or fresh_plan.get("official_order_preview", {}).get("order_count")
        != reviewed_plan.get("official_order_preview", {}).get("order_count")
        or fresh_plan.get("non_target_invariants")
        != reviewed_plan.get("non_target_invariants")
    ):
        return False
    expected_facilities = {
        str(item["facility_id"]): {
            "facility_id": str(item["facility_id"]),
            "code": str(item["code"]),
            "name": str(item["name"]),
            "city": str(item["city"]),
            "active": bool(item["active"]),
            "display_timezone": str(item["display_timezone"]),
        }
        for item in reviewed_plan.get("facilities") or []
    }
    with _open_query_only(db_path) as conn:
        actual = {
            str(row["facility_id"]): {
                "facility_id": str(row["facility_id"]),
                "code": str(row["code"]),
                "name": str(row["name"]),
                "city": str(row["city"] or ""),
                "active": bool(row["active"]),
                "display_timezone": str(row["display_timezone"]),
            }
            for row in conn.execute(
                f"""SELECT facility.facility_id,facility.code,facility.name,facility.active,
                           facility.display_timezone,profile.city
                    FROM {FACILITIES_TABLE} facility
                    LEFT JOIN {FACILITY_PROFILES_TABLE} profile USING(facility_id)
                    WHERE facility.name IN (?,?)""",
                ALLOWED_FACILITY_NAMES,
            )
        }
        if actual and actual != expected_facilities:
            return False
        for item in reviewed_plan.get("exact_mappings", {}).get("warehouse") or []:
            rows = conn.execute(
                f"""SELECT facility_id FROM {WAREHOUSE_MAPPINGS_TABLE}
                    WHERE seller_warehouse_id=? AND active=1""",
                (int(item["seller_warehouse_id"]),),
            ).fetchall()
            if rows and {str(row[0]) for row in rows} != {str(item["facility_id"])}:
                return False
        for item in reviewed_plan.get("exact_mappings", {}).get("identity") or []:
            rows = conn.execute(
                f"""SELECT target_nm_id FROM {IDENTITY_MAPPINGS_TABLE}
                    WHERE source_nm_id=? AND source_chrt_id=? AND source_barcode=?
                      AND source_sku=? AND active=1""",
                (item["source_nm_id"], item["source_chrt_id"], item["source_barcode"], item["source_sku"]),
            ).fetchall()
            if rows and {int(row[0]) for row in rows} != {int(item["target_nm_id"])}:
                return False
    return True


def _validate_reviewed_plan(
    plan: Mapping[str, Any], *, fingerprint: str, deployed_sha: str, approval_reference: str, actor: str
) -> None:
    if plan.get("contract_name") != CONTRACT_NAME or int(plan.get("contract_version") or 0) != CONTRACT_VERSION:
        raise Stage7AProductionError("reviewed plan contract is invalid")
    if plan.get("mode") != "dry_run" or not plan.get("apply_allowed"):
        raise Stage7AProductionError("reviewed plan is not apply-eligible")
    if str(plan.get("deployed_sha") or "") != deployed_sha:
        raise Stage7AProductionError("reviewed plan deployed SHA does not match")
    if str(plan.get("fingerprint") or "") != fingerprint:
        raise Stage7AProductionError("reviewed plan fingerprint does not match")
    recomputed = _fingerprint(
        {
            key: value
            for key, value in plan.items()
            if key not in {"fingerprint", "generated_at"}
        }
    )
    if recomputed != fingerprint:
        raise Stage7AProductionError("reviewed plan content does not match its fingerprint")
    if plan.get("fixed_system_pools") != ["FBS", "FBO"]:
        raise Stage7AProductionError("reviewed plan changed the fixed system pools")
    effects = plan.get("expected_effects") or {}
    if any(
        int(effects.get(key) or 0) != 0
        for key in ("wb_writes", "opening_cutover_writes", "physical_stock_writes")
    ):
        raise Stage7AProductionError("reviewed plan escapes the Stage 7A hard boundary")
    if not approval_reference.strip() or not actor.strip():
        raise Stage7AProductionError("approval_reference and actor are required")


def _facility_identity(request_id: str) -> dict[str, str]:
    digest = _fingerprint(
        {"request_id": request_id, "purpose": "facility_identity"}
    ).removeprefix("sha256:")
    return {"facility_id": "fff_" + digest[:28], "code": "FF-" + digest[:10].upper()}


def _open_query_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _backup_target_state(
    db_path: Path,
    *,
    reviewed_plan: Mapping[str, Any],
    destination: Path,
) -> dict[str, Any]:
    if destination.exists():
        try:
            existing = json.loads(destination.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise Stage7AProductionError("existing Stage 7A before-image is invalid") from exc
        if (
            not isinstance(existing, Mapping)
            or existing.get("contract_name") != CONTRACT_NAME
            or int(existing.get("contract_version") or 0) != CONTRACT_VERSION
            or existing.get("kind") != "exact_target_before_image"
            or str(existing.get("fingerprint") or "") != str(reviewed_plan["fingerprint"])
        ):
            raise Stage7AProductionError("existing Stage 7A before-image identity drifted")
        recovery_contract = dict(existing.get("recovery_contract") or {})
        return {
            "kind": "exact_target_before_image",
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
            "integrity_check": "sha256_verified",
            "resumed": True,
            "recovery_contract": recovery_contract,
        }
    target_ids = [str(item["facility_id"]) for item in reviewed_plan["facilities"]]
    warehouse_ids = [
        int(item["seller_warehouse_id"])
        for item in reviewed_plan["exact_mappings"]["warehouse"]
    ]
    identities = list(reviewed_plan["exact_mappings"]["identity"])
    with _open_query_only(db_path) as conn:
        payload = {
            "contract_name": CONTRACT_NAME,
            "contract_version": CONTRACT_VERSION,
            "fingerprint": str(reviewed_plan["fingerprint"]),
            "kind": "exact_target_before_image",
            "facilities": _rows_for_values(
                conn, FACILITIES_TABLE, "facility_id", target_ids, order_by="facility_id"
            ),
            "facility_profiles": _rows_for_values(
                conn, FACILITY_PROFILES_TABLE, "facility_id", target_ids, order_by="facility_id"
            ),
            "facility_changes": _rows_for_values(
                conn, FACILITY_CHANGES_TABLE, "facility_id", target_ids, order_by="change_id"
            ),
            "warehouse_mappings": _rows_for_values(
                conn,
                WAREHOUSE_MAPPINGS_TABLE,
                "seller_warehouse_id",
                warehouse_ids,
                order_by="mapping_id",
            ),
            "identity_mappings": [
                dict(row)
                for item in identities
                for row in conn.execute(
                    f"""SELECT * FROM {IDENTITY_MAPPINGS_TABLE}
                        WHERE source_nm_id=? AND source_chrt_id=? AND source_barcode=?
                          AND source_sku=? ORDER BY mapping_id""",
                    (
                        item["source_nm_id"],
                        item["source_chrt_id"],
                        item["source_barcode"],
                        item["source_sku"],
                    ),
                )
            ],
            "collector_state": [
                dict(row) for row in conn.execute(f"SELECT * FROM {STATE_TABLE} ORDER BY state_id")
            ],
            "non_target_invariants": _non_target_snapshot(conn),
            "recovery_contract": {
                "automatic_destructive_rollback": False,
                "immutable_official_observations_are_retained": True,
                "configuration_restore_requires_separate_owner_authorization": True,
                "forward_reconciliation_supported": True,
            },
        }
    _write_private_json(destination, payload)
    return {
        "kind": "exact_target_before_image",
        "path": str(destination),
        "size_bytes": destination.stat().st_size,
        "sha256": _sha256_file(destination),
        "integrity_check": "sha256_verified",
        "resumed": False,
        "recovery_contract": payload["recovery_contract"],
    }


def _rows_for_values(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: list[Any],
    *,
    order_by: str,
) -> list[dict[str, Any]]:
    if not values:
        return []
    placeholders = ",".join("?" for _ in values)
    return [
        dict(row)
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE {column} IN ({placeholders}) ORDER BY {order_by}",
            tuple(values),
        )
    ]


def _backup_env_file(source: Path, destination: Path) -> dict[str, Any]:
    if destination.exists():
        return {"path": str(destination), "sha256": _sha256_file(destination), "resumed": True}
    data = source.read_bytes()
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return {"path": str(destination), "sha256": _sha256_file(destination), "resumed": False}


def _ensure_env_value(path: Path, *, key: str, value: str) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    found = 0
    for line in lines:
        if line.startswith(f"{key}="):
            found += 1
            output.append(f"{key}={value}")
        else:
            output.append(line)
    if found > 1:
        raise Stage7AProductionError(f"duplicate {key} entries in environment file")
    if not found:
        output.append(f"{key}={value}")
    content = ("\n".join(output).rstrip("\n") + "\n").encode("utf-8")
    if path.read_bytes() == content:
        return {"changed": False, "key": key, "configured": True}
    temp = path.with_name(path.name + f".ff-stage-7a.{os.getpid()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)
    return {"changed": True, "key": key, "configured": True}


def _env_value(path: Path, key: str) -> str:
    if not path.is_file():
        return ""
    values = [line.split("=", 1)[1].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.startswith(f"{key}=")]
    if len(values) > 1:
        raise Stage7AProductionError(f"duplicate {key} entries in environment file")
    return values[0] if values else ""


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _count_if_present(conn: sqlite3.Connection, table: str) -> int:
    present = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return _count(conn, table) if present else 0


def _json_list(value: Any) -> list[Any]:
    try:
        result = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return result if isinstance(result, list) else []


def _review_start_unix() -> int:
    return int(datetime.fromisoformat(BACKFILL_REVIEW_FROM).replace(tzinfo=timezone.utc).timestamp())


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_private_json(path: Path, value: Any) -> None:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n").encode("utf-8")
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unix_now() -> int:
    return int(datetime.now(timezone.utc).timestamp())
