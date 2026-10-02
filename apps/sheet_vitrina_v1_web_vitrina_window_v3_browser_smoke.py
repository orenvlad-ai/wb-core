"""Browser contract check for a 365-day window_v3 with bounded DOM and cache."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)

FIELDS = [
    "value", "display_text", "cell_kind", "formatter_id", "renderer_id",
    "presentation_state", "presentation_tone", "presentation_reason",
    "quality_state", "quality_label", "quality_reason", "completeness_state",
    "missing_sku_count", "quantity_semantic_kind", "quantity_source_observed_at",
    "inventory_finalization_digest",
]


def get_json(url: str) -> dict:
    with urlopen(url, timeout=60) as response:
        return json.load(response)


def wait_full_visible_window(page, label: str) -> None:
    page.wait_for_function("""() => {
      const view = state.windowV3;
      const viewport = view.lastViewport;
      if (view.stale || view.error) return true;
      return !!viewport && viewport.rowIndexes.length > 0 && viewport.dateIndexes.length > 0 &&
        viewport.rowIndexes.every(row => viewport.dateIndexes.every(date =>
          view.visibleCells.get(String(row) + ':' + String(date))?.status === 'loaded'));
    }""", timeout=30000)
    state = page.evaluate("""() => ({stale: state.windowV3.stale, error: state.windowV3.error,
      expected: state.windowV3.lastViewport.rowIndexes.length * state.windowV3.lastViewport.dateIndexes.length,
      loaded: state.windowV3.lastViewport.rowIndexes.reduce((count, row) => count +
        state.windowV3.lastViewport.dateIndexes.filter(date =>
          state.windowV3.visibleCells.get(String(row) + ':' + String(date))?.status === 'loaded').length, 0)})""")
    if state["stale"] or state["error"] or state["loaded"] != state["expected"]:
        raise AssertionError(f"{label} did not fill its full viewport: {state}")


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        read = base_url + DEFAULT_SHEET_WEB_VITRINA_READ_PATH
        shell = get_json(read + "?surface=page_composition&shell_format=metadata_v2")
        table = get_json(read + "?surface=page_composition&include_table_data=1")
        original_table = table["table_surface"]
        original_rows = original_table["rows"]
        static_columns = [column for column in original_table["columns"] if not column["id"].startswith("date:")]
        date_template = next(column for column in original_table["columns"] if column["id"].startswith("date:"))
        end = date(2026, 4, 21)
        dates = [(end - timedelta(days=364 - index)).isoformat() for index in range(365)]
        columns = static_columns + [dict(date_template, id="date:" + day, header=day) for day in dates]
        rows = []
        for copy_index in range(4):
            for row in original_rows:
                rows.append({
                    "row_id": row["row_id"] + ("::copy" + str(copy_index) if copy_index else ""),
                    "row_order": len(rows), "row_kind": row["row_kind"],
                    "section_id": row.get("section_id", ""), "group_id": row.get("group_id", ""),
                    "depth": row.get("depth", 0), "parent_id": row.get("parent_id", ""),
                    "search_text": row.get("search_text", ""),
                    "static_cells": {key: value for key, value in row["values"].items() if not key.startswith("date:")},
                    "filter_tokens": row.get("filter_tokens", {}),
                })
        edge_row = deepcopy(rows[-1])
        edge_row["row_id"] = "window-v3-edge-only"
        edge_row["row_order"] = len(rows)
        edge_row["search_text"] = "edge only"
        rows.append(edge_row)
        blank = next((cell for row in original_rows for key, cell in row["values"].items()
                      if key.startswith("date:") and cell.get("value") is None), None)
        blank = blank or {field: None for field in FIELDS}
        blank["display_text"] = "—"
        blanks = [blank.get(field) for field in FIELDS]
        for row in rows:
            packed_static = {}
            for column_id, cell in row["static_cells"].items():
                values = [cell.get(field) for field in FIELDS]
                while len(values) > 2 and values[-1] == blanks[len(values) - 1]:
                    values.pop()
                packed_static[column_id] = values
            row["static_cells"] = packed_static
        base_cells = [next((cell for key, cell in row["values"].items() if key.startswith("date:")), blank)
                      for row in original_rows]
        base_cells.append(blank)
        filters = deepcopy(table["filter_surface"])
        filters["sort_options"].append({
            "value": "oldest_desc", "column_id": "date:" + dates[0], "direction": "desc", "label": "Старая дата"
        })
        shell.setdefault("meta", {})["window_v3_available"] = True
        shell["historical_access"]["default_date_from"] = dates[0]
        shell["historical_access"]["default_date_to"] = dates[-1]
        shell["historical_access"]["options"] = [{"value": day, "label": day} for day in reversed(dates)]
        metadata = {key: value for key, value in original_table.items() if key != "rows"}
        metadata["columns"] = columns
        metadata["rows"] = []
        manifest = {
            "state": "ready", "response_schema_version": 3, "window_format": "window_v3", "kind": "manifest",
            "session_id": "fixture-session", "content_token": "fixture-version-1",
            "period": {"date_from": dates[0], "date_to": dates[-1]}, "business_date": dates[-1],
            "dates": [{"date": day, "coverage": "covered" if index == 10 else "missing" if index == 11 else "exact",
                       "source_as_of_date": dates[9] if index == 10 else None} for index, day in enumerate(dates)],
            "columns": columns, "metric_catalog": [], "rows": rows, "total_row_count": len(rows),
            "static_value_encoding": {"format": "indexed_cells_v2", "fields": FIELDS, "defaults": blanks},
            "initial_cursor": "initial", "chunk_shape": {"max_dates": 2, "max_rows": 2000, "max_decoded_bytes": 8388608},
            "table_surface": metadata, "filter_surface": filters,
        }
        cursors: dict[str, tuple[int, list[int]]] = {}
        global_queries: dict[str, list[int]] = {}
        pending_jobs: dict[str, dict] = {}
        counts = {"manifest": 0, "global": 0, "seek": 0, "chunk": 0, "legacy_full": 0}
        ack_seen: list[tuple[str, list[str]]] = []
        fail_ack_seek_once = {"enabled": False}
        version_stale = {"enabled": False}

        def cell_for(row_index: int, date_index: int) -> dict:
            if row_index == len(rows) - 1:
                if date_index != 364:
                    return blank
                return dict(blank, value="EDGE_MATCH", display_text="EDGE_MATCH", cell_kind="text")
            if date_index == 11:
                return blank
            if row_index == 0 and date_index == 10:
                return dict(base_cells[0], completeness_state="partial", missing_sku_count=2)
            return base_cells[row_index % len(original_rows)]

        def route_read(route) -> None:
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get("shell_format") == ["metadata_v2"]:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(shell))
                return
            if query.get("include_table_data") == ["1"]:
                counts["legacy_full"] += 1
                route.fulfill(status=500, content_type="application/json", body=json.dumps({"error": "legacy full forbidden"}))
                return
            if query.get("window_format") != ["window_v3"]:
                route.continue_()
                return
            operation = query.get("window_op", [""])[0]
            counts[operation] = counts.get(operation, 0) + 1
            ack_ids = query.get("ack_job_ids", [""])[0].split(",") if query.get("ack_job_ids") else []
            if ack_ids:
                ack_seen.append((operation, ack_ids))
            if operation == "manifest":
                job_id = "manifest-job-" + str(counts["manifest"])
                pending_jobs[job_id] = manifest
                route.fulfill(status=202, content_type="application/json", body=json.dumps(
                    {"state": "pending", "job_id": job_id, "retry_after_ms": 25}))
                return
            elif operation == "global":
                search = query.get("search", [""])[0].lower()
                option = query.get("sort", [""])[0]
                matched = [index for index, row in enumerate(rows) if not search or search in row["search_text"].lower()
                           or (index == len(rows) - 1 and search in "edge_match")]
                handle = "global-" + str(counts["global"])
                global_queries[handle] = matched
                sort_column = next((item["column_id"] for item in filters["sort_options"] if item["value"] == option), "")
                sort_inputs = []
                for index in matched:
                    cell = cell_for(index, dates.index(sort_column[5:])) if sort_column.startswith("date:") else rows[index]["static_cells"].get(sort_column, {})
                    sort_inputs.append({"row_index": index, "value": cell[0] if isinstance(cell, list) else cell.get("value"),
                                        "display_text": cell[1] if isinstance(cell, list) else cell.get("display_text"), "row_order": index})
                payload = {"response_schema_version": 3, "window_format": "window_v3", "kind": "global",
                           "content_token": manifest["content_token"], "global_handle": handle, "search": search,
                           "sort": option, "state": "ready", "total_row_count": len(rows),
                           "matched_row_count": len(matched), "matched_row_indexes": matched, "sort_inputs": sort_inputs}
                if search == "slow_old":
                    pending_jobs["old-global-job"] = payload
                    route.fulfill(status=202, content_type="application/json", body=json.dumps(
                        {"state": "pending", "job_id": "old-global-job", "retry_after_ms": 700}))
                    return
            elif operation == "job":
                job_id = query["job_id"][0]
                if job_id in ack_ids:
                    raise AssertionError("job was acknowledged on its own result poll")
                payload = pending_jobs[job_id]
            elif operation == "seek":
                if fail_ack_seek_once["enabled"] and "retry-test-ack" in ack_ids:
                    fail_ack_seek_once["enabled"] = False
                    route.fulfill(status=503, content_type="application/json", body=json.dumps(
                        {"message": "one-shot request failure"}))
                    return
                date_index = int(query["date_index"][0])
                selected = query.get("selected_row_indexes", [""])[0]
                indexes = [int(value) for value in selected.split(",")] if selected else list(range(
                    int(query["row_start"][0]), int(query["row_start"][0]) + int(query["row_count"][0])))
                cursor = "cursor-" + str(counts["seek"])
                cursors[cursor] = (date_index, indexes)
                payload = {"response_schema_version": 3, "window_format": "window_v3", "kind": "seek", "cursor": cursor}
            elif operation == "chunk":
                if version_stale["enabled"]:
                    route.fulfill(status=409, content_type="application/json", body=json.dumps(
                        {"code": "window_version_stale", "state": "stale", "message": "fixture changed"}))
                    return
                date_index, indexes = cursors[query["cursor"][0]]
                selected_dates = dates[date_index:date_index + 2]
                packed_rows = []
                for row_index in indexes:
                    values = []
                    for local_index, _day in enumerate(selected_dates):
                        if date_index + local_index == 11:
                            continue  # loaded business missing: no cell entry, not transport missing
                        cell = cell_for(row_index, date_index + local_index)
                        values.append([local_index] + [cell.get(field) for field in FIELDS])
                    packed_rows.append({"row_index": row_index, "values": values})
                payload = {"response_schema_version": 3, "window_format": "window_v3", "kind": "chunk",
                           "session_id": manifest["session_id"], "content_token": manifest["content_token"],
                           "dates": selected_dates, "row_start": int(query.get("row_start", ["0"])[0]),
                           "requested_row_count": len(indexes), "row_count": len(indexes), "has_more_rows": False,
                           "next_cursor": None, "value_encoding": {"format": "indexed_cells_v2", "fields": FIELDS,
                                                                  "defaults": blanks}, "rows": packed_rows}
                payload["decoded_bytes"] = len(json.dumps(payload, ensure_ascii=False).encode())
                job_id = "chunk-job-" + str(counts["chunk"])
                pending_jobs[job_id] = payload
                route.fulfill(status=202, content_type="application/json", body=json.dumps(
                    {"state": "pending", "job_id": job_id, "retry_after_ms": 25}))
                return
            elif operation == "cancel":
                payload = {"response_schema_version": 3, "window_format": "window_v3", "kind": "cancel", "cancelled": True}
            else:
                raise AssertionError(f"unexpected operation: {operation}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1600, "height": 900})
                page.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "**", route_read)
                page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
                page.wait_for_function("() => state.windowV3.visibleCells.size > 0", timeout=30000)
                first = page.evaluate("""() => ({rows: document.querySelectorAll('[data-table-body] tr.metric-data-row').length,
                  columns: document.querySelectorAll('[data-table-head] th[data-col-id^="date:"]').length,
                  chunks: state.windowV3.transferChunks.size, bytes: state.windowV3.transferBytes,
                  visibleBytes: state.windowV3.visibleBytes})""")
                if not 0 < first["rows"] <= 200 or first["columns"] <= 4 or first["chunks"] > 2 or first["bytes"] > 16777216 or first["visibleBytes"] > 16777216:
                    raise AssertionError(f"initial window bound: {first}")
                coverage = page.evaluate("""() => ({
                  covered: document.querySelector('[data-table-head] th[data-col-id="date:2025-05-02"]')?.title,
                  missing: document.querySelector('[data-table-head] th[data-col-id="date:2025-05-03"]')?.title
                })""")
                if "покрыта" not in str(coverage["covered"]) or "нет бизнес-факта" not in str(coverage["missing"]):
                    raise AssertionError(f"date coverage: {coverage}")
                page.wait_for_function("() => state.windowV3.visibleCells.has('0:10') && state.windowV3.visibleCells.has('0:11')", timeout=30000)
                business_states = page.evaluate("""() => ({
                  partial: state.windowV3.visibleCells.get('0:10').cell.completeness_state,
                  missing: state.windowV3.visibleCells.get('0:11').cell.value,
                  loaded: state.windowV3.visibleCells.get('0:11').status
                })""")
                if business_states != {"partial": "partial", "missing": None, "loaded": "loaded"}:
                    raise AssertionError(f"loading/business-state boundary: {business_states}")
                page.wait_for_function("() => !!state.windowV3.globalResult", timeout=30000)
                extra_date = page.evaluate("""async () => {
                  const view = state.windowV3;
                  if (view.viewportController) view.viewportController.abort();
                  view.viewportGeneration += 1;
                  view.visibleCells.clear(); view.visibleBytes = 0;
                  view.transferChunks.clear(); view.transferBytes = 0;
                  const dateIndex = 5;
                  const column = state.composition.table_surface.columns.find(item => item.id === 'date:' + view.manifest.dates[dateIndex].date);
                  const viewport = {...view.lastViewport, dateColumns: [column], dateIndexes: [dateIndex],
                    rowIndexes: [0], slots: [{rowIndex: 0, separator: false}]};
                  await windowV3FillViewport(viewport, view.generation, view.viewportGeneration, new AbortController().signal);
                  const result = {requested: view.visibleCells.has('0:5'), extra: view.visibleCells.has('0:6')};
                  view.visibleKey = ''; scheduleWindowV3Viewport();
                  return result;
                }""")
                if extra_date != {"requested": True, "extra": False}:
                    raise AssertionError(f"extra adjacent chunk date escaped visible window: {extra_date}")
                page.wait_for_function("() => document.querySelectorAll('[data-table-head] th[data-col-id^=\"date:\"]').length > 4", timeout=30000)
                gap_v3 = page.evaluate("""() => {
                  state.history.availableDateSet.delete('2026-04-10');
                  state.history.draftDateFrom = '2026-04-09';
                  state.history.draftDateTo = '2026-04-11';
                  const result = validateHistoryDraft();
                  state.history.availableDateSet.add('2026-04-10');
                  return result;
                }""")
                if gap_v3:
                    raise AssertionError(f"v3 covered/missing gap was blocked: {gap_v3}")
                calendar_presets = page.evaluate("""() => ({
                  month: resolveHistoryPresetRange('month', '2025-08-31'),
                  rolling: resolveHistoryPresetRange('rolling_30', '2025-08-31'),
                  all: resolveHistoryPresetRange('all_history', '2026-04-21')
                })""")
                if calendar_presets != {
                    "month": {"dateFrom": "2025-08-01", "dateTo": "2025-08-31"},
                    "rolling": {"dateFrom": "2025-08-02", "dateTo": "2025-08-31"},
                    "all": {"dateFrom": dates[0], "dateTo": dates[-1]},
                }:
                    raise AssertionError(f"calendar month, rolling 30 and all-history merged: {calendar_presets}")
                quarter_presets = page.evaluate("""() => ({
                  quarter: resolveHistoryPresetRange('quarter', '2026-09-30'),
                  rolling: resolveHistoryPresetRange('rolling_90', '2026-09-30')
                })""")
                if quarter_presets != {
                    "quarter": {"dateFrom": "2026-07-01", "dateTo": "2026-09-30"},
                    "rolling": {"dateFrom": "2026-07-03", "dateTo": "2026-09-30"},
                }:
                    raise AssertionError(f"calendar quarter and rolling 90 merged: {quarter_presets}")
                page.evaluate("() => {tableScrollNode.scrollLeft = tableScrollNode.scrollWidth; tableScrollNode.scrollTop = tableScrollNode.scrollHeight / 2;}")
                page.wait_for_function(
                    "(id) => !!document.querySelector('[data-table-head] th[data-col-id=\"' + id + '\"]')",
                    arg="date:" + dates[-1], timeout=30000,
                )
                page.wait_for_timeout(300)
                middle = page.evaluate("""() => ({rows: document.querySelectorAll('[data-table-body] tr.metric-data-row').length,
                  chunks: state.windowV3.transferChunks.size, bytes: state.windowV3.transferBytes,
                  visibleBytes: state.windowV3.visibleBytes, dates: document.querySelectorAll('[data-table-head] th[data-col-id^="date:"]').length})""")
                if middle["rows"] > 200 or middle["chunks"] > 2 or middle["bytes"] > 16777216 or middle["visibleBytes"] > 16777216:
                    raise AssertionError(f"scrolled window bound: {middle}")
                page.evaluate("() => {tableScrollNode.scrollLeft = 0; tableScrollNode.scrollTop = 0;}")
                page.wait_for_function(
                    "(id) => !!document.querySelector('[data-table-head] th[data-col-id=\"' + id + '\"]')",
                    arg="date:" + dates[0], timeout=30000,
                )
                page.evaluate("() => {state.filters.search = 'EDGE_MATCH'; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.matched_row_count === 1", timeout=30000)
                matched = page.evaluate("() => ({count: state.windowV3.displayIndexes.length, row: state.windowV3.displayIndexes[0]})")
                if matched != {"count": 1, "row": len(rows) - 1}:
                    raise AssertionError(f"evicted-date global search: {matched}")
                page.evaluate("() => {tableScrollNode.scrollLeft = tableScrollNode.scrollWidth;}")
                page.wait_for_function("() => document.querySelector('[data-table-body] td[data-col-id=\"date:2026-04-21\"]')?.textContent.includes('EDGE_MATCH')", timeout=30000)
                page.evaluate("() => {tableScrollNode.scrollLeft = 0;}")
                page.evaluate("() => {state.filters.search = ''; state.filters.sort = 'oldest_desc'; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.sort === 'oldest_desc'", timeout=30000)
                sorted_result = page.evaluate("""() => {
                  const inputs = state.windowV3.globalResult.sort_inputs;
                  const rows = inputs.map(item => ({row_id: String(item.row_index), values: {
                    ['date:' + state.windowV3.manifest.dates[0].date]: {value: item.value, display_text: item.display_text},
                    row_order: {value: item.row_order}
                  }}));
                  return {
                    expected: sortRows(rows, state.composition).map(row => Number(row.row_id)),
                    windowOrder: windowV3SortIndexes(state.windowV3.globalResult,
                      state.windowV3.globalResult.matched_row_indexes.map(Number)),
                    actual: state.windowV3.displayIndexes,
                    inputCount: inputs.length
                  };
                }""")
                if sorted_result["inputCount"] != len(rows) or sorted_result["windowOrder"] != sorted_result["expected"]:
                    raise AssertionError(f"unloaded-date sort comparator: {sorted_result}")
                wait_full_visible_window(page, "immediate clear-search and sort")
                page.evaluate("() => {state.filters.search = 'EDGE_MATCH'; state.filters.sort = ''; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.search === 'edge_match'", timeout=30000)
                wait_full_visible_window(page, "search before settled transition")
                page.evaluate("() => {state.filters.search = ''; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.search === '' && state.windowV3.globalResult.sort === ''", timeout=30000)
                wait_full_visible_window(page, "settled clear-search")
                page.evaluate("() => {state.filters.sort = 'oldest_desc'; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.sort === 'oldest_desc'", timeout=30000)
                wait_full_visible_window(page, "sort after settled clear-search")
                page.evaluate("() => {state.filters.search = 'SLOW_OLD'; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalPending && state.windowV3.globalKey.startsWith('SLOW_OLD')")
                page.evaluate("() => {state.filters.search = 'EDGE_MATCH'; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.search === 'edge_match'")
                if page.evaluate("() => state.windowV3.displayIndexes.length") != 1:
                    raise AssertionError("out-of-order global result replaced the newest search")
                page.evaluate("() => {state.filters.search = ''; state.filters.sort = ''; renderTable();}")
                page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.search === ''")
                page.evaluate("() => {tableScrollNode.scrollLeft = 720; tableScrollNode.scrollTop = 950;}")
                before_refresh = page.evaluate("() => ({left: tableScrollNode.scrollLeft, top: tableScrollNode.scrollTop, section: state.filters.section})")
                page.evaluate("() => reloadBusinessProjectionTable({revision_no: 2, revision_id: 'fixture-new'})")
                page.wait_for_function("() => state.windowV3.manifest && state.businessProjection.revisionNo === 2", timeout=30000)
                page.wait_for_function("() => state.windowV3.globalResult && document.querySelectorAll('[data-table-body] tr.metric-data-row').length > 0", timeout=30000)
                after_refresh = page.evaluate("() => ({left: tableScrollNode.scrollLeft, top: tableScrollNode.scrollTop, section: state.filters.section})")
                if abs(before_refresh["left"] - after_refresh["left"]) > 2 or abs(before_refresh["top"] - after_refresh["top"]) > 33 or before_refresh["section"] != after_refresh["section"]:
                    raise AssertionError(f"projection refresh lost position/settings: {before_refresh} -> {after_refresh}")
                page.wait_for_function("""() => {
                  const view = state.windowV3, viewport = view.lastViewport;
                  return !!viewport && viewport.rowIndexes.every(row => viewport.dateIndexes.every(date =>
                    view.visibleCells.get(row + ':' + date)?.status === 'loaded'));
                }""", timeout=30000)
                fail_ack_seek_once["enabled"] = True
                ack_retry = page.evaluate("""async () => {
                  const stateBefore = state.windowV3;
                  stateBefore.ackJobIds.push('retry-test-ack');
                  const params = {session_id: stateBefore.sessionId, content_token: stateBefore.token,
                    date_index: 0, row_start: 0, row_count: 1};
                  let failed = false;
                  try { await windowV3Request('seek', params); } catch (_error) { failed = true; }
                  const retained = stateBefore.ackJobIds.includes('retry-test-ack');
                  const retry = await windowV3Request('seek', params);
                  return {failed, retained, cleared: !stateBefore.ackJobIds.includes('retry-test-ack'),
                    retried: !!retry.cursor};
                }""")
                if ack_retry != {"failed": True, "retained": True, "cleared": True, "retried": True}:
                    raise AssertionError(f"failed piggyback ACK was lost: {ack_retry}")
                if not any(any(job_id.startswith("manifest-job-") for job_id in ids) for _op, ids in ack_seen):
                    raise AssertionError("accepted manifest job was not piggyback-acknowledged")
                if not any(any(job_id.startswith("chunk-job-") for job_id in ids) for _op, ids in ack_seen):
                    raise AssertionError("accepted chunk job was not piggyback-acknowledged")
                manifest_before_stale = counts["manifest"]
                version_stale["enabled"] = True
                page.evaluate("() => {tableScrollNode.scrollLeft = tableScrollNode.scrollWidth; tableScrollNode.scrollTop = tableScrollNode.scrollHeight;}")
                page.wait_for_function("() => state.windowV3.stale === true", timeout=30000)
                page.wait_for_timeout(1000)
                if counts["manifest"] != manifest_before_stale:
                    raise AssertionError("409 caused automatic manifest restart")
                legacy_calendar = page.evaluate("""() => {
                  state.composition.meta.window_v3_available = false;
                  renderHistoricalAccess(state.composition);
                  const hasRolling = !!historyPresetsNode.querySelector('[data-history-preset="rolling_30"]');
                  state.history.availableDateSet.delete('2026-04-10');
                  state.history.availableDates = state.history.availableDates.filter(date => date !== '2026-04-10');
                  state.history.draftDateFrom = '2026-04-09';
                  state.history.draftDateTo = '2026-04-11';
                  const error = validateHistoryDraft();
                  const markup = buildHistoryMonthMarkup({monthStart: '2026-04-01'});
                  const disabledGap = /data-history-day="2026-04-10"[^>]*disabled/.test(markup);
                  const week = resolveHistoryPresetRange('week', '2026-04-16');
                  return {hasRolling, error, disabledGap, week};
                }""")
                if legacy_calendar["hasRolling"] or "непрерывным" not in legacy_calendar["error"] or not legacy_calendar["disabledGap"] or legacy_calendar["week"] != {"dateFrom": "2026-04-11", "dateTo": "2026-04-16"}:
                    raise AssertionError(f"legacy calendar changed before capability: {legacy_calendar}")
                if counts["legacy_full"]:
                    raise AssertionError(f"window_v3 fetched legacy full table: {counts}")
                print("window_v3_browser: ok ->", {"first": first, "middle": middle, "counts": counts})
            finally:
                browser.close()


if __name__ == "__main__":
    main()
