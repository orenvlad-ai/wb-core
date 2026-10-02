"""Local-only, repeatable window_v3 browser measurements on a copied runtime.

The source runtime is never opened by the server. Run this only after the
window_v3 HTTP route is ready; each cold run gets a fresh copied database.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import resource
import shutil
import socket
import sys
from tempfile import TemporaryDirectory
import threading
from time import perf_counter
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    build_registry_upload_http_server,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.registry_upload_http_entrypoint import RegistryUploadHttpEntrypointConfig

FIXED_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
DATE_FROM = "2025-10-03"
DATE_TO = "2026-10-02"
MIB = 1024 * 1024
VIEWPORT = {"width": 1000, "height": 900}
PERIOD_CHOICES = ("14", "30", "31", "90", "92", "180", "365", "rolling_30", "month_31", "all")
UI_PERIOD_PRESETS = {"rolling_30": "rolling_30", "month_31": "month", "all": "all_history"}


def period_bounds(period: str, fixture_start: str) -> tuple[str, str, int]:
    end = date(2026, 8, 31) if period == "month_31" else date.fromisoformat(DATE_TO)
    start = (date.fromisoformat(fixture_start) if period == "all" else
             date(2026, 8, 1) if period == "month_31" else
             end - timedelta(days=(30 if period == "rolling_30" else int(period)) - 1))
    if start < date.fromisoformat(fixture_start):
        raise ValueError(f"period {period} begins before the fixture")
    return start.isoformat(), end.isoformat(), (end - start).days + 1


def normalize_ru_maxrss(raw: int | None) -> float | None:
    if raw is None:
        return None
    # Darwin reports bytes; Linux reports KiB.
    return round(raw / MIB if sys.platform == "darwin" else raw / 1024, 2)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def server_worker(runtime_dir: str, port: int, control) -> None:
    server = None
    thread = None
    try:
        path = Path(runtime_dir)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=path)
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=path, runtime=runtime, now_factory=lambda: FIXED_NOW,
        )
        config = RegistryUploadHttpEntrypointConfig(
            host="127.0.0.1", port=port,
            upload_path="/v1/registry-upload", sheet_plan_path="/v1/sheet-vitrina-v1/plan",
            sheet_refresh_path="/v1/sheet-vitrina-v1/refresh",
            sheet_status_path="/v1/sheet-vitrina-v1/status",
            sheet_operator_ui_path="/sheet-vitrina-v1/operator", runtime_dir=path,
        )
        server = build_registry_upload_http_server(config, entrypoint=entrypoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        control.send({"ready": True, "port": port})
        while True:
            command = control.recv()
            if command == "rss":
                control.send({"ru_maxrss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
            elif command == "stop":
                break
    except Exception as error:
        control.send({"ready": False, "error": str(error)})
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=5)
        control.close()


def start_server(runtime_dir: Path):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    port = free_port()
    process = context.Process(target=server_worker, args=(str(runtime_dir), port, child))
    process.start()
    if not parent.poll(60):
        process.terminate()
        process.join(timeout=5)
        raise RuntimeError("local server did not become ready")
    status = parent.recv()
    if not status.get("ready"):
        process.join(timeout=5)
        raise RuntimeError("local server failed: " + str(status.get("error")))
    return process, parent, "http://127.0.0.1:" + str(port)


def stop_server(process, control) -> int | None:
    rss = None
    if process.is_alive():
        control.send("rss")
        if control.poll(10):
            rss = control.recv().get("ru_maxrss")
        control.send("stop")
    process.join(timeout=15)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
    control.close()
    return rss


def copied_runtime(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    source_db = source / "registry_upload_runtime.sqlite3"
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    shutil.copy2(source_db, destination / source_db.name)
    return destination


def browser_snapshot(page) -> dict:
    return page.evaluate("""() => {
      if (window.gc) window.gc();
      const view = state.windowV3;
      return {
        dom_rows: document.querySelectorAll('[data-table-body] tr.metric-data-row').length,
        dom_date_columns: document.querySelectorAll('[data-table-head] th[data-col-id^="date:"]').length,
        transfer_chunks: view.transferChunks.size,
        transfer_bytes: view.transferBytes,
        visible_cells: view.visibleCells.size,
        visible_bytes: view.visibleBytes,
        heap_after_gc_bytes: performance.memory?.usedJSHeapSize ?? null,
        heap_peak_50ms_bytes: window.__windowV3HeapPeak ?? null,
        viewport: view.lastViewport ? {first_slot: view.lastViewport.firstSlot,
          last_slot: view.lastViewport.lastSlot,
          dates: view.lastViewport.dateIndexes} : null,
        error: view.error, stale: view.stale
      };
    }""")


def paint_proxy(page) -> float:
    """Two animation frames after data readiness; a paint scheduling proxy, not GPU paint time."""
    return page.evaluate("""() => new Promise(resolve => {
      const started = performance.now();
      requestAnimationFrame(() => requestAnimationFrame(() => resolve(
        Math.round((performance.now() - started) * 10) / 10)));
    })""")


def wait_visible_cells(page, previous_key: str | None = None) -> None:
    page.wait_for_function("""(previousKey) => {
      const view = state.windowV3;
      const viewport = view.lastViewport;
      if (view.stale || view.error) return true;
      if (!viewport || !viewport.rowIndexes.length || !viewport.dateIndexes.length) return false;
      if (previousKey && view.visibleKey === previousKey) return false;
      return viewport.rowIndexes.every(row => viewport.dateIndexes.every(date =>
        ['loaded', 'error'].includes(view.visibleCells.get(String(row) + ':' + String(date))?.status)));
    }""", arg=previous_key, timeout=180000)
    outcome = page.evaluate("""() => ({stale: state.windowV3.stale, error: state.windowV3.error,
      failed: Array.from(state.windowV3.visibleCells.values()).some(entry => entry.status === 'error')})""")
    if outcome["stale"] or outcome["error"] or outcome["failed"]:
        raise AssertionError("visible window failed: " + str(outcome))


def failure_snapshot(page) -> dict:
    return page.evaluate("""() => {
      const view = state.windowV3;
      const viewport = view.lastViewport;
      const expected = viewport ? viewport.rowIndexes.flatMap(row =>
        viewport.dateIndexes.map(date => String(row) + ':' + String(date))) : [];
      const missing = expected.filter(key => view.visibleCells.get(key)?.status !== 'loaded');
      const statusCounts = {};
      for (const key of expected) {
        const status = view.visibleCells.get(key)?.status || 'absent';
        statusCounts[status] = (statusCounts[status] || 0) + 1;
      }
      return {
        generation: view.generation, viewport_generation: view.viewportGeneration,
        visible_key: view.visibleKey, stale: view.stale, error: view.error,
        global_pending: view.globalPending,
        global: view.globalResult ? {search: view.globalResult.search,
          sort: view.globalResult.sort, matched: view.globalResult.matched_row_count,
          handle: view.globalResult.global_handle} : null,
        filter_search: state.filters.search, filter_sort: state.filters.sort,
        viewport: viewport ? {rows: viewport.rowIndexes, dates: viewport.dateIndexes,
          first_slot: viewport.firstSlot, last_slot: viewport.lastSlot} : null,
        expected_count: expected.length, loaded_count: expected.length - missing.length,
        status_counts: statusCounts, missing_keys: missing.slice(0, 40),
        missing_entries: missing.slice(0, 10).map(key => [key, view.visibleCells.get(key)]),
        transfer_chunks: Array.from(view.transferChunks.values()).map(entry => ({
          dates: entry.chunk?.dates, rows: entry.chunk?.rows?.map(row => row.row_index),
          row_count: entry.chunk?.row_count, decoded_bytes: entry.bytes})),
        chunk_trace: window.__v3ChunkTrace || [],
        transfer_bytes: view.transferBytes, visible_bytes: view.visibleBytes,
        dom_rows: document.querySelectorAll('[data-table-body] tr.metric-data-row').length,
        heap_bytes: performance.memory?.usedJSHeapSize ?? null,
        heap_peak_50ms_bytes: window.__windowV3HeapPeak ?? null
      };
    }""")


def jump(page, x_fraction: float, y_fraction: float) -> dict:
    old_key = page.evaluate("() => state.windowV3.visibleKey")
    started = perf_counter()
    page.evaluate("""([x, y]) => {
      tableScrollNode.scrollLeft = (tableScrollNode.scrollWidth - tableScrollNode.clientWidth) * x;
      tableScrollNode.scrollTop = (tableScrollNode.scrollHeight - tableScrollNode.clientHeight) * y;
    }""", [x_fraction, y_fraction])
    wait_visible_cells(page, old_key)
    paint_ms = paint_proxy(page)
    return {"loaded_plus_2raf_proxy_ms": round((perf_counter() - started) * 1000, 1),
            "paint_proxy_2raf_ms": paint_ms, **browser_snapshot(page)}


def refresh_state(page) -> dict:
    return page.evaluate("""() => ({
      top: tableScrollNode.scrollTop, left: tableScrollNode.scrollLeft,
      filters: {section: state.filters.section, group: state.filters.group,
        scope_kind: state.filters.scope_kind, metric: state.filters.metric,
        sort: state.filters.sort,
        selected_metric_keys: Array.from(state.filters.selectedMetricKeys || []).sort()},
      visible_column_ids: Array.from(state.columns.visibleIds || []),
      metric_display: {...state.metricPresentation.unifiedDisplay},
      expanded_metric_anchors: Array.from(state.metricPresentation.expandedAnchors || []).sort()
    })""")


def run_one(browser, base_url: str, label: str, period: str, date_from: str, date_to: str,
            day_count: int, *, trace_chunks: bool = False) -> dict:
    context = browser.new_context(viewport=VIEWPORT)
    requests = Counter()
    statuses = Counter()
    recent_http = []
    partial = {"label": label, "stage": "navigation", "completed": {}}
    context.add_init_script("""(() => {
      window.__windowV3HeapPeak = 0;
      window.setInterval(() => {
        const value = performance.memory?.usedJSHeapSize || 0;
        window.__windowV3HeapPeak = Math.max(window.__windowV3HeapPeak, value);
      }, 50);
    })();""")

    def on_response(response) -> None:
        parsed = urlsplit(response.url)
        if parsed.path != DEFAULT_SHEET_WEB_VITRINA_READ_PATH:
            return
        query = parse_qs(parsed.query)
        if query.get("window_format") == ["window_v3"]:
            operation = query.get("window_op", [""])[0]
            requests[operation] += 1
            statuses[str(response.status)] += 1
            recent_http.append({"op": operation, "status": response.status,
                                "date_index": query.get("date_index", [""])[0],
                                "row_start": query.get("row_start", [""])[0],
                                "row_count": query.get("row_count", [""])[0]})
            del recent_http[:-30]
        elif query.get("include_table_data") == ["1"]:
            requests["legacy_full"] += 1

    page = context.new_page()
    page.on("response", on_response)
    via_ui = period in UI_PERIOD_PRESETS
    url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode({
        "history_mode": "explicit", "date_from": date_to if via_ui else date_from, "date_to": date_to,
    })
    try:
        navigation_started = perf_counter()
        page.goto(url, wait_until="domcontentloaded", timeout=180000)
        if trace_chunks:
            page.evaluate("""() => {
              window.__v3ChunkTrace = [];
              const remember = windowV3RememberChunk;
              windowV3RememberChunk = function(key, chunk) {
                const viewport = state.windowV3.lastViewport;
                window.__v3ChunkTrace.push({key, dates: chunk.dates,
                  rows: (chunk.rows || []).map(row => row.row_index),
                  viewport_rows: viewport?.rowIndexes || [],
                  viewport_dates: viewport?.dateIndexes || []});
                if (window.__v3ChunkTrace.length > 32) window.__v3ChunkTrace.shift();
                return remember(key, chunk);
              };
            }""")
        setup_ms = None
        setup_requests = None
        if via_ui:
            page.wait_for_function("() => state.windowV3.manifest && state.windowV3.manifest.dates.length === 1", timeout=180000)
            wait_visible_cells(page)
            setup_ms = round((perf_counter() - navigation_started) * 1000, 1)
            setup_requests = dict(requests)
            requests.clear()
            statuses.clear()
        started = perf_counter() if via_ui else navigation_started
        if via_ui:
            page.locator("[data-history-toggle]").click()
            page.locator('[data-history-preset="' + UI_PERIOD_PRESETS[period] + '"]').click()
            selected = page.evaluate("() => ({from: state.history.draftDateFrom, to: state.history.draftDateTo})")
            if selected != {"from": date_from, "to": date_to}:
                raise AssertionError(f"UI period {period} selected wrong dates: {selected}")
            page.locator("[data-history-save]").click()
        page.wait_for_function("""(expected) => state.windowV3.manifest
          && state.windowV3.manifest.dates.length === expected.days
          && state.windowV3.manifest.period.date_from === expected.from
          && state.windowV3.manifest.period.date_to === expected.to""",
          arg={"days": day_count, "from": date_from, "to": date_to}, timeout=180000)
        wait_visible_cells(page)
        initial_paint_ms = paint_proxy(page)
        initial = {"loaded_plus_2raf_proxy_ms": round((perf_counter() - started) * 1000, 1),
                   "selection_mode": "UI " + UI_PERIOD_PRESETS[period] if via_ui else "explicit URL",
                   "ui_setup_one_day_ms": setup_ms, "ui_setup_requests": setup_requests,
                   "paint_proxy_2raf_ms": initial_paint_ms, **browser_snapshot(page)}
        partial["completed"]["initial"] = initial
        candidate = page.evaluate("""() => {
          const view = state.windowV3;
          for (const [key, entry] of view.visibleCells) {
            const [rowIndex, dateIndex] = key.split(':').map(Number);
            const display = String(entry.cell?.display_text || '').trim();
            if (display.length < 4 || display === '—' || display === 'Загрузка…') continue;
            const catalog = view.catalogRows[rowIndex];
            if (String(catalog.search_text || '').toLocaleLowerCase('ru').includes(display.toLocaleLowerCase('ru'))) continue;
            return {row_index: rowIndex, date_index: dateIndex, display_text: display, cell_key: key};
          }
          return null;
        }""")
        if not candidate:
            raise AssertionError("no date-cell search candidate outside static catalog")
        partial["stage"] = "middle_jump"
        midpoint = jump(page, 0.5, 0.5)
        partial["completed"]["middle"] = midpoint
        partial["stage"] = "last_jump"
        last = jump(page, 1, 1)
        partial["completed"]["last"] = last
        partial["stage"] = "first_jump"
        first_again = jump(page, 0, 0)
        partial["completed"]["first_again"] = first_again
        partial["stage"] = "last_again_jump"
        last_again = jump(page, 1, 1)
        partial["completed"]["last_again"] = last_again
        if page.evaluate("(index) => state.windowV3.lastViewport.dateIndexes.includes(index)", candidate["date_index"]):
            raise AssertionError("candidate date was not evicted; viewport is too wide for this period")
        if page.evaluate("(key) => state.windowV3.visibleCells.has(key)", candidate["cell_key"]):
            raise AssertionError("candidate date cell was not evicted before global search")
        search_text = candidate["display_text"][:80]
        search_started = perf_counter()
        partial["stage"] = "evicted_date_search"
        page.evaluate("(search) => {state.filters.search = search; renderTable();}", search_text)
        page.wait_for_function("() => state.windowV3.globalResult && String(state.windowV3.globalResult.search).toLowerCase() === String(state.filters.search).toLowerCase()", timeout=180000)
        wait_visible_cells(page)
        search_paint_ms = paint_proxy(page)
        search = {"candidate": candidate, "matched": page.evaluate("() => state.windowV3.globalResult.matched_row_count"),
                  "row_found": page.evaluate("(index) => state.windowV3.displayIndexes.includes(index)", candidate["row_index"]),
                  "loaded_plus_2raf_proxy_ms": round((perf_counter() - search_started) * 1000, 1),
                  "paint_proxy_2raf_ms": search_paint_ms,
                  **browser_snapshot(page)}
        partial["completed"]["search"] = search
        if not search["row_found"]:
            raise AssertionError("global search missed a cell from an evicted date")
        clear_started = perf_counter()
        page.evaluate("() => {state.filters.search = ''; renderTable();}")
        partial["stage"] = "clear_search_visible_cells"
        page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.search === ''", timeout=180000)
        wait_visible_cells(page)
        clear_paint_ms = paint_proxy(page)
        clear_search = {"loaded_plus_2raf_proxy_ms": round((perf_counter() - clear_started) * 1000, 1),
                        "paint_proxy_2raf_ms": clear_paint_ms, **browser_snapshot(page)}
        partial["completed"]["clear_search"] = clear_search
        sort_value = page.evaluate("""() => {
          const options = state.composition.filter_surface?.sort_options || [];
          const firstDate = state.windowV3.manifest.dates[0]?.date;
          return options.find(option => option.column_id === 'date:' + firstDate)?.value ||
            options.find(option => String(option.column_id || '').startsWith('date:'))?.value || '';
        }""")
        if not sort_value:
            raise AssertionError("no date sort option in manifest")
        sort_started = perf_counter()
        page.evaluate("(sort) => {state.filters.sort = sort; renderTable();}", sort_value)
        partial["stage"] = "sort_visible_cells"
        page.wait_for_function("() => state.windowV3.globalResult && state.windowV3.globalResult.sort === state.filters.sort", timeout=180000)
        wait_visible_cells(page)
        sort_paint_ms = paint_proxy(page)
        comparator = page.evaluate("""() => {
          const result = state.windowV3.globalResult;
          const option = (state.composition.filter_surface?.sort_options || []).find(item => item.value === state.filters.sort);
          const rows = result.sort_inputs.map(item => ({row_id: String(item.row_index), values: {
            [option.column_id]: {value: item.value, display_text: item.display_text},
            row_order: {value: item.row_order}
          }}));
          const expected = sortRows(rows, state.composition).map(row => Number(row.row_id));
          const actual = windowV3SortIndexes(result, result.matched_row_indexes.map(Number));
          return {equal: expected.length === actual.length && expected.every((index, position) => index === actual[position]),
            inputs: rows.length, first_expected: expected.slice(0, 5), first_actual: actual.slice(0, 5)};
        }""")
        if not comparator["equal"]:
            raise AssertionError("global sort differs from the legacy JS comparator: " + str(comparator))
        sort = {"option": sort_value, "comparator": comparator,
                "loaded_plus_2raf_proxy_ms": round((perf_counter() - sort_started) * 1000, 1),
                "paint_proxy_2raf_ms": sort_paint_ms, **browser_snapshot(page)}
        partial["completed"]["sort"] = sort
        partial["stage"] = "projection_refresh"
        prior_view_key = page.evaluate("() => state.windowV3.visibleKey")
        page.evaluate("""() => {
          const maxTop = tableScrollNode.scrollHeight - tableScrollNode.clientHeight;
          tableScrollNode.scrollTop = Math.round(maxTop * 0.5);
        }""")
        wait_visible_cells(page, prior_view_key)
        paint_proxy(page)
        before_refresh = refresh_state(page)
        if before_refresh["top"] <= 0:
            raise AssertionError("projection refresh exercise did not reach nonzero vertical scroll")
        before_identity = page.evaluate("() => ({generation: state.windowV3.generation, session: state.windowV3.sessionId})")
        manifest_requests_before = requests["manifest"]
        page.evaluate("() => reloadBusinessProjectionTable({revision_no: state.businessProjection.revisionNo + 1, revision_id: 'browser-readback'})")
        page.wait_for_function("(before) => state.windowV3.generation > before.generation && state.windowV3.sessionId && state.windowV3.sessionId !== before.session", arg=before_identity, timeout=180000)
        wait_visible_cells(page)
        refresh_paint_ms = paint_proxy(page)
        after_refresh = refresh_state(page)
        refresh = {"before": before_refresh, "after": after_refresh, "old_identity": before_identity,
                   "new_identity": page.evaluate("() => ({generation: state.windowV3.generation, session: state.windowV3.sessionId})"),
                   "paint_proxy_2raf_ms": refresh_paint_ms, **browser_snapshot(page)}
        partial["completed"]["refresh"] = refresh
        if requests["manifest"] <= manifest_requests_before:
            raise AssertionError("projection refresh did not request a new manifest")
        if abs(before_refresh["left"] - after_refresh["left"]) > 2 or abs(before_refresh["top"] - after_refresh["top"]) > 33 \
                or any(before_refresh[key] != after_refresh[key] for key in (
                    "filters", "visible_column_ids", "metric_display", "expanded_metric_anchors")):
            raise AssertionError("projection refresh lost scroll or selected settings: " + str(refresh))
        result = {"label": label, "initial": initial, "middle": midpoint, "last": last,
                  "first_again": first_again, "last_again": last_again, "search": search,
                  "clear_search": clear_search, "sort": sort,
                  "projection_refresh_simulated": refresh, "requests": dict(requests), "statuses": dict(statuses)}
        if requests["legacy_full"]:
            raise AssertionError("window_v3 opened a legacy full table GET")
        for phase in (initial, midpoint, last, first_again, last_again, search, clear_search, sort, refresh):
            if phase["dom_rows"] > 200 or phase["transfer_chunks"] > 2 \
                    or phase["transfer_bytes"] > 16 * MIB or phase["visible_bytes"] > 16 * MIB:
                raise AssertionError("browser window bound failed: " + str(phase))
            heap = phase["heap_after_gc_bytes"]
            if heap is not None and heap > 96 * MIB:
                raise AssertionError("retained browser JS heap exceeded 96 MiB: " + str(phase))
        return result
    except Exception as error:
        try:
            partial["diagnostic"] = failure_snapshot(page)
        except Exception as diagnostic_error:
            partial["diagnostic_error"] = str(diagnostic_error)
        partial["requests"] = dict(requests)
        partial["statuses"] = dict(statuses)
        partial["recent_http"] = recent_http
        error.window_v3_partial = partial
        raise
    finally:
        context.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--cold-only", action="store_true", help="one diagnostic cold series without warm-up or warm samples")
    parser.add_argument("--diagnose-chunks", action="store_true", help="record bounded chunk/viewport correspondence on failures")
    parser.add_argument("--periods", nargs="+", choices=PERIOD_CHOICES, default=["365"],
                        help="calendar-day periods; 'all' uses the fixture's first date")
    args = parser.parse_args()
    if args.runs < 1:
        raise ValueError("--runs must be positive")
    summary = json.loads((args.runtime_dir / "fixture-summary.json").read_text())
    if summary.get("date_from") != DATE_FROM or summary.get("date_to") != DATE_TO:
        raise AssertionError("unexpected fixed fixture period")
    report = {"fixture": summary, "fixed_now": FIXED_NOW.isoformat(),
              "viewport": VIEWPORT,
              "method": "first visible cells loaded plus double requestAnimationFrame paint proxy; post-GC heap and 50 ms sampled peak",
              "cold_semantics": "fresh DB copy, server process, Chromium process and browser context per sample; OS file cache is not forcibly cleared; UI presets first open one day, then timed UI apply",
              "warm_semantics": "one full warm-up excluded from samples; same DB copy, server process and Chromium process thereafter; new browser context/page and new V3 session per sample; service caches may persist until TTL",
              "runs_per_mode": args.runs, "periods": {}, "errors": []}
    browser_args = ["--js-flags=--expose-gc", "--enable-precise-memory-info"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            for period in args.periods:
                date_from, date_to, day_count = period_bounds(period, summary["date_from"])
                measurements = {"date_from": date_from, "date_to": date_to, "days": day_count,
                                "cold": [], "warm": []}
                report["periods"][period] = measurements
                for index in range(args.runs):
                    with TemporaryDirectory(prefix="window-v3-cold-") as temporary:
                        runtime_dir = copied_runtime(args.runtime_dir, Path(temporary) / "runtime")
                        process, control, base_url = start_server(runtime_dir)
                        result = None
                        try:
                            browser = playwright.chromium.launch(headless=True, args=browser_args)
                            try:
                                result = run_one(browser, base_url, "cold-" + str(index + 1), period, date_from, date_to, day_count,
                                                 trace_chunks=args.diagnose_chunks)
                            finally:
                                browser.close()
                        finally:
                            rss = stop_server(process, control)
                            if result is None:
                                measurements["failed_server_peak_rss_mib"] = normalize_ru_maxrss(rss)
                        result["server_peak_rss_mib"] = normalize_ru_maxrss(rss)
                        measurements["cold"].append(result)
                        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                if args.cold_only:
                    continue
                with TemporaryDirectory(prefix="window-v3-warm-") as temporary:
                    runtime_dir = copied_runtime(args.runtime_dir, Path(temporary) / "runtime")
                    process, control, base_url = start_server(runtime_dir)
                    warm_completed = False
                    try:
                        browser = playwright.chromium.launch(headless=True, args=browser_args)
                        try:
                            warm_up_started = perf_counter()
                            warm_up = run_one(browser, base_url, "warm-up", period, date_from, date_to, day_count,
                                              trace_chunks=args.diagnose_chunks)
                            measurements["warm_up"] = {
                                "elapsed_ms": round((perf_counter() - warm_up_started) * 1000, 1),
                                "result": warm_up,
                            }
                            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                            for index in range(args.runs):
                                measurements["warm"].append(run_one(
                                    browser, base_url, "warm-" + str(index + 1), period, date_from, date_to, day_count,
                                    trace_chunks=args.diagnose_chunks))
                                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                            warm_completed = True
                        finally:
                            browser.close()
                    finally:
                        rss = stop_server(process, control)
                        if not warm_completed:
                            measurements["failed_warm_server_peak_rss_mib"] = normalize_ru_maxrss(rss)
                    measurements["warm_server_peak_rss_mib"] = normalize_ru_maxrss(rss)
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error),
                                 "partial": getattr(error, "window_v3_partial", None)})
        raise
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
