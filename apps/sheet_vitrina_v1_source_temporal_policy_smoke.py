"""Targeted smoke-check for source-aware temporal policy reduction in sheet_vitrina_v1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.contracts.sheet_vitrina_v1 import (
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)

BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-21"
TODAY_CURRENT_DATE = "2026-04-22"
REFRESHED_AT = "2026-04-22T08:05:00Z"
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-source-temporal-policy-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-22T08:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")
        current_state = runtime.load_current_state()
        enabled = [item for item in current_state.config_v2 if item.enabled]
        if not enabled:
            raise AssertionError("fixture must expose at least one enabled SKU")

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=REFRESHED_AT,
            plan=_build_plan(first_nm_id=enabled[0].nm_id),
        )
        refresh_status = runtime.load_sheet_vitrina_refresh_status(as_of_date=AS_OF_DATE)
        if refresh_status.semantic_status != "error":
            raise AssertionError(f"aggregate semantic status must stay error because dual-day source failed, got {refresh_status}")
        if refresh_status.source_outcome_counts != {"success": 10, "warning": 1, "error": 1}:
            raise AssertionError(f"source outcome counts mismatch, got {refresh_status.source_outcome_counts}")
        if refresh_status.source_temporal_policies.get("stocks") != "yesterday_closed_only":
            raise AssertionError(f"stocks policy must be normalized on reread, got {refresh_status.source_temporal_policies}")
        if refresh_status.source_temporal_policies.get("spp") != "dual_day_intraday_tolerant":
            raise AssertionError(f"spp policy must be normalized on reread, got {refresh_status.source_temporal_policies}")
        if refresh_status.source_temporal_policies.get("fin_report_daily") != "dual_day_intraday_tolerant":
            raise AssertionError(f"fin_report_daily policy must be normalized on reread, got {refresh_status.source_temporal_policies}")

        source_outcomes = {item["source_key"]: item for item in refresh_status.source_outcomes}
        if source_outcomes["stocks"]["status"] != "success":
            raise AssertionError(f"stocks must stay green with non-required today_current, got {source_outcomes['stocks']}")
        if "текущий день для этого источника не требуется" not in str(source_outcomes["stocks"]["reason"]):
            raise AssertionError(f"stocks reason must explain yesterday-only policy, got {source_outcomes['stocks']}")
        if source_outcomes["fin_report_daily"]["status"] != "success":
            raise AssertionError(f"fin_report_daily must stay green on tolerated intraday non-yield, got {source_outcomes['fin_report_daily']}")
        if source_outcomes["spp"]["status"] != "success":
            raise AssertionError(f"spp must stay green on tolerated intraday non-yield, got {source_outcomes['spp']}")
        if "текущий день для этого источника ещё не дал финальные данные" not in str(source_outcomes["fin_report_daily"]["reason"]):
            raise AssertionError(f"fin_report_daily reason must explain tolerated intraday miss, got {source_outcomes['fin_report_daily']}")
        if "текущий день для этого источника ещё не дал финальные данные" not in str(source_outcomes["spp"]["reason"]):
            raise AssertionError(f"spp reason must explain tolerated intraday miss, got {source_outcomes['spp']}")
        if source_outcomes["seller_funnel_snapshot"]["status"] != "error":
            raise AssertionError(f"seller_funnel_snapshot must keep required dual-day failure red, got {source_outcomes['seller_funnel_snapshot']}")
        if source_outcomes["web_source_snapshot"]["status"] != "warning":
            raise AssertionError(f"web_source_snapshot must keep required dual-day warning, got {source_outcomes['web_source_snapshot']}")
        if source_outcomes["prices_snapshot"]["status"] != "success":
            raise AssertionError(f"prices_snapshot accepted-current rollover semantics must stay intact, got {source_outcomes['prices_snapshot']}")
        if source_outcomes["ads_bids"]["status"] != "success":
            raise AssertionError(f"ads_bids accepted-current rollover semantics must stay intact, got {source_outcomes['ads_bids']}")

        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=runtime.runtime_dir,
            runtime=runtime,
            now_factory=lambda: datetime(2026, 4, 22, 8, 30, tzinfo=timezone.utc),
        )
        status_payload = entrypoint.handle_sheet_status_request(as_of_date=AS_OF_DATE)
        if status_payload["status"] != "error":
            raise AssertionError(f"/status semantic payload mismatch, got {status_payload}")
        if status_payload["source_outcome_counts"] != {"success": 10, "warning": 1, "error": 1}:
            raise AssertionError(f"/status source_outcome_counts mismatch, got {status_payload}")

        contract_payload = entrypoint.handle_sheet_web_vitrina_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            as_of_date=AS_OF_DATE,
        )
        policy_counts = contract_payload["status_summary"]["source_policy_counts"]
        if policy_counts.get("yesterday_closed_only") != 1:
            raise AssertionError(f"web-vitrina contract must expose normalized stocks policy count, got {policy_counts}")
        if policy_counts.get("dual_day_intraday_tolerant") != 2:
            raise AssertionError(f"web-vitrina contract must expose intraday-tolerant policy count, got {policy_counts}")

        composition_payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            as_of_date=AS_OF_DATE,
            include_source_status=True,
        )
        if composition_payload["status_badge"]["tone"] != "error":
            raise AssertionError(f"top badge must follow required-slot failures only, got {composition_payload['status_badge']}")
        loading_rows = composition_payload["activity_surface"]["loading_table"]["rows"]
        if [item["source_key"] for item in loading_rows[:2]] != [
            "seller_funnel_snapshot",
            "web_source_snapshot",
        ]:
            raise AssertionError(f"activity order must stay error -> warning -> success, got {loading_rows}")
        item_by_source = {item["source_key"]: item for item in loading_rows}
        if item_by_source["stocks"]["yesterday"]["tone"] != "success":
            raise AssertionError(f"stocks card must stay green, got {item_by_source['stocks']}")
        if item_by_source["fin_report_daily"]["yesterday"]["tone"] != "success":
            raise AssertionError(f"fin_report_daily card must stay green, got {item_by_source['fin_report_daily']}")
        if item_by_source["spp"]["yesterday"]["tone"] != "success":
            raise AssertionError(f"spp yesterday cell must stay green, got {item_by_source['spp']}")
        if _loading_row_tone(item_by_source["seller_funnel_snapshot"]) != "error":
            raise AssertionError(f"seller_funnel_snapshot card must stay red, got {item_by_source['seller_funnel_snapshot']}")
        if _loading_row_tone(item_by_source["web_source_snapshot"]) != "error":
            raise AssertionError(f"web_source_snapshot loading row must show non-OK, got {item_by_source['web_source_snapshot']}")
        if _loading_row_tone(item_by_source["prices_snapshot"]) != "success":
            raise AssertionError(f"prices_snapshot current-only rollover row must stay OK, got {item_by_source['prices_snapshot']}")
        if _loading_row_tone(item_by_source["ads_bids"]) != "success":
            raise AssertionError(f"ads_bids current-only rollover row must stay OK, got {item_by_source['ads_bids']}")

        print("source_temporal_policy_counts: ok ->", refresh_status.source_outcome_counts)
        print("stocks_policy_overlay: ok ->", refresh_status.source_temporal_policies["stocks"])
        print("intraday_tolerant_sources: ok ->", source_outcomes["spp"]["status"], source_outcomes["fin_report_daily"]["status"])
        print("required_dual_day_sources: ok ->", source_outcomes["seller_funnel_snapshot"]["status"], source_outcomes["web_source_snapshot"]["status"])
        print("page_composition_top_badge: ok ->", composition_payload["status_badge"]["tone"])


def _build_plan(*, first_nm_id: int) -> SheetVitrinaV1Envelope:
    legacy_policies = {
        "seller_funnel_snapshot": "dual_day_capable",
        "sales_funnel_history": "dual_day_capable",
        "web_source_snapshot": "dual_day_capable",
        "sf_period": "dual_day_capable",
        "spp": "dual_day_capable",
        "stocks": "dual_day_capable",
        "ads_compact": "dual_day_capable",
        "fin_report_daily": "dual_day_capable",
        "prices_snapshot": "accepted_current_rollover",
        "ads_bids": "accepted_current_rollover",
        "cost_price": "dual_day_capable",
        "promo_by_price": "dual_day_capable",
    }
    status_rows = [
        _status_row("seller_funnel_snapshot[yesterday_closed]", "success", AS_OF_DATE, AS_OF_DATE, note=""),
        _status_row("seller_funnel_snapshot[today_current]", "error", TODAY_CURRENT_DATE, TODAY_CURRENT_DATE, note="no payload returned"),
        _status_row(
            "sales_funnel_history[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=accepted_closed_current_attempt",
        ),
        _status_row(
            "sales_funnel_history[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=accepted_current_current_attempt",
        ),
        _status_row("web_source_snapshot[yesterday_closed]", "success", AS_OF_DATE, AS_OF_DATE, note=""),
        _status_row(
            "web_source_snapshot[today_current]",
            "not_found",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note=f"requested_date={TODAY_CURRENT_DATE}; latest_available_date={AS_OF_DATE}",
        ),
        _status_row(
            "sf_period[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=accepted_closed_current_attempt",
        ),
        _status_row(
            "sf_period[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=accepted_current_current_attempt",
        ),
        _status_row(
            "stocks[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=exact_date_stocks_history_runtime_cache; resolution_rule=accepted_closed_current_attempt",
        ),
        _status_row(
            "stocks[today_current]",
            "not_available",
            "",
            "",
            note=(
                "source is not available for today_current in the bounded live contour; "
                "today column stays blank instead of inventing fresh values"
            ),
        ),
        _status_row("spp[yesterday_closed]", "success", AS_OF_DATE, AS_OF_DATE, note=""),
        _status_row(
            "spp[today_current]",
            "error",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="HTTP 429 Too Many Requests; timeout while requesting current day",
        ),
        _status_row("fin_report_daily[yesterday_closed]", "success", AS_OF_DATE, AS_OF_DATE, note=""),
        _status_row(
            "fin_report_daily[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            requested_count=1,
            covered_count=0,
            note="fin_storage_fee_total=0.0; invalid_exact_snapshot",
        ),
        _status_row(
            "prices_snapshot[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=accepted_closed_from_prior_current_snapshot",
        ),
        _status_row(
            "prices_snapshot[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=accepted_current_current_attempt",
        ),
        _status_row(
            "ads_bids[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=accepted_closed_from_prior_current_snapshot",
        ),
        _status_row(
            "ads_bids[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=accepted_current_current_attempt",
        ),
        _status_row(
            "ads_compact[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=accepted_closed_current_attempt",
        ),
        _status_row(
            "ads_compact[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=accepted_current_current_attempt",
        ),
        _status_row(
            "cost_price[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=latest_effective_from<=slot_date",
        ),
        _status_row(
            "cost_price[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=latest_effective_from<=slot_date",
        ),
        _status_row(
            "promo_by_price[yesterday_closed]",
            "success",
            AS_OF_DATE,
            AS_OF_DATE,
            note="resolution_rule=accepted_closed_current_attempt",
        ),
        _status_row(
            "promo_by_price[today_current]",
            "success",
            TODAY_CURRENT_DATE,
            TODAY_CURRENT_DATE,
            note="resolution_rule=accepted_current_current_attempt",
        ),
    ]
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id="source-temporal-policy-fixture",
        as_of_date=AS_OF_DATE,
        date_columns=[AS_OF_DATE, TODAY_CURRENT_DATE],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="Yesterday closed",
                column_date=AS_OF_DATE,
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="Today current",
                column_date=TODAY_CURRENT_DATE,
            ),
        ],
        source_temporal_policies=legacy_policies,
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect="A1:D2",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", AS_OF_DATE, TODAY_CURRENT_DATE],
                rows=[
                    ["SKU A: Показы в воронке", f"SKU:{first_nm_id}|view_count", 10, 12],
                ],
                row_count=1,
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect=f"A1:K{len(status_rows) + 1}",
                clear_range="A:Z",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=status_rows,
                row_count=len(status_rows),
                column_count=len(STATUS_HEADER),
            ),
        ],
    )


def _status_row(
    source_key: str,
    kind: str,
    freshness: str,
    snapshot_date: str,
    *,
    requested_count: int = 1,
    covered_count: int = 1,
    note: str,
) -> list[object]:
    return [
        source_key,
        kind,
        freshness,
        snapshot_date,
        snapshot_date,
        snapshot_date,
        snapshot_date,
        requested_count,
        covered_count,
        "",
        note,
    ]


def _loading_row_tone(row: dict[str, object]) -> str:
    tones = []
    for key in ("today", "yesterday"):
        status = row.get(key)
        if isinstance(status, dict):
            tones.append(str(status.get("tone") or "warning"))
    if "error" in tones:
        return "error"
    if any(tone != "success" for tone in tones):
        return "warning"
    return "success"


if __name__ == "__main__":
    main()
