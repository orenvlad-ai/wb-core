"""Shared no-submit Seller Portal feedback-row actionability resolver.

The resolver intentionally mirrors the target-row probe path: status tab,
date/star filters, bounded DOM collection, exact row scoring, safe row menu,
and optional complaint-modal dry-run. It never clicks final submit.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from playwright.sync_api import Error as PlaywrightError, Page  # noqa: E402

from apps.seller_portal_feedbacks_complaint_dry_run_plan import (  # noqa: E402
    apply_seller_portal_date_filter,
    apply_seller_portal_star_filter,
    choose_complaint_category,
    click_complaint_category,
    close_draft_modal_without_submit,
    feedback_list_signature,
    fill_description_field,
    inspect_seller_portal_filter_state,
    reset_seller_portal_viewport_for_filters,
    wait_for_description_field_ready,
)
from apps.seller_portal_feedbacks_complaints_scout import (  # noqa: E402
    ROW_MENU_COMPLAINT_LABEL,
    _click_safe_row_menu,
    _click_tab_like,
    _safe_escape,
    _wait_for_feedback_rows,
    _wait_settle,
    assert_safe_click_label,
    click_open_row_menu_complaint_action,
    detect_complaint_success_state,
    extract_complaint_modal_state,
    extract_open_row_menu_state,
    extract_visible_feedback_rows,
)
from apps.seller_portal_feedbacks_matching_replay import (  # noqa: E402
    NO_SUBMIT_MODE,
    ReplayConfig,
    classify_match,
    collect_feedback_rows_with_scroll,
    normalize_date_key,
    safe_text,
    score_candidate,
    summarize_api_row,
    summarize_ui_row,
    ui_row_matches_requested_filters,
)


STATUS_TAB_UNANSWERED = "Ждут ответа"
STATUS_TAB_ANSWERED = "Есть ответ"
STATUS_TAB_ALL = "Отзывы"


@dataclass(frozen=True)
class ActionableResolverConfig:
    date_from: str
    date_to: str
    stars: tuple[int, ...]
    is_answered: str
    max_api_rows: int
    max_ui_rows: int
    open_complaint_modal: bool
    mode: str = NO_SUBMIT_MODE


def config_from_dry_run(config: Any, *, open_complaint_modal: bool = False) -> ActionableResolverConfig:
    return ActionableResolverConfig(
        date_from=str(config.date_from),
        date_to=str(config.date_to),
        stars=tuple(int(star) for star in config.stars),
        is_answered=str(config.is_answered),
        max_api_rows=int(config.max_api_rows),
        max_ui_rows=max(20, min(max(int(config.max_api_rows) * 6, 60), 120)),
        open_complaint_modal=open_complaint_modal,
        mode=NO_SUBMIT_MODE,
    )


def config_from_target_probe(config: Any) -> ActionableResolverConfig:
    return ActionableResolverConfig(
        date_from=str(config.date),
        date_to=str(config.date),
        stars=tuple(int(star) for star in config.stars),
        is_answered=str(config.is_answered),
        max_api_rows=int(config.max_api_rows),
        max_ui_rows=int(config.max_ui_rows),
        open_complaint_modal=bool(config.open_complaint_modal),
        mode="read-only",
    )


def resolve_feedback_actionability(
    page: Page,
    config: ActionableResolverConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any] | None = None,
    preferred_category: str = "",
    description_text: str = "",
) -> dict[str, Any]:
    expected_ui = expected_ui or {}
    result = empty_actionability_result()
    feedback_id = str(api_row.get("feedback_id") or expected_ui.get("feedback_id") or "")
    result["feedback_id"] = feedback_id
    result["expected_ui_summary"] = summarize_ui_row(dict(expected_ui)) if expected_ui else {}
    tabs = status_tabs_for_row(config, api_row, expected_ui)
    result["tabs_tried"] = tabs
    last_blocker = ""
    for tab_label in tabs:
        attempt = resolve_in_tab(
            page,
            config,
            api_row,
            expected_ui=expected_ui,
            tab_label=tab_label,
            preferred_category=preferred_category,
            description_text=description_text,
        )
        result["attempts"].append(attempt)
        result["dom_rows_collected"] += int(attempt.get("dom_rows_collected") or 0)
        result["visible_rows_checked"] += int(attempt.get("visible_rows_checked") or 0)
        result["tab_used"] = tab_label
        result["date_filter_applied"] = bool(result["date_filter_applied"] or attempt.get("date_filter_applied"))
        result["star_filter_applied"] = bool(result["star_filter_applied"] or attempt.get("star_filter_applied"))
        result["selected_star_values_after"] = sorted(
            {
                *[int(star) for star in result.get("selected_star_values_after") or [] if str(star).isdigit()],
                *[int(star) for star in attempt.get("selected_star_values_after") or [] if str(star).isdigit()],
            }
        )
        result["list_update_observed"] = bool(result["list_update_observed"] or attempt.get("list_update_observed"))
        result["scroll_used"] = bool(result["scroll_used"] or attempt.get("scroll_used"))
        result["search_used"] = bool(result["search_used"] or attempt.get("search_used"))
        last_blocker = str(attempt.get("block_reason") or last_blocker)
        if attempt.get("actionable_row_found"):
            result.update(
                {
                    "actionable_row_found": True,
                    "row_visible": True,
                    "row_menu_found": bool(attempt.get("row_menu_found")),
                    "menu_found": bool(attempt.get("row_menu_found")),
                    "complaint_action_found": bool(attempt.get("complaint_action_found")),
                    "complaint_action_available": bool(attempt.get("complaint_action_found")),
                    "modal_opened": bool(attempt.get("modal_opened")),
                    "categories_found": attempt.get("categories_found") or [],
                    "description_field_found": bool(attempt.get("description_field_found")),
                    "description_value_after_fill": str(attempt.get("description_value_after_fill") or ""),
                    "description_value_after_blur": str(attempt.get("description_value_after_blur") or ""),
                    "description_value_match": bool(attempt.get("description_value_match")),
                    "modal_closed": bool(attempt.get("modal_closed")),
                    "submit_clicked": bool(attempt.get("submit_clicked")),
                    "visible_row_match": attempt.get("visible_row_match") or {},
                    "resolved_row": attempt.get("resolved_row") or {},
                    "resolved_row_summary": summarize_ui_row(attempt.get("resolved_row") or {}),
                    "row_menu_click": attempt.get("row_menu_click") or {},
                    "menu_labels": attempt.get("menu_labels") or [],
                    "filter_controller": attempt.get("filter_controller") or {},
                    "locator_strategy": str(attempt.get("locator_strategy") or ""),
                    "tab_used": tab_label,
                    "selected_category": str(attempt.get("selected_category") or ""),
                    "category_click": attempt.get("category_click") or {},
                    "description_fill": attempt.get("description_fill") or {},
                    "complaint_action_click": attempt.get("complaint_action_click") or {},
                    "close_method": str(attempt.get("close_method") or ""),
                    "submit_button_label": str(attempt.get("submit_button_label") or ""),
                    "block_reason": str(attempt.get("block_reason") or ""),
                }
            )
            return result
        _safe_escape(page)
    result["block_reason"] = last_blocker or "actionable DOM row was not found in tried feedback tabs"
    return result


def resolve_in_tab(
    page: Page,
    config: ActionableResolverConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
    tab_label: str,
    preferred_category: str,
    description_text: str,
) -> dict[str, Any]:
    attempt = empty_attempt(tab_label)
    clicked = _click_tab_like(page, tab_label)
    attempt["tab_clicked"] = bool(clicked)
    if not clicked and tab_label != STATUS_TAB_ALL:
        attempt["block_reason"] = f"feedback tab {tab_label!r} was not found"
        return attempt
    _wait_settle(page, 1800)
    _wait_for_feedback_rows(page, timeout_ms=7000)
    filters = apply_target_filters(page, config, api_row, expected_ui=expected_ui)
    attempt["filter_controller"] = filters
    attempt["date_filter_applied"] = bool(filters.get("date_filter_applied"))
    attempt["star_filter_applied"] = bool(filters.get("star_filter_applied"))
    attempt["selected_star_values_after"] = filters.get("selected_star_values_after") or filters.get("current_selected_stars") or []
    attempt["list_update_observed"] = bool(filters.get("list_update_observed"))
    _wait_for_feedback_rows(page, timeout_ms=7000)

    visible = find_exact_visible_dom_row(page, api_row, expected_ui=expected_ui)
    attempt["visible_rows_checked"] = int(visible.get("visible_rows_checked") or 0)
    if visible.get("row"):
        return confirm_and_maybe_draft(page, attempt, visible, config, preferred_category=preferred_category, description_text=description_text)

    collected_rows, scroll_stats = collect_filtered_dom_rows(page, config, api_row, expected_ui=expected_ui, tab_label=tab_label)
    attempt["scroll_used"] = True
    attempt["scroll_attempts"].append(scroll_stats)
    attempt["dom_rows_collected"] = len(collected_rows)
    match = match_one_api_row_to_dom(api_row, collected_rows)
    attempt["visible_row_match"] = match
    if match.get("match_status") == "exact" and match.get("dom_scout_id"):
        row = next((item for item in collected_rows if str(item.get("dom_scout_id") or "") == str(match.get("dom_scout_id") or "")), {})
        return confirm_and_maybe_draft(
            page,
            attempt,
            {"row": row, "match": match, "visible_rows_checked": len(collected_rows), "locator_strategy": "shared_target_probe_collected_dom"},
            config,
            preferred_category=preferred_category,
            description_text=description_text,
        )
    attempt["block_reason"] = "exact actionable DOM row was not found after target-probe filter/materialization path"
    return attempt


def apply_target_filters(
    page: Page,
    config: ActionableResolverConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
) -> dict[str, Any]:
    date_from, date_to = feedback_filter_date_range(config, api_row, expected_ui=expected_ui)
    stars = feedback_filter_stars(config, api_row, expected_ui=expected_ui)
    reset_seller_portal_viewport_for_filters(page)
    before_signature = feedback_list_signature(page)
    date_result = apply_seller_portal_date_filter(page, date_from=date_from, date_to=date_to)
    _wait_settle(page, 900)
    star_result = apply_seller_portal_star_filter(page, stars=stars)
    _wait_settle(page, 1200)
    after_signature = feedback_list_signature(page)
    state = inspect_seller_portal_filter_state(page)
    return {
        "requested_date_from": date_from,
        "requested_date_to": date_to,
        "requested_stars": list(stars),
        "date_filter": date_result,
        "star_filter": star_result,
        "date_filter_applied": bool(date_result.get("applied")),
        "star_filter_applied": bool(star_result.get("applied")),
        "star_filter_requested": list(stars),
        "selected_star_values_before": star_result.get("selected_star_values_before") or [],
        "selected_star_values_after": star_result.get("selected_star_values_after") or star_result.get("selected_stars") or [],
        "star_apply_clicked": bool(star_result.get("apply_clicked")),
        "status_tab_selected": True,
        "list_signature_before": before_signature,
        "list_signature_after": after_signature,
        "list_update_observed": bool(after_signature.get("fingerprint") and after_signature.get("fingerprint") != before_signature.get("fingerprint")),
        "current_visible_date_range": state.get("visible_date_range") or "",
        "current_selected_stars": state.get("selected_stars") or star_result.get("selected_stars") or [],
        "selectors_used": [*(date_result.get("selectors_used") or []), *(star_result.get("selectors_used") or [])],
        "blocker": str(date_result.get("reason") or star_result.get("reason") or ""),
    }


def collect_filtered_dom_rows(
    page: Page,
    config: ActionableResolverConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
    tab_label: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    date_from, date_to = feedback_filter_date_range(config, api_row, expected_ui=expected_ui)
    rows, scroll_stats = collect_feedback_rows_with_scroll(
        page,
        max_rows=config.max_ui_rows,
        date_from=date_from or config.date_from,
    )
    replay_config = to_replay_config(config, date_from=date_from or config.date_from, date_to=date_to or config.date_to)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if not ui_row_matches_requested_filters(row, replay_config):
            continue
        enriched = dict(row)
        enriched["source"] = "seller_portal_dom"
        enriched["status_tab"] = tab_label
        enriched["tab_used"] = tab_label
        filtered.append(enriched)
    for index, row in enumerate(filtered):
        row["row_index"] = index
    return filtered, scroll_stats


def find_exact_visible_dom_row(page: Page, api_row: Mapping[str, Any], *, expected_ui: Mapping[str, Any]) -> dict[str, Any]:
    rows = extract_visible_feedback_rows(page, max_rows=80)
    match = match_one_api_row_to_dom(api_row, rows)
    row: dict[str, Any] = {}
    if match.get("match_status") == "exact":
        dom_id = str(match.get("dom_scout_id") or "")
        row = next((item for item in rows if str(item.get("dom_scout_id") or "") == dom_id), {})
    return {"row": row, "match": match, "visible_rows_checked": len(rows), "locator_strategy": "shared_target_probe_visible_dom"}


def match_one_api_row_to_dom(api_row: Mapping[str, Any], dom_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [score_candidate(api_row, row) for row in dom_rows]
    scored.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    best = scored[0] if scored else {
        "score": 0.0,
        "ui_row": {},
        "matched_fields": [],
        "missing_fields": [],
        "mismatched_fields": [],
        "reasons": ["no DOM rows collected"],
        "text_similarity": 0.0,
        "text_containment": 0.0,
    }
    close_candidates = [
        item
        for item in scored
        if float(item.get("score") or 0.0) >= 0.5 and float(best.get("score") or 0.0) - float(item.get("score") or 0.0) <= 0.08
    ]
    status = classify_match(best, ambiguity_count=len(close_candidates))
    ui_row = best.get("ui_row") if isinstance(best.get("ui_row"), dict) else {}
    return {
        "api_feedback_id": str(api_row.get("feedback_id") or ""),
        "found_in_seller_portal_dom": status in {"exact", "high"},
        "match_status": status,
        "match_score": float(best.get("score") or 0.0),
        "matched_fields": best.get("matched_fields") or [],
        "missing_fields": best.get("missing_fields") or [],
        "mismatched_fields": best.get("mismatched_fields") or [],
        "match_reason": "; ".join(str(item) for item in best.get("reasons") or []),
        "text_similarity": best.get("text_similarity", 0.0),
        "text_containment": best.get("text_containment", 0.0),
        "tab_used": str(ui_row.get("tab_used") or ui_row.get("status_tab") or ""),
        "row_index": ui_row.get("row_index", ui_row.get("ui_collection_index")),
        "ui_row_text_snippet": safe_text(str(ui_row.get("text_snippet") or ""), 260),
        "ui_review_tags": ui_row.get("review_tags") or [],
        "api_summary": summarize_api_row(api_row),
        "best_ui_candidate": summarize_ui_row(ui_row),
        "dom_scout_id": str(ui_row.get("dom_scout_id") or ""),
        "safe_for_actionability_probe": status == "exact" and bool(ui_row.get("dom_scout_id")),
    }


def confirm_and_maybe_draft(
    page: Page,
    attempt: dict[str, Any],
    visible_result: Mapping[str, Any],
    config: ActionableResolverConfig,
    *,
    preferred_category: str,
    description_text: str,
) -> dict[str, Any]:
    row = visible_result.get("row") if isinstance(visible_result.get("row"), dict) else {}
    attempt["resolved_row"] = row
    attempt["visible_row_match"] = visible_result.get("match") or {}
    attempt["locator_strategy"] = str(visible_result.get("locator_strategy") or "shared_target_probe_dom")
    attempt["row_visible"] = bool(row)
    dom_id = str(row.get("dom_scout_id") or "")
    if not dom_id:
        attempt["block_reason"] = "matched DOM row has no stable row id for menu click"
        return attempt
    clicked_menu = _click_safe_row_menu(page, dom_id)
    attempt["row_menu_click"] = clicked_menu
    attempt["row_menu_found"] = bool(clicked_menu.get("ok"))
    if not clicked_menu.get("ok"):
        attempt["block_reason"] = str(clicked_menu.get("reason") or "safe row menu not found")
        return attempt
    _wait_settle(page, 800)
    menu_state = extract_open_row_menu_state(page)
    attempt["menu_labels"] = menu_state.get("items") or []
    attempt["complaint_action_found"] = bool(menu_state.get("complaint_action_found"))
    if not menu_state.get("complaint_action_found"):
        attempt["block_reason"] = "Пожаловаться на отзыв action not found in row menu"
        _safe_escape(page)
        return attempt
    attempt["actionable_row_found"] = True
    if not config.open_complaint_modal:
        attempt["block_reason"] = ""
        return attempt
    draft_modal(page, attempt, preferred_category=preferred_category, description_text=description_text)
    return attempt


def draft_modal(page: Page, attempt: dict[str, Any], *, preferred_category: str, description_text: str) -> None:
    assert_safe_click_label(ROW_MENU_COMPLAINT_LABEL, purpose="open_complaint_modal")
    action_click = click_open_row_menu_complaint_action(page)
    attempt["complaint_action_click"] = action_click
    if not action_click.get("ok"):
        attempt["block_reason"] = str(action_click.get("reason") or "complaint action could not be clicked")
        _safe_escape(page)
        return
    _wait_settle(page, 1500)
    modal_state = extract_complaint_modal_state(page)
    attempt["modal_opened"] = bool(modal_state.get("opened"))
    attempt["categories_found"] = modal_state.get("categories") or []
    attempt["description_field_found"] = bool(modal_state.get("description_field_found"))
    attempt["submit_button_seen"] = bool(modal_state.get("submit_button_seen"))
    attempt["submit_button_label"] = str(modal_state.get("submit_button_label") or "")
    attempt["submit_clicked"] = False
    attempt["durable_success_state_seen"] = bool(detect_complaint_success_state(page).get("seen"))
    if not attempt["modal_opened"]:
        attempt["block_reason"] = "complaint modal did not open"
        return
    if preferred_category or description_text:
        category = choose_complaint_category(attempt["categories_found"], force_other=False, preferred_category=preferred_category)
        attempt["selected_category"] = category
        if not category:
            attempt["block_reason"] = "modal category could not be selected"
        else:
            category_click = click_complaint_category(page, category)
            attempt["category_click"] = category_click
            if not category_click.get("ok"):
                attempt["block_reason"] = str(category_click.get("reason") or "category could not be selected")
            else:
                _wait_settle(page, 700)
                attempt["description_field_ready_after_category"] = wait_for_description_field_ready(page, timeout_ms=6000)
                if description_text:
                    fill = fill_description_field(page, description_text)
                    attempt["description_fill"] = fill
                    attempt["description_field_found"] = bool(fill.get("ok"))
                    attempt["description_value_after_fill"] = str(fill.get("value_after_fill") or "")
                    attempt["description_value_after_blur"] = str(fill.get("value_after_blur") or "")
                    attempt["description_value_match"] = bool(fill.get("value_match"))
                    if not fill.get("ok"):
                        attempt["block_reason"] = str(fill.get("reason") or "description field unavailable")
                    elif not fill.get("value_match"):
                        attempt["block_reason"] = "description field value mismatch before final submit; submit blocked"
    close_result = close_draft_modal_without_submit(page)
    attempt["close_method"] = str(close_result.get("close_method") or "")
    attempt["modal_closed"] = bool(close_result.get("modal_closed"))
    _wait_settle(page, 600)
    if not attempt["modal_closed"]:
        attempt["modal_closed"] = not bool(extract_complaint_modal_state(page).get("opened"))
    attempt["durable_success_state_after_close"] = bool(detect_complaint_success_state(page).get("seen"))


def feedback_filter_date_range(
    config: ActionableResolverConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
) -> tuple[str, str]:
    candidate_date = (
        normalize_date_key(expected_ui.get("review_datetime") or expected_ui.get("review_date") or expected_ui.get("created_at"))
        or normalize_date_key(api_row.get("created_at") or api_row.get("created_date") or api_row.get("review_datetime"))
    )
    if candidate_date:
        return candidate_date, candidate_date
    return config.date_from, config.date_to


def feedback_filter_stars(
    config: ActionableResolverConfig,
    api_row: Mapping[str, Any],
    *,
    expected_ui: Mapping[str, Any],
) -> tuple[int, ...]:
    rating = normalize_rating(expected_ui.get("rating") or api_row.get("product_valuation") or api_row.get("rating"))
    if rating:
        return (int(rating),)
    return tuple(config.stars)


def normalize_rating(value: Any) -> int | None:
    try:
        rating = int(str(value or "").strip())
    except ValueError:
        return None
    return rating if 1 <= rating <= 5 else None


def status_tabs_for_row(config: ActionableResolverConfig, api_row: Mapping[str, Any], expected_ui: Mapping[str, Any]) -> list[str]:
    if config.is_answered == "true":
        return [STATUS_TAB_ANSWERED]
    if config.is_answered == "false":
        return [STATUS_TAB_UNANSWERED]
    if config.is_answered == "all":
        return [STATUS_TAB_UNANSWERED, STATUS_TAB_ANSWERED]
    value = expected_ui.get("is_answered", api_row.get("is_answered"))
    if value is True or str(value).lower() in {"true", "1", "yes", "есть ответ"}:
        return [STATUS_TAB_ANSWERED, STATUS_TAB_ALL, STATUS_TAB_UNANSWERED]
    if value is False or str(value).lower() in {"false", "0", "no", "ждут ответа"}:
        return [STATUS_TAB_UNANSWERED, STATUS_TAB_ALL, STATUS_TAB_ANSWERED]
    return [STATUS_TAB_ALL, STATUS_TAB_UNANSWERED, STATUS_TAB_ANSWERED]


def to_replay_config(config: ActionableResolverConfig, *, date_from: str, date_to: str) -> ReplayConfig:
    return ReplayConfig(
        date_from=date_from,
        date_to=date_to,
        stars=config.stars,
        is_answered=config.is_answered,
        max_api_rows=config.max_api_rows,
        max_ui_rows=config.max_ui_rows,
        mode=NO_SUBMIT_MODE,
        storage_state_path=Path("."),
        wb_bot_python=Path("."),
        output_dir=Path("."),
        start_url="",
        headless=True,
        timeout_ms=20000,
        write_artifacts=False,
        apply_ui_filters="yes",
        targeted_search="no",
        max_targeted_searches=0,
    )


def empty_actionability_result() -> dict[str, Any]:
    return {
        "feedback_id": "",
        "tabs_tried": [],
        "tab_used": "",
        "attempts": [],
        "expected_ui_summary": {},
        "actionable_row_found": False,
        "row_visible": False,
        "row_menu_found": False,
        "menu_found": False,
        "complaint_action_found": False,
        "complaint_action_available": False,
        "modal_opened": False,
        "categories_found": [],
        "description_field_found": False,
        "description_value_after_fill": "",
        "description_value_after_blur": "",
        "description_value_match": False,
        "modal_closed": False,
        "submit_clicked": False,
        "date_filter_applied": False,
        "star_filter_applied": False,
        "selected_star_values_after": [],
        "list_update_observed": False,
        "search_used": False,
        "scroll_used": False,
        "dom_rows_collected": 0,
        "visible_rows_checked": 0,
        "visible_row_match": {},
        "resolved_row": {},
        "resolved_row_summary": {},
        "row_menu_click": {},
        "menu_labels": [],
        "filter_controller": {},
        "locator_strategy": "",
        "selected_category": "",
        "category_click": {},
        "description_fill": {},
        "complaint_action_click": {},
        "close_method": "",
        "submit_button_label": "",
        "block_reason": "",
    }


def empty_attempt(tab_label: str) -> dict[str, Any]:
    return {
        "tab": tab_label,
        "tab_clicked": False,
        "date_filter_applied": False,
        "star_filter_applied": False,
        "selected_star_values_after": [],
        "list_update_observed": False,
        "search_used": False,
        "scroll_used": False,
        "dom_rows_collected": 0,
        "visible_rows_checked": 0,
        "scroll_attempts": [],
        "filter_controller": {},
        "visible_row_match": {},
        "resolved_row": {},
        "locator_strategy": "",
        "row_visible": False,
        "row_menu_found": False,
        "menu_labels": [],
        "complaint_action_found": False,
        "actionable_row_found": False,
        "modal_opened": False,
        "categories_found": [],
        "description_field_found": False,
        "description_value_after_fill": "",
        "description_value_after_blur": "",
        "description_value_match": False,
        "modal_closed": False,
        "submit_clicked": False,
        "block_reason": "",
    }
