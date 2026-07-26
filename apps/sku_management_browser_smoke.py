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
    table_reads = [0]
    history_reads = [0]
    price_commit_mode = ["success"]
    force_settings_conflict = [False]
    console_errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.add_init_script(
            """
            const nativeFetch = window.fetch.bind(window);
            window.fetch = function(input, init) {
              const url = String(input || "");
              const delay = url.includes("/sku-management/price/preview") || url.includes("/sku-management/bid/preview")
                ? 160
                : url.includes("/sku-management/price/commit") || url.includes("/sku-management/bid/commit")
                  ? 360
                  : 0;
              return delay
                ? new Promise((resolve, reject) => window.setTimeout(() => nativeFetch(input, init).then(resolve, reject), delay))
                : nativeFetch(input, init);
            };
            """
        )
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
        page.on("pageerror", lambda error: console_errors.append(str(error)))

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
                    if force_settings_conflict[0]:
                        force_settings_conflict[0] = False
                        settings["revision"] = int(settings["revision"]) + 1
                        route.fulfill(
                            status=409,
                            content_type="application/json",
                            body=json.dumps({
                                "error": "config_revision_conflict",
                                "current": {
                                    "revision": settings["revision"],
                                    "config": {"forecast": settings["forecast"], "table": settings["table"]},
                                },
                            }),
                        )
                        return
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
                if price_commit_mode[0] == "mismatch":
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({
                        "status": "success",
                        "confirmed_value": 840,
                        "readback_status": "mismatch",
                        "error": "WB readback mismatch",
                    }))
                    return
                committed_parameters.append("seller_price")
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "status": "success",
                    "confirmed_value": 850,
                    "confirmed_price": 1000,
                    "confirmed_discount": 15,
                    "readback_status": "matching",
                    "buyer_price": {"value": 701, "quality": "observed", "source": "public_wb_card", "freshness": "2026-07-14"},
                    "event": {"event_id": "event-price", "confirmed_at": "2026-07-14T10:00:00Z"},
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
                    "event": {"event_id": "event-bid", "confirmed_at": "2026-07-14T10:05:00Z"},
                }))
                return
            if path.startswith("/v1/sheet-vitrina-v1/sku-management/history"):
                history_reads[0] += 1
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
                table_reads[0] += 1
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "settings": settings,
                    "rows": _rows(),
                    "meta": {
                        "writes_enabled": True,
                        "metric_policy": {
                            "business_timezone": "Asia/Yekaterinburg",
                            "cumulative_exact_date": "2026-07-24",
                            "cumulative_no_fallback": True,
                        },
                        "warehouse_exclusion": {
                            "active": True,
                            "excluded_wb_warehouse_ids": [101, 102],
                            "names": ["Альфа", "Бета"],
                        },
                    },
                }))
                return
            if path == "/v1/sheet-vitrina-v1/sku-management/sku/101":
                route.fulfill(status=200, content_type="application/json", body=json.dumps({
                    "contract_name": "sheet_vitrina_v1_sku_management_detail",
                    "row": _rows()[0],
                    "meta": {"warehouse_exclusion": {"active": True, "names": ["Альфа", "Бета"]}},
                    "history": {
                        "rows": [{
                            "nm_id": 101,
                            "parameter": "seller_price",
                            "old_value": 1000,
                            "requested_value": 990,
                            "confirmed_value": 990,
                            "actor": "operator",
                            "commit_status": "confirmed",
                            "readback_status": "matching",
                            "confirmed_at": "2026-07-25T08:00:00Z",
                            "warnings": [],
                        }],
                    },
                }))
                return
            route.fulfill(status=404, content_type="application/json", body='{"error":"not found"}')

        page.route("http://sku.test/**", route_handler)
        page.goto("http://sku.test/page", wait_until="domcontentloaded")
        page.wait_for_selector("[data-sku-management-body] tr")
        if page.locator("[data-sku-management-body] tr").count() != 3:
            raise AssertionError("retired persisted filters must not hide active SKU rows")
        _assert_first_row(page, "HIGH", "default risk/deficit sort")
        if page.locator("[data-sku-filter]").count() != 1 or page.locator('[data-sku-filter="search"]').count() != 1:
            raise AssertionError("filter toolbar must contain only SKU search")
        if page.locator("[data-sku-column-manager]").count() != 1:
            raise AssertionError("filter toolbar must retain the server-owned column selector")
        retired_controls = page.locator('[data-sku-filter="risk"], [data-sku-filter="promo"], [data-sku-filter^="coverage_"], [data-sku-filter^="deficit_"]')
        if retired_controls.count() or "Проблемный округ" in page.locator("[data-sku-management-panel]").inner_text():
            raise AssertionError("retired filters/problem-district presentation must not render")
        nearest_text = page.locator('[data-sku-cell="nearest_inbound"]').first.inner_text().replace("\xa0", " ")
        if "INV-071" not in nearest_text or "2 500 шт." not in nearest_text:
            raise AssertionError("nearest supplier invoice/date/SKU quantity must render compactly")
        product_cell = page.locator('[data-sku-management-body] tr').first.locator('[data-sku-cell="product"]')
        if "HIGH risk product" not in product_cell.inner_text() or "nmID 101" not in product_cell.inner_text() or "SKU-HIGH-INTERNAL" in product_cell.inner_text():
            raise AssertionError("product cell must show title first, secondary nmID, and no internal SKU")
        if page.locator('[data-sku-sort="product"]').inner_text().strip() != "Название / nmID":
            raise AssertionError("compact product header is required")
        if "Учитывается политика инцидентов · Не участвуют: Альфа, Бета" not in page.locator("[data-sku-warehouse-exclusion-summary]").inner_text():
            raise AssertionError("SKU surface must explain the canonical warehouse exclusions")
        if "за 24.07" not in page.locator('[data-sku-sort="profit_rub"]').inner_text() or "за 24.07" not in page.locator('[data-sku-sort="ads_spend_rub"]').inner_text():
            raise AssertionError("cumulative metric headers must expose exact D-2")
        if "обновлено 25.07" not in page.locator('[data-sku-cell="seller_price"]').first.inner_text():
            raise AssertionError("snapshot cells must expose last successful update")

        modal_styles = page.evaluate(
            """() => {
              const cards = Array.from(document.querySelectorAll('.operator-modal-card')).map((node) => {
                const style = getComputedStyle(node);
                return {background: style.backgroundColor, opacity: style.opacity};
              });
              const backdrop = getComputedStyle(document.querySelector('.operator-modal-backdrop'));
              const header = getComputedStyle(document.querySelector('[data-sku-management-head] th'));
              const shell = document.querySelector('.sku-management-table-shell');
              const cell = getComputedStyle(document.querySelector('[data-sku-management-body] td'));
              return {
                cards,
                backdrop: backdrop.backgroundColor,
                modalZ: Number(backdrop.zIndex),
                headerZ: Number(header.zIndex),
                headerPosition: header.position,
                headerBackground: header.backgroundColor,
                shellBorder: getComputedStyle(shell).borderStyle,
                horizontalOverflow: shell.scrollWidth > shell.clientWidth,
                rowSeparator: cell.borderBottomStyle,
              };
            }"""
        )
        page.evaluate(
            """() => {
              const opener = document.createElement('button');
              opener.id = 'fixture-vitrina-sku-opener';
              document.body.appendChild(opener);
              openVitrinaSkuModal(101, opener);
            }"""
        )
        page.locator('[data-sku-management-modal][data-sku-modal-state="quick_ready"]').wait_for()
        quick_modal = page.locator("[data-sku-management-modal]")
        if "HIGH risk product" not in quick_modal.inner_text() or "nmID 101" not in quick_modal.inner_text():
            raise AssertionError("Vitrina SKU popup must use the narrow canonical SKU detail")
        if quick_modal.locator("[data-quick-sku-price]").count() != 1 or quick_modal.locator("[data-quick-sku-bid-option]").count() != 1:
            raise AssertionError("Vitrina SKU popup must expose price and exact campaign/placement controls")
        if "confirmed / matching" not in quick_modal.inner_text():
            raise AssertionError("Vitrina SKU popup history must be filtered to the selected nmID and expose readback")
        page.keyboard.press("Escape")
        if not quick_modal.is_hidden():
            raise AssertionError("Escape must close the side-effect-free SKU popup")
        if len(modal_styles["cards"]) < 3 or any(item != {"background": "rgb(23, 25, 31)", "opacity": "1"} for item in modal_styles["cards"]):
            raise AssertionError(f"all operator modal cards must be fully opaque: {modal_styles}")
        if "0.76" not in modal_styles["backdrop"] or modal_styles["modalZ"] <= modal_styles["headerZ"]:
            raise AssertionError(f"modal backdrop/z-index contract mismatch: {modal_styles}")
        if modal_styles["headerPosition"] != "sticky" or modal_styles["headerBackground"] != "rgb(32, 36, 44)":
            raise AssertionError(f"structured opaque sticky header is missing: {modal_styles}")
        if modal_styles["shellBorder"] != "solid" or modal_styles["rowSeparator"] != "solid" or not modal_styles["horizontalOverflow"]:
            raise AssertionError(f"table grid/scroll structure mismatch: {modal_styles}")
        sticky = page.evaluate(
            """() => {
              const shell=document.querySelector('.sku-management-table-shell');
              const header=document.querySelector('[data-sku-column="product"]');
              const cell=document.querySelector('[data-sku-cell="product"]');
              const before={headerLeft:header.getBoundingClientRect().left,cellLeft:cell.getBoundingClientRect().left};
              shell.scrollLeft=640;
              const after={headerLeft:header.getBoundingClientRect().left,cellLeft:cell.getBoundingClientRect().left};
              const style=getComputedStyle(cell);
              const point=document.elementFromPoint(cell.getBoundingClientRect().left+12,cell.getBoundingClientRect().top+12);
              return {before,after,position:style.position,z:Number(style.zIndex),background:style.backgroundColor,topCell:point&&point.closest('[data-sku-cell]')&&point.closest('[data-sku-cell]').dataset.skuCell};
            }"""
        )
        if sticky["before"] != sticky["after"] or sticky["position"] != "sticky" or sticky["z"] < 3 or sticky["background"] != "rgb(23, 27, 34)" or sticky["topCell"] != "product":
            raise AssertionError(f"product column must stay opaque and above scrolled cells: {sticky}")
        page.locator("[data-sku-management-body] tr").first.hover()
        hover_background = page.locator("[data-sku-management-body] tr").first.locator("td").first.evaluate("node => getComputedStyle(node).backgroundColor")
        if hover_background != "rgb(29, 33, 41)":
            raise AssertionError("row hover must visibly preserve the dark management-table language")

        _assert_three_state_sort(page, "risk", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "seller_price", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "coverage_pct", "HIGH", "LOW", "HIGH")
        _assert_three_state_sort(page, "deficit_units", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "last_price_change_at", "LOW", "HIGH", "HIGH")
        _assert_three_state_sort(page, "nearest_inbound", "LOW", "HIGH", "HIGH")

        _set_filter(page, "search", "102")
        _assert_only_row(page, "LOW", "SKU/nmID search")
        _set_filter(page, "search", "no-such-sku")
        if "Нет SKU" not in page.locator("[data-sku-management-body]").inner_text():
            raise AssertionError("empty filtered state must be explicit")
        _set_filter(page, "search", "")

        manager = page.locator("[data-sku-column-manager]")
        manager.locator("summary").click()
        if not manager.locator('[data-sku-column-visible="product"]').is_disabled():
            raise AssertionError("mandatory product column must not be accidentally hidden")
        if not manager.locator('[data-sku-column-drag-handle="product"]').is_disabled() or manager.locator("[data-sku-column-row]").first.get_attribute("data-sku-column-row") != "product":
            raise AssertionError("mandatory product column must not move away from the first position")
        if manager.locator('[data-sku-column-visible="first_problem_district"]').count():
            raise AssertionError("retired problem-district preference must be migrated out of the selector")
        manager.locator('[data-sku-column-visible="buyer_price"]').uncheck()
        manager.locator('[data-sku-column-width="product"]').fill("300")
        manager.locator('[data-sku-column-width="product"]').press("Tab")
        original_ids = manager.locator("[data-sku-column-row]").evaluate_all("nodes => nodes.map(node => node.dataset.skuColumnRow)")
        manager.locator('[data-sku-column-row="buyer_price"]').drag_to(manager.locator('[data-sku-column-row="risk"]'))
        reordered_ids = manager.locator("[data-sku-column-row]").evaluate_all("nodes => nodes.map(node => node.dataset.skuColumnRow)")
        if reordered_ids == original_ids:
            raise AssertionError("drag-and-drop must immediately reorder even a hidden column")
        if reordered_ids[0] != "product":
            raise AssertionError("reordering other columns must preserve product first")
        page.wait_for_timeout(700)
        if page.locator('[data-sku-sort="buyer_price"]').count() != 0:
            raise AssertionError("column selector must hide buyer price")
        if not saved_payloads or "table" not in saved_payloads[-1]:
            raise AssertionError("column/filter/sort preferences must persist server-side")
        persisted_table = saved_payloads[-1]["table"]
        if persisted_table.get("filters") != {"search": ""} or "first_problem_district" in json.dumps(persisted_table):
            raise AssertionError("persisted presentation state must drop retired filters and problem district")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_selector("[data-sku-management-body] tr")
        expected_visible_first = next(item for item in reordered_ids if item != "buyer_price")
        if page.locator("[data-sku-management-head] th").first.get_attribute("data-sku-sort") != expected_visible_first:
            raise AssertionError("hidden-column order must survive page refresh without changing visible order")
        style = page.locator('[data-sku-sort="product"]').get_attribute("style") or ""
        if "300px" not in style or page.locator('[data-sku-sort="buyer_price"]').count() != 0:
            raise AssertionError("column width/visibility must survive page refresh")
        manager = page.locator("[data-sku-column-manager]")
        manager.locator("summary").click()
        restored_ids = manager.locator("[data-sku-column-row]").evaluate_all("nodes => nodes.map(node => node.dataset.skuColumnRow)")
        if restored_ids != reordered_ids:
            raise AssertionError("server-owned drag order must restore after reload")
        manager.locator('[data-sku-column-visible="buyer_price"]').check()
        page.wait_for_timeout(700)
        visible_ids = page.locator("[data-sku-management-head] th").evaluate_all("nodes => nodes.map(node => node.dataset.skuSort)")
        if (visible_ids.index("buyer_price") < visible_ids.index("risk")) != (restored_ids.index("buyer_price") < restored_ids.index("risk")):
            raise AssertionError("visibility and order must remain independent preferences")
        force_settings_conflict[0] = True
        manager.locator('[data-sku-column-drag-handle="buyer_price"]').press("ArrowDown")
        page.wait_for_function("() => document.querySelector('[data-sku-management-error]').textContent.includes('другой сессии')")

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

        _set_filter(page, "search", "risk")
        page.locator('[data-sku-sort="seller_price"]').click()
        page.wait_for_timeout(500)
        if page.locator('[data-sku-price-input="101"]').count() != 0:
            raise AssertionError("seller price must start as compact click-to-edit value")
        page.locator('[data-sku-price-edit="101"]').click()
        page.locator('[data-sku-price-input="101"]').fill("850")
        page.locator('[data-sku-price-preview="101"]').click()
        _assert_modal_state(page, "preview_loading")
        page.locator('[data-sku-modal-cancel]').first.click()
        page.wait_for_timeout(260)
        if not page.locator("[data-sku-management-modal]").is_hidden():
            raise AssertionError("closing preview_loading must invalidate the pending response instead of reopening the modal")
        page.locator('[data-sku-price-preview="101"]').click()
        _assert_modal_state(page, "preview_loading")
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'preview_ready'")
        if "Всё равно изменить" not in page.locator("[data-sku-modal-confirm]").inner_text():
            raise AssertionError("stabilization warning must expose explicit override")
        page.get_by_role("button", name="Отменить", exact=True).click()
        if not page.locator("[data-sku-management-modal]").is_hidden() or commit_payloads:
            raise AssertionError("Отменить must close preview without mutation")
        page.locator('[data-sku-price-preview="101"]').click()
        _assert_modal_state(page, "preview_loading")
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'preview_ready'")
        scroll_before = page.evaluate(
            """() => {
              const shell = document.querySelector('.sku-management-table-shell');
              shell.style.maxHeight = '76px';
              shell.scrollLeft = 420;
              shell.scrollTop = 22;
              document.querySelector('[data-sku-row-nm-id="102"]').dataset.identity = 'untouched';
              return {left: shell.scrollLeft, top: shell.scrollTop};
            }"""
        )
        headers_before_price = page.locator("[data-sku-management-head] th").evaluate_all(
            "nodes => nodes.map(node => ({id:node.dataset.skuSort,text:node.textContent}))"
        )
        reads_before_price = table_reads[0]
        history_before_price = history_reads[0]
        page.locator("[data-sku-modal-confirm]").click()
        _assert_modal_state(page, "commit_running")
        page.wait_for_timeout(160)
        _assert_modal_state(page, "readback_pending")
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'success'")
        modal_text = page.locator("[data-sku-management-modal]").inner_text()
        if "Цена изменена" not in modal_text or "950 → 850 ₽" not in modal_text or "WB readback" not in modal_text:
            raise AssertionError("price success must expose old/confirmed values only after matching readback")
        if not commit_payloads[-1].get("override_stabilization") or not commit_payloads[-1].get("override_warnings"):
            raise AssertionError("explicit UI override must be sent and audited")
        if page.locator('[data-sku-price-input="101"]').count() != 1 or page.locator('[data-sku-price-edit="101"]').count() != 0:
            raise AssertionError("target display cell must not be replaced optimistically before the operator closes success")
        page.wait_for_timeout(800)
        _assert_modal_state(page, "success")
        if page.locator("[data-sku-modal-confirm]").inner_text() != "Закрыть":
            raise AssertionError("success modal must remain open with Закрыть as its primary action")
        if not page.locator("[data-sku-modal-secondary]").is_hidden():
            raise AssertionError("success modal must not retain Отменить as a competing action")
        success_layout = page.evaluate(
            """() => {
              const card=document.querySelector('[data-sku-management-modal] .operator-modal-card');
              const body=document.querySelector('[data-sku-modal-body]');
              const cardBox=card.getBoundingClientRect();const bodyBox=body.getBoundingClientRect();
              return {cardHeight:cardBox.height,bodyHeight:bodyBox.height,cardBackground:getComputedStyle(card).backgroundColor,bodyBackground:getComputedStyle(body).backgroundColor};
            }"""
        )
        if success_layout["cardHeight"] < 200 or success_layout["bodyHeight"] < 100 or success_layout["cardBackground"] != "rgb(23, 25, 31)" or success_layout["bodyBackground"] != "rgb(23, 25, 31)":
            raise AssertionError(f"success modal layout/opacity mismatch: {success_layout}")
        page.screenshot(path="/tmp/wb-core-sku-management-price-success.png")
        page.locator("[data-sku-management-modal] .operator-modal-card").screenshot(path="/tmp/wb-core-sku-management-price-success-card.png")
        page.locator("[data-sku-modal-confirm]").click()
        page.wait_for_selector("[data-sku-management-modal]", state="hidden")
        price_row = page.locator('[data-sku-row-nm-id="101"]')
        if "850" not in price_row.locator('[data-sku-cell="seller_price"]').inner_text():
            raise AssertionError("confirmed price must patch only the target seller-price cell")
        if "701" not in price_row.locator('[data-sku-cell="buyer_price"]').inner_text():
            raise AssertionError("factual buyer-price readback must patch the buyer-price cell")
        if "2026-07-14T10:00:00Z" not in price_row.locator('[data-sku-cell="last_price_change_at"]').inner_text():
            raise AssertionError("confirmed price timestamp/stabilization state must patch locally")
        scroll_after_price = page.evaluate("() => { const shell=document.querySelector('.sku-management-table-shell'); return {left:shell.scrollLeft,top:shell.scrollTop}; }")
        headers_after_price = page.locator("[data-sku-management-head] th").evaluate_all(
            "nodes => nodes.map(node => ({id:node.dataset.skuSort,text:node.textContent}))"
        )
        if scroll_after_price != scroll_before or page.locator('[data-sku-filter="search"]').input_value() != "risk" or headers_after_price != headers_before_price:
            raise AssertionError(f"price cell patch must preserve search and table scroll: {scroll_before} -> {scroll_after_price}")
        if page.locator('[data-sku-row-nm-id="102"]').get_attribute("data-identity") != "untouched":
            raise AssertionError("price success must not rebuild non-target rows")
        if table_reads[0] != reads_before_price or history_reads[0] <= history_before_price:
            raise AssertionError("price success may refresh open history, never the whole SKU table")
        if "950 → 850" not in page.locator("[data-sku-history-body]").inner_text():
            raise AssertionError("open history must refresh after confirmed mutation")

        price_commit_mode[0] = "mismatch"
        page.locator('[data-sku-price-edit="101"]').click()
        page.locator('[data-sku-price-input="101"]').fill("840")
        page.locator('[data-sku-price-preview="101"]').click()
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'preview_ready'")
        mismatch_reads_before = table_reads[0]
        page.locator("[data-sku-modal-confirm]").click()
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'controlled_error'")
        if "Цена изменена" in page.locator("[data-sku-management-modal]").inner_text():
            raise AssertionError("readback mismatch must never render green price success")
        page.locator("[data-sku-modal-confirm]").click()
        if "850" not in page.locator('[data-sku-row-nm-id="101"] [data-sku-cell="seller_price"]').inner_text():
            raise AssertionError("readback mismatch must restore the unchanged confirmed target cell")
        if table_reads[0] != mismatch_reads_before:
            raise AssertionError("controlled readback mismatch must not refetch the table")
        price_commit_mode[0] = "success"

        page.locator('[data-sku-bid-edit="101"]').click()
        page.locator('[data-sku-bid-option="101"]').select_option("78|recommendations")
        page.locator('[data-sku-bid-input="101"]').fill("18")
        page.locator('[data-sku-bid-preview="101"]').click()
        _assert_modal_state(page, "preview_loading")
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'preview_ready'")
        if "78 / recommendations" not in page.locator("[data-sku-management-modal]").inner_text() or "Всё равно изменить" not in page.locator("[data-sku-modal-confirm]").inner_text():
            raise AssertionError("multiple campaigns require exact advert_id/placement and cross-warning override")
        if preview_payloads[-1].get("advert_id") != 78 or preview_payloads[-1].get("placement") != "recommendations":
            raise AssertionError("frontend must not collapse placement identity")
        bid_scroll_before = page.evaluate("() => { const shell=document.querySelector('.sku-management-table-shell'); shell.scrollLeft=360; shell.scrollTop=18; return {left:shell.scrollLeft,top:shell.scrollTop}; }")
        reads_before_bid = table_reads[0]
        history_before_bid = history_reads[0]
        page.locator("[data-sku-modal-confirm]").click()
        _assert_modal_state(page, "commit_running")
        page.wait_for_timeout(160)
        _assert_modal_state(page, "readback_pending")
        page.wait_for_function("() => document.querySelector('[data-sku-management-modal]').dataset.skuModalState === 'success'")
        if "Ставка изменена" not in page.locator("[data-sku-management-modal]").inner_text() or "17 → 18 ₽" not in page.locator("[data-sku-management-modal]").inner_text():
            raise AssertionError("bid success must expose confirmed matching WB readback")
        if commit_payloads[-1].get("preview_id") != "bid-preview" or not commit_payloads[-1].get("override_stabilization"):
            raise AssertionError("bid commit must use only the confirmed preview with override")
        page.wait_for_timeout(800)
        _assert_modal_state(page, "success")
        page.locator("[data-sku-modal-confirm]").click()
        page.wait_for_selector("[data-sku-management-modal]", state="hidden")
        bid_scroll_after = page.evaluate("() => { const shell=document.querySelector('.sku-management-table-shell'); return {left:shell.scrollLeft,top:shell.scrollTop}; }")
        if bid_scroll_after != bid_scroll_before or table_reads[0] != reads_before_bid or history_reads[0] <= history_before_bid:
            raise AssertionError("bid cell patch must preserve scroll and refresh only open history")
        bid_cell = page.locator('[data-sku-row-nm-id="101"] [data-sku-cell="current_bid"]')
        if "15–18 ₽" not in bid_cell.inner_text():
            raise AssertionError("only the selected advert_id/placement bid must update from confirmed readback")
        if "2026-07-14T10:05:00Z" not in page.locator('[data-sku-row-nm-id="101"] [data-sku-cell="last_bid_change_at"]').inner_text():
            raise AssertionError("confirmed bid timestamp/stabilization state must patch locally")
        if "17 → 18" not in page.locator("[data-sku-history-body]").inner_text():
            raise AssertionError("bid event must appear in persistent history after refresh")
        page.screenshot(path="/tmp/wb-core-sku-management-ui-polish.png", full_page=True)
        history.locator("summary").click()
        if history.get_attribute("open") is not None:
            raise AssertionError("history block must close")
        unexpected_console_errors = [
            item for item in console_errors
            if "status of 409 (Conflict)" not in item and "status of 422 (Unprocessable Entity)" not in item
        ]
        if unexpected_console_errors:
            raise AssertionError(f"unexpected browser console errors: {unexpected_console_errors}")
        browser.close()
    print("sku_management_browser_smoke: OK")


def _assert_first_row(page: Page, token: str, context: str) -> None:
    if token not in page.locator("[data-sku-management-body] tr").first.inner_text():
        raise AssertionError(f"{context}: expected first row {token}")


def _assert_modal_state(page: Page, expected: str) -> None:
    actual = page.locator("[data-sku-management-modal]").get_attribute("data-sku-modal-state")
    if actual != expected:
        raise AssertionError(f"expected modal state {expected}, got {actual}")


def _assert_three_state_sort(page: Page, key: str, asc: str, desc: str, none: str) -> None:
    header = page.locator(f'[data-sku-sort="{key}"]')
    header.click()
    _assert_first_row(page, asc, f"{key} asc")
    if key == "nearest_inbound" and "UNKNOWN" not in page.locator("[data-sku-management-body] tr").last.inner_text():
        raise AssertionError("empty nearest-inbound values must stay at the bottom in ascending order")
    page.locator(f'[data-sku-sort="{key}"]').click()
    _assert_first_row(page, desc, f"{key} desc")
    if key == "nearest_inbound" and "UNKNOWN" not in page.locator("[data-sku-management-body] tr").last.inner_text():
        raise AssertionError("empty nearest-inbound values must stay at the bottom in descending order")
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
        "table": {
            "visible_columns": [],
            "column_order": ["first_problem_district", "product", "risk"],
            "column_widths": {"first_problem_district": 240},
            "filters": {"risk": "unknown", "promo": "yes", "coverage_min": 100, "deficit_max": 5},
            "sort": [
                {"key": "first_problem_district", "direction": "asc"},
                {"key": "risk_rank", "direction": "desc"},
                {"key": "deficit_date", "direction": "asc"},
            ],
        },
    }


def _rows() -> list[dict[str, object]]:
    options = [{"advert_id": 77, "campaign_name": "Search", "placement": "search", "current_bid_rub": 15}, {"advert_id": 78, "campaign_name": "Recommendations", "placement": "recommendations", "current_bid_rub": 17}]
    common = {"quality": "complete", "quality_warnings": [], "buyer_price_source": "public_wb_card", "buyer_price_quality": "observed", "buyer_price_freshness": "2026-07-25", "buyer_price_updated_at": "2026-07-25T08:15:00Z", "seller_price_updated_at": "2026-07-25T08:00:00Z", "spp_proxy": 0.136, "spp_proxy_updated_at": "2026-07-25T08:15:00Z", "campaign_count": 2, "placement_count": 2, "campaigns_updated_at": "2026-07-25T08:20:00Z", "current_bid": None, "current_bid_updated_at": "2026-07-25T08:20:00Z", "ad_options": options, "ads_drr": 0.1, "ads_drr_attributed": 0.2, "ads_spend_rub": 1234, "funnel": {"view_count": 100, "openCount": 50, "cartCount": 20, "addToCartConversion": 0.4, "cartToOrderConversion": 0.5}, "orders": 10, "sales_rub": 9000, "profit_rub": 2000, "margin_pct": 0.22, "last_bid_change_at": "2026-07-01T10:00:00Z"}
    return [
        {**common, "nm_id": 101, "sku": "SKU-HIGH-INTERNAL", "name": "HIGH risk product", "risk": "high", "risk_rank": 2, "deficit_date": "2026-07-15", "coverage_pct": 20, "deficit_units": 80, "nearest_supplier_inbound": {"shipment_id": "shipment-071", "invoice_no": "INV-071", "arrival_date": "2026-08-18", "quantity": 2500, "date_source": "actual_shipment_date"}, "first_problem_district": "central", "reason": "near deficit", "seller_price": 950, "buyer_price": 777, "promo_label": "1 / 2", "promo_count": 1, "promo_participation": 1, "promo_freshness": "2026-07-25T08:30:00Z", "last_price_change_at": "2026-07-13T10:00:00Z"},
        {**common, "nm_id": 102, "sku": "SKU-LOW-INTERNAL", "name": "LOW risk product", "risk": "low", "risk_rank": 0, "deficit_date": "2026-09-01", "coverage_pct": 140, "deficit_units": 0, "nearest_supplier_inbound": {"shipment_id": "shipment-020", "invoice_no": "INV-020", "arrival_date": "2026-07-29", "quantity": 120, "date_source": "planned_shipment_date"}, "first_problem_district": None, "reason": "no deficit", "seller_price": 700, "buyer_price": 650, "promo_label": "0 / 2", "promo_count": 0, "promo_participation": 0, "promo_freshness": "2026-07-25T08:30:00Z", "last_price_change_at": "2026-07-01T10:00:00Z"},
        {**common, "nm_id": 103, "sku": "SKU-UNKNOWN-INTERNAL", "name": "UNKNOWN evidence product", "risk": "unknown", "risk_rank": -1, "deficit_date": None, "coverage_pct": None, "deficit_units": None, "nearest_supplier_inbound": None, "first_problem_district": "unknown", "reason": "regional unknown", "quality": "partial", "quality_warnings": ["regional evidence missing"], "seller_price": 800, "buyer_price": None, "buyer_price_quality": "missing", "buyer_price_freshness": "", "buyer_price_updated_at": "", "promo_label": "н/д", "promo_count": None, "promo_participation": None, "promo_freshness": "", "last_price_change_at": ""},
    ]


def _history_row(event_id: str, parameter: str, old_value: float, confirmed_value: float) -> dict[str, object]:
    return {"event_id": event_id, "nm_id": 101, "parameter": parameter, "old_value": old_value, "requested_value": confirmed_value, "confirmed_value": confirmed_value, "delta": confirmed_value - old_value, "requested_at": "2026-07-13T09:59:00Z", "confirmed_at": "2026-07-13T10:00:00Z", "actor": "operator", "source": "sku_management", "advert_id": 78 if parameter == "advertising_bid" else None, "campaign": "Recommendations" if parameter == "advertising_bid" else "", "placement": "recommendations" if parameter == "advertising_bid" else "", "preview_id": "preview", "correlation_id": "operation", "commit_status": "confirmed", "readback_status": "matching", "warnings": ["same_parameter_stabilization"], "stabilization_override": True, "warning_override": True, "error": ""}


if __name__ == "__main__":
    main()
