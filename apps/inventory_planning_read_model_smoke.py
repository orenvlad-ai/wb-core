#!/usr/bin/env python3
"""Contract smoke for current WB + signed facility×FBS planning metrics."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.inventory_planning_read_model import (
    FORMULA_VERSION,
    INCIDENT_LINES_TABLE,
    INCIDENT_MANIFESTS_TABLE,
    InventoryPlanningReadModel,
    SELLER_STOCK_LINES_TABLE,
    SELLER_STOCK_READBACKS_TABLE,
)
from packages.application.ff_pool_cutover import MANIFESTS_TABLE
from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE
from packages.application.ff_pool_foundation import (
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.registry_upload_db_backed_runtime import _ensure_schema
from packages.application.warehouse_functional import STAGES, ensure_warehouse_functional_schema
from packages.application.wb_fbs_orders import WAREHOUSE_MAPPINGS_TABLE


NOW = "2026-08-17T08:00:00Z"


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="inventory-planning-smoke-") as raw:
        db_path = Path(raw) / "runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            ensure_warehouse_functional_schema(conn)
            _seed(conn)
            conn.commit()

        model = InventoryPlanningReadModel(db_path=db_path)
        missing = model.current()
        assert _metric(missing, "wb_total") == 30
        assert _metric(missing, "wb_effective_total") is None
        assert missing["wb"]["incident_evidence"]["fail_closed"] is True
        assert missing["wb"]["aggregate_only"] is True
        assert missing["wb"]["districts"] == {
            "available": False,
            "reason_ru": "Недоступно: WB временно не передаёт распределение",
            "historical_values_preserved": True,
        }
        assert _metric(missing, "fbs_total") == 97
        assert _metric(missing, "total") == 127
        assert missing["fbs"]["physical"] == 105
        assert missing["fbs"]["reserved"] == 8
        assert missing["fbs"]["available"] == 97
        assert len(missing["fbs"]["facilities"]) == 2
        assert missing["fbs"]["facilities"][0]["seller_stock"]["delta_to_ledger_physical"] == 1
        missing_skus = {item["nm_id"]: item for item in missing["skus"]}
        assert missing_skus[1]["wb_total"] == 10
        assert missing_skus[1]["fbs_physical"] == 105
        assert missing_skus[1]["fbs_reserved"] == 8
        assert missing_skus[1]["fbs_total"] == 97
        assert missing_skus[1]["total"] == 107
        assert missing_skus[1]["wb_effective_total"] is None
        assert missing_skus[1]["fbs_facilities"][0]["seller_stock"] == {
            "quantity": 6,
            "delta_to_ledger_physical": 1,
            "role": "reconciliation_only",
        }
        assert missing_skus[2]["fbs_total"] == 0
        assert missing_skus[2]["total"] == 20
        assert missing_skus[2]["quality"]["total"] == "partial"
        assert "Частичные данные" in missing_skus[2]["quality"][
            "fbs_total_reason_ru"
        ]
        assert missing["formula"]["version"] == FORMULA_VERSION
        assert missing["formula"]["effective_from"] == "2026-08-16"
        assert missing["formula"]["six_stage_total_changed"] is False
        assert STAGES == (
            "production",
            "china_to_ff",
            "ff",
            "ff_to_wb",
            "wb",
            "wb_acceptance_discrepancy",
        )

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES('map-moscow-ambiguous',101,'orenburg','sha256:map-ambiguous',1,?,'smoke')",
                (NOW,),
            )
            conn.commit()
        ambiguous_seller_mapping = model.current()
        assert ambiguous_seller_mapping["fbs"]["facilities"][0]["seller_stock"][
            "quantity"
        ] is None
        assert ambiguous_seller_mapping["fbs"]["seller_stock_reconciliation"][
            "mapping_quality"
        ] == "ambiguous"

        with sqlite3.connect(db_path) as conn:
            _seed_invalid_incident_evidence(conn)
            conn.commit()
        invalid = model.current()
        assert _metric(invalid, "wb_effective_total") is None
        assert invalid["wb"]["incident_evidence"]["quality"] == (
            "unavailable_exact_incident_evidence_invalid"
        )

        with sqlite3.connect(db_path) as conn:
            _seed_exact_incident_evidence(conn)
            conn.commit()
        exact = model.current()
        assert exact["wb"]["incident_quantity"] == 35
        assert _metric(exact, "wb_effective_total") == -5
        assert _metric(exact, "effective_total") == 92
        assert exact["wb"]["incident_evidence"]["synthetic_cap_applied"] is False
        exact_skus = {item["nm_id"]: item for item in exact["skus"]}
        assert exact_skus[1]["incident_quantity"] == 35
        assert exact_skus[1]["wb_effective_total"] == -25
        assert exact_skus[1]["effective_total"] == 72

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"UPDATE {FACILITIES_TABLE} SET active=1,updated_at=? WHERE facility_id='orenburg'",
                ("2026-08-17T09:00:00Z",),
            )
            conn.commit()
        activated = model.current()
        assert _metric(activated, "fbs_facility:orenburg") == 100
        assert _metric(activated, "fbs_total") == 97
        assert activated["formula"]["effective_from"] == exact["formula"]["effective_from"]

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"UPDATE {FACILITIES_TABLE} SET active=0,updated_at=? WHERE facility_id='moscow'",
                ("2026-08-17T10:00:00Z",),
            )
            conn.commit()
        deactivated = model.current()
        assert _metric(deactivated, "fbs_total") == 97
        assert _metric(deactivated, "fbs_facility:moscow") == -3
        assert next(
            item for item in deactivated["fbs"]["facilities"]
            if item["facility_id"] == "moscow"
        )["applicable"] is True
        assert deactivated["fbs"]["inactive_history_rewritten"] is False

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at) VALUES('empty','FF-EMPTY','Без ledger',1,'Asia/Yekaterinburg',?,?)",
                (NOW, NOW),
            )
            conn.execute(
                f"INSERT INTO {FACILITY_PROFILES_TABLE}(facility_id,city,future_fields_json,created_at,updated_at) VALUES('empty','Москва','{{}}',?,?)",
                (NOW, NOW),
            )
            conn.commit()
        unavailable = model.current()
        assert _metric(unavailable, "fbs_facility:empty") is None
        assert _metric(unavailable, "fbs_total") == 97
        assert _metric(unavailable, "total") == 127
        assert unavailable["quality"]["fbs"] == "exact_ledger"
        empty = next(
            item for item in unavailable["fbs"]["facilities"]
            if item["facility_id"] == "empty"
        )
        assert empty["applicable"] is False
        assert empty["state"] == "inapplicable"

    print("inventory_planning_read_model_smoke: OK")
    return 0


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO sheet_vitrina_v1_warehouse_functional_active(slot,version_id,updated_at) VALUES(1,'whfv-current',?)",
        (NOW,),
    )
    raw_rows = [
        {
            "nmId": 1,
            "warehouseId": -999999,
            "warehouseName": "Склад WB",
            "regionName": "Склад WB",
            "quantity": 10,
        },
        {
            "nmId": 2,
            "warehouseId": -999999,
            "warehouseName": "Склад WB",
            "regionName": "Склад WB",
            "quantity": 20,
        },
    ]
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_warehouse_wb_snapshots(
               snapshot_id,version_id,fetched_at,snapshot_date,requested_nm_ids_json,
               pagination_complete,page_count,page_offsets_json,raw_row_count,raw_rows_digest,
               raw_rows_json,items_json,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "wbsnap-current",
            "whfv-current",
            NOW,
            "2026-08-17",
            "[1,2]",
            1,
            1,
            "[0]",
            2,
            "sha256:wb-current",
            json.dumps(raw_rows, ensure_ascii=False),
            json.dumps([{"nm_id": 1, "quantity": 10}, {"nm_id": 2, "quantity": 20}]),
            NOW,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_incident_policy_revisions(
               seller_id,revision,active,warehouse_ids_json,warehouse_identities_json,
               warehouse_entries_json,reason,effective_from,effective_to,policy_status,
               actor,created_at,source,legacy_payloads_json
           ) VALUES('canonical',1,1,'[507]','[]','[]','incident','2026-08-01','','active','smoke',?,'incident_policy','[]')""",
        (NOW,),
    )
    conn.execute(
        f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(7,1,1,'smoke-epoch',?,'{{}}')",
        (NOW,),
    )
    for facility_id, code, name, city, active in (
        ("moscow", "FF-MOSCOW", "Москва", "Москва", 1),
        ("orenburg", "FF-ORENBURG", "Оренбург", "Оренбург", 0),
    ):
        conn.execute(
            f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (facility_id, code, name, active, "Asia/Yekaterinburg", NOW, NOW),
        )
        conn.execute(
            f"INSERT INTO {FACILITY_PROFILES_TABLE}(facility_id,city,future_fields_json,created_at,updated_at) VALUES(?,?,'{{}}',?,?)",
            (facility_id, city, NOW, NOW),
        )
    for facility_id, quantity in (("moscow", 5), ("orenburg", 100)):
        conn.execute(
            f"""INSERT INTO {BALANCES_TABLE}(
                   facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                   source_watermark,updated_at
               ) VALUES(?,'FBS',1,7,?,'0',NULL,'smoke',?)""",
            (facility_id, quantity, NOW),
        )
    conn.execute(
        f"""INSERT INTO {MANIFESTS_TABLE}(
               cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,feature_epoch,
               aggregate_revision,aggregate_digest,detail_digest,observation_watermark_sequence,
               observation_watermark_digest,mapping_digest,fbw_origins_digest,
               control_evidence_digest,non_target_digest,opening_document_id,
               source_snapshot_digest,created_at,manifest_json
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "cutover-smoke",
            "sha256:manifest",
            "a" * 40,
            "2026-08-16T10:00:00Z",
            "2026-08-16",
            7,
            "whfv-current",
            "sha256:aggregate",
            "sha256:detail",
            1,
            "sha256:watermark",
            "sha256:mapping",
            "sha256:origins",
            "sha256:control",
            "sha256:non-target",
            "opening-smoke",
            "sha256:snapshot",
            NOW,
            "{}",
        ),
    )
    conn.execute(
        f"""INSERT INTO {CURRENT_TABLE}(
               cutover_id,order_id,state,episode_sequence,source_revision,status_digest,
               supplier_status,wb_status,facility_id,pool,nm_id,quantity,frozen_wac_rub,
               debit_event_id,updated_at
           ) VALUES('cutover-smoke',9001,'reserved',1,'sha256:order','sha256:status',
                    'confirm','waiting','moscow','FBS',1,8,'0','',?)""",
        (NOW,),
    )
    conn.execute(
        f"""INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(
               mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by
           ) VALUES('map-moscow',101,'moscow','sha256:map-moscow',1,?,'smoke')""",
        (NOW,),
    )
    conn.execute(
        f"INSERT INTO {SELLER_STOCK_READBACKS_TABLE}(readback_id,seller_id,captured_at,source,source_digest,complete,metadata_json) VALUES('readback-smoke','canonical',?,'official_seller_warehouse_stock','sha256:readback',1,'{{}}')",
        (NOW,),
    )
    conn.execute(
        f"INSERT INTO {SELLER_STOCK_LINES_TABLE}(readback_id,seller_warehouse_id,nm_id,quantity,line_digest) VALUES('readback-smoke',101,1,6,'sha256:readback-line')"
    )


def _seed_exact_incident_evidence(conn: sqlite3.Connection) -> None:
    scope_digest = "sha256:" + hashlib.sha256(b"[1,2]").hexdigest()
    lines = [
        {"nm_id": 1, "incident_quantity": 35},
        {"nm_id": 2, "incident_quantity": 0},
    ]
    material = {
        "seller_id": "canonical",
        "policy_revision": 1,
        "wb_snapshot_id": "wbsnap-current",
        "wb_snapshot_digest": "sha256:wb-current",
        "evidence_date": "2026-08-17",
        "sku_scope_digest": scope_digest,
        "lines": lines,
    }
    evidence_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    conn.execute(
        f"""INSERT INTO {INCIDENT_MANIFESTS_TABLE}(
               evidence_id,seller_id,policy_revision,wb_snapshot_id,wb_snapshot_digest,
               evidence_date,sku_scope_digest,evidence_digest,source,captured_at,metadata_json
           ) VALUES('incident-smoke','canonical',1,'wbsnap-current','sha256:wb-current',
                    '2026-08-17',?,?,'canonical_incident_registry',?,'{{}}')""",
        (scope_digest, evidence_digest, "2026-08-17T09:00:00Z"),
    )
    conn.executemany(
        f"INSERT INTO {INCIDENT_LINES_TABLE}(evidence_id,nm_id,incident_quantity,evidence_digest) VALUES('incident-smoke',?,?,?)",
        ((line["nm_id"], line["incident_quantity"], evidence_digest) for line in lines),
    )


def _seed_invalid_incident_evidence(conn: sqlite3.Connection) -> None:
    scope_digest = "sha256:" + hashlib.sha256(b"[1,2]").hexdigest()
    conn.execute(
        f"""INSERT INTO {INCIDENT_MANIFESTS_TABLE}(
               evidence_id,seller_id,policy_revision,wb_snapshot_id,wb_snapshot_digest,
               evidence_date,sku_scope_digest,evidence_digest,source,captured_at,metadata_json
           ) VALUES('incident-partial','canonical',1,'wbsnap-current','sha256:wb-current',
                    '2026-08-17',?,'sha256:partial','canonical_incident_registry',
                    '2026-08-17T08:10:00Z','{{}}')""",
        (scope_digest,),
    )
    conn.execute(
        f"INSERT INTO {INCIDENT_LINES_TABLE}(evidence_id,nm_id,incident_quantity,evidence_digest) VALUES('incident-partial',1,35,'sha256:partial')"
    )


def _metric(payload: dict[str, object], key: str) -> int | None:
    return next(
        item["value"]
        for item in payload["metrics"]  # type: ignore[index]
        if item["metric_key"] == key
    )


if __name__ == "__main__":
    raise SystemExit(main())
