"""Read-only production UI proof for retired and preserved Vitrina metrics."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
    DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
)
from packages.application.sheet_vitrina_v1_archived_metrics import (  # noqa: E402
    LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS,
)
from packages.application.sheet_vitrina_v1_incident_stocks import (  # noqa: E402
    INCIDENT_STOCK_FACT_METRIC_KEYS,
    INCIDENT_STOCK_FIELDS,
    incident_stock_metric_key,
    incident_stock_total_metric_key,
)
from packages.application.sheet_vitrina_v1_our_wb_costs import (  # noqa: E402
    OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
    OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
    OUR_WB_UNIT_COST_RUB_METRIC_KEY,
)
from packages.application.sheet_vitrina_v1_own_product_capital import (  # noqa: E402
    OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
)

RETIRED_KEYS = frozenset(
    (*INCIDENT_STOCK_FACT_METRIC_KEYS, *LEGACY_COST_PROXY_1_ARCHIVED_METRIC_KEYS)
)
PRESERVED_INCIDENT_KEYS = frozenset(
    metric_key
    for variant in ("incident", "effective")
    for region, _source, _suffix in INCIDENT_STOCK_FIELDS
    for metric_key in (
        incident_stock_metric_key(variant, region),
        incident_stock_total_metric_key(variant, region),
    )
)
PRESERVED_CANONICAL_KEYS = frozenset(
    {
        "stock_total",
        "total_stock_total",
        OUR_WB_UNIT_COST_RUB_METRIC_KEY,
        OUR_WB_PROXY_PROFIT_3_RUB_METRIC_KEY,
        OUR_WB_PROXY_MARGIN_3_PCT_METRIC_KEY,
        OWN_TOTAL_CAPITAL_RUB_METRIC_KEY,
    }
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--target-file",
        help=(
            "Optional canonical hosted-runtime target used to create a "
            "short-lived app-session cookie without printing it."
        ),
    )
    parser.add_argument("--ignore-https-errors", action="store_true")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    auth_cookie: str | None = None
    if args.target_file:
        from apps.registry_upload_http_entrypoint_hosted_runtime import (
            _build_probe_auth_cookie,
            _ensure_active_hosted_runtime_target,
            load_hosted_runtime_target,
        )

        target = load_hosted_runtime_target(Path(args.target_file).resolve())
        _ensure_active_hosted_runtime_target(
            target,
            action="metric-retirement-production-ui-flow",
        )
        if str(target.public_base_url or "").rstrip("/") != base_url:
            raise ValueError(
                "production UI base URL must match the canonical hosted-runtime target"
            )
        auth_cookie = _build_probe_auth_cookie(target, timeout_seconds=30.0)
        if not auth_cookie:
            raise RuntimeError(
                "production metric-retirement UI flow requires safely available "
                "app-session auth"
            )

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = run_flow(
        base_url=base_url,
        output_dir=output_dir,
        ignore_https_errors=bool(args.ignore_https_errors),
        auth_cookie=auth_cookie,
    )
    evidence_path = output_dir / "metric-retirement-ui-evidence.json"
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    evidence_path.write_bytes(encoded)
    print(
        json.dumps(
            {
                **result,
                "evidence_path": str(evidence_path),
                "evidence_sha256": hashlib.sha256(encoded).hexdigest(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def run_flow(
    *,
    base_url: str,
    output_dir: Path,
    ignore_https_errors: bool,
    auth_cookie: str | None = None,
) -> dict[str, Any]:
    requested_url = base_url + DEFAULT_SHEET_WEB_VITRINA_UI_PATH
    read_url = base_url + DEFAULT_SHEET_WEB_VITRINA_READ_PATH
    document_chain: list[dict[str, Any]] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[str] = []
    screenshot_path = output_dir / "metric-retirement-production.png"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(ignore_https_errors=ignore_https_errors)
        _add_auth_cookie(
            context,
            base_url=base_url,
            auth_cookie=auth_cookie,
        )
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on(
            "response",
            lambda response: (
                server_errors.append(f"{response.status} {response.url}")
                if response.status >= 500
                else None
            ),
        )
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=30000)
        if response is None:
            raise AssertionError("document navigation returned no response")
        request = response.request
        redirects = []
        redirected_from = request.redirected_from
        while redirected_from is not None:
            redirects.append(redirected_from.url)
            redirected_from = redirected_from.redirected_from
        document_chain.extend(
            {"url": url, "redirect": True}
            for url in reversed(redirects)
        )
        document_chain.append(
            {"url": response.url, "status": response.status, "redirect": False}
        )
        if response.status >= 500:
            raise AssertionError(f"document response failed: {response.status}")

        page.wait_for_selector("[data-table-shell]:not(.is-hidden)", timeout=30000)
        page.wait_for_selector("[data-table-body] tr", timeout=30000)
        title = page.title().strip()
        body_text = page.locator("body").inner_text().strip()
        if not title or not body_text:
            raise AssertionError("production page must have a non-empty title and body")

        table_keys = set(
            page.locator("[data-table-body] [data-metric-key]").evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('data-metric-key') || '').filter(Boolean)"
            )
        )
        leaked_table = sorted(table_keys & RETIRED_KEYS)
        if leaked_table:
            raise AssertionError(f"retired metrics leaked into table: {leaked_table}")

        page.locator("[data-metrics-settings-open]").click()
        page.wait_for_selector("[data-metrics-presentation]:not([hidden])", timeout=10000)
        page.wait_for_selector("[data-metric-config-row]", timeout=10000)
        settings_keys = set(
            page.locator("[data-metric-config-row]").evaluate_all(
                """nodes => nodes.flatMap(node => [
                  node.getAttribute('data-total-metric-key') || '',
                  node.getAttribute('data-sku-metric-key') || ''
                ]).filter(Boolean)"""
            )
        )
        leaked_settings = sorted(settings_keys & RETIRED_KEYS)
        if leaked_settings:
            raise AssertionError(
                f"retired metrics leaked into settings: {leaked_settings}"
            )

        page.locator("[data-metrics-settings-close]").first.click()
        filters_toggle = page.locator("[data-filters-toggle]")
        filters_toggle.click()
        page.locator("[data-sku-metric-toggle]").click()
        page.wait_for_selector("[data-sku-metric-option]", timeout=10000)
        picker_keys = set(
            page.locator("[data-sku-metric-option]").evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('data-sku-metric-option') || '').filter(Boolean)"
            )
        )
        leaked_picker = sorted(picker_keys & RETIRED_KEYS)
        if leaked_picker:
            raise AssertionError(f"retired metrics leaked into picker: {leaked_picker}")

        missing_incident = sorted(PRESERVED_INCIDENT_KEYS - settings_keys)
        if missing_incident:
            raise AssertionError(
                f"incident/effective metrics disappeared from settings: {missing_incident}"
            )
        missing_canonical = sorted(PRESERVED_CANONICAL_KEYS - settings_keys)
        if missing_canonical:
            raise AssertionError(
                f"canonical stock/cost/capital metrics disappeared: {missing_canonical}"
            )

        read_response = context.request.get(read_url)
        if read_response.status >= 500:
            raise AssertionError(
                f"public read contract returned {read_response.status}"
            )
        read_payload = read_response.json()
        public_metric_keys = _collect_metric_keys(read_payload)
        leaked_read = sorted(public_metric_keys & RETIRED_KEYS)
        if leaked_read:
            raise AssertionError(
                f"retired metrics leaked into public read/catalog: {leaked_read}"
            )

        page.screenshot(path=str(screenshot_path), full_page=True)
        final_url = page.url
        browser.close()

    if page_errors or server_errors:
        raise AssertionError(
            f"fatal browser evidence: page_errors={page_errors}, server_errors={server_errors}"
        )
    return {
        "status": "ok",
        "requested_url": requested_url,
        "final_url": final_url,
        "document_chain": document_chain,
        "title": title,
        "body_nonempty": bool(body_text),
        "page_errors": page_errors,
        "console_errors": console_errors,
        "server_errors": server_errors,
        "auth": {
            "mode": "app_session_cookie" if auth_cookie else "none",
            "cookie_configured": bool(auth_cookie),
        },
        "retired_keys_absent_from": ["public_read", "table", "settings", "picker"],
        "preserved_incident_key_count": len(PRESERVED_INCIDENT_KEYS),
        "preserved_canonical_keys": sorted(PRESERVED_CANONICAL_KEYS),
        "screenshot_path": str(screenshot_path),
        "screenshot_sha256": hashlib.sha256(screenshot_path.read_bytes()).hexdigest(),
    }


def _add_auth_cookie(
    context: Any,
    *,
    base_url: str,
    auth_cookie: str | None,
) -> None:
    if not auth_cookie:
        return
    cookie_name, separator, cookie_value = auth_cookie.partition("=")
    if separator != "=" or cookie_name != "wb_core_web_session" or not cookie_value:
        raise ValueError("invalid app-session cookie supplied to metric-retirement UI flow")
    context.add_cookies(
        [
            {
                "name": cookie_name,
                "value": cookie_value,
                "url": base_url,
                "httpOnly": True,
                "sameSite": "Lax",
            }
        ]
    )


def _collect_metric_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        metric_key = value.get("metric_key")
        if isinstance(metric_key, str) and metric_key:
            keys.add(metric_key)
        for nested in value.values():
            keys.update(_collect_metric_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.update(_collect_metric_keys(nested))
    return keys


if __name__ == "__main__":
    main()
