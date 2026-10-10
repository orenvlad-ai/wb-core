"""Actual Chromium: bounded ambiguous writes, same-ID recovery and stale reads."""
from contextlib import closing
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_shipments_http_smoke import PATH, server_for, stop, request
from apps.operator_supplier_shipments_smoke import seed
from apps.operator_supplier_factual_dates_smoke import fixture, SHIPMENT_ID
from apps.sheet_vitrina_v1_supplier_shipments_http_smoke import _build_invoice_fixture, TARGET_FACILITY_ID
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_SUPPLIER_UI_PATH
from packages.application import operator_supplier_shipments as source, operator_supplier_factual_dates as facts


def marker(result, entity_id):
    return {"request_id": result["request_id"], "action": result["action"],
            "entity_id": entity_id, "payload_digest": result.get("wire_digest") or result["payload_digest"]}


def set_pending(page, value):
    page.evaluate("""value => {
      const config=JSON.parse(document.getElementById('sheet-vitrina-v1-supplier-config').textContent);
      const key='wbc_supplier_source_pending_v1:'+(config.user_config_key || 'local_operator');
      localStorage.setItem(key,JSON.stringify(value));
    }""", value)


class RecoveryBrowser(unittest.TestCase):
    def test_never_settling_saved_post_and_get_foreign_receipt_restart_once(self):
        with TemporaryDirectory(prefix="supplier-bounded-browser-") as raw:
            runtime, entry, _ = seed(raw)
            server, thread, base = server_for(entry)
            try:
                with sync_playwright() as pw:
                    browser = pw.chromium.launch()
                    context = browser.new_context()
                    writes, held, reads = [], [], []
                    read_mode = "foreign"

                    def post(route):
                        if route.request.method != "POST":
                            route.continue_(); return
                        writes.append(route.request.post_data_json["request_id"])
                        response = route.fetch()
                        self.assertEqual(response.status, 200)
                        held.append(route)  # Durable save, transport never settles.

                    def read(route):
                        reads.append(route.request.url)
                        response = route.fetch()
                        payload = response.json()
                        if read_mode == "foreign":
                            payload["request_id"] = "foreign-request"
                            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
                        elif read_mode == "held":
                            held.append(route)
                        else:
                            route.fulfill(response=response)

                    context.route("**/supplier-shipments", post)
                    context.route("**/supplier-shipments?request_id=*", read)
                    page = context.new_page()
                    page.goto(base + DEFAULT_SHEET_SUPPLIER_UI_PATH + "?embedded=operator")
                    page.locator("#addShipmentButton").click()
                    page.locator("#invoiceFileInput").set_input_files({"name": "synthetic.xlsx",
                        "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "buffer": _build_invoice_fixture()})
                    expect(page.locator("#cardMessage")).to_contain_text("Invoice распознан")
                    page.locator("#shipmentDateInput").fill("2026-10-10")
                    page.locator("#targetFacilityInput").select_option(TARGET_FACILITY_ID)
                    page.locator("#saveShipmentButton").click()
                    expect(page.locator("#supplierSourceAcceptance")).to_contain_text("Проверяем сохранение", timeout=11000)
                    self.assertEqual(len(writes), 1)
                    self.assertEqual(len(reads), 1)
                    self.assertEqual(page.locator(".ff-operation-check").count(), 0)
                    expect(page.locator("#saveShipmentButton")).to_be_disabled()
                    read_mode = "held"
                    page.get_by_role("button", name="Проверить статус", exact=True).click()
                    expect(page.get_by_role("button", name="Проверить статус", exact=True)).to_be_enabled(timeout=7000)
                    self.assertEqual(page.locator(".ff-operation-check").count(), 0)
                    self.assertEqual(len(writes), 1)
                    retained = page.evaluate("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}}).filter(v=>v&&v.request_id)")
                    self.assertEqual([row["request_id"] for row in retained], writes)
                    page.close()
                    read_mode = "exact"
                    page = context.new_page()
                    page.goto(base + DEFAULT_SHEET_SUPPLIER_UI_PATH + "?embedded=operator")
                    expect(page.locator("#supplierSourceAcceptance .ff-operation-check")).to_be_visible()
                    expect(page.locator("#supplierSourceAcceptance")).to_contain_text("Ожидает обработки")
                    self.assertEqual(page.get_by_text("Обработано", exact=True).count(), 0)
                    expect(page.locator("#saveShipmentButton")).to_be_enabled()
                    native = request(base, PATH + "?request_id=" + writes[0])[1]
                    expect(page.locator("[data-ff-operation-receipt]")).to_have_attribute("data-ff-operation-receipt", native["acceptance"]["operation_id"])
                    self.assertEqual(len(writes), 1)
                    with closing(source.readonly(runtime.db_path)) as conn:
                        self.assertEqual(conn.execute(f"SELECT count(*) FROM {source.TABLE}").fetchone()[0], 1)
                        self.assertEqual(conn.execute("SELECT count(*) FROM sheet_vitrina_v1_supplier_shipments").fetchone()[0], 1)
                    self.assertFalse(any("/refresh" in url or "/cycle" in url for url in reads))
                    for route in held:
                        route.abort("failed")
                    context.close(); browser.close()
            finally:
                stop(server, thread)

    def test_late_recovery_cannot_clear_or_present_a_newer_pending_request(self):
        with TemporaryDirectory(prefix="supplier-read-race-") as raw:
            runtime, entry, payload = seed(raw)
            server, thread, base = server_for(entry)
            try:
                first = request(base, PATH, "POST", payload)[1]
                first_read = request(base, PATH + "?request_id=" + payload["request_id"])[1]
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(); context = browser.new_context()
                    page = context.new_page(); page.goto(base + DEFAULT_SHEET_SUPPLIER_UI_PATH)
                    set_pending(page, marker(first_read, ""))
                    held = []
                    context.route("**/supplier-shipments?request_id=" + payload["request_id"],
                                  lambda route: held.append((route, route.fetch())))
                    page.reload()
                    page.wait_for_timeout(100)
                    self.assertTrue(held)
                    second = request(base, PATH + "/" + first["shipment_id"], "PATCH", {
                        "request_id": "supplier-newer-race", "shipment_date": "2026-10-11"})[1]
                    second_read = request(base, PATH + "?request_id=supplier-newer-race")[1]
                    key = page.evaluate("'wbc_supplier_source_pending_v1:'+(JSON.parse(document.getElementById('sheet-vitrina-v1-supplier-config').textContent).user_config_key || 'local_operator')")
                    peer = context.new_page(); peer.goto(base + "/synthetic-storage-peer")
                    peer.evaluate("({key,value}) => localStorage.setItem(key,JSON.stringify(value))", {
                        "key": key, "value": marker(second_read, first["shipment_id"])})
                    expect(page.locator("#supplierSourceAcceptance")).to_contain_text("Проверяем сохранение")
                    for route, response in held:
                        route.fulfill(response=response)
                    page.wait_for_timeout(100)
                    self.assertEqual(page.locator(".ff-operation-check").count(), 0)
                    self.assertEqual(page.evaluate("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}}).filter(v=>v&&v.request_id).map(v=>v.request_id)"), ["supplier-newer-race"])
                    page.get_by_role("button", name="Проверить статус", exact=True).click()
                    expect(page.locator("[data-ff-operation-receipt]")).to_have_attribute("data-ff-operation-receipt", second["acceptance"]["operation_id"])
                    self.assertEqual(page.evaluate("Object.values(localStorage).map(v=>{try{return JSON.parse(v)}catch(_){return null}}).filter(v=>v&&v.request_id).length"), 0)
                    context.close(); browser.close()
            finally:
                stop(server, thread)

    def test_late_card_and_registry_reads_do_not_replace_newer_selection(self):
        with TemporaryDirectory(prefix="supplier-card-race-") as raw:
            runtime, entry, payload = seed(raw)
            server, thread, base = server_for(entry)
            try:
                saved = request(base, PATH, "POST", payload)[1]
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(); page = browser.new_page()
                    page.goto(base + DEFAULT_SHEET_SUPPLIER_UI_PATH + "?embedded=operator")
                    row = page.locator('tr[data-row="' + saved["shipment_id"] + '"]')
                    expect(row).to_be_visible()
                    held = []
                    page.route("**/supplier-shipments/" + saved["shipment_id"], lambda route: held.append((route, route.fetch())))
                    row.click(); page.wait_for_timeout(100); self.assertTrue(held)
                    page.locator("#addShipmentButton").click()
                    for route, response in held:
                        route.fulfill(response=response)
                    expect(page.locator("#cardTitle")).to_have_text("Новый заказ")
                    expect(page.locator("#shipmentDateInput")).to_have_value("")
                    # A failed refresh retains the usable prior list and its rows.
                    page.unroute("**/supplier-shipments/" + saved["shipment_id"])
                    page.locator("#closeShipmentButton").click()
                    row.click()
                    expect(page.locator("#cardTitle")).to_contain_text(saved["shipment_id"])
                    page.route("**/supplier-shipments", lambda route: route.fulfill(status=503, content_type="application/json", body='{"error":"synthetic-read-failure"}'))
                    page.locator("#priceCheckButton").click()
                    expect(row).to_be_visible()
                    expect(page.locator("#registryMessage")).to_contain_text("synthetic-read-failure")
                    browser.close()
            finally:
                stop(server, thread)

    def test_failed_factual_poll_releases_controls_without_processing_completion(self):
        self.factual_poll_failure("error")

    def test_timed_out_factual_poll_preserves_readonly_controls_and_pending_date(self):
        self.factual_poll_failure("held")

    def test_foreign_factual_success_cannot_complete_the_selected_native_job(self):
        self.factual_poll_failure("foreign")

    def factual_poll_failure(self, mode):
        with TemporaryDirectory(prefix="supplier-factual-poll-") as raw:
            runtime, entry = fixture(raw)
            server, thread, base = server_for(entry)
            try:
                preview = request(base, PATH + "/" + SHIPMENT_ID + "/factual-dates/preview", "POST", {"actual_shipment_date": "2026-06-26"})[1]
                accepted = request(base, PATH + "/" + SHIPMENT_ID + "/factual-dates/confirm", "POST", {
                    "request_id": "factual-poll-failure", "confirmation_token": preview["confirmation_token"]})[1]
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(); page = browser.new_page()
                    held, writes = [], []
                    page.on("request", lambda req: writes.append(req.method) if PATH in req.url and req.method in {"POST", "PATCH", "DELETE"} else None)
                    def poll(route):
                        if mode == "held": held.append(route)
                        elif mode == "foreign":
                            result = route.fetch().json()
                            result.update(correction_id="foreign-correction", status="success")
                            route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
                        else: route.fulfill(status=503, content_type="application/json", body='{"error":"synthetic-poll-failure"}')
                    page.route("**/supplier-shipments/" + SHIPMENT_ID + "/factual-date-correction", poll)
                    page.goto(base + DEFAULT_SHEET_SUPPLIER_UI_PATH + "?embedded=operator")
                    page.locator('tr[data-row="' + SHIPMENT_ID + '"]').click()
                    message = "Ответ не получен вовремя" if mode == "held" else "Получен статус другого изменения даты" if mode == "foreign" else "synthetic-poll-failure"
                    expect(page.locator("#registryMessage")).to_contain_text(message, timeout=8000)
                    expect(page.locator("#shipmentDateInput")).to_be_enabled()
                    expect(page.locator("#actualFfAcceptanceDateInput")).to_be_disabled()
                    expect(page.locator("#saveShipmentButton")).to_have_attribute("aria-busy", "false")
                    current = request(base, PATH + "/" + SHIPMENT_ID + "/factual-dates/status?request_id=factual-poll-failure")[1]
                    self.assertEqual(current["acceptance"]["operation_id"], accepted["acceptance"]["operation_id"])
                    self.assertFalse(current["acceptance"]["physical_applied"])
                    self.assertFalse(current["acceptance"]["processing"]["complete"])
                    set_pending(page, marker(current, SHIPMENT_ID))
                    page.reload()
                    expect(page.locator("#supplierSourceAcceptance .ff-operation-check")).to_be_visible()
                    expect(page.locator("#supplierProcessingStatus")).to_be_visible()
                    expect(page.locator("#supplierProcessingStatus")).to_contain_text("Принята фактическая дата: 26.06.2026")
                    expect(page.locator("#supplierProcessingStatus")).to_contain_text("Текущая фактическая дата: 25.06.2026")
                    self.assertEqual(page.get_by_text("Обработано", exact=True).count(), 0)
                    with closing(source.readonly(runtime.db_path)) as conn:
                        self.assertEqual(conn.execute(f"SELECT count(*) FROM {facts.TABLE}").fetchone()[0], 1)
                        self.assertEqual(conn.execute(f"SELECT count(*) FROM {facts.JOB}").fetchone()[0], 1)
                    self.assertEqual(writes, [])
                    for route in held: route.abort("failed")
                    browser.close()
            finally:
                stop(server, thread)


if __name__ == "__main__":
    unittest.main()
