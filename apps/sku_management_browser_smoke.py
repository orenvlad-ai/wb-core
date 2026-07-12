"""Browser behavior smoke for SKU sorting, filters, columns and inline confirmation."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import _render_sheet_vitrina_web_vitrina_ui


def main() -> None:
    html = _render_sheet_vitrina_web_vitrina_ui(
        read_path="/v1/sheet-vitrina-v1/web-vitrina",
        operator_path="/sheet-vitrina-v1/operator",
        refresh_path="/v1/sheet-vitrina-v1/refresh",
        job_path="/v1/sheet-vitrina-v1/job",
        role="operator",
        allowed_sections=["sku_management"],
        active_tab="sku-management",
    )
    settings = _settings()
    saved_payloads: list[dict[str, object]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        def route_handler(route):
            request = route.request
            path = request.url.split("sku.test", 1)[-1]
            if path == "/page":
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/settings"):
                if request.method == "POST":
                    body = json.loads(request.post_data or "{}")
                    saved_payloads.append(body)
                    settings["revision"] = int(settings["revision"]) + 1
                    settings["forecast"] = body.get("forecast") or settings["forecast"]
                    settings["table"] = body.get("table") or settings["table"]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/price/preview"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"status":"preview_ready","preview":{"preview_id":"price-preview","operation_id":"op-price","parameter":"seller_price","nm_id":101,"current":{"price":1000,"discount":10,"discountedPrice":900},"new":{"price":1000,"discount":15,"discountedPrice":850},"target_seller_price":850,"current_buyer_price":777,"estimated_buyer_price":None,"warnings":["same_parameter_stabilization"],"stabilization_warnings":[{"code":"same_parameter_stabilization","message":"Цена этого SKU изменялась 2 дня назад. Для стабильного наблюдения рекомендуется подождать ещё 1 день."}]}}))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/price/commit"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"status":"success","confirmed_value":850,"event":{"event_id":"event-1"}}))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/history"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"rows":[],"pagination":{"limit":50,"offset":0,"total":0}}))
                return
            if path == "/v1/sheet-vitrina-v1/sku-management":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"settings":settings,"rows":_rows(),"meta":{"writes_enabled":True}}))
                return
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')

        page.route("http://sku.test/**", route_handler)
        page.goto("http://sku.test/page", wait_until="domcontentloaded")
        page.wait_for_selector("[data-sku-management-body] tr")
        if page.locator("[data-sku-management-body] tr").count() != 2:
            raise AssertionError("SKU table must render two active rows")
        if "HIGH" not in page.locator("[data-sku-management-body] tr").first.inner_text():
            raise AssertionError("default sort must put highest/nearest risk first")

        page.locator('[data-sku-sort="risk"]').click()
        if "LOW" not in page.locator("[data-sku-management-body] tr").first.inner_text():
            raise AssertionError("first header click must sort risk ascending")
        page.locator('[data-sku-sort="risk"]').click()
        if "HIGH" not in page.locator("[data-sku-management-body] tr").first.inner_text():
            raise AssertionError("second header click must sort risk descending")
        page.locator('[data-sku-sort="risk"]').click()

        page.locator('[data-sku-filter="risk"]').select_option("low")
        if page.locator("[data-sku-management-body] tr").count() != 1 or "LOW" not in page.locator("[data-sku-management-body]").inner_text():
            raise AssertionError("risk filter must narrow rows")
        page.locator('[data-sku-filter="risk"]').select_option("")

        page.locator("[data-sku-column-manager] summary").click()
        page.locator('[data-sku-column-visible="buyer_price"]').uncheck()
        if page.locator('[data-sku-sort="buyer_price"]').count() != 0:
            raise AssertionError("column selector must hide buyer price column")
        page.wait_for_timeout(500)
        if not saved_payloads or "table" not in saved_payloads[-1]:
            raise AssertionError("column/filter/sort preferences must persist through server-owned settings")

        if page.locator('[data-sku-price-input="101"]').count() != 0:
            raise AssertionError("seller price must start as a compact click-to-edit value")
        page.locator('[data-sku-price-edit="101"]').click()
        page.locator('[data-sku-price-input="101"]').fill("850")
        page.locator('[data-sku-price-preview="101"]').click()
        page.wait_for_selector("[data-sku-management-modal]:not([hidden])")
        if "Всё равно изменить" not in page.locator("[data-sku-modal-confirm]").inner_text():
            raise AssertionError("stabilization warning must expose explicit override action")
        page.locator("[data-sku-modal-confirm]").click()
        page.wait_for_selector("[data-sku-management-modal] .prices-badge.success")
        if "WB readback подтверждён" not in page.locator("[data-sku-management-modal]").inner_text():
            raise AssertionError("UI success must wait for confirmed readback response")
        browser.close()
    print("sku_management_browser_smoke: OK")


def _settings() -> dict[str, object]:
    return {
        "status":"ok", "revision":0, "updated_at":"", "canonical_store":"server_runtime_user_config",
        "forecast":{"sales_avg_period_days":14,"forecast_horizon_days":90,"future_order_period_days":30,"production_lead_days":30,"factory_to_ff_lead_days":30,"ff_to_wb_lead_days":7,"safety_stock_days":14,"price_stabilization_days":3,"bid_stabilization_days":3,"cross_warnings_enabled":True,"order_batch_qty":100},
        "table":{"visible_columns":[],"column_order":[],"column_widths":{},"filters":{},"sort":[{"key":"risk_rank","direction":"desc"},{"key":"deficit_date","direction":"asc"}]},
    }


def _rows() -> list[dict[str, object]]:
    common = {"quality":"complete","quality_warnings":[],"seller_price":900,"buyer_price":777,"spp_proxy":0.136,"promo_label":"1 / 2","promo_count":2,"campaign_count":2,"placement_count":2,"current_bid":None,"ad_options":[{"advert_id":77,"campaign_name":"Search","placement":"search","current_bid_rub":15},{"advert_id":78,"campaign_name":"Recommendations","placement":"recommendations","current_bid_rub":17}],"ads_drr":0.1,"ads_drr_attributed":0.2,"funnel":{"view_count":100,"openCount":50,"cartCount":20},"orders":10,"sales_rub":9000,"profit_rub":2000,"margin_pct":0.22,"last_price_change_at":"","last_bid_change_at":""}
    return [
        {**common,"nm_id":101,"sku":"HIGH","name":"High risk","risk":"high","risk_rank":2,"deficit_date":"2026-07-15","coverage_pct":20,"deficit_units":80,"first_problem_district":"central","reason":"near deficit"},
        {**common,"nm_id":102,"sku":"LOW","name":"Low risk","risk":"low","risk_rank":0,"deficit_date":None,"coverage_pct":140,"deficit_units":0,"first_problem_district":None,"reason":"no deficit"},
    ]


if __name__ == "__main__":
    main()
