"""Smoke-check source-aware loading-table status reduction for web-vitrina."""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    _build_auto_update_result_payload,
    _build_refresh_result_payload,
    _activity_reason_ru,
    _build_web_vitrina_loading_table,
    _web_vitrina_source_status_not_loaded_activity_surface,
)
from packages.application.sheet_vitrina_v1_web_vitrina import _active_refresh_summary  # noqa: E402


TODAY = "2026-04-25"
YESTERDAY = "2026-04-24"


def main() -> None:
    _assert_not_loaded_activity_surface_is_lazy_neutral()
    _assert_current_snapshot_latest_confirmed_is_ok()
    _assert_missing_current_without_fallback_is_not_ok()
    _assert_stocks_today_not_required_is_ok()
    _assert_promo_latest_confirmed_is_ok()
    _assert_fin_report_yesterday_latest_confirmed_is_ok()
    _assert_spp_proxy_missing_public_price_reason_is_human()
    _assert_archived_onec_source_is_hidden()
    _assert_archived_onec_failure_is_nonblocking()
    _assert_empty_source_outcomes_reconcile()
    _assert_refresh_result_uses_active_source_semantics()
    print("web_vitrina_source_aware_statuses: ok")


def _assert_not_loaded_activity_surface_is_lazy_neutral() -> None:
    surface = _web_vitrina_source_status_not_loaded_activity_surface(
        snapshot_as_of_date=YESTERDAY,
        snapshot_id="snapshot-fixture",
        refreshed_at="2026-04-25T08:00:00Z",
        read_model="persisted_ready_snapshot",
        available_dates=[YESTERDAY, TODAY],
        default_refresh_date=YESTERDAY,
        metric_labels_by_source={"prices_snapshot": ["Цена со скидкой (₽)"]},
        group_last_updated_at={"wb_api": "2026-04-25T08:00:00Z"},
    )
    loading_table = surface["loading_table"]
    if loading_table["source_status_state"] != "not_loaded":
        raise AssertionError(f"not_loaded source-status state mismatch, got {surface}")
    if loading_table["rows"] or loading_table["groups"] or loading_table["columns"]:
        raise AssertionError(f"not_loaded source-status must not expose stale rows/groups/columns, got {surface}")
    if surface["upload_summary"]["items"]:
        raise AssertionError(f"not_loaded source-status must not expose stale summary items, got {surface}")
    if "не OK" in str(surface):
        raise AssertionError(f"not_loaded source-status must not reduce missing details to not OK, got {surface}")


def _assert_archived_onec_source_is_hidden() -> None:
    table = _build_web_vitrina_loading_table(
        upload_summary={"items": [_item("onec_stocks", [])]},
        today_date=TODAY,
        yesterday_date=YESTERDAY,
        available_dates=[YESTERDAY, TODAY],
        default_refresh_date=YESTERDAY,
        metric_labels_by_source={"onec_stocks": []},
        group_last_updated_at={"onec_product_capital": "2026-04-25T08:00:00Z"},
    )
    if table["rows"] or any(
        item.get("group_id") == "onec_product_capital" for item in table["groups"]
    ):
        raise AssertionError(f"archived 1C source must not leak into active loading surface, got {table}")


def _assert_archived_onec_failure_is_nonblocking() -> None:
    summary = _active_refresh_summary(
        SimpleNamespace(
            semantic_status="error",
            semantic_label="Ошибка",
            semantic_tone="error",
            semantic_reason="1C failed",
            source_outcome_counts={"success": 1, "warning": 0, "error": 1},
            source_outcomes=[
                {"source_key": "stocks", "status": "success"},
                {"source_key": "onec_stocks", "status": "error"},
            ],
        )
    )
    if summary["status"] != "success" or summary["counts"]["error"] != 0:
        raise AssertionError(f"archived-only 1C failure must not drive active status, got {summary}")
    if sum(summary["counts"].values()) != len(summary["outcomes"]):
        raise AssertionError(f"active source counts must equal the visible outcome list, got {summary}")

    archived_only = _active_refresh_summary(
        SimpleNamespace(
            semantic_status="error",
            semantic_label="Ошибка",
            semantic_tone="error",
            semantic_reason="1C failed",
            source_outcome_counts={"success": 0, "warning": 0, "error": 1},
            source_outcomes=[{"source_key": "onec_stocks", "status": "error"}],
        )
    )
    if archived_only["counts"] != {"success": 0, "warning": 0, "error": 0} or archived_only["outcomes"]:
        raise AssertionError(f"archived-only status must expose an empty reconciled active list, got {archived_only}")


def _assert_empty_source_outcomes_reconcile() -> None:
    summary = _active_refresh_summary(
        SimpleNamespace(
            semantic_status="warning",
            semantic_label="Внимание",
            semantic_tone="warning",
            semantic_reason="No persisted source detail",
            source_outcome_counts={"success": 4, "warning": 2, "error": 1},
            source_outcomes=[],
        )
    )
    if summary["counts"] != {"success": 0, "warning": 0, "error": 0} or summary["outcomes"]:
        raise AssertionError(f"empty active source list must not retain technical source counts, got {summary}")


def _assert_refresh_result_uses_active_source_semantics() -> None:
    result = SimpleNamespace(
        semantic_status="error",
        semantic_label="Ошибка",
        semantic_tone="error",
        semantic_reason="archived 1C failed",
        source_outcome_counts={"success": 1, "warning": 0, "error": 1},
        source_outcomes=[
            {"source_key": "stocks", "status": "success"},
            {"source_key": "onec_stocks", "status": "error"},
        ],
        snapshot_id="snapshot-active-source-smoke",
        as_of_date=TODAY,
        refreshed_at="2026-04-25T08:00:00Z",
    )
    payload = _build_refresh_result_payload(result)
    if payload["semantic_status"] != "success" or payload["technical_semantic_status"] != "error":
        raise AssertionError(f"refresh/job result must reduce archived sources and retain raw audit, got {payload}")
    if payload["source_outcome_counts"] != {"success": 1, "warning": 0, "error": 0}:
        raise AssertionError(f"refresh/job active counts mismatch, got {payload}")
    auto_result = _build_auto_update_result_payload(
        refresh_payload=payload,
        load_payload=None,
        technical_status="success",
        finished_at="2026-04-25T08:01:00Z",
        error=None,
    )
    if auto_result["semantic_status"] != "success":
        raise AssertionError(f"archived-only failure must not persist a failed auto job, got {auto_result}")


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


def _assert_spp_proxy_missing_public_price_reason_is_human() -> None:
    reason = _activity_reason_ru(
        tone="warning",
        detail="",
        note="missing_public_buyer_price=3; resolution_rule=accepted_current_current_attempt",
    )
    expected = "публичная цена WB не получена для 3 SKU"
    if reason != expected:
        raise AssertionError(f"SPP proxy missing public price reason mismatch: expected {expected!r}, got {reason!r}")


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
