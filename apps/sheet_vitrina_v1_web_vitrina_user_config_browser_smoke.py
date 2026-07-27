"""Browser smoke for server-side web-vitrina metrics presentation config."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import threading
from tempfile import TemporaryDirectory
from urllib import parse as urllib_parse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_page_composition_smoke import (  # noqa: E402
    BUNDLE_FIXTURE,
    NOW,
    _build_activity_surface_fixture,
    _build_plan,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_JOB_PATH,
    DEFAULT_SHEET_REFRESH_PATH,
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH,
    _render_sheet_vitrina_web_vitrina_ui,
)
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime  # noqa: E402
from packages.application.registry_upload_http_entrypoint import _active_incident_metric_catalog  # noqa: E402
from packages.application.sheet_vitrina_v1_archived_metrics import (  # noqa: E402
    LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS,
)
from packages.application.sheet_vitrina_v1_incident_stocks import INCIDENT_STOCK_FACT_METRIC_KEYS  # noqa: E402
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock  # noqa: E402
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter  # noqa: E402
from packages.application.web_vitrina_page_composition import build_web_vitrina_page_composition  # noqa: E402
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model  # noqa: E402

STORAGE_KEY = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:metric-presentation:v1"
TOTAL_ORDER_SUM_SELECTOR = (
    '[data-metric-display-select][data-metric-config-scope="total"]'
    '[data-metric-config-key="total_orderSum"]'
)
RETIRED_METRIC_KEYS = frozenset(
    (*INCIDENT_STOCK_FACT_METRIC_KEYS, *LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS)
)


def main() -> None:
    with TemporaryDirectory(prefix="web-vitrina-user-config-browser-") as tmp:
        composition = _build_composition(Path(tmp) / "runtime")
        with FixtureServer(composition) as server:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    _run_checks(browser, server)
                finally:
                    browser.close()
    print(
        {
            "status": "ok",
            "checks": [
                "local_migration",
                "server_priority",
                "retired_metric_sanitation",
                "reload",
                "cleared_local_storage",
            ],
        }
    )


def _run_checks(browser, server: "FixtureServer") -> None:
    local_candidate = {
        "version": 2,
        "scopes": {
            "total": {
                "order": [
                    "total_orderSum",
                    "total_wb_stock_fact_qty",
                    "avg_cost_price_rub",
                    "total_proxy_profit_rub",
                    "avg_ctr_current",
                ],
                "display": {
                    "total_orderSum": "hidden",
                    "total_wb_stock_fact_qty": "collapsed",
                    "avg_cost_price_rub": "hidden",
                },
                "manual": True,
            },
            "sku": {
                "order": ["wb_stock_fact_qty", "cost_price_rub", "proxy_profit_rub"],
                "display": {
                    "wb_stock_fact_qty": "collapsed",
                    "cost_price_rub": "hidden",
                },
                "manual": True,
            },
        },
        "expanded_anchors": ["sku::wb_stock_fact_qty", "total::avg_cost_price_rub"],
    }
    context = browser.new_context()
    page = context.new_page()
    page.add_init_script(
        "(() => { const [storageKey, payload] = "
        + json.dumps([STORAGE_KEY, local_candidate], ensure_ascii=False)
        + "; window.localStorage.setItem(storageKey, JSON.stringify(payload)); })();"
    )
    page.goto(server.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
    _open_metrics(page)
    page.wait_for_selector(TOTAL_ORDER_SUM_SELECTOR)
    _wait_for_server_save_count(server, 1)
    migrated = server.user_config["config"]
    if migrated["scopes"]["total"]["display"].get("total_orderSum") != "hidden":
        raise AssertionError(f"valid localStorage must migrate once when server config is missing, got {migrated}")
    _assert_retired_metrics_absent(page, migrated)
    context.close()

    server.user_config = {
        "status": "ok",
        "revision": 4,
        "updated_at": "2026-06-01T10:00:00Z",
        "config": {
            "version": 2,
            "scopes": {
                "total": {
                    "order": [
                        "total_orderSum",
                        "total_wb_stock_fact_qty",
                        "avg_cost_price_rub",
                        "total_orderCount",
                    ],
                    "display": {
                        "total_wb_stock_fact_qty": "collapsed",
                        "avg_cost_price_rub": "hidden",
                    },
                    "manual": True,
                },
                "sku": {
                    "order": ["wb_stock_fact_qty", "cost_price_rub"],
                    "display": {"cost_price_rub": "hidden"},
                    "manual": True,
                },
            },
            "expanded_anchors": ["sku::wb_stock_fact_qty"],
        },
    }
    server.save_count = 0
    stale_context = browser.new_context()
    stale_page = stale_context.new_page()
    stale_page.add_init_script(
        "(() => { const [storageKey, payload] = "
        + json.dumps([STORAGE_KEY, local_candidate], ensure_ascii=False)
        + "; window.localStorage.setItem(storageKey, JSON.stringify(payload)); })();"
    )
    stale_page.goto(server.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
    _open_metrics(stale_page)
    stale_page.wait_for_selector(TOTAL_ORDER_SUM_SELECTOR)
    if stale_page.locator(TOTAL_ORDER_SUM_SELECTOR).input_value() != "shown":
        raise AssertionError("stale localStorage must not hide total_orderSum when server config exists")
    if server.save_count != 0:
        raise AssertionError("initial render from server config must not resave stale localStorage")
    _assert_retired_metrics_absent(stale_page)

    stale_page.select_option(TOTAL_ORDER_SUM_SELECTOR, "hidden")
    _wait_for_server_save_count(server, 1)
    if server.user_config["revision"] != 5:
        raise AssertionError(f"user change must persist to next server revision, got {server.user_config}")
    _assert_retired_metrics_absent(stale_page, server.user_config["config"])
    stale_context.close()

    clear_context = browser.new_context()
    clear_page = clear_context.new_page()
    clear_page.goto(server.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
    _open_metrics(clear_page)
    clear_page.wait_for_selector(TOTAL_ORDER_SUM_SELECTOR)
    if clear_page.locator(TOTAL_ORDER_SUM_SELECTOR).input_value() != "hidden":
        raise AssertionError("cleared localStorage/new browser context must restore server-side metric config")
    _assert_retired_metrics_absent(clear_page, server.user_config["config"])
    clear_context.close()


def _assert_retired_metrics_absent(page, config: object | None = None) -> None:
    for metric_key in RETIRED_METRIC_KEYS:
        selector = f'[data-metric-config-key="{metric_key}"]'
        if page.locator(selector).count():
            raise AssertionError(f"retired metric leaked into settings/picker: {metric_key}")
    if config is None:
        return
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
    leaked = sorted(metric_key for metric_key in RETIRED_METRIC_KEYS if metric_key in serialized)
    if leaked:
        raise AssertionError(f"retired metric keys survived saved-state sanitation: {leaked}")


def _open_metrics(page) -> None:
    page.locator("[data-metrics-presentation]").evaluate("node => node.open = true")


def _wait_for_server_save_count(server: "FixtureServer", expected: int) -> None:
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        if server.save_count >= expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"user config save was not observed, expected {expected}, got {server.save_count}")


def _build_composition(runtime_dir: Path) -> dict[str, object]:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-21T12:00:00Z")
    if accepted.status != "accepted":
        raise AssertionError(f"fixture bundle must be accepted, got {accepted}")
    current_state = runtime.load_current_state()
    enabled = [item for item in current_state.config_v2 if item.enabled]
    first_group = enabled[0].group
    start_date = NOW.date() - timedelta(days=6)
    for offset in range(7):
        snapshot_date = (start_date + timedelta(days=offset)).isoformat()
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=f"{snapshot_date}T12:05:00Z",
            plan=_build_plan(
                as_of_date=snapshot_date,
                first_nm_id=enabled[0].nm_id,
                second_nm_id=enabled[1].nm_id,
                first_group=first_group,
            ),
        )
    contract = SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: NOW).build(
        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    )
    view_model = build_web_vitrina_view_model(contract)
    adapter = build_web_vitrina_gravity_table_adapter(view_model)
    return build_web_vitrina_page_composition(
        contract=contract,
        view_model=view_model,
        adapter=adapter,
        page_route=DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
        read_route=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
        operator_route="/sheet-vitrina-v1/operator",
        available_snapshot_dates=runtime.list_sheet_vitrina_ready_snapshot_dates(descending=True),
        selected_as_of_date=None,
        selected_date_from=None,
        selected_date_to=None,
        activity_surface=_build_activity_surface_fixture(),
        metric_catalog=_active_incident_metric_catalog(),
    )


class FixtureServer:
    def __init__(self, composition: dict[str, object]) -> None:
        self.composition = composition
        self.user_config: dict[str, object] = {
            "status": "missing",
            "revision": 0,
            "updated_at": "",
            "config": None,
        }
        self.save_count = 0
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def __enter__(self) -> "FixtureServer":
        port = _reserve_free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        server = self
        html = _render_sheet_vitrina_web_vitrina_ui(
            read_path=DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
            operator_path="/sheet-vitrina-v1/operator",
            refresh_path=DEFAULT_SHEET_REFRESH_PATH,
            job_path=DEFAULT_SHEET_JOB_PATH,
        )

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                if parsed.path == DEFAULT_SHEET_WEB_VITRINA_UI_PATH:
                    _write(self, HTTPStatus.OK, "text/html; charset=utf-8", html)
                    return
                if parsed.path == DEFAULT_SHEET_WEB_VITRINA_READ_PATH:
                    _write(self, HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(server.composition, ensure_ascii=False))
                    return
                if parsed.path == DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH:
                    payload = {
                        "status": server.user_config.get("status"),
                        "config_key": "metric_presentation",
                        "schema_version": 1 if server.user_config.get("status") == "ok" else 0,
                        "revision": server.user_config.get("revision", 0),
                        "updated_at": server.user_config.get("updated_at", ""),
                        "config": server.user_config.get("config"),
                    }
                    _write(self, HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(payload, ensure_ascii=False))
                    return
                _write(self, HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", json.dumps({"error": "not_found"}))

            def do_POST(self) -> None:  # noqa: N802
                parsed = urllib_parse.urlparse(self.path)
                if parsed.path != DEFAULT_SHEET_WEB_VITRINA_USER_CONFIG_PATH:
                    _write(self, HTTPStatus.NOT_FOUND, "application/json; charset=utf-8", json.dumps({"error": "not_found"}))
                    return
                length = int(self.headers.get("Content-Length") or "0")
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                base_revision = int(payload.get("base_revision") or 0)
                current_revision = int(server.user_config.get("revision") or 0)
                if base_revision != current_revision:
                    _write(
                        self,
                        HTTPStatus.CONFLICT,
                        "application/json; charset=utf-8",
                        json.dumps({"status": "conflict", "current": server.user_config}, ensure_ascii=False),
                    )
                    return
                server.save_count += 1
                server.user_config = {
                    "status": "ok",
                    "revision": current_revision + 1,
                    "updated_at": "2026-06-01T10:00:00Z",
                    "config": payload.get("config"),
                }
                _write(
                    self,
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps(
                        {
                            "status": "ok",
                            "config_key": "metric_presentation",
                            "schema_version": 1,
                            "revision": server.user_config["revision"],
                            "updated_at": server.user_config["updated_at"],
                            "config": server.user_config["config"],
                        },
                        ensure_ascii=False,
                    ),
                )

            def log_message(self, *_args) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if self.thread:
            self.thread.join(timeout=2)


def _write(handler: BaseHTTPRequestHandler, status: HTTPStatus, content_type: str, body: str) -> None:
    raw = body.encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _reserve_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    main()
