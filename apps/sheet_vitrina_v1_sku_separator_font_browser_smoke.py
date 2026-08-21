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
            layout_samples = page.evaluate(
                """() => {
                  const root = document.documentElement;
                  const original = root.style.getPropertyValue('--table-font-size');
                  const samples = [9, 10.5, 13].map((fontSize) => {
                    root.style.setProperty('--table-font-size', fontSize + 'px');
                    const row = document.querySelector('.sku-separator-row');
                    const label = row ? row.querySelector('.sku-separator-label') : null;
                    const styles = label ? getComputedStyle(label) : null;
                    return {
                      fontSize,
                      rowHeight: row ? row.getBoundingClientRect().height : 0,
                      labelHeight: label ? label.getBoundingClientRect().height : 0,
                      labelFontSize: styles ? parseFloat(styles.fontSize) : 0,
                      lineHeight: styles ? parseFloat(styles.lineHeight) : 0,
                      paddingTop: styles ? parseFloat(styles.paddingTop) : 0,
                      paddingRight: styles ? parseFloat(styles.paddingRight) : 0,
                      paddingBottom: styles ? parseFloat(styles.paddingBottom) : 0,
                      paddingLeft: styles ? parseFloat(styles.paddingLeft) : 0,
                      position: styles ? styles.position : '',
                      overflow: styles ? styles.overflow : '',
                      textOverflow: styles ? styles.textOverflow : '',
                      whiteSpace: styles ? styles.whiteSpace : '',
                      tagName: label ? label.tagName : '',
                      title: label ? (label.getAttribute('title') || '') : ''
                    };
                  });
                  if (original) {
                    root.style.setProperty('--table-font-size', original);
                  } else {
                    root.style.removeProperty('--table-font-size');
                  }
                  return samples;
                }"""
            )
            for sample in layout_samples:
                if (
                    abs(float(sample["labelFontSize"]) - 2 * float(sample["fontSize"])) > 0.01
                    or float(sample["paddingTop"]) <= 0
                    or float(sample["paddingRight"]) <= 0
                    or float(sample["paddingBottom"]) <= 0
                    or float(sample["paddingLeft"]) <= 0
                    or float(sample["rowHeight"]) <= float(sample["lineHeight"])
                    or float(sample["labelHeight"]) <= float(sample["lineHeight"])
                    or sample["position"] != "sticky"
                    or sample["overflow"] != "hidden"
                    or sample["textOverflow"] != "ellipsis"
                    or sample["whiteSpace"] != "nowrap"
                    or sample["tagName"] != "BUTTON"
                    or not str(sample["title"]).startswith("Открыть управление SKU:")
                ):
                    raise AssertionError(
                        "SKU separator must keep typography/click semantics and gain bounded padding "
                        f"at every supported table font size, got {sample}"
                    )
            separator_button = page.locator(".sku-separator-label[data-open-vitrina-sku]").first
            separator_button.click()
            page.wait_for_selector("[data-sku-management-modal]:not([hidden])", timeout=5000)
            click_state = page.evaluate(
                """() => {
                  const modal = document.querySelector('[data-sku-management-modal]');
                  return {
                    modalVisible: !!modal && !modal.hidden,
                    modalText: modal ? (modal.textContent || '') : ''
                  };
                }"""
            )
            if not click_state["modalVisible"] or "clean iPhone 14" not in click_state["modalText"]:
                raise AssertionError(f"SKU separator click must keep opening its SKU modal, got {click_state}")
            page.keyboard.press("Escape")
            page.wait_for_selector("[data-sku-management-modal]", state="hidden", timeout=5000)
            if console_errors or page_errors or failed_responses:
                raise AssertionError(
                    "separator page must render without fatal browser errors: "
                    f"console={console_errors}, page={page_errors}, responses={failed_responses}"
                )
            context.close()
            browser.close()
    print(
        {
            "status": "ok",
            "separator": result,
            "layout_samples": layout_samples,
            "click": {"modal_visible": True, "sku": "clean iPhone 14"},
        }
    )


if __name__ == "__main__":
    main()
