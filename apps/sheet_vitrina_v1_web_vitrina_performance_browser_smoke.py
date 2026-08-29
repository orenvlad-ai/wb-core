"""Focused browser proof for one safe Web Vitrina RUM envelope per page load."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import time

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)
from packages.application.web_vitrina_performance import (  # noqa: E402
    WEB_VITRINA_PERFORMANCE_METRICS,
)


def main() -> None:
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                compact = _check_viewport(
                    browser,
                    base_url,
                    width=560,
                    expected_bucket="compact_560",
                    force_resource_metrics_unavailable=True,
                )
                wide = _check_viewport(
                    browser,
                    base_url,
                    width=1440,
                    expected_bucket="wide_1440",
                    force_resource_metrics_unavailable=False,
                )
            finally:
                browser.close()
    print("web_vitrina_performance_browser: ok ->", compact, wide)


def _check_viewport(
    browser: object,
    base_url: str,
    *,
    width: int,
    expected_bucket: str,
    force_resource_metrics_unavailable: bool,
) -> dict[str, object]:
    context = browser.new_context(viewport={"width": width, "height": 900})
    envelopes: list[dict[str, object]] = []
    if force_resource_metrics_unavailable:
        context.add_init_script(
            """
            const originalGetEntriesByName = performance.getEntriesByName.bind(performance);
            performance.getEntriesByName = function(name, type) {
              if (type === 'resource') return [];
              return originalGetEntriesByName(name, type);
            };
            """
        )

    def fail_telemetry(route: object) -> None:
        try:
            envelopes.append(json.loads(route.request.post_data or "{}"))
        finally:
            route.abort("failed")

    context.route(
        "**/v1/sheet-vitrina-v1/web-vitrina/performance",
        fail_telemetry,
    )
    page = context.new_page()
    try:
        page.goto(base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
        page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
        page.wait_for_function(
            "() => document.querySelectorAll('[data-table-body] tr').length > 0",
            timeout=20000,
        )
        deadline = time.monotonic() + 5
        while not envelopes and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        page.wait_for_timeout(300)
        if page.locator("[data-table-body] tr").count() <= 0:
            raise AssertionError("telemetry transport failure must not hide the rendered table")
    finally:
        context.close()

    if len(envelopes) != 1:
        raise AssertionError(
            f"one page load must emit exactly one performance envelope, got {len(envelopes)}"
        )
    envelope = envelopes[0]
    if envelope.get("viewport_bucket") != expected_bucket:
        raise AssertionError(f"viewport bucket mismatch: {envelope}")
    if len(json.dumps(envelope, separators=(",", ":")).encode("utf-8")) > 4096:
        raise AssertionError("browser performance envelope exceeds 4 KiB")
    metrics = envelope.get("metrics") or {}
    unavailable = set(envelope.get("unavailable_metrics") or [])
    if set(metrics) != set(WEB_VITRINA_PERFORMANCE_METRICS):
        raise AssertionError(f"browser performance metrics mismatch: {metrics}")
    for name, value in metrics.items():
        if value is None:
            if name not in unavailable:
                raise AssertionError(f"null metric must be explicitly unavailable: {name}")
        elif name in unavailable or not isinstance(value, (int, float)) or value < 0:
            raise AssertionError(f"performance phase must be monotonic and numeric: {name}={value!r}")
    required_phase_metrics = {
        "shell_ttfb_ms",
        "shell_download_ms",
        "shell_json_parse_ms",
        "shell_merge_render_ms",
        "shell_double_raf_paint_ms",
        "table_ttfb_ms",
        "table_download_ms",
        "table_json_parse_ms",
        "table_merge_render_ms",
        "table_double_raf_paint_ms",
    }
    if any(metrics.get(name) is None for name in required_phase_metrics):
        raise AssertionError(f"browser phase metrics must be available after the full table paint: {envelope}")
    if force_resource_metrics_unavailable:
        expected_unavailable = {
            "shell_transfer_bytes",
            "shell_encoded_body_bytes",
            "shell_decoded_body_bytes",
            "table_transfer_bytes",
            "table_encoded_body_bytes",
            "table_decoded_body_bytes",
        }
        if not expected_unavailable.issubset(unavailable):
            raise AssertionError(f"resource byte absence must be explicit: {envelope}")
    return {
        "viewport_bucket": expected_bucket,
        "one_envelope": True,
        "telemetry_failure_non_blocking": True,
        "explicit_unavailable": bool(unavailable),
    }


if __name__ == "__main__":
    main()
