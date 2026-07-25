#!/usr/bin/env python3
"""Authenticated read-only Playwright acceptance for production autoanswers UI."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page, sync_playwright


UI_PATH = "/sheet-vitrina-v1/vitrina?tab=feedbacks"
TRANSIENT_GET_ERROR_MARKERS = ("econnreset", "socket hang up")
TRANSIENT_GET_MAX_ATTEMPTS = 3


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _json_get(
    context: BrowserContext,
    url: str,
    *,
    label: str,
    retry_delay_seconds: float = 1.0,
) -> dict[str, Any]:
    response = None
    for attempt in range(TRANSIENT_GET_MAX_ATTEMPTS):
        try:
            response = context.request.get(url, headers={"Accept": "application/json"}, timeout=120_000)
            break
        except PlaywrightError as exc:
            transient = any(marker in str(exc).lower() for marker in TRANSIENT_GET_ERROR_MARKERS)
            if not transient or attempt + 1 >= TRANSIENT_GET_MAX_ATTEMPTS:
                raise
            time.sleep(max(0.0, float(retry_delay_seconds)) * (attempt + 1))
    _assert(response is not None, f"{label}: GET returned no response")
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
        "content_classification",
    ):
        _assert(name in item, f"local feedback item misses {name}")
    _assert(isinstance(item.get("productDetails"), dict), "local feedback productDetails must be an object")
    _assert(isinstance(item.get("answer"), dict), "local feedback answer must be an object")


def _deduplicate_feedback_candidates(
    *groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for group in groups:
        for candidate in group:
            candidate_id = str(candidate.get("id") or "")
            if not candidate_id or candidate_id in seen_ids:
                continue
            seen_ids.add(candidate_id)
            candidates.append(candidate)
    return candidates


def run_autoanswers_ui_flow(
    *,
    base_url: str,
    auth_cookie: str,
    evidence_dir: Path,
    headless: bool = True,
    expected_state: str = "off-force",
    verify_limit_save: bool = False,
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
    if expected_state not in {"off-force", "off-unforced", "manual", "auto_all"}:
        raise ValueError(
            "expected_state must be off-force, off-unforced, manual or auto_all"
        )

    requested_url = normalized_base_url + UI_PATH
    page_errors: list[str] = []
    console_errors: list[str] = []
    server_errors: list[dict[str, Any]] = []
    navigation_chain: list[dict[str, Any]] = []
    fatal_surface_matches: list[str] = []
    media_responses: list[dict[str, Any]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(viewport={"width": 1600, "height": 1000})
        context.grant_permissions(["clipboard-read", "clipboard-write"], origin=normalized_base_url)
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
        page.on(
            "response",
            lambda response: media_responses.append(
                {
                    "status": response.status,
                    "content_type": str(response.headers.get("content-type") or "").split(";", 1)[0],
                    "url_sha256": hashlib.sha256(response.url.encode("utf-8")).hexdigest(),
                }
            )
            if "/v1/sheet-vitrina-v1/feedbacks/media?" in response.url
            else None,
        )

        response = page.goto(requested_url, wait_until="domcontentloaded", timeout=120_000)
        _assert(response is not None and response.status == 200, "autoanswers document must return HTTP 200")
        page.locator('[data-unified-tab-panel="feedbacks"]:not([hidden])').wait_for(timeout=60_000)
        page.locator('[data-feedbacks-subpanel="server-reviews"]:not([hidden])').wait_for(timeout=60_000)
        expected_title = {
            "manual": "Ручной режим",
        }.get(expected_state, "Автоответы выключены")
        if expected_state == "auto_all":
            page.wait_for_function(
                "() => document.querySelector('[data-autoanswers-master-status]').textContent.trim().startsWith('Работает')",
                timeout=60_000,
            )
        else:
            page.wait_for_function(
                "title => document.querySelector('[data-autoanswers-master-status]').textContent.trim() === title",
                arg=expected_title,
                timeout=60_000,
            )
        master_status = page.locator("[data-autoanswers-master-status]")
        master_status_text = master_status.inner_text().strip()
        master_status_class = str(master_status.get_attribute("class") or "")
        if expected_state == "auto_all":
            _assert(
                master_status_text.startswith("Работает"),
                "Full-mode operator status must remain a running presentation",
            )
            _assert(
                "is-on" in master_status_class or "is-starting" in master_status_class,
                "Full-mode operator status must not use an error presentation",
            )
        if expected_state == "off-force":
            page.wait_for_function(
                "document.querySelector('[data-autoanswers-settings-note]').textContent.includes('WB_AUTOANSWERS_FORCE_OFF=true')",
                timeout=60_000,
            )
        elif expected_state == "manual":
            page.wait_for_function(
                "document.querySelector('[data-autoanswers-settings-note]').textContent.includes('только кнопкой')",
                timeout=60_000,
            )
        elif expected_state == "auto_all":
            page.wait_for_function(
                "document.querySelector('[data-autoanswers-settings-note]').textContent.includes('hard safety gates')",
                timeout=60_000,
            )
        else:
            page.wait_for_function(
                "document.querySelector('[data-autoanswers-settings-note]').textContent.includes('Синхронизация и просмотр продолжаются')",
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
        if expected_state == "off-force":
            _assert(setting_values.get("master_enabled") is False, "persisted master-switch must be OFF")
            _assert(setting_values.get("force_off") is True, "production force-off must be true")
            _assert(setting_values.get("effective_enabled") is False, "effective mode must be OFF")
            _assert(page.locator("[data-autoanswers-mode]").is_disabled(), "mode selector must be disabled by force-off")
            _assert(page.locator("[data-autoanswers-save-mode]").is_disabled(), "mode save must be disabled by force-off")
        elif expected_state == "manual":
            _assert(setting_values.get("master_enabled") is True, "manual acceptance requires persisted master ON")
            _assert(setting_values.get("force_off") is False, "manual acceptance requires force-off removed")
            _assert(setting_values.get("effective_enabled") is True, "manual acceptance requires effective ON")
            _assert(str(setting_values.get("mode") or "") == "manual", "effective production mode must be manual")
            _assert(page.locator("[data-autoanswers-mode]").input_value() == "manual", "selector must show manual")
            _assert(not page.locator("[data-autoanswers-mode]").is_disabled(), "admin mode selector must be enabled")
        elif expected_state == "auto_all":
            _assert(
                setting_values.get("master_enabled") is True,
                "auto_all acceptance requires persisted master ON",
            )
            _assert(
                setting_values.get("force_off") is False,
                "auto_all acceptance requires force-off removed",
            )
            _assert(
                setting_values.get("effective_enabled") is True,
                "auto_all acceptance requires effective ON",
            )
            _assert(
                str(setting_values.get("mode") or "") == "auto_all",
                "effective production mode must be auto_all",
            )
            _assert(
                page.locator("[data-autoanswers-mode]").input_value() == "auto_all",
                "selector must show Full/auto_all",
            )
            lifecycle = dict(settings.get("lifecycle") or {})
            components = dict(
                lifecycle.get("components")
                or lifecycle.get("component_states")
                or {}
            )
            _assert(
                lifecycle.get("lifecycle_state") == "running"
                and lifecycle.get("desired") is True
                and lifecycle.get("actual") is True
                and lifecycle.get("drift_status") == "matched"
                and lifecycle.get("fresh_scheduler_tick") is True,
                "auto_all lifecycle must be running, matched and fresh",
            )
            for component_name in ("worker", "readonly_sync"):
                component = dict(components.get(component_name) or {})
                _assert(
                    component.get("desired") is True
                    and component.get("actual") is True
                    and component.get("drift_status") == "matched",
                    f"auto_all {component_name} component must be desired/actual/matched",
                )
        else:
            _assert(setting_values.get("master_enabled") is False, "unforced OFF requires persisted master-switch OFF")
            _assert(setting_values.get("force_off") is False, "unforced OFF requires force-off removed")
            _assert(setting_values.get("effective_enabled") is False, "unforced OFF must remain ineffective")
            _assert(page.locator("[data-autoanswers-mode]").input_value() == "off", "selector must show OFF")
            _assert(not page.locator("[data-autoanswers-mode]").is_disabled(), "admin selector must remain available")
        _assert(
            page.locator("[data-autoanswers-backlog]").is_disabled(),
            "obsolete backlog control must stay disabled",
        )
        runtime_before = dict(settings.get("runtime") or {})
        budget_before = dict(settings.get("budget") or {})
        reconciliation_before = dict(settings.get("reconciliation") or {})
        limit_fields = (
            "hourly_cap_usd",
            "daily_cap_usd",
            "monthly_cap_usd",
            "max_paid_reviews_per_hour",
            "global_paid_review_concurrency",
            "max_inflight_role_calls",
            "max_materialized_processing_jobs",
        )
        limits_before = {
            field: setting_values.get(field)
            for field in limit_fields
        }
        _assert(
            str(settings.get("settings_revision") or "").startswith("sha256:"),
            "settings revision is missing",
        )
        limit_contract = dict(settings.get("limits_contract") or {})
        _assert(
            set((limit_contract.get("fields") or {}).keys()) == set(limit_fields),
            "server-owned limit contract is incomplete",
        )
        limits_button = page.locator("[data-autoanswers-open-limits]").first
        _assert(limits_button.is_visible(), "main limits action must be visible")
        limits_button.click()
        limits_modal = page.locator(
            "[data-autoanswers-limits-modal]:not([hidden])"
        )
        limits_modal.wait_for(timeout=30_000)
        _assert(
            limits_modal.locator("[data-autoanswers-setting]").count()
            == len(limit_fields),
            "limits modal must expose all seven editable global limits",
        )
        for field in limit_fields:
            input_node = limits_modal.locator(
                f'[data-autoanswers-setting="{field}"]'
            )
            _assert(input_node.count() == 1, f"limits modal misses {field}")
            _assert(
                float(input_node.input_value()) == float(limits_before[field]),
                f"limits modal does not show current {field}",
            )
        modal_style = limits_modal.locator(".autoanswers-limits-modal").evaluate(
            """node => ({
              background: getComputedStyle(node).backgroundColor,
              opacity: getComputedStyle(node).opacity
            })"""
        )
        _assert(
            modal_style == {"background": "rgb(23, 25, 31)", "opacity": "1"},
            "limits modal must be opaque and dark",
        )
        active_run_cap_text = limits_modal.locator(
            "[data-autoanswers-active-run-cap]"
        ).inner_text().strip()
        _assert(active_run_cap_text, "active run cap explanation is missing")
        bounds_text = limits_modal.locator(
            "[data-autoanswers-limits-bounds]"
        ).inner_text().strip()
        _assert(
            "Серверные границы" in bounds_text,
            "server-owned limit bounds are not visible",
        )
        limits_screenshot_path = evidence_dir / "wb_autoanswers_limits_modal.png"
        page.screenshot(path=str(limits_screenshot_path), full_page=True)
        if verify_limit_save:
            limits_modal.locator("[data-autoanswers-save-limits]").click()
            page.wait_for_function(
                """() => {
                  const node = document.querySelector('[data-autoanswers-limits-result]');
                  return node && node.textContent.includes('Сохранено и подтверждено сервером');
                }""",
                timeout=120_000,
            )
        limits_modal.locator("[data-autoanswers-close-limits]").last.click()
        limits_modal.wait_for(state="hidden", timeout=30_000)
        if expected_state != "auto_all":
            _assert(
                int(runtime_before.get("claimable_ai_jobs") or 0) == 0,
                "inactive/manual production acceptance requires zero claimable AI jobs",
            )
            _assert(
                int(runtime_before.get("claimable_publication_writes") or 0) == 0,
                "inactive/manual production acceptance requires zero claimable publication writes",
            )
            _assert(
                int((runtime_before.get("ai_jobs") or {}).get("processing") or 0) == 0,
                "inactive/manual production acceptance requires zero active AI jobs",
            )
            _assert(
                int((runtime_before.get("publication_jobs") or {}).get("publishing") or 0) == 0,
                "inactive/manual production acceptance requires zero active publication writes",
            )
            _assert(
                float(budget_before.get("active_reserved_usd") or 0) == 0,
                "inactive/manual production acceptance requires zero active reservations",
            )
        filter_names = (
            "unanswered",
            "status",
            "rating",
            "route",
            "sku",
            "date_from",
            "date_to",
            "flag",
            "system_answer",
            "content_classification",
        )
        for filter_name in filter_names:
            _assert(
                page.locator(f'[data-autoanswers-filter="{filter_name}"]').count() == 1,
                f"feedback filter {filter_name} must be rendered exactly once",
            )
        queue_metrics = page.locator("[data-autoanswers-queue-metrics]")
        queue_metric_count = queue_metrics.locator(".autoanswers-queue-metric").count()
        queue_metric_labels = {
            label.strip()
            for label in queue_metrics.locator(".autoanswers-queue-metric span").all_inner_texts()
        }
        required_queue_metric_labels = {
            "Всего в scope",
            "Без ответа WB",
            "Опубликованы",
            "Ошибки",
            "Осталось",
            "С содержанием",
            "Пустых осталось",
            "Начальный состав",
            "Добавлено после старта",
            "Добавлено с содержанием",
            "Добавлено пустых",
            "Сейчас в запуске",
            "Добавлено в последний раз",
            "Вне текущего run",
        }
        _assert(queue_metric_count >= 27, "queue dashboard metrics are incomplete")
        _assert(
            required_queue_metric_labels.issubset(queue_metric_labels),
            "queue dashboard misses required rolling/operator metrics",
        )
        queue_context_text = page.locator("[data-autoanswers-queue-context]").inner_text()
        for marker in (
            "начальный состав",
            "добавлено +",
            "сейчас",
            "приоритет",
            "очередь обновлена",
        ):
            _assert(marker in queue_context_text, f"queue context misses {marker}")
        _assert(
            page.locator("[data-autoanswers-progress-bars] .autoanswers-progress-row").count() == 2,
            "preparation/publication progress bars are missing",
        )
        _assert(
            page.locator("[data-autoanswers-content-progress-bars] .autoanswers-progress-row").count() == 2,
            "content-bearing preparation/publication progress bars are missing",
        )
        _assert(page.locator("[data-autoanswers-progress-card]").count() == 2, "progress cards must be separate")
        card_styles = page.evaluate(
            """() => {
              const all = document.querySelector('[data-autoanswers-progress-card="all"]');
              const content = document.querySelector('[data-autoanswers-progress-card="content-bearing"]');
              return {
                gap: content.getBoundingClientRect().top - all.getBoundingClientRect().bottom,
                allBackground: getComputedStyle(all).backgroundColor,
                contentBackground: getComputedStyle(content).backgroundColor,
                allBorder: getComputedStyle(all).borderColor,
                contentBorder: getComputedStyle(content).borderColor
              };
            }"""
        )
        _assert(float(card_styles["gap"]) >= 14, "progress cards need a visible vertical gap")
        _assert(
            card_styles["allBackground"] != card_styles["contentBackground"]
            or card_styles["allBorder"] != card_styles["contentBorder"],
            "progress cards need distinct visual treatment",
        )
        progress = dict(runtime_before.get("progress") or {})
        rolling = dict(progress.get("rolling_admission") or {})
        if expected_state == "auto_all":
            _assert(
                int(rolling.get("current_total") or 0)
                == int(rolling.get("initial_membership") or 0)
                + int(rolling.get("admitted_since_start") or 0),
                "rolling current total does not reconcile",
            )
            _assert(
                bool(str(rolling.get("last_refresh_at") or "").strip()),
                "rolling admission refresh timestamp is missing",
            )
        stage_specs = (
            ("all_preparation", page.locator("[data-autoanswers-progress-bars] .autoanswers-progress-row").nth(0)),
            ("all_publication", page.locator("[data-autoanswers-progress-bars] .autoanswers-progress-row").nth(1)),
            ("content_bearing_preparation", page.locator("[data-autoanswers-content-progress-bars] .autoanswers-progress-row").nth(0)),
            ("content_bearing_publication", page.locator("[data-autoanswers-content-progress-bars] .autoanswers-progress-row").nth(1)),
        )
        stage_evidence: dict[str, Any] = {}
        for stage_name, locator in stage_specs:
            stage = dict(progress.get(stage_name) or {})
            done = int(stage.get("done") or 0)
            total = int(stage.get("total") or 0)
            remaining = int(stage.get("remaining") or 0)
            text = locator.inner_text()
            if total == 0:
                _assert("Нет отзывов в этой категории" in text, f"{stage_name} zero denominator label")
                display_percent = None
            else:
                _assert(f"{done} из {total}" in text, f"{stage_name} exact X/Y is missing")
                _assert(f"осталось {remaining}" in text, f"{stage_name} remaining is missing")
                display_percent = float(stage["percent"])
                _assert(f"{display_percent:.1f}%" in text, f"{stage_name} exact percent is missing")
            if expected_state == "manual":
                _assert(
                    "Приостановлено вручную" in text,
                    f"{stage_name} must show manual pause",
                )
            elif expected_state == "auto_all":
                _assert(
                    "Приостановлено вручную" not in text,
                    f"{stage_name} must not show a manual pause in auto_all",
                )
            stage_evidence[stage_name] = {
                "done": done,
                "total": total,
                "remaining": remaining,
                "percent": display_percent,
            }
        _assert(
            int(progress.get("content_bearing_total") or 0)
            + int(progress.get("rating_only_total") or 0)
            + int(progress.get("indeterminate_total") or 0)
            == int((progress.get("all_preparation") or {}).get("total") or 0),
            "content taxonomy must reconcile to the all-reviews denominator",
        )
        desktop_screenshot_path = evidence_dir / "wb_autoanswers_progress_desktop.png"
        page.screenshot(path=str(desktop_screenshot_path), full_page=True)
        _assert(
            bool(page.locator("[data-autoanswers-stop-reason]").inner_text().strip()),
            "queue stop reason must be visible",
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
        for system_filter in (
            "created", "missing", "awaiting_generation", "processing", "needs_review",
            "ready_publication", "publication_queue", "published", "error",
        ):
            filtered_feedbacks("system_" + system_filter, system_answer=system_filter)
        media_filter_rows: dict[str, list[dict[str, Any]]] = {}
        for media_filter in ("has_photo", "has_video"):
            media_rows = filtered_feedbacks(media_filter, **{media_filter: "true"})
            media_filter_rows[media_filter] = media_rows
            _assert(
                all(bool(row.get(media_filter)) for row in media_rows),
                f"{media_filter} filter returned a non-matching feedback",
            )
        flag_rows: dict[str, list[dict[str, Any]]] = {}
        for flag_filter in ("needs_review", "published", "error"):
            flag_rows[flag_filter] = filtered_feedbacks(flag_filter, **{flag_filter: "true"})
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
        if expected_state == "manual" and unanswered_rows:
            page.locator('[data-autoanswers-filter="unanswered"]').select_option("true")
            page.locator("[data-autoanswers-apply]").click()
            page.wait_for_function(
                "document.querySelector('[data-autoanswers-list-meta]').textContent.includes('Найдено:')",
                timeout=60_000,
            )
        detail_sample = dict(unanswered_rows[0]) if expected_state == "manual" and unanswered_rows else (dict(items[0]) if items else None)
        if detail_sample:
            first_item = detail_sample
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
            detail_body = page.locator("[data-autoanswers-detail-body]")
            detail_text = detail_body.inner_text()
            for marker in ("Товар", "Ответ Wildberries", "Статус", "Техническая информация"):
                _assert(marker in detail_text, f"detail drawer misses {marker}")
            technical = page.locator("[data-autoanswers-detail-technical]")
            _assert(technical.count() == 1, "detail must expose exactly one technical information spoiler")
            _assert(not technical.evaluate("node => node.open"), "technical information must be closed by default")
            for technical_marker in ("Route:", "JSON contract", "Hard gates", "Audit trail"):
                _assert(technical_marker not in detail_text, f"closed detail leaked {technical_marker}")
            for optional_label, source_value in (
                ("Плюсы:", first_item.get("pros")),
                ("Минусы:", first_item.get("cons")),
                ("Теги:", first_item.get("tags")),
            ):
                if not source_value:
                    _assert(optional_label not in detail_text, f"empty {optional_label} occupies detail space")
            editor = page.locator("[data-autoanswers-manual-reply]")
            if editor.count():
                dimensions = editor.evaluate("node => ({client: node.clientHeight, scroll: node.scrollHeight})")
                _assert(dimensions["client"] + 2 >= dimensions["scroll"], "ready answer textarea did not auto-grow")
            technical.locator("summary").click()
            expanded_text = detail_body.inner_text()
            for technical_marker in ("Route:", "JSON contract", "Hard gates", "Audit trail"):
                _assert(technical_marker in expanded_text, f"technical spoiler misses {technical_marker}")
            current_content_jobs = [
                dict(job)
                for job in detail_row["ai_jobs"]
                if int(job.get("content_version") or 0) == int(detail_row.get("content_version") or 0)
            ]
            if expected_state == "manual" and not str((first_item.get("answer") or {}).get("text") or "").strip():
                if not current_content_jobs:
                    _assert(
                        page.locator("[data-autoanswers-generate]").count() == 1,
                        "eligible manual review must expose exactly one generate button",
                    )
                    _assert(
                        page.locator("[data-autoanswers-publish]").count() == 0,
                        "publication must not be offered before a guarded generated result",
                    )
                else:
                    _assert(
                        page.locator("[data-autoanswers-generate]").count() == 0,
                        "an existing current job must suppress duplicate generation",
                    )
            if expected_state != "manual":
                _assert(
                    page.locator("[data-autoanswers-generate]").count() == 0,
                    "non-manual mode must hide manual generation",
                )
            detail_evidence = {
                "feedback_id_sha256": hashlib.sha256(str(first_item["id"]).encode("utf-8")).hexdigest(),
                "media_count": len(detail_row["media"]),
                "has_photo": bool(first_item.get("has_photo")),
                "has_video": bool(first_item.get("has_video")),
                "has_existing_answer": bool((first_item.get("answer") or {}).get("text")),
                "processing_status": first_item.get("processing_status"),
                "publication_status": first_item.get("publication_status"),
                "current_content_job_count": len(current_content_jobs),
                "technical_closed_by_default": True,
                "technical_expands": True,
                "textarea_auto_grow": not editor.count() or dimensions["client"] + 2 >= dimensions["scroll"],
            }
            page.locator("[data-autoanswers-detail-dialog]").evaluate("node => node.close()")

        table_answer_evidence: dict[str, Any] = {"present": False}
        answer_candidate = next(
            (
                row
                for row in [*flag_rows.get("published", []), *flag_rows.get("needs_review", []), *items]
                if str(row.get("generated_reply") or "").strip()
            ),
            None,
        )
        if answer_candidate:
            page.evaluate(
                """row => {
                  state.feedbacks.server.items = [row];
                  state.feedbacks.server.loaded = true;
                  state.feedbacks.server.loading = false;
                  state.feedbacks.server.total = 1;
                  renderAutoanswersServer();
                }""",
                answer_candidate,
            )
            answer_box = page.locator(".autoanswers-answer-box")
            answer_box.wait_for(timeout=30_000)
            before_copy = answer_box.inner_text()
            dimensions = answer_box.evaluate(
                "node => ({height: node.getBoundingClientRect().height, overflowY: getComputedStyle(node).overflowY, background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color})"
            )
            _assert(dimensions["height"] <= 90, "table answer expanded the row")
            _assert(dimensions["overflowY"] in {"auto", "scroll"}, "table answer does not scroll internally")
            _assert(dimensions["background"] != "rgb(248, 250, 252)", "table answer retained the light background")
            _assert(dimensions["color"] != "rgb(0, 0, 0)", "table answer contrast is invalid")
            page.locator("[data-autoanswers-copy]").click()
            page.get_by_role("button", name="Скопировано").wait_for(timeout=10_000)
            after_copy = answer_box.inner_text().replace("Скопировано", "Копировать")
            _assert(after_copy == before_copy, "copy action changed the displayed answer")
            table_answer_evidence = {"present": True, "fixed_height": True, "copy_without_mutation": True}

        media_evidence: dict[str, Any] = {}
        media_candidates = _deduplicate_feedback_candidates(
            media_filter_rows.get("has_photo", []),
            media_filter_rows.get("has_video", []),
            flag_rows.get("needs_review", []),
            unanswered_rows,
            items,
        )
        ready_by_kind: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        for candidate in media_candidates:
            if len(ready_by_kind) == 2:
                break
            detail = _json_get(
                context,
                normalized_base_url
                + "/v1/sheet-vitrina-v1/feedbacks/detail?id="
                + quote(str(candidate["id"]), safe=""),
                label="media-ready feedback detail",
            )
            detail_row = dict(detail.get("feedback") or {})
            for media in detail_row.get("media") or []:
                kind = str(media.get("kind") or "")
                available = bool(media.get("primary_available")) if kind == "photo" else bool(
                    media.get("preview_available")
                )
                if kind in {"photo", "video"} and available and kind not in ready_by_kind:
                    ready_by_kind[kind] = (candidate, detail_row)
        for kind in ("photo", "video"):
            _assert(kind in ready_by_kind, f"production UI has no prepared real {kind} candidate")
            candidate, detail_row = ready_by_kind[kind]
            media_response_start = len(media_responses)
            page.evaluate("id => openAutoanswersDetail(id)", str(candidate["id"]))
            page.locator("[data-autoanswers-detail-dialog][open]").wait_for(timeout=60_000)
            image = page.locator(
                '.autoanswers-media-item[alt="Фото покупателя"]'
                if kind == "photo"
                else '.autoanswers-media-item[alt="Превью видео покупателя"]'
            ).first
            image.wait_for(timeout=60_000)
            asset_url_sha256 = hashlib.sha256(str(image.get_attribute("src") or "").encode("utf-8")).hexdigest()
            page.wait_for_function(
                "node => node.complete && node.naturalWidth > 0",
                arg=image.element_handle(),
                timeout=60_000,
            )
            image_state = image.evaluate("node => ({complete: node.complete, width: node.naturalWidth})")
            _assert(image_state["complete"] and image_state["width"] > 0, f"real {kind} did not render")
            matching_responses = [
                response
                for response in media_responses[media_response_start:]
                if response["url_sha256"] == asset_url_sha256
            ]
            _assert(matching_responses, f"real {kind} emitted no private media response")
            media_response = matching_responses[-1]
            _assert(media_response["status"] == 200, f"real {kind} media returned HTTP {media_response['status']}")
            _assert(
                str(media_response["content_type"]).startswith("image/"),
                f"real {kind} media returned a non-image MIME",
            )
            media_evidence[kind] = {
                "feedback_id_sha256": hashlib.sha256(str(candidate["id"]).encode("utf-8")).hexdigest(),
                "rendered": True,
                "metadata_count": len(detail_row.get("media") or []),
                "asset_status": media_response["status"],
                "asset_content_type": media_response["content_type"],
                "asset_url_sha256": media_response["url_sha256"],
            }
            page.locator("[data-autoanswers-detail-dialog]").evaluate("node => node.close()")

        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate(
            """() => {
              state.feedbacks.server.items = state.feedbacks.server.items.slice(0, 1);
              renderAutoanswersServer();
            }"""
        )
        narrow_overflow = page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth + 1"
        )
        _assert(not narrow_overflow, "autoanswers narrow layout causes document-level horizontal overflow")
        _assert(
            page.locator("[data-autoanswers-progress-card]").count() == 2,
            "both progress cards must remain rendered in narrow layout",
        )

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

        settings_after = _json_get(
            context,
            normalized_base_url + "/v1/sheet-vitrina-v1/feedbacks/autoanswers/settings",
            label="autoanswers settings readback",
        )
        runtime_after = dict(settings_after.get("runtime") or {})
        setting_values_after = dict(settings_after.get("settings") or {})
        reconciliation_after = dict(settings_after.get("reconciliation") or {})
        limits_unchanged = all(
            float(setting_values_after.get(field))
            == float(limits_before[field])
            for field in limit_fields
        )
        run_cap_unchanged = all(
            reconciliation_after.get(field) == reconciliation_before.get(field)
            for field in (
                "transition_run_id",
                "run_max_usd",
                "run_max_paid_reviews",
            )
        )
        _assert(limits_unchanged, "production limit verification changed a value")
        _assert(
            run_cap_unchanged,
            "production limit verification changed active run cap",
        )
        jobs_unchanged = (
            runtime_after.get("ai_jobs") == runtime_before.get("ai_jobs")
            and runtime_after.get("publication_jobs")
            == runtime_before.get("publication_jobs")
        )
        if expected_state != "auto_all":
            _assert(jobs_unchanged, "read-only UI acceptance changed durable jobs")

        narrow_screenshot_path = evidence_dir / "wb_autoanswers_progress_narrow.png"
        page.screenshot(path=str(narrow_screenshot_path), full_page=True)
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
            "operator_status": {
                "text": master_status_text,
                "class": master_status_class,
            },
        },
        "expected_state": expected_state,
        "jobs_unchanged": jobs_unchanged,
        "background_progress_allowed": expected_state == "auto_all",
        "ui_business_mutations": 0,
        "ui_settings_mutations": 1 if verify_limit_save else 0,
        "active_ai_jobs": int((runtime_before.get("ai_jobs") or {}).get("processing") or 0),
        "active_publication_jobs": int((runtime_before.get("publication_jobs") or {}).get("publishing") or 0),
        "active_reserved_usd": float(budget_before.get("active_reserved_usd") or 0),
        "limits": {
            "modal_visible": True,
            "opaque_dark": True,
            "editable_fields": list(limit_fields),
            "settings_revision_present": True,
            "safe_same_value_save_requested": bool(verify_limit_save),
            "readback_confirmed": bool(verify_limit_save),
            "values_unchanged": limits_unchanged,
            "active_run_cap_unchanged": run_cap_unchanged,
            "active_run_cap_text": active_run_cap_text,
            "bounds_text": bounds_text,
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
        "table_answer": table_answer_evidence,
        "media": media_evidence,
        "narrow_layout": {"document_overflow": False},
        "progress": {
            "stages": stage_evidence,
            "content_bearing_total": int(progress.get("content_bearing_total") or 0),
            "rating_only_total": int(progress.get("rating_only_total") or 0),
            "indeterminate_total": int(progress.get("indeterminate_total") or 0),
            "rating_only_excluded_from_content_card": True,
            "queue_metric_count": queue_metric_count,
            "queue_metric_labels": sorted(queue_metric_labels),
            "rolling_admission": {
                "initial_membership": int(rolling.get("initial_membership") or 0),
                "admitted_since_start": int(rolling.get("admitted_since_start") or 0),
                "admitted_by_class": dict(rolling.get("admitted_by_class") or {}),
                "admitted_by_rating": dict(rolling.get("admitted_by_rating") or {}),
                "current_total": int(rolling.get("current_total") or 0),
                "last_refresh_at": str(rolling.get("last_refresh_at") or ""),
                "current_priority_bucket": str(
                    rolling.get("current_priority_bucket") or ""
                ),
            },
        },
        "screenshots": {
            "desktop": str(desktop_screenshot_path),
            "narrow": str(narrow_screenshot_path),
            "limits_modal": str(limits_screenshot_path),
        },
    }
    evidence_path = evidence_dir / "wb_autoanswers_ui_evidence.json"
    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    evidence_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    return {**evidence, "evidence_path": str(evidence_path), "evidence_sha256": f"sha256:{evidence_hash}"}
