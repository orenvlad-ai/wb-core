"""Local smoke checks for complaint batch funnel accounting."""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.seller_portal_feedbacks_complaint_batch import (  # noqa: E402
    _batch_aggregate,
    _merge_candidate_state,
    _reason_group,
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
            ],
        },
    )
    _merge_candidate_state(
        state,
        day="2026-04-01",
        run_report={"run_id": "run-2", "candidates": [_candidate("pending-1", "review", submit_success=True)]},
    )
    aggregate = _batch_aggregate(state, [{"runs": [{"run_id": "run-1"}, {"run_id": "run-2"}]}])
    if aggregate["ai_candidates_to_complain"] != 4:
        raise AssertionError(f"yes/review denominator must exclude AI no: {aggregate} state={state}")
    if aggregate["submitted"] != 2:
        raise AssertionError(f"submitted retry pass must update pending candidate: {aggregate} state={state}")
    if aggregate["not_submitted_reasons"].get("already_in_journal") != 1:
        raise AssertionError(f"already-in-journal reason must be grouped: {aggregate}")
    if aggregate["not_submitted_reasons"].get("already_complained_in_wb") != 1:
        raise AssertionError(f"disabled already-complained reason must be grouped: {aggregate}")
    if _reason_group("description field value mismatch before final submit") != "description_not_persisted":
        raise AssertionError("description mismatch must be grouped as description_not_persisted")
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


if __name__ == "__main__":
    main()
