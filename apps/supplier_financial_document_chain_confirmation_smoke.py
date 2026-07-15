"""Exact invoice-136 confirmation inside supplier/publication reconciliation."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.supplier_shipment_factual_date_correction_smoke import (  # noqa: E402
    DOCUMENT_ID,
    HISTORICAL_SHIPMENT_ID,
    SHIPMENT_ID,
    _runtime_fixture,
)
from packages.application.registry_upload_db_backed_runtime import _connect  # noqa: E402
from packages.application.own_product_capital import _ensure_own_capital_schema  # noqa: E402
from packages.application.canonical_cost_engine import (  # noqa: E402
    CanonicalCostBlocked,
    CanonicalCostEngine,
)
from packages.application.supplier_financial_document_exact_policy import (  # noqa: E402
    AUTHORIZED_FINANCIAL_DOCUMENT_CONFIRMATION_IDENTITY,
)
from packages.application.supplier_shipment_factual_correction import (  # noqa: E402
    SupplierShipmentFactualCorrectionBlock,
)


FINANCIAL_DOCUMENT_ID = "fdoc_883d0528332c4900aa92348a45163b48"
EXPENSE_LINE_ID = "fline_d0d1fe10dd8f410da3e3d785a914c839"


def main() -> int:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = _runtime_fixture(root / "runtime")
        _insert_invoice_136(runtime.db_path)
        _insert_volatile_non_target_document(runtime.db_path)
        historical_change = {
            "shipment_id": HISTORICAL_SHIPMENT_ID,
            "action": "activate",
            "exception_code": "legacy_ff_accepted_without_date",
            "expected_invoice_no": "26GN237",
            "expected_invoice_date": "2026-03-29",
            "expected_shipment_date": "2026-05-22",
            "reason": "legacy_ff_accepted_without_known_factual_date",
            "provenance": "operator_confirmed_historical_registry_state",
        }
        confirmation = {"document_id": FINANCIAL_DOCUMENT_ID}
        block = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-15T16:30:00Z",
        )
        common = {
            "shipment_id": SHIPMENT_ID,
            "new_actual_shipment_date": "2026-06-25",
            "actor": "invoice-confirmation-smoke",
            "expected_invoice_no": "26GN390",
            "expected_invoice_document_id": DOCUMENT_ID,
            "historical_status_change": historical_change,
        }
        without_confirmation = block.dry_run(**common)
        first = block.dry_run(
            **common,
            financial_document_confirmation=confirmation,
        )
        second = block.dry_run(
            **common,
            financial_document_confirmation=confirmation,
        )
        assert first["fingerprint"] == second["fingerprint"]
        assert first["request_fingerprint"] == second["request_fingerprint"]
        assert first["dependency_closure_digest"] == second["dependency_closure_digest"]
        assert first["candidate_canonical_digest"] == second["candidate_canonical_digest"]
        assert first["candidate_canonical_digest"] != without_confirmation["candidate_canonical_digest"]
        plan = first["financial_document_confirmation"]
        assert plan["previous_status"] == "parsed"
        assert plan["new_status"] == "confirmed"
        assert plan["document_id"] == FINANCIAL_DOCUMENT_ID
        assert plan["accounting_proof"]["expense_amount_rub"] == "1075030"
        assert abs(Decimal(plan["accounting_proof"]["rounding_delta_rub"])) <= Decimal(
            plan["accounting_proof"]["rounding_tolerance_rub"]
        )
        assert plan["accounting_proof"]["event_count"] == 27
        assert plan["accounting_proof"]["unique_nm_id_count"] == 27
        assert plan["accounting_proof"]["quantity_delta"] == "0"
        assert plan["accounting_proof"]["new_event_count"] == 0
        assert plan["accounting_proof"]["all_allocations_match"] is True
        canonical_effect = first["financial_document_canonical_after"]
        assert canonical_effect["component_count"] == 27
        assert canonical_effect["unique_component_identity_count"] == 27
        assert canonical_effect["unique_nm_id_count"] == 27
        assert (
            canonical_effect["recognized_capital_rub"]
            == plan["accounting_proof"]["stored_event_capital_rub"]
        )
        assert (
            canonical_effect["paid_capital_rub"]
            == plan["accounting_proof"]["stored_event_capital_rub"]
        )
        assert canonical_effect["paid_equivalent_quantity"] == "0"
        stage_with = first["reconciliation"]["target_after"]["2026-07-15"][
            "PRODUCTION_TO_FF"
        ]
        stage_without = without_confirmation["reconciliation"]["target_after"][
            "2026-07-15"
        ]["PRODUCTION_TO_FF"]
        expected_capital = Decimal(plan["accounting_proof"]["stored_event_capital_rub"])
        assert abs(
            Decimal(str(stage_with["recognized_capital_rub"]))
            - Decimal(str(stage_without["recognized_capital_rub"]))
            - expected_capital
        ) <= Decimal("0.000027")
        assert abs(
            Decimal(str(stage_with["paid_capital_rub"]))
            - Decimal(str(stage_without["paid_capital_rub"]))
            - expected_capital
        ) <= Decimal("0.000027")
        assert stage_with["physical_quantity"] == stage_without["physical_quantity"]
        assert (
            stage_with["paid_equivalent_quantity"]
            == stage_without["paid_equivalent_quantity"]
        )
        assert first["rebuild"]["second_run_changed"] == 0

        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_financial_documents "
                "SET updated_at='2026-07-15T16:31:00Z' WHERE document_id=?",
                (FINANCIAL_DOCUMENT_ID,),
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_financial_documents "
                "SET updated_at='2026-07-15T16:31:01Z' WHERE document_id=?",
                ("fdoc_volatile_read_refresh",),
            )
            conn.commit()
        after_volatile_touch = block.dry_run(
            **common,
            financial_document_confirmation=confirmation,
        )
        assert after_volatile_touch["fingerprint"] == first["fingerprint"]
        assert (
            after_volatile_touch["financial_document_confirmation"]["evidence_fingerprint"]
            == plan["evidence_fingerprint"]
        )
        try:
            block.dry_run(
                **common,
                financial_document_confirmation={"document_id": "fdoc_not_authorized"},
            )
        except ValueError as exc:
            assert "exact-only" in str(exc)
        else:
            raise AssertionError("non-exact financial document unexpectedly passed")

        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_financial_expense_lines "
                "SET amount_rub=amount_rub+1 WHERE line_id=?",
                (EXPENSE_LINE_ID,),
            )
            conn.commit()
        try:
            block.dry_run(**common, financial_document_confirmation=confirmation)
        except ValueError as exc:
            assert "amount_rub" in str(exc)
        else:
            raise AssertionError("semantic financial drift unexpectedly passed")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_financial_expense_lines "
                "SET amount_rub=amount_rub-1 WHERE line_id=?",
                (EXPENSE_LINE_ID,),
            )
            conn.commit()

        with _connect(runtime.db_path) as conn:
            event_id = conn.execute(
                "SELECT event_id FROM sheet_vitrina_v1_own_capital_events "
                "WHERE event_id LIKE ? ORDER BY event_id LIMIT 1",
                (f"cost_payment:financial_expense:{FINANCIAL_DOCUMENT_ID}:%",),
            ).fetchone()[0]
            conn.execute(
                "UPDATE sheet_vitrina_v1_own_capital_events SET quantity='1' "
                "WHERE event_id=?",
                (event_id,),
            )
            conn.commit()
        try:
            block.dry_run(**common, financial_document_confirmation=confirmation)
        except ValueError as exc:
            assert "event drift: quantity" in str(exc)
        else:
            raise AssertionError("quantity-bearing cost event unexpectedly passed")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_own_capital_events SET quantity='0' "
                "WHERE event_id=?",
                (event_id,),
            )
            conn.commit()

        before = _financial_state(runtime.db_path)

        def fail_after_commit(phase: str) -> None:
            if phase == "before_post_verify":
                raise RuntimeError("synthetic invoice confirmation verification failure")

        failing = SupplierShipmentFactualCorrectionBlock(
            runtime=runtime,
            timestamp_factory=lambda: "2026-07-15T16:30:00Z",
            failure_injector=fail_after_commit,
        )
        try:
            failing.apply(
                **common,
                financial_document_confirmation=confirmation,
                fingerprint=first["fingerprint"],
                backup_dir=root / "failed-backups",
            )
        except RuntimeError as exc:
            assert "synthetic" in str(exc)
        else:
            raise AssertionError("post-commit failure unexpectedly succeeded")
        assert _financial_state(runtime.db_path) == before

        applied = block.apply(
            **common,
            financial_document_confirmation=confirmation,
            fingerprint=first["fingerprint"],
            backup_dir=root / "backups",
        )
        assert applied["applied"] is True
        assert applied["post_run"]["changed"] == 0
        after = _financial_state(runtime.db_path)
        assert after["parse_status"] == "confirmed"
        assert after["event_count"] == before["event_count"] == 27
        assert after["event_capital"] == before["event_capital"]
        assert after["event_quantity"] == before["event_quantity"] == "0"
        assert after["event_ids"] == before["event_ids"]
        assert after["correction_evidence_fingerprint"] == plan["evidence_fingerprint"]

        with _connect(runtime.db_path) as conn:
            event_id = after["event_ids"][0]
            conn.execute(
                "UPDATE sheet_vitrina_v1_own_capital_events SET quantity='1' "
                "WHERE event_id=?",
                (event_id,),
            )
            conn.commit()
        try:
            CanonicalCostEngine(runtime=runtime).rebuild(
                date_from="2026-07-01", date_to="2026-07-15"
            )
        except CanonicalCostBlocked as exc:
            assert exc.code == "exact_supplier_expense_event_accounting_drift"
        else:
            raise AssertionError("canonical exact expense drift unexpectedly passed")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_own_capital_events SET quantity='0' "
                "WHERE event_id=?",
                (event_id,),
            )
            conn.commit()

        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_financial_expense_lines "
                "SET amount_rub=0 WHERE line_id=?",
                (EXPENSE_LINE_ID,),
            )
            conn.commit()
        try:
            CanonicalCostEngine(runtime=runtime).rebuild(
                date_from="2026-07-01", date_to="2026-07-15"
            )
        except CanonicalCostBlocked as exc:
            assert exc.code == "exact_supplier_expense_identity_drift"
        else:
            raise AssertionError("missing exact canonical expense unexpectedly passed")
        with _connect(runtime.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_supplier_financial_expense_lines "
                "SET amount_rub=1075030 WHERE line_id=?",
                (EXPENSE_LINE_ID,),
            )
            conn.commit()

        repeated = block.dry_run(
            **common,
            financial_document_confirmation=confirmation,
        )
        assert repeated["financial_document_confirmation"]["previous_status"] == "confirmed"
        assert repeated["financial_document_confirmation"]["evidence_fingerprint"] == plan["evidence_fingerprint"]
        assert repeated["rebuild"]["second_run_changed"] == 0
        assert repeated["would_change"] is False
        no_op = block.apply(
            **common,
            financial_document_confirmation=confirmation,
            fingerprint=repeated["fingerprint"],
            backup_dir=root / "backups",
        )
        assert no_op["applied"] is False
        assert no_op["post_run"]["changed"] == 0
    print("supplier_financial_document_chain_confirmation_smoke: ok")
    return 0


def _insert_invoice_136(db_path: Path) -> None:
    expected = AUTHORIZED_FINANCIAL_DOCUMENT_CONFIRMATION_IDENTITY
    amounts = [Decimal(index * 100) for index in range(1, 28)]
    total_product_value = sum(amounts, Decimal("0"))
    with _connect(db_path) as conn:
        _ensure_own_capital_schema(conn)
        conn.execute(
            "DELETE FROM sheet_vitrina_v1_supplier_shipment_lines WHERE shipment_id=?",
            (SHIPMENT_ID,),
        )
        for index, amount in enumerate(amounts, start=1):
            nm_id = 100000000 + index
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_supplier_shipment_lines(
                    line_id,shipment_id,line_type,sort_order,internal_sku,internal_nm_id,
                    internal_name,qty,unit_price,amount,currency,match_status,
                    manual_override,raw_json
                ) VALUES(?,?, 'product',?,?,?,?,100,?,?, 'CNY','matched',0,'{}')
                """,
                (
                    f"invoice-136-line-{index}",
                    SHIPMENT_ID,
                    index,
                    f"SKU-{nm_id}",
                    nm_id,
                    f"SKU {nm_id}",
                    str(amount / Decimal("100")),
                    str(amount),
                ),
            )
        conn.execute(
            """
            UPDATE sheet_vitrina_v1_supplier_shipments
            SET product_qty_total=2700,product_amount_total=?,invoice_amount_total=?
            WHERE shipment_id=?
            """,
            (str(total_product_value), str(total_product_value), SHIPMENT_ID),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_documents(
                document_id,supplier_order_id,document_type,original_filename,
                stored_file_path,file_content_type,file_sha256,uploaded_at,updated_at,
                parse_status,vendor,document_number,document_date,currency,total_amount,
                total_amount_rub,vat_rate,vat_amount_rub,due_date,route,contract_ref,
                cbr_usd_rate_requested_date,cbr_usd_rate_effective_date,cbr_usd_rate_value,
                rate_source,rate_source_status,raw_parse_json,normalized_parse_json,
                parser_version,warnings_json,errors_json
            ) VALUES(?,?,?,?,?,?,?,?,?,'parsed',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                FINANCIAL_DOCUMENT_ID,
                SHIPMENT_ID,
                "logistics_invoice",
                "invoice-136.pdf",
                "supplier_financial_documents/files/invoice-136.pdf",
                "application/pdf",
                expected["file_sha256"],
                "2026-07-15T15:51:23Z",
                "2026-07-15T15:51:23Z",
                "ООО ВОРЛД-ЛОГИСТИК",
                "136",
                "2026-07-15",
                "RUB",
                1075030,
                1075030,
                5,
                51191.9,
                "2026-07-20",
                "",
                "договор транспортной экспедиции № ORE от 04.06.2026",
                "2026-07-15",
                "2026-07-15",
                77.49,
                "cbr",
                "ok",
                "{}",
                json.dumps(
                    {
                        "document_type": "logistics_invoice",
                        "document_number": "136",
                        "document_date": "2026-07-15",
                        "currency": "RUB",
                        "total_amount_rub": 1075030,
                    },
                    sort_keys=True,
                ),
                "supplier_financial_document_parser_v6",
                "[]",
                "[]",
            ),
        )
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines(
                line_id,financial_document_id,supplier_order_id,sort_order,category,
                stage,description,amount,currency,amount_rub,vat_rate,vat_amount_rub,
                included_in_logistics_efficiency,included_in_customs_total,status,
                confidence,raw_json
            ) VALUES(?,?,?,1,'border_expedition','logistics_stage',?,1075030,'RUB',
                     1075030,5,51191.9,1,0,'parsed',0.9,'{}')
            """,
            (
                EXPENSE_LINE_ID,
                FINANCIAL_DOCUMENT_ID,
                SHIPMENT_ID,
                "Счет логиста №136: логистический этап",
            ),
        )
        remaining = Decimal("1075030")
        for index, amount in enumerate(amounts, start=1):
            nm_id = 100000000 + index
            allocated = (
                remaining
                if index == len(amounts)
                else Decimal("1075030") * amount / total_product_value
            )
            remaining -= allocated
            capital = _money_text(allocated)
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_own_capital_events(
                    event_id,event_type,effective_date,shipment_id,supply_id,nm_id,
                    stage_from,stage_to,quantity,capital_rub,confirmed_quantity,
                    cost_layer_id,warehouse,destination,payload_json,evidence_hash,created_at
                ) VALUES(?,'cost_payment','2026-07-15',?,'',?,'','PRODUCTION_TO_FF',
                         '0',?,'0',?,'','',?,?,?)
                """,
                (
                    f"cost_payment:financial_expense:{FINANCIAL_DOCUMENT_ID}:{nm_id}:{index}",
                    SHIPMENT_ID,
                    nm_id,
                    capital,
                    f"expense:financial_expense:{FINANCIAL_DOCUMENT_ID}:{nm_id}:{index}",
                    json.dumps({"source": "supplier_financial_document"}, sort_keys=True),
                    expected["event_evidence_hash"],
                    "2026-07-15T15:51:24Z",
                ),
            )
        conn.commit()


def _insert_volatile_non_target_document(db_path: Path) -> None:
    """Model a read-refresh timestamp touch on unrelated document evidence."""

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO sheet_vitrina_v1_supplier_financial_documents(
                document_id,supplier_order_id,document_type,original_filename,
                stored_file_path,file_content_type,file_sha256,uploaded_at,updated_at,
                parse_status,vendor,document_number,document_date,currency,total_amount,
                total_amount_rub,vat_rate,vat_amount_rub,due_date,route,contract_ref,
                cbr_usd_rate_requested_date,cbr_usd_rate_effective_date,cbr_usd_rate_value,
                rate_source,rate_source_status,raw_parse_json,normalized_parse_json,
                parser_version,warnings_json,errors_json
            )
            SELECT 'fdoc_volatile_read_refresh',supplier_order_id,document_type,
                   original_filename,stored_file_path,file_content_type,file_sha256,
                   uploaded_at,'2026-07-15T15:51:48Z',parse_status,vendor,'volatile',
                   document_date,currency,total_amount,total_amount_rub,vat_rate,
                   vat_amount_rub,due_date,route,contract_ref,
                   cbr_usd_rate_requested_date,cbr_usd_rate_effective_date,
                   cbr_usd_rate_value,rate_source,rate_source_status,raw_parse_json,
                   normalized_parse_json,parser_version,warnings_json,errors_json
            FROM sheet_vitrina_v1_supplier_financial_documents
            WHERE document_id=?
            """,
            (FINANCIAL_DOCUMENT_ID,),
        )
        conn.commit()


def _financial_state(db_path: Path) -> dict[str, object]:
    with _connect(db_path) as conn:
        document = conn.execute(
            "SELECT parse_status FROM sheet_vitrina_v1_supplier_financial_documents "
            "WHERE document_id=?",
            (FINANCIAL_DOCUMENT_ID,),
        ).fetchone()
        events = conn.execute(
            "SELECT event_id,capital_rub,quantity FROM sheet_vitrina_v1_own_capital_events "
            "WHERE event_id LIKE ? ORDER BY event_id",
            (f"cost_payment:financial_expense:{FINANCIAL_DOCUMENT_ID}:%",),
        ).fetchall()
        correction = conn.execute(
            "SELECT report_json FROM sheet_vitrina_v1_supplier_shipment_factual_corrections "
            "WHERE shipment_id=? AND status='success' ORDER BY completed_at DESC LIMIT 1",
            (SHIPMENT_ID,),
        ).fetchone() if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='sheet_vitrina_v1_supplier_shipment_factual_corrections'"
        ).fetchone() else None
    report = json.loads(str(correction[0])) if correction else {}
    return {
        "parse_status": str(document[0]),
        "event_count": len(events),
        "event_ids": [str(item[0]) for item in events],
        "event_capital": _money_text(
            sum((Decimal(str(item[1])) for item in events), Decimal("0"))
        ),
        "event_quantity": _money_text(
            sum((Decimal(str(item[2])) for item in events), Decimal("0"))
        ),
        "correction_evidence_fingerprint": str(
            (report.get("financial_document_confirmation") or {}).get(
                "evidence_fingerprint"
            )
            or ""
        ),
    }


def _money_text(value: Decimal) -> str:
    text = format(value.quantize(Decimal("0.000001")), "f").rstrip("0").rstrip(".")
    return text or "0"


if __name__ == "__main__":
    raise SystemExit(main())
