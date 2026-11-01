"""Exercise atomic revision, exact links, drift and transaction rollback."""

from copy import deepcopy
from datetime import date
from io import BytesIO
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook
from apps.production_apply_launcher import execute
from apps.production_apply_contract import AdapterError
from apps.supplier_invoice_parser_smoke import _build_invoice_fixture
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.supplier_invoice_parser import parse_supplier_invoice_xlsx
from packages.application.supplier_shipment_invoice_revision import SupplierInvoiceRevisionAdapter, snapshot, connect, candidate, digest
from packages.application.supplier_shipments import SupplierShipmentsBlock
from packages.application.own_product_capital import _ensure_own_capital_schema
from packages.application.cny_ledger import CnyLedgerBlock


def fixture(root, *, invoice_date=None):
    original = _build_invoice_fixture()
    if invoice_date:
        workbook = load_workbook(BytesIO(original))
        workbook.active["B2"] = invoice_date
        raw = BytesIO(); workbook.save(raw)
        original = raw.getvalue()
    parsed = parse_supplier_invoice_xlsx(original, filename="old.xlsx")
    (root / "old.xlsx").write_bytes(original)
    workbook = load_workbook(BytesIO(original))
    # This fixture has three products followed by packaging; adjust a product
    # and its amount, plus any declared total, with the same unit prices.
    sheet = workbook.active
    row = parsed["lines"][0]["raw"]["worksheet_row"]
    sheet.cell(row, 5).value += 10
    sheet.cell(row, 7).value = sheet.cell(row, 5).value * sheet.cell(row, 6).value
    sheet.cell(sheet.max_row, 7).value += 10
    raw = BytesIO(); workbook.save(raw)
    updated = raw.getvalue()
    new_path = root / "new.xlsx"; new_path.write_bytes(updated)
    new_parsed = parse_supplier_invoice_xlsx(updated, filename="new.xlsx")
    assert not new_parsed["errors"], new_parsed["errors"]
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=root)
    now = "2026-07-01T00:00:00Z"
    meta = parsed["metadata"]
    sha = hashlib.sha256(original).hexdigest()
    header = {**meta, **parsed["summary"], "shipment_id": "sup_test", "created_at": now, "updated_at": now, "shipment_date": "2026-09-20", "source_filename": "old.xlsx", "source_file_path": "old.xlsx", "source_file_sha256": sha, "invoice_document_id": "invoice_test", "supplier_name": "Factory", "match_status": "all_matched"}
    lines = [{**line, "line_id": "line_" + str(i), "internal_nm_id": i + 1 if line["line_type"] == "product" else None} for i, line in enumerate(parsed["lines"])]
    runtime.save_supplier_shipment(header=header, lines=lines)
    runtime.save_trade_document({"document_id": "invoice_test", "document_type": "invoice", "number": header["invoice_no"], "document_date": header["invoice_date"], "supplier_name": "Factory", "currency": header["currency"], "amount_total": header["invoice_amount_total"], "file_path": "old.xlsx", "file_original_name": "old.xlsx", "file_sha256": sha, "status": "active", "created_at": now, "updated_at": now})
    runtime.save_trade_document({"document_id": "contract_test", "document_type": "contract", "number": "contract", "status": "active", "created_at": now, "updated_at": now})
    runtime.save_invoice_contract_link(invoice_document_id="invoice_test", contract_document_id="contract_test", created_at=now, updated_at=now, linked_by="operator", source="operator")
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("INSERT INTO sheet_vitrina_v1_cny_documents(document_id,document_type,source,source_order_id,context_order_id,natural_key,uploaded_at,created_at,updated_at,status,operation_date,cny_amount) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("payment_test", "supplier_cny_payment", "operator", "sup_test", "sup_test", "payment-key", now, now, now, "posted", "2026-07-20", "15.00"))
        conn.execute("INSERT INTO sheet_vitrina_v1_supplier_financial_documents(document_id,supplier_order_id,document_type,original_filename,stored_file_path,file_content_type,file_sha256,uploaded_at,updated_at,parse_status,total_amount_rub) VALUES(?,?,?,?,?,?,?,?,?,?,?)", ("fee_test", "sup_test", "bank_fee_statement", "fee.pdf", "fee.pdf", "application/pdf", "b" * 64, now, now, "confirmed", 100.0))
    marker = root / "sha"; marker.write_text("a" * 40)
    request = {"runtime_dir": str(root), "hostname": socket.gethostname(), "runtime_sha_file": str(marker), "runtime_sha": "a" * 40, "shipment_id": "sup_test", "invoice_no": header["invoice_no"], "source_path": str(new_path), "source_sha256": hashlib.sha256(updated).hexdigest()}
    return request, runtime, parsed, new_parsed


def historical_fixture(root, *, dispatch="2026-10-31"):
    request, runtime, old, _ = fixture(root, invoice_date="2026-09-28")
    workbook = load_workbook(root / "old.xlsx")
    removed = old["lines"][1]
    workbook.active.cell(workbook.active.max_row, 7).value -= removed["amount"]
    workbook.active.delete_rows(removed["raw"]["worksheet_row"])
    path = root / "historical.xlsx"; workbook.save(path)
    request.update(source_path=str(path), source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), removed_barcodes=[removed["barcode"]], historical_pre_dispatch={
        "source_date": "2026-10-10", "effective_date": "2026-10-10", "dispatch_date": "2026-10-31",
        "reason": "Factory removed a cancelled product before dispatch",
        "evidence": "Factory WeChat revised-file message 2026-10-10 13:58 and approval 13:59; owner confirmed dispatch 2026-10-31",
    })
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET actual_shipment_date=? WHERE shipment_id='sup_test'", (dispatch,))
        _ensure_own_capital_schema(conn)
    return request, runtime


def add_stock_event(conn, shipment_id):
    conn.execute("INSERT INTO sheet_vitrina_v1_ff_stock_operations(operation_id,operation_type,source_type,source_key,source_object_id,created_at) VALUES(?,?,?,?,?,?)", ("event-" + shipment_id, "supplier_receipt", "supplier_shipment", "supplier_shipment_acceptance:" + shipment_id, shipment_id, "2026-11-01T00:00:00Z"))


def assert_historical_revision():
    adapter = SupplierInvoiceRevisionAdapter()
    with patch("packages.application.supplier_shipment_invoice_revision.business_today", return_value=date(2026, 11, 1)), patch("packages.application.supplier_shipment_invoice_revision.trusted_actor", return_value="trusted-cli-principal"):
        for dispatch in ("2026-10-31", None):
            with TemporaryDirectory() as raw:
                root = Path(raw)
                request, runtime = historical_fixture(root, dispatch=dispatch)
                with connect(runtime.db_path) as conn:
                    before = snapshot(conn, "sup_test", historical=True)
                original_bytes = (root / "old.xlsx").read_bytes()
                if dispatch:
                    try:
                        adapter.preview({k: v for k, v in request.items() if k != "historical_pre_dispatch"}, "ordinary-dispatched")
                    except Exception as exc:
                        assert "unshipped" in str(exc)
                    else:
                        raise AssertionError("ordinary mode admitted a dispatched order")
                preview = adapter.preview(request, "historical-revision")
                context = preview["scope"]["historical_pre_dispatch"]
                assert context["actor"] == "trusted-cli-principal" and context["factual_date_correction_required"] is (dispatch is None)
                assert not (root / "supplier_invoice_revisions").exists()
                # Activity belonging to another shipment must not invalidate
                # this target, even when it involves the same product nmID.
                with sqlite3.connect(runtime.db_path) as conn:
                    add_stock_event(conn, "another-order")
                    conn.execute("INSERT INTO sheet_vitrina_v1_own_capital_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("other-dispatch", "stage_transfer", "2026-10-31", "another-order", "", 1, "PRODUCTION", "PRODUCTION_TO_FF", "10", "100", "10", "", "", "", "{}", "other-hash", "2026-11-01T00:00:00Z"))
                with patch.object(adapter, "enqueue", return_value={"status": "queued"}):
                    result = execute(action="apply", adapter_name="test", operation_id="historical-revision", request=request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"], adapters={"test": adapter})
                assert result["state"] == "applied", result
                with connect(runtime.db_path) as conn:
                    after = snapshot(conn, "sup_test", historical=True)
                assert after["header"]["actual_shipment_date"] == before["header"]["actual_shipment_date"]
                assert after["header"]["invoice_date"] == "2026-09-28"
                assert after["protected"] == before["protected"] and len(after["lines"]) == len(before["lines"]) - 1
                evidence = root / "supplier_invoice_revisions/historical-revision"
                assert json.loads((evidence / "before.json").read_text()) == before
                assert json.loads((evidence / "candidate.json").read_text())["historical_pre_dispatch"] == context
                assert (root / "old.xlsx").read_bytes() == original_bytes
                block = SupplierShipmentsBlock(runtime=runtime)
                assert block.download_invoice("sup_test")[0] == Path(request["source_path"]).read_bytes()
                assert block.download_trade_document_file(after["document"]["document_id"])[0] == Path(request["source_path"]).read_bytes()
        for change, expected in (({"source_date": "2026-11-01"}, "date-order"), ({"effective_date": "2026-11-01"}, "date-order"), ({"dispatch_date": "2026-11-02"}, "date-order"), ({"source_date": "2026-02-30"}, "revision-date"), ({"evidence": ""}, "evidence"), ({"actor": "forged"}, "context"), ({"dispatch_date": "2026-10-30"}, "dispatch-date-mismatch")):
            with TemporaryDirectory() as raw:
                request, _ = historical_fixture(Path(raw))
                request["historical_pre_dispatch"].update(change)
                try:
                    adapter.preview(request, "historical-invalid")
                except Exception as exc:
                    assert expected in str(exc), str(exc)
                else:
                    raise AssertionError(change)
        for mutation in (
            "UPDATE sheet_vitrina_v1_supplier_shipments SET actual_ff_acceptance_date='2026-11-01' WHERE shipment_id='sup_test'",
            "UPDATE sheet_vitrina_v1_supplier_shipments SET historical_status_exception='legacy_ff_accepted_without_date' WHERE shipment_id='sup_test'",
            "UPDATE sheet_vitrina_v1_supplier_shipments SET expenses_complete=1 WHERE shipment_id='sup_test'",
            "INSERT INTO sheet_vitrina_v1_own_capital_expense_certifications VALUES('sup_test',1,'operator','2026-11-01T00:00:00Z')",
            "INSERT INTO sheet_vitrina_v1_own_capital_events VALUES('dispatch','stage_transfer','2026-10-31','sup_test','',1,'PRODUCTION','PRODUCTION_TO_FF','10','100','10','','','','{}','hash','2026-11-01T00:00:00Z')",
            "INSERT INTO sheet_vitrina_v1_ff_stock_operations(operation_id,operation_type,source_type,source_key,source_object_id,created_at) VALUES('receipt','supplier_receipt','supplier_shipment','supplier_shipment_acceptance:sup_test','sup_test','2026-11-01T00:00:00Z')",
            "INSERT INTO sheet_vitrina_v1_supplier_ff_cost_layers(layer_id,supplier_shipment_id,status,calculated_at,reconciliation_status,inputs_hash,version) VALUES('layer','sup_test','ready','2026-11-01T00:00:00Z','ok','hash',1)",
            "INSERT INTO sheet_vitrina_v1_warehouse_business_operations VALUES('movement','china_acceptance','supplier','supplier_shipment_acceptance','sup_test','revision',1,'2026-11-01','2026-11-01T00:00:00Z','{}')",
        ):
            with TemporaryDirectory() as raw:
                request, runtime = historical_fixture(Path(raw))
                with sqlite3.connect(runtime.db_path) as conn:
                    conn.execute(mutation)
                try:
                    adapter.preview(request, "historical-material")
                except AdapterError as exc:
                    assert str(exc) in {"revision-requires-unshipped-active-order", "historical-revision-requires-unreceived-order", "certified-order-requires-separate-correction", "historical-revision-material-boundary-exists"}, str(exc)
                else:
                    raise AssertionError(mutation)
        with TemporaryDirectory() as raw:
            request, runtime = historical_fixture(Path(raw))
            preview = adapter.preview(request, "historical-race")
            original_plan = adapter.plan
            def raced_plan(value):
                result = original_plan(value)
                with sqlite3.connect(runtime.db_path) as conn:
                    add_stock_event(conn, "sup_test")
                return result
            with patch.object(adapter, "plan", side_effect=raced_plan):
                try:
                    adapter.apply(request, "historical-race", preview)
                except Exception as exc:
                    assert "transaction-prestate-drift" in str(exc), str(exc)
                else:
                    raise AssertionError("concurrent stock receipt admitted")
            assert runtime.load_supplier_shipment("sup_test")["header"]["invoice_date"] == "2026-09-28"
        with TemporaryDirectory() as raw:
            request, runtime = historical_fixture(Path(raw), dispatch=None)
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute("UPDATE sheet_vitrina_v1_supplier_shipment_lines SET match_status='matched_by_barcode' WHERE line_type='product'")
                conn.execute("UPDATE sheet_vitrina_v1_supplier_financial_documents SET document_date='2026-10-28' WHERE document_id='fee_test'")
                conn.execute("INSERT INTO sheet_vitrina_v1_supplier_financial_expense_lines(line_id,financial_document_id,supplier_order_id,sort_order,category,amount,currency,amount_rub,status) VALUES('fee-line','fee_test','sup_test',1,'bank_fee',100,'RUB',100,'confirmed')")
            now = "2026-11-01T00:00:00Z"
            runtime.save_cny_document({"document_id": "opening", "document_type": "opening_balance", "source": "cny_account", "natural_key": "historical-opening", "uploaded_at": now, "created_at": now, "updated_at": now, "operation_date": "2026-07-01", "currency": "CNY", "cny_amount": "100", "rub_amount": "1200", "parsed_payload": {"cny_amount": "100", "rub_value": "1200"}, "status": "posted"})
            # A saved native money core may still await derived cost work.
            CnyLedgerBlock(runtime=runtime)._replay_ledger(reason="historical-fixture-core", core_only=True)
            preview = adapter.preview(request, "historical-native")
            result = execute(action="apply", adapter_name="test", operation_id="historical-native", request=request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"], adapters={"test": adapter})
            assert result["state"] == "applied" and result["readback"]["source_verified"], result
            assert result["readback"]["queue"]["preparation_revision"], result
            with connect(runtime.db_path) as conn:
                assert conn.execute("SELECT count(*) FROM sheet_vitrina_v1_own_capital_events WHERE shipment_id='sup_test' AND event_type='cost_payment'").fetchone()[0] > 0
                assert all(value is None or value == 0 for value in snapshot(conn, "sup_test", historical=True)["historical_guards"].values())


def main():
    adapter = SupplierInvoiceRevisionAdapter()
    adapters = {"test": adapter}
    with TemporaryDirectory() as raw:
        root = Path(raw)
        request, runtime, old, new = fixture(root)
        with connect(runtime.db_path) as conn:
            before = snapshot(conn, "sup_test")
        preview = execute(action="preview", adapter_name="test", operation_id="test-revision", request=request, adapters=adapters)
        assert preview["scope"]["after_quantity"] == preview["scope"]["before_quantity"] + 10
        assert not (root / "supplier_invoice_revisions").exists()
        changed = deepcopy(new); changed["lines"][0]["barcode"] = "9999999999999"
        for bad in (changed,):
            try:
                candidate(before, bad, root / "new.xlsx", request["source_sha256"])
            except ValueError:
                raise
            except Exception as exc:
                assert "assortment" in str(exc)
            else:
                raise AssertionError("changed barcode admitted")
        with patch.object(adapter, "enqueue", return_value={"queue_id": "test-queue", "status": "queued"}):
            result = execute(action="apply", adapter_name="test", operation_id="test-revision", request=request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"], adapters=adapters)
        assert result["state"] == "applied", result
        with connect(runtime.db_path) as conn:
            after = snapshot(conn, "sup_test")
        assert after["protected"] == before["protected"]
        assert len(after["protected"]["cny_documents"]) == 1
        assert len(after["protected"]["supplier_financial_documents"]) == 1
        assert {x["line_id"] for x in before["lines"]} == {x["line_id"] for x in after["lines"]}
        assert (root / after["header"]["source_file_path"]).read_bytes() == (root / "new.xlsx").read_bytes()
        assert json.loads((root / "supplier_invoice_revisions/test-revision/before.json").read_text()) == before
        assert execute(action="readback", adapter_name="test", operation_id="test-revision", request=request, adapters=adapters)["state"] == "applied"
        # A second operation pinned to the old prestate cannot mutate the order.
        try:
            execute(action="apply", adapter_name="test", operation_id="test-drift", request=request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"], adapters=adapters)
        except Exception as exc:
            assert "drift" in str(exc)
        else:
            raise AssertionError("stale preview admitted")
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_supplier_shipments SET actual_ff_acceptance_date='2026-09-01' WHERE shipment_id='sup_test'")
        try:
            adapter.preview(request, "test-accepted")
        except Exception as exc:
            assert "unshipped" in str(exc)
        else:
            raise AssertionError("received order admitted")
    with TemporaryDirectory() as raw:
        request, runtime, _, _ = fixture(Path(raw))
        preview = adapter.preview(request, "test-rollback")
        with connect(runtime.db_path) as conn:
            before = snapshot(conn, "sup_test")
        # Abort on the document write after the header/lines have been updated.
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("CREATE TRIGGER reject_revision BEFORE UPDATE ON sheet_vitrina_v1_trade_documents BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        try:
            adapter.apply(request, "test-rollback", preview)
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("injected failure did not abort")
        with connect(runtime.db_path) as conn:
            assert digest(snapshot(conn, "sup_test")) == digest(before)
    with TemporaryDirectory() as raw:
        root = Path(raw)
        request, runtime, old, _ = fixture(root)
        workbook = load_workbook(root / "old.xlsx")
        sheet = workbook.active
        removed = old["lines"][1]
        sheet.cell(sheet.max_row, 7).value -= removed["amount"]
        sheet.delete_rows(removed["raw"]["worksheet_row"])
        source = root / "removed.xlsx"
        workbook.save(source)
        request.update(source_path=str(source), source_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        try:
            adapter.preview(request, "test-no-removal-authority")
        except Exception as exc:
            assert "assortment" in str(exc)
        else:
            raise AssertionError("unrequested deletion admitted")
        request["removed_barcodes"] = [removed["barcode"]]
        preview = adapter.preview(request, "test-removal")
        with connect(runtime.db_path) as conn:
            before = snapshot(conn, "sup_test")
        with patch.object(adapter, "enqueue", return_value={"queue_id": "test-queue", "status": "queued"}):
            result = execute(action="apply", adapter_name="test", operation_id="test-removal", request=request, expected_prestate=preview["prestate_sha256"], expected_candidate=preview["candidate_sha256"], adapters=adapters)
        assert result["state"] == "applied", result
        with connect(runtime.db_path) as conn:
            after = snapshot(conn, "sup_test")
        assert len(after["lines"]) == len(before["lines"]) - 1
        assert removed["barcode"] not in {x["barcode"] for x in after["lines"]}
        assert before["protected"] == after["protected"]
        assert after["header"]["product_qty_total"] == before["header"]["product_qty_total"] - removed["qty"]
        assert json.loads((root / "supplier_invoice_revisions/test-removal/before.json").read_text()) == before
        # Readback must notice a removed line reappearing, not just updated totals.
        with sqlite3.connect(runtime.db_path) as conn:
            line = next(x for x in before["lines"] if x["barcode"] == removed["barcode"])
            columns = list(line)
            conn.execute("INSERT INTO sheet_vitrina_v1_supplier_shipment_lines (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")", [line[k] for k in columns])
        assert adapter.readback(request, "test-removal")["state"] == "failed"
    assert_historical_revision()
    print("supplier_invoice_revision_smoke: OK")


if __name__ == "__main__":
    main()
