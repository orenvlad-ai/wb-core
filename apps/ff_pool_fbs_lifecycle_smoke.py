#!/usr/bin/env python3
"""Exact historical and post-T FBS lifecycle integration smoke."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ff_pool_cutover_production_smoke import (  # noqa: E402
    GATE_AT,
    SHA,
    SHIPMENT_ID,
    _Clock,
    _barrier,
    _seed,
)
from packages.application.ff_pool_cutover import read_ff_pool_cutover_status  # noqa: E402
from packages.application.ff_pool_cutover_production import (  # noqa: E402
    FfPoolCutoverProductionMutation,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    EVENTS_TABLE,
    RECONCILIATION_TABLE,
    available_quantity,
    process_post_t_fbs_lifecycle,
)
from packages.application.ff_pool_foundation import read_ff_pool_feature_state  # noqa: E402
from packages.application.ff_pool_documents import FfPoolDocumentService  # noqa: E402
from packages.application.ff_pool_surfaces import FfPoolSurface  # noqa: E402
from packages.contracts.ff_pool_documents import DocumentIdentity  # noqa: E402
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
    _ensure_schema,
)
from packages.application.warehouse_functional import (  # noqa: E402
    ensure_warehouse_functional_schema,
)


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime_dir = root / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        runtime_dir.mkdir(parents=True)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_warehouse_functional_schema(conn)
            _ensure_schema(conn)
            _seed(conn)
            _append_status(
                conn,
                order_id=9001,
                revision="order_revision_9001",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-14T04:04:00Z",
                insert_current=False,
            )
            _append_status(
                conn,
                order_id=9001,
                revision="order_revision_9001_cancelled",
                supplier_status="complete",
                wb_status="canceled_by_client",
                episode=3,
                observed_at="2026-08-14T04:05:00Z",
                insert_current=False,
            )
            conn.commit()
        env_file = root / "runtime.env"
        env_file.write_text("WB_FBS_COLLECTOR_ENABLED=true\n", encoding="utf-8")
        runner = FfPoolCutoverProductionMutation(
            runtime_dir=runtime_dir,
            env_file=env_file,
            deployed_sha=SHA,
            timestamp_factory=_Clock(),
        )
        gate = runner.build_gate_plan(excluded_shipment_ids=[SHIPMENT_ID])
        historical = gate["source"]["historical_fbs_summary"]
        assert historical["counts"]["pre_t_handoff_debit"] == 1
        assert historical["quantities"]["pre_t_handoff_debit"] == 1
        assert historical["debit_capital_rub"] == "10"
        assert historical["post_handoff_reconciliation_count"] == 1
        applied = runner.apply(
            gate,
            fingerprint=gate["fingerprint"],
            approval_reference="owner-gate-lifecycle",
            actor="smoke",
            backup_dir=root / "backups",
            external_barrier_evidence=_barrier(),
        )
        assert applied["status"] == "applied_reconciled"
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT quantity FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()[0] == 9
            assert conn.execute(
                "SELECT quantity FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id='wf_stage7c' AND warehouse_key='ff' AND nm_id=101"
            ).fetchone()[0] == "9"
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} WHERE event_type='opening_handoff_debit'"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {EVENTS_TABLE} "
                "WHERE event_type='post_handoff_reconciliation' AND order_id=9001"
            ).fetchone()[0] == 1
            assert conn.execute(
                f"SELECT COUNT(*) FROM {RECONCILIATION_TABLE} WHERE order_id=9001"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_cutover_order_status_evidence "
                "WHERE order_id=9001"
            ).fetchone()[0] == 2

        # Eleven active post-T orders create reservations only.  Available may
        # be negative, but physical and capital remain untouched.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            for order_id in range(9200, 9211):
                _insert_post_t_order(conn, order_id=order_id, supplier="new", wb="waiting")
            conn.commit()
        before_wb = _wb_evidence_digest(runtime.db_path)
        processed = _process(runtime.db_path, "2026-08-14T06:10:00Z")
        assert processed["summary"]["reserved"] == 11
        assert _wb_evidence_digest(runtime.db_path) == before_wb
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            available = available_quantity(
                conn, cutover_id=_cutover_id(conn), facility_id="fac_moscow", nm_id=101
            )
            assert available == {"physical": 9, "reserved": 11, "available": -2}

        # A quantity correction before handoff refreshes the exact reservation
        # from immutable status evidence without changing physical stock.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9202,
                revision="post_revision_9202_v2",
                supplier_status="new",
                wb_status="waiting",
                episode=2,
                observed_at="2026-08-14T06:10:30Z",
                insert_current=True,
                quantity=3,
            )
            conn.commit()
        refreshed = _process(runtime.db_path, "2026-08-14T06:10:45Z")
        assert refreshed["summary"]["reservation_refreshed"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            available = available_quantity(
                conn, cutover_id=_cutover_id(conn), facility_id="fac_moscow", nm_id=101
            )
            assert available == {"physical": 9, "reserved": 13, "available": -4}

        # A pre-handoff cancellation releases only the reservation.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9200,
                revision="post_revision_9200_v2",
                supplier_status="cancel",
                wb_status="waiting",
                episode=2,
                observed_at="2026-08-14T06:11:00Z",
                insert_current=True,
            )
            conn.commit()
        before_wb = _wb_evidence_digest(runtime.db_path)
        released = _process(runtime.db_path, "2026-08-14T06:12:00Z")
        assert released["summary"]["released"] == 1
        assert _wb_evidence_digest(runtime.db_path) == before_wb

        # WB-controlled complete/sorted fulfills once with frozen opening WAC.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v2",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-14T06:13:00Z",
                insert_current=True,
            )
            conn.commit()
        handed = _process(runtime.db_path, "2026-08-14T06:14:00Z")
        assert handed["summary"]["fulfilled"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            physical = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            assert tuple(physical) == (8, "80")
            operation_count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0]
            feature = read_ff_pool_feature_state(conn, aggregate_revision="wf_stage7c")
            assert feature.reader_effective is True

        # Later sold/closed is a no-op; later cancellation is evidence for a
        # separate reconciliation lane and never silently returns stock.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v3",
                supplier_status="complete",
                wb_status="sold",
                episode=3,
                observed_at="2026-08-14T06:15:00Z",
                insert_current=True,
            )
            conn.commit()
        terminal = _process(runtime.db_path, "2026-08-14T06:16:00Z")
        assert terminal["summary"]["terminal_noop"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0] == operation_count
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v4",
                supplier_status="complete",
                wb_status="canceled_by_client",
                episode=4,
                observed_at="2026-08-14T06:17:00Z",
                insert_current=True,
            )
            conn.commit()
        reconciled = _process(runtime.db_path, "2026-08-14T06:18:00Z")
        assert reconciled["summary"]["reconciliation"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {RECONCILIATION_TABLE}").fetchone()[0] == 2
            assert conn.execute(
                "SELECT quantity FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()[0] == 8

        # Reordered waiting→sorted evidence after fulfillment is consumed by
        # exact status sequence but never creates a second physical debit.
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v5",
                supplier_status="complete",
                wb_status="waiting",
                episode=5,
                observed_at="2026-08-14T06:18:30Z",
                insert_current=True,
            )
            _append_status(
                conn,
                order_id=9201,
                revision="post_revision_9201_v6",
                supplier_status="complete",
                wb_status="sorted",
                episode=6,
                observed_at="2026-08-14T06:18:40Z",
                insert_current=True,
            )
            conn.commit()
        reordered = _process(runtime.db_path, "2026-08-14T06:19:00Z")
        assert reordered["summary"]["status_noop"] == 2
        duplicate_retry = _process(runtime.db_path, "2026-08-14T06:19:30Z")
        assert duplicate_retry["processed_count"] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_warehouse_business_operations"
            ).fetchone()[0] == operation_count

        # A late-arriving order locally observed at/before T is isolated;
        # it neither double-debits nor globally blocks post-T processing.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn,
                order_id=9300,
                supplier="complete",
                wb="sorted",
                source_created_at="2026-08-14T05:04:00Z",
                observed_at=GATE_AT,
            )
            conn.commit()
        late = _process(runtime.db_path, "2026-08-14T06:20:00Z")
        assert late["summary"]["late_pre_t"] == 1
        repeated = _process(runtime.db_path, "2026-08-14T06:21:00Z")
        assert repeated["summary"]["fulfilled"] == 0
        assert repeated["summary"]["late_pre_t"] == 0
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            status = read_ff_pool_cutover_status(conn)
            assert status["readback"]["status"] == "pass"
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_pool_cutover_late_pre_t_cases"
            ).fetchone()[0] == 1

        # The manifest-pinned shipment remains in transit through cutover and
        # can later be accepted exactly once by the guided Migration 139 flow.
        # Exact invoice evidence may arrive after opening.  It is still not a
        # receipt/cost layer and therefore does not retroactively change the
        # cutover; it gives the guided acceptance an exact supplier-capital
        # basis for the two physically received units.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipments
                   SET approx_yuan_rate='10',
                       product_qty_total='66000',
                       product_amount_total='66000',
                       extras_amount_total='0',
                       invoice_amount_total='66000',
                       updated_at='2026-08-15T04:00:00Z'
                   WHERE shipment_id=?""",
                (SHIPMENT_ID,),
            )
            conn.execute(
                """UPDATE sheet_vitrina_v1_supplier_shipment_lines
                   SET unit_price='1', amount='66000', currency='CNY',
                       invoice_price_yuan_snapshot='1',
                       reference_purchase_price_yuan_snapshot='1'
                   WHERE shipment_id=? AND line_type='product'""",
                (SHIPMENT_ID,),
            )
            conn.commit()
        service = FfPoolDocumentService(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=_DocClock(),
        )
        _shipment, _shipment_lines, shipment_revision = FfPoolSurface(
            db_path=runtime.db_path,
            runtime_dir=runtime_dir,
            timestamp_factory=_DocClock(),
        ).supplier_shipment_source(SHIPMENT_ID)
        identity = DocumentIdentity(
            request_id="guided:26gn527:request",
            source_system="operator_ui",
            source_type="china_acceptance_workbook",
            source_id=SHIPMENT_ID,
            source_revision=shipment_revision,
            idempotency_epoch=1,
            actor="warehouse-operator",
            business_date="2026-08-15",
        )
        preview = service.accept_preview(
            identity=identity,
            document_kind="china_acceptance",
            manifest={
                "facility_id": "fac_moscow",
                "source_revision": shipment_revision,
                "allocations": [
                    {
                        "nm_id": 101,
                        "expected_quantity": 66_000,
                        "accepted_quantity": 2,
                        "quantity_fbs": 1,
                        "quantity_fbo": 1,
                        "accepted_capital_rub": "20.00",
                        "discrepancy_type": "shortage",
                        "discrepancy_quantity": 65_998,
                        "identity_evidence_digest": "sha256:" + "9" * 64,
                    }
                ],
                "expenses": [
                    {
                        "amount_rub": "2.00",
                        "basis": "Фактическая приёмка",
                        "metadata": {"allocation_scope": "both"},
                    }
                ],
            },
        )
        assert preview["state"] == "ready", preview
        posted = service.post(str(preview["request_id"]))
        assert posted["state"] == "complete", posted
        repeated_acceptance = service.post(str(preview["request_id"]))
        assert repeated_acceptance["state"] == "complete"
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            shipment = conn.execute(
                "SELECT actual_ff_acceptance_date,order_status FROM sheet_vitrina_v1_supplier_shipments "
                "WHERE shipment_id=?",
                (SHIPMENT_ID,),
            ).fetchone()
            assert tuple(shipment) == ("2026-08-15", "accepted_ff")
            assert conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_ff_stock_operations "
                "WHERE source_key=?",
                (f"supplier_shipment_acceptance:{SHIPMENT_ID}",),
            ).fetchone()[0] == 1
            fbs = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            fbo = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBO' AND nm_id=101"
            ).fetchone()
            assert int(fbs[0]) == 9 and Decimal(str(fbs[1])) == Decimal("91")
            assert int(fbo[0]) == 1 and Decimal(str(fbo[1])) == Decimal("11")
            assert read_ff_pool_feature_state(
                conn, aggregate_revision="wf_stage7c"
            ).reader_effective is True
            guided_readback = read_ff_pool_cutover_status(conn)["readback"]
            assert guided_readback["status"] == "pass", guided_readback

        # Quantity comes from immutable official status evidence; it is never
        # approximated as one unit merely because one order row is present.
        with sqlite3.connect(runtime.db_path) as conn:
            _insert_post_t_order(
                conn, order_id=9400, supplier="new", wb="waiting", quantity=3
            )
            conn.commit()
        exact_reserved = _process(runtime.db_path, "2026-08-15T08:10:00Z")
        assert exact_reserved["summary"]["reserved"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            _append_status(
                conn,
                order_id=9400,
                revision="post_revision_9400_v2",
                supplier_status="complete",
                wb_status="sorted",
                episode=2,
                observed_at="2026-08-15T08:11:00Z",
                insert_current=True,
                quantity=3,
            )
            conn.commit()
        exact_handoff = _process(runtime.db_path, "2026-08-15T08:12:00Z")
        assert exact_handoff["summary"]["fulfilled"] == 1
        with sqlite3.connect(runtime.db_path) as conn:
            exact_balance = conn.execute(
                "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
                "WHERE facility_id='fac_moscow' AND pool='FBS' AND nm_id=101"
            ).fetchone()
            assert int(exact_balance[0]) == 6
            assert Decimal(str(exact_balance[1])) == Decimal("61")
            assert read_ff_pool_cutover_status(conn)["readback"]["status"] == "pass"
    print("ff_pool_fbs_lifecycle_smoke: OK")
    return 0


def _insert_post_t_order(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    supplier: str,
    wb: str,
    source_created_at: str = "2026-08-14T06:00:00Z",
    observed_at: str = "2026-08-14T06:01:00Z",
    quantity: int = 1,
) -> None:
    revision = f"post_revision_{order_id}_v1"
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
               observation_id,order_id,source_revision,supply_id,delivery_type,
               source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
               skus_json,observed_at,collector_date_from,collector_date_to,collector_cursor
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"post_observation_{order_id}", order_id, revision, "post-supply", "fbs",
            source_created_at, 501, 601, 101, 201, "seller-101", '["sku-101"]',
            observed_at, 1, 2, 0,
        ),
    )
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
               evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
               barcode,seller_sku,outcome,warehouse_mapping_id,identity_mapping_id,
               evidence_digest,observed_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            f"post_identity_evidence_{order_id}", order_id, revision, 501, 101, 201,
            "sku-101", "seller-101", "matched", "warehouse_mapping_1",
            "identity_mapping_1",
            "sha256:" + hashlib.sha256(f"identity:{order_id}".encode()).hexdigest(),
            observed_at,
        ),
    )
    _append_status(
        conn,
        order_id=order_id,
        revision=revision,
        supplier_status=supplier,
        wb_status=wb,
        episode=1,
        observed_at=observed_at,
        insert_current=True,
        quantity=quantity,
    )


class _DocClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat(timespec="seconds").replace("+00:00", "Z")
        self.value += timedelta(seconds=1)
        return result


def _append_status(
    conn: sqlite3.Connection,
    *,
    order_id: int,
    revision: str,
    supplier_status: str,
    wb_status: str,
    episode: int,
    observed_at: str,
    insert_current: bool,
    quantity: int = 1,
) -> None:
    existing_order = conn.execute(
        """SELECT 1 FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
           WHERE order_id=? AND source_revision=?""",
        (order_id, revision),
    ).fetchone()
    if existing_order is None:
        prior = conn.execute(
            """SELECT supply_id,delivery_type,source_created_at,warehouse_id,office_id,
                      nm_id,chrt_id,seller_sku,skus_json,collector_date_from,
                      collector_date_to,collector_cursor
               FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations
               WHERE order_id=? ORDER BY observation_sequence DESC LIMIT 1""",
            (order_id,),
        ).fetchone()
        if prior is None:
            raise AssertionError(f"missing order source for status revision {order_id}/{revision}")
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_order_observations(
                   observation_id,order_id,source_revision,supply_id,delivery_type,
                   source_created_at,warehouse_id,office_id,nm_id,chrt_id,seller_sku,
                   skus_json,observed_at,collector_date_from,collector_date_to,
                   collector_cursor
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"status_order_{order_id}_{episode}", order_id, revision,
                *tuple(prior[:9]), observed_at, *tuple(prior[9:]),
            ),
        )
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_identity_evidence(
                   evidence_id,order_id,order_revision,warehouse_id,nm_id,chrt_id,
                   barcode,seller_sku,outcome,warehouse_mapping_id,
                   identity_mapping_id,evidence_digest,observed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f"status_identity_{order_id}_{episode}", order_id, revision,
                int(prior[3]), int(prior[5]), int(prior[6]), "sku-101",
                str(prior[7]), "matched", "warehouse_mapping_1",
                "identity_mapping_1",
                "sha256:" + hashlib.sha256(
                    f"status-identity:{order_id}:{revision}".encode()
                ).hexdigest(),
                observed_at,
            ),
        )
    digest = "sha256:" + hashlib.sha256(
        f"{order_id}:{revision}:{supplier_status}:{wb_status}:{quantity}".encode("utf-8")
    ).hexdigest()
    conn.execute(
        """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_observations(
               observation_id,order_id,order_revision,status_digest,supplier_status,
               wb_status,positive_quantity,observed_at
           ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            f"status_{order_id}_{episode}", order_id, revision, digest,
            supplier_status, wb_status, quantity, observed_at,
        ),
    )
    if insert_current:
        conn.execute(
            """INSERT INTO sheet_vitrina_v1_wb_supplies_fbs_status_current(
                   order_id,order_revision,status_digest,supplier_status,wb_status,
                   source_observed_at,local_first_seen_at,local_last_seen_at,
                   observation_count,episode_sequence
               ) VALUES(?,?,?,?,?,'',?,?,?,?)
               ON CONFLICT(order_id) DO UPDATE SET
                   order_revision=excluded.order_revision,
                   status_digest=excluded.status_digest,
                   supplier_status=excluded.supplier_status,
                   wb_status=excluded.wb_status,
                   local_last_seen_at=excluded.local_last_seen_at,
                   observation_count=excluded.observation_count,
                   episode_sequence=excluded.episode_sequence""",
            (
                order_id, revision, digest, supplier_status, wb_status,
                observed_at, observed_at, episode, episode,
            ),
        )


def _process(path: Path, timestamp: str) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        result = process_post_t_fbs_lifecycle(
            conn, occurred_at=timestamp, schema_ready=True
        )
        conn.commit()
        return result


def _cutover_id(conn: sqlite3.Connection) -> str:
    return str(
        conn.execute(
            "SELECT cutover_id FROM sheet_vitrina_v1_ff_pool_cutover_manifests"
        ).fetchone()[0]
    )


def _wb_evidence_digest(path: Path) -> str:
    with sqlite3.connect(path) as conn:
        payload = {
            "orders": conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_order_observations ORDER BY observation_sequence"
            ).fetchall(),
            "statuses": conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_status_observations ORDER BY observation_sequence"
            ).fetchall(),
            "current": conn.execute(
                "SELECT * FROM sheet_vitrina_v1_wb_supplies_fbs_status_current ORDER BY order_id"
            ).fetchall(),
        }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
