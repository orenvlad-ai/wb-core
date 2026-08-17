"""Browser smoke for server-side web-vitrina metrics presentation config."""

from __future__ import annotations

from datetime import timedelta
from dataclasses import replace
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
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _active_incident_metric_catalog,
    _sanitize_web_vitrina_metric_presentation_config,
)
from packages.application.sheet_vitrina_v1_archived_metrics import (  # noqa: E402
    LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (  # noqa: E402
    BUYOUT_PERCENT_METRIC_KEY,
    LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_incident_stocks import INCIDENT_STOCK_FACT_METRIC_KEYS  # noqa: E402
from packages.application.sheet_vitrina_v1_proxy_v4 import (  # noqa: E402
    PROXY_V4_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_PROFIT_RUB_METRIC_KEY,
    PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY,
    PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock  # noqa: E402
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter  # noqa: E402
from packages.application.web_vitrina_page_composition import build_web_vitrina_page_composition  # noqa: E402
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model  # noqa: E402

STORAGE_KEY = "wb-core:sheet-vitrina-v1:web-vitrina:page-state:v1:metric-presentation:v1"
BUYOUT_LOGICAL_METRIC_ID = (
    f"pair::{BUYOUT_PERCENT_METRIC_KEY}::{BUYOUT_PERCENT_METRIC_KEY}"
)
PROXY_V4_LOGICAL_METRIC_IDS = (
    f"pair::{PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY}::{PROXY_V4_PROFIT_RUB_METRIC_KEY}",
    f"pair::{PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY}::{PROXY_V4_MARGIN_PCT_METRIC_KEY}",
)
TOTAL_ORDER_SUM_SELECTOR = (
    '[data-metric-config-row][data-total-metric-key="total_orderSum"] '
    "[data-metric-display-select]"
)
RETIRED_METRIC_KEYS = frozenset(
    (*INCIDENT_STOCK_FACT_METRIC_KEYS, *LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS)
)


def main() -> None:
    _check_server_config_sanitizer()
    with TemporaryDirectory(prefix="web-vitrina-user-config-browser-") as tmp:
        composition = _build_composition(Path(tmp) / "runtime")
        with FixtureServer(composition) as server:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    _run_buyout_pair_checks(browser, server)
                    server.reset_user_config()
                    _run_checks(browser, server)
                finally:
                    browser.close()
    print(
        {
            "status": "ok",
            "checks": [
                "v3_v4_sanitizer",
                "buyout_total_sku_logical_pair",
                "buyout_saved_preset_compatibility",
                "buyout_shared_display_control",
                "local_migration_total_basis",
                "local_migration_scope_only_preserved",
                "local_migration_related_preferences_preserved",
                "local_migration_idempotent",
                "server_priority",
                "retired_metric_sanitation",
                "reload",
                "preset_crud",
                "manual_selection_isolation",
                "hidden_stale_member",
                "sku_highlight_blue_yellow",
                "sku_highlight_reload_reset",
                "sku_highlight_stale_sanitation",
                "modal_search_and_layout",
                "cleared_local_storage",
            ],
        }
    )


def _check_server_config_sanitizer() -> None:
    sanitized = _sanitize_web_vitrina_metric_presentation_config(
        {
            "version": 3,
            "scopes": {
                "sku": {
                    "order": ["wb_stock_incident_qty", "wb_stock_effective_qty"],
                    "display": {
                        "wb_stock_incident_qty": "shown",
                        "wb_stock_effective_qty": "collapsed",
                    },
                    "manual": True,
                }
            },
            "sku_presets": [
                {
                    "preset_id": "focus",
                    "name": "Фокус",
                    "metric_keys": ["wb_stock_incident_qty", "stale_metric"],
                }
            ],
            "sku_metric_selection": {
                "mode": "preset",
                "preset_id": "focus",
                "all": False,
                "metric_keys": [],
            },
            "sku_highlight_metric_keys": [
                "wb_stock_incident_qty",
                "wb_stock_incident_qty",
                "",
                "wb_stock_effective_qty",
                "x" * 161,
            ],
            "migrations": {
                "incident_effective_shown_v1": True,
                "sku_presets_seeded_v1": True,
            },
        }
    )
    if (
        sanitized.get("version") != 3
        or sanitized["scopes"]["sku"]["display"].get("wb_stock_incident_qty") != "shown"
        or sanitized.get("sku_presets", [{}])[0].get("metric_keys")
        != ["wb_stock_incident_qty", "stale_metric"]
        or (sanitized.get("sku_metric_selection") or {}).get("preset_id") != "focus"
        or sanitized.get("sku_highlight_metric_keys")
        != ["wb_stock_incident_qty", "wb_stock_effective_qty"]
        or not sanitized.get("migrations", {}).get("incident_effective_shown_v1")
    ):
        raise AssertionError(f"server sanitizer must preserve v3 statuses/presets/selection, got {sanitized}")

    legacy = _sanitize_web_vitrina_metric_presentation_config(
        {
            "version": 2,
            "scopes": {
                "sku": {
                    "order": ["wb_stock_incident_qty"],
                    "display": {"wb_stock_incident_qty": "collapsed"},
                    "manual": True,
                }
            },
        }
    )
    if (
        legacy.get("version") != 3
        or legacy.get("migrations", {}).get("incident_effective_shown_v1")
        or legacy.get("migrations", {}).get("sku_presets_seeded_v1")
        or legacy["scopes"]["sku"]["display"].get("wb_stock_incident_qty") != "collapsed"
    ):
        raise AssertionError(f"legacy config must retain narrow client migration evidence, got {legacy}")

    unified = _sanitize_web_vitrina_metric_presentation_config(
        {
            "version": 4,
            "presentation": {
                "order": [
                    "pair::total_orderSum::orderSum",
                    "pair::total_orderSum::orderSum",
                    "",
                    "x" * 401,
                    "sku::ctr",
                ],
                "display": {
                    "pair::total_orderSum::orderSum": "hidden",
                    "sku::ctr": "collapsed",
                    "sku::bad": "invalid",
                },
                "manual": True,
            },
            "expanded_anchors": ["pair::total_orderSum::orderSum"],
            "migrations": {
                "incident_effective_shown_v1": True,
                "sku_presets_seeded_v1": True,
                "unified_presentation_v1": True,
                "inventory_planning_metric_keys_v1": [
                    "inventory_wb_total_qty_v1",
                    "inventory_wb_total_qty_v1",
                    "x" * 161,
                ],
            },
        }
    )
    if (
        unified.get("version") != 4
        or unified.get("presentation", {}).get("order")
        != ["pair::total_orderSum::orderSum", "sku::ctr"]
        or unified.get("presentation", {}).get("display")
        != {
            "pair::total_orderSum::orderSum": "hidden",
            "sku::ctr": "collapsed",
        }
        or not unified.get("presentation", {}).get("manual")
        or not unified.get("migrations", {}).get("unified_presentation_v1")
        or unified.get("migrations", {}).get("inventory_planning_metric_keys_v1")
        != ["inventory_wb_total_qty_v1"]
    ):
        raise AssertionError(f"server sanitizer must preserve bounded v4 unified config, got {unified}")


def _run_buyout_pair_checks(browser, server: "FixtureServer") -> None:
    server.user_config = {
        "status": "ok",
        "revision": 1,
        "updated_at": "2026-06-01T10:00:00Z",
        "config": {
            "version": 4,
            "presentation": {
                "order": [BUYOUT_LOGICAL_METRIC_ID],
                "display": {BUYOUT_LOGICAL_METRIC_ID: "shown"},
                "manual": True,
            },
            "expanded_anchors": [],
            "sku_presets": [
                {
                    "preset_id": "buyout",
                    "name": "Выкуп",
                    "metric_keys": [BUYOUT_PERCENT_METRIC_KEY],
                }
            ],
            "sku_highlight_metric_keys": [],
            "sku_metric_selection": {
                "mode": "preset",
                "preset_id": "buyout",
                "all": False,
                "metric_keys": [],
            },
            "migrations": {
                "incident_effective_shown_v1": True,
                "sku_presets_seeded_v1": True,
                "unified_presentation_v1": True,
            },
        },
    }
    server.save_count = 0
    context = browser.new_context()
    page = context.new_page()
    page.goto(server.base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH, wait_until="domcontentloaded")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)

    total_selector = (
        f'tr[data-row-kind="total"] td[data-col-id="metric_label"]'
        f'[data-metric-key="{BUYOUT_PERCENT_METRIC_KEY}"]'
    )
    sku_selector = (
        f'tr[data-row-kind="sku"] td[data-col-id="metric_label"]'
        f'[data-metric-key="{BUYOUT_PERCENT_METRIC_KEY}"]'
    )
    if page.locator(total_selector).count() != 1 or page.locator(sku_selector).count() < 2:
        raise AssertionError("buyoutPercent must render one TOTAL row and current SKU rows")
    immature_cells = page.locator(
        f'td[data-metric-key="{BUYOUT_PERCENT_METRIC_KEY}"][data-cell-date]:not([data-cell-date=""])'
    ).all_inner_texts()
    if not immature_cells or any(value.strip() != "—" for value in immature_cells):
        raise AssertionError(
            f"immature buyoutPercent SKU/TOTAL browser cells must render dashes, got {immature_cells}"
        )
    if page.locator(f'[data-metric-key="{LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY}"]').count():
        raise AssertionError("legacy avg_buyoutPercent must not render")
    if page.locator("[data-sku-metric-summary]").inner_text().strip() != "SKU-метрики: Выкуп":
        raise AssertionError("saved buyout preset must survive unified pair reconciliation")

    _open_sku_metric_picker(page)
    if page.locator(f'[data-sku-metric-option="{BUYOUT_PERCENT_METRIC_KEY}"]').count() != 1:
        raise AssertionError("SKU picker must contain exactly one logical buyoutPercent option")
    if page.locator(f'[data-sku-metric-option="{LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY}"]').count():
        raise AssertionError("legacy TOTAL average must not duplicate the SKU picker item")
    for metric_key in (PROXY_V4_PROFIT_RUB_METRIC_KEY, PROXY_V4_MARGIN_PCT_METRIC_KEY):
        if page.locator(f'[data-sku-metric-option="{metric_key}"]').count() != 1:
            raise AssertionError(f"SKU picker must contain one V4 metric item: {metric_key}")

    _open_metrics(page)
    pair_row = page.locator(
        f'[data-metric-config-row="{BUYOUT_LOGICAL_METRIC_ID}"]'
        f'[data-total-metric-key="{BUYOUT_PERCENT_METRIC_KEY}"]'
        f'[data-sku-metric-key="{BUYOUT_PERCENT_METRIC_KEY}"]'
    )
    if (
        pair_row.count() != 1
        or pair_row.get_attribute("data-metric-availability") != "common"
        or pair_row.locator(".metrics-config-label").inner_text().strip() != "Процент выкупа"
        or pair_row.locator(".metrics-config-scope-badge").count()
    ):
        raise AssertionError("TOTAL/SKU buyoutPercent must be one common logical row without scope badge")
    if page.locator(
        f'[data-total-metric-key="{LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY}"],'
        f'[data-sku-metric-key="{LEGACY_AVG_BUYOUT_PERCENT_METRIC_KEY}"]'
    ).count():
        raise AssertionError("legacy avg_buyoutPercent must not enter the metrics modal")
    for logical_id in PROXY_V4_LOGICAL_METRIC_IDS:
        v4_pair = page.locator(f'[data-metric-config-row="{logical_id}"]')
        if (
            v4_pair.count() != 1
            or v4_pair.get_attribute("data-metric-availability") != "common"
            or v4_pair.locator(".metrics-config-scope-badge").count()
        ):
            raise AssertionError(f"V4 SKU/TOTAL metrics must be one common logical item: {logical_id}")

    display_select = pair_row.locator("[data-metric-display-select]")
    save_before = server.save_count
    display_select.select_option("hidden")
    _wait_for_server_save_count(server, save_before + 1)
    page.wait_for_function(
        """({totalSelector, skuSelector}) => (
          document.querySelectorAll(totalSelector).length === 0
          && document.querySelectorAll(skuSelector).length === 0
        )""",
        arg={"totalSelector": total_selector, "skuSelector": sku_selector},
        timeout=5000,
    )
    saved = server.user_config["config"]
    if (
        saved.get("presentation", {}).get("order", []).count(BUYOUT_LOGICAL_METRIC_ID) != 1
        or saved.get("presentation", {}).get("display", {}).get(BUYOUT_LOGICAL_METRIC_ID)
        != "hidden"
        or saved.get("sku_presets", [{}])[0].get("metric_keys")
        != [BUYOUT_PERCENT_METRIC_KEY]
    ):
        raise AssertionError(f"paired display and preset membership must persist together, got {saved}")

    save_before = server.save_count
    display_select.select_option("shown")
    _wait_for_server_save_count(server, save_before + 1)
    page.wait_for_function(
        """({totalSelector, skuSelector}) => (
          document.querySelectorAll(totalSelector).length === 1
          && document.querySelectorAll(skuSelector).length >= 2
        )""",
        arg={"totalSelector": total_selector, "skuSelector": sku_selector},
        timeout=5000,
    )
    context.close()


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
                "order": [
                    "avg_price_seller_discounted",
                    "avg_addToCartConversion",
                    "wb_stock_fact_qty",
                    "cost_price_rub",
                    "proxy_profit_rub",
                ],
                "display": {
                    "avg_price_seller_discounted": "collapsed",
                    "avg_addToCartConversion": "hidden",
                    "wb_stock_fact_qty": "collapsed",
                    "cost_price_rub": "hidden",
                },
                "manual": True,
            },
        },
        "expanded_anchors": ["sku::wb_stock_fact_qty", "total::avg_cost_price_rub"],
        "sku_presets": [
            {
                "preset_id": "saved",
                "name": "Сохранённый",
                "metric_keys": ["avg_price_seller_discounted"],
            }
        ],
        "sku_highlight_metric_keys": ["avg_price_seller_discounted"],
        "sku_metric_selection": {
            "mode": "preset",
            "preset_id": "saved",
            "all": False,
            "metric_keys": [],
        },
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
    migrated_display = migrated.get("presentation", {}).get("display", {})
    migrated_total_order_sum = next(
        (
            status
            for logical_id, status in migrated_display.items()
            if "total_orderSum" in logical_id
        ),
        "",
    )
    if (
        migrated.get("version") != 4
        or migrated_total_order_sum != "hidden"
        or migrated.get("presentation", {}).get("order", [None])[0]
        != "total::total_orderSum"
        or migrated_display.get("sku::avg_price_seller_discounted") != "collapsed"
        or migrated_display.get("sku::avg_addToCartConversion") != "hidden"
        or not migrated.get("migrations", {}).get("incident_effective_shown_v1")
        or not migrated.get("migrations", {}).get("unified_presentation_v1")
        or [preset.get("name") for preset in migrated.get("sku_presets", [])] != ["Сохранённый"]
        or migrated.get("sku_highlight_metric_keys") != ["avg_price_seller_discounted"]
        or migrated.get("sku_metric_selection", {}).get("preset_id") != "saved"
    ):
        raise AssertionError(f"valid localStorage must migrate once when server config is missing, got {migrated}")
    save_count_after_migration = server.save_count
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    page.wait_for_timeout(700)
    if server.save_count != save_count_after_migration:
        raise AssertionError(
            "versioned unified migration must be idempotent after its first persisted save, "
            f"before={save_count_after_migration}, after={server.save_count}"
        )
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
    _wait_for_server_save_count(server, 1)
    if stale_page.locator(TOTAL_ORDER_SUM_SELECTOR).input_value() != "shown":
        raise AssertionError("stale localStorage must not hide total_orderSum when server config exists")
    if server.user_config["config"].get("version") != 4:
        raise AssertionError("server v2 metric config must migrate in place to v4")
    _assert_retired_metrics_absent(stale_page, server.user_config["config"])

    stale_page.select_option(TOTAL_ORDER_SUM_SELECTOR, "hidden")
    _wait_for_server_save_count(server, 2)
    if server.user_config["revision"] != 6:
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
    _check_sku_metric_presets(clear_page, server)
    clear_context.close()


def _assert_retired_metrics_absent(page, config: object | None = None) -> None:
    for metric_key in RETIRED_METRIC_KEYS:
        selector = (
            f'[data-total-metric-key="{metric_key}"],'
            f'[data-sku-metric-key="{metric_key}"]'
        )
        if page.locator(selector).count():
            raise AssertionError(f"retired metric leaked into settings/picker: {metric_key}")
    if config is None:
        return
    serialized = json.dumps(config, ensure_ascii=False, sort_keys=True)
    leaked = sorted(metric_key for metric_key in RETIRED_METRIC_KEYS if metric_key in serialized)
    if leaked:
        raise AssertionError(f"retired metric keys survived saved-state sanitation: {leaked}")


def _open_metrics(page) -> None:
    modal = page.locator("[data-metrics-presentation]")
    if modal.get_attribute("hidden") is not None:
        page.locator("[data-metrics-settings-open]").click()
    page.wait_for_selector("[data-metrics-presentation]:not([hidden])", timeout=5000)


def _wait_for_server_save_count(server: "FixtureServer", expected: int) -> None:
    import time

    deadline = time.time() + 5
    while time.time() < deadline:
        if server.save_count >= expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"user config save was not observed, expected {expected}, got {server.save_count}")


def _open_sku_metric_picker(page) -> None:
    if page.locator("[data-metrics-presentation]").get_attribute("hidden") is None:
        page.locator("[data-metrics-settings-close]").first.click()
    if page.locator("[data-filters-rail]").get_attribute("hidden") is not None:
        page.locator("[data-filters-toggle]").click()
    if page.locator("[data-sku-metric-panel]").get_attribute("hidden") is not None:
        page.locator("[data-sku-metric-toggle]").click()
    page.wait_for_selector("[data-sku-metric-panel]:not([hidden])", timeout=5000)


def _check_sku_metric_presets(page, server: "FixtureServer") -> None:
    _open_sku_metric_picker(page)
    mode_labels = page.locator("[data-sku-metric-mode]").all_inner_texts()
    if not any(label.startswith("Ручной выбор") for label in mode_labels) or not any(
        label.startswith("Анализ") for label in mode_labels
    ):
        raise AssertionError(f"picker must expose manual mode and the initial Анализ preset, got {mode_labels}")
    _check_sku_metric_highlights(page, server)

    page.locator("[data-sku-preset-configure]").click()
    page.wait_for_selector("[data-sku-preset-modal]:not([hidden])", timeout=5000)
    modal_layout = page.evaluate(
        """() => {
          const backdrop = document.querySelector('[data-sku-preset-modal]');
          const card = backdrop && backdrop.querySelector('.sku-preset-modal');
          const members = backdrop && backdrop.querySelector('[data-sku-preset-members]');
          const rect = card ? card.getBoundingClientRect() : null;
          const styles = card ? getComputedStyle(card) : null;
          const memberStyles = members ? getComputedStyle(members) : null;
          return {
            cardVisible: !!(rect && rect.width > 300 && rect.height > 300),
            insideViewport: !!(
              rect
              && rect.left >= 0
              && rect.top >= 0
              && rect.right <= window.innerWidth
              && rect.bottom <= window.innerHeight
            ),
            darkBackground: !!(
              styles
              && /^rgb\\((\\d+), (\\d+), (\\d+)\\)$/.test(styles.backgroundColor)
              && styles.backgroundColor !== 'rgb(255, 255, 255)'
            ),
            memberViewportHeight: members ? members.clientHeight : 0,
            memberScrollHeight: members ? members.scrollHeight : 0,
            memberOverflowY: memberStyles ? memberStyles.overflowY : '',
            groupCount: backdrop ? backdrop.querySelectorAll('.sku-preset-group').length : 0
          };
        }"""
    )
    if (
        not modal_layout["cardVisible"]
        or not modal_layout["insideViewport"]
        or not modal_layout["darkBackground"]
        or int(modal_layout["memberViewportHeight"]) < 180
        or int(modal_layout["memberScrollHeight"]) < int(modal_layout["memberViewportHeight"])
        or modal_layout["memberOverflowY"] not in {"auto", "scroll"}
        or int(modal_layout["groupCount"]) < 2
    ):
        raise AssertionError(f"preset modal and long grouped list must remain usable in dark UI, got {modal_layout}")
    original_member_count = page.locator("[data-sku-preset-member]").count()
    page.locator("[data-sku-preset-search]").fill("__metric_not_found__")
    if "Метрики не найдены" not in page.locator("[data-sku-preset-members]").inner_text():
        raise AssertionError("preset metric search must show a truthful no-match state")
    page.locator("[data-sku-preset-search]").fill("")
    if page.locator("[data-sku-preset-member]").count() != original_member_count:
        raise AssertionError("clearing preset metric search must restore the full non-hidden list")
    members = page.locator("[data-sku-preset-member]")
    if members.count() < 2:
        raise AssertionError("preset editor requires at least two non-hidden SKU metrics")
    for index in range(1, members.count()):
        members.nth(index).uncheck()
    page.locator("[data-sku-preset-name]").fill("Фокус")
    save_before = server.save_count
    page.locator("[data-sku-preset-save]").click()
    page.wait_for_function(
        "() => !!document.querySelector('[data-sku-preset-modal]').hidden",
        timeout=5000,
    )
    _wait_for_server_save_count(server, save_before + 1)
    preset_payload = server.user_config["config"]
    focus_presets = [
        preset for preset in preset_payload.get("sku_presets", []) if preset.get("name") == "Фокус"
    ]
    if len(focus_presets) != 1 or len(focus_presets[0].get("metric_keys", [])) != 1:
        raise AssertionError(f"renamed preset must persist its explicit metric set, got {preset_payload}")
    focus_preset = focus_presets[0]
    focus_preset_id = str(focus_preset["preset_id"])
    focus_metric_key = str(focus_preset["metric_keys"][0])
    if page.locator("[data-sku-metric-summary]").inner_text().strip() != "SKU-метрики: Фокус":
        raise AssertionError("saved preset must apply immediately")

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    if page.locator("[data-sku-metric-summary]").inner_text().strip() != "SKU-метрики: Фокус":
        raise AssertionError("last selected preset must survive reload")

    _open_sku_metric_picker(page)
    manual_target = page.locator(
        f'[data-sku-metric-option]:not([data-sku-metric-option="{focus_metric_key}"])'
    ).first
    manual_target.click()
    save_before = server.save_count
    page.locator("[data-sku-metric-apply]").click()
    _wait_for_server_save_count(server, save_before + 1)
    after_manual = server.user_config["config"]
    persisted_focus = next(
        preset for preset in after_manual.get("sku_presets", []) if preset.get("preset_id") == focus_preset_id
    )
    if persisted_focus.get("metric_keys") != [focus_metric_key]:
        raise AssertionError("manual picker correction must not overwrite a saved preset")
    if (after_manual.get("sku_metric_selection") or {}).get("mode") != "manual":
        raise AssertionError("manual correction must persist as the last manual selection")

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    if page.locator("[data-sku-metric-summary]").inner_text().strip() == "SKU-метрики: Фокус":
        raise AssertionError("manual last selection must remain distinct from the saved preset")

    _open_sku_metric_picker(page)
    page.locator("[data-sku-preset-configure]").click()
    page.wait_for_selector("[data-sku-preset-modal]:not([hidden])", timeout=5000)
    page.locator("[data-sku-preset-new]").click()
    page.locator("[data-sku-preset-name]").fill("Временный")
    save_before = server.save_count
    page.locator("[data-sku-preset-save]").click()
    page.wait_for_function(
        "() => !!document.querySelector('[data-sku-preset-modal]').hidden",
        timeout=5000,
    )
    _wait_for_server_save_count(server, save_before + 1)
    if not any(
        preset.get("name") == "Временный"
        for preset in server.user_config["config"].get("sku_presets", [])
    ):
        raise AssertionError("new preset must persist")

    _open_sku_metric_picker(page)
    page.locator("[data-sku-preset-configure]").click()
    page.wait_for_selector("[data-sku-preset-modal]:not([hidden])", timeout=5000)
    save_before = server.save_count
    page.locator("[data-sku-preset-delete]").click()
    _wait_for_server_save_count(server, save_before + 1)
    if any(
        preset.get("name") == "Временный"
        for preset in server.user_config["config"].get("sku_presets", [])
    ):
        raise AssertionError("deleted preset must be removed from the registry")
    page.locator("[data-sku-preset-close]").first.click()

    _open_sku_metric_picker(page)
    focus_color = page.locator(f'[data-sku-metric-color="{focus_metric_key}"]')
    if focus_color.count() != 1 or focus_color.is_disabled():
        raise AssertionError("visible focus metric must expose an enabled color checkbox before hiding")
    focus_color.check()
    save_before = server.save_count
    page.locator("[data-sku-metric-apply]").click()
    _wait_for_server_save_count(server, save_before + 1)
    if server.user_config["config"].get("sku_highlight_metric_keys") != [focus_metric_key]:
        raise AssertionError("focus metric highlight must persist before stale sanitation")

    _open_metrics(page)
    sku_display_selector = (
        f'[data-metric-config-row][data-sku-metric-key="{focus_metric_key}"] '
        "[data-metric-display-select]"
    )
    save_before = server.save_count
    page.select_option(sku_display_selector, "hidden")
    _wait_for_server_save_count(server, save_before + 1)
    if focus_metric_key in server.user_config["config"].get("sku_highlight_metric_keys", []):
        raise AssertionError("hiding a metric must immediately remove its active invisible highlight")
    _open_sku_metric_picker(page)
    if page.locator(f'[data-sku-metric-option="{focus_metric_key}"]').count() != 0:
        raise AssertionError("a hidden metric must be excluded from the picker")
    page.locator(f'[data-sku-metric-mode="preset"][data-sku-metric-preset-id="{focus_preset_id}"]').click()
    save_before = server.save_count
    page.locator("[data-sku-metric-apply]").click()
    _wait_for_server_save_count(server, save_before + 1)
    stale_safe = server.user_config["config"]
    stale_focus = next(
        preset for preset in stale_safe.get("sku_presets", []) if preset.get("preset_id") == focus_preset_id
    )
    if stale_focus.get("metric_keys") != [focus_metric_key]:
        raise AssertionError("hiding a metric must not rewrite saved preset membership")
    server.user_config["config"]["sku_highlight_metric_keys"] = [focus_metric_key, "stale_metric"]
    save_before = server.save_count
    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    _wait_for_server_save_count(server, save_before + 1)
    if server.user_config["config"].get("sku_highlight_metric_keys"):
        raise AssertionError(
            "reload sanitation must remove hidden and unavailable highlight keys from server config"
        )
    if page.locator("[data-sku-metric-summary]").inner_text().strip() != "SKU-метрики: Фокус":
        raise AssertionError("preset identity must survive reload when a stale member becomes hidden")


def _check_sku_metric_highlights(page, server: "FixtureServer") -> None:
    metric_keys = page.evaluate(
        """() => {
          const optionKeys = new Set(Array.from(document.querySelectorAll('[data-sku-metric-option]'))
            .map((node) => node.getAttribute('data-sku-metric-option') || '')
            .filter(Boolean));
          return Array.from(new Set(Array.from(document.querySelectorAll(
            '[data-table-body] tr[data-row-kind="sku"] td[data-metric-key][data-cell-date]:not([data-cell-date=""])'
          )).filter((node) => (
            (node.textContent || '').trim() !== '—'
            && (node.getAttribute('data-presentation-state') || '') !== 'unavailable'
          )).map((node) => node.getAttribute('data-metric-key') || '').filter((key) => optionKeys.has(key)))).slice(0, 2);
        }"""
    )
    if len(metric_keys) != 2:
        raise AssertionError(f"highlight smoke requires two rendered SKU metrics, got {metric_keys}")
    for metric_key in metric_keys:
        color_control = page.locator(f'[data-sku-metric-color="{metric_key}"]')
        if color_control.count() != 1 or color_control.is_disabled():
            raise AssertionError(f"shown metric must expose one enabled color checkbox: {metric_key}")
        color_control.check()
    palette_indexes = [
        page.locator(f'[data-sku-metric-color="{metric_key}"]').get_attribute(
            "data-sku-highlight-palette-index"
        )
        for metric_key in metric_keys
    ]
    if palette_indexes != ["1", "2"]:
        raise AssertionError(f"first two highlights must deterministically use blue/yellow, got {palette_indexes}")

    save_before = server.save_count
    page.locator("[data-sku-metric-apply]").click()
    _wait_for_server_save_count(server, save_before + 1)
    if server.user_config["config"].get("sku_highlight_metric_keys") != metric_keys:
        raise AssertionError(
            f"ordered highlight metric keys must persist server-side, got {server.user_config}"
        )
    _assert_sku_metric_highlight_render(page, metric_keys)

    page.reload(wait_until="domcontentloaded")
    page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=20000)
    _assert_sku_metric_highlight_render(page, metric_keys)
    _open_sku_metric_picker(page)
    reloaded_indexes = [
        page.locator(f'[data-sku-metric-color="{metric_key}"]').get_attribute(
            "data-sku-highlight-palette-index"
        )
        for metric_key in metric_keys
    ]
    if reloaded_indexes != ["1", "2"] or any(
        not page.locator(f'[data-sku-metric-color="{metric_key}"]').is_checked()
        for metric_key in metric_keys
    ):
        raise AssertionError(
            f"highlight order/colors must survive reload, got indexes={reloaded_indexes}"
        )

    save_before = server.save_count
    page.locator("[data-sku-metric-reset]").click()
    _wait_for_server_save_count(server, save_before + 1)
    if server.user_config["config"].get("sku_highlight_metric_keys") != []:
        raise AssertionError("SKU metric reset must clear the color selection")
    if page.locator('[data-sku-metric-highlight-index]:not([data-sku-metric-highlight-index=""])').count():
        raise AssertionError("SKU metric reset must remove all rendered user highlights")


def _assert_sku_metric_highlight_render(page, metric_keys: list[str]) -> None:
    result = page.evaluate(
        """(metricKeys) => {
          const metrics = metricKeys.map((metricKey, index) => {
            const temporal = Array.from(document.querySelectorAll(
              'tr[data-row-kind="sku"] td[data-metric-key="' + CSS.escape(metricKey) + '"][data-cell-date]:not([data-cell-date=""])'
            )).filter((node) => (
              (node.getAttribute('data-presentation-state') || '') !== 'unavailable'
              && (node.textContent || '').trim() !== '—'
            ));
            const labels = Array.from(document.querySelectorAll(
              'tr[data-row-kind="sku"] td[data-col-id="metric_label"][data-metric-key="' + CSS.escape(metricKey) + '"]'
            ));
            const expectedIndex = String(index + 1);
            const sample = temporal[0] || labels[0] || null;
            return {
              metricKey,
              temporalCount: temporal.length,
              labelCount: labels.length,
              allTemporalHighlighted: temporal.every((node) => (
                node.getAttribute('data-sku-metric-highlight-index') === expectedIndex
              )),
              allLabelsHighlighted: labels.every((node) => (
                node.getAttribute('data-sku-metric-highlight-index') === expectedIndex
              )),
              background: sample ? getComputedStyle(sample).backgroundColor : '',
              foreground: sample ? getComputedStyle(sample).color : '',
              paletteColor: sample
                ? getComputedStyle(sample).getPropertyValue('--sku-highlight-color').trim()
                : ''
            };
          });
          return {
            metrics,
            totalHighlightCount: document.querySelectorAll(
              'tr[data-row-kind="total"] [data-sku-metric-highlight-index]:not([data-sku-metric-highlight-index=""])'
            ).length,
            unavailableHighlightCount: document.querySelectorAll(
              '[data-presentation-state="unavailable"][data-sku-metric-highlight-index]:not([data-sku-metric-highlight-index=""])'
            ).length
          };
        }""",
        metric_keys,
    )
    if result["totalHighlightCount"] != 0 or result["unavailableHighlightCount"] != 0:
        raise AssertionError(f"highlight must exclude TOTAL and unavailable cells, got {result}")
    if any(
        not metric["temporalCount"]
        or not metric["labelCount"]
        or not metric["allTemporalHighlighted"]
        or not metric["allLabelsHighlighted"]
        for metric in result["metrics"]
    ):
        raise AssertionError(f"highlight must cover every rendered SKU/date value and label, got {result}")
    backgrounds = [metric["background"] for metric in result["metrics"]]
    if not all(backgrounds) or len(set(backgrounds)) != 2:
        raise AssertionError(f"first two highlights must render as distinct blue/yellow backgrounds, got {result}")
    if [metric["paletteColor"] for metric in result["metrics"]] != ["#3b82f6", "#facc15"]:
        raise AssertionError(f"first two rendered palette colors must be blue/yellow, got {result}")
    if any(metric["foreground"] != "rgb(244, 244, 245)" for metric in result["metrics"]):
        raise AssertionError(f"highlighted values must remain readable in the dark table, got {result}")


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
        runtime.save_temporal_source_snapshot(
            source_key="sales_funnel_history",
            snapshot_date=snapshot_date,
            captured_at=f"{snapshot_date}T12:00:00Z",
            payload={
                "kind": "success",
                "date_from": snapshot_date,
                "date_to": snapshot_date,
                "count": 4,
                "items": [
                    {
                        "date": snapshot_date,
                        "nm_id": enabled[0].nm_id,
                        "metric": BUYOUT_PERCENT_METRIC_KEY,
                        "value": 0.5,
                    },
                    {
                        "date": snapshot_date,
                        "nm_id": enabled[0].nm_id,
                        "metric": "orderCount",
                        "value": 10,
                    },
                    {
                        "date": snapshot_date,
                        "nm_id": enabled[1].nm_id,
                        "metric": BUYOUT_PERCENT_METRIC_KEY,
                        "value": 0.9,
                    },
                    {
                        "date": snapshot_date,
                        "nm_id": enabled[1].nm_id,
                        "metric": "orderCount",
                        "value": 30,
                    },
                ],
            },
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=f"{snapshot_date}T12:05:00Z",
            plan=_with_proxy_v4_rows(
                _build_plan(
                    as_of_date=snapshot_date,
                    first_nm_id=enabled[0].nm_id,
                    second_nm_id=enabled[1].nm_id,
                    first_group=first_group,
                ),
                nm_ids=[enabled[0].nm_id, enabled[1].nm_id],
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


def _with_proxy_v4_rows(plan, *, nm_ids: list[int]):
    sheets = []
    for sheet in plan.sheets:
        if sheet.sheet_name != "DATA_VITRINA":
            sheets.append(sheet)
            continue
        blanks = [""] * len(plan.date_columns)
        rows = [
            *sheet.rows,
            ["Proxy прибыль 4", f"TOTAL|{PROXY_V4_TOTAL_PROFIT_RUB_METRIC_KEY}", *blanks],
            ["Прокси маржинальность 4", f"TOTAL|{PROXY_V4_TOTAL_MARGIN_PCT_METRIC_KEY}", *blanks],
        ]
        for nm_id in nm_ids:
            rows.extend(
                [
                    ["SKU: Proxy прибыль 4", f"SKU:{nm_id}|{PROXY_V4_PROFIT_RUB_METRIC_KEY}", *blanks],
                    ["SKU: Прокси маржинальность 4", f"SKU:{nm_id}|{PROXY_V4_MARGIN_PCT_METRIC_KEY}", *blanks],
                ]
            )
        sheets.append(replace(sheet, rows=rows, row_count=len(rows)))
    return replace(plan, sheets=sheets)


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

    def reset_user_config(self) -> None:
        self.user_config = {
            "status": "missing",
            "revision": 0,
            "updated_at": "",
            "config": None,
        }
        self.save_count = 0

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
                        "schema_version": 2 if server.user_config.get("status") == "ok" else 0,
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
                            "schema_version": 2,
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
