"""Explicit supplier advances conserve bank cash and stay outside product cost."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.application.cny_ledger import CnyLedgerBlock, supplier_payment_allocation
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _connect
from packages.application.warehouse_functional import _supplier_cost_allocations
from packages.application.supplier_financial_documents import build_supplier_payment_readiness
from apps.cny_ledger_smoke import _save_payment
from apps.supplier_preparation_intents_smoke import source, HEADER

NOW = "2026-07-04T10:00:00Z"


def fixture(raw, *, opening="100", invoice="75", payment="85", include_balance=True):
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw))
    source(runtime, {**HEADER, "shipment_id": "order", "supplier_name": "Factory",
        "contract_no": "FR-001/26", "contract_date": "2026-06-08", "invoice_date": "2026-07-01",
        "shipment_date": "2026-07-01", "invoice_amount_total": invoice,
        "declared_invoice_total": invoice, "product_amount_total": invoice, "product_qty_total": 10},
        [{"line_id": "product", "line_type": "product", "internal_nm_id": 101,
          "qty": 10, "unit_price": str(D(invoice)/10), "amount": invoice, "match_status": "matched"}])
    ledger = CnyLedgerBlock(runtime=runtime, timestamp_factory=lambda: NOW)
    ledger.create_opening_balance({"operation_date": "2026-07-01", "cny_amount": opening,
                                   "rub_value": str((D(opening)*D("12.3457")).quantize(D(".01")))})
    _save_payment(runtime, "advance", "order", "2026-07-02T10:00:00Z", "15")
    assert ledger.replay_ledger(reason="advance")["status"] == "ok"
    if include_balance:
        _save_payment(runtime, "balance", "order", "2026-07-03T10:00:00Z", payment)
    return runtime, ledger


def allocate(runtime, ledger, **changes):
    document = runtime.load_cny_document("balance")
    args = dict(settlement_cny="60", advance_cny="25", supplier_name="Factory",
                contract_no="FR-001/26", contract_date="2026-06-08", actor="operator",
                reason="Revised invoice; explicit supplier advance", confirmed=True,
                expected_file_sha256=document["file_sha256"], expected_total_cny=document["cny_amount"])
    return ledger.allocate_supplier_payment("balance", **{**args, **changes})


def test_conservation_and_cogs():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw)
        original = runtime.load_cny_document("balance")
        result = allocate(runtime, ledger)
        assert result["readback_confirmed"] and result["replay"]["status"] == "ok", result
        document = runtime.load_cny_document("balance")
        for key in ("document_id", "cny_amount", "file_sha256", "stored_file_path", "natural_key"):
            assert document[key] == original[key], key
        assert supplier_payment_allocation(document)["advance_cny"] == "25"
        status = ledger.get_status()
        operations = status["ledger_operations"]
        payment = next(row for row in operations if row["source_document_id"] == "balance" and row["operation_type"] == "supplier_payment_out")
        credit = next(row for row in operations if row["operation_type"] == "supplier_advance_out")
        assert D(payment["cny_delta"]) == -60 and D(credit["cny_delta"]) == -25
        full_cost = D("1234.57") - D("185.19")
        assert abs(D(payment["rub_value_delta"])) + abs(D(credit["rub_value_delta"])) == full_cost
        assert D(payment["balance_cny_after"]) == 25 and D(credit["balance_cny_after"]) == 0
        assert D(status["summary"]["balance_cny"]) == 0 and D(status["summary"]["balance_rub_value"]) == 0
        assert len(status["supplier_advances"]) == 1
        assert status["supplier_advances"][0]["included_in_cogs"] is False
        detail = runtime.load_supplier_shipment("order")
        assert D(detail["header"]["cny_paid_amount"]) == 75
        paid_rub = D(detail["header"]["cny_payment_currency_rub_cost"])
        assert paid_rub + abs(D(credit["rub_value_delta"])) == D("1234.57")
        with _connect(runtime.db_path) as conn:
            layers = conn.execute("SELECT paid_cny,paid_rub FROM sheet_vitrina_v1_own_capital_payment_layers WHERE shipment_id='order'").fetchall()
            raw_documents = [dict(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_cny_documents WHERE source_order_id='order'")]
        assert sum((D(row["paid_cny"]) for row in layers), D(0)) == 75
        assert sum((D(row["paid_rub"]) for row in layers), D(0)) == paid_rub
        readiness = build_supplier_payment_readiness(detail["header"], raw_documents)
        assert readiness["complete"] and D(str(readiness["confirmed_paid"])) == 75, readiness
        projection = _supplier_cost_allocations({"shipments": [detail["header"]],
            "shipment_lines": [{**line, "shipment_id": "order"} for line in detail["lines"]], "cny_documents": raw_documents,
            "cny_operations": operations})["order"]
        assert D(projection["capital_rub"]) == paid_rub, projection
        assert all(control["conserved"] for control in projection["component_controls"])
        before_operations = deepcopy(operations)
        before_document = deepcopy(document)
        assert allocate(runtime, ledger)["idempotent"] is True
        assert runtime.load_cny_document("balance") == before_document
        assert runtime.list_cny_ledger_operations() == before_operations


def test_full_funds_required():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw, opening="90")
        allocate(runtime, ledger)
        operations = runtime.list_cny_ledger_operations()
        assert not any(row["operation_type"] == "supplier_advance_out" for row in operations)
        blocked = next(row for row in operations if row["source_document_id"] == "balance")
        assert blocked["status"] == "blocked"
        assert D(ledger.get_status()["summary"]["balance_cny"]) == 75
        assert D(runtime.load_supplier_shipment("order")["header"]["cny_paid_amount"]) == 15


def test_explicit_validation():
    for change in ({"confirmed": False}, {"settlement_cny": "61"}, {"supplier_name": "Other"},
                   {"expected_file_sha256": "wrong"}, {"actor": ""}, {"settlement_cny": "60.001"},
                   {"settlement_cny": "garbage60"}, {"advance_cny": D("NaN")}):
        with TemporaryDirectory() as raw:
            runtime, ledger = fixture(raw)
            before = runtime.load_cny_document("balance")
            try:
                allocate(runtime, ledger, **change)
            except ValueError:
                pass
            else:
                raise AssertionError(change)
            assert runtime.load_cny_document("balance") == before
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw)
        allocate(runtime, ledger)
        try:
            allocate(runtime, ledger, settlement_cny="59", advance_cny="26")
        except ValueError as exc:
            assert "already exists" in str(exc)
        else:
            raise AssertionError("allocation evidence was silently overwritten")


def test_legacy_unchanged():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw, invoice="100", payment="85")
        assert ledger.replay_ledger(reason="legacy")["status"] == "ok"
        assert ledger.get_status()["supplier_advances"] == []
        assert supplier_payment_allocation(runtime.load_cny_document("balance")) is None
        assert D(runtime.load_supplier_shipment("order")["header"]["cny_paid_amount"]) == 100
        assert sum(row["operation_type"] == "supplier_payment_out" for row in runtime.list_cny_ledger_operations()) == 2


def test_initial_upload_with_allocation():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw, include_balance=False)
        parsed = {"normalized_parse": {"document_type": "supplier_cny_payment", "currency": "CNY",
            "document_number": "24", "document_date": "2026-07-03", "cny_amount": "85",
            "transfer_amount": "85", "operation_datetime": "2026-07-03T10:00:00Z"},
            "warnings": [], "errors": [], "raw_parse": {}, "parser_version": "fixture"}
        allocation = {"version": 1, "authority": "operator_confirmed", "actor": "operator",
            "reason": "Revised invoice; supplier advance", "source_order_id": "order",
            "settlement_cny": "60", "advance_cny": "25", "supplier_name": "Factory",
            "contract_no": "FR-001/26", "contract_date": "2026-06-08", "confirmed_at": NOW}
        with patch("packages.application.cny_ledger.parse_cny_document_pdf", return_value=parsed):
            args = dict(file_bytes=b"bank-payment-source", uploaded_filename="payment24.pdf",
                        source_order_id="order", payment_allocation=allocation)
            before = runtime.list_cny_documents()
            try:
                ledger.upload_document(**{**args, "payment_allocation": {**allocation, "settlement_cny": "61"}})
            except ValueError:
                pass
            else:
                raise AssertionError("invalid allocation admitted on initial upload")
            assert runtime.list_cny_documents() == before
            result = ledger.upload_document(**args)
            assert result["readback_confirmed"] and result["replay"]["status"] == "ok", result
            saved = runtime.load_cny_document(result["document_id"])
            assert D(saved["cny_amount"]) == 85 and supplier_payment_allocation(saved)["advance_cny"] == "25"
            assert len(runtime.list_cny_documents()) == len(before) + 1
            assert ledger.upload_document(**args)["idempotent"]
            assert len(runtime.list_cny_documents()) == len(before) + 1
            assert D(runtime.load_supplier_shipment("order")["header"]["cny_paid_amount"]) == 75


def test_invalid_persisted_metadata_fails_closed():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw)
        allocate(runtime, ledger)
        document = runtime.load_cny_document("balance")
        parsed = deepcopy(document["parsed_payload"])
        parsed["supplier_payment_allocation"]["advance_cny"] = "26"
        runtime.save_cny_document({**document, "parsed_payload": parsed})
        assert ledger.get_status()["supplier_advances"][0]["status"] == "needs_review"
        ledger.replay_ledger(reason="invalid_allocation")
        operations = runtime.list_cny_ledger_operations()
        assert not any(row["operation_type"] == "supplier_advance_out" for row in operations)
        assert next(row for row in operations if row["source_document_id"] == "balance")["status"] == "blocked"
        assert D(ledger.get_status()["summary"]["balance_cny"]) == 85


def test_counted_sequence_warning_advance_is_visible():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw)
        for document_id in ("advance", "balance"):
            document = runtime.load_cny_document(document_id)
            parsed = {**document["parsed_payload"], "operation_datetime": "", "operation_date": "2026-07-03"}
            runtime.save_cny_document({**document, "operation_datetime": "", "operation_date": "2026-07-03", "parsed_payload": parsed})
        allocate(runtime, ledger)
        credit = next(row for row in runtime.list_cny_ledger_operations() if row["operation_type"] == "supplier_advance_out")
        assert credit["status"] == "needs_review" and credit["error_reason"] == "date_only_deterministic_sequence", credit
        advances = ledger.get_status()["supplier_advances"]
        assert len(advances) == 1 and D(advances[0]["amount_cny"]) == 25, advances
        assert advances[0]["calculation_warning"] == "date_only_deterministic_sequence"


def test_allocation_order_binding_survives_archive_restore():
    with TemporaryDirectory() as raw:
        runtime, ledger = fixture(raw)
        allocate(runtime, ledger)
        original = runtime.load_cny_document("balance")
        original_operations = runtime.list_cny_ledger_operations()
        try:
            ledger.relink_document("balance", target_shipment_id="another-order")
        except ValueError as exc:
            assert "cannot be moved" in str(exc)
        else:
            raise AssertionError("allocated payment moved to another order")
        assert runtime.load_cny_document("balance") == original
        assert runtime.list_cny_ledger_operations() == original_operations
        ledger.delete_document("balance")
        archived = runtime.load_cny_document("balance")
        assert archived["status"] == "excluded"
        for writer in (ledger.restore_document, ledger.relink_document):
            try:
                writer("balance", target_shipment_id="another-order")
            except ValueError as exc:
                assert "cannot be moved" in str(exc)
            else:
                raise AssertionError("archived allocated payment moved to another order")
            assert runtime.load_cny_document("balance") == archived
        restored = ledger.restore_document("balance", target_shipment_id="order")
        assert restored["outcome"] == "restored" and restored["readback_confirmed"], restored
        assert runtime.load_cny_document("balance")["parsed_payload"] == original["parsed_payload"]
        assert len(ledger.get_status()["supplier_advances"]) == 1
        assert D(runtime.load_supplier_shipment("order")["header"]["cny_paid_amount"]) == 75


if __name__ == "__main__":
    test_conservation_and_cogs()
    test_full_funds_required()
    test_explicit_validation()
    test_legacy_unchanged()
    test_initial_upload_with_allocation()
    test_invalid_persisted_metadata_fails_closed()
    test_counted_sequence_warning_advance_is_visible()
    test_allocation_order_binding_survives_archive_restore()
    print("supplier_payment_advance_smoke: OK")
