"""Attachment HTTP preview/confirm retains original cash and confirms its split."""
from decimal import Decimal as D
from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_financial_native_smoke import setup
from apps.operator_supplier_shipments_http_smoke import server_for, stop, request, PATH
from apps.supplier_financial_documents_smoke import _post_multipart
from apps.cny_ledger_smoke import _save_payment, PAYMENT_TEXT
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.cny_ledger import supplier_payment_allocation
from packages.application import cny_preparation_intents
from packages.adapters import registry_upload_http_entrypoint as http

NOW = "2026-11-01T12:00:00Z"
BODY = b"original synthetic bank payment 24"
PAYMENT = PAYMENT_TEXT.replace("№ 1", "№ 24").replace("785087,50", "447716,25").replace("CN-1", "FR-001/26").replace("01.05.2026", "08.06.2026").replace("от 13 мая 2026", "от 28 октября 2026").replace("13.05.2026", "28.10.2026")


def fixture(raw, *, invoice="493725"):
    runtime, ledger = setup(raw)
    detail = runtime.load_supplier_shipment("source")
    header = {**detail["header"], "invoice_no": "26GN870", "supplier_name": "Factory",
              "contract_no": "FR-001/26", "contract_date": "2026-06-08", "invoice_amount_total": invoice,
              "product_amount_total": invoice, "declared_invoice_total": invoice, "source_file_sha256": "invoice-source"}
    runtime.save_supplier_shipment(header=header, lines=detail["lines"])
    opening = next(document for document in runtime.list_cny_documents() if document["document_type"] == "opening_balance")
    runtime.save_cny_document({**opening, "cny_amount": "526725", "rub_amount": "5267250",
        "parsed_payload": {**opening["parsed_payload"], "cny_amount": "526725", "rub_value": "5267250"}})
    _save_payment(runtime, "advance", "source", "2026-09-29T10:00:00Z", "79008.75")
    assert ledger.replay_ledger(reason="fixture")["status"] == "ok"
    assert D(runtime.load_supplier_shipment("source")["header"]["cny_paid_amount"]) == D("79008.75")
    entry = RegistryUploadHttpEntrypoint(runtime=runtime, runtime_dir=runtime.runtime_dir, activated_at_factory=lambda: NOW)
    entry.cny_ledger_block.pdf_text_extractor = lambda body, filename: (PAYMENT, {"method": "synthetic text layer"}, [])
    return runtime, entry


def preview(base):
    status, result = _post_multipart(base + PATH + "/source/financial-documents", BODY,
        filename="original-payment24.pdf", fields={"propose_payment_allocation": "true", "actor": "spoof", "settlement_cny": "1"})
    assert status == 200 and result.get("confirmation_token"), result
    return result


def confirm(base, token, identity):
    return request(base, PATH + "/source/financial-documents/confirm-upload", "POST", {
        "request_id": identity, "confirmation_token": token,
        "payment_allocation": {"settlement_cny": "1", "advance_cny": "447715.25", "actor": "spoof"}})


def test_real_upload_and_source_retention():
    with TemporaryDirectory() as raw:
        runtime, entry = fixture(raw)
        server, thread, base = server_for(entry)
        try:
            before = runtime.list_cny_documents()
            p = preview(base)
            assert runtime.list_cny_documents() == before
            assert D(str(p["document"]["amount"])) == D("447716.25")
            allocation = p["payment_allocation"]
            assert D(allocation["settlement_cny"]) == D("414716.25") and D(allocation["advance_cny"]) == 33000
            assert allocation["actor"] and allocation["actor"] != "spoof" and allocation["source_order_id"] == "source"
            stored = runtime.load_supplier_confirmation_preview(p["confirmation_token"])
            assert stored["source_sha256"] == sha256(BODY).hexdigest()
            assert stored["payload"]["payment_allocation"] == allocation
            assert stored["payload"]["allocation_target_revision"]
            legacy_status, legacy = request(base, PATH + "/source/financial-documents/confirm-upload", "POST", {"confirmation_token": p["confirmation_token"]})
            assert legacy_status == 409 and "request_id" in legacy["error"], legacy
            assert runtime.list_cny_documents() == before
            calls = []
            replace = runtime.replace_cny_ledger_operations
            def retain(operations):
                calls.append(operations)
                return replace(operations)
            with patch.object(runtime, "replace_cny_ledger_operations", side_effect=retain):
                status, result = confirm(base, p["confirmation_token"], "attachment-payment24")
            assert status == 200 and result["status"] == "accepted", result
            documents = [document for document in runtime.list_cny_documents() if document.get("file_sha256") == sha256(BODY).hexdigest()]
            assert len(documents) == 1
            document = documents[0]
            assert D(document["cny_amount"]) == D("447716.25")
            assert supplier_payment_allocation(document) == allocation
            assert entry.cny_ledger_block.download_document_file(document["document_id"])[0] == BODY
            for operations in calls:
                emitted = [row for row in operations if row.get("source_document_id") == document["document_id"]]
                assert next(row for row in emitted if row["operation_type"] == "supplier_payment_out")["cny_delta"] == "-414716.25"
                assert D(next(row for row in emitted if row["operation_type"] == "supplier_advance_out")["cny_delta"]) == -33000
            assert calls
            assert D(runtime.load_supplier_shipment("source")["header"]["cny_paid_amount"]) == 493725
            repeated = confirm(base, p["confirmation_token"], "attachment-payment24")[1]
            assert repeated["acceptance"]["operation_id"] == result["acceptance"]["operation_id"]
            assert len(runtime.list_cny_documents()) == len(before) + 1
        finally:
            stop(server, thread)


def test_revision_and_sha_guards():
    for original, changed in (("526725", "493725"), ("493725", "526725")):
        with TemporaryDirectory() as raw:
            runtime, entry = fixture(raw, invoice=original)
            server, thread, base = server_for(entry)
            try:
                p = preview(base)
                assert bool(p.get("payment_allocation")) == (original == "493725")
                before = runtime.list_cny_documents()
                detail = runtime.load_supplier_shipment("source")
                # Preserve updated_at deliberately: the real invoice identity
                # and balance must be pinned, independently of a timestamp.
                runtime.save_supplier_shipment(header={**detail["header"], "invoice_amount_total": changed}, lines=detail["lines"])
                result = confirm(base, p["confirmation_token"], "stale-invoice")[1]
                assert result["status"] == "rejected", result
                assert runtime.list_cny_documents() == before
            finally:
                stop(server, thread)
    with TemporaryDirectory() as raw:
        runtime, entry = fixture(raw)
        server, thread, base = server_for(entry)
        try:
            p = preview(base)
            before = runtime.list_cny_documents()
            with patch.object(http, "_current_web_user_config_key", return_value="other-operator"), patch.object(http, "_current_web_user_actor", return_value="other-operator"):
                result = confirm(base, p["confirmation_token"], "foreign-actor")[1]
            assert result["status"] == "rejected" and runtime.list_cny_documents() == before, result
            stored = runtime.load_supplier_confirmation_preview(p["confirmation_token"])
            (runtime.runtime_dir / stored["payload"]["staging_path"]).write_bytes(b"changed bank source")
            result = confirm(base, p["confirmation_token"], "changed-sha")[1]
            assert result["status"] == "rejected" and runtime.list_cny_documents() == before, result
        finally:
            stop(server, thread)


def test_proposal_and_revision_use_one_context():
    with TemporaryDirectory() as raw:
        runtime, entry = fixture(raw)
        server, thread, base = server_for(entry)
        try:
            context = entry._supplier_cny_payment_allocation_context
            calls = []
            def read_once(shipment_id):
                result = context(shipment_id)
                calls.append(result[1])
                return result
            with patch.object(entry, "_supplier_cny_payment_allocation_context", side_effect=read_once):
                p = preview(base)
            stored = runtime.load_supplier_confirmation_preview(p["confirmation_token"])
            assert calls == [stored["payload"]["allocation_target_revision"]], calls
        finally:
            stop(server, thread)


if __name__ == "__main__":
    test_real_upload_and_source_retention()
    test_revision_and_sha_guards()
    test_proposal_and_revision_use_one_context()
    print("supplier_payment_allocation_http_smoke: OK")
