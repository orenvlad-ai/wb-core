"""Focused browser regression for the Web Vitrina SKU separator typography."""

from __future__ import annotations

from pathlib import Path
import sys

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
    _check_sku_separators,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)


def main() -> None:
    console_errors: list[str] = []
    page_errors: list[str] = []
    failed_responses: list[str] = []
    with LocalWebVitrinaFixtureServer(with_ready_snapshot=True) as base_url:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1440, "height": 900},
                color_scheme="dark",
            )
            page = context.new_page()
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on(
                "response",
                lambda response: failed_responses.append(
                    f"{response.status} {response.url}"
                )
                if response.status >= 500
                else None,
            )
            response = page.goto(
                base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
                wait_until="domcontentloaded",
            )
            if response is None or response.status >= 500:
                raise AssertionError(f"Web Vitrina route failed: {response}")
            page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
            result = _check_sku_separators(page)
            if console_errors or page_errors or failed_responses:
                raise AssertionError(
                    "separator page must render without fatal browser errors: "
                    f"console={console_errors}, page={page_errors}, responses={failed_responses}"
                )
            context.close()
            browser.close()
    print({"status": "ok", "separator": result})


if __name__ == "__main__":
    main()
