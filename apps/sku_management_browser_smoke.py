"""Browser behavior smoke for SKU management; every upstream write is fake."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, sync_playwright

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
    baseline_forecast = dict(settings["forecast"])
    saved_payloads: list[dict[str, object]] = []
    preview_payloads: list[dict[str, object]] = []
    commit_payloads: list[dict[str, object]] = []
    history_queries: list[dict[str, list[str]]] = []
    committed_parameters: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

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
                    forecast = body.get("forecast") or {}
                    if int(forecast.get("forecast_horizon_days") or 0) < 7:
                        route.fulfill(status=422, content_type="application/json", body='{"error":"forecast_horizon_days must be between 7 and 365"}')
                        return
                    settings["revision"] = int(settings["revision"]) + 1
                    settings["forecast"] = forecast or settings["forecast"]
                    settings["table"] = body.get("table") or settings["table"]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(settings))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/price/preview"):
                body = json.loads(request.post_data or "{}")
                preview_payloads.append(body)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "status": "preview_ready",
                    "preview": {
                        "preview_id": "price-preview",
                        "operation_id": "op-price",
                        "parameter": "seller_price",
                        "nm_id": 101,
                        "current": {"price": 1000, "discount": 5, "discountedPrice": 950},
                        "new": {"price": 1000, "discount": 15, "discountedPrice": 850},
                        "target_seller_price": 850,
                        "current_buyer_price": 777,
                        "estimated_buyer_price": None,
                        "warnings": ["same_parameter_stabilization"],
                        "override_required_warnings": ["same_parameter_stabilization"],
                        "stabilization_warnings": [{
                            "code": "same_parameter_stabilization",
                            "message": "Цена этого SKU изменялась 2 дня назад. Рекомендуется подождать ещё 1 день.",
                        }],
                    },
                }))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/price/commit"):
                body = json.loads(request.post_data or "{}")
                commit_payloads.append(body)
                committed_parameters.append("seller_price")
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "status": "success",
                    "confirmed_value": 850,
                    "confirmed_price": 1000,
                    "confirmed_discount": 15,
                    "readback_status": "matching",
                    "event": {"event_id": "event-price"},
                }))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/bid/preview"):
                body = json.loads(request.post_data or "{}")
                preview_payloads.append(body)
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "status": "preview_ready",
                    "preview": {
                        "preview_id": "bid-preview",
                        "operation_id": "op-bid",
                        "parameter": "advertising_bid",
                        "nm_id": 101,
                        "advert_id": 78,
                        "campaign_name": "Recommendations",
                        "placement": "recommendations",
                        "old_value": 17,
                        "requested_value": 18,
                        "min_bid_rub": 10,
                        "warnings": ["cross_parameter_stabilization"],
                        "override_required_warnings": ["cross_parameter_stabilization"],
                        "stabilization_warnings": [{
                            "code": "cross_parameter_stabilization",
                            "message": "Продолжается период стабилизации после изменения цены.",
                        }],
                    },
                }))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/bid/commit"):
                body = json.loads(request.post_data or "{}")
                commit_payloads.append(body)
                committed_parameters.append("advertising_bid")
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "status": "success",
                    "confirmed_value": 18,
                    "readback_status": "matching",
                    "event": {"event_id": "event-bid"},
                }))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/history"):
                query = parse_qs(urlsplit(path).query)
                history_queries.append(query)
                offset = int((query.get("offset") or ["0"])[0])
                parameter = (query.get("parameter") or [""])[0]
                rows = [_history_row("baseline", "seller_price", 900, 950)] if offset == 0 else [_history_row("page-2", "advertising_bid", 16, 17)]
                if committed_parameters and offset == 0:
                    rows = [_history_row("event-" + committed_parameters[-1], committed_parameters[-1], 17 if committed_parameters[-1] == "advertising_bid" else 950, 18 if committed_parameters[-1] == "advertising_bid" else 850)] + rows
                if parameter:
                    rows = [item for item in rows if item["parameter"] == parameter]
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "rows": rows,
                    "pagination": {"limit": 50, "offset": offset, "total": 51},
                }))
                return
            if path == "/v1/sheet-vitrina-v1/sku-management":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "settings": settings,
                    "rows": _rows(),
                    "meta": {"writes_enabled": True},
                }))
                return
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')

        page.route("http://sku.test/**", route_handler)
        page.goto("http://sku.test/page", wait_until="domcontentloaded")
        page.wait_for_selector("[data-sku-management-body] tr")
        if page.locator("[data-sku-management-body] tr").count() != 3:
            raise AssertionError("SKU table must render every active row")
        _assert_first_row(page, "HIGH", "default risk/deficit sort")

        _assert_three_state_sort(page, "risk", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "seller_price", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "coverage_pct", "HIGH", "LOW", "HIGH")
        _assert_three_state_sort(page, "deficit_units", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "last_price_change_at", "LOW", "HIGH", "HIGH")

        _set_filter(page, "search", "102")
        _assert_only_row(page, "LOW", "SKU/nmID search")
        _set_filter(page, "search", "")
        _set_filter(page, "risk", "unknown")
        _assert_only_row(page, "UNKNOWN", "risk filter")
        _set_filter(page, "risk", "")
        for value, token in (("yes", "HIGH"), ("no", "LOW"), ("unknown", "UNKNOWN")):
            _set_filter(page, "promo", value)
            _assert_only_row(page, token, f"promo={value} filter")
        _set_filter(page, "promo", "")
        _set_filter(page, "coverage_min", "100")
        _set_filter(page, "coverage_max", "160")
        _assert_only_row(page, "LOW", "coverage range")
        _set_filter(page, "coverage_min", "")
        _set_filter(page, "coverage_max", "")
        _set_filter(page, "deficit_min", "70")
        _set_filter(page, "deficit_max", "90")
        _assert_only_row(page, "HIGH", "deficit range")
        _set_filter(page, "deficit_min", "")
        _set_filter(page, "deficit_max", "")
        _set_filter(page, "search", "no-such-sku")
        if "Нет SKU" not in page.locator("[data-sku-management-body]").inner_text():
            raise AssertionError("empty filtered state must be explicit")
        _set_filter(page, "search", "")

        manager = page.locator("[data-sku-column-manager]")
        manager.locator("summary").click()
        manager.locator('[data-sku-column-visible="buyer_price"]').uncheck()
        manager.locator('[data-sku-column-width="product"]').fill("300")
        manager.locator('[data-sku-column-width="product"]').press("Tab")
        manager.locator('[data-sku-column-down="product"]').click()
        page.wait_for_timeout(700)
        if page.locator('[data-sku-sort="buyer_price"]').count() != 0:
            raise AssertionError("column selector must hide buyer price")
        if not saved_payloads or "table" not in saved_payloads[-1]:
            raise AssertionError("column/filter/sort preferences must persist server-side")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("[data-sku-management-body] tr")
        if page.locator("[data-sku-management-head] th").first.get_attribute("data-sku-sort") != "risk":
            raise AssertionError("column order must survive page refresh")
        style = page.locator('[data-sku-sort="product"]').get_attribute("style") or ""
        if "300px" not in style or page.locator('[data-sku-sort="buyer_price"]').count() != 0:
            raise AssertionError("column width/visibility must survive page refresh")
        manager = page.locator("[data-sku-column-manager]")
        manager.locator("summary").click()
        manager.locator('[data-sku-column-up="product"]').click()
        manager.locator('[data-sku-column-visible="buyer_price"]').check()
        page.wait_for_timeout(700)

        page.locator("[data-sku-management-settings] summary").click()
        horizon = page.locator('[data-sku-setting="forecast_horizon_days"]')
        horizon.fill("1")
        page.locator("[data-sku-settings-save]").click()
        page.wait_for_function("() => document.querySelector('[data-sku-management-error]').textContent.includes('between 7 and 365')")
        updated = {
            "sales_avg_period_days": "30", "forecast_horizon_days": "120", "future_order_period_days": "14",
            "production_lead_days": "20", "factory_to_ff_lead_days": "15", "ff_to_wb_lead_days": "5",
            "safety_stock_days": "0", "order_batch_qty": "50", "price_stabilization_days": "0",
            "bid_stabilization_days": "0",
        }
        for key, value in updated.items():
            node = page.locator(f'[data-sku-setting="{key}"]')
            node.select_option(value) if key == "sales_avg_period_days" else node.fill(value)
        page.locator('[data-sku-setting="cross_warnings_enabled"]').uncheck()
        revision_before = int(settings["revision"])
        page.locator("[data-sku-settings-save]").click()
        page.wait_for_function("expected => document.querySelector('[data-sku-setting=\"forecast_horizon_days\"]').value === expected", arg="120")
        page.wait_for_timeout(300)
        if int(settings["revision"]) <= revision_before or settings["forecast"]["safety_stock_days"] != 0:
            raise AssertionError("all forecast inputs, including zero-day switches, must persist")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("[data-sku-management-body] tr")
        if page.locator('[data-sku-setting="forecast_horizon_days"]').input_value() != "120" or page.locator('[data-sku-setting="cross_warnings_enabled"]').is_checked():
            raise AssertionError("forecast settings must survive refresh")
        page.locator("[data-sku-management-settings] summary").click()
        for key, value in baseline_forecast.items():
            node = page.locator(f'[data-sku-setting="{key}"]')
            if key == "cross_warnings_enabled":
                node.check() if value else node.uncheck()
            elif key == "sales_avg_period_days":
                node.select_option(str(value))
            else:
                node.fill(str(value))
        page.locator("[data-sku-settings-save]").click()
        page.wait_for_timeout(500)

        history = page.locator("[data-sku-management-history]")
        history.locator("summary").click()
        page.wait_for_function("() => document.querySelector('[data-sku-history-body]').textContent.includes('900')")
        if "900 → 950" not in page.locator("[data-sku-history-body]").inner_text():
            raise AssertionError("collapsed history must load persisted events when opened")
        page.locator('[data-sku-history-filter="parameter"]').select_option("advertising_bid")
        page.locator("[data-sku-history-refresh]").click()
        page.wait_for_timeout(100)
        if not history_queries or history_queries[-1].get("parameter") != ["advertising_bid"]:
            raise AssertionError("history filters must reach the server")
        page.locator('[data-sku-history-filter="parameter"]').select_option("")
        page.locator("[data-sku-history-refresh]").click()
        page.locator("[data-sku-history-next]").click()
        page.wait_for_function("() => document.querySelector('[data-sku-history-page]').textContent === '2'")
        if "16 → 17" not in page.locator("[data-sku-history-body]").inner_text():
            raise AssertionError("history pagination must load the next page")
        page.locator("[data-sku-history-prev]").click()
        page.wait_for_function("() => document.querySelector('[data-sku-history-page]').textContent === '1'")

        if page.locator('[data-sku-price-input="101"]').count() != 0:
            raise AssertionError("seller price must start as compact click-to-edit value")
        page.locator('[data-sku-price-edit="101"]').click()
        page.locator('[data-sku-price-input="101"]').fill("850")
        page.locator('[data-sku-price-preview="101"]').click()
        page.wait_for_selector("[data-sku-management-modal]:not([hidden])")
        if "Всё равно изменить" not in page.locator("[data-sku-modal-confirm]").inner_text():
            raise AssertionError("stabilization warning must expose explicit override")
        page.get_by_role("button", name="Отменить", exact=True).click()
        if not page.locator("[data-sku-management-modal]").is_hidden() or commit_payloads:
            raise AssertionError("Отменить must close preview without mutation")
        page.locator('[data-sku-price-preview="101"]').click()
        page.wait_for_selector("[data-sku-management-modal]:not([hidden])")
        page.locator("[data-sku-modal-confirm]").click()
        page.wait_for_selector("[data-sku-management-modal] .prices-badge.success")
        modal_text = page.locator("[data-sku-management-modal]").inner_text()
        if "WB readback подтверждён" not in modal_text or "discount 15%" not in modal_text:
            raise AssertionError("price success must expose exact confirmed tuple after readback")
        if not commit_payloads[-1].get("override_stabilization") or not commit_payloads[-1].get("override_warnings"):
            raise AssertionError("explicit UI override must be sent and audited")
        page.wait_for_selector("[data-sku-management-modal]", state="hidden")
        page.wait_for_timeout(150)
        if "950 → 850" not in page.locator("[data-sku-history-body]").inner_text():
            raise AssertionError("open history must refresh after confirmed mutation")

        page.locator('[data-sku-bid-edit="101"]').click()
        page.locator('[data-sku-bid-option="101"]').select_option("78|recommendations")
        page.locator('[data-sku-bid-input="101"]').fill("18")
        page.locator('[data-sku-bid-preview="101"]').click()
        page.wait_for_selector("[data-sku-management-modal]:not([hidden])")
        if "78 / recommendations" not in page.locator("[data-sku-management-modal]").inner_text() or "Всё равно изменить" not in page.locator("[data-sku-modal-confirm]").inner_text():
            raise AssertionError("multiple campaigns require exact advert_id/placement and cross-warning override")
        if preview_payloads[-1].get("advert_id") != 78 or preview_payloads[-1].get("placement") != "recommendations":
            raise AssertionError("frontend must not collapse placement identity")
        page.locator("[data-sku-modal-confirm]").click()
        page.wait_for_selector("[data-sku-management-modal] .prices-badge.success")
        if commit_payloads[-1].get("preview_id") != "bid-preview" or not commit_payloads[-1].get("override_stabilization"):
            raise AssertionError("bid commit must use only the confirmed preview with override")
        page.wait_for_selector("[data-sku-management-modal]", state="hidden")
        if "17 → 18" not in page.locator("[data-sku-history-body]").inner_text():
            raise AssertionError("bid event must appear in persistent history after refresh")
        history.locator("summary").click()
        if history.get_attribute("open") is not None:
            raise AssertionError("history block must close")
        browser.close()
    print("sku_management_browser_smoke: OK")


def _assert_first_row(page: Page, token: str, context: str) -> None:
    if token not in page.locator("[data-sku-management-body] tr").first.inner_text():
        raise AssertionError(f"{context}: expected first row {token}")


def _assert_three_state_sort(page: Page, key: str, asc: str, desc: str, none: str) -> None:
    header = page.locator(f'[data-sku-sort="{key}"]')
    header.click()
    _assert_first_row(page, asc, f"{key} asc")
    page.locator(f'[data-sku-sort="{key}"]').click()
    _assert_first_row(page, desc, f"{key} desc")
    page.locator(f'[data-sku-sort="{key}"]').click()
    _assert_first_row(page, none, f"{key} none")


def _set_filter(page: Page, key: str, value: str) -> None:
    node = page.locator(f'[data-sku-filter="{key}"]')
    node.select_option(value) if key in {"risk", "promo"} else node.fill(value)


def _assert_only_row(page: Page, token: str, context: str) -> None:
    rows = page.locator("[data-sku-management-body] tr")
    if rows.count() != 1 or token not in rows.first.inner_text():
        raise AssertionError(f"{context}: expected only {token}")


def _settings() -> dict[str, object]:
    return {
        "status": "ok", "revision": 0, "updated_at": "", "canonical_store": "server_runtime_user_config",
        "forecast": {"sales_avg_period_days": 14, "forecast_horizon_days": 90, "future_order_period_days": 30, "production_lead_days": 30, "factory_to_ff_lead_days": 30, "ff_to_wb_lead_days": 7, "safety_stock_days": 14, "price_stabilization_days": 3, "bid_stabilization_days": 3, "cross_warnings_enabled": True, "order_batch_qty": 100},
        "table": {"visible_columns": [], "column_order": [], "column_widths": {}, "filters": {}, "sort": [{"key": "risk_rank", "direction": "desc"}, {"key": "deficit_date", "direction": "asc"}]},
    }


def _rows() -> list[dict[str, object]]:
    options = [{"advert_id": 77, "campaign_name": "Search", "placement": "search", "current_bid_rub": 15}, {"advert_id": 78, "campaign_name": "Recommendations", "placement": "recommendations", "current_bid_rub": 17}]
    common = {"quality": "complete", "quality_warnings": [], "buyer_price_source": "public_wb_card", "buyer_price_quality": "observed", "buyer_price_freshness": "2026-07-13", "spp_proxy": 0.136, "campaign_count": 2, "placement_count": 2, "current_bid": None, "ad_options": options, "ads_drr": 0.1, "ads_drr_attributed": 0.2, "funnel": {"view_count": 100, "openCount": 50, "cartCount": 20, "addToCartConversion": 0.4, "cartToOrderConversion": 0.5}, "orders": 10, "sales_rub": 9000, "profit_rub": 2000, "margin_pct": 0.22, "last_bid_change_at": "2026-07-01T10:00:00Z"}
    return [
        {**common, "nm_id": 101, "sku": "HIGH", "name": "High risk", "risk": "high", "risk_rank": 2, "deficit_date": "2026-07-15", "coverage_pct": 20, "deficit_units": 80, "first_problem_district": "central", "reason": "near deficit", "seller_price": 950, "buyer_price": 777, "promo_label": "1 / 2", "promo_count": 1, "promo_participation": 1, "promo_freshness": "2026-07-13", "last_price_change_at": "2026-07-13T10:00:00Z"},
        {**common, "nm_id": 102, "sku": "LOW", "name": "Low risk", "risk": "low", "risk_rank": 0, "deficit_date": "2026-09-01", "coverage_pct": 140, "deficit_units": 0, "first_problem_district": None, "reason": "no deficit", "seller_price": 700, "buyer_price": 650, "promo_label": "0 / 2", "promo_count": 0, "promo_participation": 0, "promo_freshness": "2026-07-13", "last_price_change_at": "2026-07-01T10:00:00Z"},
        {**common, "nm_id": 103, "sku": "UNKNOWN", "name": "Partial evidence", "risk": "unknown", "risk_rank": -1, "deficit_date": None, "coverage_pct": None, "deficit_units": None, "first_problem_district": "unknown", "reason": "regional unknown", "quality": "partial", "quality_warnings": ["regional evidence missing"], "seller_price": 800, "buyer_price": None, "buyer_price_quality": "missing", "buyer_price_freshness": "", "promo_label": "н/д", "promo_count": None, "promo_participation": None, "promo_freshness": "", "last_price_change_at": ""},
    ]


def _history_row(event_id: str, parameter: str, old_value: float, confirmed_value: float) -> dict[str, object]:
    return {"event_id": event_id, "nm_id": 101, "parameter": parameter, "old_value": old_value, "requested_value": confirmed_value, "confirmed_value": confirmed_value, "delta": confirmed_value - old_value, "requested_at": "2026-07-13T09:59:00Z", "confirmed_at": "2026-07-13T10:00:00Z", "actor": "operator", "source": "sku_management", "advert_id": 78 if parameter == "advertising_bid" else None, "campaign": "Recommendations" if parameter == "advertising_bid" else "", "placement": "recommendations" if parameter == "advertising_bid" else "", "preview_id": "preview", "correlation_id": "operation", "commit_status": "confirmed", "readback_status": "matching", "warnings": ["same_parameter_stabilization"], "stabilization_override": True, "warning_override": True, "error": ""}


if __name__ == "__main__":
    main()
