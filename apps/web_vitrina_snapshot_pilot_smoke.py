"""Local-only finished snapshot pilot fixture and focused contract smoke.

Run ``python3 apps/web_vitrina_snapshot_pilot_smoke.py --serve`` and open one
of the printed URLs. This does not read a production database.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import gzip
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)
from packages.application.web_vitrina_snapshot_pilot import (
    MAX_ARTIFACT_BYTES,
    SnapshotPilotError,
    _partition,
    provision_store,
    publish_finished,
    read_finished,
)
from packages.application.web_vitrina_compact_table import CELL_DEFAULTS, CELL_FIELDS


PERIODS = (("2026-04-07", "2026-04-20"), ("2026-03-21", "2026-04-20"))


def _build(server: LocalWebVitrinaFixtureServer, start: str, end: str,
           *, compact: bool = False) -> dict:
    return server.entrypoint.handle_sheet_web_vitrina_page_composition_request(
        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
        operator_route="/sheet-vitrina-v1",
        date_from=start, date_to=end, include_table_data=True,
        table_format="indexed_cells_v2" if compact else "legacy",
    )


def _dense(rows: list[dict], columns: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        values = {}
        for entry in row["values"]:
            fields = list(entry[1:]) + list(CELL_DEFAULTS[len(entry) - 1:])
            values[columns[entry[0]]["id"]] = dict(zip(CELL_FIELDS, fields))
        result.append({**row, "values": values})
    return result


def _url(base: str, start: str, end: str, *, pilot: bool) -> str:
    query = {"history_mode": "explicit", "date_from": start, "date_to": end}
    if pilot:
        query["snapshot_pilot"] = "1"
    return base + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode(query)


def _read_http(url: str) -> dict:
    with urlopen(url, timeout=30) as response:
        return json.load(response)


def _check_pilot_transport(url: str, expected: dict) -> None:
    for accepted, compressed in (
        ("gzip", True), ("gzip;q=0", False),
        ("*;q=1,gzip;q=0", False), ("*;q=0.5", True),
        ("identity", False),
    ):
        with urlopen(Request(url, headers={"Accept-Encoding": accepted}), timeout=30) as response:
            wire = response.read()
            assert response.headers.get("Content-Encoding") == ("gzip" if compressed else None)
            assert response.headers.get("Vary") == "Accept-Encoding"
            assert response.headers.get("Cache-Control") == "private, no-store"
            assert int(response.headers["Content-Length"]) == len(wire)
            decoded = gzip.decompress(wire) if compressed else wire
            assert json.loads(decoded) == expected


def _prepare(server: LocalWebVitrinaFixtureServer) -> dict[tuple[str, str], dict]:
    provision_store(server.snapshot_pilot_store)
    full = {}
    for start, end in PERIODS:
        composition = _build(server, start, end)
        full[(start, end)] = composition
        (server.runtime_dir / f"full_projection_{start}_{end}.json").write_text(
            json.dumps(composition, ensure_ascii=False), encoding="utf-8"
        )
        publish_finished(server.snapshot_pilot_store, composition, date_from=start, date_to=end)
    return full


def _browser_flow(base: str) -> None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        requests: list[str] = []
        errors: list[str] = []
        page.on("request", lambda request: requests.append(request.url)
                if "/v1/sheet-vitrina-v1/web-vitrina" in request.url else None)
        page.on("pageerror", lambda error: errors.append(str(error)))
        for start, end in PERIODS:
            page.goto(_url(base, start, end, pilot=True), wait_until="domcontentloaded")
            page.wait_for_selector("[data-snapshot-pilot-toolbar]:visible", timeout=20000)
            assert "снимок сохранён:" in page.locator("[data-table-summary-line]").inner_text().lower()
            assert page.locator("[data-table-summary-updated-at]").get_attribute(
                "data-table-summary-updated-at"
            )
            assert page.locator('[data-table-head] th[data-col-id^="date:"]').count() == (
                14 if start == PERIODS[0][0] else 31
            )
            assert page.locator("[data-table-body] tr").count() == 14
            assert page.locator("[data-load-refresh-button]").is_hidden()
            page.locator("[data-snapshot-pilot-expand]").click()
            page.wait_for_function(
                "document.querySelector('[data-snapshot-pilot-toolbar]').hidden",
                timeout=20000,
            )
            assert page.locator("[data-table-body] tr").count() > 14
        assert not errors, errors
        table_requests = [url for url in requests if "/v1/sheet-vitrina-v1/web-vitrina" in url]
        assert len(table_requests) == 4, table_requests
        assert all("snapshot_pilot=1" in url for url in table_requests), table_requests
        start, end = PERIODS[0]
        page.goto(_url(base, start, end, pilot=False), wait_until="domcontentloaded")
        page.wait_for_selector("[data-table-body] tr", timeout=30000)
        assert any("snapshot_pilot=1" not in url for url in requests[4:]), requests
        browser.close()


def test_contract() -> None:
    server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=31,
                                          snapshot_pilot=True)
    with server as base:
        full = _prepare(server)
        for start, end in PERIODS:
            original = full[(start, end)]
            summary = read_finished(server.snapshot_pilot_store, date_from=start,
                                    date_to=end, part="summary")
            generation = summary["snapshot_pilot"]["generation_id"]
            sku = read_finished(server.snapshot_pilot_store, date_from=start,
                                date_to=end, part="sku", generation_id=generation)
            original_rows = original["table_surface"]["rows"]
            assert summary["table_surface"]["columns"] == original["table_surface"]["columns"]
            assert summary["meta"]["visible_date_columns"] == original["meta"]["visible_date_columns"]
            assert all(row["row_kind"].lower() != "sku" for row in summary["table_surface"]["rows"])
            assert all(row["row_kind"].lower() == "sku" for row in sku["rows"])
            assert _dense(summary["table_surface"]["rows"], summary["table_surface"]["columns"]) == [
                row for row in original_rows if row["row_kind"].lower() != "sku"
            ]
            assert _dense(sku["rows"], summary["table_surface"]["columns"]) == [
                row for row in original_rows if row["row_kind"].lower() == "sku"
            ]
            assert sku["catalog_order"] == [row["row_id"] for row in original_rows]
            assert "SKU:" not in json.dumps(summary, ensure_ascii=False)
            query = urlencode({"surface": "page_composition", "snapshot_pilot": "1",
                               "part": "summary", "date_from": start, "date_to": end})
            http_summary = _read_http(base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + query)
            assert http_summary["snapshot_pilot"]["generation_id"] == generation
            assert http_summary["snapshot_pilot"]["assembled_at"] == summary["snapshot_pilot"]["assembled_at"]
            assert http_summary["table_surface"]["rows"] == summary["table_surface"]["rows"]
            _check_pilot_transport(base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + query,
                                   summary)
            query_sku = urlencode({"surface": "page_composition", "snapshot_pilot": "1",
                                   "part": "sku", "date_from": start, "date_to": end,
                                   "generation_id": generation})
            assert _read_http(base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + query_sku)["rows"] == sku["rows"]
            _check_pilot_transport(base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + query_sku,
                                   sku)

        # The existing compact full response is accepted directly, without a
        # second dense matrix; its 16-field cells match the legacy evaluator.
        compact = _build(server, *PERIODS[0], compact=True)
        compact_summary, compact_sku, _, _ = _partition(
            compact, date_from=PERIODS[0][0], date_to=PERIODS[0][1]
        )
        assert _dense(json.loads(compact_summary)["table_surface"]["rows"],
                      compact["table_surface"]["columns"]) == [
            row for row in full[PERIODS[0]]["table_surface"]["rows"]
            if row["row_kind"].lower() != "sku"
        ]
        assert _dense(json.loads(compact_sku)["rows"], compact["table_surface"]["columns"]) == [
            row for row in full[PERIODS[0]]["table_surface"]["rows"]
            if row["row_kind"].lower() == "sku"
        ]
        for mutate in (
            lambda c: c["table_surface"].__setitem__("returned_row_count", 1),
            lambda c: c["table_surface"].__setitem__("total_row_count", 1),
            lambda c: [row.__setitem__("row_kind", "sku") for row in c["table_surface"]["rows"]
                       if row["row_kind"].lower() == "total"],
            lambda c: c["table_surface"]["groupings"][0]["row_ids"].pop(),
            lambda c: c["table_surface"]["groupings"][0]["row_ids"].append("SKU:nonexistent|metric"),
        ):
            malformed = deepcopy(full[PERIODS[0]])
            mutate(malformed)
            try:
                _partition(malformed, date_from=PERIODS[0][0], date_to=PERIODS[0][1])
                raise AssertionError("truncated catalog accepted")
            except SnapshotPilotError:
                pass
        malformed_compact = deepcopy(compact)
        malformed_compact["table_surface"]["rows"][0]["values"][0][0] = 9999
        try:
            _partition(malformed_compact, date_from=PERIODS[0][0], date_to=PERIODS[0][1])
            raise AssertionError("malformed compact cell accepted")
        except SnapshotPilotError:
            pass
        compact_month = _build(server, *PERIODS[1], compact=True)
        scale = deepcopy(compact_month)
        scale_rows = scale["table_surface"]["rows"]
        templates = [row for row in scale_rows if row["row_kind"].lower() == "sku"]
        grouping_by_row = {
            row_id: group for group in scale["table_surface"]["groupings"]
            for row_id in group["row_ids"]
        }
        for index in range(8000 - len(scale_rows)):
            template = templates[index % len(templates)]
            row_id = f"SKU:pilot-synthetic-{index:05d}|{template['row_id'].split('|', 1)[-1]}"
            scale_rows.append({**template, "row_id": row_id,
                               "search_text": f"synthetic pilot {index}"})
            grouping_by_row[template["row_id"]]["row_ids"].append(row_id)
        scale["table_surface"]["total_row_count"] = len(scale_rows)
        scale["table_surface"]["returned_row_count"] = len(scale_rows)
        scale_summary, scale_sku, _, _ = _partition(
            scale, date_from=PERIODS[1][0], date_to=PERIODS[1][1]
        )
        scale_bytes = len(scale_summary) + len(scale_sku)
        assert len(scale_rows) == 8000 and scale_bytes < MAX_ARTIFACT_BYTES
        print(f"synthetic structural shape 8000 rows x 31 dates: {scale_bytes} JSON bytes (<128MiB)")

        start, end = PERIODS[0]
        before = read_finished(server.snapshot_pilot_store, date_from=start, date_to=end, part="summary")
        old_generation = before["snapshot_pilot"]["generation_id"]
        changed = deepcopy(full[(start, end)])
        changed["table_surface"]["rows"][0]["values"]["date:2026-04-20"]["display_text"] = "Тестовая версия"
        try:
            publish_finished(server.snapshot_pilot_store, changed, date_from=start,
                             date_to=end, fail_before_pointer=True)
            raise AssertionError("injected failure did not fail")
        except SnapshotPilotError as exc:
            assert str(exc) == "injected_publish_failure"
        assert read_finished(server.snapshot_pilot_store, date_from=start, date_to=end,
                             part="summary")["snapshot_pilot"]["generation_id"] == old_generation
        new_generation = publish_finished(server.snapshot_pilot_store, changed,
                                          date_from=start, date_to=end)
        assert new_generation != old_generation
        assert read_finished(server.snapshot_pilot_store, date_from=start, date_to=end,
                             part="sku", generation_id=old_generation)["snapshot_pilot"]["generation_id"] == old_generation
        for bad in (None, "loading", "error"):
            invalid = deepcopy(full[(start, end)])
            invalid["meta"]["current_state"] = bad
            try:
                publish_finished(server.snapshot_pilot_store, invalid, date_from=start, date_to=end)
                raise AssertionError("non-ready table published")
            except SnapshotPilotError:
                pass
        assert read_finished(server.snapshot_pilot_store, date_from=start, date_to=end,
                             part="summary")["snapshot_pilot"]["generation_id"] == new_generation
        _browser_flow(base)
        # A pilot GET must not call the business evaluator or mutate source DB.
        def forbidden_build(**_kwargs):
            raise AssertionError("business evaluator ran during pilot read")
        server.entrypoint.handle_sheet_web_vitrina_page_composition_request = forbidden_build
        query = urlencode({"surface": "page_composition", "snapshot_pilot": "1",
                           "part": "summary", "date_from": start, "date_to": end})
        assert _read_http(base + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + query)[
            "snapshot_pilot"
        ]["generation_id"] == new_generation
        print("snapshot pilot 14/31 contract, HTTP, last-good: PASS")


def serve_fixture() -> None:
    server = LocalWebVitrinaFixtureServer(with_ready_snapshot=True, ready_days=31,
                                          snapshot_pilot=True)
    with server as base:
        _prepare(server)
        print("Synthetic fixture; these timings are not production timings.", flush=True)
        print("Store:", server.snapshot_pilot_store, flush=True)
        print("Full projections:", server.runtime_dir / "full_projection_*.json", flush=True)
        for start, end in PERIODS:
            print(f"Pilot {start}..{end}:", _url(base, start, end, pilot=True), flush=True)
            print(f"Old path {start}..{end}:", _url(base, start, end, pilot=False), flush=True)
        print("Press Ctrl-C to stop the local fixture server.", flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass


def import_artifact(path: Path, store: Path, start: str, end: str) -> None:
    """Manual import of an already finished trusted full projection JSON."""
    if not store.exists():
        provision_store(store)
    composition = json.loads(path.read_text(encoding="utf-8"))
    print(publish_finished(store, composition, date_from=start, date_to=end))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--import-artifact", type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--date-from")
    parser.add_argument("--date-to")
    args = parser.parse_args()
    if args.serve:
        serve_fixture()
    elif args.import_artifact:
        if not args.store or not args.date_from or not args.date_to:
            parser.error("artifact import requires --store, --date-from and --date-to")
        import_artifact(args.import_artifact, args.store, args.date_from, args.date_to)
    else:
        test_contract()


if __name__ == "__main__":
    main()
