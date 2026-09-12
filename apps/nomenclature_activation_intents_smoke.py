#!/usr/bin/env python3
"""Temporary source crash/revision/consumer proofs; never contacts WB."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.ff_pool_dense_fbs_smoke import (  # noqa: E402
    NOW, _enable_writer, _insert_facility, _sku, _assert_zero,
    _SharedDocumentFactory, _LoseSecondPostBeforeCommit, _AmbiguousAfterCommit,
)
from packages.application import nomenclature_activation_intents as intents  # noqa: E402
from packages.application.ff_pool_dense_fbs import DenseFbsError, DenseFbsService  # noqa: E402
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, LINES_TABLE, REQUESTS_TABLE, FfPoolDocumentService  # noqa: E402
from packages.application.ff_pool_fbs_applicability import DENSE_INTENTS_TABLE  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime, _ensure_schema,
)
from packages.application.wb_supplies import WbSuppliesBlock  # noqa: E402


class Interrupted(BaseException):
    pass


def fixture(path: Path, facilities: int = 2) -> RegistryUploadDbBackedRuntime:
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=path)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        _enable_writer(conn)
        for index in range(facilities):
            _insert_facility(conn, f"f{index}", f"F{index}", active=True)
        conn.commit()
    return runtime


def ordinary(runtime: RegistryUploadDbBackedRuntime) -> dict:
    # Real existing ordinary consumer entry. Other domains have empty temporary
    # inputs; isolate their effects, not the activation/document implementation.
    block = WbSuppliesBlock.__new__(WbSuppliesBlock)
    block.runtime = runtime
    block._ensure_ff_stock_wb_auto_writeoff_checkpoint = lambda **kw: {}
    block.ff_stock_ledger = SimpleNamespace(
        apply_confirmed_wb_supply_returns=lambda: {},
        record_wb_supply_debits=lambda rows: {},
    )
    return block.reconcile_functional_ff_state()


def staged(runtime: RegistryUploadDbBackedRuntime, items: list[dict]) -> None:
    with patch.object(DenseFbsService, "activate_staged_skus", side_effect=Interrupted):
        try:
            runtime.save_nomenclature_items_atomic(items)
        except Interrupted:
            return
    raise AssertionError("expected interruption after source commit")


def sources(runtime: RegistryUploadDbBackedRuntime) -> list[dict]:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(f"SELECT * FROM {intents.TABLE} ORDER BY item_id")]


def counts(runtime: RegistryUploadDbBackedRuntime) -> tuple[int, int]:
    with sqlite3.connect(runtime.db_path) as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in (DOCUMENTS_TABLE, LINES_TABLE))


def saved_plan(runtime: RegistryUploadDbBackedRuntime) -> dict:
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(f"SELECT * FROM {DENSE_INTENTS_TABLE} ORDER BY rowid DESC LIMIT 1").fetchone()
    result = dict(row)
    result["staged_items"] = json.loads(result["plan_json"])["expected_subject"]["staged_items"]
    return result


def resume_old(runtime: RegistryUploadDbBackedRuntime, plan: dict) -> None:
    DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir).activate_staged_skus(
        staged_items=plan["staged_items"], orchestration_key=plan["orchestration_key"],
        request_identity=plan["request_identity"], actor=plan["actor"],
    )


def check_process_crashes() -> dict:
    outcomes = []
    for boundary in ("before_commit", "after_commit"):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = fixture(Path(temporary))
            code = """
import os,sys
from pathlib import Path
from unittest.mock import patch
from apps.ff_pool_dense_fbs_smoke import _sku,NOW
from packages.application import nomenclature_activation_intents as intents
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(sys.argv[1]))
original=intents.record_source_write
def crash(*args,**kwargs):
    original(*args,**kwargs)
    os._exit(71)
if sys.argv[2]=='before_commit':
    with patch.object(intents,'record_source_write',side_effect=crash):
        runtime.save_nomenclature_item(_sku(101,updated_at=NOW))
else:
    with patch.object(DenseFbsService,'activate_staged_skus',side_effect=lambda **kw:os._exit(72)):
        runtime.save_nomenclature_item(_sku(101,updated_at=NOW))
"""
            completed = subprocess.run([sys.executable, "-c", code, temporary, boundary], cwd=ROOT, check=False)
            assert completed.returncode == (71 if boundary == "before_commit" else 72)
            fresh = RegistryUploadDbBackedRuntime(runtime_dir=Path(temporary))
            if boundary == "before_commit":
                assert fresh.load_nomenclature_item("dense-sku-101") is None
                with sqlite3.connect(fresh.db_path) as conn:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (intents.TABLE,)).fetchone()
                    assert not exists or conn.execute(f"SELECT COUNT(*) FROM {intents.TABLE}").fetchone()[0] == 0
            else:
                assert sources(fresh)[0]["status"] == "pending"
                ordinary(fresh)
                assert fresh.load_nomenclature_item("dense-sku-101")["is_active"] is True
                for index in range(2):
                    _assert_zero(fresh.db_path, f"f{index}", 101)
                assert sources(fresh)[0]["status"] == "active"
            outcomes.append(boundary)
    return {"process_exit_boundaries": outcomes}


def check_mixed_and_partial() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        inactive = {**_sku(103, updated_at=NOW), "is_active": False}
        staged(runtime, [_sku(101, updated_at=NOW), _sku(102, updated_at=NOW), inactive])
        assert [item["status"] for item in sources(runtime)] == ["pending", "pending", "inactive"]
        consumer = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
            document_service_factory=_SharedDocumentFactory(_LoseSecondPostBeforeCommit))
        result = intents.drain_nomenclature_activation_intents(runtime, service=consumer)
        assert result["status"] == "pending"
        assert counts(runtime)[0] == 1
        assert [item["status"] for item in sources(runtime)] == ["pending", "pending", "inactive"]
        fresh = RegistryUploadDbBackedRuntime(runtime_dir=Path(temporary))
        ordinary(fresh)
        assert counts(fresh)[0] == 2
        assert [item["status"] for item in sources(fresh)] == ["active", "active", "inactive"]
        before = counts(fresh)
        ordinary(fresh)
        fresh.save_nomenclature_items_atomic([_sku(101, updated_at=NOW), _sku(102, updated_at=NOW), inactive])
        assert counts(fresh) == before
        for index in range(2):
            for nm_id in (101, 102):
                _assert_zero(fresh.db_path, f"f{index}", nm_id)
        assert fresh.load_nomenclature_item(inactive["item_id"])["is_active"] is False
    return {"mixed_staged_inactive": True, "partial_documents": "1/2 pending -> 2/2 active", "duplicate_documents": 0}


def check_stale_and_cancel() -> dict:
    for action in ("inactive_same_timestamp", "delete", "new_revision", "metadata_raw_drift"):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = fixture(Path(temporary))
            staged(runtime, [_sku(101, updated_at=NOW), _sku(102, updated_at=NOW)])
            with patch.object(DenseFbsService, "_materialize", side_effect=Interrupted):
                try:
                    ordinary(runtime)
                except Interrupted:
                    pass
            old = saved_plan(runtime)
            if action == "inactive_same_timestamp":
                runtime.save_nomenclature_item({**_sku(101, updated_at=NOW), "is_active": False})
            elif action == "delete":
                runtime.delete_nomenclature_item("dense-sku-101", updated_at=NOW)
            elif action == "new_revision":
                staged(runtime, [{**_sku(101, updated_at=NOW), "nm_id": 104, "comment": "new revision same timestamp"}])
            else:
                with sqlite3.connect(runtime.db_path) as conn:
                    conn.execute(f"UPDATE {intents.CATALOG} SET comment='out of band' WHERE item_id='dense-sku-101'")
            before = counts(runtime)
            try:
                resume_old(runtime, old)
            except DenseFbsError as exc:
                assert exc.code == "sku_activation_source_superseded"
            else:
                raise AssertionError("old source activation must be rejected")
            assert counts(runtime) == before == (0, 0)
            if action != "metadata_raw_drift":
                ordinary(runtime)
                assert runtime.load_nomenclature_item("dense-sku-102")["is_active"] is True
                assert runtime.load_nomenclature_item("dense-sku-101")["is_active"] is (action == "new_revision")
                if action == "new_revision":
                    assert sources(runtime)[0]["revision"] == 2
                    assert sources(runtime)[0]["status"] == "active"
            else:
                assert ordinary(runtime)["nomenclature_activation"]["status"] == "pending"
                assert counts(runtime) == before
    return {"stale_before_effects": 4, "remaining_cohort_completed": 3}


def check_metadata_and_aba() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        staged(runtime, [_sku(101, updated_at=NOW)])
        stale_read = runtime.load_nomenclature_item("dense-sku-101")
        assert not stale_read["is_active"] and stale_read["activation_status"] == "pending"
        updated = runtime.save_nomenclature_item({**stale_read, "comment": "metadata"}, preserve_staged_activation=True)
        assert updated["is_active"] and updated["activation_source_revision"] == 2
        before = counts(runtime)
        runtime.save_nomenclature_item({**updated, "is_active": False})
        # A delayed metadata writer must preserve the later explicit off choice.
        later = runtime.save_nomenclature_item({**stale_read, "comment": "delayed metadata"}, preserve_staged_activation=True)
        assert not later["is_active"] and later["activation_status"] == "inactive"
        assert counts(runtime) == before
        activated = runtime.save_nomenclature_item(_sku(101, updated_at=NOW))
        assert activated["is_active"] and activated["activation_source_revision"] > 2
        assert counts(runtime) == (before[0] + 2, before[1])  # new explicit activation, no physical delta
    return {"metadata_preserves_staging": True, "delayed_metadata_preserves_manual_off": True, "aba_same_timestamp": True}


def check_ack_atomicity() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        staged(runtime, [_sku(101, updated_at=NOW)])
        original = intents.acknowledge_source
        def stop_after_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise Interrupted
        with patch.object(intents, "acknowledge_source", side_effect=stop_after_ack):
            try:
                ordinary(runtime)
            except Interrupted:
                pass
        assert not runtime.load_nomenclature_item("dense-sku-101")["is_active"]
        assert sources(runtime)[0]["status"] == "pending"
        old = saved_plan(runtime)
        before = counts(runtime)
        assert before[0] == 2
        ordinary(runtime)
        assert sources(runtime)[0]["status"] == "active" and counts(runtime) == before
        # Old source ack cannot close a later requirement, even with the same SKU.
        runtime.save_nomenclature_item({**_sku(101, updated_at=NOW), "is_active": False})
        staged(runtime, [_sku(101, updated_at=NOW)])
        with sqlite3.connect(runtime.db_path) as conn:
            try:
                intents.acknowledge_source(conn, old["staged_items"], intent_id=old["intent_id"])
            except DenseFbsError as exc:
                assert exc.code == "sku_activation_source_ack_drift"
            else:
                raise AssertionError("stale acknowledgment accepted")
            conn.rollback()
        assert sources(runtime)[0]["status"] == "pending"
        ordinary(runtime)
        assert sources(runtime)[0]["status"] == "active" and counts(runtime) == (before[0] + 2, before[1])
    return {"publication_and_source_ack_atomic": True, "stale_ack_rejected": True}


def check_ambiguous_transport() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        staged(runtime, [_sku(101, updated_at=NOW)])
        consumer = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
                                  document_service_factory=_AmbiguousAfterCommit)
        assert intents.drain_nomenclature_activation_intents(runtime, service=consumer)["status"] == "ok"
        assert counts(runtime) == (2, 0) and sources(runtime)[0]["status"] == "active"
        ordinary(runtime)
        assert counts(runtime) == (2, 0)
    return {"ambiguous_transport_readback_only": True}


def check_direct_canonical_resume() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        staged(runtime, [_sku(101, updated_at=NOW)])
        consumer = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
            document_service_factory=_SharedDocumentFactory(_LoseSecondPostBeforeCommit))
        assert intents.drain_nomenclature_activation_intents(runtime, service=consumer)["status"] == "pending"
        assert counts(runtime) == (1, 0)
        runtime.save_nomenclature_item({**_sku(101, updated_at=NOW), "is_active": False})
        with sqlite3.connect(runtime.db_path) as conn:
            request_id = conn.execute(f"SELECT request_id FROM {REQUESTS_TABLE} WHERE state='ready'").fetchone()[0]
        result = FfPoolDocumentService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, resume=False).post(request_id)
        assert result["state"] == "blocked" and result["error"]["code"] == "sku_activation_source_superseded"
        assert counts(runtime) == (1, 0)
        assert not runtime.load_nomenclature_item("dense-sku-101")["is_active"]
    return {"direct_ready_document_stale_resume_blocked": True}


def check_metadata_writers() -> dict:
    from packages.application.supplier_shipments import SupplierShipmentsBlock, _normalize_nomenclature_import_row

    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        item = {**_sku(101, updated_at=NOW), "match_key": "fixture|iphone16", "barcode": "4600000000101"}
        staged(runtime, [item])
        block = SupplierShipmentsBlock.__new__(SupplierShipmentsBlock)
        block.runtime = runtime
        block.timestamp_factory = lambda: NOW
        block._validate_nomenclature_group = lambda *args, **kwargs: None
        block._validate_nomenclature_unique = lambda *args, **kwargs: None
        block._sync_nomenclature_barcode_item = lambda item, **kwargs: (item, {})
        result = block.update_nomenclature_item("dense-sku-101", {"comment": "metadata HTTP"})
        assert result["item"]["is_active"] is True
        result = block.update_nomenclature_item("dense-sku-101", {"is_active": False})
        assert result["item"]["is_active"] is False
        staged(runtime, [item])
        existing = runtime.load_nomenclature_item("dense-sku-101")
        for explicit, expected in (({}, True), ({"is_active": "нет"}, False)):
            operation = _normalize_nomenclature_import_row(
                {"item_id": existing["item_id"], "nomenclature_name": "Metadata import", **explicit},
                row_number=2, existing_by_id={existing["item_id"]: existing},
                active_by_match_key={}, sku_groups=[{"group_key": "fixture", "is_active": True}], now=NOW,
            )
            assert operation["item"]["is_active"] is expected
            assert operation["preserve_staged_activation"] is expected
    return {"http_explicit_off_vs_metadata": True, "import_explicit_off_vs_metadata": True}


def check_bounded_fairness() -> dict:
    with tempfile.TemporaryDirectory() as temporary:
        runtime = fixture(Path(temporary))
        for nm_id in (101, 102, 103):
            staged(runtime, [_sku(nm_id, updated_at=NOW)])
        attempted = []
        def unavailable(**kwargs):
            attempted.append(kwargs["staged_items"][0]["item_id"])
            raise DenseFbsError("temporary_fixture_blocker", "not ready")
        for _ in range(3):
            assert intents.drain_nomenclature_activation_intents(runtime,
                service=SimpleNamespace(activate_staged_skus=unavailable), batch_limit=1)["status"] == "pending"
        assert len(set(attempted)) == 3 and counts(runtime) == (0, 0)
        ordinary(runtime)
        assert all(row["status"] == "active" for row in sources(runtime))
    return {"bounded_consumer_no_starvation": True}


def check_legacy_source_boundary() -> dict:
    changes = ("delete_new_timestamp", "delete_same_timestamp", "hidden", "nm_id", "updated_at", "is_active", "missing", "healthy", "cancel_rollback")
    for change in changes:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = fixture(Path(temporary))
            # The old source contract committed an inactive row, then handed
            # Dense only item_id/nm_id/updated_at, without a source revision.
            with patch.object(intents, "record_source_write"):
                runtime.save_nomenclature_item(_sku(101, updated_at=NOW))
            items = [{"item_id": "dense-sku-101", "nm_id": 101, "updated_at": NOW}]
            identity = intents._fingerprint(items)
            consumer = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir,
                document_service_factory=_SharedDocumentFactory(_LoseSecondPostBeforeCommit))
            try:
                consumer.activate_staged_skus(staged_items=items, orchestration_key="sku-activation:" + identity,
                    request_identity=identity, actor="registry_nomenclature_write")
            except DenseFbsError:
                pass
            else:
                raise AssertionError("legacy partial fault absent")
            assert counts(runtime) == (1, 0)
            old = saved_plan(runtime)
            assert "source_revision" not in old["staged_items"][0]
            with sqlite3.connect(runtime.db_path) as conn:
                assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (intents.TABLE,)).fetchone()
                request_id = conn.execute(f"SELECT request_id FROM {REQUESTS_TABLE} WHERE state='ready'").fetchone()[0]
            if change == "cancel_rollback":
                original = intents.cancel_source_activation
                def interrupted_cancel(*args):
                    original(*args)
                    raise Interrupted
                with patch.object(intents, "cancel_source_activation", side_effect=interrupted_cancel):
                    try:
                        runtime.delete_nomenclature_item("dense-sku-101", updated_at="2026-08-26T08:01:00Z")
                    except Interrupted:
                        pass
                assert runtime.load_nomenclature_item("dense-sku-101")["updated_at"] == NOW
                with sqlite3.connect(runtime.db_path) as conn:
                    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (intents.TABLE,)).fetchone()
            if change in {"healthy", "cancel_rollback"}:
                service = DenseFbsService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir)
                arguments = dict(staged_items=items, orchestration_key=old["orchestration_key"],
                                 request_identity=identity, actor=old["actor"])
                assert service.activate_staged_skus(**arguments)["state"] == "active"
                assert service.activate_staged_skus(**arguments)["idempotent"] is True
                assert counts(runtime) == (2, 0)
                continue
            if change.startswith("delete_"):
                timestamp = NOW if change == "delete_same_timestamp" else "2026-08-26T08:01:00Z"
                runtime.delete_nomenclature_item("dense-sku-101", updated_at=timestamp)
                tombstone = sources(runtime)
                assert len(tombstone) == 1 and tombstone[0]["status"] == "cancelled"
                assert tombstone[0]["source_updated_at"] == timestamp
            else:
                with sqlite3.connect(runtime.db_path) as conn:
                    updates = {"hidden": "is_hidden=1", "nm_id": "nm_id=102", "updated_at": "updated_at='2026-08-26T08:01:00Z'", "is_active": "is_active=1"}
                    if change == "missing":
                        conn.execute(f"DELETE FROM {intents.CATALOG} WHERE item_id='dense-sku-101'")
                    else:
                        conn.execute(f"UPDATE {intents.CATALOG} SET {updates[change]} WHERE item_id='dense-sku-101'")
            with sqlite3.connect(runtime.db_path) as conn:
                before = conn.execute(f"SELECT * FROM {intents.CATALOG}").fetchall()
            result = FfPoolDocumentService(db_path=runtime.db_path, runtime_dir=runtime.runtime_dir, resume=False).post(request_id)
            assert result["state"] == "blocked" and result["error"]["code"] == "sku_activation_source_superseded"
            try:
                resume_old(runtime, old)
            except DenseFbsError as exc:
                assert exc.code == "sku_activation_source_superseded"
            else:
                raise AssertionError("stale legacy Dense resume accepted")
            with sqlite3.connect(runtime.db_path) as conn:
                assert conn.execute(f"SELECT * FROM {intents.CATALOG}").fetchall() == before
            assert counts(runtime) == (1, 0)
    return {"legacy_stale_ready_and_dense_blocked": 7, "legacy_delete_tombstones": 2,
            "healthy_legacy_resume_and_completed_retry": True, "legacy_cancel_and_tombstone_atomic": True}


def main() -> int:
    with patch("socket.create_connection", side_effect=AssertionError("external network forbidden")) as network:
        result = {**check_process_crashes(), **check_mixed_and_partial(),
                  **check_stale_and_cancel(), **check_metadata_and_aba(), **check_ack_atomicity(),
                  **check_ambiguous_transport(), **check_direct_canonical_resume(), **check_metadata_writers(),
                  **check_bounded_fairness(), **check_legacy_source_boundary()}
        assert network.call_count == 0
    print(json.dumps({"status": "ok", "external_calls": 0, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
