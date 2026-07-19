#!/usr/bin/env python3
"""Authenticated read-only Playwright acceptance for production autoanswers UI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright


UI_PATH = "/sheet-vitrina-v1/vitrina?tab=feedbacks"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _json_get(context: BrowserContext, url: str, *, label: str) -> dict[str, Any]:
    response = context.request.get(url, headers={"Accept": "application/json"}, timeout=120_000)
    _assert(response.status == 200, f"{label}: expected HTTP 200, got {response.status}")
    payload = response.json()
    _assert(isinstance(payload, dict), f"{label}: expected JSON object")
    return payload


def _validate_feedback_item(item: dict[str, Any]) -> None:
    for name in (
        "id",
        "content_version",
        "content_version_hash",
        "wb_observation_hash",
        "processing_status",
        "publication_status",
        "has_photo",
        "has_video",
    ):
        _assert(name in item, f"local feedback item misses {name}")
    _assert(isinstance(item.get("productDetails"), dict), "local feedback productDetails must be an object")
    _assert(isinstance(item.get("answer"), dict), "local feedback answer must be an object")


def run_autoanswers_ui_flow(
    *,
    base_url: str,
    auth_cookie: str,
    evidence_dir: Path,
    headless: bool = True,
) -> dict[str, Any]:
    normalized_base_url = str(base_url or "").strip().rstrip("/")
    parsed = urlparse(normalized_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("autoanswers UI flow requires an absolute http(s) base URL")
    cookie_name, separator, cookie_value = str(auth_cookie or "").partition("=")
    if separator != "=" or cookie_name != "wb_core_web_session" or not cookie_value:
        raise ValueError("autoanswers UI flow requires a valid app-session cookie")
    evidence_dir = evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)

    requested_url = normalized_base_url + UI_PATH
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    navigation_chain: list[dict[str, Any]] = []
    fatal_surface_matches: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
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
        page: Page = context.new_page()
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on(
            "console",
            lambda message: console_errors.append(message.text) if message.type == "error" else None,
        )
        page.on(
            "response",
            lambda response: server_errors.append(
                {
                    "status": response.status,
                    "url": response.url,
                    "resource_type": response.request.resource_type,
                }
            )
            if response.status >= 500
            else None,
        )
        page.on(
            "response",
            lambda response: navigation_chain.append({"status": response.status, "url": response.url})
            if response.request.resource_type == "document"
            else None,
        )

        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(response is not None and response.status == 200, "autoanswers document must return HTTP 200")
        page.locator('[data-unified-tab-panel="feedbacks"]:not([hidden])').wait_for(timeout=60_000)
        page.locator('[data-feedbacks-subpanel="server-reviews"]:not([hidden])').wait_for(timeout=60_000)
        page.wait_for_function(
            "document.querySelector('[data-autoanswers-master-status]').textContent.trim() === 'Автоответы выключены'",
            timeout=60_000,
        )
        page.wait_for_function(
            "document.querySelector('[data-autoanswers-settings-note]').textContent.includes('WB_AUTOANSWERS_FORCE_OFF=true')",
            timeout=60_000,
        )
        page.wait_for_function(
            "document.querySelector('[data-autoanswers-list-meta]').textContent.includes('Найдено:')",
            timeout=60_000,
        )

        settings = _json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings",
            label="autoanswers settings",
        )
        setting_values = dict(settings.get("settings") or {})
        _assert(setting_values.get("master_enabled") is False, "persisted master-switch must be OFF")
        _assert(setting_values.get("force_off") is True, "production force-off must be true")
        _assert(setting_values.get("effective_enabled") is False, "effective mode must be OFF")
        _assert(str(setting_values.get("mode") or "") == "draft_only", "default persisted mode must be draft_only")
        _assert(page.locator("[data-autoanswers-toggle]").is_disabled(), "master control must be disabled by force-off")
        _assert(page.locator("[data-autoanswers-mode]").is_disabled(), "mode control must be disabled by force-off")
        _assert(page.locator("[data-autoanswers-save-mode]").is_disabled(), "mode save must be disabled by force-off")
        _assert(page.locator("[data-autoanswers-backlog]").is_disabled(), "backlog control must be disabled by force-off")
        filter_names = (
            "unanswered",
            "status",
            "rating",
            "route",
            "sku",
            "date_from",
            "date_to",
            "flag",
        )
        for filter_name in filter_names:
            _assert(
                page.locator(f'[data-autoanswers-filter="{filter_name}"]').count() == 1,
                f"feedback filter {filter_name} must be rendered exactly once",
            )

        first = _json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/feedbacks/local?page=1&page_size=50",
            label="feedbacks page 1",
        )
        _assert(first.get("contract_name") == "sheet_vitrina_v1_feedbacks_local", "local contract identity")
        items = list(first.get("items") or [])
        _assert(int(first.get("page_size") or 0) == 50, "default server page size must be 50")
        _assert(len(items) <= 50, "first page must be bounded to 50")
        first_ids = [str(item.get("id") or "") for item in items]
        _assert(len(first_ids) == len(set(first_ids)), "first page contains duplicate feedback IDs")
        for item in items:
            _validate_feedback_item(dict(item))
        ui_rows = page.locator("[data-autoanswers-result] .autoanswers-table tbody tr").count()
        _assert(ui_rows == len(items), "visible review row count must match local page 1")

        second = _json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/feedbacks/local?page=2&page_size=50",
            label="feedbacks page 2",
        )
        second_ids = [str(item.get("id") or "") for item in second.get("items") or []]
        _assert(len(second_ids) == len(set(second_ids)), "second page contains duplicate feedback IDs")
        _assert(set(first_ids).isdisjoint(second_ids), "server pagination repeats feedback IDs")

        filter_checks: dict[str, int] = {}

        def filtered_feedbacks(label: str, **query: Any) -> list[dict[str, Any]]:
            payload = _json_get(
                context,
                normalized_base_url
                + "/v1/sheet-vitrina-v1/feedbacks/local?"
                + urlencode({"page": 1, "page_size": 50, **query}),
                label=f"feedback filter {label}",
            )
            rows = [dict(row) for row in payload.get("items") or []]
            for row in rows:
                _validate_feedback_item(row)
            filter_checks[label] = len(rows)
            return rows

        unanswered_rows = filtered_feedbacks("unanswered", unanswered="true")
        _assert(
            all(not str((row.get("answer") or {}).get("text") or "").strip() for row in unanswered_rows),
            "unanswered filter returned a feedback with an existing answer",
        )
        for media_filter in ("has_photo", "has_video"):
            media_rows = filtered_feedbacks(media_filter, **{media_filter: "true"})
            _assert(
                all(bool(row.get(media_filter)) for row in media_rows),
                f"{media_filter} filter returned a non-matching feedback",
            )
        for flag_filter in ("needs_review", "published", "error"):
            filtered_feedbacks(flag_filter, **{flag_filter: "true"})
        if items:
            sample = dict(items[0])
            rating = int(sample.get("productValuation") or 0)
            if 1 <= rating <= 5:
                rating_rows = filtered_feedbacks("rating", rating=rating)
                _assert(
                    all(int(row.get("productValuation") or 0) == rating for row in rating_rows),
                    "rating filter returned a non-matching feedback",
                )
            sample_date = str(sample.get("createdDate") or "")[:10]
            if sample_date:
                date_rows = filtered_feedbacks("period", date_from=sample_date, date_to=sample_date)
                _assert(
                    all(str(row.get("createdDate") or "")[:10] == sample_date for row in date_rows),
                    "period filter returned a feedback outside the requested date",
                )
            product = dict(sample.get("productDetails") or {})
            sku = str(product.get("nm_id") or product.get("supplier_article") or "").strip()
            if sku:
                sku_rows = filtered_feedbacks("sku", sku=sku)
                _assert(
                    all(
                        sku
                        in {
                            str((row.get("productDetails") or {}).get("nm_id") or ""),
                            str((row.get("productDetails") or {}).get("supplier_article") or ""),
                        }
                        for row in sku_rows
                    ),
                    "SKU filter returned a non-matching feedback",
                )
            status = str(sample.get("processing_status") or "").strip()
            if status:
                status_rows = filtered_feedbacks("status", status=status)
                _assert(
                    all(str(row.get("processing_status") or "") == status for row in status_rows),
                    "status filter returned a non-matching feedback",
                )
            route = str(sample.get("route") or "").strip()
            if route:
                route_rows = filtered_feedbacks("route", route=route)
                _assert(
                    all(str(row.get("route") or "") == route for row in route_rows),
                    "route filter returned a non-matching feedback",
                )

        detail_evidence: dict[str, Any] | None = None
        if items:
            first_item = dict(items[0])
            detail = _json_get(
                context,
                normalized_base_url
                + "/v1/sheet-vitrina-v1/feedbacks/detail?id="
                + quote(str(first_item["id"]), safe=""),
                label="feedback detail",
            )
            detail_row = dict(detail.get("feedback") or {})
            _assert(str(detail_row.get("id") or "") == str(first_item["id"]), "detail feedback identity")
            _assert(isinstance(detail_row.get("media"), list), "detail media metadata must be a list")
            _assert(isinstance(detail_row.get("ai_jobs"), list), "detail AI jobs must be a list")
            _assert(isinstance(detail_row.get("publications"), list), "detail publication jobs must be a list")
            _assert(isinstance(detail_row.get("audit"), list), "detail audit must be a list")
            page.locator("[data-autoanswers-open]").first.click()
            page.locator("[data-autoanswers-detail-dialog][open]").wait_for(timeout=60_000)
            detail_text = page.locator("[data-autoanswers-detail-body]").inner_text()
            for marker in ("Отзыв", "Товар", "AI / публикация", "Медиа", "Generated reply", "WB reply", "Audit trail"):
                _assert(marker in detail_text, f"detail drawer misses {marker}")
            detail_evidence = {
                "feedback_id_sha256": hashlib.sha256(str(first_item["id"]).encode("utf-8")).hexdigest(),
                "media_count": len(detail_row["media"]),
                "has_photo": bool(first_item.get("has_photo")),
                "has_video": bool(first_item.get("has_video")),
                "has_existing_answer": bool((first_item.get("answer") or {}).get("text")),
                "processing_status": first_item.get("processing_status"),
                "publication_status": first_item.get("publication_status"),
            }

        body_text = page.locator("body").inner_text()
        for marker in ("Internal Server Error", "Traceback", "Unexpected token", "Данные отзывов не загружены"):
            if marker in body_text:
                fatal_surface_matches.append(marker)
        _assert(bool(page.title().strip()), "document title must be non-empty")
        _assert(bool(body_text.strip()), "document body must be non-empty")
        _assert(page.url == requested_url, "authenticated feedback UI must not redirect")
        _assert(not server_errors, "production UI loaded one or more 5xx responses")
        _assert(not page_errors, "production UI emitted page errors")
        _assert(not console_errors, "production UI emitted console errors")
        _assert(not fatal_surface_matches, "production UI contains a fatal error surface")

        screenshot_path = evidence_dir / "wb_autoanswers_feedbacks.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        browser.close()

    evidence = {
        "status": "passed",
        "requested_url": requested_url,
        "final_url": requested_url,
        "navigation_chain": navigation_chain,
        "document_title_present": True,
        "body_present": True,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "server_errors": server_errors,
        "fatal_surface_matches": fatal_surface_matches,
        "settings": {
            "master_enabled": setting_values["master_enabled"],
            "force_off": setting_values["force_off"],
            "effective_enabled": setting_values["effective_enabled"],
            "mode": setting_values["mode"],
        },
        "list": {
            "total": int(first.get("total") or 0),
            "page_1_count": len(items),
            "page_2_count": len(second_ids),
            "page_size": int(first.get("page_size") or 0),
            "duplicates_page_1": 0,
            "duplicates_page_2": 0,
            "cross_page_duplicates": 0,
            "filter_counts": filter_checks,
        },
        "detail": detail_evidence,
        "screenshot": str(screenshot_path),
    }
    evidence_path = evidence_dir / "wb_autoanswers_ui_evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    evidence_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    return {**evidence, "evidence_path": str(evidence_path), "evidence_sha256": f"sha256:{evidence_hash}"}
