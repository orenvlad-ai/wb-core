"""Focused browser regressions for the released V3 startup UI."""

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
    DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


async def ready_manifest(route, base_url: str) -> dict:
    response = await route.fetch(headers={"Accept": "application/json", "Accept-Encoding": "identity"})
    for _attempt in range(40):
        if response.status == 200:
            return await response.json()
        if response.status != 202:
            raise AssertionError(f"manifest preparation returned {response.status}")
        job_id = (await response.json())["job_id"]
        await asyncio.sleep(0.05)
        response = await route.fetch(
            url=base_url + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "?" + urlencode({
                "surface": "page_composition", "window_format": "window_v3",
                "window_op": "job", "job_id": job_id,
            }),
            headers={"Accept": "application/json", "Accept-Encoding": "identity"},
        )
    raise AssertionError("fixture manifest job did not complete")


async def check_pending_projection(browser, base_url: str, *, force_stale: bool = False) -> None:
    page = await browser.new_page(viewport={"width": 1200, "height": 800})
    release_manifest = asyncio.Event()
    chunk_started = asyncio.Event()
    release_chunk = asyncio.Event()
    counts = {"manifest": 0, "legacy_full": 0, "status": 0, "global_stale": 0}

    async def read_route(route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("include_table_data") == ["1"]:
            counts["legacy_full"] += 1
            await route.fulfill(status=503, content_type="application/json", body='{"error":"unexpected full GET"}')
        elif query.get("window_op") == ["manifest"]:
            counts["manifest"] += 1
            if counts["manifest"] == 1:
                payload = await ready_manifest(route, base_url)
                await release_manifest.wait()
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            else:
                await route.continue_()
        elif force_stale and query.get("window_op") == ["global"] and not counts["global_stale"]:
            counts["global_stale"] += 1
            await route.fulfill(status=409, content_type="application/json", body='{"message":"stale fixture"}')
        elif not force_stale and query.get("window_op") == ["chunk"] and not chunk_started.is_set():
            chunk_started.set()
            await release_chunk.wait()
            await route.continue_()
        else:
            await route.continue_()

    async def status_route(route) -> None:
        counts["status"] += 1
        await route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "revision_no": 999, "revision_id": "fixture-new", "published_at": "2026-04-22T12:00:00Z",
            "updating": False,
        }))

    await page.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "**", read_route)
    await page.route("**" + DEFAULT_SHEET_WEB_VITRINA_BUSINESS_PROJECTION_STATUS_PATH + "**", status_route)
    try:
        await page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode({
            "history_mode": "explicit", "date_from": "2026-04-20", "date_to": "2026-04-21",
        }), wait_until="domcontentloaded")
        await page.wait_for_function("() => state.composition?.meta?.window_v3_available === true && !state.windowV3.manifest")
        await page.wait_for_function("() => state.businessProjection.pendingStatus?.revision_no === 999", timeout=10000)
        if counts["legacy_full"]:
            raise AssertionError(f"pending V3 manifest launched legacy full GET: {counts}")
        release_manifest.set()
        if not force_stale:
            await asyncio.wait_for(chunk_started.wait(), timeout=30)
            await page.evaluate("""() => Promise.all([
              reloadBusinessProjectionTable({revision_no:999,revision_id:'fixture-new'}),
              reloadBusinessProjectionTable({revision_no:999,revision_id:'fixture-new'})
            ])""")
            if counts["manifest"] != 1 or counts["legacy_full"]:
                raise AssertionError(f"duplicate revision restarted in-flight V3 viewport: {counts}")
            release_chunk.set()
        await page.wait_for_function("""() => state.windowV3.firstWindowReady
          && state.businessProjection.revisionNo === 999
          && !state.businessProjection.refreshInProgress
          && !state.businessProjection.pendingStatus""", timeout=90000)
        expected_manifests = 2 if force_stale else 1
        if counts["manifest"] != expected_manifests or counts["legacy_full"]:
            raise AssertionError(f"projection revision was not applied by a causally checked V3 window: {counts}")
        await page.evaluate("() => pollBusinessProjectionRevision()")
        await page.wait_for_function("() => !state.businessProjection.polling")
        if counts["manifest"] != expected_manifests or counts["legacy_full"]:
            raise AssertionError(f"stable projection caused a restart or full GET: {counts}")
    finally:
        release_manifest.set()
        release_chunk.set()
        await page.close()


async def check_initial_window(browser, base_url: str) -> None:
    page = await browser.new_page(viewport={"width": 1200, "height": 800})
    chunks_started = asyncio.Event()

    async def read_route(route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("window_op") == ["manifest"]:
            payload = await ready_manifest(route, base_url)
            payload["dates"][1]["coverage"] = "covered"
            payload["dates"][1]["source_as_of_date"] = "2026-04-19"
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        elif query.get("window_op") == ["chunk"]:
            chunks_started.set()
            await asyncio.sleep(0.6)
            await route.continue_()
        else:
            await route.continue_()

    await page.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "**", read_route)
    try:
        await page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode({
            "history_mode": "explicit", "date_from": "2026-04-19", "date_to": "2026-04-21",
        }), wait_until="domcontentloaded")
        await asyncio.wait_for(chunks_started.wait(), timeout=30)
        before = await page.evaluate("""() => ({
          ready: state.windowV3.firstWindowReady,
          fresh: !!state.tableSnapshot.displayedRequestKey,
          overlay: !document.querySelector('[data-window-v3-loading-overlay]').hidden,
          hiddenTable: getComputedStyle(document.querySelector('[data-table-scroll]')).visibility === 'hidden',
          loadingCells: Array.from(document.querySelectorAll('[data-table-body] td[data-cell-load-state="not_loaded"]'))
            .some(cell => cell.textContent.includes('Загрузка'))
        })""")
        if before != {"ready": False, "fresh": False, "overlay": True, "hiddenTable": True, "loadingCells": True}:
            raise AssertionError(f"incomplete first viewport was shown as ready or missing: {before}")
        await page.wait_for_function("""() => {
          const view = state.windowV3, viewport = view.lastViewport;
          return view.firstWindowReady && !!view.globalResult && !!viewport
            && viewport.rowIndexes.every(row => viewport.dateIndexes.every(date =>
              view.visibleCells.get(row + ':' + date)?.status === 'loaded'));
        }""", timeout=90000)
        after = await page.evaluate("""() => {
          const view=state.windowV3;
          const covered=document.querySelector('[data-table-head] th[data-col-id="date:2026-04-20"]');
          const missing=document.querySelector('[data-table-head] th[data-col-id="date:2026-04-21"]');
          const width=state.composition.table_surface.columns.find(col => col.id === 'date:2026-04-20').size;
          const table=document.querySelector('[data-table-scroll] .vitrina-table');
          const heads=Array.from(document.querySelectorAll('[data-table-head] th'));
          const cols=Array.from(document.querySelectorAll('[data-window-v3-cols] col'));
          const staticWidths=state.windowV3.lastViewport.staticColumns.every(col => {
            const head=heads.find(node => node.dataset.colId === col.id);
            const cell=document.querySelector('[data-table-body] tr.metric-data-row td[data-col-id="' + col.id + '"]');
            const expected=Math.max(24, Number(col.size || col.min_size || 180), Number(col.min_size || 0));
            return head && cell && Math.abs(head.getBoundingClientRect().width - expected) <= 1
              && Math.abs(cell.getBoundingClientRect().width - expected) <= 1;
          });
          return {
            overlay: document.querySelector('[data-window-v3-loading-overlay]').hidden,
            visibleTable: getComputedStyle(document.querySelector('[data-table-scroll]')).visibility === 'visible',
            fresh: !!state.tableSnapshot.displayedRequestKey,
            coveredText: covered?.textContent, coveredTitle: covered?.title,
            coveredAria: covered?.getAttribute('aria-label'), coveredMarker: covered?.dataset.dateCoverage,
            coveredWidth: Math.round(covered?.getBoundingClientRect().width || 0), expectedWidth: width,
            fixedGeometry: cols.length === heads.length && staticWidths
              && Math.abs(table.getBoundingClientRect().width - cols.reduce((sum,col) => sum + col.getBoundingClientRect().width,0)) <= 1,
            headerEllipsis: getComputedStyle(covered).textOverflow === 'ellipsis',
            missingMarker: missing?.dataset.dateCoverage,
            loadedMissing: Array.from(document.querySelectorAll('[data-table-body] td[data-cell-load-state="loaded"]'))
              .some(cell => cell.dataset.colId === 'date:2026-04-21' && cell.textContent.trim() === '—'),
            unloadedVisible: document.querySelectorAll('[data-table-body] td[data-cell-load-state="not_loaded"]').length
          };
        }""")
        if not (after["overlay"] and after["visibleTable"] and after["fresh"]
                and after["coveredText"] == "2026-04-20"
                and "покрыта снимком 2026-04-19" in (after["coveredTitle"] or "")
                and after["coveredTitle"] == after["coveredAria"]
                and after["coveredMarker"] == "covered"
                and after["coveredWidth"] == after["expectedWidth"]
                and after["fixedGeometry"] and after["headerEllipsis"]
                and after["missingMarker"] == "missing"
                and after["loadedMissing"] and after["unloadedVisible"] == 0):
            raise AssertionError(f"V3 first viewport, coverage, or fixed widths regressed: {after}")
    finally:
        await page.close()


async def check_late_projection(browser, base_url: str) -> None:
    page = await browser.new_page(viewport={"width": 1200, "height": 800})
    chunk_started = asyncio.Event()
    release_chunk = asyncio.Event()
    second_manifest_started = asyncio.Event()
    release_second_manifest = asyncio.Event()
    counts = {"manifest": 0, "legacy_full": 0, "chunk": 0}

    async def read_route(route) -> None:
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("include_table_data") == ["1"]:
            counts["legacy_full"] += 1
            await route.fulfill(status=503, content_type="application/json", body='{"error":"unexpected full GET"}')
        elif query.get("window_op") == ["manifest"]:
            counts["manifest"] += 1
            if counts["manifest"] == 2:
                response = await route.fetch()
                second_manifest_started.set()
                await release_second_manifest.wait()
                await route.fulfill(response=response)
                return
            await route.continue_()
        elif query.get("window_op") == ["chunk"] and not counts["chunk"]:
            counts["chunk"] += 1
            response = await route.fetch()
            chunk_started.set()
            await release_chunk.wait()
            await route.fulfill(response=response)
        else:
            await route.continue_()

    await page.route("**" + DEFAULT_SHEET_WEB_VITRINA_READ_PATH + "**", read_route)
    try:
        await page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?" + urlencode({
            "history_mode": "explicit", "date_from": "2026-04-20", "date_to": "2026-04-21",
        }), wait_until="domcontentloaded")
        await asyncio.wait_for(chunk_started.wait(), timeout=30)
        await page.evaluate("""() => windowV3QueuePendingProjection({
          revision_no: 1000, revision_id: 'late-after-dispatch', published_at: '2026-04-22T12:01:00Z'
        })""")
        release_chunk.set()
        await asyncio.wait_for(second_manifest_started.wait(), timeout=30)
        before_new_manifest = await page.evaluate("""() => ({
          revision: state.businessProjection.revisionNo,
          ready: state.windowV3.firstWindowReady,
          pending: state.businessProjection.pendingStatus?.revision_no
        })""")
        if before_new_manifest["revision"] == 1000 or before_new_manifest["ready"]:
            raise AssertionError(f"old chunk acknowledged late status before new checks: {before_new_manifest}")
        release_second_manifest.set()
        await page.wait_for_function("""() => state.windowV3.firstWindowReady
          && state.businessProjection.revisionNo === 1000
          && !state.businessProjection.pendingStatus""", timeout=90000)
        if counts["manifest"] != 2 or counts["legacy_full"]:
            raise AssertionError(f"late status was acknowledged by old chunk or caused legacy GET: {counts}")
    finally:
        release_chunk.set()
        release_second_manifest.set()
        await page.close()


async def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True, advertise_window_v3=True) as base_url:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                await check_pending_projection(browser, base_url)
                await check_pending_projection(browser, base_url, force_stale=True)
                await check_late_projection(browser, base_url)
                await check_initial_window(browser, base_url)
            finally:
                await browser.close()
    print("window_v3_live_ui_browser: pending projection, fixed coverage, and complete initial viewport ok")


if __name__ == "__main__":
    asyncio.run(main())
