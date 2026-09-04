#!/usr/bin/env python3
"""Acceptance smoke for applicability-gated dense FBS initialization."""

from __future__ import annotations

import argparse
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal
from pathlib import Path
import hashlib
import io
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
    _strict_domain_manifest_v3,
    _write_private,
    run as run_orenburg_cli,
)
from packages.application.ff_pool_dense_fbs import (  # noqa: E402
    DenseFbsError,
    DenseFbsResumableError,
    DenseFbsService,
    ZERO_REPAIR_MANIFEST_SCHEMA,
    _fingerprint,
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
    current_business_date,
    fbs_physical_component,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    BALANCES_TABLE,
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
    OPERATIONS_TABLE,
)
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.root_storage_policy import RootStoragePolicyError  # noqa: E402
from packages.application.storage_registry import (  # noqa: E402
    MANIFEST_FILENAME,
    MONOLITH_FILENAME,
    atomic_write_manifest,
    build_manifest,
)
from packages.contracts.ff_pool_documents import DocumentIdentity  # noqa: E402
from packages.application.warehouse_functional_lock import (  # noqa: E402
    warehouse_functional_write_lock,
)


NOW = "2026-08-26T08:00:00Z"
TODAY = "2026-08-26"
ORENBURG_FACILITY_ID = "fff_2579bb2741ed4ab23b11bb4c4183"
ORENBURG_SELLER_WAREHOUSE_ID = 854205
ORENBURG_OFFICIAL_OFFICE_ID = 12223
ORENBURG_HISTORICAL_ZERO_DATE = "2026-08-24"
ORENBURG_EXISTING_NM_IDS = (
    210183142,
    210183919,
    210184534,
    245720334,
    259460529,
    259465495,
    259473237,
    391659990,
    428850065,
    428853741,
    428854140,
    428854299,
    428855306,
    428855560,
    428855758,
    428855978,
    497414010,
    497414624,
    497416271,
    497417163,
    497417474,
)
ORENBURG_TARGET_NM_IDS = (
    259466031,
    391660889,
    391661710,
    391662410,
    391662965,
    391663632,
    428849827,
    428854502,
    497413772,
    497415593,
    497416559,
    497416931,
    1221231049,
    1221235702,
    1221244040,
    1221249681,
    1235346302,
    1235353505,
    1235356960,
    1235358879,
    1235360281,
    1235361692,
    1235365622,
    1235366828,
    1235368116,
    1235369738,
    1235373410,
    1235374572,
    1235375860,
    1235377899,
    1235379341,
    1235381785,
    1235384726,
    1235387930,
    1235392011,
    1235393709,
    1235398515,
    1235399866,
    1235404761,
    1235405720,
    1235406475,
    1235406984,
    1235407826,
    1235409896,
    1235411727,
    1235412880,
    1235413454,
    1235414081,
    1235419785,
    1235421650,
)
ORENBURG_ORIGINAL_TARGET_NM_IDS = ORENBURG_TARGET_NM_IDS[:12]
ORENBURG_WB_CONTENT_TARGET_NM_IDS = ORENBURG_TARGET_NM_IDS[12:]
ORENBURG_EXPECTED_EXISTING_NON_TARGET_FBS_ROWS = len(ORENBURG_EXISTING_NM_IDS)
ORENBURG_EXPECTED_STOCK_MANAGED_ROSTER = len(ORENBURG_TARGET_NM_IDS) + len(
    ORENBURG_EXISTING_NM_IDS
)


class _AmbiguousAfterCommit:
    """Commit once, then emulate a lost transport response."""

    def __init__(self, **kwargs: Any) -> None:
        self._delegate = FfPoolDocumentService(**kwargs)

    def post(self, request_id: str) -> dict[str, Any]:
        self._delegate.post(request_id)
        raise ConnectionError("simulated response loss after commit")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _LostBeforeCommit:
    """Persist a ready request, then lose transport before any business commit."""

    def __init__(self, **kwargs: Any) -> None:
        self._delegate = FfPoolDocumentService(**kwargs)

    def post(self, request_id: str) -> dict[str, Any]:
        raise ConnectionError("simulated transport loss before commit")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _LoseSecondPostBeforeCommit:
    """Complete one canonical document, then lose the next pre-commit."""

    def __init__(self, **kwargs: Any) -> None:
        self._delegate = FfPoolDocumentService(**kwargs)
        self.post_count = 0

    def post(self, request_id: str) -> dict[str, Any]:
        self.post_count += 1
        if self.post_count == 2:
            raise ConnectionError("simulated second-document loss before commit")
        return self._delegate.post(request_id)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class _SharedDocumentFactory:
    def __init__(self, service_type: type[Any]) -> None:
        self.service_type = service_type
        self.instance: Any | None = None

    def __call__(self, **kwargs: Any) -> Any:
        if self.instance is None:
            self.instance = self.service_type(**kwargs)
        return self.instance


def main() -> int:
    lifecycle = _lifecycle_contract()
    benchmark = _production_shaped_benchmark()
    print(
        json.dumps(
            {
                "status": "ok",
                "lifecycle": lifecycle,
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
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {DENSE_INTENT_EVENTS_TABLE} WHERE state='active'"
                ).fetchone()[0]
                == 2
            )
            line = conn.execute(
                f"SELECT quantity,capital_rub,metadata_json FROM {DOCUMENT_LINES_TABLE} "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                (facility_id,),
            ).fetchone()
            assert int(line[0]) == 0 and Decimal(str(line[1])) == 0
            assert json.loads(str(line[2]))["explicit_physical_zero"] is True

        # Merely copying the server source labels cannot bypass the durable
        # staged intent.  The canonical post binds the exact plan/document
        # specification before it can touch an inactive facility or balance.
        spoof_revision = "sha256:" + "a" * 64
        spoof_service = FfPoolDocumentService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=lambda: NOW,
            resume=False,
        )
        spoof_identity = DocumentIdentity(
            request_id="dense-spoof-without-intent",
            source_system="wb_core_dense_fbs",
            source_type="ff_pool_dense_fbs_initialization_v1",
            source_id=f"missing-intent:{facility_id}",
            source_revision=spoof_revision,
            idempotency_epoch=1,
            actor="dense-smoke",
            business_date=TODAY,
        )
        spoof_dense = {
            "contract_name": "ff_pool_dense_fbs_initialization_v1",
            "intent_id": "missing-intent",
            "subject_kind": "facility_activation",
            "subject_id": facility_id,
            "effective_from": TODAY,
            "cutover_at": NOW,
            "roster_fingerprint": "sha256:" + "b" * 64,
            "plan_fingerprint": spoof_revision,
            "applicable_nm_ids": [101],
            "expected_balance_rows": [],
        }
        spoof = spoof_service.accept_preview(
            identity=spoof_identity,
            document_kind="pool_inventory",
            manifest={
                "facility_id": facility_id,
                "scope": "FBS",
                "targets": [{"nm_id": 101, "target_fbs": 0}],
                "dense_fbs_initialization": spoof_dense,
            },
        )
        assert spoof["state"] in {"accepted", "ready"}, spoof
        if spoof["state"] == "accepted":
            spoof = spoof_service.process_request(str(spoof["request_id"]))
            assert spoof["state"] == "ready"
        spoof = spoof_service.post(str(spoof["request_id"]))
        assert spoof["state"] == "blocked"
        assert spoof["error"]["code"] == "dense_fbs_intent_binding_invalid"

        # New active SKU is staged inactive, materialized across every active
        # FBS facility, read back, and only then published active.
        second = runtime.save_nomenclature_item(
            _sku(202, updated_at="2026-08-26T08:01:00Z")
        )
        assert second["is_active"] is True
        _assert_zero(runtime.db_path, facility_id, 202)

        # Retirement can never hide non-zero FBS physical truth.  Both the
        # save/update and delete paths fail before active publication changes.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {BALANCES_TABLE} SET quantity=7,capital_rub='70',wac_rub='10',"
                "source_watermark='forward-receipt-fixture',updated_at=? "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=202",
                ("2026-08-26T08:02:00Z", facility_id),
            )
            movement_count = _count(conn, LINES_TABLE)
            document_line_count = _count(conn, DOCUMENT_LINES_TABLE)
            conn.commit()
        for attempted in (
            {**_sku(202, updated_at="2026-08-26T08:02:30Z"), "is_hidden": True},
            {
                **_sku(222, updated_at="2026-08-26T08:02:31Z"),
                "item_id": str(second["item_id"]),
            },
        ):
            try:
                runtime.save_nomenclature_item(attempted)
            except FbsApplicabilityError as exc:
                assert exc.code == "fbs_sku_retirement_blocked"
                assert exc.details["blockers"]["nonzero_fbs_rows"]
            else:
                raise AssertionError(
                    "save/update retirement must keep non-zero SKU active"
                )
        try:
            runtime.delete_nomenclature_item(
                str(second["item_id"]), updated_at="2026-08-26T08:03:00Z"
            )
        except FbsApplicabilityError as exc:
            assert exc.code == "fbs_sku_retirement_blocked"
        else:
            raise AssertionError("delete retirement must keep non-zero SKU active")
        still_active = runtime.load_nomenclature_item(str(second["item_id"]))
        assert still_active is not None and still_active["is_active"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            retained_nonzero = conn.execute(
                f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=202",
                (facility_id,),
            ).fetchone()
            assert tuple(retained_nonzero) == (7, "70", "10")
            conn.execute(
                f"DELETE FROM {BALANCES_TABLE} "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=202",
                (facility_id,),
            )
            conn.commit()
        try:
            runtime.delete_nomenclature_item(
                str(second["item_id"]), updated_at="2026-08-26T08:03:15Z"
            )
        except FbsApplicabilityError as exc:
            assert exc.code == "fbs_sku_retirement_blocked"
            assert exc.details["blockers"]["incomplete_coverage"]
        else:
            raise AssertionError("retirement requires complete canonical FBS coverage")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"""INSERT INTO {BALANCES_TABLE}(
                       facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,
                       wac_rub,source_watermark,updated_at
                   ) VALUES(?,'FBS',202,1,0,'0',NULL,
                            'explicit-zero-retirement-fixture',?)""",
                (facility_id, "2026-08-26T08:03:30Z"),
            )
            conn.commit()

        # A covered canonical zero may be archived.  Its balance and immutable
        # documents survive; reactivation reuses the row without a movement.
        runtime.delete_nomenclature_item(
            str(second["item_id"]), updated_at="2026-08-26T08:04:00Z"
        )
        archived = runtime.load_nomenclature_item(str(second["item_id"]))
        assert archived is not None and archived["is_active"] is False
        reactivated = runtime.save_nomenclature_item(
            _sku(202, updated_at="2026-08-26T08:05:00Z")
        )
        assert reactivated["is_active"] is True
        with sqlite3.connect(runtime.db_path) as conn:
            retained = conn.execute(
                f"SELECT quantity,capital_rub,wac_rub FROM {BALANCES_TABLE} "
                "WHERE facility_id=? AND pool='FBS' AND nm_id=202",
                (facility_id,),
            ).fetchone()
            assert tuple(retained) == (0, "0", None)
            assert _count(conn, LINES_TABLE) == movement_count
            assert _count(conn, DOCUMENT_LINES_TABLE) >= document_line_count
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {DOCUMENT_LINES_TABLE} "
                    "WHERE facility_id=? AND pool='FBS' AND nm_id=202 "
                    "AND json_extract(metadata_json,'$.explicit_physical_zero')=1",
                    (facility_id,),
                ).fetchone()[0]
                == 2
            )

        # Active lifecycle reservations and unfinished official orders are
        # independent retirement blockers even when physical stock is zero.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_ff_pool_fbs_lifecycle_current(
                       cutover_id,order_id,state,episode_sequence,source_revision,
                       status_digest,supplier_status,wb_status,facility_id,pool,nm_id,
                       quantity,frozen_wac_rub,debit_event_id,updated_at
                   ) VALUES('cutover-retirement',9202001,'reserved',1,'rev','digest',
                            'new','waiting',?,'FBS',202,1,'1','',?)""",
                (facility_id, NOW),
            )
            conn.commit()
        try:
            runtime.delete_nomenclature_item(
                str(second["item_id"]), updated_at="2026-08-26T08:05:30Z"
            )
        except FbsApplicabilityError as exc:
            assert exc.code == "fbs_sku_retirement_blocked"
            assert exc.details["blockers"]["active_lifecycle_reservations"]
        else:
            raise AssertionError("active FBS reservation must block retirement")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_ff_pool_fbs_lifecycle_current "
                "SET state='released' WHERE cutover_id='cutover-retirement'"
            )
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
                       observation_id,order_id,source_revision,supply_id,delivery_type,
                       source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
                       rid_sha256,order_uid_sha256,skus_json,cargo_type,cross_border_type,
                       is_zero_order,observed_at,collector_date_from,collector_date_to,
                       collector_cursor
                   ) VALUES('retirement-order',9202002,'revision-retirement','','fbs','',854205,12223,
                            202,NULL,'sku','','','[]',NULL,NULL,0,?,20260826,20260826,0)""",
                (NOW,),
            )
            conn.commit()
        try:
            runtime.delete_nomenclature_item(
                str(second["item_id"]), updated_at="2026-08-26T08:06:00Z"
            )
        except FbsApplicabilityError as exc:
            assert exc.code == "fbs_sku_retirement_blocked"
            assert exc.details["blockers"]["unfinished_orders"]
        else:
            raise AssertionError("unfinished FBS order must block retirement")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_current(
                       order_id,order_revision,status_digest,supplier_status,wb_status,
                       source_observed_at,local_first_seen_at,local_last_seen_at,
                       observation_count,episode_sequence
                   ) VALUES(9202002,'revision-retirement','digest','complete','sold',?,?,?,1,1)""",
                (NOW, NOW, NOW),
            )
            conn.commit()
        retired_after_terminal_order = runtime.delete_nomenclature_item(
            str(second["item_id"]), updated_at="2026-08-26T08:06:30Z"
        )
        assert retired_after_terminal_order["is_active"] is False
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            try:
                _movement(
                    conn,
                    facility_id=facility_id,
                    pool="FBS",
                    nm_id=202,
                    operation_id="inactive-sku-receipt",
                    business_date=TODAY,
                )
            except FfPoolDocumentError as exc:
                assert exc.code == "fbs_pair_inapplicable"
            else:
                raise AssertionError("normal FBS writer must reject an archived SKU")
            conn.rollback()
        second = runtime.save_nomenclature_item(
            _sku(202, updated_at="2026-08-26T08:07:00Z")
        )
        assert second["is_active"] is True

        # Default applicability can only be overridden by dated immutable
        # evidence.  Reinstatement reuses the retained exact zero row.
        applicability_service = DenseFbsService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=lambda: NOW,
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
            before_ekt_midnight = fbs_physical_component(
                conn,
                facility_id=facility_id,
                nm_id=101,
                as_of_date="2026-08-25T18:59:59Z",
                projection_epoch=1,
            )
            after_ekt_midnight = fbs_physical_component(
                conn,
                facility_id=facility_id,
                nm_id=101,
                as_of_date="2026-08-25T19:00:00Z",
                projection_epoch=1,
            )
            assert before_ekt_midnight["state"] == "exact_zero"
            assert after_ekt_midnight["state"] == "inapplicable"
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
            assert (
                conn.execute(
                    f"SELECT 1 FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=101",
                    (facility_id,),
                ).fetchone()
                is None
            )
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
            assert tuple(
                conn.execute(
                    f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} "
                    "WHERE facility_id=? AND pool='FBO' AND nm_id=101",
                    (facility_id,),
                ).fetchone()
            ) == (1, "1")
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

        assert current_business_date("2026-08-25T18:59:59Z") == "2026-08-25"
        assert current_business_date("2026-08-25T19:00:00Z") == "2026-08-26"

        # A pre-commit transport loss leaves a durable exact-id resumable
        # intent.  A later orchestration retry posts only the unfinished
        # canonical request and then activates the facility.
        before_commit = surface.create_facility(
            {
                "request_id": "dense-smoke-facility-before-commit-stage",
                "name": "Before Commit Loss",
                "city": "Тест",
                "active": False,
            },
            actor="dense-smoke",
        )
        before_commit_id = str(before_commit["facility"]["facility_id"])
        before_commit_updated_at = str(before_commit["facility"]["updated_at"])
        resumable_service = DenseFbsService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            document_service_factory=_LostBeforeCommit,
        )
        documents_before_loss = _table_count(runtime.db_path, DOCUMENTS_TABLE)
        try:
            resumable_service.activate_facility(
                facility_id=before_commit_id,
                expected_updated_at=before_commit_updated_at,
                request_id="dense-smoke-before-commit-activation",
                request_identity="sha256:" + "b" * 64,
                actor="dense-smoke",
            )
        except DenseFbsResumableError as exc:
            assert exc.code == "dense_fbs_document_transport_resumable"
            assert exc.details["canonical_state"] == "ready"
        else:
            raise AssertionError("pre-commit transport loss must remain resumable")
        assert surface.facility_detail(before_commit_id)["facility"]["active"] is False
        assert _table_count(runtime.db_path, DOCUMENTS_TABLE) == documents_before_loss
        with sqlite3.connect(runtime.db_path) as conn:
            latest_state = conn.execute(
                f"""SELECT event.state FROM {DENSE_INTENT_EVENTS_TABLE} event
                     JOIN {DENSE_INTENTS_TABLE} intent USING(intent_id)
                    WHERE intent.orchestration_key=?
                    ORDER BY event.event_sequence DESC LIMIT 1""",
                ("facility:dense-smoke-before-commit-activation:dense-fbs",),
            ).fetchone()[0]
            assert latest_state == "resumable"
        resumed = DenseFbsService(
            db_path=runtime.db_path, runtime_dir=runtime_dir
        ).activate_facility(
            facility_id=before_commit_id,
            expected_updated_at=before_commit_updated_at,
            request_id="dense-smoke-before-commit-activation",
            request_identity="sha256:" + "b" * 64,
            actor="dense-smoke",
        )
        assert resumed["state"] == "active"
        assert (
            _table_count(runtime.db_path, DOCUMENTS_TABLE) == documents_before_loss + 1
        )

        # A two-facility SKU activation proves that exact resume reads back the
        # first completed document and posts only the second ready request.
        multi_updated_at = "2026-08-26T08:05:10Z"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_nomenclature_items(
                       item_id,is_active,is_hidden,nm_id,nomenclature_name,
                       product_type,match_key,aliases_json,created_at,updated_at
                   ) VALUES('dense-sku-304',0,0,304,'Dense SKU 304','fixture',
                            'dense-304','[]',?,?)""",
                (NOW, multi_updated_at),
            )
            conn.commit()
        multi_documents_before = _table_count(runtime.db_path, DOCUMENTS_TABLE)
        multi_factory = _SharedDocumentFactory(_LoseSecondPostBeforeCommit)
        multi_service = DenseFbsService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            document_service_factory=multi_factory,
        )
        multi_identity = "sha256:" + "c" * 64
        try:
            multi_service.activate_staged_skus(
                staged_items=[
                    {
                        "item_id": "dense-sku-304",
                        "nm_id": 304,
                        "updated_at": multi_updated_at,
                    }
                ],
                orchestration_key="sku-activation:" + multi_identity,
                request_identity=multi_identity,
                actor="dense-smoke",
            )
        except DenseFbsResumableError as exc:
            assert exc.details["canonical_state"] == "ready"
        else:
            raise AssertionError("second canonical submit loss must remain resumable")
        assert (
            _table_count(runtime.db_path, DOCUMENTS_TABLE) == multi_documents_before + 1
        )
        staged_multi = runtime.load_nomenclature_item("dense-sku-304")
        assert staged_multi is not None and staged_multi["is_active"] is False
        multi_resumed = DenseFbsService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
        ).activate_staged_skus(
            staged_items=[
                {
                    "item_id": "dense-sku-304",
                    "nm_id": 304,
                    "updated_at": multi_updated_at,
                }
            ],
            orchestration_key="sku-activation:" + multi_identity,
            request_identity=multi_identity,
            actor="dense-smoke",
        )
        assert multi_resumed["state"] == "active"
        assert (
            _table_count(runtime.db_path, DOCUMENTS_TABLE) == multi_documents_before + 2
        )
        multi_repeated = DenseFbsService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
        ).activate_staged_skus(
            staged_items=[
                {
                    "item_id": "dense-sku-304",
                    "nm_id": 304,
                    "updated_at": multi_updated_at,
                }
            ],
            orchestration_key="sku-activation:" + multi_identity,
            request_identity=multi_identity,
            actor="dense-smoke",
        )
        assert multi_repeated["idempotent"] is True
        assert (
            _table_count(runtime.db_path, DOCUMENTS_TABLE) == multi_documents_before + 2
        )

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
        drift_service = DenseFbsService(
            db_path=runtime.db_path, runtime_dir=runtime_dir
        )
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

        # Subject CAS drift after completed documents is terminal intent
        # evidence, not a resumable transport state or external-active success.
        subject_drift = surface.create_facility(
            {
                "request_id": "dense-smoke-subject-cas-stage",
                "name": "Subject CAS Drift",
                "city": "Тест",
                "active": False,
            },
            actor="dense-smoke",
        )
        subject_drift_id = str(subject_drift["facility"]["facility_id"])
        subject_drift_updated_at = str(subject_drift["facility"]["updated_at"])
        subject_identity = "sha256:" + "e" * 64
        subject_service = DenseFbsService(
            db_path=runtime.db_path, runtime_dir=runtime_dir
        )
        subject_intent = subject_service._load_or_plan_facility_intent(
            orchestration_key="facility:dense-smoke-subject-cas:dense-fbs",
            facility_id=subject_drift_id,
            expected_updated_at=subject_drift_updated_at,
            request_identity=subject_identity,
            actor="dense-smoke",
        )
        subject_service._materialize(subject_intent)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                f"UPDATE {FACILITIES_TABLE} SET updated_at=? WHERE facility_id=?",
                ("2026-08-26T08:05:45Z", subject_drift_id),
            )
            conn.commit()
        try:
            subject_service.activate_facility(
                facility_id=subject_drift_id,
                expected_updated_at=subject_drift_updated_at,
                request_id="dense-smoke-subject-cas",
                request_identity=subject_identity,
                actor="dense-smoke",
            )
        except DenseFbsError as exc:
            assert exc.code == "facility_activation_cas_drift"
        else:
            raise AssertionError("subject CAS drift must terminally block publication")
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    f"""SELECT event.state FROM {DENSE_INTENT_EVENTS_TABLE} event
                     WHERE event.intent_id=? ORDER BY event.event_sequence DESC LIMIT 1""",
                    (subject_intent["intent_id"],),
                ).fetchone()[0]
                == "blocked"
            )
            assert (
                conn.execute(
                    f"SELECT active FROM {FACILITIES_TABLE} WHERE facility_id=?",
                    (subject_drift_id,),
                ).fetchone()[0]
                == 0
            )

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
            runtime.save_nomenclature_item(_sku(404, updated_at="2026-08-26T08:06:00Z"))
        except DenseFbsError as exc:
            assert exc.code == "preexisting_dense_fbs_coverage_incomplete"
        else:
            raise AssertionError("new SKU must not repair a pre-existing legacy gap")
        staged_404 = runtime.load_nomenclature_item("dense-sku-404")
        assert staged_404 is not None and staged_404["is_active"] is False
        with sqlite3.connect(runtime.db_path) as conn:
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE facility_id='fff_legacy_gap'"
                ).fetchone()[0]
                == 0
            )

        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            states = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT DISTINCT state FROM {DENSE_INTENT_EVENTS_TABLE}"
                ).fetchall()
            }
            assert {
                "staged",
                "materializing",
                "resumable",
                "materialized",
                "active",
                "blocked",
            } <= states
            assert _count(conn, APPLICABILITY_EVENTS_TABLE) == 2
        return {
            "active_facility_count": sum(
                bool(item["active"]) for item in surface.facilities_page()["facilities"]
            ),
            "document_count": document_count,
            "applicability_event_count": 2,
            "shared_lock_serialized": True,
            "ambiguous_transport_reconciled": True,
            "before_commit_transport_resumed": True,
            "multi_document_resume_completed_only_once": True,
            "retirement_nonzero_reservation_order_guards": True,
            "business_date_boundary": "2026-08-25T19:00:00Z->2026-08-26",
            "cas_drift_failed_closed": True,
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
        initial_elapsed = time.monotonic() - started
        storage_before_new_sku = _dense_storage_bytes(runtime.db_path)
        database_before_new_sku = runtime.db_path.stat().st_size

        new_sku_started = time.monotonic()
        new_sku = runtime.save_nomenclature_item(
            _sku(800_000_000 + sku_count, updated_at="2026-08-26T09:00:00Z")
        )
        new_sku_elapsed = time.monotonic() - new_sku_started
        assert new_sku["is_active"] is True
        elapsed = time.monotonic() - started
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            pair_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE pool='FBS'"
                ).fetchone()[0]
            )
            assert pair_count == (sku_count + 1) * facility_count
            assert (
                conn.execute(
                    f"SELECT COUNT(*) FROM {BALANCES_TABLE} WHERE pool='FBS' "
                    "AND (quantity<>0 OR capital_rub<>'0' OR wac_rub IS NOT NULL)"
                ).fetchone()[0]
                == 0
            )
            assert _count(conn, LINES_TABLE) == 0
            assert _count(conn, DOCUMENTS_TABLE) == facility_count * 2
            assert _count(conn, DOCUMENT_LINES_TABLE) == pair_count
            assert _count(conn, APPLICABILITY_EVENTS_TABLE) == 0
            compact_plan = json.loads(
                str(
                    conn.execute(
                        f"""SELECT plan_json FROM {DENSE_INTENTS_TABLE}
                             WHERE subject_kind='sku_activation'
                             ORDER BY created_at DESC,intent_id DESC LIMIT 1"""
                    ).fetchone()[0]
                )
            )
            assert len(compact_plan["pairs"]) == facility_count
            assert compact_plan["existing_coverage_proof"]["pair_count"] == (
                sku_count * facility_count
            )
            assert compact_plan["existing_coverage_proof"]["rows_persisted"] is False
            assert compact_plan["existing_coverage_proof"]["complete"] is True
            assert len(compact_plan["roster"]["skus"]) == sku_count + 1
            assert len(compact_plan["roster"]["facilities"]) == facility_count
        storage_after_new_sku = _dense_storage_bytes(runtime.db_path)
        db_bytes = runtime.db_path.stat().st_size
        assert elapsed < 30
        assert db_bytes < 25 * 1024 * 1024
        incremental_storage = {
            key: storage_after_new_sku[key] - storage_before_new_sku[key]
            for key in storage_after_new_sku
        }
        assert incremental_storage["dense_intent_bytes"] < 512 * 1024
        assert db_bytes - database_before_new_sku < 2 * 1024 * 1024
        return {
            "initial_sku_count": sku_count,
            "final_sku_count": sku_count + 1,
            "facility_count": facility_count,
            "pair_count": pair_count,
            "initial_facility_activation_seconds": round(initial_elapsed, 3),
            "new_sku_existing_facilities_seconds": round(new_sku_elapsed, 3),
            "elapsed_seconds": round(elapsed, 3),
            "database_bytes": db_bytes,
            **storage_after_new_sku,
            "new_sku_incremental_bytes": incremental_storage,
            "new_sku_persisted_pair_rows": facility_count,
            "new_sku_compact_existing_pair_proof": sku_count * facility_count,
            "default_applicability_event_rows": 0,
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
    wb_content = int(nm_id) in set(ORENBURG_WB_CONTENT_TARGET_NM_IDS)
    vendor_code = f"wb-vendor-{nm_id}"
    evidence = (
        {
            "source": "wb_content_cards",
            "endpoint": "/content/v2/get/cards/list",
            "result": "created",
            "match_type": "nm_id",
            "nm_id": int(nm_id),
            "vendor_code": vendor_code,
            "barcode_count": 1,
        }
        if wb_content
        else {
            "source": "wb_content_cards",
            "endpoint": "/content/v2/get/cards/list",
            "result": "matched",
            "match_type": "vendor_code",
            "nm_id": int(nm_id),
            "vendor_code": vendor_code,
            "barcode_count": 1,
        }
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_nomenclature_items(
               item_id,is_active,is_hidden,nm_id,nomenclature_name,product_type,
               match_key,aliases_json,vendor_code,wb_title,wb_updated_at,
               wb_synced_at,wb_sync_status,wb_sync_evidence_json,created_at,updated_at
           ) VALUES(?,1,0,?,?,?,?,'[]',?,?,?,?,?,?,?,?)""",
        (
            f"repair-nm-{nm_id}",
            nm_id,
            f"Repair SKU {nm_id}",
            "fixture",
            f"repair-{nm_id}",
            vendor_code,
            f"WB SKU {nm_id}",
            NOW,
            NOW,
            "created" if wb_content else "matched_vendor_code",
            json.dumps(evidence, sort_keys=True),
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
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_count(db_path: Path, table: str) -> int:
    with sqlite3.connect(db_path) as conn:
        return _count(conn, table)


def _dense_storage_bytes(db_path: Path) -> dict[str, int]:
    with sqlite3.connect(db_path) as conn:
        intent_bytes = int(
            conn.execute(
                f"SELECT COALESCE(SUM(length(CAST(plan_json AS BLOB))),0) "
                f"FROM {DENSE_INTENTS_TABLE}"
            ).fetchone()[0]
        ) + int(
            conn.execute(
                f"SELECT COALESCE(SUM(length(CAST(receipt_json AS BLOB))),0) "
                f"FROM {DENSE_INTENT_EVENTS_TABLE}"
            ).fetchone()[0]
        )
        manifest_bytes = int(
            conn.execute(
                """SELECT COALESCE(SUM(
                           length(CAST(request_payload_json AS BLOB))+
                           length(CAST(preview_manifest_json AS BLOB))
                       ),0)
                     FROM sheet_vitrina_v1_ff_pool_document_requests
                    WHERE source_type=?""",
                ("ff_pool_dense_fbs_initialization_v1",),
            ).fetchone()[0]
        )
        document_bytes = int(
            conn.execute(
                f"""SELECT COALESCE(SUM(length(CAST(posted_manifest_json AS BLOB))),0)
                      FROM {DOCUMENTS_TABLE} WHERE source_type=?""",
                ("ff_pool_dense_fbs_initialization_v1",),
            ).fetchone()[0]
        ) + int(
            conn.execute(
                f"""SELECT COALESCE(SUM(length(CAST(line.metadata_json AS BLOB))),0)
                      FROM {DOCUMENT_LINES_TABLE} line
                      JOIN {DOCUMENTS_TABLE} document USING(document_id)
                     WHERE document.source_type=?""",
                ("ff_pool_dense_fbs_initialization_v1",),
            ).fetchone()[0]
        )
    return {
        "dense_intent_bytes": intent_bytes,
        "dense_manifest_bytes": manifest_bytes,
        "dense_document_bytes": document_bytes,
    }


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint_rows(db_path: Path, sql: str, parameters: tuple[Any, ...]) -> str:
    with sqlite3.connect(db_path) as conn:
        rows = [list(row) for row in conn.execute(sql, parameters).fetchall()]
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )


if __name__ == "__main__":
    raise SystemExit(main())
