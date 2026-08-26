#!/usr/bin/env python3
"""Acceptance smoke for applicability-gated dense FBS initialization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_dense_fbs import (  # noqa: E402
    ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS,
    ORENBURG_FACILITY_ID,
    ORENBURG_TARGET_NM_IDS,
)
from packages.application.ff_pool_dense_fbs import (  # noqa: E402
    DenseFbsError,
    DenseFbsService,
)
from packages.application.ff_pool_documents import (  # noqa: E402
    DOCUMENT_LINES_TABLE,
    DOCUMENTS_TABLE,
    LINES_TABLE,
    FfPoolDocumentError,
    FfPoolDocumentService,
    _apply_balance_movement,
)
from packages.application.ff_pool_fbs_applicability import (  # noqa: E402
    APPLICABILITY_EVENTS_TABLE,
    DENSE_INTENT_EVENTS_TABLE,
    DENSE_INTENTS_TABLE,
    FbsApplicabilityError,
    fbs_physical_component,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


NOW = "2026-08-26T08:00:00Z"
TODAY = "2026-08-26"


class _AmbiguousAfterCommit:
    """Commit once, then emulate a lost transport response."""

    def __init__(self, **kwargs: Any) -> None:
        self._delegate = FfPoolDocumentService(**kwargs)

    def post(self, request_id: str) -> dict[str, Any]:
        self._delegate.post(request_id)
        raise ConnectionError("simulated response loss after commit")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


def main() -> int:
    lifecycle = _lifecycle_contract()
    repair = _orenburg_repair_contract()
    benchmark = _production_shaped_benchmark()
    print(
        json.dumps(
            {
                "status": "ok",
                "lifecycle": lifecycle,
                "orenburg_repair": repair,
                "benchmark": benchmark,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _lifecycle_contract() -> dict[str, Any]:
    with TemporaryDirectory(prefix="dense-fbs-lifecycle-") as raw:
        runtime_dir = Path(raw) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)

        # A first SKU can be published before any facility exists.  No physical
        # pair can be inferred or materialized in that empty dimension.
        first = runtime.save_nomenclature_item(_sku(101, updated_at=NOW))
        assert first["is_active"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            assert _count(conn, BALANCES_TABLE) == 0
            _enable_writer(conn)
            conn.commit()

        surface = FfPoolSurface(db_path=runtime.db_path, runtime_dir=runtime_dir)
        non_target_before = _non_fbs_digest(runtime.db_path)
        moscow = surface.create_facility(
            {
                "request_id": "dense-smoke-facility-moscow",
                "name": "Москва Dense",
                "city": "Москва",
                "active": True,
            },
            actor="dense-smoke",
        )
        facility_id = str(moscow["facility"]["facility_id"])
        assert moscow["facility"]["active"] is True
        _assert_zero(runtime.db_path, facility_id, 101)
        assert _non_fbs_digest(runtime.db_path) == non_target_before
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            assert _count(conn, LINES_TABLE) == 0
            assert _count(conn, DOCUMENTS_TABLE) == 1
            assert _count(conn, DENSE_INTENTS_TABLE) == 2  # first SKU + facility
            assert conn.execute(
                f"SELECT COUNT(*) FROM {DENSE_INTENT_EVENTS_TABLE} WHERE state='active'"
            ).fetchone()[0] == 2
            line = conn.execute(
                f"SELECT quantity,capital_rub,metadata_json FROM {DOCUMENT_LINES_TABLE} "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                (facility_id,),
            ).fetchone()
            assert int(line[0]) == 0 and Decimal(str(line[1])) == 0
            assert json.loads(str(line[2]))["explicit_physical_zero"] is True

        # New active SKU is staged inactive, materialized across every active
        # FBS facility, read back, and only then published active.
        second = runtime.save_nomenclature_item(
            _sku(202, updated_at="2026-08-26T08:01:00Z")
        )
        assert second["is_active"] is True
        _assert_zero(runtime.db_path, facility_id, 202)

        # Archive/reactivation keeps the physical history and does not emit a
        # movement.  Existing non-zero quantity/capital/WAC are retained.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {BALANCES_TABLE} SET quantity=7,capital_rub='70',wac_rub='10',"
                "source_watermark='forward-receipt-fixture',updated_at=? "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=202",
                ("2026-08-26T08:02:00Z", facility_id),
            )
            movement_count = _count(conn, LINES_TABLE)
            conn.commit()
        runtime.delete_nomenclature_item(
            str(second["item_id"]), updated_at="2026-08-26T08:03:00Z"
        )
        archived = runtime.load_nomenclature_item(str(second["item_id"]))
        assert archived is not None and archived["is_active"] is False
        reactivated = runtime.save_nomenclature_item(
            _sku(202, updated_at="2026-08-26T08:04:00Z")
        )
        assert reactivated["is_active"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            retained = conn.execute(
                f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=202",
                (facility_id,),
            ).fetchone()
            assert tuple(retained) == (7, "70", "10")
            assert _count(conn, LINES_TABLE) == movement_count

        # Default applicability can only be overridden by dated immutable
        # evidence.  Reinstatement reuses the retained exact zero row.
        applicability_service = DenseFbsService(
            db_path=runtime.db_path, runtime_dir=runtime_dir
        )
        inapplicable = applicability_service.record_applicability(
            facility_id=facility_id,
            nm_id=101,
            state="inapplicable",
            effective_from=TODAY,
            reason="SKU is not physically handled at this facility",
            provenance={"ticket": "dense-smoke-1"},
            actor="dense-smoke",
        )
        repeated_inapplicable = applicability_service.record_applicability(
            facility_id=facility_id,
            nm_id=101,
            state="inapplicable",
            effective_from=TODAY,
            reason="SKU is not physically handled at this facility",
            provenance={"ticket": "dense-smoke-1"},
            actor="dense-smoke",
        )
        assert repeated_inapplicable["event_id"] == inapplicable["event_id"]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            typed = fbs_physical_component(
                conn,
                facility_id=facility_id,
                nm_id=101,
                as_of_date=TODAY,
                projection_epoch=1,
            )
            assert typed["state"] == "inapplicable"
            assert typed["provenance"]["event_id"] == inapplicable["event_id"]
            try:
                _movement(
                    conn,
                    facility_id=facility_id,
                    pool="FBS",
                    nm_id=101,
                    operation_id="inapplicable-receipt",
                    business_date=TODAY,
                )
            except FfPoolDocumentError as exc:
                assert exc.code == "fbs_pair_inapplicable"
            else:
                raise AssertionError("inapplicable FBS writer must fail closed")
        applicability_service.record_applicability(
            facility_id=facility_id,
            nm_id=101,
            state="applicable",
            effective_from=TODAY,
            reason="dated handling approval restored",
            provenance={"ticket": "dense-smoke-2"},
            actor="dense-smoke",
        )
        _assert_zero(runtime.db_path, facility_id, 101)

        # Applicability evidence cannot hide or reinstate a malformed zero
        # shape; it must not turn missing cost truth into a valid zero.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {BALANCES_TABLE} SET wac_rub='1' "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                (facility_id,),
            )
            conn.commit()
        try:
            applicability_service.record_applicability(
                facility_id=facility_id,
                nm_id=101,
                state="inapplicable",
                effective_from=TODAY,
                reason="malformed zero must stay visible",
                provenance={"ticket": "dense-smoke-invalid-zero"},
                actor="dense-smoke",
            )
        except FbsApplicabilityError as exc:
            assert exc.code == "inapplicable_nonzero_physical_blocked"
        else:
            raise AssertionError("malformed explicit zero cannot become inapplicable")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {BALANCES_TABLE} SET wac_rub=NULL "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                (facility_id,),
            )
            conn.commit()

        # A missing applicable row never becomes zero through a receipt.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"DELETE FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                (facility_id,),
            )
            try:
                _movement(
                    conn,
                    facility_id=facility_id,
                    pool="FBS",
                    nm_id=101,
                    operation_id="missing-receipt",
                    business_date=TODAY,
                )
            except FfPoolDocumentError as exc:
                assert exc.code == "applicable_fbs_balance_missing"
            else:
                raise AssertionError("receipt must not create a missing FBS row")
            assert conn.execute(
                f"SELECT 1 FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                (facility_id,),
            ).fetchone() is None
            conn.rollback()

        # The immutable dense document proves T0 even after a later watermark.
        # A backdated event is routed to reconciliation, not current-zero copy.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                _movement(
                    conn,
                    facility_id=facility_id,
                    pool="FBS",
                    nm_id=101,
                    operation_id="backdated-receipt",
                    business_date="2026-08-25",
                )
            except FfPoolDocumentError as exc:
                assert exc.code == "backdated_fbs_event_requires_reconciliation"
            else:
                raise AssertionError("backdated FBS event must enter reconciliation")

        # FBO stays outside dense initialization and preserves its legacy
        # writer semantics.  No WB/aggregate table was touched by dense zeros.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            _movement(
                conn,
                facility_id=facility_id,
                pool="FBO",
                nm_id=101,
                operation_id="fbo-receipt",
                business_date=TODAY,
            )
            assert tuple(conn.execute(
                f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
                "WHERE facility_id=? AND pool='FBO' AND nm_id=101",
                (facility_id,),
            ).fetchone()) == (1, "1")
            conn.rollback()

        # The common warehouse writer lock serializes nomenclature staging and
        # publication.  Nothing becomes visible while another writer holds it.
        started = threading.Event()

        def save_locked_sku() -> dict[str, Any]:
            started.set()
            return runtime.save_nomenclature_item(
                _sku(303, updated_at="2026-08-26T08:05:00Z")
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            with warehouse_functional_write_lock(runtime_dir):
                future = pool.submit(save_locked_sku)
                assert started.wait(timeout=2)
                time.sleep(0.05)
                assert not future.done()
            locked_result = future.result(timeout=10)
        assert locked_result["is_active"] is True
        _assert_zero(runtime.db_path, facility_id, 303)

        # Ambiguous post transport is reconciled by canonical request readback;
        # the document is not blindly submitted twice.
        ambiguous = surface.create_facility(
            {
                "request_id": "dense-smoke-facility-ambiguous-stage",
                "name": "Оренбург Ambiguous",
                "city": "Оренбург",
                "active": False,
            },
            actor="dense-smoke",
        )
        ambiguous_id = str(ambiguous["facility"]["facility_id"])
        ambiguous_updated_at = str(ambiguous["facility"]["updated_at"])
        ambiguous_service = DenseFbsService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            document_service_factory=_AmbiguousAfterCommit,
        )
        completed = ambiguous_service.activate_facility(
            facility_id=ambiguous_id,
            expected_updated_at=ambiguous_updated_at,
            request_id="dense-smoke-ambiguous-activation",
            request_identity="sha256:" + "a" * 64,
            actor="dense-smoke",
        )
        assert completed["state"] == "active"
        document_count = _table_count(runtime.db_path, DOCUMENTS_TABLE)
        repeated = ambiguous_service.activate_facility(
            facility_id=ambiguous_id,
            expected_updated_at=ambiguous_updated_at,
            request_id="dense-smoke-ambiguous-activation",
            request_identity="sha256:" + "a" * 64,
            actor="dense-smoke",
        )
        assert repeated["idempotent"] is True
        assert _table_count(runtime.db_path, DOCUMENTS_TABLE) == document_count

        # A pinned balance row changing after plan construction is a terminal
        # CAS/drift block; the staged facility is never published active.
        drifted = surface.create_facility(
            {
                "request_id": "dense-smoke-facility-drift-stage",
                "name": "CAS Drift",
                "city": "Тест",
                "active": False,
            },
            actor="dense-smoke",
        )
        drifted_id = str(drifted["facility"]["facility_id"])
        drifted_updated_at = str(drifted["facility"]["updated_at"])
        drift_service = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime_dir)
        intent = drift_service._load_or_plan_facility_intent(
            orchestration_key="facility:dense-smoke-drift:dense-fbs",
            facility_id=drifted_id,
            expected_updated_at=drifted_updated_at,
            request_identity="sha256:" + "d" * 64,
            actor="dense-smoke",
        )
        first_planned_nm = int(intent["plan"]["documents"][0]["targets"][0]["nm_id"])
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES(?,'FBS',?,1,0,'0',NULL,'drift-fixture',?)""",
                (drifted_id, first_planned_nm, NOW),
            )
            conn.commit()
        try:
            drift_service._materialize(intent)
        except (DenseFbsError, FfPoolDocumentError) as exc:
            assert getattr(exc, "code", "") in {
                "dense_fbs_balance_drift",
                "dense_fbs_document_incomplete",
            }
        else:
            raise AssertionError("dense balance CAS drift must block activation")
        assert surface.facility_detail(drifted_id)["facility"]["active"] is False

        # A new SKU cannot opportunistically repair a legacy gap belonging to
        # already-active SKU/facility pairs.  It stays staged inactive and the
        # pre-existing missing pair remains missing for the separate repair.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_facility(
                conn,
                "fff_legacy_gap",
                "FF-LEGACY-GAP",
                active=True,
            )
            conn.commit()
        try:
            runtime.save_nomenclature_item(
                _sku(404, updated_at="2026-08-26T08:06:00Z")
            )
        except DenseFbsError as exc:
            assert exc.code == "preexisting_dense_fbs_coverage_incomplete"
        else:
            raise AssertionError("new SKU must not repair a pre-existing legacy gap")
        staged_404 = runtime.load_nomenclature_item("dense-sku-404")
        assert staged_404 is not None and staged_404["is_active"] is False
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE facility_id='fff_legacy_gap'"
            ).fetchone()[0] == 0

        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            states = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT DISTINCT state FROM {DENSE_INTENT_EVENTS_TABLE}"
                ).fetchall()
            }
            assert {"staged", "materializing", "materialized", "active", "blocked"} <= states
            assert _count(conn, APPLICABILITY_EVENTS_TABLE) == 2
        return {
            "active_facility_count": sum(
                bool(item["active"]) for item in surface.facilities_page()["facilities"]
            ),
            "document_count": document_count,
            "applicability_event_count": 2,
            "shared_lock_serialized": True,
            "ambiguous_transport_reconciled": True,
            "cas_drift_failed_closed": True,
        }


def _orenburg_repair_contract() -> dict[str, Any]:
    with TemporaryDirectory(prefix="dense-fbs-orenburg-plan-") as raw:
        runtime_dir = Path(raw) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True)
        non_target_nm_ids = [700_000_000 + value for value in range(21)]
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            _enable_writer(conn)
            _insert_facility(conn, ORENBURG_FACILITY_ID, "FF-ORENBURG-EXACT", active=True)
            _insert_facility(conn, "fff_moscow_non_target", "FF-MOSCOW", active=True)
            for nm_id in (*ORENBURG_TARGET_NM_IDS, *non_target_nm_ids):
                _insert_nomenclature(conn, nm_id)
            for position, nm_id in enumerate(non_target_nm_ids, start=1):
                conn.execute(
                    f"""INSERT INTO {BALANCES_TABLE}(
                           facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                           wac_rub,source_watermark,updated_at
                       ) VALUES(?,'FBS',?,1,?,?,?, 'orenburg-existing',?)""",
                    (
                        ORENBURG_FACILITY_ID,
                        nm_id,
                        position,
                        str(position),
                        "1",
                        NOW,
                    ),
                )
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES('fff_moscow_non_target','FBS',700000000,1,9,'9','1',
                            'moscow-existing',?)""",
                (NOW,),
            )
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES(?,'FBO',700000000,1,8,'8','1','fbo-existing',?)""",
                (ORENBURG_FACILITY_ID, NOW),
            )
            conn.commit()
        before = _file_sha256(runtime.db_path)
        service = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime_dir)
        plan = service.build_zero_repair_plan(
            facility_id=ORENBURG_FACILITY_ID,
            nm_ids=ORENBURG_TARGET_NM_IDS,
            expected_existing_non_target_count=ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS,
        )
        repeated = service.build_zero_repair_plan(
            facility_id=ORENBURG_FACILITY_ID,
            nm_ids=ORENBURG_TARGET_NM_IDS,
            expected_existing_non_target_count=ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS,
        )
        after = _file_sha256(runtime.db_path)
        assert before == after
        assert plan == repeated
        assert plan["apply_allowed"] is True
        assert plan["apply_entrypoint_exposed"] is False
        assert plan["blockers"] == []
        assert plan["nm_ids"] == sorted(ORENBURG_TARGET_NM_IDS)
        assert plan["expected_effects"] == {
            "balance_insert_count": 12,
            "balance_update_count": 0,
            "quantity_delta": 0,
            "capital_delta_rub": "0",
            "wac_effect": None,
            "movement_line_count": 0,
            "pool_inventory_document_count": 1,
        }
        assert plan["non_targets"]["target_facility_existing_fbs_row_count"] == 21
        assert plan["non_targets"]["wb_snapshots_count"] == 0
        assert str(plan["non_targets"]["wb_snapshots_digest"]).startswith("sha256:")
        assert plan["storage"] == {
            "whole_database_copy": False,
            "bounded_target_row_count": 12,
            "non_target_digest_fetch_chunk_rows": 512,
            "non_target_rows_retained_in_memory": False,
        }
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' "
                f"AND nm_id IN ({','.join('?' for _ in ORENBURG_TARGET_NM_IDS)})",
                (ORENBURG_FACILITY_ID, *ORENBURG_TARGET_NM_IDS),
            ).fetchone()[0] == 0
            assert conn.execute(
                f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS'",
                (ORENBURG_FACILITY_ID,),
            ).fetchone()[0] == 21
            assert conn.execute(
                f"SELECT quantity FROM {BALANCES_TABLE} WHERE facility_id='fff_moscow_non_target' "
                "AND pool='FBS' AND nm_id=700000000"
            ).fetchone()[0] == 9
            assert conn.execute(
                f"SELECT quantity FROM {BALANCES_TABLE} WHERE facility_id=? "
                "AND pool='FBO' AND nm_id=700000000",
                (ORENBURG_FACILITY_ID,),
            ).fetchone()[0] == 8
        return {
            "facility_id": ORENBURG_FACILITY_ID,
            "target_count": len(plan["nm_ids"]),
            "existing_non_target_fbs_count": 21,
            "fingerprint": plan["fingerprint"],
            "query_only_file_digest_unchanged": True,
            "apply_exposed": False,
        }


def _production_shaped_benchmark() -> dict[str, Any]:
    with TemporaryDirectory(prefix="dense-fbs-benchmark-") as raw:
        runtime_dir = Path(raw) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True)
        sku_count = 250
        facility_count = 4
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            _ensure_schema(conn)
            _enable_writer(conn)
            for nm_id in range(800_000_000, 800_000_000 + sku_count):
                _insert_nomenclature(conn, nm_id)
            conn.commit()
        surface = FfPoolSurface(db_path=runtime.db_path, runtime_dir=runtime_dir)
        started = time.monotonic()
        facility_ids: list[str] = []
        for number in range(facility_count):
            result = surface.create_facility(
                {
                    "request_id": f"dense-benchmark-facility-{number}",
                    "name": f"Dense Benchmark {number}",
                    "city": "Benchmark",
                    "active": True,
                },
                actor="dense-benchmark",
            )
            facility_ids.append(str(result["facility"]["facility_id"]))
        elapsed = time.monotonic() - started
        with sqlite3.connect(runtime.db_path) as conn:
            pair_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE pool='FBS'"
                ).fetchone()[0]
            )
            assert pair_count == sku_count * facility_count
            assert conn.execute(
                f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE pool='FBS' "
                "AND (quantity<>0 OR capital_rub<>'0' OR wac_rub IS NOT NULL)"
            ).fetchone()[0] == 0
            assert _count(conn, LINES_TABLE) == 0
            assert _count(conn, DOCUMENTS_TABLE) == facility_count
            assert _count(conn, DOCUMENT_LINES_TABLE) == pair_count
        db_bytes = runtime.db_path.stat().st_size
        assert elapsed < 30
        assert db_bytes < 25 * 1024 * 1024
        return {
            "sku_count": sku_count,
            "facility_count": facility_count,
            "pair_count": pair_count,
            "elapsed_seconds": round(elapsed, 3),
            "database_bytes": db_bytes,
            "whole_database_copy": False,
        }


def _sku(nm_id: int, *, updated_at: str) -> dict[str, Any]:
    return {
        "item_id": f"dense-sku-{nm_id}",
        "is_active": True,
        "is_hidden": False,
        "our_sku": f"dense-{nm_id}",
        "nm_id": nm_id,
        "barcode": f"barcode-{nm_id}",
        "nomenclature_name": f"Dense SKU {nm_id}",
        "product_type": "fixture",
        "match_key": f"dense-{nm_id}",
        "aliases": [],
        "created_at": NOW,
        "updated_at": updated_at,
    }


def _enable_writer(conn: sqlite3.Connection) -> None:
    if conn.execute(f"SELECT 1 FROM {FEATURE_EPOCHS_TABLE} WHERE epoch=1").fetchone():
        return
    conn.execute(
        f"""INSERT INTO {FEATURE_EPOCHS_TABLE}(
               epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json
           ) VALUES(1,1,1,'dense-fbs-smoke',?,'{{}}')""",
        (NOW,),
    )


def _insert_facility(
    conn: sqlite3.Connection, facility_id: str, code: str, *, active: bool
) -> None:
    conn.execute(
        f"""INSERT INTO {FACILITIES_TABLE}(
               facility_id,code,name,active,display_timezone,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (facility_id, code, code, int(active), "Asia/Yekaterinburg", NOW, NOW),
    )
    conn.execute(
        f"""INSERT INTO {FACILITY_PROFILES_TABLE}(
               facility_id,city,future_fields_json,created_at,updated_at
           ) VALUES(?,?,'{{}}',?,?)""",
        (facility_id, code, NOW, NOW),
    )


def _insert_nomenclature(conn: sqlite3.Connection, nm_id: int) -> None:
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_nomenclature_items(
               item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,
               match_key,aliases_json,created_at,updated_at
           ) VALUES(?,1,0,?,?,?,?,'[]',?,?)""",
        (
            f"repair-nm-{nm_id}",
            nm_id,
            f"Repair SKU {nm_id}",
            "fixture",
            f"repair-{nm_id}",
            NOW,
            NOW,
        ),
    )


def _movement(
    conn: sqlite3.Connection,
    *,
    facility_id: str,
    pool: str,
    nm_id: int,
    operation_id: str,
    business_date: str,
) -> None:
    _apply_balance_movement(
        conn,
        movement={
            "facility_id": facility_id,
            "pool": pool,
            "nm_id": nm_id,
            "quantity_delta": 1,
            "capital_delta_cents": 100,
            "wac_snapshot": "1",
            "metadata": {"dense_fbs_smoke": True},
        },
        operation_id=operation_id,
        line_no=1,
        epoch=1,
        posted_at=f"{business_date}T09:00:00Z",
        business_date=business_date,
    )


def _assert_zero(db_path: Path, facility_id: str, nm_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
            "WHERE facility_id=? AND pool='FBS' AND nm_id=?",
            (facility_id, nm_id),
        ).fetchone()
        assert row is not None and tuple(row) == (0, "0", None)
        typed = fbs_physical_component(
            conn,
            facility_id=facility_id,
            nm_id=nm_id,
            as_of_date=TODAY,
            projection_epoch=1,
        )
        assert typed["state"] == "exact_zero"
        assert typed["quantity"] == 0


def _non_fbs_digest(db_path: Path) -> str:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        selected = [
            table
            for table in (
                "sheet_vitrina_v1_warehouse_wb_snapshots",
                "sheet_vitrina_v1_warehouse_functional_balances",
                "sheet_vitrina_v1_inventory_history_components",
            )
            if table in tables
        ]
        material = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in selected
        }
    return "sha256:" + hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return _count(conn, table)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
