"""Operational notifications: actual HTTP/auth and Chromium UI, no WB transport."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import expect, sync_playwright
from apps.operator_supplier_shipments_http_smoke import server_for, stop, request
from packages.adapters import registry_upload_http_entrypoint as http
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint


class NotificationFixture:
    def __init__(self):
        self.supplies = {
            "42000001": dict(supply_id="supply:42000001", wb_supply_id="42000001", number_label="42000001", warehouse_display="Екатеринбург", quantity_added=13500, status_id=3, status_label="Отгрузка разрешена"),
            "42000002": dict(supply_id="supply:42000002", wb_supply_id="42000002", number_label="42000002", warehouse_display="Крыловская", quantity_added=14000, status_id=3, status_label="Отгрузка разрешена"),
        }
        self.states = {key: "missing_source" for key in self.supplies}
        self.assignments = {}
        self.calls = []
        self.revision = "v1"
        self.disabled = False

    def lifecycle(self, sid):
        return dict(status=self.states[sid], reserved_qty=13500 if self.states[sid] == "reserved" else 0,
                    shortages=[dict(nm_id=428855758, required=14000, available=13750, missing=250)] if self.states[sid] == "shortage" else [])

    def notifications(self):
        if self.disabled:
            return dict(status="disabled", notifications=[])
        rows = [dict(id="fbo:"+sid, reason_revision=self.revision+":"+self.states[sid], supply_id="supply:"+sid,
                     supply=self.supplies[sid], **self.lifecycle(sid)) for sid in self.supplies if self.states[sid] not in {"reserved", "debited"}]
        # Repeated source rows must not produce duplicate notifications.
        return dict(status="ready", notifications=rows+deepcopy(rows[:1]))

    def detail(self, sid):
        return dict(status="ready", assignment_allowed=True, current_assignment=self.assignments.get(sid), history=[],
                    facilities=[dict(facility_id="moscow", code="FF-001", name="Москва"), dict(facility_id="orenburg", code="FF-002", name="Оренбург")], fbo_lifecycle={**self.lifecycle(sid), "supply": self.supplies[sid]})

    def assign(self, sid, payload, *, actor):
        self.calls.append((sid, dict(payload), actor))
        self.assignments[sid] = dict(facility_id=payload["facility_id"], facility_code="FF-001", facility_name="Москва", assignment_id="assignment:"+str(len(self.calls)), request_id=payload["request_id"])
        self.states[sid] = ("debited" if self.supplies[sid]["status_id"] in {4, 5, 6} else "reserved") if sid == "42000001" else "shortage"
        return dict(status="saved", assignment=self.assignments[sid], fbo_lifecycle=self.lifecycle(sid))


def main():
    with TemporaryDirectory(prefix="fbo-notifications-ui-") as raw, patch.dict("os.environ", {"SUPPLIER_VIEW_ENABLED": "1"}):
        rt = RegistryUploadDbBackedRuntime(runtime_dir=Path(raw)/"runtime")
        rt.list_wb_supplies()
        fixture = NotificationFixture()
        rt.save_wb_supply_rows(rows=[{**row, "cache_key": row["supply_id"]} for row in fixture.supplies.values()], warehouses=[], synced_at="2026-11-09T08:00:00Z")
        entry = RegistryUploadHttpEntrypoint(runtime=rt, runtime_dir=rt.runtime_dir)
        entry.handle_fbo_notifications_request = fixture.notifications
        entry.handle_ff_wb_supply_origin_detail_request = fixture.detail
        entry.handle_ff_wb_supply_origin_assign_request = fixture.assign
        entry.handle_wb_supplies_detail_request = lambda sid: dict(supply=fixture.supplies[sid.removeprefix("supply:")], goods=[], goods_summary={})
        entry.handle_wb_supplies_sync_request = lambda body: (_ for _ in ()).throw(AssertionError("WB sync not permitted"))
        server, thread, base = server_for(entry)
        try:
            endpoint = http.DEFAULT_FBO_NOTIFICATIONS_PATH
            before = rt.db_path.read_bytes()
            with urlopen(base+endpoint) as response:
                assert response.status == 200 and response.headers["Cache-Control"] == "private, no-store"
                assert len(json.loads(response.read())["notifications"]) == 3
            assert before == rt.db_path.read_bytes(), "Notification GET wrote database"
            with patch.object(http, "_web_auth_config", return_value={"configured": True, "enabled": True}), patch.object(http, "_authenticated_web_user", return_value={"username": "reports", "role": "operator", "allowed_sections": ["reports"]}):
                assert request(base, endpoint)[0] == 403
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                # Production routes this shell to the independent supplier
                # listener. Use its exact renderer/configuration in this local
                # single-listener HTTP fixture, keeping the real iframe URL.
                supplier_shell = http._render_sheet_vitrina_operator_ui(
                    daily_report_path=http.DEFAULT_SHEET_DAILY_REPORT_PATH,
                    stock_report_path=http.DEFAULT_SHEET_STOCK_REPORT_PATH,
                    plan_report_path=http.DEFAULT_SHEET_PLAN_REPORT_PATH,
                    refresh_path=http.DEFAULT_SHEET_REFRESH_PATH,
                    load_path=http.DEFAULT_SHEET_LOAD_PATH,
                    status_path=http.DEFAULT_SHEET_STATUS_PATH,
                    job_path=http.DEFAULT_SHEET_JOB_PATH,
                    embedded_tab="factory-order", independent_supplier_read=True)
                context.route("**/sheet-vitrina-v1/supply-view?*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=supplier_shell))
                page = context.new_page(); errors = []; posts = []
                page.on("pageerror", lambda err: errors.append(str(err)))
                context.on("request", lambda req: posts.append(req.url) if req.method == "POST" else None)
                page.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+"?tab=factory-order")
                bell = page.get_by_role("button", name="Уведомления", exact=True)
                expect(bell).to_be_visible(); expect(page.locator("[data-notification-dot]")).to_be_visible()
                bell.focus(); page.keyboard.press("Enter")
                panel = page.locator("[data-notification-panel]")
                expect(panel).to_be_visible()
                expect(page.locator(".notification-item")).to_have_count(2)
                expect(page.locator("[data-notification-count]")).to_have_text("Требуют действия · 2")
                expect(panel).to_contain_text("Екатеринбург"); expect(panel).to_contain_text("13 500")
                expect(page.locator("[data-notification-dot]")).to_be_hidden()
                page.keyboard.press("Escape"); expect(panel).to_be_hidden(); expect(bell).to_be_focused()
                page.reload(); expect(page.locator("[data-notification-dot]")).to_be_hidden()
                # Production starts with an already initialized independent
                # supplier frame. Its notification bridge must not consume and
                # lose the first requested WB supply while redirecting.
                initial_frame = page.frame_locator('[data-operator-embed-frame="factory-order"]')
                expect(initial_frame.locator('[data-supply-mode-button="supplier"]')).to_have_attribute("aria-selected", "true")
                assert "/supply-view?" in page.locator('[data-operator-embed-frame="factory-order"]').get_attribute("src")
                expect(page.locator('[data-operator-embed-frame="factory-order"]')).not_to_have_attribute("data-notification-ready", "true")
                bell.click(); expect(page.locator(".notification-item")).to_have_count(2)
                page.locator('[data-notification-id="fbo:42000001"]').get_by_role("button", name="Уточнить").click()
                frame = page.frame_locator('[data-operator-embed-frame="factory-order"]')
                expect(frame.locator("#wbSupplyFfOriginPanel")).to_be_visible()
                expect(frame.locator("#wbSupplyFfOriginSelect")).to_be_enabled()
                expect(frame.locator("#wbSuppliesDetailTitle")).to_contain_text("42000001")
                assert not any("/wb-supply-origins/" in url for url in posts), posts
                # An already open legacy parent can still send the bridge into
                # the supplier document. Its redirect must retain the target;
                # after that redirect src remains /supply-view, but the live
                # canonical operator capability supports subsequent clicks.
                defence_page = context.new_page()
                defence_page.on("pageerror", lambda err: errors.append(str(err)))
                defence_page.goto(base+http.DEFAULT_SHEET_WEB_VITRINA_UI_PATH+"?tab=factory-order")
                defence_frame = defence_page.frame_locator('[data-operator-embed-frame="factory-order"]')
                expect(defence_frame.locator('[data-supply-mode-button="supplier"]')).to_have_attribute("aria-selected", "true")
                defence_page.evaluate("document.querySelector('[data-operator-embed-frame=\"factory-order\"]').contentWindow.postMessage({type:'wbc_open_notification_supply',supply_id:'42000001'},location.origin)")
                expect(defence_frame.locator("#wbSupplyFfOriginPanel")).to_be_visible()
                expect(defence_frame.locator("#wbSuppliesDetailTitle")).to_contain_text("42000001")
                expect(defence_frame.locator("#wbSupplyFfOriginSelect")).to_be_enabled()
                assert "/supply-view?" in defence_page.locator('[data-operator-embed-frame="factory-order"]').get_attribute("src")
                navigations = []
                defence_child = next(child for child in defence_page.frames if "/sheet-vitrina-v1/operator?" in child.url)
                defence_page.on("framenavigated", lambda navigated: navigations.append(navigated.url) if navigated == defence_child else None)
                defence_page.get_by_role("button", name="Уведомления", exact=True).click()
                defence_page.locator('[data-notification-id="fbo:42000002"]').get_by_role("button", name="Уточнить").click()
                expect(defence_frame.locator("#wbSuppliesDetailTitle")).to_contain_text("42000002")
                assert not navigations, navigations
                defence_page.close()
                frame.locator("#wbSupplyFfOriginSelect").select_option("moscow")
                save = frame.locator("#wbSupplyFfOriginSaveButton")
                expect(save).to_have_text("Сохранить и зарезервировать")
                expect(frame.locator("#wbSupplyFfOriginPolicyNote")).not_to_contain_text("Поставка уже отгружена")
                save.evaluate("button=>{button.click();button.click();}")
                expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("Товар зарезервирован")
                assert len(fixture.calls) == 1, fixture.calls
                expect(save).to_be_disabled()
                bell.click(); expect(page.locator(".notification-item")).to_have_count(1)
                page.locator('[data-notification-id="fbo:42000002"]').get_by_role("button", name="Уточнить").click()
                expect(frame.locator("#wbSuppliesDetailTitle")).to_contain_text("42000002")
                frame.locator("#wbSupplyFfOriginSelect").select_option("moscow")
                # Lost POST response: recover by exact request identity using GET, never send twice.
                def lose_response(route):
                    if route.request.method == "POST":
                        result = route.fetch(); assert result.status == 200; route.abort("failed")
                    else:
                        route.continue_()
                context.route("**/wb-supply-origins/42000002", lose_response)
                save.click()
                expect(frame.locator("#wbSupplyFfOriginMessage")).to_contain_text("SKU 428855758: не хватает 250 шт.")
                expect(save).to_have_text("Проверить резерв")
                expect(frame.locator("#wbSupplyFfOriginCheckButton")).to_be_hidden()
                assert len(fixture.calls) == 2
                bell.click(); expect(page.locator(".notification-item")).to_have_count(1)
                expect(panel).to_contain_text("Не хватает товара для резерва")
                expect(panel).to_contain_text("428855758")
                page.locator("#notification-panel-title").click()
                expect(panel).to_be_visible()
                page.locator('[data-unified-tab-button="reports"]').click(); expect(panel).to_be_hidden()
                assert len([url for url in posts if "/wb-supply-origins/" in url]) == 2, posts
                # Unknown outcome stays guarded across reload; checking performs GET only.
                bell.click()
                page.locator('[data-notification-id="fbo:42000002"]').get_by_role("button", name="Уточнить").click()
                context.unroute("**/wb-supply-origins/42000002", lose_response)
                context.route("**/wb-supply-origins/42000002", lambda route: route.abort("failed") if route.request.method == "POST" else route.continue_())
                save.click()
                expect(frame.locator("#wbSupplyFfOriginCheckButton")).to_be_visible()
                expect(save).to_be_disabled()
                assert len(fixture.calls) == 2
                page.reload(); bell.click()
                page.locator('[data-notification-id="fbo:42000002"]').get_by_role("button", name="Уточнить").click()
                expect(frame.locator("#wbSupplyFfOriginCheckButton")).to_be_visible()
                expect(save).to_be_disabled()
                frame.locator("#wbSupplyFfOriginCheckButton").click()
                expect(save).to_be_disabled()
                pending = page.evaluate("JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key=>key.startsWith('wbc:fbo-origin-pending:'))))['42000002']")
                assert pending and len(fixture.calls) == 2
                fixture.assign("42000002", pending, actor="fixture-late-response")
                frame.locator("#wbSupplyFfOriginCheckButton").click()
                expect(frame.locator("#wbSupplyFfOriginCheckButton")).to_be_hidden()
                expect(save).to_be_enabled()
                assert len([url for url in posts if "/wb-supply-origins/" in url]) == 3, posts
                # A reason change is new, while unchanged unresolved work stays seen.
                fixture.revision = "v2"; page.reload()
                expect(page.locator("[data-notification-dot]")).to_be_visible()
                bell.click(); expect(page.locator("[data-notification-dot]")).to_be_hidden()
                # Account-scoped read markers do not hide work for a different login.
                keys = page.evaluate("Object.keys(localStorage).filter(key=>key.startsWith('wbc:operational-notifications:seen:'))")
                assert keys == ["wbc:operational-notifications:seen:v1:"+page.evaluate("WEB_VITRINA_CONFIG.user_config_key")], keys
                expect(page.locator("[data-notification-feedback]")).to_have_text("")
                page.keyboard.press("Escape")
                page.evaluate("key=>{localStorage.setItem(key+':another-account',localStorage.getItem(key));localStorage.removeItem(key);}", keys[0])
                page.reload(); expect(page.locator("[data-notification-dot]")).to_be_visible()
                bell.click(); expect(page.locator("[data-notification-dot]")).to_be_hidden()
                # Small screen retains a usable right-side control and readable popover.
                page.set_viewport_size({"width": 390, "height": 844})
                bounds = panel.bounding_box(); assert bounds and bounds["x"] >= 0 and bounds["x"]+bounds["width"] <= 391, bounds
                page.screenshot(path=str(Path(raw)/"notifications-mobile.png"))
                fixture.disabled = True; page.reload(); bell.click()
                expect(panel).to_contain_text("Уведомления FBO пока недоступны")
                # Selecting an origin for an already shipped supply immediately
                # debits FBO; the existing action must disclose that before POST.
                shipped_page = context.new_page()
                shipped_page.on("pageerror", lambda err: errors.append(str(err)))
                fixture.states["42000001"] = "missing_source"
                fixture.assignments.pop("42000001", None)
                calls_before_shipped = len(fixture.calls)
                for shipped_status in (4, 5, 6):
                    fixture.supplies["42000001"]["status_id"] = shipped_status
                    shipped_page.goto(base+http.DEFAULT_SHEET_OPERATOR_UI_PATH+"?embedded_tab=factory-order&supply_mode=wildberries&notification_supply_id=42000001")
                    shipped_save = shipped_page.locator("#wbSupplyFfOriginSaveButton")
                    expect(shipped_save).to_have_text("Сохранить источник и списать")
                    expect(shipped_page.locator("#wbSupplyFfOriginPolicyNote")).to_contain_text("Поставка уже отгружена")
                    assert len(fixture.calls) == calls_before_shipped
                shipped_page.locator("#wbSupplyFfOriginSelect").select_option("moscow")
                shipped_save.click()
                expect(shipped_page.locator("#wbSupplyFfOriginMessage")).to_contain_text("Списание FBO подтверждено")
                assert len(fixture.calls) == calls_before_shipped + 1 and fixture.states["42000001"] == "debited"
                shipped_page.close()
                assert not errors, errors
                context.close(); browser.close()
        finally:
            stop(server, thread)
    print("FBO notifications HTTP + Chromium: auth/readonly GET, global bell, seen vs unresolved, real modal bridge, reservation/shortage, double-click/lost response, mobile/default-off: OK")


if __name__ == "__main__":
    main()
