"""Cross-surface browser regression for the shared sheet_vitrina_v1 UI system."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import urlparse

from playwright.sync_api import Page, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_INSTRUCTIONS_UI_PATH,
    DEFAULT_SETTINGS_UI_PATH,
    DEFAULT_SHEET_OPERATOR_UI_PATH,
    DEFAULT_SHEET_SUPPLIER_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    _render_sheet_vitrina_message_page,
    _render_sheet_vitrina_supplier_safe_ui,
)


VIEWPORTS = (
    {"width": 1920, "height": 1080, "name": "screenshot-desktop"},
    {"width": 1280, "height": 900, "name": "narrow-desktop"},
    {"width": 760, "height": 900, "name": "compact-supported"},
)


class _StaticSurfaceServer:
    def __init__(self) -> None:
        self._pages = {
            "/supplier-safe": _render_sheet_vitrina_supplier_safe_ui(),
            "/message": _render_sheet_vitrina_message_page(
                "Недостаточно прав",
                "У текущей учётной записи нет доступа к этому разделу.",
            ),
        }
        self.server = ThreadingHTTPServer(("127.0.0.1", _reserve_free_port()), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def _handler(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                page = owner._pages.get(path)
                if page is None:
                    body = json.dumps({"items": [], "rows": [], "status": "ready"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                else:
                    body = page.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def __enter__(self) -> "_StaticSurfaceServer":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _route_warehouse_options(route: object) -> None:
    route.fulfill(
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(
            {
                "snapshot_date": "2026-04-21",
                "fetched_at": "2026-04-21T15:00:00Z",
                "pagination_complete": True,
                "raw_rows_digest": "sha256:ui-system-layout-fixture",
                "options": [],
            }
        ),
    )


def _route_ads_skus(route: object) -> None:
    route.fulfill(
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(
            {
                "contract_name": "sheet_vitrina_v1_ads_skus",
                "generated_at": "2026-04-21T15:00:00Z",
                "last_refreshed_at": "2026-04-21T15:00:00Z",
                "cache_ttl_seconds": 300,
                "period": {"date_from": "2026-04-15", "date_to": "2026-04-21"},
                "rows": [],
                "meta": {"campaign_count": 0, "external_nm_count": 0},
            }
        ),
    )


def _route_prices(route: object) -> None:
    path = urlparse(route.request.url).path
    contract_name = (
        "sheet_vitrina_v1_prices_quarantine"
        if path.endswith("/quarantine")
        else "sheet_vitrina_v1_prices_goods"
    )
    route.fulfill(
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(
            {
                "contract_name": contract_name,
                "generated_at": "2026-04-21T15:00:00Z",
                "write_enabled": False,
                "rows": [],
                "meta": {"returned_count": 0},
            }
        ),
    )


def _route_api(route: object) -> None:
    path = urlparse(route.request.url).path
    if path.endswith("/supply/wb-warehouses/exclusion-options"):
        _route_warehouse_options(route)
        return
    if path.endswith("/ads/skus"):
        _route_ads_skus(route)
        return
    if path.endswith("/prices/goods") or path.endswith("/prices/quarantine"):
        _route_prices(route)
        return
    route.fulfill(
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(
            {
                "status": "ready",
                "generated_at": "2026-04-21T15:00:00Z",
                "rows": [],
                "items": [],
                "groups": [],
                "documents": [],
                "options": [],
                "runs": [],
            }
        ),
    )


def _layout_evidence(page: Page) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const root = document.documentElement;
          const body = document.body;
          const viewportWidth = root.clientWidth;
          const visible = node => {
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return !node.closest('details:not([open])')
              && style.display !== 'none' && style.visibility !== 'hidden'
              && Number(style.opacity || 1) > 0 && rect.width > 0 && rect.height > 0;
          };
          const hasLocalScroller = node => {
            let current = node.parentElement;
            while (current && current !== body) {
              const style = getComputedStyle(current);
              if (['auto', 'scroll'].includes(style.overflowX)
                  && current.scrollWidth > current.clientWidth + 1) return true;
              current = current.parentElement;
            }
            return false;
          };
          const escaped = Array.from(body.querySelectorAll('*')).filter(visible).filter(node => {
            const rect = node.getBoundingClientRect();
            return (rect.right > viewportWidth + 1 || rect.left < -1) && !hasLocalScroller(node);
          }).slice(0, 12).map(node => ({
            tag: node.tagName,
            className: String(node.className || '').slice(0, 100),
            text: String(node.textContent || '').trim().slice(0, 100),
            rect: {left: Math.round(node.getBoundingClientRect().left), right: Math.round(node.getBoundingClientRect().right)}
          }));
          const uncontainedWideTables = Array.from(body.querySelectorAll('table')).filter(visible).filter(table => {
            if (table.getBoundingClientRect().right <= viewportWidth + 1) return false;
            return !hasLocalScroller(table);
          }).slice(0, 8).map(table => String(table.className || table.id || 'table'));
          const system = document.querySelector('[data-sheet-vitrina-ui-system="v1"]');
          const sampleControl = Array.from(body.querySelectorAll('button, input, select')).find(visible);
          const samplePanel = Array.from(body.querySelectorAll('.panel, .block, .card, .warehouse-card')).find(visible);
          const bodyStyle = getComputedStyle(body);
          const controlStyle = sampleControl ? getComputedStyle(sampleControl) : null;
          const panelStyle = samplePanel ? getComputedStyle(samplePanel) : null;
          return {
            title: document.title,
            bodyTextLength: String(body.innerText || '').trim().length,
            uiSystemCount: system ? 1 : 0,
            viewportWidth,
            documentWidth: root.scrollWidth,
            bodyWidth: body.scrollWidth,
            escaped,
            uncontainedWideTables,
            fontFamily: bodyStyle.fontFamily,
            backgroundColor: bodyStyle.backgroundColor,
            controlHeight: controlStyle ? Math.round(parseFloat(controlStyle.minHeight) || sampleControl.getBoundingClientRect().height) : 0,
            controlRadius: controlStyle ? controlStyle.borderRadius : '',
            panelRadius: panelStyle ? panelStyle.borderRadius : ''
          };
        }"""
    )


def _assert_layout(page: Page, *, name: str, url: str, viewport: dict[str, object]) -> dict[str, object]:
    page_errors: list[str] = []
    console_errors: list[str] = []
    failed_responses: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.on(
        "response",
        lambda response: failed_responses.append(f"{response.status} {response.url}")
        if response.status >= 400
        else None,
    )
    response = page.goto(url, wait_until="domcontentloaded")
    if response is None or response.status >= 500:
        raise AssertionError(f"{name} did not render without 5xx: {response and response.status}")
    page.wait_for_timeout(300)
    evidence = _layout_evidence(page)
    if evidence["uiSystemCount"] != 1:
        raise AssertionError(f"{name} must include the shared UI system once: {evidence}")
    if evidence["bodyTextLength"] <= 0 or not evidence["title"]:
        raise AssertionError(f"{name} must render non-empty title/body: {evidence}")
    if evidence["documentWidth"] > evidence["viewportWidth"] + 1 or evidence["bodyWidth"] > evidence["viewportWidth"] + 1:
        raise AssertionError(f"{name} has document-level horizontal overflow at {viewport['name']}: {evidence}")
    if evidence["escaped"] or evidence["uncontainedWideTables"]:
        raise AssertionError(f"{name} has uncontained visible content at {viewport['name']}: {evidence}")
    if "Inter" not in evidence["fontFamily"] or (
        evidence["controlHeight"] and evidence["controlHeight"] < 30
    ):
        raise AssertionError(f"{name} does not use normalized typography/control geometry: {evidence}")
    if page_errors or console_errors or failed_responses:
        raise AssertionError(
            f"{name} emitted browser errors: page={page_errors}, console={console_errors}, responses={failed_responses}"
        )
    return evidence


def _assert_warehouse_hierarchy(page: Page, base_url: str) -> dict[str, object]:
    response = page.goto(
        base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=warehouses&warehouse=update",
        wait_until="domcontentloaded",
    )
    if response is None or response.status != 200:
        raise AssertionError("warehouse hierarchy surface did not return HTTP 200")
    page.wait_for_timeout(500)
    result = page.evaluate(
        """() => {
          const active = selector => document.querySelector(selector);
          const height = node => node ? Math.round(node.getBoundingClientRect().height) : 0;
          const detailNav = active('.warehouse-detail-switcher');
          const card = active('[data-warehouse-update-view]');
          const longValue = 'whfv_' + '0123456789abcdef'.repeat(12);
          const target = active('[data-warehouse-update-run-id]');
          if (target) { target.textContent = longValue; target.title = longValue; }
          const targetRect = target ? target.getBoundingClientRect() : {right: 0};
          const itemRect = target && target.closest('.warehouse-summary-item')
            ? target.closest('.warehouse-summary-item').getBoundingClientRect() : {right: 0};
          return {
            mainHeight: height(active('.unified-tab-button')),
            sectionHeight: height(active('.warehouse-section-switcher .warehouse-switch')),
            detailHeight: height(active('.warehouse-detail-switcher .warehouse-switch')),
            contentGap: detailNav && card ? Math.round(card.getBoundingClientRect().top - detailNav.getBoundingClientRect().bottom) : -1,
            longValueContained: targetRect.right <= itemRect.right + 1,
            longValueTitle: target ? target.title : '',
            documentWidth: document.documentElement.scrollWidth,
            viewportWidth: document.documentElement.clientWidth,
            tableWrapOverflow: getComputedStyle(active('.warehouse-table-wrap')).overflowX
          };
        }"""
    )
    if not (
        result["mainHeight"] > result["sectionHeight"] > result["detailHeight"]
        and result["contentGap"] >= 12
        and result["longValueContained"]
        and result["longValueTitle"].startswith("whfv_")
        and result["documentWidth"] <= result["viewportWidth"] + 1
        and result["tableWrapOverflow"] in {"auto", "scroll"}
    ):
        raise AssertionError(f"warehouse navigation/technical-value contract failed: {result}")
    return result


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url, _StaticSurfaceServer() as static:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            coverage: dict[str, dict[str, object]] = {}
            route_families = {
                "vitrina": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                "warehouses": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=warehouses&warehouse=update",
                "feedbacks": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=feedbacks",
                "ads": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=ads",
                "prices": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=prices",
                "sku-management": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=sku-management",
                "research": base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH + "?tab=research",
                "operator": base_url + DEFAULT_SHEET_OPERATOR_UI_PATH,
                "settings": base_url + DEFAULT_SETTINGS_UI_PATH + "?embedded=1",
                "instructions": base_url + DEFAULT_INSTRUCTIONS_UI_PATH + "?embedded=1",
                "supplier-internal": base_url + DEFAULT_SHEET_SUPPLIER_UI_PATH,
                "supplier-safe": static.base_url + "/supplier-safe",
                "message-error": static.base_url + "/message",
            }
            for viewport in VIEWPORTS:
                context = browser.new_context(
                    viewport={"width": int(viewport["width"]), "height": int(viewport["height"])}
                )
                context.route("**/v1/**", _route_api)
                for name, url in route_families.items():
                    page = context.new_page()
                    coverage[f"{name}@{viewport['name']}"] = _assert_layout(
                        page,
                        name=name,
                        url=url,
                        viewport=viewport,
                    )
                    page.close()
                context.close()
            hierarchy_context = browser.new_context(viewport={"width": 1920, "height": 1080})
            hierarchy_context.route("**/v1/**", _route_api)
            hierarchy_page = hierarchy_context.new_page()
            hierarchy = _assert_warehouse_hierarchy(hierarchy_page, base_url)
            hierarchy_context.close()
            browser.close()
    print(
        json.dumps(
            {
                "status": "OK",
                "route_family_count": len(route_families),
                "viewport_count": len(VIEWPORTS),
                "coverage_count": len(coverage),
                "warehouse_hierarchy": hierarchy,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
