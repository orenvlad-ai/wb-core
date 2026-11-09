"""Real FBO domain -> HTTP -> global Chromium notifications and origin assignment.

All writes target a disposable fixture. The existing composition drawer uses its
real cached projection; any upstream supply API access fails this test.
"""
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from apps.fbo_supply_lifecycle_smoke import LifecycleTests, NOW
from apps.operator_supplier_shipments_http_smoke import server_for, stop
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application import fbo_supply_lifecycle as domain


class NoSupplyTransport:
    def __getattr__(self, name):
        raise AssertionError("Upstream WB calls prohibited: " + name)


def get(base, path, payload=None):
    headers = {"Content-Type": "application/json", "X-WB-FF-Pool-CSRF": "1", "Accept": "application/json"}
    with urlopen(Request(base+path, data=json.dumps(payload).encode() if payload is not None else None, headers=headers), timeout=15) as response:
        return response.status, json.loads(response.read())


def physical(fixture):
    with fixture.conn() as conn:
        return {
            "aggregate": [tuple(row) for row in conn.execute(f"SELECT * FROM {domain.AGGREGATE} ORDER BY operation_id")],
            "aggregate_lines": [tuple(row) for row in conn.execute(f"SELECT * FROM {domain.AGGREGATE_LINES} ORDER BY operation_id,line_no")],
            "pools": [tuple(row) for row in conn.execute(f"SELECT * FROM {domain.BALANCES_TABLE} ORDER BY facility_id,pool,nm_id")],
            "addressed_movements": [tuple(row) for row in conn.execute(f"SELECT * FROM {domain.OPERATIONS_TABLE} ORDER BY operation_id")],
        }


def main():
    fixture = LifecycleTests(methodName="runTest"); fixture.setUp()
    server = thread = None
    try:
        fixture.seed("1", [(101, 13500)], number_label="41911131", warehouse_display="Екатеринбург", quantity_added=13500, status_label="Отгрузка разрешена")
        fixture.seed("2", [(101, 14000)], number_label="41923596", warehouse_display="Крыловская", quantity_added=14000, status_label="Отгрузка разрешена")
        fixture.seed("3", [(101, 234000)], status=5, number_label="Историческая", quantity_added=234000)
        fixture.seed("4", [(101, 1)], status=1, number_label="Черновик")
        # Existing 234k aggregate remains separate from the selected physical FF/FBO pool.
        fixture.runtime.create_ff_stock_operation(operation_id="legacy-234k-opening", operation_type="receipt", source_type="fixture", source_key="legacy-234k-opening", created_at="2026-11-01T00:00:00Z", lines=[dict(nm_id=101, quantity_delta=233700)])
        with fixture.conn() as conn:
            conn.execute(f"UPDATE {domain.BALANCES_TABLE} SET quantity=27250,capital_rub='272500',wac_rub='10' WHERE facility_id='facility-a' AND pool='FBO' AND nm_id=101")
            conn.execute(f"INSERT INTO {domain.BALANCES_TABLE} VALUES(?,?,?,?,?,?,?,?,?)", ("facility-a", "FBS", 101, 1, 100000, "500000", "5", "fixture", NOW))
            for sid in ("1", "2", "3", "4"):
                conn.execute("UPDATE sheet_vitrina_v1_wb_supplies SET raw_detail_json='{}',raw_package_json='[]' WHERE supply_id=?", (sid,))
        fixture.runtime.create_ff_stock_reservation_operation(operation_id="legacy-first", source_key="legacy-first", supply_id="supply:1", supply_revision="legacy", operation_type="reserve", created_at=NOW, diagnostics={}, lines=[dict(nm_id=101, quantity_delta=13500)], expected_current={})
        baseline = physical(fixture)
        fixture.cutover()
        assert physical(fixture) == baseline
        assert fixture.service.detail("1")["legacy_reserved_qty"] == 13500
        entry = RegistryUploadHttpEntrypoint(runtime=fixture.runtime, runtime_dir=fixture.runtime.runtime_dir, activated_at_factory=lambda: NOW)
        entry.wb_supplies_block.source = NoSupplyTransport()
        # Existing cached composition path, not a fabricated supply/UI response.
        entry.handle_wb_supplies_cached_detail_request = lambda sid: entry.wb_supplies_block._cached_supply_detail(sid, reason="fixture_no_provider")
        server, thread, base = server_for(entry)
        notices = http.DEFAULT_FBO_NOTIFICATIONS_PATH
        origin = http.DEFAULT_FF_POOL_WB_SUPPLY_ORIGINS_PATH
        before = fixture.runtime.db_path.read_bytes()
        status, payload = get(base, notices)
        assert status == 200 and len(payload["notifications"]) == 2, payload
        assert {row["supply_id"] for row in payload["notifications"]} == {"1", "2"}
        for item in payload["notifications"]:
            assert item["status"] == "missing_source" and item["reason_revision"] and item["supply"]["warehouse_display"]
        assert get(base, origin+"/1")[1]["fbo_lifecycle"]["status"] == "missing_source"
        assert fixture.runtime.db_path.read_bytes() == before, "Lifecycle/origin HTTP GET changed DB"
        with sync_playwright() as pw, patch.dict("os.environ", {"SUPPLIER_VIEW_ENABLED": "1"}):
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page(); errors = []; requests = []
            page.on("pageerror", lambda err: errors.append(str(err)))
            context.on("request", lambda req: requests.append(req) if req.method == "POST" and "/wb-supply-origins/" in req.url else None)
            page.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+"?tab=reports")
            bell = page.get_by_role("button", name="Уведомления", exact=True)
            expect(page.locator("[data-notification-dot]")).to_be_visible()
            bell.click()
            expect(page.locator(".notification-item")).to_have_count(2)
            expect(page.locator("[data-notification-panel]")).to_contain_text("Екатеринбург")
            page.locator('[data-notification-id="fbo:supply:1:needs_origin"]').get_by_role("button", name="Уточнить").click()
            frame = page.frame_locator('[data-operator-embed-frame="factory-order"]')
            expect(frame.locator("#wbSuppliesDetailTitle")).to_contain_text("41911131")
            expect(frame.locator("#wbSupplyFfOriginSelect")).to_be_enabled()
            frame.locator("#wbSupplyFfOriginSelect").select_option("facility-a")
            save = frame.locator("#wbSupplyFfOriginSaveButton")
            save.evaluate("button=>{button.click();button.click();}")
            expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("Товар зарезервирован: 13 500 шт.")
            assert len(requests) == 1
            first_body = json.loads(requests[0].post_data)
            replay = get(base, origin+"/1", first_body)[1]
            assert replay["idempotent"] and replay["fbo_lifecycle"]["reserved_qty"] == 13500, replay
            assert physical(fixture) == baseline
            assert sum(row["quantity"] for row in fixture.runtime.list_ff_stock_reservations(supply_id="1")) == 13500
            assert fixture.runtime.list_ff_stock_reservations(supply_id="supply:1") == []
            bell.click()
            expect(page.locator(".notification-item")).to_have_count(1)
            page.locator('[data-notification-id="fbo:supply:2:needs_origin"]').get_by_role("button", name="Уточнить").click()
            expect(frame.locator("#wbSuppliesDetailTitle")).to_contain_text("41923596")
            frame.locator("#wbSupplyFfOriginSelect").select_option("facility-a")
            def lose(route):
                if route.request.method == "POST":
                    result = route.fetch(); assert result.status == 200; route.abort("failed")
                else:
                    route.continue_()
            context.route("**/wb-supply-origins/2", lose)
            save.click()
            expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("SKU 101: не хватает 250 шт.")
            expect(frame.locator("#wbSupplyFfOriginCheckButton")).to_be_hidden()
            expect(save).to_have_text("Проверить резерв")
            assert len(requests) == 2
            second_body = json.loads(requests[1].post_data)
            assert get(base, origin+"/2", second_body)[1]["idempotent"]
            state = get(base, origin+"/2")[1]["fbo_lifecycle"]
            assert state["status"] == "shortage" and state["reserved_qty"] == 0
            assert state["shortages"] == [dict(nm_id=101, required=14000, available=13750, missing=250)]
            assert fixture.runtime.list_ff_stock_reservations(supply_id="2") == []
            assert physical(fixture) == baseline, "Status 3 reserve changed quantity/capital or historical 234k"
            with fixture.conn() as conn:
                assert conn.execute("SELECT COUNT(*) FROM sheet_vitrina_v1_wb_supply_ff_origin_assignments").fetchone()[0] == 2
                assert conn.execute(f"SELECT SUM(quantity_delta) FROM {domain.AGGREGATE_LINES}").fetchone()[0] == 234000
            bell.click()
            expect(page.locator(".notification-item")).to_have_count(1)
            expect(page.locator("[data-notification-panel]")).to_contain_text("Не хватает товара для резерва")
            expect(page.locator("[data-notification-panel]")).to_contain_text("250 шт.")
            page.keyboard.press("Escape"); page.reload()
            expect(page.locator("[data-notification-dot]")).to_be_hidden()
            bell.click(); expect(page.locator(".notification-item")).to_have_count(1)
            assert not errors, errors
            context.close(); browser.close()
        # Repeated reads remain pure after the writes and have stable reason identity.
        before = fixture.runtime.db_path.read_bytes()
        current = get(base, notices)[1]
        assert get(base, notices)[1] == current
        assert before == fixture.runtime.db_path.read_bytes()
    finally:
        if server:
            stop(server, thread)
        fixture.doCleanups()
    print("Real FBO domain+HTTP+Chromium: 13500 reserved once, full-supply shortage250, legacy alias consolidated, 234000/current quantities unchanged at status3, repeat/lost POST recovered, GET pure: OK")


if __name__ == "__main__":
    main()
