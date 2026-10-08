"""Synthetic stock-monitor UI checks: missing data, warehouse detail, sorting and bounded DOM."""
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
import json
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture(period=14, horizon=60, date_offset=0):
    today = date(2026, 10, 8)
    dates = [(today + timedelta(days=i)).isoformat() for i in range(date_offset, min(horizon, date_offset+90))]
    def cells(stock, demand, unknown=False):
        return [{"date": day, "quantity": None if unknown and i else max(0, stock-i*demand),
                 "cover_days": None if unknown else max(0, stock/demand-i),
                 "bar_ratio": None if unknown else min(1, max(0, stock/demand-i)/30),
                 "color": "unknown" if unknown else "green" if stock/demand-i >= 30 else "yellow" if stock/demand-i >= 14 else "orange" if stock/demand-i >= 7 else "red",
                 "inbounds": [{"shipment_id": "fixture-1", "label": "Поставка <A>", "quantity": 200,
                              "status": "production", "arrival_date": day}] if i == 2 else []}
                for i, day in enumerate(dates, date_offset)]
    rows = [{"nm_id": 1, "name": "Без прогноза", "category": "Чехлы", "avg_daily_sales": None,
             "current_stock": 100, "deficit_date": None, "deficit_days": None,
             "forecast_state": "unavailable", "warnings": ["Нет полных дней"],
             "cells": cells(100, 10, True), "warehouses": []}]
    for nm in range(2, 96):
        stock = 800 if nm == 2 else nm*10
        row = {"nm_id": nm, "name": "Модель %03d" % nm, "category": "Чехлы",
               "current_stock": stock, "avg_daily_sales": 10, "forecast_state": "ready",
               "deficit_date": (today+timedelta(days=stock//10)).isoformat(), "deficit_days": stock//10,
               "warnings": [], "cells": cells(stock, 10), "warehouses": []}
        row["warehouses"] = [{**row, "name": "FBS Москва", "facility_id": "moscow", "warehouses": [],
                              "stock_source": {"captured_at": "2026-10-06T10:00:00+05:00",
                                               "warning": "Снимок 2 дня назад"},
                              "demand_quality": {"qualified_days": 5, "requested_days": period,
                                                 "first_date": "2026-09-01", "last_date": "2026-10-07"}}]
        rows.append(row)
    return {"contract_name": "stock_monitor", "generated_at": "2026-10-08T11:00:00+05:00",
            "report_date": today.isoformat(), "period_days": period, "horizon_days": horizon, "date_offset": date_offset,
            "dates": dates, "rows": rows, "warnings": [], "cache": {"hit": True}, "freshness": {"fbs_captured_at": "2026-10-06T10:00:00+05:00"}}


def main():
    from urllib.parse import urlparse, parse_qs
    from playwright.sync_api import sync_playwright
    requests = []
    refreshes = []
    refresh_mode = {"kind": "instant", "period": None, "polls": 0, "old_generation": 0}
    snippet = (ROOT / "packages/adapters/templates/stock_monitor.html").read_text()
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.endswith("/stock-monitor"):
                query = parse_qs(parsed.query)
                requests.append(query)
                payload = fixture(int(query["period_days"][0]), int(query["horizon_days"][0]), int(query.get("date_offset", [0])[0]))
                if refreshes:
                    payload["generated_at"] = "2026-10-08T11:00:%02d+05:00" % len(refreshes)
                if int(query["period_days"][0]) == refresh_mode["period"]:
                    requested = refreshes and refreshes[-1]["period_days"] == refresh_mode["period"]
                    pending = not requested or refresh_mode["polls"] < 2
                    if requested:
                        refresh_mode["polls"] += 1
                    if refresh_mode["kind"] == "cold" and pending:
                        payload = {"status": "unavailable", "period_days": refresh_mode["period"], "rows": [], "cache": {"hit": False}, "message": "Снимок готовится"}
                    elif refresh_mode["kind"] == "old" and pending:
                        payload["generated_at"] = "2026-10-08T11:00:%02d+05:00" % refresh_mode["old_generation"]
                    elif refresh_mode["kind"] == "failed" and requested:
                        payload["generated_at"] = "2026-10-08T11:00:%02d+05:00" % refresh_mode["old_generation"]
                        payload["refresh"] = {"status": "failed", "attempted_at": "2026-10-08T12:01:00+05:00", "error_code": "fixture_failure"}
                body = json.dumps(payload).encode()
                content_type = "application/json"
            else:
                body = ("<!doctype html><meta charset=utf-8><style>:root{--panel-bg:white;--panel-subtle:#f3f4f6;--text:#202020;--muted:#666;--border:#ddd;--warning-text:#a55;--error-text:#b00}body{font-family:Arial;margin:20px}</style>" + snippet +
                        "<script>document.querySelector('[data-stock-monitor]').hidden=false;window.WbcStockMonitor.activate()</script>").encode()
                content_type = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ";charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # A superseded fetch is aborted by the UI.
        def do_POST(self):
            assert self.path == "/v1/sheet-vitrina-v1/stock-monitor/refresh"
            assert self.headers.get("X-WB-FF-Pool-CSRF") == "1"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            refreshes.append(payload)
            refresh_mode["polls"] = 0
            body = json.dumps({"status": "refreshing", "period_days": payload["period_days"]}).encode()
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1450, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto("http://127.0.0.1:%d/" % server.server_port)
            page.wait_for_selector('[data-sm-expand="1"]')
            assert page.locator('[data-sm-table] tbody tr').first.locator('td').nth(1).inner_text() == "Без прогноза !"
            assert page.locator('[data-sm-row="1"][data-sm-cell="1"]').inner_text() == "—"
            assert page.locator('[data-sm-row="1"][data-sm-cell="0"]').inner_text() == "100"
            page.locator('[data-sm-expand="2"]').click()
            warehouse = page.locator('.sm-child').first
            assert "FBS Москва" in warehouse.inner_text()
            warehouse.locator('[data-sm-cell="0"]').click()
            assert "Снимок 2 дня назад" in page.locator('[data-sm-detail]').inner_text()
            assert "Подходящих дней: 5 / 14" in page.locator('[data-sm-detail]').inner_text()
            page.locator('[data-sm-sort="name"]').click()
            assert page.locator('[data-sm-table] tbody tr').first.locator('td').nth(1).inner_text() == "Без прогноза !"
            page.locator('[data-sm-sort="name"]').click()
            assert "Модель 095" in page.locator('[data-sm-table] tbody tr').first.inner_text()
            page.locator('.sm-columns summary').click()
            page.locator('[data-sm-column="category"]').uncheck()
            page.locator('.sm-columns summary').click()
            assert page.locator('[data-sm-sort="category"]').count() == 0
            page.locator('[name="search"]').fill("Модель 002")
            assert page.locator('[data-sm-expand]').count() == 1
            page.locator('[name="display"]').select_option("bars")
            assert page.locator('[data-stock-monitor]').get_attribute("data-display") == "bars"
            page.locator('[data-sm-jump]').first.click()
            page.wait_for_function("document.querySelector('[name=horizon]').value==='95' && document.querySelector('[data-stock-monitor]').getAttribute('aria-busy')===null")
            assert requests[-1]["horizon_days"] == ["95"]
            page.locator('[name="search"]').fill("")
            page.locator('[name="period"]').fill("30")
            page.locator('[name="horizon"]').fill("1200")
            page.locator('[data-sm-form]').get_by_role("button", name="Пересчитать").click()
            page.wait_for_function("document.querySelector('[data-sm-sort=date]')&&document.querySelector('[data-stock-monitor]').getAttribute('aria-busy')===null")
            page.wait_for_function("document.querySelector('[data-sm-table]').offsetWidth>45000")
            assert requests[-1]["period_days"] == ["30"]
            assert refreshes == [{"period_days": 30}]
            assert page.locator('[data-sm-table] th.sm-day').count() <= 90
            assert page.locator('[data-sm-table] tbody td').count() < 10000
            page.locator('[data-sm-scroll]').evaluate("el=>{el.scrollLeft=20000}")
            page.wait_for_function("Number(document.querySelector('[data-sm-sort=date]').dataset.smIndex)>400")
            assert page.locator('[data-sm-table] th.sm-day').count() <= 90
            # The target period has no published snapshot. Opening/reading it
            # must not be mistaken for successful completion of the refresh.
            refresh_mode.update(kind="cold", period=45, polls=0, old_generation=len(refreshes))
            page.locator('[name="period"]').fill("45")
            page.locator('[data-sm-form]').get_by_role("button", name="Пересчитать").click()
            page.wait_for_function("document.querySelector('[data-sm-status]').textContent.includes('Пересчёт выполняется')")
            assert "Среднее: 30 д." in page.locator('[data-sm-meta]').inner_text()
            page.wait_for_function("document.querySelector('[data-sm-status]').textContent.includes('Снимок обновлён · среднее по 45')")
            # An old target-period snapshot also cannot count as the new result.
            refresh_mode.update(kind="old", period=60, polls=0, old_generation=len(refreshes))
            page.locator('[name="period"]').fill("60")
            page.locator('[data-sm-form]').get_by_role("button", name="Пересчитать").click()
            page.wait_for_function("document.querySelector('[data-sm-status]').textContent.includes('Пересчёт выполняется')")
            assert "Снимок обновлён" not in page.locator('[data-sm-status]').inner_text()
            page.wait_for_function("document.querySelector('[data-sm-status]').textContent.includes('Снимок обновлён · среднее по 60')")
            refresh_mode.update(kind="failed", period=75, polls=0, old_generation=len(refreshes))
            page.locator('[name="period"]').fill("75")
            page.locator('[data-sm-form]').get_by_role("button", name="Пересчитать").click()
            page.wait_for_function("document.querySelector('[data-sm-status]').dataset.tone==='error'")
            assert "Пересчёт не выполнен" in page.locator('[data-sm-status]').inner_text()
            assert "Предыдущий снимок" in page.locator('[data-sm-status]').inner_text()
            assert not errors, errors
            browser.close()
        shell_checks()
    finally:
        server.shutdown()
        server.server_close()
    print(json.dumps({"status": "ok", "checked": ["unknown_not_zero", "warehouse_forecast", "stale_warning", "quality_details", "sort", "hidden_columns", "search", "display_modes", "deficit_jump", "independent_period", "bounded_horizontal_dom", "no_cache_not_published", "old_target_cache_not_published", "refresh_failure_visible", "composed_shell_nav", "compact_row_geometry"], "api_requests": len(requests)}))


def shell_checks():
    """Check the composed main page, not just the isolated panel."""
    from urllib.parse import urlparse, parse_qs
    from playwright.sync_api import sync_playwright, expect
    from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
    from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def monitor_route(route):
                query = parse_qs(urlparse(route.request.url).query)
                assert route.request.method == "GET", "Opening the monitor must not start a calculation"
                route.fulfill(json=fixture(int(query.get("period_days", [14])[0]), int(query.get("horizon_days", [60])[0]), int(query.get("date_offset", [0])[0])))
            page.route("**/v1/sheet-vitrina-v1/stock-monitor?**", monitor_route)
            page.route("**/*.css", lambda route: route.fulfill(path=str(ROOT / "packages/adapters/templates/sheet_vitrina_v1_ui_system.css"), content_type="text/css"))
            page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
            page.locator('[data-unified-tab-button="warehouses"]').click()
            page.locator('[data-open-stock-monitor]').click()
            expect(page.locator('[data-stock-monitor]')).to_be_visible()
            expect(page.locator('[data-warehouse-cost-view]')).to_be_hidden()
            expect(page.locator('[data-fbs-orders-view]')).to_be_hidden()
            page.wait_for_selector('[data-sm-expand="3"]')
            first_cell = page.locator('[data-sm-row="3"][data-sm-cell="0"]')
            assert first_cell.evaluate("el=>el.getBoundingClientRect().height") <= 30
            assert first_cell.locator('.sm-number').evaluate("el=>el.getBoundingClientRect().height") <= 15
            assert first_cell.evaluate("el=>Math.abs(el.closest('td').getBoundingClientRect().width-38)<1")
            page.locator('[data-sm-expand="3"]').click()
            evidence = os.environ.get("WBC_STOCK_MONITOR_EVIDENCE_DIR")
            if evidence:
                target = Path(evidence)
                target.mkdir(parents=True, exist_ok=True)
                for theme in ("dark", "light"):
                    # The application currently provides dark tokens. Light tokens
                    # here are a fixture proving the component inherits its theme.
                    if theme == "light":
                        page.evaluate("""() => {const values={'--ui-bg':'#f5f6f8','--ui-surface':'#ffffff','--ui-surface-subtle':'#f0f2f5','--ui-surface-elevated':'#ffffff','--ui-control':'#ffffff','--ui-text':'#20252e','--ui-text-muted':'#626c7b','--ui-text-subtle':'#364152','--ui-border':'#e3e7ed','--ui-border-strong':'#ced4dc','--ui-warning':'#a86b10','--ui-danger':'#b52c38'};for(const [key,value] of Object.entries(values))document.documentElement.style.setProperty(key,value);document.documentElement.style.colorScheme='light';}""")
                    for width in (1440, 900):
                        page.set_viewport_size({"width": width, "height": 1000})
                        page.screenshot(path=str(target / f"monitor-{theme}-{width}.png"))
            page.locator('[data-open-warehouse-costs]').click()
            expect(page.locator('[data-stock-monitor]')).to_be_hidden()
            expect(page.locator('[data-warehouse-cost-view]')).to_be_visible()
            page.locator('[data-open-stock-monitor]').click()
            expect(page.locator('[data-stock-monitor]')).to_be_visible()
            expect(page.locator('[data-warehouse-cost-view]')).to_be_hidden()
            assert not errors, errors
            browser.close()


if __name__ == "__main__":
    main()
