#!/usr/bin/env python3
"""Read-only Playwright acceptance flow for the production warehouse UI."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import time
from typing import Any, Mapping
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import FrameLocator, Page, expect, sync_playwright


WAREHOUSES = (
    ("production", "На производстве"),
    ("china_to_ff", "Китай → FF"),
    ("ff", "Склад FF"),
    ("ff_to_wb", "FF → WB"),
    ("wb", "Склад WB"),
    ("wb_acceptance_discrepancy", "Расхождения приёмки WB"),
)
WAREHOUSE_UI_PATH = "/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=production"
WAREHOUSE_CHAIN_RECOVERY_PROFILE = "warehouse_chain_recovery_20260719"
WAREHOUSE_COST_TRANSPARENCY_PROFILE = "warehouse_cost_transparency_20260720"
WAREHOUSE_RECOVERY_POLICY_PROFILE = "warehouse_recovery_policy_20260726"
VITRINA_INCIDENT_PROVISIONAL_PROFILE = "vitrina_incident_provisional_20260727"


def _exact_canary_lifecycles(
    operations: list[dict[str, Any]],
    *,
    deployed_sha: str,
) -> dict[str, list[str]]:
    normalized_sha = str(deployed_sha or "").strip().lower()
    exact_canaries = [
        item
        for item in operations
        if bool((item.get("scope") or {}).get("canary"))
        and str(
            (item.get("scope") or {}).get("deployed_sha") or ""
        ).lower()
        == normalized_sha
    ]
    return {
        tier: [
            str(item.get("lifecycle") or "")
            for item in exact_canaries
            if str(item.get("tier") or "") == tier
        ]
        for tier in ("T1", "T2")
    }


def _period_vitrina_url(base_url: str, *, date_to: str) -> str:
    """Open the canonical vitrina tab regardless of browser-local tab state."""

    return (
        str(base_url or "").strip().rstrip("/")
        + "/sheet-vitrina-v1/vitrina?tab=vitrina&history_mode=explicit"
        + "&date_from=2026-07-01&date_to="
        + quote(str(date_to or ""), safe="")
    )


def run_warehouse_ui_flow(
    *,
    base_url: str,
    auth_cookie: str | None,
    expected_readback: Mapping[str, Any],
    evidence_dir: Path,
    deployed_sha: str = "",
    headless: bool = True,
    strict_business_acceptance: bool = True,
    acceptance_profile: str | None = None,
    allowed_server_error_paths: tuple[str, ...] = (),
    allowed_console_error_messages: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run acceptance and always persist a machine-readable terminal report."""

    try:
        return _run_warehouse_ui_flow(
            base_url=base_url,
            auth_cookie=auth_cookie,
            expected_readback=expected_readback,
            evidence_dir=evidence_dir,
            deployed_sha=deployed_sha,
            headless=headless,
            strict_business_acceptance=strict_business_acceptance,
            acceptance_profile=acceptance_profile,
            allowed_server_error_paths=allowed_server_error_paths,
            allowed_console_error_messages=allowed_console_error_messages,
        )
    except Exception as exc:
        target = Path(evidence_dir).resolve()
        target.mkdir(parents=True, exist_ok=True)
        report = {
            "status": "failed",
            "requested_url": str(base_url or "").strip().rstrip("/") + WAREHOUSE_UI_PATH,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "strict_business_acceptance": bool(strict_business_acceptance),
            "acceptance_profile": str(acceptance_profile or ""),
            "deployed_sha": str(deployed_sha or ""),
        }
        (target / "warehouse_ui_flow_report.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        raise


def _run_warehouse_ui_flow(
    *,
    base_url: str,
    auth_cookie: str | None,
    expected_readback: Mapping[str, Any],
    evidence_dir: Path,
    deployed_sha: str = "",
    headless: bool = True,
    strict_business_acceptance: bool = True,
    acceptance_profile: str | None = None,
    allowed_server_error_paths: tuple[str, ...] = (),
    allowed_console_error_messages: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Render every warehouse and compare visible values with canonical readback."""

    normalized_base_url = str(base_url or "").strip().rstrip("/")
    parsed_base_url = urlparse(normalized_base_url)
    if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname:
        raise ValueError("warehouse UI flow requires an absolute http(s) base URL")
    normalized_acceptance_profile = str(acceptance_profile or "").strip()
    if normalized_acceptance_profile not in {
        "",
        WAREHOUSE_CHAIN_RECOVERY_PROFILE,
        WAREHOUSE_COST_TRANSPARENCY_PROFILE,
        WAREHOUSE_RECOVERY_POLICY_PROFILE,
        VITRINA_INCIDENT_PROVISIONAL_PROFILE,
    }:
        raise ValueError(f"unknown warehouse UI acceptance profile: {normalized_acceptance_profile}")
    normalized_deployed_sha = str(deployed_sha or "").strip().lower()
    if (
        normalized_acceptance_profile
        in {
            WAREHOUSE_RECOVERY_POLICY_PROFILE,
            VITRINA_INCIDENT_PROVISIONAL_PROFILE,
        }
        and not re.fullmatch(r"[0-9a-f]{40}", normalized_deployed_sha)
    ):
        raise ValueError(
            "profiled warehouse UI acceptance requires an exact deployed SHA"
        )
    documents = [
        dict(item)
        for item in expected_readback.get("documents") or []
        if str(item.get("document_type") or "") in {"functional_cutover", "warehouse_sync"}
    ]
    expected_by_key = {str(item.get("warehouse_key") or ""): dict(item) for item in documents}
    expected_keys = [key for key, _ in WAREHOUSES]
    if expected_readback.get("status") != "ready" or sorted(expected_by_key) != sorted(expected_keys):
        raise ValueError("warehouse UI flow requires a reconciled six-document readback")
    if len(documents) != len(expected_keys):
        raise ValueError("warehouse UI flow requires exactly six opening documents")
    reconciliation = dict(expected_readback.get("reconciliation") or {})
    if int(reconciliation.get("negative_balance_count") or 0) != 0 or int(reconciliation.get("positive_cost_gap_count") or 0) != 0:
        raise ValueError("warehouse UI flow requires non-negative fully cost-covered readback")
    cutover_discrepancy = dict(expected_readback.get("cutover_opening_discrepancy") or {})
    if Decimal(str(cutover_discrepancy.get("quantity") or 0)) != 0:
        raise ValueError("functional cutover discrepancy opening must be zero")
    historical_cost = dict(expected_readback.get("historical_wb_cost_projection") or {})
    value_gap_count = int(
        historical_cost.get("value_gap_count")
        if historical_cost.get("value_gap_count") is not None
        else historical_cost.get("gap_count") or 0
    )
    if value_gap_count != 0:
        raise ValueError("historical WB cost projection has positive uncovered rows")
    if strict_business_acceptance and int(historical_cost.get("total_gap_count") or historical_cost.get("gap_count") or 0) != 0:
        raise ValueError("historical WB cost projection has missing business dates")
    sync_readback = dict(expected_readback.get("sync") or {})
    if not str(sync_readback.get("last_success_at") or ""):
        raise ValueError("warehouse UI flow requires a successful bounded WB sync timestamp")
    wb_snapshot = dict(expected_readback.get("wb_snapshot") or {})
    if strict_business_acceptance:
        if (
            normalized_acceptance_profile == WAREHOUSE_COST_TRANSPARENCY_PROFILE
            and int(wb_snapshot.get("sku_count") or 0) != 33
        ):
            raise ValueError("profiled official WB snapshot must reconcile all 33 SKU")
        if not bool(wb_snapshot.get("pagination_complete")):
            raise ValueError("official WB snapshot pagination must be complete")
        if int(wb_snapshot.get("exact_duplicate_count") or 0) != 0 or int(
            wb_snapshot.get("source_key_duplicate_count") or 0
        ) != 0:
            raise ValueError("official WB snapshot contains duplicate source rows")
        if not bool(wb_snapshot.get("raw_to_canonical_mapping_matches")):
            raise ValueError("official WB raw payload does not reconcile to canonical items")
        if (
            Decimal(str(wb_snapshot.get("physical_quantity") or 0))
            + Decimal(str(wb_snapshot.get("in_way_to_client") or 0))
            + Decimal(str(wb_snapshot.get("in_way_from_client") or 0))
            != Decimal(str(wb_snapshot.get("wb_contour_quantity") or 0))
        ):
            raise ValueError("official WB contour arithmetic does not reconcile")

    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    navigation_chain: list[dict[str, Any]] = []
    sku_management_page_responses: list[Any] = []
    fatal_surface_matches: list[dict[str, str]] = []
    screenshots: list[str] = []
    warehouse_evidence: list[dict[str, Any]] = []
    warehouse_detail_by_key: dict[str, dict[str, Any]] = {}
    warehouse_action_theme: dict[str, Any] = {}
    incident_policy_evidence: dict[str, Any] = {}
    recovery_policy_evidence: dict[str, Any] = {}
    business_acceptance: dict[str, Any] = {}
    settings_evidence: dict[str, Any] = {}
    supplier_evidence: dict[str, Any] = {}
    consumer_evidence: dict[str, Any] = {}
    vitrina_incident_provisional_evidence: dict[str, Any] = {}
    requested_url = normalized_base_url + WAREHOUSE_UI_PATH

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        if auth_cookie:
            cookie_name, separator, cookie_value = auth_cookie.partition("=")
            if separator != "=" or cookie_name != "wb_core_web_session" or not cookie_value:
                raise ValueError("invalid app-session cookie supplied to warehouse UI flow")
            context.add_cookies(
                [
                    {
                        "name": cookie_name,
                        "value": cookie_value,
                        "url": normalized_base_url,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
        page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text) if message.type == "error" else None,
        )
        page.on(
            "response",
            lambda response: server_errors.append(
                {"status": response.status, "url": response.url, "resource_type": response.request.resource_type}
            )
            if response.status >= 500
            else None,
        )
        warehouse_sync_post_requests: list[str] = []
        protected_business_post_requests: list[str] = []
        page.on(
            "request",
            lambda candidate: warehouse_sync_post_requests.append(candidate.url)
            if (
                candidate.method == "POST"
                and urlparse(candidate.url).path
                == "/v1/sheet-vitrina-v1/warehouses/sync"
            )
            else None,
        )
        page.on(
            "request",
            lambda candidate: protected_business_post_requests.append(candidate.url)
            if (
                candidate.method == "POST"
                and urlparse(candidate.url).path
                in {
                    "/v1/sheet-vitrina-v1/supply/wb-warehouse-exclusions/settings",
                    "/v1/sheet-vitrina-v1/sku-management/price/preview",
                    "/v1/sheet-vitrina-v1/sku-management/price/commit",
                    "/v1/sheet-vitrina-v1/sku-management/bid/preview",
                    "/v1/sheet-vitrina-v1/sku-management/bid/commit",
                }
            )
            else None,
        )
        page.on(
            "response",
            lambda navigation_response: navigation_chain.append(
                {"status": navigation_response.status, "url": navigation_response.url}
            )
            if navigation_response.request.resource_type == "document"
            else None,
        )
        page.on(
            "response",
            lambda candidate: sku_management_page_responses.append(candidate)
            if (
                candidate.request.method == "GET"
                and urlparse(candidate.url).path == "/v1/sheet-vitrina-v1/sku-management"
            )
            else None,
        )
        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(response is not None and response.status == 200, "warehouse document response must be HTTP 200")
        initial_final_url = page.url
        main_navigation_chain = list(navigation_chain)
        _assert(initial_final_url == requested_url, "warehouse main navigation must not redirect unexpectedly")
        page.locator('[data-unified-tab-panel="warehouses"]:not([hidden])').wait_for(timeout=60_000)
        _assert(
            page.locator('[data-warehouse-key]:not([data-warehouse-key="update"])').count() == 6,
            "exactly six warehouse detail selectors must render",
        )
        _assert(
            page.locator('[data-warehouse-key="update"]').count() == 1,
            "separate current-source update tab must render",
        )
        _assert(
            page.locator("[data-warehouse-sync], [data-warehouse-emergency]").count() == 0,
            "manual sync/rebuild controls must be absent from six warehouse detail pages",
        )
        _assert(bool(page.title().strip()), "document title must be non-empty")
        _assert(bool(page.locator("body").inner_text().strip()), "document body must be non-empty")
        page.locator('[data-warehouse-key="update"]').click()
        page.locator("[data-warehouse-update-view]:not([hidden])").wait_for(timeout=60_000)
        _assert(
            not warehouse_sync_post_requests,
            "opening update tab must not start a warehouse mutation",
        )
        recovery_payload = _protected_json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/warehouses/recovery",
            label="Warehouse recovery policy API",
        )
        page.wait_for_function(
            """() => {
                const node = document.querySelector("[data-warehouse-recovery-status]");
                return node && !["Загрузка…", "Status недоступен"].includes(node.textContent.trim());
            }""",
            timeout=60_000,
        )
        recovery_operations = list(recovery_payload.get("operations") or [])
        recovery_orphans = dict(recovery_payload.get("orphan_scanner") or {})
        recovery_capacity = dict(recovery_payload.get("capacity") or {})
        _assert(
            recovery_payload.get("contract_name") == "warehouse_recovery_policy_v1",
            "recovery API exposes the authoritative contract",
        )
        _assert(
            {
                "free_bytes",
                "reserved_bytes",
                "operational_reserve_bytes",
                "available_after_reservations_bytes",
                "degraded",
                "hard_stop",
            }.issubset(recovery_capacity),
            "recovery API exposes capacity reservations and watermarks",
        )
        _assert(
            isinstance(recovery_payload.get("writer"), dict)
            and isinstance(recovery_payload.get("timer"), dict)
            and isinstance(recovery_payload.get("tiers"), list),
            "recovery API exposes writer, timer and deterministic tier table",
        )
        if normalized_acceptance_profile == WAREHOUSE_RECOVERY_POLICY_PROFILE:
            lifecycles_by_tier = _exact_canary_lifecycles(
                recovery_operations,
                deployed_sha=normalized_deployed_sha,
            )
            active_or_failed = {
                "planned",
                "reserved",
                "writing",
                "mutation_running",
                "failed_recoverable",
                "quarantined",
            }
            _assert(
                "rolled_back" in lifecycles_by_tier["T1"]
                and "retained" in lifecycles_by_tier["T2"]
                and not any(
                    lifecycle in active_or_failed
                    for lifecycles in lifecycles_by_tier.values()
                    for lifecycle in lifecycles
                ),
                "exact deployed-SHA bounded and wide canaries are terminal",
            )
            _assert(
                recovery_payload.get("status") == "ready"
                and recovery_orphans.get("status") == "clean"
                and int(recovery_orphans.get("orphan_count") or 0) == 0,
                "production recovery status has no orphan or quarantine leak",
            )
            _assert(
                bool(recovery_orphans.get("policy_activation_at"))
                and "pre_policy_legacy_count" in recovery_orphans,
                "production recovery status exposes the activation baseline",
            )
        recovery_status_text = page.locator(
            "[data-warehouse-recovery-status]"
        ).inner_text().strip()
        recovery_orphan_text = page.locator(
            "[data-warehouse-recovery-orphans]"
        ).inner_text().strip()
        recovery_table_text = page.locator(
            "[data-warehouse-recovery-operations]"
        ).inner_text().strip()
        _assert(
            recovery_status_text in {"Policy ready", "Требуется внимание"}
            and bool(recovery_table_text),
            "recovery lifecycle renders visibly",
        )
        pre_policy_legacy_count = int(
            recovery_orphans.get("pre_policy_legacy_count") or 0
        )
        if pre_policy_legacy_count:
            _assert(
                f"pre-policy baseline {pre_policy_legacy_count}"
                in recovery_orphan_text,
                "pre-policy backup baseline renders visibly",
            )
        recovery_screenshot = evidence_dir / "warehouse_recovery_policy.png"
        page.screenshot(path=str(recovery_screenshot), full_page=False)
        screenshots.append(str(recovery_screenshot))
        recovery_policy_evidence = {
            "contract_name": recovery_payload.get("contract_name"),
            "status": recovery_payload.get("status"),
            "deployed_sha": normalized_deployed_sha,
            "visible_status": recovery_status_text,
            "visible_orphan_status": recovery_orphan_text,
            "operation_count": len(recovery_operations),
            "tiers": sorted(
                {str(item.get("tier") or "") for item in recovery_operations}
            ),
            "lifecycles": sorted(
                {str(item.get("lifecycle") or "") for item in recovery_operations}
            ),
            "capacity": recovery_capacity,
            "writer": recovery_payload.get("writer"),
            "timer": recovery_payload.get("timer"),
            "orphan_scanner": recovery_orphans,
            "screenshot": str(recovery_screenshot),
            "business_post_requests": list(protected_business_post_requests),
        }
        warehouse_action_theme = _warehouse_action_theme_evidence(page)

        for warehouse_key, warehouse_name in WAREHOUSES:
            expected = expected_by_key[warehouse_key]
            page.locator(f'[data-warehouse-key="{warehouse_key}"]').click()
            page.wait_for_function(
                "expected => document.querySelector('[data-warehouse-title]').textContent === expected",
                arg=warehouse_name,
                timeout=60_000,
            )
            document_row = page.locator(
                f'[data-warehouse-document-id="{expected["document_id"]}"]'
            )
            document_row.wait_for(timeout=60_000)

            detail_payload = _protected_json_get(
                context,
                normalized_base_url + "/v1/sheet-vitrina-v1/warehouses/" + warehouse_key,
                label=f"{warehouse_name}: detail API",
            )
            warehouse_detail_by_key[warehouse_key] = detail_payload
            detail_summary = dict(detail_payload.get("warehouse") or {})
            detail_balances = list(detail_payload.get("balances") or [])
            detail_documents = list(detail_payload.get("documents") or [])
            matching_documents = [
                item
                for item in detail_documents
                if str(item.get("document_id") or "") == str(expected.get("document_id") or "")
            ]
            _assert(len(matching_documents) == 1, f"{warehouse_name}: active version document is in detail registry")
            detail_document = matching_documents[0]
            _assert(
                str(detail_document.get("document_id") or "") == str(expected.get("document_id") or ""),
                f"{warehouse_name}: detail/readback document identity",
            )
            _assert(
                Decimal(str(detail_document.get("total_quantity") or 0))
                == Decimal(str(expected.get("quantity") or 0)),
                f"{warehouse_name}: opening document/readback quantity",
            )
            _assert(
                dict(detail_document.get("provenance") or {})
                == dict(expected.get("provenance") or {}),
                f"{warehouse_name}: document provenance/readback",
            )
            if warehouse_key == "wb_acceptance_discrepancy":
                _assert(
                    all(Decimal(str(item.get("quantity") or 0)) >= 0 for item in detail_balances),
                    "Расхождения приёмки WB: negative balances are forbidden",
                )

            summary_values = page.locator("[data-warehouse-summary] .warehouse-summary-value").all_inner_texts()
            expected_summary_count = 7 if warehouse_key in {"ff", "wb"} else 4
            _assert(len(summary_values) == expected_summary_count, f"{warehouse_name}: summary values")
            visible_sku_count = _visible_decimal(summary_values[0])
            visible_quantity = _visible_decimal(summary_values[1])
            visible_capital = _visible_money(summary_values[2])
            expected_sku_count = Decimal(str(detail_summary.get("sku_count") or 0))
            expected_quantity = Decimal(str(detail_summary.get("total_quantity") or 0))
            expected_capital = Decimal(str(detail_summary.get("total_capital_rub") or 0))
            expected_wac = Decimal(str(detail_summary.get("average_unit_cost_rub") or 0))
            _assert(visible_sku_count == expected_sku_count, f"{warehouse_name}: visible SKU count")
            _assert(visible_quantity == expected_quantity, f"{warehouse_name}: visible total quantity")
            _assert(abs(visible_capital - expected_capital) < Decimal("0.02"), f"{warehouse_name}: visible capital")
            if expected_quantity > 0:
                visible_wac = _visible_money(summary_values[3])
                _assert(abs(visible_wac - expected_wac) < Decimal("0.02"), f"{warehouse_name}: visible WAC")
                _assert(expected_capital > 0 and expected_wac > 0, f"{warehouse_name}: positive cost coverage")
            else:
                _assert(summary_values[3].strip() == "—", f"{warehouse_name}: zero warehouse WAC is honestly empty")
            _assert("Актуальность:" in page.locator("[data-warehouse-meta]").inner_text(), f"{warehouse_name}: timestamp")
            _assert("Источник:" in page.locator("[data-warehouse-meta]").inner_text(), f"{warehouse_name}: source")
            if warehouse_key == "wb":
                labels = page.locator("[data-warehouse-summary] .warehouse-summary-label").all_inner_texts()
                _assert(labels[1] == "Всего в контуре WB", "WB contour replaces ambiguous total tile")
                _assert("На складах WB + В пути к покупателям + В пути возврата на WB" in page.locator("[data-warehouse-meta]").inner_text(), "WB contour formula is visible")
            if warehouse_key == "ff":
                labels = page.locator("[data-warehouse-summary] .warehouse-summary-label").all_inner_texts()
                _assert(
                    labels == [
                        "Уникальных SKU",
                        "Физический остаток",
                        "Товарный капитал",
                        "Средневзвешенная себестоимость",
                        "Зарезервировано",
                        "Доступно",
                        "Необеспеченный резерв",
                    ],
                    "Склад FF: physical and reservation summary labels",
                )
            expected_status = str(detail_summary.get("status_label") or "").strip()
            expected_status_detail = str(
                detail_summary.get("status_description") or ""
            ).strip()
            page.wait_for_function(
                """expected => {
                    const status = (
                        document.querySelector("[data-warehouse-status]")?.textContent || ""
                    ).trim();
                    const detail = (
                        document.querySelector("[data-warehouse-status-detail]")?.textContent || ""
                    ).trim();
                    return status === expected.status && detail === expected.detail;
                }""",
                arg={"status": expected_status, "detail": expected_status_detail},
                timeout=60_000,
            )
            visible_status = page.locator("[data-warehouse-status]").inner_text().strip()
            _assert(
                visible_status == expected_status,
                f"{warehouse_name}: localized functional status",
            )
            _assert(
                page.locator("[data-warehouse-status-detail]").inner_text().strip()
                == expected_status_detail,
                f"{warehouse_name}: status reason and timestamp surface",
            )
            _assert(
                all(
                    Decimal(str(item.get("quantity") or 0)) >= 0
                    and (Decimal(str(item.get("quantity") or 0)) == 0 or Decimal(str(item.get("wac_rub") or 0)) > 0)
                    for item in detail_balances
                ),
                f"{warehouse_name}: no negative or uncovered SKU balances",
            )
            if warehouse_key == "wb":
                contour = dict(detail_summary.get("wb_contour") or {})
                expected_contour = [
                    Decimal(str(contour.get("quantity") or 0)),
                    Decimal(str(contour.get("in_way_to_client") or 0)),
                    Decimal(str(contour.get("in_way_from_client") or 0)),
                ]
                _assert([_visible_decimal(value) for value in summary_values[4:7]] == expected_contour, "Склад WB: contour components")
                _assert(visible_quantity == Decimal(str(contour.get("total") or 0)), "Склад WB: contour total")
                policy_card = page.locator("[data-wb-incident-policy-card]")
                policy_card.wait_for(state="visible", timeout=60_000)
                page.wait_for_function(
                    """() => {
                      const audit = document.querySelector("[data-wb-incident-audit]");
                      const options = document.querySelector("[data-wb-incident-options]");
                      return audit && options
                        && !audit.textContent.includes("Revision ещё не создана")
                        && !options.textContent.includes("загружается");
                    }""",
                    timeout=60_000,
                )
                policy_text = policy_card.inner_text()
                _assert(
                    "Инциденты на складах WB" in policy_text
                    and "Капитал и фактический складской контур не изменяются" in policy_text,
                    "Склад WB: incident policy contract is visible",
                )
                option_nodes = page.locator("[data-wb-incident-warehouse-id]")
                if strict_business_acceptance:
                    _assert(
                        option_nodes.count() > 0,
                        "Склад WB: complete current snapshot exposes incident-policy warehouse options",
                    )
                _assert(
                    all(
                        str(option_nodes.nth(index).get_attribute("data-wb-incident-warehouse-id") or "").isdigit()
                        for index in range(option_nodes.count())
                    ),
                    "Склад WB: incident policy options use stable numeric warehouse IDs",
                )
                incident_policy_evidence = {
                    "visible": True,
                    "badge": page.locator("[data-wb-incident-policy-badge]").inner_text().strip(),
                    "revision_audit": page.locator("[data-wb-incident-audit]").inner_text().strip(),
                    "numeric_warehouse_option_count": option_nodes.count(),
                    "selected_warehouse_count": page.locator(
                        "[data-wb-incident-warehouse-id]:checked"
                    ).count(),
                    "effective_from": page.locator(
                        "[data-wb-incident-effective-from]"
                    ).input_value(),
                    "active": page.locator("[data-wb-incident-active]").is_checked(),
                    "apply_not_clicked": True,
                }
            warehouse_surface_text = page.locator('[data-unified-tab-panel="warehouses"]').inner_text()
            for marker in ("Internal Server Error", "Traceback", "Остатки / Склады failed", "Данные склада не загружены."):
                if marker in warehouse_surface_text:
                    fatal_surface_matches.append({"warehouse_key": warehouse_key, "marker": marker})

            balance_count = page.locator("[data-warehouse-balance-row]").count()
            _assert_warehouse_balance_cardinality(
                warehouse_key=warehouse_key,
                expected_sku_count=int(expected_sku_count),
                detail_balances=detail_balances,
                visible_balance_count=balance_count,
                warehouse_name=warehouse_name,
            )
            _assert(
                all(str(item.get("warning") or "").strip() for item in detail_balances),
                f"{warehouse_name}: every cost row has a visible localized status",
            )
            certified_count = sum(1 for item in detail_balances if bool(item.get("certified")))
            if certified_count:
                certified_without_identity_warning = sum(
                    1
                    for item in detail_balances
                    if bool(item.get("certified"))
                    and not str(item.get("identity_warning") or "").strip()
                )
                _assert(
                    page.locator('[data-warehouse-balance-row] .warehouse-balance-warning[data-tone="success"]').count()
                    == certified_without_identity_warning,
                    f"{warehouse_name}: only certified rows without identity warnings have green visible status",
                )
            if warehouse_key == "ff":
                legacy_payload = _protected_json_get(
                    context,
                    normalized_base_url
                    + "/v1/sheet-vitrina-v1/supply/ff-stocks?operations_limit=50&operations_page=1&show_technical_archive=0",
                    label="Склад FF: legacy canonical API",
                )
                legacy_rows = list(((legacy_payload.get("registry") or {}).get("rows") or []))
                legacy_nonzero = {
                    int(item.get("nm_id") or 0): Decimal(str(item.get("quantity") or 0))
                    for item in legacy_rows
                    if Decimal(str(item.get("quantity") or 0)) != 0
                }
                detail_nonzero = {
                    int(item.get("nm_id") or 0): Decimal(str(item.get("quantity") or 0))
                    for item in detail_balances
                    if Decimal(str(item.get("quantity") or 0)) != 0
                }
                _assert(detail_nonzero == legacy_nonzero, "Склад FF: unified/legacy canonical quantities")
            top_screenshot = evidence_dir / f"warehouse_{warehouse_key}_top.png"
            page.screenshot(path=str(top_screenshot), full_page=False)
            screenshots.append(str(top_screenshot))

            document_row.scroll_into_view_if_needed()
            document_row.locator("details").first.click()
            expected_lines = list(detail_document.get("lines") or [])
            rendered_document_lines = document_row.locator(".warehouse-document-lines tbody tr").count()
            _assert(
                rendered_document_lines == max(1, len(expected_lines)),
                f"{warehouse_name}: opening document line count",
            )
            document_text = document_row.inner_text()
            _assert(str(expected.get("document_id") or "") in document_text, f"{warehouse_name}: document number")
            _assert(
                any(label in document_text for label in ("Функциональный cutover", "Почасовая версия склада")),
                f"{warehouse_name}: functional document type",
            )
            document_provenance = document_row.locator(
                ".warehouse-document-provenance > details.warehouse-source-details"
            )
            _assert(document_provenance.count() == 1, f"{warehouse_name}: document provenance control")
            document_provenance.click()
            _assert(
                document_row.locator(".warehouse-document-provenance .warehouse-evidence-item").count() >= 1,
                f"{warehouse_name}: human-readable document evidence",
            )
            technical_document = document_row.locator(
                ".warehouse-document-provenance details.warehouse-technical-evidence"
            )
            technical_document.click()
            document_provenance_text = technical_document.locator("pre").inner_text()
            _assert(bool(document_provenance_text.strip()), f"{warehouse_name}: document provenance payload")
            if expected_lines:
                line_evidence = document_row.locator(
                    ".warehouse-document-lines details.warehouse-source-details"
                ).first
                line_evidence.click()
                _assert(
                    line_evidence.locator(".warehouse-evidence-item").count() >= 1,
                    f"{warehouse_name}: human-readable line evidence",
                )
                line_technical = line_evidence.locator("details.warehouse-technical-evidence")
                line_technical.click()
                _assert(
                    bool(line_technical.locator("pre").inner_text().strip()),
                    f"{warehouse_name}: line provenance",
                )
            document_screenshot = evidence_dir / f"warehouse_{warehouse_key}_document.png"
            page.screenshot(path=str(document_screenshot), full_page=False)
            screenshots.append(str(document_screenshot))
            warehouse_evidence.append(
                {
                    "warehouse_key": warehouse_key,
                    "warehouse_name": warehouse_name,
                    "document_id": str(expected.get("document_id") or ""),
                    "sku_count": int(expected_sku_count),
                    "total_quantity": str(detail_summary.get("total_quantity") or 0),
                    "opening_sku_count": int(expected.get("sku_count") or 0),
                    "opening_total_quantity": str(expected.get("quantity") or 0),
                    "balance_rows": balance_count,
                    "document_lines": len(expected_lines),
                    "document_provenance": dict(expected.get("provenance") or {}),
                    "top_screenshot": str(top_screenshot),
                    "document_screenshot": str(document_screenshot),
                }
            )

        page.locator('[data-unified-tab-button="factory-order"]').click()
        frame = page.frame_locator('[data-operator-embed-frame="factory-order"]')
        frame.locator('[data-supply-mode-button="fulfillment"]').click()
        frame.locator('[data-ff-section-button="stocks"]').click()
        page.wait_for_function(
            "document.querySelector('[data-warehouse-title]').textContent === 'Склад FF'",
            timeout=60_000,
        )
        _assert("tab=warehouses" in page.url and "warehouse=ff" in page.url, "legacy FF transition")
        legacy_ff_expected = next(
            item for item in warehouse_evidence if item.get("warehouse_key") == "ff"
        )
        legacy_ff_document = page.locator(
            f'[data-warehouse-document-id="{legacy_ff_expected["document_id"]}"]'
        )
        legacy_ff_document.wait_for(timeout=60_000)
        legacy_summary_values = page.locator(
            "[data-warehouse-summary] .warehouse-summary-value"
        ).all_inner_texts()
        _assert(len(legacy_summary_values) == 7, "legacy FF transition: reservation-aware summary values")
        _assert(
            _visible_decimal(legacy_summary_values[0])
            == Decimal(str(legacy_ff_expected["sku_count"])),
            "legacy FF transition: loaded SKU count",
        )
        _assert(
            _visible_decimal(legacy_summary_values[1])
            == Decimal(str(legacy_ff_expected["total_quantity"])),
            "legacy FF transition: loaded total quantity",
        )
        _assert(_visible_money(legacy_summary_values[2]) > 0, "legacy FF transition: capital loaded")
        _assert(_visible_money(legacy_summary_values[3]) > 0, "legacy FF transition: WAC loaded")
        legacy_ff_detail = _protected_json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/warehouses/ff",
            label="legacy FF transition: functional detail",
        )
        _assert(
            page.locator("[data-warehouse-status]").inner_text().strip()
            == str((legacy_ff_detail.get("warehouse") or {}).get("status_label") or "").strip(),
            "legacy FF transition: localized functional status",
        )
        _assert(
            page.locator("[data-warehouse-balance-row]").count()
            == int(legacy_ff_expected["balance_rows"]),
            "legacy FF transition: loaded balance rows",
        )
        legacy_screenshot = evidence_dir / "warehouse_legacy_ff_transition.png"
        page.screenshot(path=str(legacy_screenshot), full_page=False)
        screenshots.append(str(legacy_screenshot))

        if not strict_business_acceptance:
            final_url = page.url
            context.close()
            browser.close()
            unexpected_server_errors = [
                item
                for item in server_errors
                if urlparse(str(item.get("url") or "")).path not in set(allowed_server_error_paths)
            ]
            unexpected_console_errors = [
                message for message in console_errors if message not in set(allowed_console_error_messages)
            ]
            _assert(not unexpected_server_errors, f"5xx responses: {unexpected_server_errors}")
            _assert(not page_errors, f"pageerror: {page_errors}")
            _assert(not unexpected_console_errors, f"console errors: {unexpected_console_errors}")
            _assert(not fatal_surface_matches, f"fatal UI surface: {fatal_surface_matches}")
            report = {
                "status": "ok",
                "acceptance_scope": "local_warehouse_structure",
                "requested_url": requested_url,
                "final_url": final_url,
                "initial_final_url": initial_final_url,
                "document_status": int(response.status) if response is not None else None,
                "main_navigation_chain": main_navigation_chain,
                "all_document_responses": navigation_chain,
                "title_nonempty": True,
                "body_nonempty": True,
                "cutover": dict(expected_readback.get("cutover") or {}),
                "warehouses": warehouse_evidence,
                "calculation_parameters": {"status": "not_in_local_structure_scope"},
                "supplier_registry": {"status": "not_in_local_structure_scope"},
                "dependent_consumers": {"status": "not_in_local_structure_scope"},
                "hourly_sync": dict(expected_readback.get("sync") or {}),
                "warehouse_action_theme": warehouse_action_theme,
                "recovery_policy": recovery_policy_evidence,
                "historical_wb_cost_projection": dict(expected_readback.get("historical_wb_cost_projection") or {}),
                "legacy_ff_transition": True,
                "legacy_ff_reconciliation": {
                    "document_id": str(legacy_ff_expected["document_id"]),
                    "sku_count": int(legacy_ff_expected["sku_count"]),
                    "total_quantity": str(legacy_ff_expected["total_quantity"]),
                    "balance_rows": int(legacy_ff_expected["balance_rows"]),
                    "economics_loaded": True,
                    "loaded_before_screenshot": True,
                },
                "server_errors": server_errors,
                "unexpected_server_errors": unexpected_server_errors,
                "page_errors": page_errors,
                "console_errors": console_errors,
                "unexpected_console_errors": unexpected_console_errors,
                "fatal_surface_matches": fatal_surface_matches,
                "screenshots": screenshots,
            }
            report_path = evidence_dir / "warehouse_ui_flow_report.json"
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            report["report_path"] = str(report_path)
            return report

        business_acceptance = _assert_business_warehouse_acceptance(
            warehouse_detail_by_key,
            acceptance_profile=normalized_acceptance_profile or None,
        )

        settings_url = normalized_base_url + "/sheet-vitrina-v1/settings"
        settings_response = page.goto(settings_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(settings_response is not None and settings_response.status == 200, "calculation settings page status")
        settings_surface = _settings_frame_locator(page)
        settings_surface.locator('[data-settings-group-button="user-directory"]').click()
        settings_surface.locator('[data-settings-group-panel="user-directory"]:not([hidden])').wait_for(
            timeout=60_000
        )
        buyout_input = settings_surface.locator('[data-calculation-rate="buyout_rate"]')
        buyout_input.wait_for(timeout=60_000)
        expect(buyout_input).to_have_value("91", timeout=60_000)
        _assert(
            buyout_input.input_value() == "91",
            "visible calculation settings buyout 91%",
        )
        settings_payload = _protected_json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/settings/calculation-parameters",
            label="calculation settings API",
        )
        parameters = dict(((settings_payload.get("current") or {}).get("parameters") or {}))
        _assert(parameters.get("buyout_rate_pct") == "91", "calculation settings buyout 91%")
        _assert(parameters.get("included_expense_rate_pct") == "44", "calculation settings expenses 44%")
        _assert(parameters.get("retained_share_pct") == "56", "calculation settings retained share 56%")
        reference = dict(settings_payload.get("reference") or {})
        _assert(reference.get("status") == "ready" and len(reference.get("weeks") or []) == 3, "three closed WB weeks")
        _assert(
            settings_surface.locator("#calculationExpenseTotal").inner_text().strip() == "44%",
            "visible expenses 44%",
        )
        _assert(
            settings_surface.locator("#calculationRetainedShare").inner_text().strip() == "56%",
            "visible retained share 56%",
        )
        _assert(
            "canonical_WB_WAC" in settings_surface.locator("#calculationFormulaPreview").inner_text(),
            "visible Proxy formula",
        )
        reference_rows = settings_surface.locator("#calculationReferenceRows tr")
        expect(reference_rows).to_have_count(6, timeout=60_000)
        _assert(reference_rows.count() == 6, "six WB reference rows")
        reference_values = settings_surface.locator(
            "#calculationReferenceRows tr td:not(:first-child)"
        ).all_inner_texts()
        _assert(
            reference_values
            and all(
                value.strip() == "—"
                or (
                    value.strip().endswith("%")
                    and len(value.strip()) <= 8
                )
                for value in reference_values
            ),
            "WB reference percentages are rounded and readable",
        )
        history_rows = settings_surface.locator("#calculationHistoryRows tr")
        history_rows.first.wait_for(timeout=60_000)
        _assert(history_rows.count() >= 1, "settings version history")
        settings_screenshot = evidence_dir / "calculation_parameters.png"
        page.screenshot(path=str(settings_screenshot), full_page=True)
        screenshots.append(str(settings_screenshot))
        settings_evidence = {
            "url": page.url,
            "embedded_url": page.locator("[data-settings-embed-frame]").get_attribute("src"),
            "buyout_rate_pct": parameters.get("buyout_rate_pct"),
            "included_expense_rate_pct": parameters.get("included_expense_rate_pct"),
            "retained_share_pct": parameters.get("retained_share_pct"),
            "effective_date": (settings_payload.get("current") or {}).get("effective_date"),
            "reference_weeks": reference.get("weeks"),
            "history_count": len(settings_payload.get("history") or []),
            "screenshot": str(settings_screenshot),
        }

        supplier_registry_url = normalized_base_url + "/sheet-vitrina-v1/vitrina?tab=factory-order"
        supplier_registry_response = page.goto(
            supplier_registry_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        _assert(
            supplier_registry_response is not None and supplier_registry_response.status == 200,
            "supplier registry shell status",
        )
        supplier_operator = page.frame_locator('[data-operator-embed-frame="factory-order"]')
        supplier_operator.locator('[data-supply-mode-button="shipment-registry"]').click()
        supplier_operator.locator(
            '[data-supply-mode-panel="shipment-registry"]:not([hidden])'
        ).wait_for(timeout=60_000)
        supplier_operator.get_by_text("Средняя себестоимость: на производстве", exact=True).wait_for(
            timeout=60_000
        )
        supplier_operator.get_by_text("Средняя себестоимость: Китай → FF", exact=True).wait_for(
            timeout=60_000
        )
        supplier_registry_embedded_url = supplier_operator.locator("body").evaluate(
            "element => element.ownerDocument.location.href"
        )
        supplier_registry_screenshot = evidence_dir / "supplier_registry_stage_costs.png"
        page.screenshot(path=str(supplier_registry_screenshot), full_page=True)
        screenshots.append(str(supplier_registry_screenshot))
        registry_payload = _protected_json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/supply/supplier-shipments/registry",
            label="supplier registry API",
        )
        registry_json = json.dumps(registry_payload, ensure_ascii=False)
        _assert("production_average_cost_rub" in registry_json, "supplier production cost field")
        _assert("china_to_ff_average_cost_rub" in registry_json, "supplier China-to-FF cost field")
        cost_profile_evidence: dict[str, Any] = {}
        if normalized_acceptance_profile == WAREHOUSE_COST_TRANSPARENCY_PROFILE:
            cost_profile_evidence = _assert_supplier_cost_transparency_profile(
                page=page,
                context=context,
                base_url=normalized_base_url,
                registry_payload=registry_payload,
                evidence_dir=evidence_dir,
            )
            screenshots.extend(cost_profile_evidence.pop("screenshots"))
        bank_fee_payload: dict[str, Any] | None = None
        bank_fee_shipment_id = ""
        for column in reversed(list(registry_payload.get("columns") or [])):
            shipment_id = str((column or {}).get("shipment_id") or "").strip()
            if not shipment_id:
                continue
            candidate = _protected_json_get(
                context,
                normalized_base_url
                + "/v1/sheet-vitrina-v1/supply/supplier-shipments/"
                + quote(shipment_id, safe="")
                + "/financial-documents",
                label=f"supplier financial API: {shipment_id}",
            )
            exact_fee = ((candidate.get("summary") or {}).get("per_unit") or {}).get("exact_bank_fees_rub")
            if exact_fee is not None and Decimal(str(exact_fee)) > 0:
                bank_fee_payload = candidate
                bank_fee_shipment_id = shipment_id
                break
        _assert(bank_fee_payload is not None, "supplier shipment with confirmed positive bank fees")
        registry_bank_fee_display = _registry_cell_display(
            registry_payload,
            section_id="fact_expenses",
            row_id="bank_fees_rub",
            shipment_id=bank_fee_shipment_id,
        )
        _assert(_visible_money(registry_bank_fee_display) > 0, "supplier registry aggregated bank commissions")
        bank_fee_lines = [
            dict(item)
            for item in (bank_fee_payload or {}).get("expense_lines") or []
            if str(item.get("stage") or "") == "bank_fee"
            and Decimal(str(item.get("amount") or 0)) > 0
        ]
        _assert(bank_fee_lines, "supplier bank fee detail lines")
        _assert(
            all(str((item.get("raw") or {}).get("source") or "") == "bank_fee_statement" for item in bank_fee_lines),
            "supplier bank fee provenance",
        )
        _assert(
            all(str(item.get("currency") or "") in {"RUB", "CNY"} for item in bank_fee_lines),
            "supplier bank fee source currencies",
        )
        _assert(
            all(Decimal(str(item.get("amount_rub") or 0)) > 0 for item in bank_fee_lines),
            "supplier bank fee RUB equivalents",
        )
        supplier_detail_url = _supplier_financial_detail_url(normalized_base_url, bank_fee_shipment_id)
        supplier_detail_response = page.goto(supplier_detail_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(supplier_detail_response is not None and supplier_detail_response.status == 200, "supplier fee detail page status")
        page.locator("#shipmentCard:not([hidden])").wait_for(timeout=60_000)
        # The shipment shell renders a temporary 0,00 total before the
        # financial-documents request completes.  Waiting only for a value
        # different from "-" races that initial paint and can reject a
        # correct production readback.  Pin the completed async state first.
        expect(page.locator("#financialDocumentsMessage")).to_have_text(
            "",
            timeout=60_000,
        )
        page.locator("[data-financial-document-row]").first.wait_for(
            timeout=60_000,
        )
        parent_fee_text = page.locator("[data-bank-fee-total]").inner_text().strip()
        _assert(_visible_money(parent_fee_text) > 0, "supplier bank fee parent total")
        fee_lines_by_document: dict[str, list[dict[str, Any]]] = {}
        for item in bank_fee_lines:
            document_id = str(item.get("financial_document_id") or "")
            if document_id:
                fee_lines_by_document.setdefault(document_id, []).append(item)
        _assert(fee_lines_by_document, "supplier bank fee source document")
        fee_document_id, selected_fee_lines = max(
            fee_lines_by_document.items(),
            key=lambda item: (len(item[1]), item[0]),
        )
        fee_document_row = page.locator(f'[data-financial-document-row="{fee_document_id}"]')
        fee_document_row.wait_for(timeout=60_000)
        fee_document_row.click()
        visible_fee_rows = page.locator('#financialExpenseRows tr[data-expense-source="bank_fee_statement"]')
        _assert(visible_fee_rows.count() == len(selected_fee_lines), "visible supplier bank fee detail lines")
        _assert(
            all("bank_fee_statement" in visible_fee_rows.nth(index).inner_text() for index in range(visible_fee_rows.count())),
            "visible supplier bank fee provenance",
        )
        _assert(
            {visible_fee_rows.nth(index).get_attribute("data-expense-currency") for index in range(visible_fee_rows.count())}
            == {str(item.get("currency") or "") for item in selected_fee_lines},
            "visible supplier bank fee currencies",
        )
        supplier_screenshot = evidence_dir / "supplier_registry_costs.png"
        page.screenshot(path=str(supplier_screenshot), full_page=True)
        screenshots.append(str(supplier_screenshot))
        supplier_evidence = {
            "url": page.url,
            "registry_url": supplier_registry_url,
            "registry_embedded_url": supplier_registry_embedded_url,
            "registry_status": registry_payload.get("status"),
            "production_cost_field": True,
            "china_to_ff_cost_field": True,
            "bank_commissions_visible": True,
            "bank_fee_shipment_id": bank_fee_shipment_id,
            "bank_fee_total": parent_fee_text,
            "registry_bank_fee_total": registry_bank_fee_display,
            "bank_fee_line_count": len(bank_fee_lines),
            "bank_fee_currencies": sorted({str(item.get("currency") or "") for item in bank_fee_lines}),
            "bank_fee_sources": sorted({str((item.get("raw") or {}).get("source") or "") for item in bank_fee_lines}),
            "registry_screenshot": str(supplier_registry_screenshot),
            "screenshot": str(supplier_screenshot),
            **cost_profile_evidence,
        }

        vitrina_url = normalized_base_url + "/sheet-vitrina-v1/vitrina?tab=warehouses&warehouse=production"
        vitrina_response = page.goto(vitrina_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(vitrina_response is not None and vitrina_response.status == 200, "vitrina consumer page status")
        _assert(
            page.locator("[data-open-stock-report]").inner_text().strip() == "Отчёт об остатках",
            "stock report public navigation label",
        )
        page.locator("[data-open-stock-report]").click()
        report_surface = _stock_report_frame_locator(page)
        report_surface.get_by_role("heading", name="Отчёт по остаткам", exact=True).wait_for(
            timeout=60_000
        )
        stock_report_screenshot = evidence_dir / "stock_report_navigation.png"
        page.screenshot(path=str(stock_report_screenshot), full_page=False)
        screenshots.append(str(stock_report_screenshot))
        page.locator('[data-unified-tab-button="sku-management"]').click()
        page.locator('[data-unified-tab-panel="sku-management"]:not([hidden])').wait_for(timeout=60_000)
        _assert(bool(page.locator('[data-unified-tab-panel="sku-management"]').inner_text().strip()), "SKU management visible render")
        page.wait_for_function(
            "() => ((document.querySelector('[data-sku-management-status]') || {}).textContent || '').trim().startsWith('SKU:')",
            timeout=120_000,
        )
        _assert(
            len(sku_management_page_responses) == 1,
            "SKU management page issued exactly one protected source request",
        )
        sku_management_response = sku_management_page_responses[0]
        _assert(
            sku_management_response.status == 200,
            f"SKU management page request: HTTP {sku_management_response.status}",
        )
        sku_management_payload = sku_management_response.json()
        _assert(isinstance(sku_management_payload, dict), "SKU management page response: JSON object")
        _assert(
            not page.locator("[data-sku-management-error]").inner_text().strip(),
            "SKU management visible error state",
        )
        sku_dom_summary = _sku_management_dom_summary(
            page,
            source_rows=list(sku_management_payload.get("rows") or []),
        )
        page.locator('[data-sku-sort="profit_rub"]').scroll_into_view_if_needed(timeout=60_000)
        sku_screenshot = evidence_dir / "sku_management_consumer.png"
        page.screenshot(path=str(sku_screenshot), full_page=False)
        screenshots.append(str(sku_screenshot))
        snapshot_date = str(wb_snapshot.get("snapshot_date") or "")[:10]
        last_closed_date = str(
            datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Yekaterinburg")).date()
            - timedelta(days=1)
        )
        period_date_to = min(snapshot_date, last_closed_date) if snapshot_date else last_closed_date
        period_vitrina_url = _period_vitrina_url(
            normalized_base_url,
            date_to=period_date_to,
        )
        period_vitrina_response = page.goto(
            period_vitrina_url,
            wait_until="domcontentloaded",
            timeout=120_000,
        )
        _assert(
            period_vitrina_response is not None and period_vitrina_response.status == 200,
            "canonical vitrina period page status",
        )
        page.locator('[data-unified-tab-panel="vitrina"]:not([hidden])').wait_for(timeout=60_000)
        page.wait_for_function(
            "document.body.innerText.includes('proxy прибыль 3') && document.body.innerText.includes('Прокси маржинальность 3')",
            timeout=120_000,
        )
        filled_metrics = {
            metric_key: _filled_metric_cells(page, metric_key=metric_key, date_from="2026-07-01")
            for metric_key in (
                "our_wb_unit_cost_rub",
                "proxy_profit_3_rub",
                "proxy_margin_3_pct",
            )
        }
        _assert(
            all(count > 0 for count in filled_metrics.values()),
            "canonical WB cost and Proxy 3 are filled from 2026-07-01 where persisted inputs exist",
        )
        closed_dates = []
        cursor = date.fromisoformat("2026-07-01")
        closed_end = date.fromisoformat(period_date_to)
        while cursor <= closed_end:
            closed_dates.append(cursor.isoformat())
            cursor += timedelta(days=1)
        wb_contour_by_date = dict(
            historical_cost.get("contour_quantities_by_date") or {}
        )
        closed_date_coverage = {
            metric_key: {
                day: _metric_date_coverage(
                    page,
                    metric_key=metric_key,
                    day=day,
                    wb_contour_by_scope=wb_contour_by_date.get(day),
                )
                for day in closed_dates
            }
            for metric_key in (
                "our_wb_unit_cost_rub",
                "proxy_profit_3_rub",
                "proxy_margin_3_pct",
            )
        }
        for metric_key, day_rows in closed_date_coverage.items():
            for day, coverage in day_rows.items():
                _assert(coverage["total"] > 0, f"{metric_key} {day}: rendered cells")
                _assert(
                    coverage["filled"] == coverage["applicable"],
                    f"{metric_key} {day}: all applicable closed-date cells are filled",
                )
        exact_gap_dates: dict[str, dict[str, dict[str, int]]] = {}
        if normalized_acceptance_profile in {
            WAREHOUSE_CHAIN_RECOVERY_PROFILE,
            WAREHOUSE_COST_TRANSPARENCY_PROFILE,
        }:
            exact_gap_dates = {
                metric_key: {
                    day: _metric_date_coverage(
                        page,
                        metric_key=metric_key,
                        day=day,
                        wb_contour_by_scope=wb_contour_by_date.get(day),
                    )
                    for day in ("2026-07-17", "2026-07-18")
                }
                for metric_key in (
                    "our_wb_unit_cost_rub",
                    "proxy_profit_3_rub",
                    "proxy_margin_3_pct",
                )
            }
            for metric_key, day_rows in exact_gap_dates.items():
                for day, coverage in day_rows.items():
                    _assert(coverage["total"] > 0, f"{metric_key} {day}: rendered cells")
                    _assert(
                        coverage["filled"] == coverage["applicable"],
                        f"{metric_key} {day}: no unexplained gaps",
                    )
        archived_metric_keys = (
            "our_wb_cost_confirmed_share_pct",
            "total_our_wb_cost_confirmed_share_pct",
            "proxy_profit_2_rub",
            "total_proxy_profit_2_rub",
            "own_total_paid_equivalent_qty",
            "own_total_confirmed_share_pct",
            "own_inventory_capital_return_pct",
            "own_underaccepted_wb_qty",
        )
        leaked_archived = [
            metric_key
            for metric_key in archived_metric_keys
            if page.locator(f'[data-metric-key="{metric_key}"]').count() > 0
        ]
        _assert(not leaked_archived, f"active vitrina has no archived metric rows: {leaked_archived}")
        canonical_stage_fields = ("qty", "unit_cost_rub", "capital_rub")
        canonical_stage_keys = [
            f"own_capital_{stage}_{field}"
            for stage in (
                "PRODUCTION",
                "PRODUCTION_TO_FF",
                "FF",
                "FF_TO_WB",
                "WB",
                "WB_ACCEPTANCE_DISCREPANCY",
            )
            for field in canonical_stage_fields
        ]
        missing_canonical = [
            metric_key
            for metric_key in canonical_stage_keys
            if page.locator(f'[data-metric-key="{metric_key}"]').count() == 0
        ]
        _assert(not missing_canonical, f"canonical six-stage metric block is complete: {missing_canonical}")
        incident_metric_keys = (
            "wb_stock_fact_qty",
            "wb_stock_incident_qty",
            "wb_stock_effective_qty",
            "total_wb_stock_fact_qty",
            "total_wb_stock_incident_qty",
            "total_wb_stock_effective_qty",
        )
        missing_incident_metrics = [
            metric_key
            for metric_key in incident_metric_keys
            if page.locator(f'[data-metric-config-key="{metric_key}"]').count() == 0
        ]
        _assert(
            not missing_incident_metrics,
            f"stable fact/incident/effective metric family is configurable: {missing_incident_metrics}",
        )
        incident_metric_display = {
            metric_key: page.locator(
                f'[data-metric-config-key="{metric_key}"]'
            ).first.get_attribute("data-metric-display-status")
            for metric_key in incident_metric_keys
        }
        _assert(
            all(value in {"shown", "collapsed", "hidden"} for value in incident_metric_display.values()),
            "incident-aware metric family has stable visibility state",
        )
        unconfirmed_style = page.evaluate(
            """() => {
              const cells = Array.from(document.querySelectorAll("td.cell-server-unconfirmed"));
              const styles = cells.map(cell => {
                const style = getComputedStyle(cell);
                return {
                  backgroundColor: style.backgroundColor,
                  color: style.color,
                  boxShadow: style.boxShadow,
                  title: cell.getAttribute("title") || ""
                };
              });
              const ruleText = Array.from(document.styleSheets).flatMap(sheet => {
                try { return Array.from(sheet.cssRules || []).map(rule => rule.cssText || ""); }
                catch (_) { return []; }
              }).filter(text => text.includes("cell-server-unconfirmed")).join("\\n");
              return {count: cells.length, styles, ruleText};
            }"""
        )
        _assert(
            "background: inherit" in str(unconfirmed_style.get("ruleText") or "").lower(),
            "unconfirmed cells inherit the dark table background instead of a light-yellow fill",
        )
        _assert(
            all(
                not str(item.get("backgroundColor") or "").startswith("rgb(254, 243")
                and not str(item.get("backgroundColor") or "").startswith("rgb(255, 251")
                for item in unconfirmed_style.get("styles") or []
            ),
            "rendered unconfirmed cells have no light-yellow fill",
        )
        incident_cell_evidence = page.evaluate(
            """() => {
              const cells = Array.from(document.querySelectorAll("td.cell-incident-adjusted"));
              return {
                count: cells.length,
                tooltipsComplete: cells.every(cell => {
                  const value = (cell.getAttribute("title") || "").toLocaleLowerCase("ru-RU");
                  return value.includes("факт:")
                    && value.includes("на инцидентных складах:")
                    && value.includes("operational остаток:")
                    && value.includes("revision");
                }),
                styles: cells.slice(0, 10).map(cell => {
                  const style = getComputedStyle(cell);
                  return {color: style.color, boxShadow: style.boxShadow};
                })
              };
            }"""
        )
        _assert(
            not incident_cell_evidence["count"] or incident_cell_evidence["tooltipsComplete"],
            "only incident-derived cells use the distinct marker with complete audit tooltip",
        )
        vitrina_policy_badge = page.locator("[data-vitrina-incident-policy-badge]")
        policy_currently_active = str(incident_policy_evidence.get("badge") or "").startswith(
            "Учитывается политика инцидентов"
        )
        if policy_currently_active:
            _assert(
                vitrina_policy_badge.is_visible()
                and vitrina_policy_badge.inner_text().strip().startswith(
                    "Учитывается политика инцидентов"
                ),
                "Vitrina shows the active incident-policy read-only badge",
            )
        if (
            normalized_acceptance_profile
            == VITRINA_INCIDENT_PROVISIONAL_PROFILE
        ):
            _assert(
                incident_policy_evidence.get("active") is True
                and incident_policy_evidence.get("selected_warehouse_count") == 5
                and incident_policy_evidence.get("effective_from") == "2026-07-25"
                and "Revision 2"
                in str(incident_policy_evidence.get("revision_audit") or ""),
                "seller-level incident policy revision 2 is active from 2026-07-25 "
                "with exactly five selected warehouses",
            )
            vitrina_incident_provisional_evidence = (
                _assert_vitrina_incident_provisional_profile(
                    page,
                    evidence_dir=evidence_dir,
                )
            )
            screenshots.extend(
                vitrina_incident_provisional_evidence.get("screenshots") or []
            )

        sku_quick_popup_evidence: dict[str, Any] = {
            "checked": False,
            "business_post_requests": protected_business_post_requests,
        }
        consumer_screenshots = [
            str(stock_report_screenshot),
            str(sku_screenshot),
            *(
                vitrina_incident_provisional_evidence.get("screenshots")
                or []
            ),
        ]
        if strict_business_acceptance:
            sku_opener = page.locator("[data-open-vitrina-sku]").first
            sku_opener.wait_for(state="visible", timeout=60_000)
            popup_nm_id = sku_opener.get_attribute("data-open-vitrina-sku")
            _assert(bool(popup_nm_id and popup_nm_id.isdigit()), "Vitrina SKU label exposes an exact nmID")
            sku_opener.click()
            sku_modal = page.locator('[data-sku-management-modal][data-sku-modal-state="quick_ready"]')
            sku_modal.wait_for(state="visible", timeout=120_000)
            _assert(
                f"nmID {popup_nm_id}" in sku_modal.inner_text(),
                "Vitrina SKU popup is bound to the clicked exact nmID",
            )
            _assert(
                sku_modal.locator("[data-quick-sku-price]").count() == 1
                and sku_modal.locator("[data-quick-sku-bid-option]").count() == 1
                and sku_modal.locator(".sku-quick-history").count()
                + sku_modal.get_by_text("операций пока нет").count()
                >= 1,
                "Vitrina SKU popup reuses price, exact campaign/placement and per-SKU history surfaces",
            )
            sku_popup_screenshot = evidence_dir / "vitrina_sku_quick_popup.png"
            page.screenshot(path=str(sku_popup_screenshot), full_page=False)
            screenshots.append(str(sku_popup_screenshot))
            consumer_screenshots.append(str(sku_popup_screenshot))
            page.keyboard.press("Escape")
            _assert(sku_modal.is_hidden(), "Escape closes the SKU popup without side effects")
            _assert(
                page.evaluate(
                    "expected => document.activeElement?.getAttribute('data-open-vitrina-sku') === expected",
                    popup_nm_id,
                ),
                "SKU popup restores focus to its Vitrina opener",
            )
            sku_quick_popup_evidence = {
                "checked": True,
                "nm_id": popup_nm_id,
                "price_form": True,
                "exact_bid_selector": True,
                "history_filtered_by_nm_id": True,
                "closed_with_escape": True,
                "business_post_requests": protected_business_post_requests,
                "screenshot": str(sku_popup_screenshot),
            }
        _assert(
            not protected_business_post_requests,
            f"read-only production flow issued no policy/price/bid POST: {protected_business_post_requests}",
        )
        if normalized_acceptance_profile == WAREHOUSE_COST_TRANSPARENCY_PROFILE:
            historical_unavailable_cells = page.locator(
                'td[data-metric-key^="own_capital_"][data-cell-date="2026-07-18"]'
            )
            _assert(historical_unavailable_cells.count() > 0, "18 July warehouse history cells are rendered")
            _assert(
                all(
                    historical_unavailable_cells.nth(index).inner_text().strip() == "—"
                    and historical_unavailable_cells.nth(index)
                    .locator('[aria-label="Исторические данные отсутствуют"]')
                    .count()
                    == 1
                    for index in range(historical_unavailable_cells.count())
                ),
                "18 July unproved warehouse history uses one compact dash with an accessible reason",
            )
            functional_closed_cells = page.locator(
                f'td[data-metric-key^="own_capital_"][data-cell-date="{period_date_to}"]'
            )
            _assert(functional_closed_cells.count() > 0, "last closed warehouse history cells are rendered")
            _assert(
                all(
                    functional_closed_cells.nth(index)
                    .locator('[aria-label="Исторические данные отсутствуют"]')
                    .count()
                    == 0
                    for index in range(functional_closed_cells.count())
                ),
                "last closed functional warehouse date is not presented as a historical gap",
            )
        proxy_screenshot = evidence_dir / "proxy3_vitrina.png"
        page.screenshot(path=str(proxy_screenshot), full_page=False)
        screenshots.append(str(proxy_screenshot))
        consumer_screenshots.append(str(proxy_screenshot))
        consumer_evidence = {
            "stock_report_navigation": True,
            "sku_management_visible": True,
            "sku_management_row_count": sku_dom_summary["row_count"],
            "sku_management_proxy_3_row_count": sku_dom_summary["proxy_3_row_count"],
            "sku_management_visible_proxy_3_row_count": sku_dom_summary["proxy_3_row_count"],
            "proxy_profit_3_visible": True,
            "proxy_margin_3_visible": True,
            "filled_metric_cells_from_2026_07_01": filled_metrics,
            "exact_gap_date_coverage": exact_gap_dates,
            "closed_date_coverage": closed_date_coverage,
            "archived_metric_keys_absent": list(archived_metric_keys),
            "canonical_stage_metric_keys": canonical_stage_keys,
            "incident_metric_display": incident_metric_display,
            "incident_adjusted_cells": incident_cell_evidence,
            "incident_policy_badge": {
                "expected_active": policy_currently_active,
                "visible": vitrina_policy_badge.is_visible(),
                "label": vitrina_policy_badge.inner_text().strip(),
            },
            "vitrina_incident_provisional": (
                vitrina_incident_provisional_evidence
            ),
            "unconfirmed_cell_style": unconfirmed_style,
            "sku_quick_popup": sku_quick_popup_evidence,
            "warehouse_history_unavailable_reason_date": (
                "2026-07-18"
                if normalized_acceptance_profile == WAREHOUSE_COST_TRANSPARENCY_PROFILE
                else None
            ),
            "warehouse_history_last_closed_date": (
                period_date_to
                if normalized_acceptance_profile == WAREHOUSE_COST_TRANSPARENCY_PROFILE
                else None
            ),
            "period_url": period_vitrina_url,
            "screenshots": consumer_screenshots,
        }
        final_url = page.url
        context.close()
        browser.close()

    unexpected_server_errors = [
        item
        for item in server_errors
        if urlparse(str(item.get("url") or "")).path not in set(allowed_server_error_paths)
    ]
    _assert(not unexpected_server_errors, f"5xx responses: {unexpected_server_errors}")
    _assert(not page_errors, f"pageerror: {page_errors}")
    unexpected_console_errors = [
        message for message in console_errors if message not in set(allowed_console_error_messages)
    ]
    _assert(not unexpected_console_errors, f"console errors: {unexpected_console_errors}")
    _assert(not fatal_surface_matches, f"fatal UI surface: {fatal_surface_matches}")
    report = {
        "status": "ok",
        "deployed_sha": normalized_deployed_sha,
        "requested_url": requested_url,
        "final_url": final_url,
        "initial_final_url": initial_final_url,
        "document_status": int(response.status) if response is not None else None,
        "main_navigation_chain": main_navigation_chain,
        "all_document_responses": navigation_chain,
        "title_nonempty": True,
        "body_nonempty": True,
        "cutover": dict(expected_readback.get("cutover") or {}),
        "warehouses": warehouse_evidence,
        "calculation_parameters": settings_evidence,
        "supplier_registry": supplier_evidence,
        "dependent_consumers": consumer_evidence,
        "hourly_sync": dict(expected_readback.get("sync") or {}),
        "recent_warehouse_versions": list(expected_readback.get("recent_versions") or []),
        "official_wb_snapshot": wb_snapshot,
        "warehouse_action_theme": warehouse_action_theme,
        "incident_policy": incident_policy_evidence,
        "recovery_policy": recovery_policy_evidence,
        "business_acceptance": business_acceptance,
        "acceptance_profile": normalized_acceptance_profile or None,
        "historical_wb_cost_projection": dict(expected_readback.get("historical_wb_cost_projection") or {}),
        "legacy_ff_transition": True,
        "legacy_ff_reconciliation": {
            "document_id": str(legacy_ff_expected["document_id"]),
            "sku_count": int(legacy_ff_expected["sku_count"]),
            "total_quantity": str(legacy_ff_expected["total_quantity"]),
            "balance_rows": int(legacy_ff_expected["balance_rows"]),
            "economics_loaded": True,
            "loaded_before_screenshot": True,
        },
        "server_errors": server_errors,
        "unexpected_server_errors": unexpected_server_errors,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "unexpected_console_errors": unexpected_console_errors,
        "fatal_surface_matches": fatal_surface_matches,
        "screenshots": screenshots,
    }
    report_path = evidence_dir / "warehouse_ui_flow_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    report["report_path"] = str(report_path)
    return report


def _assert_vitrina_incident_provisional_profile(
    page: Page,
    *,
    evidence_dir: Path,
) -> dict[str, Any]:
    target_date = "2026-07-25"
    quality_phrase = (
        "Рассчитано по полученному снимку, полнота WB не подтверждена"
    )
    for _index in range(200):
        collapsed = page.locator(
            '[data-metric-anchor-toggle][aria-expanded="false"]'
        )
        if collapsed.count() == 0:
            break
        collapsed.first.click()

    quality_badge = page.locator(
        "[data-vitrina-incident-quality-badge]:not([hidden])"
    )
    quality_badge.wait_for(state="visible", timeout=60_000)
    _assert(
        quality_phrase in str(quality_badge.get_attribute("title") or "")
        and quality_phrase
        in str(quality_badge.get_attribute("aria-label") or ""),
        "provisional quality badge exposes the exact accessible explanation",
    )

    suffixes = (
        "",
        "_central",
        "_northwest",
        "_volga",
        "_south_caucasus",
        "_ural",
        "_far_siberia",
    )

    def _cells(metric_key: str) -> dict[str, tuple[Decimal | None, Any]]:
        locator = page.locator(
            f'td[data-cell-date="{target_date}"]'
            f'[data-metric-key="{metric_key}"]'
        )
        result: dict[str, tuple[Decimal | None, Any]] = {}
        for index in range(locator.count()):
            cell = locator.nth(index)
            row_id = str(cell.get_attribute("data-row-id") or "")
            text = cell.inner_text().strip()
            value = None if text in {"", "—"} else _visible_decimal(text)
            result[_vitrina_incident_projection_scope(row_id)] = (value, cell)
        return result

    family_evidence: dict[str, Any] = {}
    positive_incident_cells = 0
    provisional_filled_cells = 0
    for suffix in suffixes:
        fact_key = f"wb_stock_fact_qty{suffix}"
        incident_key = f"wb_stock_incident_qty{suffix}"
        effective_key = f"wb_stock_effective_qty{suffix}"
        fact_cells = _cells(fact_key)
        incident_cells = _cells(incident_key)
        effective_cells = _cells(effective_key)
        sku_ids = sorted(
            {
                row_id
                for row_id in (
                    set(fact_cells) | set(incident_cells) | set(effective_cells)
                )
                if row_id.startswith("SKU:")
            }
        )
        projected: list[tuple[Decimal, Decimal, Decimal]] = []
        blank_count = 0
        for row_id in sku_ids:
            triple = (
                fact_cells.get(row_id, (None, None))[0],
                incident_cells.get(row_id, (None, None))[0],
                effective_cells.get(row_id, (None, None))[0],
            )
            if triple == (None, None, None):
                blank_count += 1
                continue
            _assert(
                all(value is not None for value in triple),
                f"{row_id} {suffix or 'total'} keeps one complete "
                "fact/incident/effective triple or three blanks",
            )
            fact = Decimal(triple[0])
            incident = Decimal(triple[1])
            effective = Decimal(triple[2])
            _assert(
                min(fact, incident, effective) >= 0
                and incident <= fact
                and effective == fact - incident,
                f"{row_id} {suffix or 'total'} incident arithmetic reconciles",
            )
            projected.append((fact, incident, effective))
            for mapping in (fact_cells, incident_cells, effective_cells):
                cell = mapping[row_id][1]
                classes = str(cell.get_attribute("class") or "")
                title = str(cell.get_attribute("title") or "")
                aria = str(cell.get_attribute("aria-label") or "")
                _assert(
                    "cell-incident-provisional" in classes
                    and quality_phrase in title
                    and quality_phrase in aria,
                    f"{row_id} {suffix or 'total'} exposes provisional quality accessibly",
                )
                provisional_filled_cells += 1
            if incident > 0:
                incident_classes = str(
                    incident_cells[row_id][1].get_attribute("class") or ""
                )
                _assert(
                    "cell-incident-adjusted" in incident_classes,
                    f"{row_id} {suffix or 'total'} positive incident keeps "
                    "the separate blue-violet marker",
                )
                positive_incident_cells += 1

        _assert(
            projected,
            f"{suffix or 'total'} incident family has received-row projections",
        )
        total_fact_cells = _cells("total_" + fact_key)
        total_incident_cells = _cells("total_" + incident_key)
        total_effective_cells = _cells("total_" + effective_key)
        total_triple = (
            total_fact_cells.get("TOTAL", (None, None))[0],
            total_incident_cells.get("TOTAL", (None, None))[0],
            total_effective_cells.get("TOTAL", (None, None))[0],
        )
        _assert(
            all(value is not None for value in total_triple),
            f"{suffix or 'total'} TOTAL triple is filled",
        )
        total_fact = Decimal(total_triple[0])
        total_incident = Decimal(total_triple[1])
        total_effective = Decimal(total_triple[2])
        _assert(
            total_fact == sum(item[0] for item in projected)
            and total_incident == sum(item[1] for item in projected)
            and total_effective == sum(item[2] for item in projected)
            and total_incident <= total_fact
            and total_effective == total_fact - total_incident,
            f"{suffix or 'total'} TOTAL reconciles with available SKU projections",
        )
        family_evidence[suffix or "total"] = {
            "projected_sku_count": len(projected),
            "blank_sku_count": blank_count,
            "fact": str(total_fact),
            "incident": str(total_incident),
            "effective": str(total_effective),
        }

    _assert(
        family_evidence["total"]["projected_sku_count"] == 33,
        "2026-07-25 base fact/incident/effective family covers all 33 accepted SKU",
    )
    _assert(
        positive_incident_cells > 0,
        "2026-07-25 contains positively adjusted incident cells",
    )
    screenshot = evidence_dir / "vitrina_incident_provisional_2026-07-25.png"
    quality_badge.scroll_into_view_if_needed(timeout=60_000)
    page.screenshot(path=str(screenshot), full_page=False)

    for _index in range(200):
        expanded = page.locator(
            '[data-metric-anchor-toggle][aria-expanded="true"]'
        )
        if expanded.count() == 0:
            break
        expanded.first.click()
    return {
        "target_date": target_date,
        "quality_badge": quality_badge.inner_text().strip(),
        "quality_phrase": quality_phrase,
        "family_reconciliation": family_evidence,
        "positive_incident_cell_count": positive_incident_cells,
        "provisional_filled_cell_count": provisional_filled_cells,
        "screenshots": [str(screenshot)],
    }


def _vitrina_incident_projection_scope(row_id: str) -> str:
    return str(row_id).partition("|")[0]


def _warehouse_action_theme_evidence(page: Page) -> dict[str, Any]:
    selectors = {
        "current_source_sync": "[data-warehouse-update-start]",
    }

    def _style(locator: Any) -> dict[str, Any]:
        return dict(
            locator.evaluate(
                """element => {
                    const style = getComputedStyle(element);
                    const rgba = value => {
                      const normalized = String(value || '').trim();
                      const hex = normalized.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
                      if (hex) {
                        const raw = hex[1].length === 3
                          ? hex[1].split('').map(channel => channel + channel).join('')
                          : hex[1];
                        return [
                          parseInt(raw.slice(0, 2), 16),
                          parseInt(raw.slice(2, 4), 16),
                          parseInt(raw.slice(4, 6), 16),
                          1
                        ];
                      }
                      const match = normalized.match(/[\\d.]+/g) || [];
                      const channels = match.slice(0, 4).map(Number);
                      while (channels.length < 3) channels.push(0);
                      if (channels.length < 4) channels.push(1);
                      return channels;
                    };
                    const luminance = value => {
                      const channels = value.slice(0, 3).map(channel => {
                        const normalized = channel / 255;
                        return normalized <= 0.03928
                          ? normalized / 12.92
                          : Math.pow((normalized + 0.055) / 1.055, 2.4);
                      });
                      return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
                    };
                    const foregroundChannels = rgba(style.color);
                    const backgroundChannels = rgba(style.backgroundColor);
                    const panelChannels = rgba(
                      getComputedStyle(document.documentElement).getPropertyValue('--panel-bg')
                    );
                    const alpha = backgroundChannels[3];
                    const effectiveBackground = backgroundChannels.slice(0, 3).map(
                      (channel, index) => channel * alpha + panelChannels[index] * (1 - alpha)
                    );
                    const foreground = luminance(foregroundChannels);
                    const background = luminance(effectiveBackground);
                    return {
                      color: style.color,
                      background: style.backgroundColor,
                      border: style.borderColor,
                      opacity: Number(style.opacity),
                      contrast_ratio: (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05),
                      disabled: Boolean(element.disabled),
                      state: element.getAttribute('data-state') || 'normal'
                    };
                }"""
            )
        )

    evidence: dict[str, Any] = {}
    for key, selector in selectors.items():
        button = page.locator(selector)
        _assert(button.count() == 1, f"warehouse {key} action exists")
        button.evaluate(
            "element => { element.disabled = false; element.setAttribute('data-state', 'normal'); }"
        )
        states: dict[str, dict[str, Any]] = {"normal": _style(button)}
        button.hover()
        page.wait_for_timeout(180)
        states["hover"] = _style(button)
        for state in ("pressed", "loading", "success", "error"):
            button.evaluate(
                "(element, state) => { element.disabled = state === 'loading'; element.setAttribute('data-state', state); }",
                state,
            )
            page.wait_for_timeout(180)
            states[state] = _style(button)
        button.evaluate(
            "element => { element.disabled = true; element.setAttribute('data-state', 'normal'); }"
        )
        page.wait_for_timeout(180)
        states["disabled"] = _style(button)
        button.evaluate(
            "element => { element.disabled = false; element.setAttribute('data-state', 'normal'); }"
        )
        _assert(states["normal"]["background"] != "rgb(255, 255, 255)", f"warehouse {key} dark-theme background")
        _assert(states["hover"]["background"] != states["normal"]["background"], f"warehouse {key} hover state")
        _assert(states["pressed"]["background"] != states["hover"]["background"], f"warehouse {key} pressed state")
        _assert(states["loading"]["disabled"] is True, f"warehouse {key} loading state")
        _assert(states["disabled"]["opacity"] < states["normal"]["opacity"], f"warehouse {key} disabled state")
        for state in ("normal", "hover", "pressed", "loading", "success", "error"):
            _assert(
                float(states[state]["contrast_ratio"]) >= 3.0,
                f"warehouse {key} {state} text contrast",
            )
        evidence[key] = states
    return evidence


def _assert_business_warehouse_acceptance(
    details: Mapping[str, Mapping[str, Any]],
    *,
    acceptance_profile: str | None,
) -> dict[str, Any]:
    _assert(set(details) == {key for key, _ in WAREHOUSES}, "business acceptance has all six warehouse details")
    production_rows = list(details["production"].get("balances") or [])
    recovery_profile = acceptance_profile == WAREHOUSE_CHAIN_RECOVERY_PROFILE
    cost_transparency_profile = acceptance_profile == WAREHOUSE_COST_TRANSPARENCY_PROFILE
    fixed_cost_controls = recovery_profile or cost_transparency_profile
    expected_identities = (
        {
            1221231049: "(Anti-Spy) iPhone 18 Pro",
            1221235702: "(Anti-Spy) iPhone 18 Pro Max",
            1221244040: "(Matte) iPhone 18 Pro",
            1221249681: "(Matte) iPhone 18 Pro Max",
        }
        if recovery_profile
        else {}
    )
    production_by_nm = {int(item.get("nm_id") or 0): dict(item) for item in production_rows}
    for nm_id, name in expected_identities.items():
        row = production_by_nm.get(nm_id)
        _assert(row is not None, f"production nmID {nm_id} exists")
        _assert(str((row or {}).get("nomenclature_name") or "") == name, f"production nmID {nm_id} exact name")
        _assert(bool(str((row or {}).get("barcode") or "")), f"production nmID {nm_id} barcode")
        _assert(
            str((row or {}).get("identity_source") or "") == "active_nomenclature_exact_nm_id",
            f"production nmID {nm_id} stable identity source",
        )
    unexplained = [
        int(item.get("nm_id") or 0)
        for item in production_rows
        if Decimal(str(item.get("quantity") or 0)) > 0
        and (
            not str(item.get("nomenclature_name") or "").strip()
            or not str(item.get("barcode") or "").strip()
            or str(item.get("identity_source") or "") == "nm_id"
        )
    ]
    _assert(not unexplained, f"production has no unexplained positive-quantity SKU: {unexplained}")

    arithmetic: dict[str, dict[str, Any]] = {}
    for warehouse_key, _ in WAREHOUSES:
        rows = list(details[warehouse_key].get("balances") or [])
        mismatches = []
        for item in rows:
            quantity = Decimal(str(item.get("quantity") or 0))
            capital = Decimal(str(item.get("capital_rub") or 0))
            wac = Decimal(str(item.get("wac_rub") or 0))
            if quantity > 0 and (wac <= 0 or abs(quantity * wac - capital) > Decimal("0.02")):
                mismatches.append(int(item.get("nm_id") or 0))
        _assert(not mismatches, f"{warehouse_key}: SKU capital = quantity × WAC")
        summary = dict(details[warehouse_key].get("warehouse") or {})
        total_quantity = Decimal(str(summary.get("total_quantity") or 0))
        total_capital = Decimal(str(summary.get("total_capital_rub") or 0))
        total_wac = Decimal(str(summary.get("average_unit_cost_rub") or 0))
        if total_quantity > 0:
            _assert(abs(total_quantity * total_wac - total_capital) < Decimal("0.02"), f"{warehouse_key}: warehouse WAC identity")
        arithmetic[warehouse_key] = {
            "quantity": str(total_quantity),
            "capital_rub": str(total_capital),
            "wac_rub": str(total_wac) if total_quantity > 0 else None,
            "sku_count": len(rows),
        }

    china_rows = list(details["china_to_ff"].get("balances") or [])
    china_wacs = [Decimal(str(item.get("wac_rub") or 0)) for item in china_rows if Decimal(str(item.get("quantity") or 0)) > 0]
    if fixed_cost_controls:
        _assert(china_wacs and min(china_wacs) < Decimal("84"), "China → FF retains lower-cost confirmed party rows")
        _assert(max(china_wacs) >= Decimal("121"), "China → FF retains arithmetically supported 121–130 ₽ rows")

    ff_control = next(
        (dict(item) for item in details["ff"].get("balances") or [] if int(item.get("nm_id") or 0) == 391662965),
        None,
    )
    if fixed_cost_controls:
        _assert(ff_control is not None, "FF control nmID 391662965 exists")
        _assert(Decimal(str((ff_control or {}).get("quantity") or 0)) == Decimal("6750"), "FF control quantity")
        _assert(abs(Decimal(str((ff_control or {}).get("wac_rub") or 0)) - Decimal("119.9415482137855")) < Decimal("0.0000001"), "FF control WAC")
        _assert(abs(Decimal(str((ff_control or {}).get("capital_rub") or 0)) - Decimal("809605.450443052125")) < Decimal("0.0001"), "FF control capital")

    ff_to_wb = dict(details["ff_to_wb"].get("warehouse") or {})
    if fixed_cost_controls:
        _assert(Decimal(str(ff_to_wb.get("total_quantity") or 0)) == 0, "FF → WB expected zero")
        _assert(
            not any(Decimal(str(item.get("quantity") or 0)) == Decimal("31500") for item in details["ff_to_wb"].get("balances") or []),
            "FF → WB has no stuck 31,500 units",
        )
    return {
        "acceptance_profile": acceptance_profile,
        "resolved_production_nm_ids": {str(key): value for key, value in expected_identities.items()},
        "unexplained_production_nm_ids": unexplained,
        "warehouse_arithmetic": arithmetic,
        "china_to_ff_wac_min": str(min(china_wacs)) if china_wacs else None,
        "china_to_ff_wac_max": str(max(china_wacs)) if china_wacs else None,
        "ff_control_391662965": (
            {
                "quantity": str((ff_control or {}).get("quantity") or 0),
                "wac_rub": str((ff_control or {}).get("wac_rub") or 0),
                "capital_rub": str((ff_control or {}).get("capital_rub") or 0),
            }
            if fixed_cost_controls
            else None
        ),
        "ff_to_wb_quantity": str(ff_to_wb.get("total_quantity") or 0),
    }


def _visible_decimal(value: str) -> Decimal:
    normalized = str(value or "").replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
    try:
        return Decimal(normalized)
    except Exception as exc:
        raise AssertionError(f"visible value is not numeric: {value!r}") from exc


def _settings_frame_locator(page: Page) -> FrameLocator:
    frame = page.locator("[data-settings-embed-frame]")
    frame.wait_for(state="visible", timeout=60_000)
    page.wait_for_function(
        "Boolean(document.querySelector('[data-settings-embed-frame]')?.getAttribute('src'))",
        timeout=60_000,
    )
    surface = page.frame_locator("[data-settings-embed-frame]")
    surface.locator("body").wait_for(timeout=60_000)
    surface.locator('html[data-settings-ready="true"]').wait_for(timeout=60_000)
    return surface


def _supplier_financial_detail_url(base_url: str, shipment_id: str) -> str:
    return _supplier_detail_url(base_url, shipment_id, tab="documents")


def _supplier_detail_url(base_url: str, shipment_id: str, *, tab: str) -> str:
    return (
        str(base_url).rstrip("/")
        + "/sheet-vitrina-v1/supplier?embedded=operator&shipment_id="
        + quote(str(shipment_id), safe="")
        + "&tab="
        + quote(str(tab), safe="")
    )


def _stock_report_frame_locator(page: Page) -> FrameLocator:
    frame = page.locator('[data-warehouse-stock-report-frame]:not([hidden])')
    frame.wait_for(timeout=60_000)
    page.wait_for_function(
        "Boolean(document.querySelector('[data-warehouse-stock-report-frame]')?.getAttribute('src'))",
        timeout=60_000,
    )
    surface = page.frame_locator("[data-warehouse-stock-report-frame]")
    surface.locator("body").wait_for(timeout=60_000)
    return surface


def _visible_money(value: str) -> Decimal:
    normalized = (
        str(value or "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("₽", "")
        .replace("RUB", "")
        .replace("CNY", "")
        .replace(",", ".")
    )
    try:
        return Decimal(normalized)
    except Exception as exc:
        raise AssertionError(f"visible money value is not numeric: {value!r}") from exc


def _filled_metric_cells(page: Page, *, metric_key: str, date_from: str) -> int:
    cells = page.locator(f'td[data-metric-key="{metric_key}"][data-cell-date]')
    filled = 0
    for index in range(cells.count()):
        cell = cells.nth(index)
        if str(cell.get_attribute("data-cell-date") or "") < date_from:
            continue
        text = cell.inner_text().strip()
        if text and text not in {"—", "-"}:
            filled += 1
    return filled


def _metric_date_coverage(
    page: Page,
    *,
    metric_key: str,
    day: str,
    wb_contour_by_scope: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    cells = page.locator(
        f'td[data-metric-key="{metric_key}"][data-cell-date="{day}"]'
    )
    filled = 0
    applicable = 0
    for index in range(cells.count()):
        cell = cells.nth(index)
        row_id = str(cell.get_attribute("data-row-id") or "")
        scope_id = row_id.rsplit("|", 1)[0]
        if metric_key == "our_wb_unit_cost_rub":
            contour = dict(wb_contour_by_scope or {})
            _assert(
                scope_id in contour,
                f"{metric_key} {day}: canonical WB contour quantity for {scope_id}",
            )
            applicability_value = _visible_optional_decimal(str(contour[scope_id]))
        else:
            applicability_metric = "total_orderSum" if scope_id == "TOTAL" else "orderSum"
            applicability_cell = page.locator(
                f'td[data-row-id="{scope_id}|{applicability_metric}"]'
                f'[data-cell-date="{day}"]'
            )
            _assert(
                applicability_cell.count() == 1,
                f"{metric_key} {day}: applicability source {scope_id}|{applicability_metric}",
            )
            applicability_value = _visible_optional_decimal(applicability_cell.inner_text())
        is_applicable = applicability_value is not None and (
            applicability_value > 0
            if metric_key == "our_wb_unit_cost_rub"
            else not (metric_key == "proxy_margin_3_pct" and applicability_value == 0)
        )
        if not is_applicable:
            continue
        applicable += 1
        if _visible_optional_decimal(cell.inner_text()) is not None:
            filled += 1
    return {
        "total": cells.count(),
        "applicable": applicable,
        "inapplicable": cells.count() - applicable,
        "filled": filled,
    }


def _visible_optional_decimal(value: str) -> Decimal | None:
    normalized = (
        str(value or "")
        .replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("₽", "")
        .replace("%", "")
        .replace(",", ".")
        .strip()
    )
    if normalized in {"", "—", "-", "Историческиеданныеотсутствуют"}:
        return None
    try:
        return Decimal(normalized)
    except Exception as exc:
        raise AssertionError(f"visible metric value is not numeric: {value!r}") from exc


def _allocated_amount_matches_eligible(
    eligible_amount: object,
    allocated_amount: object,
) -> bool:
    """Compare serialized Decimal allocations without requiring scale-identical text."""

    try:
        eligible = Decimal(str(eligible_amount))
        allocated = Decimal(str(allocated_amount))
    except Exception:
        return False
    return abs(eligible - allocated) < Decimal("0.000001")


def _assert_warehouse_balance_cardinality(
    *,
    warehouse_key: str,
    expected_sku_count: int,
    detail_balances: list[Mapping[str, Any]],
    visible_balance_count: int,
    warehouse_name: str,
) -> None:
    """Keep physical SKU totals distinct from visible FF reservation-only rows."""

    if warehouse_key == "ff":
        physical_balance_count = sum(
            1
            for item in detail_balances
            if Decimal(str(item.get("quantity") or 0)) > 0
        )
        _assert(
            physical_balance_count == expected_sku_count,
            f"{warehouse_name}: physical SKU count",
        )
    else:
        _assert(
            visible_balance_count == expected_sku_count,
            f"{warehouse_name}: balance row count",
        )
    _assert(
        visible_balance_count == len(detail_balances),
        f"{warehouse_name}: UI/detail balance rows",
    )


def _registry_cell_display(
    registry: Mapping[str, Any],
    *,
    section_id: str,
    row_id: str,
    shipment_id: str,
) -> str:
    for section in registry.get("sections") or []:
        if str((section or {}).get("section_id") or "") != section_id:
            continue
        for row in (section or {}).get("rows") or []:
            if str((row or {}).get("row_id") or "") != row_id:
                continue
            cell = ((row or {}).get("cells") or {}).get(shipment_id) or {}
            return str((cell or {}).get("display") or "")
    return ""


def _assert_supplier_cost_transparency_profile(
    *,
    page: Page,
    context: Any,
    base_url: str,
    registry_payload: Mapping[str, Any],
    evidence_dir: Path,
) -> dict[str, Any]:
    """Verify the current exact supplier-cost controls when explicitly selected."""

    control_column = next(
        (
            dict(item)
            for item in registry_payload.get("columns") or []
            if str((item or {}).get("invoice_no") or "") == "26GN390"
        ),
        None,
    )
    _assert(control_column is not None, "26GN390 supplier registry column")
    control_shipment_id = str((control_column or {}).get("shipment_id") or "")
    control_detail = _protected_json_get(
        context,
        base_url
        + "/v1/sheet-vitrina-v1/supply/supplier-shipments/"
        + quote(control_shipment_id, safe=""),
        label="26GN390 supplier detail API",
    )
    control_breakdown = dict(control_detail.get("supplier_cost_breakdown") or {})
    control_component_totals = _supplier_component_totals(control_breakdown)
    _assert(
        control_component_totals.get("supplier_payment") == Decimal("5724403.57"),
        "26GN390 supplier payment enters capital once",
    )
    _assert(
        control_component_totals.get("bank_fee") == Decimal("120899.32"),
        "26GN390 bank fees enter capital once",
    )
    _assert(
        sum(
            (
                amount
                for key, amount in control_component_totals.items()
                if key not in {"supplier_payment", "bank_fee"}
            ),
            Decimal("0"),
        )
        == Decimal("3256828.23"),
        "26GN390 China to FF expenses enter capital once",
    )
    control_conservation = dict(control_breakdown.get("controls") or {})
    _assert(
        all(
            bool(control_conservation.get(key))
            for key in (
                "document_allocation_conserved",
                "document_counted_once",
                "line_components_equal_capital",
                "shipment_lines_equal_capital",
            )
        ),
        "26GN390 allocation conservation controls",
    )
    control_document_controls = list(control_breakdown.get("document_controls") or [])
    _assert(
        bool(control_document_controls)
        and all(
            bool(item.get("conserved"))
            and _allocated_amount_matches_eligible(
                item.get("eligible_amount_rub"),
                item.get("allocated_amount_rub"),
            )
            and not list(item.get("incomplete_reasons") or [])
            for item in control_document_controls
        ),
        "26GN390 every cost-affecting document is fully allocated",
    )
    control_lines = {
        int(item.get("nm_id") or 0): dict(item)
        for item in control_breakdown.get("lines") or []
    }
    expected_anti_spy = {
        391660889: (Decimal("4500"), Decimal("586960.7448827740481998376819")),
        391661710: (Decimal("5250"), Decimal("684787.5356965697228998106288")),
    }
    for nm_id, (expected_qty, expected_capital) in expected_anti_spy.items():
        line = control_lines.get(nm_id)
        _assert(line is not None, f"26GN390 canonical cost line {nm_id}")
        _assert(
            Decimal(str((line or {}).get("quantity") or 0)) == expected_qty,
            f"26GN390 quantity {nm_id}",
        )
        _assert(
            abs(Decimal(str((line or {}).get("capital_rub") or 0)) - expected_capital)
            < Decimal("0.000001"),
            f"26GN390 capital {nm_id}",
        )
        _assert(
            abs(
                Decimal(str((line or {}).get("unit_cost_rub") or 0))
                - Decimal("130.4357210850608995999639293")
            )
            < Decimal("1e-20"),
            f"26GN390 WAC {nm_id}",
        )
        _assert(
            len((line or {}).get("components") or []) >= 7,
            f"26GN390 document components {nm_id}",
        )
    _assert(
        bool((control_breakdown.get("certification") or {}).get("certified")),
        "26GN390 source/calculation fingerprints are certified",
    )
    control_response = page.goto(
        _supplier_detail_url(base_url, control_shipment_id, tab="supply"),
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    _assert(
        control_response is not None and control_response.status == 200,
        "26GN390 supplier detail page status",
    )
    page.locator("#shipmentCard:not([hidden])").wait_for(timeout=60_000)
    for nm_id in expected_anti_spy:
        row = page.locator("#productLines tr").filter(
            has=page.locator(f'input[data-authoritative-nmid][value="{nm_id}"]')
        )
        _assert(row.count() == 1, f"26GN390 visible row {nm_id}")
        _assert(
            "130,44" in row.locator(".line-cost-value").inner_text(),
            f"26GN390 visible WAC {nm_id}",
        )
        _assert(
            row.locator(".line-cost-status").inner_text().strip()
            == "Все расходы учтены",
            f"26GN390 green status {nm_id}",
        )
        row.locator("details.line-cost-proof").click()
        _assert(
            row.get_by_text("Контроль:", exact=False).count() >= 1,
            f"26GN390 human proof {nm_id}",
        )
    control_screenshot = evidence_dir / "supplier_26GN390_anti_spy_cost_proof.png"
    page.screenshot(path=str(control_screenshot), full_page=True)

    payment_control_column = next(
        (
            dict(item)
            for item in registry_payload.get("columns") or []
            if str((item or {}).get("invoice_no") or "") == "26GN310"
        ),
        None,
    )
    _assert(payment_control_column is not None, "26GN310 supplier registry column")
    payment_control_shipment_id = str(
        (payment_control_column or {}).get("shipment_id") or ""
    )
    payment_control_detail = _protected_json_get(
        context,
        base_url
        + "/v1/sheet-vitrina-v1/supply/supplier-shipments/"
        + quote(payment_control_shipment_id, safe=""),
        label="26GN310 supplier detail API",
    )
    payment_control_breakdown = dict(
        payment_control_detail.get("supplier_cost_breakdown") or {}
    )
    payment_control_component_totals = _supplier_component_totals(
        payment_control_breakdown
    )
    _assert(
        payment_control_component_totals.get("supplier_payment")
        == Decimal("8633999.78"),
        "26GN310 supplier payment enters capital once",
    )
    _assert(
        payment_control_component_totals.get("bank_fee")
        == Decimal("182350.10"),
        "26GN310 bank fees enter capital once",
    )
    _assert(
        sum(
            (
                amount
                for key, amount in payment_control_component_totals.items()
                if key not in {"supplier_payment", "bank_fee"}
            ),
            Decimal("0"),
        )
        == Decimal("4108486.60"),
        "26GN310 China to FF expenses enter capital once",
    )
    _assert(
        Decimal(str(payment_control_breakdown.get("quantity") or 0))
        == Decimal("116250"),
        "26GN310 canonical quantity",
    )
    _assert(
        abs(
            Decimal(str(payment_control_breakdown.get("capital_rub") or 0))
            - Decimal("12924836.48")
        )
        < Decimal("0.01"),
        "26GN310 canonical capital",
    )
    _assert(
        abs(
            Decimal(
                str(payment_control_breakdown.get("average_unit_cost_rub") or 0)
            )
            - Decimal("111.1813890752688172043010753")
        )
        < Decimal("1e-20"),
        "26GN310 canonical WAC",
    )
    payment_controls = dict(payment_control_breakdown.get("controls") or {})
    _assert(
        all(
            bool(payment_controls.get(key))
            for key in (
                "document_allocation_conserved",
                "document_counted_once",
                "line_components_equal_capital",
                "shipment_lines_equal_capital",
            )
        ),
        "26GN310 conservation controls",
    )
    payment_document_controls = list(
        payment_control_breakdown.get("document_controls") or []
    )
    _assert(
        bool(payment_document_controls)
        and all(
            bool(item.get("conserved"))
            and _allocated_amount_matches_eligible(
                item.get("eligible_amount_rub"),
                item.get("allocated_amount_rub"),
            )
            and not list(item.get("incomplete_reasons") or [])
            for item in payment_document_controls
        ),
        "26GN310 every cost-affecting document is fully allocated",
    )
    _assert(
        bool(
            (payment_control_breakdown.get("certification") or {}).get(
                "certified"
            )
        ),
        "26GN310 source/calculation fingerprints are certified",
    )
    payment_control_response = page.goto(
        _supplier_detail_url(base_url, payment_control_shipment_id, tab="supply"),
        wait_until="domcontentloaded",
        timeout=120_000,
    )
    _assert(
        payment_control_response is not None
        and payment_control_response.status == 200,
        "26GN310 supplier detail page status",
    )
    page.locator("#shipmentCard:not([hidden])").wait_for(timeout=60_000)
    payment_statuses = page.locator("#productLines .line-cost-status")
    _assert(
        payment_statuses.count() > 0,
        "26GN310 visible cost statuses",
    )
    for index in range(payment_statuses.count()):
        _assert(
            payment_statuses.nth(index).inner_text().strip()
            == "Все расходы учтены",
            f"26GN310 green status row {index + 1}",
        )
    page.locator("#productLines details.line-cost-proof").first.click()
    _assert(
        page.locator("#productLines").get_by_text("Контроль:", exact=False).count()
        >= 1,
        "26GN310 human cost proof",
    )
    payment_control_screenshot = evidence_dir / "supplier_26GN310_payment_cost_proof.png"
    page.screenshot(path=str(payment_control_screenshot), full_page=True)
    return {
        "control_26GN390": {
            "shipment_id": control_shipment_id,
            "source_fingerprint": control_breakdown.get("source_fingerprint"),
            "calculation_fingerprint": control_breakdown.get("calculation_fingerprint"),
            "certification": control_breakdown.get("certification"),
            "component_totals_rub": {
                key: str(value) for key, value in control_component_totals.items()
            },
            "controls": control_conservation,
            "document_controls": control_document_controls,
            "lines": {
                str(key): value
                for key, value in control_lines.items()
                if key in expected_anti_spy
            },
            "screenshot": str(control_screenshot),
        },
        "control_26GN310": {
            "shipment_id": payment_control_shipment_id,
            "quantity": payment_control_breakdown.get("quantity"),
            "capital_rub": payment_control_breakdown.get("capital_rub"),
            "average_unit_cost_rub": payment_control_breakdown.get(
                "average_unit_cost_rub"
            ),
            "source_fingerprint": payment_control_breakdown.get(
                "source_fingerprint"
            ),
            "calculation_fingerprint": payment_control_breakdown.get(
                "calculation_fingerprint"
            ),
            "certification": payment_control_breakdown.get("certification"),
            "controls": payment_controls,
            "document_controls": payment_document_controls,
            "component_totals_rub": {
                key: str(value)
                for key, value in payment_control_component_totals.items()
            },
            "screenshot": str(payment_control_screenshot),
        },
        "screenshots": [str(control_screenshot), str(payment_control_screenshot)],
    }


def _supplier_component_totals(breakdown: Mapping[str, Any]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for item in breakdown.get("component_controls") or []:
        key = str((item or {}).get("component_key") or "").strip()
        if not key:
            continue
        result[key] = result.get(key, Decimal("0")) + Decimal(
            str((item or {}).get("source_amount_rub") or 0)
        )
    return result


def _protected_json_get(
    context: Any,
    url: str,
    *,
    label: str,
    timeout_ms: int = 60_000,
    attempts: int = 3,
) -> dict[str, Any]:
    """Read a protected API with bounded retry and secret-safe failures."""

    path = urlparse(str(url)).path
    for attempt in range(1, attempts + 1):
        try:
            response = context.request.get(
                url,
                headers={"Accept": "application/json"},
                timeout=timeout_ms,
            )
            if response.status == 200:
                payload = response.json()
                _assert(isinstance(payload, dict), f"{label}: JSON object")
                return payload
            if response.status < 500 or attempt == attempts:
                raise AssertionError(
                    f"{label}: HTTP {response.status} for {path}"
                )
        except PlaywrightError:
            if attempt == attempts:
                raise AssertionError(
                    f"{label}: transport failed after {attempts} attempts for {path}"
                ) from None
        time.sleep(0.4 * attempt)
    raise AssertionError(f"{label}: unavailable for {path}")


def _sku_management_dom_summary(
    page: Any,
    *,
    source_rows: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Reconcile rendered Proxy 3 cells with the page's original protected response."""

    status_text = page.locator("[data-sku-management-status]").inner_text().strip()
    match = re.match(r"^SKU:\s*(\d+)(?:\s*·|$)", status_text)
    _assert(match is not None, "SKU management loaded status")
    expected_row_count = int(match.group(1))
    source_nm_ids: list[str] = []
    source_proxy_3_nm_ids: set[str] = set()
    for item in source_rows:
        nm_id = str(int(item.get("nm_id") or 0))
        _assert(nm_id != "0", "SKU management source nmID")
        source_nm_ids.append(nm_id)
        profit_filled = item.get("profit_rub") is not None
        margin_filled = item.get("margin_pct") is not None
        _assert(
            profit_filled == margin_filled,
            "SKU management source Proxy 3 profit/margin applicability is aligned",
        )
        if profit_filled:
            source_proxy_3_nm_ids.add(nm_id)
    _assert(
        len(source_nm_ids) == len(set(source_nm_ids)),
        "SKU management source nmID uniqueness",
    )
    _assert(
        expected_row_count == len(source_nm_ids) and expected_row_count > 0,
        "SKU management status/source row count",
    )
    rows = page.locator("[data-sku-row-nm-id]")
    row_count = rows.count()
    _assert(row_count == expected_row_count and row_count > 0, "SKU management rendered row count")
    rendered_nm_ids: set[str] = set()
    visible_proxy_3_nm_ids: set[str] = set()
    for index in range(row_count):
        row = rows.nth(index)
        nm_id = str(row.get_attribute("data-sku-row-nm-id") or "").strip()
        _assert(nm_id and nm_id not in rendered_nm_ids, "SKU management rendered nmID uniqueness")
        rendered_nm_ids.add(nm_id)
        profit_cell = row.locator('[data-sku-cell="profit_rub"]')
        margin_cell = row.locator('[data-sku-cell="margin_pct"]')
        _assert(
            profit_cell.count() == 1 and margin_cell.count() == 1,
            "SKU management Proxy 3 consumer cell cardinality",
        )
        profit_text = profit_cell.inner_text().strip()
        margin_text = margin_cell.inner_text().strip()
        profit_filled = profit_text not in {"", "—", "-"}
        margin_filled = margin_text not in {"", "—", "-"}
        _assert(
            profit_filled == margin_filled,
            "SKU management Proxy 3 profit/margin applicability is aligned",
        )
        if profit_filled:
            visible_proxy_3_nm_ids.add(nm_id)
    _assert(
        rendered_nm_ids == set(source_nm_ids),
        "SKU management source/rendered nmID completeness",
    )
    _assert(
        visible_proxy_3_nm_ids == source_proxy_3_nm_ids,
        "SKU management source/rendered Proxy 3 completeness",
    )
    _assert(visible_proxy_3_nm_ids, "SKU management consumes populated Proxy 3")
    return {
        "status": status_text,
        "row_count": row_count,
        "proxy_3_row_count": len(visible_proxy_3_nm_ids),
    }


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
