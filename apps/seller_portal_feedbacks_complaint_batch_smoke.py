"""Local smoke checks for complaint batch funnel accounting."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_batch import (  # noqa: E402
    OTHER_WITH_DETAIL,
    _batch_aggregate,
    _classify_not_submitted,
    _continuation_window_from_batch_report,
    _finalize_unprocessed,
    _has_pending_feedback_ids_for_day,
    _merge_candidate_state,
    _not_submitted_inventory,
    _not_submitted_feedback_ids_for_day,
    _reason_group,
    _systemic_blocker,
)


def main() -> None:
    state: dict[str, dict[str, object]] = {}
    _merge_candidate_state(
        state,
        day="2026-04-01",
        run_report={
            "run_id": "run-1",
            "candidates": [
                _candidate("submitted-1", "yes", submit_success=True),
                _candidate("journal-1", "review", skip_reason="complaint already exists for feedback_id with status=waiting_response"),
                _candidate("wb-1", "yes", blocker="already_complained_in_wb: complaint action is disabled"),
                _candidate("no-1", "no", skip_reason="skipped complaint_fit=no"),
                _candidate("pending-1", "review", selected=True),
                _candidate("slot-1", "review", skip_reason="skipped because max_ai_candidates slots were already filled"),
            ],
        },
    )
    _merge_candidate_state(
        state,
        day="2026-04-01",
        run_report={"run_id": "run-2", "candidates": [_candidate("pending-1", "review", submit_success=True)]},
    )
    aggregate = _batch_aggregate(state, [{"runs": [{"run_id": "run-1"}, {"run_id": "run-2"}]}])
    if aggregate["ai_candidates_to_complain"] != 5:
        raise AssertionError(f"yes/review denominator must exclude AI no: {aggregate} state={state}")
    if aggregate["submitted"] != 2:
        raise AssertionError(f"submitted retry pass must update pending candidate: {aggregate} state={state}")
    if aggregate["not_submitted_reasons"].get("already_in_journal") != 1:
        raise AssertionError(f"already-in-journal reason must be grouped: {aggregate}")
    if aggregate["not_submitted_reasons"].get("already_complained_in_wb") != 1:
        raise AssertionError(f"disabled already-complained reason must be grouped: {aggregate}")
    denied_next = _not_submitted_feedback_ids_for_day(state, "2026-04-01")
    if denied_next != ("journal-1", "wb-1"):
        raise AssertionError(f"non-journal skips must be denied on the next bounded run: {denied_next!r}")
    if not _has_pending_feedback_ids_for_day(state, "2026-04-01"):
        raise AssertionError(f"max_ai_candidates overflow must stay pending for continuation: {state}")
    if _reason_group("description field value mismatch before final submit") != "description_not_persisted":
        raise AssertionError("description mismatch must be grouped as description_not_persisted")
    if _reason_group("submit clicked once, but success was not confirmed by network/toast/post-row evidence") != "submit_unconfirmed":
        raise AssertionError("not-confirmed submit click must be grouped as submit_unconfirmed")
    if _reason_group("unknown candidate-level skip") != OTHER_WITH_DETAIL:
        raise AssertionError("generic other must be stored as other_with_explicit_detail")
    group, detail = _classify_not_submitted(
        "exact actionable DOM row was not found after target-probe filter/materialization path",
        candidate=_actionability_candidate(
            feedback_id="short-1",
            is_answered=True,
            rating=1,
            not_found_reason="not_found_due_to_short_text_or_duplicate",
            expected_tab="Есть ответ",
            date_filter_applied=True,
            star_filter_applied=True,
            selected_stars=[1],
            dom_rows=5,
        ),
    )
    if group != "exact_match_not_found" or "short_text" not in detail:
        raise AssertionError(f"short/duplicate non-materialization must be exact_match_not_found: {group=} {detail=}")
    group, detail = _classify_not_submitted(
        "exact actionable DOM row was not found after target-probe filter/materialization path",
        candidate=_actionability_candidate(
            feedback_id="coverage-1",
            is_answered=True,
            rating=1,
            not_found_reason="not_found_due_to_no_ui_coverage",
            expected_tab="Есть ответ",
            date_filter_applied=True,
            star_filter_applied=True,
            selected_stars=[1],
            dom_rows=5,
        ),
    )
    if group != "actionable_dom_row_not_found_true_unavailable" or "dom_rows_collected=5" not in detail:
        raise AssertionError(f"no-ui-coverage must be classified as true unavailable: {group=} {detail=}")
    group, detail = _classify_not_submitted(
        "exact actionable DOM row was not found after target-probe filter/materialization path",
        candidate=_actionability_candidate(
            feedback_id="filter-1",
            is_answered=True,
            rating=2,
            not_found_reason="not_found_due_to_no_ui_coverage",
            expected_tab="Есть ответ",
            date_filter_applied=True,
            star_filter_applied=True,
            selected_stars=[1],
            dom_rows=5,
        ),
    )
    if group != "actionable_dom_row_not_found_filter_issue" or "expected_rating=2" not in detail:
        raise AssertionError(f"missing selected star must be a filter issue: {group=} {detail=}")
    group, detail = _classify_not_submitted(
        "exact actionable DOM row was not found after target-probe filter/materialization path",
        candidate=_actionability_candidate(
            feedback_id="tab-1",
            is_answered=True,
            rating=1,
            not_found_reason="not_found_due_to_no_ui_coverage",
            expected_tab="Ждут ответа",
            date_filter_applied=True,
            star_filter_applied=True,
            selected_stars=[1],
            dom_rows=5,
        ),
    )
    if group != "actionable_dom_row_not_found_wrong_tab":
        raise AssertionError(f"wrong status tab must be classified: {group=} {detail=}")
    pending_state = {
        "pending-2": {
            "feedback_id": "pending-2",
            "date": "2026-04-08",
            "status": "pending",
            "reason": "selected but not attempted yet",
        }
    }
    _finalize_unprocessed(
        pending_state,
        [{"date": "2026-04-08", "stopped_reason": "submit_unconfirmed_error"}],
        [{"date": "2026-04-08", "message": "submit_unconfirmed_error"}],
    )
    if pending_state["pending-2"]["reason_group"] != "submit_unconfirmed":
        raise AssertionError(f"pending candidate after unconfirmed stop must be explicit: {pending_state}")
    inventory = _not_submitted_inventory(
        {
            "candidates": {
                "submitted-1": {"feedback_id": "submitted-1", "status": "submitted"},
                "skip-1": {
                    "feedback_id": "skip-1",
                    "date": "2026-04-08",
                    "status": "not_submitted",
                    "complaint_fit": "review",
                    "reason_group": "actionable_dom_row_not_found_true_unavailable",
                    "reason_detail": "expected_tab=Есть ответ; dom_rows_collected=5; cursor_rows_collected=0",
                    "reason": "exact actionable DOM row was not found after target-probe filter/materialization path",
                },
            }
        }
    )
    if len(inventory) != 1 or inventory[0]["feedback_id"] != "skip-1":
        raise AssertionError(f"not-submitted inventory parser must extract skipped rows only: {inventory}")
    if _continuation_window_from_batch_report(
        {"days": [{"date": "2026-04-08", "stopped_reason": "submit_unconfirmed_error"}]}, calendar_days=10
    ) != ("2026-04-08", "2026-04-17"):
        raise AssertionError("unconfirmed partial day must resume from the same calendar day")
    if _continuation_window_from_batch_report(
        {"days": [{"date": "2026-04-08", "stopped_reason": "day_exhausted_or_no_more_safe_candidates"}]},
        calendar_days=10,
    ) != ("2026-04-09", "2026-04-18"):
        raise AssertionError("exhausted day must resume from the next calendar day")
    source_error = _systemic_blocker(
        {
            "final_conclusion": "source_error",
            "aggregate": {},
            "errors": [{"stage": "api_feedbacks", "message": "required env WB_API_TOKEN is not set"}],
        }
    )
    if "WB_API_TOKEN" not in source_error:
        raise AssertionError(f"API/source failures must stop the batch explicitly: {source_error!r}")
    print("seller_portal_feedbacks_complaint_batch_smoke: OK")


def _candidate(
    feedback_id: str,
    fit: str,
    *,
    submit_success: bool = False,
    skip_reason: str = "",
    blocker: str = "",
    selected: bool = False,
) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "ai": {"complaint_fit": fit},
        "selected_for_dry_run": selected or submit_success or bool(blocker),
        "skip_reason": skip_reason,
        "modal": {"submit_success": submit_success, "blocker": blocker},
    }


def _actionability_candidate(
    *,
    feedback_id: str,
    is_answered: bool,
    rating: int,
    not_found_reason: str,
    expected_tab: str,
    date_filter_applied: bool,
    star_filter_applied: bool,
    selected_stars: list[int],
    dom_rows: int,
) -> dict[str, object]:
    return {
        "feedback_id": feedback_id,
        "ai": {"complaint_fit": "review"},
        "api_summary": {"feedback_id": feedback_id, "is_answered": is_answered, "rating": str(rating)},
        "match": {"match_status": "not_found", "not_found_reason": not_found_reason, "candidate_count": dom_rows},
        "modal": {
            "actionability_resolver": {
                "feedback_id": feedback_id,
                "tabs_tried": [expected_tab],
                "tab_used": expected_tab,
                "date_filter_applied": date_filter_applied,
                "star_filter_applied": star_filter_applied,
                "selected_star_values_after": selected_stars,
                "dom_rows_collected": dom_rows,
                "attempts": [
                    {
                        "tab": expected_tab,
                        "date_filter_applied": date_filter_applied,
                        "star_filter_applied": star_filter_applied,
                        "selected_star_values_after": selected_stars,
                        "dom_rows_collected": dom_rows,
                        "scroll_attempts": [
                            {
                                "scroll_attempts": 3,
                                "max_scroll_attempts": 30,
                                "stop_reason": "no_new_rows_after_scroll",
                            }
                        ],
                    }
                ],
            }
        },
    }


if __name__ == "__main__":
    main()
