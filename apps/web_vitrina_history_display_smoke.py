"""Owned synthetic display fixtures; no production data or evaluator claims."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_snapshot_pilot_smoke import _build, _dense
from packages.application.web_vitrina_history_store import HistoryStore, import_finished_table
from packages.application.web_vitrina_history_http_read import read_history_page
from packages.application.web_vitrina_page_composition import WEB_VITRINA_PAGE_STATE_NAMESPACE
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_READ_PATH


def check_legacy_fbs_labels(root, table):
    """Known legacy captions only; stored rows and all dated fields are unchanged."""
    known = {"total": "total_inventory_fbs_total_qty_v1", "sku": "inventory_fbs_total_qty_v1"}
    for caption in ("raw", "empty", "custom"):
        fixture = deepcopy(table)
        columns = fixture["columns"]
        indices = {c["id"]: i for i, c in enumerate(columns)}
        rows = [next(r for r in fixture["rows"] if r["row_kind"] == kind) for kind in known]
        fixture["rows"] = rows
        date_index = next(i for i, c in enumerate(columns) if c["id"].startswith("date:"))
        fixture["columns"] = columns[:date_index + 1]
        columns = fixture["columns"]
        for position, row in enumerate(rows, start=1):
            row["values"] = [cell for cell in row["values"] if cell[0] <= date_index]
            next(cell for cell in row["values"] if cell[0] == indices["row_order"])[1:3] = [position, str(position)]
            metric = known[row["row_kind"]]
            row["row_id"] = row["row_id"].split("|", 1)[0] + "|" + metric
            for cell in row["values"]:
                if cell[0] == indices["metric_key"]:
                    cell[1:3] = [metric, metric]
                elif cell[0] == indices["metric_label"]:
                    label = metric if caption == "raw" else "" if caption == "empty" else "Сохранённая подпись FBS"
                    cell[1:3] = [label, label]
        store = HistoryStore(root / ("legacy-caption-" + caption))
        day = next(c["id"][5:] for c in columns if c["id"].startswith("date:"))
        edition = import_finished_table(store, fixture, accepted_ready={day: True})["edition_id"]
        before = {p: p.read_bytes() for p in store.root.rglob("*") if p.is_file()}
        expected = "Сохранённая подпись FBS" if caption == "custom" else "Остаток FBS: всего"
        for scope in ("summary", "sku"):
            payload = read_history_page(store, date_from=day, date_to=day, scope=scope)
            decoded = _dense(payload["table_surface"]["rows"], payload["table_surface"]["columns"])
            source = [r for r in _dense(rows, columns) if (r["row_kind"] == "sku") == (scope == "sku")]
            assert [r["row_id"] for r in decoded] == [r["row_id"] for r in source]
            for actual, original in zip(decoded, source):
                label = actual["values"]["metric_label"]
                assert label["value"] == label["display_text"] == expected, (caption, scope, label)
                for key, cell in actual["values"].items():
                    if key != "metric_label":
                        assert cell == original["values"][key], (caption, scope, key)
            options = payload["filter_surface"]["controls"][0]["options"]
            assert all(next(o for o in options if o["value"] == key)["label"] == expected for key in known.values())
            assert payload["history_snapshot"]["edition_id"] == edition
        assert all(p.read_bytes() == content for p, content in before.items())


def main():
    from playwright.sync_api import sync_playwright

    server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=1, history_snapshot=True)
    with server as base:
        original = _build(server, "2026-04-20", "2026-04-20", compact=True)
        table = deepcopy(original["table_surface"])
        old_columns = table["columns"]
        metric_index = next(i for i, c in enumerate(old_columns) if c["id"] == "metric_key")
        label_index = next(i for i, c in enumerate(old_columns) if c["id"] == "metric_label")
        sku_order = next(r for r in table["rows"] if r["row_kind"] == "sku" and
                         next(v[1] for v in r["values"] if v[0] == metric_index) == "orderCount")
        # The compact seed omits SKU orderSum; add an owned counterpart so the
        # real saved orderCount/orderSum pair IDs can both be exercised.
        sku_sum = deepcopy(sku_order)
        sku_sum["row_id"] = sku_order["row_id"].replace("|orderCount", "|orderSum")
        for cell in sku_sum["values"]:
            if cell[0] == metric_index:
                cell[1:3] = ["orderSum", "orderSum"]
            elif cell[0] == label_index:
                cell[1:3] = ["Сумма заказов", "Сумма заказов"]
        table["rows"].append(sku_sum)
        for grouping in table["groupings"]:
            if sku_order["row_id"] in grouping["row_ids"]:
                grouping["row_ids"].append(sku_sum["row_id"])
        static = [c for c in old_columns if not c["id"].startswith("date:")]
        prototype = next(c for c in old_columns if c["id"].startswith("date:"))
        dates = [(date(2026, 4, 20) - timedelta(days=i)).isoformat() for i in reversed(range(180))]
        table["columns"] = static + [{**prototype, "id": "date:" + d, "accessor_key": "date:" + d, "header": d} for d in dates]
        totals = [r for r in table["rows"] if r["row_kind"] == "total"]
        specs = [(0.28, "percent", "percent_default", "28,00%"),
                 (1234, "money", "money_rub", "1\xa0234 ₽"),
                 (1234, "money", "money_rub_per_unit", "1\xa0234 ₽/шт")]
        for row in table["rows"]:
            values = {old_columns[v[0]]["id"]: v[1:] for v in row["values"]}
            prototype_cell = deepcopy(next(v for k, v in values.items() if k.startswith("date:")))
            if row in totals[:3]:
                value, kind, formatter, _ = specs[totals.index(row)]
                prototype_cell[:5] = [value, str(value), kind, formatter, "renderer:" + kind + ":" + formatter]
            row["values"] = [[i, *values[c["id"]]] for i, c in enumerate(static)] + [
                [len(static) + i, *prototype_cell] for i in range(len(dates))]
        # This is the actual Gravity catalog shape: renderer IDs without rules.
        table.pop("formatters", None)
        for _, kind, formatter, _ in specs:
            if not any(r["formatter_id"] == formatter for r in table["renderers"]):
                table["renderers"].append({"renderer_id": "renderer:" + kind + ":" + formatter,
                                          "formatter_id": formatter, "gravity_variant": "text", "align": "end"})
        check_legacy_fbs_labels(Path(server.history_snapshot_store).parent, table)
        store = HistoryStore(server.history_snapshot_store)
        edition = import_finished_table(store, table, accepted_ready={d: True for d in dates})["edition_id"]
        saved = datetime(2026, 4, 19, 17, 20, 51, tzinfo=timezone.utc)
        edition_file = store.root / "editions" / (edition + ".json")
        os.utime(edition_file, (saved.timestamp(), saved.timestamp()))
        payload = read_history_page(store, date_from=dates[-3], date_to=dates[-1])
        assert payload["meta"]["state_namespace"] == WEB_VITRINA_PAGE_STATE_NAMESPACE
        assert payload["historical_access"]["options"][0]["value"] == dates[-1]
        assert payload["historical_access"]["available_date_min"] == dates[0]
        assert {r["formatter_id"] for r in payload["table_surface"]["formatters"]} >= {s[2] for s in specs}
        reference = {r["row_id"]: r for r in _dense(table["rows"], table["columns"])}
        for row in _dense(payload["table_surface"]["rows"], payload["table_surface"]["columns"]):
            assert row["values"]["date:" + dates[-1]] == reference[row["row_id"]]["values"]["date:" + dates[-1]]
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(timezone_id="Asia/Tbilisi")
            page.add_init_script("localStorage.setItem(" + json.dumps(WEB_VITRINA_PAGE_STATE_NAMESPACE +
                ":section-column-visibility:v1") + ", JSON.stringify({section_visible:false}));")
            page.clock.install(time=datetime(2026, 4, 20, 12, tzinfo=timezone.utc))
            requests, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: requests.append(request.url)
                    if urlsplit(request.url).path == DEFAULT_SHEET_WEB_VITRINA_READ_PATH else None)
            page.goto(base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-18&date_to=2026-04-20")
            try:
                page.wait_for_selector("[data-history-summary-load-ms]", timeout=30000)
            except Exception as error:
                raise AssertionError({"errors": errors, "requests": requests,
                                      "body": page.locator("body").inner_text()[-6000:]}) from error
            for row, (_, _, _, expected) in zip(totals, specs):
                cell = page.locator('[data-table-body] td[data-row-id="' + row["row_id"] + '"][data-col-id="date:2026-04-20"]')
                assert cell.inner_text() == expected, (row["row_id"], cell.inner_text(), expected)
            assert page.locator("[data-table-summary-updated-at]").get_attribute("data-table-summary-updated-at") == saved.isoformat()
            assert "21:20" in page.locator("[data-table-summary-line]").inner_text()
            def assert_badge_visible():
                geometry = page.locator("[data-table-summary-updated]").evaluate("""node => {
                    const r=node.getBoundingClientRect(), range=document.createRange();
                    range.selectNodeContents(node);
                    const text=range.getBoundingClientRect(), clips=[];
                    for (let parent=node.parentElement; parent; parent=parent.parentElement) {
                        const style=getComputedStyle(parent), box=parent.getBoundingClientRect();
                        if (['hidden','clip','auto','scroll'].includes(style.overflowX)) {
                            clips.push({className:parent.className,left:box.left,right:box.right});
                        }
                    }
                    return {left:r.left,right:r.right,width:r.width,scrollWidth:node.scrollWidth,
                        clientWidth:node.clientWidth,textLeft:text.left,textRight:text.right,
                        viewport:window.innerWidth,clips};
                }""")
                assert geometry["right"] <= geometry["viewport"], geometry
                assert geometry["clientWidth"] >= geometry["scrollWidth"], geometry
                assert geometry["textLeft"] >= geometry["left"] and geometry["textRight"] <= geometry["right"], geometry
                assert all(geometry["textLeft"] >= clip["left"] and geometry["textRight"] <= clip["right"]
                           for clip in geometry["clips"]), geometry

            for width in (1280, 1600, 1440, 960):
                page.set_viewport_size({"width": width, "height": 720})
                for font in ("", "Arial, sans-serif", "monospace"):
                    page.locator(".table-heading-row").evaluate("(node,font) => node.style.fontFamily=font", font)
                    assert_badge_visible()
            page.set_viewport_size({"width": 1280, "height": 720})
            page.locator(".table-heading-row").evaluate("node => node.style.fontFamily=''")
            assert page.locator('[data-table-head] th[data-col-id="section"]').count() == 0
            # Seed an existing version5 config from the native UI's own schema.
            # It must apply on the first summary paint, before lazy SKU cells.
            page.locator("[data-metrics-settings-open]").click()
            records = page.locator("[data-metric-config-row]").evaluate_all(
                "rows => rows.map(r => ({id:r.dataset.logicalMetricId, total:r.dataset.totalMetricKey,"
                "sku:r.dataset.skuMetricKey}))")
            common = [r for r in records if r["total"] and r["sku"]]
            assert len(common) >= 3, records
            anchor, collapsed, hidden = common[-3:]
            config = page.evaluate("key => JSON.parse(localStorage.getItem(key))",
                WEB_VITRINA_PAGE_STATE_NAMESPACE + ":metric-presentation:v1")
            config["presentation"]["order"] = [anchor["id"], collapsed["id"], hidden["id"]] + [
                r["id"] for r in records if r not in (anchor, collapsed, hidden)]
            config["presentation"]["display"].update({anchor["id"]: "shown", collapsed["id"]: "collapsed",
                                                      hidden["id"]: "hidden"})
            config["presentation"]["manual"] = True
            config["expanded_anchors"] = []
            config_writes = []

            def config_route(route):
                if route.request.method != "GET":
                    config_writes.append(route.request.post_data_json)
                route.fulfill(json={"status": "ok", "revision": 2, "config": config})

            page.route("**/web-vitrina/user-config", config_route)
            page.reload()
            page.wait_for_selector("[data-history-summary-load-ms]", timeout=30000)

            def assert_preferences():
                keys = page.locator('[data-table-body] tr[data-row-kind="total"]').evaluate_all(
                    "rows => rows.map(r => r.querySelector('[data-metric-key]').dataset.metricKey)")
                assert keys[0] == anchor["total"], keys
                assert collapsed["total"] not in keys and hidden["total"] not in keys
                assert page.locator('[data-table-head] th[data-col-id="section"]').count() == 0
                assert page.locator('[data-metric-anchor-toggle][data-metric-anchor-key="' + anchor["total"] + '"]').count() == 1

            assert_preferences()
            page.get_by_role("button", name="Выбрать диапазон", exact=True).click()
            assert "апрель" in page.locator("[data-history-month-label]").inner_text().lower()
            assert page.get_by_role("button", name="Предыдущий месяц", exact=True).is_enabled()
            page.get_by_role("button", name="Предыдущий месяц", exact=True).click()
            assert "март" in page.locator("[data-history-month-label]").inner_text().lower()
            assert page.get_by_role("button", name="Следующий месяц", exact=True).is_enabled()
            def assert_calendar_geometry():
                geometry = page.locator("[data-history-popover]").evaluate("""popup => {
                    const bounds=popup.getBoundingClientRect();
                    const nodes=[...popup.querySelectorAll('button,input')];
                    return {viewport:{width:innerWidth,height:innerHeight},
                        bounds:{left:bounds.left,right:bounds.right,top:bounds.top,bottom:bounds.bottom},
                        overflow:popup.scrollWidth > popup.clientWidth,
                        controls:nodes.map(node => {
                            const r=node.getBoundingClientRect();
                            const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
                            const range=document.createRange(); range.selectNodeContents(node);
                            const text=range.getBoundingClientRect();
                            return {label:node.textContent || node.getAttribute('data-history-date-from') || node.type,
                                left:r.left,right:r.right,top:r.top,bottom:r.bottom,
                                hitSelf:hit===node || node.contains(hit), hitTag:hit && hit.outerHTML.slice(0,240),
                                textFits:!node.textContent || (text.left>=r.left && text.right<=r.right),
                                overflow:node.scrollWidth > node.clientWidth};
                        })};
                }""")
                box, viewport = geometry["bounds"], geometry["viewport"]
                assert 0 <= box["left"] < box["right"] <= viewport["width"], geometry
                assert 0 <= box["top"] < box["bottom"] <= viewport["height"], geometry
                assert not geometry["overflow"], geometry
                for control in geometry["controls"]:
                    assert box["left"] <= control["left"] < control["right"] <= box["right"], geometry
                    assert box["top"] <= control["top"] < control["bottom"] <= box["bottom"], geometry
                    assert control["hitSelf"] and control["textFits"] and not control["overflow"], geometry
                return geometry

            page.get_by_role("button", name="Следующий месяц", exact=True).click()
            geometry_widths = [390, 624, 960, 1280, 1600]
            for width in geometry_widths:
                page.set_viewport_size({"width": width, "height": 900})
                page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                if page.locator("[data-history-popover]").is_hidden():
                    page.get_by_role("button", name="Выбрать диапазон", exact=True).click()
                assert_calendar_geometry()
                screenshot_dir = os.environ.get("WBC_HISTORY_DISPLAY_SCREENSHOT_DIR")
                if screenshot_dir:
                    page.screenshot(path=str(Path(screenshot_dir) / ("calendar-" + str(width) + ".png")))
                page.get_by_role("button", name="Предыдущий месяц", exact=True).click()
                assert "март" in page.locator("[data-history-month-label]").inner_text().lower()
                page.get_by_role("button", name="Следующий месяц", exact=True).click()
                assert "апрель" in page.locator("[data-history-month-label]").inner_text().lower()
                for count in (31, 1, 3, 14, 180):
                    if page.locator("[data-history-popover]").is_hidden():
                        page.get_by_role("button", name="Выбрать диапазон", exact=True).click()
                    assert_calendar_geometry()
                    label = str(count) + (" день" if count in (1, 31) else " дня" if count == 3 else " дней")
                    page.get_by_role("button", name=label, exact=True).click()
                    page.wait_for_function("count => document.querySelectorAll('[data-table-head] th[data-col-id^=\"date:\"]').length === count", arg=count)
                    query = parse_qs(urlsplit(requests[-1]).query)
                    assert query["date_to"] == ["2026-04-20"], (width, count, query)
                    assert query["date_from"] == [(date(2026, 4, 20) - timedelta(days=count - 1)).isoformat()], (width, count, query)
                    assert_preferences()
                page.get_by_role("button", name="Выбрать диапазон", exact=True).click()
                page.locator("[data-history-date-from]").fill("2026-04-18")
                page.locator("[data-history-date-to]").fill("2026-04-19")
                assert_calendar_geometry()
                page.get_by_role("button", name="Сохранить", exact=True).click()
                page.wait_for_function("document.querySelectorAll('[data-table-head] th[data-col-id^=\"date:\"]').length === 2")
                query = parse_qs(urlsplit(requests[-1]).query)
                assert query["date_from"] == ["2026-04-18"] and query["date_to"] == ["2026-04-19"], (width, query)
                assert_preferences()
            for width in (390, 624):
                page.set_viewport_size({"width": width, "height": 600})
                page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                page.get_by_role("button", name="Выбрать диапазон", exact=True).click()
                assert_calendar_geometry()
                page.get_by_role("button", name="Выбрать диапазон", exact=True).click()
            page.set_viewport_size({"width": 1280, "height": 720})
            page.locator("[data-filters-toggle]").click()
            page.locator('[data-block-kind="skus"]').first.check()
            page.locator("[data-filters-apply]").click()
            page.wait_for_selector('[data-table-body] tr[data-row-kind="sku"]')
            assert_preferences()
            assert parse_qs(urlsplit(requests[-1]).query)["edition_id"] == [edition]
            assert not config_writes, config_writes
            # Optional private compatibility evidence; account data stays outside Git.
            baseline_path = os.environ.get("WBC_HISTORY_DISPLAY_CONFIG_BASELINE")
            if baseline_path:
                config = json.loads(Path(baseline_path).read_text())["config"]
                page.goto(base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-18&date_to=2026-04-20")
                page.wait_for_selector("[data-history-summary-load-ms]", timeout=30000)
                keys = page.locator('[data-table-body] tr[data-row-kind="total"]').evaluate_all(
                    "rows => rows.map(r => r.querySelector('[data-metric-key]').dataset.metricKey)")
                assert keys[:2] == ["total_orderCount", "total_orderSum"], keys
                for key in ("fin_storage_fee_total", "total_proxy_profit_3_rub", "proxy_margin_3_pct_total"):
                    assert key not in keys, key
                assert not config_writes, config_writes
            # Exercise the native reader envelope's honest absent-current state.
            empty = read_history_page(store, date_from=dates[-1], date_to=dates[-1])
            empty["meta"]["today_current_date"] = "2026-04-21"
            empty["history_snapshot"].update(date_from="2026-04-21", date_to="2026-04-21",
                current_preliminary=True, availability={"2026-04-21": False},
                total_rows=0, next_offset=None, scope_totals={"summary": 0, "total": 0, "group": 0, "sku": 0}, reporting_groups=[])
            empty["table_surface"].update(rows=[], groupings=[], total_row_count=0, returned_row_count=0)

            def empty_route(route):
                query = parse_qs(urlsplit(route.request.url).query)
                if query.get("date_from") == ["2026-04-21"]:
                    response = deepcopy(empty)
                    response["history_snapshot"].update(scope=query.get("scope", ["catalog"])[0],
                        group_id=query.get("group_id", [""])[0], offset=int(query.get("offset", ["0"])[0]))
                    route.fulfill(json=response)
                else:
                    route.continue_()

            page.route("**/web-vitrina?**", empty_route)
            page.goto(base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-21&date_to=2026-04-21")
            page.wait_for_selector("[data-history-summary-load-ms]", state="attached", timeout=30000)
            assert "За текущий день ещё нет сохранённых данных" in page.locator("body").inner_text()
            assert "Фильтры не вернули строки" not in page.locator("body").inner_text()
            assert not errors, errors
            browser.close()
        print(json.dumps({"status": "pass", "synthetic_display_only": True, "exact16fields": True,
                          "percent_money_unit": True, "saved_at": True, "calendar": True,
                          "preset_days": [31, 1, 3, 14, 180], "calendar_geometry_widths": geometry_widths,
                          "calendar_save_each_width": True, "legacy_fbs_label_only": True,
                          "private_config_baseline": bool(baseline_path)}), flush=True)
        if os.environ.get("WBC_HISTORY_DISPLAY_PREVIEW") == "1":
            server.entrypoint.handle_sheet_web_vitrina_user_config_request = lambda **kwargs: {
                "status": "ok", "revision": 2, "config": config}
            print("PREVIEW " + base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2026-04-18&date_to=2026-04-20", flush=True)
            threading.Event().wait()


if __name__ == "__main__":
    main()
