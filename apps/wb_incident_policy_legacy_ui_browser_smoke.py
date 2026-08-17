#!/usr/bin/env python3
"""Browser acceptance for the default-collapsed WB incident legacy disclosure."""

from __future__ import annotations

import json
from pathlib import Path
import sys

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import (  # noqa: E402
    LocalWebVitrinaFixtureServer,
)
from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
    DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH,
    DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH,
)


def main() -> int:
    fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=True)
    base_url = fixture.__enter__()
    try:
        requests: list[str] = []
        console_errors: list[str] = []
        page_errors: list[str] = []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 390, "height": 844},
                color_scheme="dark",
            )
            page = context.new_page()

            def settings_route(route):
                requests.append("settings")
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(_settings_payload(), ensure_ascii=False),
                )

            def options_route(route):
                requests.append("options")
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "ok",
                            "snapshot_date": "2026-08-17",
                            "pagination_complete": True,
                            "options": [],
                        },
                        ensure_ascii=False,
                    ),
                )

            page.route(f"**{DEFAULT_WB_WAREHOUSE_EXCLUSION_SETTINGS_PATH}", settings_route)
            page.route(f"**{DEFAULT_WB_WAREHOUSE_EXCLUSION_OPTIONS_PATH}", options_route)
            page.on(
                "console",
                lambda message: console_errors.append(message.text)
                if message.type == "error"
                else None,
            )
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.goto(
                base_url
                + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
                + "?tab=warehouses&warehouse=wb",
                wait_until="domcontentloaded",
            )
            legacy = page.locator("[data-wb-incident-legacy]")
            expect(legacy).to_be_visible(timeout=30000)
            expect(legacy).not_to_have_attribute("open", "")
            if requests:
                raise AssertionError(
                    f"legacy policy loaded before explicit disclosure: {requests}"
                )
            visible_text = page.locator("body").inner_text()
            for leaked in (
                "С инцидентами",
                "Остаток WB без инц.: всего",
                "Остаток без инц.: всего",
                "Полнота WB не подтверждена",
            ):
                if leaked in visible_text:
                    raise AssertionError(f"ordinary UI leaked incident presentation: {leaked}")

            summary = legacy.locator(":scope > summary")
            expect(summary).to_have_text("Legacy: инциденты на складах WB")
            summary.focus()
            if page.evaluate("() => document.activeElement?.textContent?.trim()") != "Legacy: инциденты на складах WB":
                raise AssertionError("legacy disclosure summary is not keyboard-focusable")
            summary.press("Enter")
            page.wait_for_function(
                "() => document.querySelector('[data-wb-incident-policy-badge]')?.textContent?.includes('Сейчас: не действует')"
            )
            if requests != ["settings", "options"]:
                raise AssertionError(f"explicit disclosure must load policy exactly once: {requests}")
            expect(legacy).to_have_attribute("open", "")
            expect(page.locator("[data-wb-incident-policy-badge]")).to_have_text(
                "Настроено: выключено · Сейчас: не действует"
            )

            page.locator("[data-wb-incident-drawer] > summary").click()
            option_text = page.locator("[data-wb-incident-options]").inner_text()
            for identity in (
                "Коледино",
                "ID 507",
                "Электросталь",
                "ID 117986",
                "историческая identity",
            ):
                if identity not in option_text:
                    raise AssertionError(
                        f"aggregate-only current snapshot lost legacy identity {identity!r}: {option_text}"
                    )

            history_details = page.locator("[data-wb-incident-history]").locator("xpath=..")
            history_details.locator(":scope > summary").click()
            history_text = page.locator("[data-wb-incident-history]").inner_text()
            for evidence in (
                "Revision 3 · disabled",
                "Revision 2 · enabled",
                "Revision 1 · enabled",
                "Коледино (ID 507, 2026-07-01)",
                "Электросталь (ID 117986, 2026-07-12)",
                "2026-08-16",
                "incident_policy_legacy_disable_v1",
                "actor: owner-disable",
                "created: 2026-08-17T08:00:00Z",
                "reason: Legacy mode",
            ):
                if evidence not in history_text:
                    raise AssertionError(
                        f"legacy history lost exact revision evidence {evidence!r}: {history_text}"
                    )
            responsive = page.evaluate(
                """() => ({
                  width: window.innerWidth,
                  dark: getComputedStyle(document.documentElement).colorScheme.includes('dark'),
                  summaryVisible: !!document.querySelector('[data-wb-incident-legacy] > summary')?.getClientRects().length,
                  bodyWidth: document.body.scrollWidth,
                  viewportWidth: document.documentElement.clientWidth
                })"""
            )
            if (
                responsive["width"] != 390
                or not responsive["dark"]
                or not responsive["summaryVisible"]
                or responsive["bodyWidth"] > responsive["viewportWidth"] + 2
            ):
                raise AssertionError(f"mobile/dark legacy disclosure failed: {responsive}")
            context.close()
            browser.close()
        if console_errors or page_errors:
            raise AssertionError(
                f"legacy disclosure browser errors: console={console_errors}, pageerror={page_errors}"
            )
    finally:
        fixture.__exit__(None, None, None)
    print("wb_incident_policy_legacy_ui_browser_smoke: OK")
    return 0


def _settings_payload() -> dict[str, object]:
    identities = [
        {
            "warehouse_id": 507,
            "warehouse_name": "Коледино",
            "effective_from": "2026-07-01",
            "effective_to_exclusive": "",
            "source": "incident_policy_v2",
        },
        {
            "warehouse_id": 117986,
            "warehouse_name": "Электросталь",
            "effective_from": "2026-07-12",
            "effective_to_exclusive": "",
            "source": "incident_policy_v2",
        },
    ]
    return {
        "status": "ok",
        "revision": 3,
        "effective_revision": 3,
        "active": False,
        "configured_active": False,
        "policy_status": "disabled",
        "reason": "Legacy mode",
        "actor": "owner",
        "created_at": "2026-08-17T08:00:00Z",
        "warehouse_entries": [],
        "legacy_warehouse_entries": identities,
        "excluded_wb_warehouse_ids": [],
        "revision_history": [
            {
                "revision": 3,
                "active": False,
                "warehouse_entries": identities,
                "warehouse_identities": [],
                "effective_from": "2026-08-16",
                "effective_to": "",
                "policy_status": "disabled",
                "actor": "owner-disable",
                "created_at": "2026-08-17T08:00:00Z",
                "reason": "Legacy mode",
                "source": "incident_policy_legacy_disable_v1",
            },
            {
                "revision": 2,
                "active": True,
                "warehouse_entries": identities,
                "warehouse_identities": [],
                "effective_from": "2026-07-12",
                "effective_to": "",
                "policy_status": "monitoring",
                "actor": "owner-monitoring",
                "created_at": "2026-07-12T08:00:00Z",
                "reason": "Historical monitoring",
                "source": "incident_policy_v2",
            },
            {
                "revision": 1,
                "active": True,
                "warehouse_entries": identities[:1],
                "warehouse_identities": [],
                "effective_from": "2026-07-01",
                "effective_to": "",
                "policy_status": "active",
                "actor": "owner-open",
                "created_at": "2026-07-01T08:00:00Z",
                "reason": "Historical incident",
                "source": "incident_policy_v2",
            },
        ],
    }


if __name__ == "__main__":
    raise SystemExit(main())
