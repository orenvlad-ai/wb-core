"""Local fixture integration only; no production/full-history performance claims."""
from copy import deepcopy
from datetime import date, timedelta
import json
import os
import re
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from apps.web_vitrina_snapshot_pilot_smoke import _build, _dense
from packages.application.web_vitrina_history_store import HistoryStore, import_finished_table
from packages.application.web_vitrina_history_http_read import read_history_page
from packages.adapters.registry_upload_http_entrypoint import DEFAULT_SHEET_WEB_VITRINA_READ_PATH


def _read(base, **query):
    url = base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + urlencode({
        "surface": "page_composition", "history_snapshot": "1", **query})
    try:
        with urlopen(url, timeout=20) as response:
            assert response.headers["Cache-Control"] == "private, no-store"
            return response.status, json.load(response)
    except HTTPError as error:
        return error.code, json.load(error)


def _must_not_evaluate(*args, **kwargs):
    raise AssertionError("derived GET called a native evaluator")


def _browser(base, edition, expected_summary):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        # Force real UI summary pagination without changing production defaults.
        page.add_init_script("""const nativeFetch = window.fetch;
          window.fetch = function(input, init) {
            const url = new URL(String(input), window.location.origin);
            if (url.searchParams.get('history_snapshot') === '1' && url.searchParams.get('scope') === 'total')
              url.searchParams.set('limit', '2');
            if (url.searchParams.get('history_snapshot') === '1' && url.searchParams.get('scope') === 'sku')
              url.searchParams.set('limit', '2');
            return nativeFetch.call(this, url.toString(), init);
          };""")
        requests, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda req: requests.append(req.url)
                if urlsplit(req.url).path == DEFAULT_SHEET_WEB_VITRINA_READ_PATH else None)
        # The 180-day table below is deliberately synthetic transport coverage.
        start = (date(2026, 4, 20) - timedelta(days=179)).isoformat()
        page.goto(base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from="
                  + start + "&date_to=2026-04-20", wait_until="domcontentloaded")
        try:
            page.wait_for_selector("[data-history-summary-load-ms]", timeout=30000)
        except Exception as error:
            raise AssertionError({"initial_history_load": str(error), "page_errors": errors,
                                  "requests": requests, "body": page.locator("body").inner_text()[:4000]}) from error
        assert page.locator('[data-table-head] th[data-col-id^="date:"]').count() == 180, {
            "headers": page.locator("[data-table-head]").inner_text(), "errors": errors,
            "summary": page.locator("[data-filter-summary]").inner_text(), "requests": requests}
        assert page.locator("[data-table-body] tr").count() == expected_summary
        assert page.locator("[data-load-refresh-button]").is_hidden()
        page.wait_for_selector("[data-history-summary-load-ms]", timeout=30000)
        timing = page.locator("[data-history-summary-load-ms]")
        assert timing.get_attribute("data-history-summary-edition") == edition
        assert int(timing.get_attribute("data-history-summary-pages")) > 1
        assert int(timing.get_attribute("data-history-summary-rows")) == expected_summary
        assert float(timing.get_attribute("data-history-summary-load-ms")) > 0
        assert page.evaluate("performance.getEntriesByName('wb-history-summary-complete').length") == 1
        assert len(requests) > 1
        assert parse_qs(urlsplit(requests[0]).query).get("scope") == ["catalog"]
        assert all(parse_qs(urlsplit(url).query).get("scope") == ["total"] for url in requests[1:])
        assert all(parse_qs(urlsplit(url).query).get("edition_id") == [edition] for url in requests[1:])
        def total_order():
            return page.locator('[data-table-body] tr[data-row-kind="total"]').evaluate_all(
                "rows => rows.map(row => row.querySelector('[data-metric-key]').getAttribute('data-metric-key'))")

        original_order = total_order()
        assert len(original_order) > 3 and len(set(original_order)) == len(original_order)
        # Exercise the existing user controls before any SKU page. Both these
        # settings must survive TOTAL-only <-> TOTAL/SKU logical-ID changes.
        page.locator("[data-metrics-settings-open]").click()
        hidden_key, moved_key, first_key = original_order[1], original_order[-1], original_order[0]
        page.locator('[data-metric-config-row][data-total-metric-key="' + hidden_key + '"] [data-metric-display-select]').select_option("hidden")
        page.evaluate("""({moved, first}) => {
          const rows = [...document.querySelectorAll('[data-metric-config-row]')];
          const source = rows.find(row => row.dataset.totalMetricKey === moved);
          const target = rows.find(row => row.dataset.totalMetricKey === first);
          const transfer = new DataTransfer();
          source.querySelector('[data-metric-drag-handle]').dispatchEvent(new DragEvent('dragstart',
            {bubbles: true, dataTransfer: transfer}));
          target.dispatchEvent(new DragEvent('drop', {bubbles: true, cancelable: true,
            dataTransfer: transfer, clientY: target.getBoundingClientRect().top}));
        }""", {"moved": moved_key, "first": first_key})
        page.locator("[data-metrics-settings-close]").last.click()
        expected_order = [moved_key] + [key for key in original_order if key not in {moved_key, hidden_key}]
        assert total_order() == expected_order, "user hide/reorder controls did not change TOTAL as intended"

        def assert_total_preserved(stage):
            assert total_order() == expected_order, {"stage": stage, "expected": expected_order,
                                                   "actual": total_order(), "hidden": hidden_key}

        evidence = os.environ.get("WBC_HISTORY_UI_EVIDENCE_DIR")
        if evidence:
            page.screenshot(path=str(Path(evidence) / "history-summary-fixture-180.png"))
        page.locator("[data-filters-toggle]").click()
        choice = page.locator('[data-block-kind="skus"]').first
        group_value = choice.get_attribute("data-block-group")
        choice.check()
        page.locator("[data-filters-apply]").click()
        page.wait_for_selector('[data-table-body] tr[data-row-kind="sku"]', timeout=30000)
        assert_total_preserved("first SKU page")
        assert parse_qs(urlsplit(requests[-1]).query)["edition_id"] == [edition]
        page.locator("[data-filters-toggle]").click()
        complete = page.locator(".block-sku-page").first
        label = complete.locator("span").inner_text()
        assert "загружено" in label and "SKU" in label, label
        assert complete.locator("button").count() == 0
        block = page.evaluate("id => historySnapshotState.blocks.get(historyBlockKey('sku',id))", group_value)
        assert block["total"] > 2 and len(block["rows"]) == block["total"]
        assert block["requests"] > 1
        query = parse_qs(urlsplit(requests[-1]).query)
        assert int(query["offset"][0]) >= block["total"] - 2 and query["edition_id"] == [edition]
        assert query.get("group_id", [""]) == [group_value]
        scopes = {row["row_id"].split("|")[0] for row in block["rows"]}
        visible_scopes = set(page.locator('[data-table-body] tr[data-row-kind="sku"]').evaluate_all(
            "rows => rows.map(row => row.getAttribute('data-row-scope-key'))"))
        assert scopes == visible_scopes, {"expected": scopes, "visible": visible_scopes}
        assert_total_preserved("complete SKU group")
        sku_requests = sum(parse_qs(urlsplit(url).query).get("scope") == ["sku"] for url in requests)
        page.locator('[data-block-kind="skus"][data-block-group="' + group_value + '"]').uncheck()
        page.locator("[data-filters-apply]").click()
        page.wait_for_function("historySnapshotState.busy === false")
        assert page.locator('[data-table-body] tr[data-row-kind="sku"]').count() == 0
        page.locator("[data-filters-toggle]").click()
        page.locator('[data-block-kind="skus"][data-block-group="' + group_value + '"]').check()
        page.locator("[data-filters-apply]").click()
        page.wait_for_selector('[data-table-body] tr[data-row-kind="sku"]')
        assert sum(parse_qs(urlsplit(url).query).get("scope") == ["sku"] for url in requests) == sku_requests
        assert_total_preserved("cached SKU toggle")
        page.locator("[data-filters-toggle]").click()
        page.locator("[data-filters-close]").click()
        assert not errors, errors
        if evidence:
            page.screenshot(path=str(Path(evidence) / "history-sku-fixture-180.png"))
        assert all("history_snapshot=1" in url for url in requests), requests
        # Unavailable ranges terminate, without native/window/finished fallback.
        before = len(requests)
        page.goto(base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=2025-01-01&date_to=2025-01-03")
        page.wait_for_timeout(500)
        assert len(requests) == before + 1, requests[before:]
        assert all("history_snapshot=1" in url for url in requests), requests
        before = len(requests)
        page.goto(base + "/sheet-vitrina-v1/vitrina?history_mode=explicit&date_from=invalid")
        page.wait_for_timeout(300)
        # Existing route normalization may canonicalize malformed UI URLs;
        # even then only the derived reader is allowed after activation.
        assert all("history_snapshot=1" in url for url in requests[before:]), requests[before:]
        assert not errors, errors
        browser.close()


def main(browser_only=False):
    server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=31, history_snapshot=True)
    with server as base:
        original = _build(server, "2026-03-21", "2026-04-20", compact=True)
        table = deepcopy(original["table_surface"])
        static = [c for c in table["columns"] if not c["id"].startswith("date:")]
        prototype = next(c for c in table["columns"] if c["id"].startswith("date:"))
        dates = [(date(2026, 4, 20) - timedelta(days=i)).isoformat() for i in reversed(range(180))]
        old_columns = table["columns"]
        table["columns"] = static + [{**prototype, "id": "date:" + d, "accessor_key": "date:" + d, "header": d} for d in dates]
        for row in table["rows"]:
            values = {old_columns[v[0]]["id"]: v[1:] for v in row["values"]}
            cells = [values[c["id"]] for c in old_columns if c["id"].startswith("date:")]
            row["values"] = [[i, *values[c["id"]]] for i, c in enumerate(static)] + [
                [len(static)+i, *cells[i % len(cells)]] for i in range(len(dates))]
        store = HistoryStore(server.history_snapshot_store)
        result = import_finished_table(store, table, accepted_ready={d: True for d in dates})
        edition = result["edition_id"]
        before = {str(p.relative_to(store.root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()}
        server.entrypoint.handle_sheet_web_vitrina_page_composition_request = _must_not_evaluate
        server.entrypoint.handle_sheet_web_vitrina_shell_metadata_request = _must_not_evaluate
        if browser_only:
            summary = read_history_page(store, date_from=dates[0], date_to=dates[-1])
            _browser(base, edition, summary["history_snapshot"]["total_rows"])
            print(json.dumps({"status": "pass", "fixture_only": True,
                "synthetic_180_transport_only": True, "complete_sku_tail_and_cache": True,
                "transport_sku_page_cap_override": 2, "same_edition_full_range": True,
                "total_order_and_user_hidden_preserved": True, "cached_group_human_label": True,
                "no_heavy_fallback": True}))
            return
        for count in (1, 3, 14, 31, 180):
            start = dates[-count]
            combined = []
            offset = 0
            while True:
                status, payload = _read(base, date_from=start, date_to=dates[-1], scope="summary", limit=2,
                    offset=offset, edition_id=edition)
                assert status == 200, payload
                assert payload["history_snapshot"]["edition_id"] == edition
                assert len(payload["table_surface"]["columns"]) == len(static) + count
                combined += _dense(payload["table_surface"]["rows"], payload["table_surface"]["columns"])
                next_offset = payload["history_snapshot"]["next_offset"]
                if next_offset is None:
                    break
                assert next_offset > offset
                offset = next_offset
            expected = [r["row_id"] for r in table["rows"] if r["row_kind"] in {"total", "group"}]
            assert [r["row_id"] for r in combined] == expected
        # Verify every dated16field, no serializer coercion, including copied fixture quality/provenance.
        reference = {r["row_id"]: r for r in _dense(table["rows"], table["columns"])}
        for scope in ("summary", "sku"):
            offset = 0
            while True:
                payload = read_history_page(store, date_from=dates[0], date_to=dates[-1], scope=scope,
                    edition_id=edition, limit=16, offset=offset)
                for row in _dense(payload["table_surface"]["rows"], payload["table_surface"]["columns"]):
                    for d in dates:
                        assert row["values"]["date:"+d] == reference[row["row_id"]]["values"]["date:"+d]
                    for c in static:
                        assert row["values"][c["id"]] == reference[row["row_id"]]["values"][c["id"]]
                offset = payload["history_snapshot"]["next_offset"]
                if offset is None:
                    break
        for params, expected_error in (
            ({"date_from": "2025-01-01", "date_to": "2025-01-03"}, "history_date_unavailable"),
            ({"date_from": dates[-1], "date_to": dates[-1], "edition_id": "0"*64}, "snapshot_expired"),
        ):
            status, payload = _read(base, **params)
            assert status == 409 and payload["error"] == expected_error, payload
        status, payload = _read(base, date_from=dates[0], date_to=dates[-1], scope="sku")
        assert status == 422
        try:
            urlopen(base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?surface=page_composition", timeout=10)
        except HTTPError as error:
            assert error.code == 409 and json.load(error)["error"] == "history_snapshot_required"
        else:
            raise AssertionError("configured history allowed native fallback")
        after = {str(p.relative_to(store.root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()}
        assert before == after
        summary = read_history_page(store, date_from=dates[0], date_to=dates[-1])
        _browser(base, edition, summary["history_snapshot"]["total_rows"])
        after_browser = {str(p.relative_to(store.root)): (p.stat().st_size, p.stat().st_mtime_ns) for p in store.root.rglob("*") if p.is_file()}
        assert before == after_browser
        print(json.dumps({"status": "pass", "fixture_only": True, "synthetic_180_transport_only": True,
            "ranges": [1,3,14,31,180], "exact16fields": True, "readonly": True, "no_heavy_fallback": True,
            "pinned_lazy_sku_browser": True}))


if __name__ == "__main__":
    main(browser_only="--browser-only" in sys.argv)
