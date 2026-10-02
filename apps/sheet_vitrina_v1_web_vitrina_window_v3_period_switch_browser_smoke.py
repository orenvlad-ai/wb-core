"""Controlled late manifest/chunk responses and one-shot chunk retry in V3."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer  # noqa: E402
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)

OLD_FROM = "2026-04-20"
OLD_TO = "2026-04-21"
NEW_FROM = NEW_TO = "2026-04-21"


class ControlledRoute:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.old_response_started = asyncio.Event()
        self.release_old = asyncio.Event()
        self.old_job_id = "delayed-old-" + mode
        self.old_period_active = True
        self.intercepted = False
        self.late_attempted = False
        self.legacy_full = 0
        self.chunk_errors = 0

    async def __call__(self, route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("include_table_data") == ["1"]:
            self.legacy_full += 1
        if query.get("window_format") != ["window_v3"]:
            await route.continue_()
            return
        operation = query.get("window_op", [""])[0]
        if self.mode == "manifest" and operation == "manifest" and not self.intercepted \
                and query.get("date_from") == [OLD_FROM]:
            self.intercepted = True
            await route.fulfill(status=202, content_type="application/json", body=json.dumps(
                {"state": "pending", "job_id": self.old_job_id, "retry_after_ms": 25}))
            return
        if self.mode == "chunk_pending" and operation == "chunk" and self.old_period_active:
            # A viewport redraw may abort this 202 before its first job poll.
            # Keep holding old-period chunks until a poll is actually in flight.
            self.intercepted = True
            await route.fulfill(status=202, content_type="application/json", body=json.dumps(
                {"state": "pending", "job_id": self.old_job_id, "retry_after_ms": 25}))
            return
        if self.mode == "chunk_ready" and operation == "chunk" and self.old_period_active:
            self.intercepted = True
            self.old_response_started.set()
            await self.release_old.wait()
            await self._fulfill_late_result(route, "chunk")
            return
        if self.mode == "chunk_error" and operation == "chunk" and not self.intercepted:
            self.intercepted = True
            self.chunk_errors += 1
            await route.fulfill(status=503, content_type="application/json", body=json.dumps(
                {"message": "one-shot chunk failure"}))
            return
        if operation == "job" and query.get("job_id") == [self.old_job_id]:
            self.old_response_started.set()
            await self.release_old.wait()
            kind = "manifest" if self.mode == "manifest" else "chunk"
            await self._fulfill_late_result(route, kind)
            return
        await route.continue_()

    async def _fulfill_late_result(self, route, kind: str) -> None:
        self.late_attempted = True
        stale = {"response_schema_version": 3, "window_format": "window_v3", "kind": kind,
                 "session_id": "delayed-old-session", "content_token": "delayed-old-token",
                 "period": {"date_from": OLD_FROM, "date_to": OLD_TO},
                 "rows": [], "dates": []}
        try:
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(stale))
        except Exception:
            # An aborted fetch may already have closed its route. The old
            # worker still attempted to publish after the new generation.
            pass


async def wait_new_window(page, old_session: str = "") -> dict:
    await page.wait_for_function("""(oldSession) => {
      const view = state.windowV3;
      const viewport = view.lastViewport;
      return view.manifest?.period?.date_from === '2026-04-21'
        && view.manifest?.period?.date_to === '2026-04-21'
        && !!view.sessionId && view.sessionId !== oldSession
        && !!viewport && viewport.dateIndexes.length === 1
        && viewport.rowIndexes.length > 0
        && viewport.rowIndexes.every(row =>
          view.visibleCells.get(row + ':0')?.status === 'loaded');
    }""", arg=old_session, timeout=90000)
    return await page.evaluate("""() => ({session: state.windowV3.sessionId,
      token: state.windowV3.token, generation: state.windowV3.generation,
      period: state.windowV3.manifest.period, error: state.windowV3.error,
      date_indexes: state.windowV3.lastViewport.dateIndexes,
      rows: document.querySelectorAll('[data-table-body] tr.metric-data-row').length})""")


async def switch_period(page) -> None:
    await page.evaluate("""() => {
      updatePageHistoryPeriod({dateFrom: '2026-04-21', dateTo: '2026-04-21'});
      void loadPageComposition({awaitTable: true});
    }""")


async def check_late_response(browser, base_url: str, mode: str) -> None:
    route = ControlledRoute(mode)
    page = await browser.new_page(viewport={"width": 1200, "height": 800})
    await page.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "**", route)
    url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode(
        {"history_mode": "explicit", "date_from": OLD_FROM, "date_to": OLD_TO})
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await asyncio.wait_for(route.old_response_started.wait(), timeout=90)
        old_session = await page.evaluate("() => state.windowV3.sessionId")
        if mode == "manifest" and old_session:
            raise AssertionError("held old manifest unexpectedly became active")
        if mode.startswith("chunk_") and not old_session:
            raise AssertionError("held old chunk had no active old session")
        route.old_period_active = False
        await switch_period(page)
        fresh = await wait_new_window(page, old_session)
        route.release_old.set()
        await asyncio.sleep(0.2)
        after = await page.evaluate("""() => ({session: state.windowV3.sessionId,
          token: state.windowV3.token, generation: state.windowV3.generation,
          period: state.windowV3.manifest?.period, error: state.windowV3.error,
          date_indexes: state.windowV3.lastViewport?.dateIndexes})""")
        if not route.late_attempted or route.legacy_full or fresh["error"] \
                or any(after[key] != fresh[key] for key in ("session", "token", "generation", "period", "error", "date_indexes")):
            raise AssertionError(f"late {mode} response contaminated the new period: {fresh} -> {after}")
    finally:
        route.release_old.set()
        await page.close()


async def check_chunk_retry(browser, base_url: str) -> None:
    route = ControlledRoute("chunk_error")
    page = await browser.new_page(viewport={"width": 1200, "height": 800})
    await page.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "**", route)
    url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode(
        {"history_mode": "explicit", "date_from": OLD_FROM, "date_to": OLD_TO})
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        await page.wait_for_function("""() => state.windowV3.error.includes('one-shot chunk failure')
          && Array.from(state.windowV3.visibleCells.values()).some(entry => entry.status === 'error')""", timeout=90000)
        before = await page.evaluate("() => ({session: state.windowV3.sessionId, generation: state.windowV3.generation})")
        await page.evaluate("""() => {
          const view = state.windowV3;
          view.visibleCells.clear(); view.visibleBytes = 0; view.visibleKey = ''; view.error = '';
          renderWindowV3Table();
        }""")
        await page.wait_for_function("""() => {
          const view = state.windowV3, viewport = view.lastViewport;
          return !!viewport && viewport.rowIndexes.length > 0 && viewport.dateIndexes.length > 0
            && viewport.rowIndexes.every(row => viewport.dateIndexes.every(date =>
              view.visibleCells.get(row + ':' + date)?.status === 'loaded'));
        }""", timeout=90000)
        after = await page.evaluate("""() => {
          const view = state.windowV3, viewport = view.lastViewport;
          const rowIds = Array.from(document.querySelectorAll(
            '[data-table-body] tr.metric-data-row td[data-col-id="metric_label"]'))
            .map(cell => cell.getAttribute('data-row-id'));
          return {session: view.sessionId, generation: view.generation,
            error: view.error, visible_cells: view.visibleCells.size,
            expected_cells: viewport.rowIndexes.length * viewport.dateIndexes.length,
            dom_rows: rowIds.length, unique_dom_rows: new Set(rowIds).size,
            expected_rows: viewport.rowIndexes.length};
        }""")
        if route.chunk_errors != 1 or route.legacy_full or after["session"] != before["session"] \
                or after["generation"] != before["generation"] or after["error"] \
                or after["visible_cells"] != after["expected_cells"] \
                or after["dom_rows"] != after["unique_dom_rows"] \
                or after["dom_rows"] != after["expected_rows"]:
            raise AssertionError(f"explicit chunk retry duplicated or lost cells: {after}")
    finally:
        await page.close()


async def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True, advertise_window_v3=True) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                await check_late_response(browser, base_url, "manifest")
                await check_late_response(browser, base_url, "chunk_ready")
                await check_late_response(browser, base_url, "chunk_pending")
                await check_chunk_retry(browser, base_url)
            finally:
                await browser.close()
    print("window_v3_period_switch_browser: late manifest/ready+pending chunks and explicit chunk retry ok")


if __name__ == "__main__":
    asyncio.run(main())
