"""Smoke checks for the exact 41-row Moscow FBS physical-zero mutation."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.ff_pool_cutover import MANIFESTS_TABLE  # noqa: E402
from packages.application.ff_pool_documents import (  # noqa: E402
    DOCUMENT_LINES_TABLE,
    DOCUMENTS_TABLE,
)
from packages.application.ff_pool_fbs_lifecycle import CURRENT_TABLE  # noqa: E402
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    LINES_TABLE,
)
from packages.application.ff_pool_zero_physical_production import (  # noqa: E402
    FfPoolZeroPhysicalProductionError,
    FfPoolZeroPhysicalProductionMutation,
    TARGET_NM_IDS,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)


DEPLOYED_SHA = "a" * 40
NOW = "2026-08-19T12:00:00Z"
FACILITY_ID = "fff_d67e8c823d5f81dd988d00dbfea6"
ORENBURG_FACILITY_ID = "fff_2579bb2741ed4ab23b11bb4c4183"
CUTOVER_ID = "cutover-fixture-20260819"


def main() -> None:
    with TemporaryDirectory(prefix="ff-pool-zero-physical-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        _seed(runtime.db_path)
        mutation = FfPoolZeroPhysicalProductionMutation(
            runtime_dir=runtime_dir,
            deployed_sha=DEPLOYED_SHA,
            timestamp_factory=lambda: NOW,
        )
        before_bytes = _sha256(runtime.db_path)
        plan = mutation.build_plan()
        after_bytes = _sha256(runtime.db_path)
        assert before_bytes == after_bytes
        assert plan["apply_allowed"] is True
        assert plan["scope"] == {
            "facility_id": FACILITY_ID,
            "facility_name": "FF Москва",
            "facility_city": "Москва",
            "pool": "FBS",
            "nm_ids": list(TARGET_NM_IDS),
            "absolute_physical_target": 0,
        }
        assert len(TARGET_NM_IDS) == 41
        assert plan["expected_effects"]["balance_insert_count"] == 41
        assert plan["expected_effects"]["movement_line_count"] == 0
        before_non_target = plan["pre_change"]["non_target_invariants"]
        before_totals = plan["pre_change"]["totals"]

        result = mutation.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="github-pr-1006#issuecomment-apply-gate",
            actor="owner",
            evidence_dir=runtime_dir / "backups" / "ff-pool-zero-physical-production",
        )
        assert result["status"] == "complete"
        assert result["idempotent"] is False
        assert result["recovery"]["lifecycle"] == "retained"
        assert result["recovery"]["undo_artifact"]["state"] == "verified"
        readback = result["readback"]
        assert all(
            item["state"] == "explicit_zero" for item in readback["target_rows"]
        )
        assert [item["reserved"] for item in readback["target_rows"]] == [
            5,
            *([0] * 40),
        ]
        assert [item["available"] for item in readback["target_rows"]] == [
            -5,
            *([0] * 40),
        ]
        assert readback["fbs_status_read_model"]["target_nm_ids_missing"] == []
        assert readback["fbs_status_read_model"]["target_nm_ids_unblocked"] is True
        assert readback["fbs_status_read_model"]["calculation_enabled"] is True
        assert readback["fbs_status_read_model"]["other_active_facility_blockers"]
        assert readback["totals"]["physical"] == before_totals["physical"]
        assert readback["totals"]["reserved"] == 5
        assert readback["fbs_status_read_model"]["available"] == 5
        assert readback["non_target_invariants"] == before_non_target
        _assert_database(runtime.db_path)

        repeated = mutation.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="github-pr-1006#issuecomment-apply-gate",
            actor="owner",
            evidence_dir=runtime_dir / "backups" / "ff-pool-zero-physical-production",
        )
        assert repeated["idempotent"] is True
        _assert_database(runtime.db_path)

        evidence_path = Path(result["evidence_path"])
        evidence_path.unlink()
        recovered = mutation.apply(
            plan,
            fingerprint=plan["fingerprint"],
            approval_reference="github-pr-1006#issuecomment-apply-gate",
            actor="owner",
            evidence_dir=runtime_dir / "backups" / "ff-pool-zero-physical-production",
        )
        assert recovered["idempotent"] is True
        assert recovered["recovered_after_response_loss"] is True
        assert recovered["non_target_invariants_exact_match"] is True
        assert recovered["post_release_concurrent_drift"] == []
        assert evidence_path.is_file()
        _assert_database(runtime.db_path)

        try:
            mutation.apply(
                plan,
                fingerprint=plan["fingerprint"],
                approval_reference="github-pr-1006#wrong-apply-gate",
                actor="owner",
                evidence_dir=runtime_dir / "backups" / "ff-pool-zero-physical-production",
            )
        except FfPoolZeroPhysicalProductionError as exc:
            assert "existing evidence is invalid" in str(exc)
        else:
            raise AssertionError("idempotent retry must preserve the exact apply gate")

    with TemporaryDirectory(prefix="ff-pool-zero-physical-stale-") as directory:
        root = Path(directory)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        _seed(runtime.db_path)
        mutation = FfPoolZeroPhysicalProductionMutation(
            runtime_dir=runtime_dir,
            deployed_sha=DEPLOYED_SHA,
            timestamp_factory=lambda: NOW,
        )
        plan = mutation.build_plan()
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES(?,?,?,1,1,'10','10','unexpected',?)""",
                (FACILITY_ID, "FBS", TARGET_NM_IDS[0], NOW),
            )
            conn.commit()
        try:
            mutation.apply(
                plan,
                fingerprint=plan["fingerprint"],
                approval_reference="github-pr-1006#issuecomment-apply-gate",
                actor="owner",
                evidence_dir=runtime_dir / "backups" / "ff-pool-zero-physical-production",
            )
        except FfPoolZeroPhysicalProductionError as exc:
            assert "changed after" in str(exc)
        else:
            raise AssertionError("stale reviewed zero plan must fail closed")

    with TemporaryDirectory(prefix="ff-pool-zero-physical-existing-") as directory:
        runtime_dir = Path(directory) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime.list_wb_supplies()
        _seed(runtime.db_path)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES(?,?,?,1,0,'0',NULL,'independent-zero',?)""",
                (FACILITY_ID, "FBS", TARGET_NM_IDS[0], NOW),
            )
            conn.commit()
        mutation = FfPoolZeroPhysicalProductionMutation(
            runtime_dir=runtime_dir,
            deployed_sha=DEPLOYED_SHA,
            timestamp_factory=lambda: NOW,
        )
        plan = mutation.build_plan()
        assert plan["apply_allowed"] is False
        assert plan["expected_effects"]["balance_insert_count"] == 40
        assert any("must all still be missing" in item for item in plan["blockers"])
    print("ff_pool_zero_physical_production_smoke: OK")


def _seed(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "INSERT INTO registry_upload_versions VALUES('fixture-v1',?,?)",
            (NOW, NOW),
        )
        conn.execute(
            "INSERT INTO registry_upload_results VALUES('fixture-v1','accepted',3,0,0,'[]',?)",
            (NOW,),
        )
        conn.execute(
            "INSERT INTO registry_upload_current_state VALUES(1,'fixture-v1',?)",
            (NOW,),
        )
        conn.executemany(
            "INSERT INTO registry_upload_config_v2 VALUES('fixture-v1',?,1,?,'fixture',?)",
            [
                (nm_id, f"SKU-{nm_id}", index)
                for index, nm_id in enumerate(TARGET_NM_IDS[:3], start=1)
            ],
        )
        conn.execute(
            f"""INSERT INTO {FACILITIES_TABLE}(
                   facility_id,code,name,active,display_timezone,created_at,updated_at
               ) VALUES(?,?,'FF Москва',1,'Asia/Yekaterinburg',?,?)""",
            (FACILITY_ID, "MSK-PROD", NOW, NOW),
        )
        conn.execute(
            f"INSERT INTO {FACILITY_PROFILES_TABLE} VALUES(?,'Москва','{{}}',?,?)",
            (FACILITY_ID, NOW, NOW),
        )
        conn.execute(
            f"""INSERT INTO {FACILITIES_TABLE}(
                   facility_id,code,name,active,display_timezone,created_at,updated_at
               ) VALUES(?,?,'FF Оренбург',1,'Asia/Yekaterinburg',?,?)""",
            (ORENBURG_FACILITY_ID, "ORENBURG-PROD", NOW, NOW),
        )
        conn.execute(
            f"INSERT INTO {FACILITY_PROFILES_TABLE} VALUES(?,'Оренбург','{{}}',?,?)",
            (ORENBURG_FACILITY_ID, NOW, NOW),
        )
        conn.execute(
            f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
                   epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
               ) VALUES(1,1,1,'fixture-epoch',?,'{{}}')""",
            (NOW,),
        )
        conn.execute(
            f"""INSERT INTO {MANIFESTS_TABLE}(
                   cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,
                   feature_epoch,aggregate_revision,aggregate_digest,detail_digest,
                   observation_watermark_sequence,observation_watermark_digest,
                   mapping_digest,fbw_origins_digest,control_evidence_digest,
                   non_target_digest,opening_document_id,source_snapshot_digest,
                   created_at,manifest_json
               ) VALUES(?,?,?,?,'2026-08-19',1,'agg','agg-d','detail-d',0,
                        'obs-d','map-d','fbw-d','control-d','non-target-d',
                        'opening-fixture','source-d',?,'{{}}')""",
            (CUTOVER_ID, "manifest-fixture", DEPLOYED_SHA, NOW, NOW),
        )
        conn.execute(
            f"""INSERT INTO {BALANCES_TABLE}(
                   facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                   source_watermark,updated_at
               ) VALUES(?, 'FBS', 111, 1, 10, '100', '10', 'opening-fixture', ?)""",
            (FACILITY_ID, NOW),
        )
        conn.execute(
            f"""INSERT INTO {BALANCES_TABLE}(
                   facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,wac_rub,
                   source_watermark,updated_at
               ) VALUES(?, 'FBS', 111, 1, 7, '70', '10', 'orenburg-opening', ?)""",
            (ORENBURG_FACILITY_ID, NOW),
        )
        conn.execute(
            f"""INSERT INTO {CURRENT_TABLE}(
                   cutover_id,order_id,state,episode_sequence,source_revision,status_digest,
                   supplier_status,wb_status,facility_id,pool,nm_id,quantity,
                   frozen_wac_rub,debit_event_id,updated_at
               ) VALUES(?,1001,'reserved',1,'reservation-v1','status-d','new','waiting',
                        ?,'FBS',?,5,'10','',?)""",
            (CUTOVER_ID, FACILITY_ID, TARGET_NM_IDS[0], NOW),
        )
        conn.executemany(
            """INSERT INTO sheet_vitrina_v1_nomenclature_items(
                   item_id,is_active,is_hidden,nm_id,barcode,barcodes_json,
                   barcode_source,barcode_status,vendor_code,nomenclature_name,
                   product_type,match_key,aliases_json,created_at,updated_at
               ) VALUES(?,1,0,?,?,?,'fixture','active',?,?,?,?,'[]',?,?)""",
            [
                (
                    f"item-{nm_id}",
                    nm_id,
                    str(nm_id),
                    f'["{nm_id}"]',
                    f"SKU-{nm_id}",
                    f"SKU-{nm_id}",
                    "fixture",
                    f"SKU-{nm_id}",
                    NOW,
                    NOW,
                )
                for nm_id in TARGET_NM_IDS
            ],
        )
        conn.commit()


def _assert_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        target_rows = conn.execute(
            f"""SELECT nm_id,quantity,capital_rub,wac_rub,source_watermark
                FROM {BALANCES_TABLE}
                WHERE facility_id=? AND pool='FBS'
                  AND nm_id IN ({','.join('?' for _ in TARGET_NM_IDS)})
                ORDER BY nm_id""",
            (FACILITY_ID, *TARGET_NM_IDS),
        ).fetchall()
        assert [row[0] for row in target_rows] == list(TARGET_NM_IDS)
        assert all(row[1] == 0 and row[2] == "0" and row[3] is None for row in target_rows)
        assert len({row[4] for row in target_rows}) == 1
        document_id = str(target_rows[0][4])
        assert conn.execute(
            f"SELECT document_kind FROM {DOCUMENTS_TABLE} WHERE document_id=?",
            (document_id,),
        ).fetchone() == ("pool_inventory",)
        assert conn.execute(
            f"SELECT COUNT(*) FROM {DOCUMENT_LINES_TABLE} WHERE document_id=?",
            (document_id,),
        ).fetchone()[0] == 41
        assert conn.execute(f"SELECT COUNT(*) FROM {LINES_TABLE}").fetchone()[0] == 0
        assert conn.execute(
            f"SELECT quantity FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=111",
            (FACILITY_ID,),
        ).fetchone() == (10,)
        assert conn.execute(
            f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool='FBS' AND nm_id=111",
            (ORENBURG_FACILITY_ID,),
        ).fetchone() == (7, "70", "10")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    main()
