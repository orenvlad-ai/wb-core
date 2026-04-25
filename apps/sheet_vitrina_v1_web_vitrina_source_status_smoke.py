"""Smoke-check source-aware loading-table status reduction for web-vitrina."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import _build_web_vitrina_loading_table  # noqa: E402


TODAY = "2026-04-25"
YESTERDAY = "2026-04-24"


def main() -> None:
    _assert_current_snapshot_latest_confirmed_is_ok()
    _assert_missing_current_without_fallback_is_not_ok()
    _assert_stocks_today_not_required_is_ok()
    _assert_promo_latest_confirmed_is_ok()
    _assert_fin_report_yesterday_latest_confirmed_is_ok()
    print("web_vitrina_source_aware_statuses: ok")


def _assert_current_snapshot_latest_confirmed_is_ok() -> None:
    rows = _rows(
        [
            _item(
                "prices_snapshot",
                [
                    _slot(
                        "yesterday_closed",
                        YESTERDAY,
                        status="warning",
                        kind="success",
                        note="resolution_rule=accepted_closed_from_prior_current_snapshot",
                        covered_count=2,
                    ),
                    _slot(
                        "today_current",
                        TODAY,
                        status="warning",
                        kind="success",
                        note="resolution_rule=accepted_current_preserved_after_invalid_attempt",
                        covered_count=2,
                    ),
                ],
            ),
            _item(
                "ads_bids",
                [
                    _slot(
                        "yesterday_closed",
                        YESTERDAY,
                        status="warning",
                        kind="success",
                        note="resolution_rule=accepted_closed_from_prior_current_cache",
                        covered_count=2,
                    ),
                    _slot(
                        "today_current",
                        TODAY,
                        status="warning",
                        kind="success",
                        note="resolution_rule=accepted_prior_current_runtime_cache",
                        covered_count=2,
                    ),
                ],
            ),
        ]
    )
    for source_key in ("prices_snapshot", "ads_bids"):
        row = _row(rows, source_key)
        if not row["yesterday"]["ok"] or not row["today"]["ok"]:
            raise AssertionError(f"{source_key} latest confirmed slots must be OK, got {row}")


def _assert_missing_current_without_fallback_is_not_ok() -> None:
    row = _row(
        _rows(
            [
                _item(
                    "prices_snapshot",
                    [
                        _slot(
                            "yesterday_closed",
                            YESTERDAY,
                            status="warning",
                            kind="success",
                            note="resolution_rule=accepted_closed_from_prior_current_snapshot",
                            covered_count=2,
                        ),
                        _slot(
                            "today_current",
                            TODAY,
                            status="warning",
                            kind="missing",
                            note="no payload returned",
                            covered_count=0,
                        ),
                    ],
                )
            ]
        ),
        "prices_snapshot",
    )
    if row["today"]["ok"]:
        raise AssertionError(f"missing current snapshot without accepted fallback must stay not OK, got {row}")


def _assert_stocks_today_not_required_is_ok() -> None:
    row = _row(
        _rows(
            [
                _item(
                    "stocks",
                    [
                        _slot(
                            "yesterday_closed",
                            YESTERDAY,
                            status="warning",
                            kind="missing",
                            note="no payload returned",
                            covered_count=0,
                        ),
                        _slot(
                            "today_current",
                            TODAY,
                            status="warning",
                            kind="not_available",
                            note=(
                                "source is not available for today_current in the bounded live contour; "
                                "today column stays blank instead of inventing fresh values"
                            ),
                            covered_count=0,
                        ),
                    ],
                )
            ]
        ),
        "stocks",
    )
    if not row["today"]["ok"] or row["yesterday"]["ok"]:
        raise AssertionError(f"stocks today must be OK/non-degrading while required yesterday remains strict, got {row}")


def _assert_promo_latest_confirmed_is_ok() -> None:
    row = _row(
        _rows(
            [
                _item(
                    "promo_by_price",
                    [
                        _slot(
                            "yesterday_closed",
                            YESTERDAY,
                            status="warning",
                            kind="success",
                            note="resolution_rule=accepted_closed_from_interval_replay",
                            covered_count=2,
                        ),
                        _slot(
                            "today_current",
                            TODAY,
                            status="warning",
                            kind="success",
                            note="resolution_rule=exact_date_promo_current_runtime_cache",
                            covered_count=2,
                        ),
                    ],
                )
            ]
        ),
        "promo_by_price",
    )
    if not row["yesterday"]["ok"] or not row["today"]["ok"]:
        raise AssertionError(f"promo latest confirmed slots must be OK, got {row}")


def _assert_fin_report_yesterday_latest_confirmed_is_ok() -> None:
    row = _row(
        _rows(
            [
                _item(
                    "fin_report_daily",
                    [
                        _slot(
                            "yesterday_closed",
                            YESTERDAY,
                            status="warning",
                            kind="success",
                            note="resolution_rule=accepted_closed_runtime_snapshot",
                            covered_count=2,
                        ),
                        _slot(
                            "today_current",
                            TODAY,
                            status="warning",
                            kind="missing",
                            note="invalid_exact_snapshot=zero_like_fin_report_daily",
                            covered_count=0,
                        ),
                    ],
                )
            ]
        ),
        "fin_report_daily",
    )
    if not row["yesterday"]["ok"]:
        raise AssertionError(f"fin_report_daily yesterday accepted truth must be OK, got {row}")
    if row["yesterday"]["label"] != "OK":
        raise AssertionError(f"fin_report_daily yesterday label must be OK, got {row}")


def _rows(items: list[dict[str, object]]) -> list[dict[str, object]]:
    table = _build_web_vitrina_loading_table(
        upload_summary={"items": items},
        today_date=TODAY,
        yesterday_date=YESTERDAY,
        available_dates=[YESTERDAY, TODAY],
        default_refresh_date=YESTERDAY,
        metric_labels_by_source={},
        group_last_updated_at={},
    )
    return list(table["rows"])


def _row(rows: list[dict[str, object]], source_key: str) -> dict[str, object]:
    for row in rows:
        if row["source_key"] == source_key:
            return row
    raise AssertionError(f"missing source row {source_key}: {rows}")


def _item(source_key: str, slots: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_key": source_key,
        "endpoint_id": source_key,
        "endpoint_label": source_key,
        "label_ru": source_key,
        "status_label": "Внимание",
        "tone": "warning",
        "detail": "fixture",
        "slot_statuses": slots,
    }


def _slot(
    temporal_slot: str,
    date_value: str,
    *,
    status: str,
    kind: str,
    note: str,
    covered_count: int,
) -> dict[str, object]:
    return {
        "temporal_slot": temporal_slot,
        "status": status,
        "tone": status,
        "label": "Внимание" if status == "warning" else "Успешно",
        "reason": note,
        "kind": kind,
        "note": note,
        "requested_count": 2,
        "covered_count": covered_count,
        "snapshot_date": date_value,
        "date": date_value,
        "date_from": date_value,
        "date_to": date_value,
    }


if __name__ == "__main__":
    main()
