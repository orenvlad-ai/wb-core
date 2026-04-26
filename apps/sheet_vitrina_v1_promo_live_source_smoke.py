"""Targeted smoke-check for live-wired promo source semantics."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.promo_metric_truth import PromoCandidateRow, evaluate_candidate_rows
from packages.application.promo_xlsx_collector_block import parse_period_text
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.sheet_vitrina_v1_live_plan import (
    EXECUTION_MODE_AUTO_DAILY,
    TEMPORAL_SLOT_TODAY_CURRENT,
    SheetVitrinaV1LivePlanBlock,
)
from packages.contracts.promo_live_source import (
    PromoLiveSourceEnvelope,
    PromoLiveSourceIncomplete,
    PromoLiveSourceItem,
    PromoLiveSourceRequest,
    PromoLiveSourceSuccess,
)
from packages.contracts.promo_xlsx_collector_block import PromoMetadata


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-19"
CURRENT_DATE = "2026-04-20"
ACTIVATED_AT = "2026-04-20T06:00:00Z"
INTERVAL_REPLAY_DATE = "2026-04-17"
PRICES_ACCEPTED_CURRENT_ROLE = "accepted_current_snapshot"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]][:3]
    if len(requested_nm_ids) < 3:
        raise AssertionError("fixture bundle must expose at least three enabled nm_ids")

    _assert_cross_year_parse_rule()
    metric_truth_note = _assert_candidate_entry_price_metric_cases()
    canonical_note = _assert_canonical_eligible_set_cases(requested_nm_ids)
    web_vitrina_note = _assert_web_vitrina_period_payload_for_promo_dates(bundle, requested_nm_ids)
    _assert_promo_source_runtime_mapping(bundle, requested_nm_ids)
    preserved_note = _assert_accepted_current_preserved_after_invalid_attempt(bundle, requested_nm_ids)
    replay_note = _assert_historical_interval_replay_cache_fill(requested_nm_ids)

    print("cross_year_parse_rule: ok -> low-confidence period keeps null exact dates")
    print(f"candidate_entry_price_metric_cases: ok -> {metric_truth_note}")
    print(f"canonical_eligible_set: ok -> {canonical_note}")
    print(f"web_vitrina_period_payload: ok -> {web_vitrina_note}")
    print("promo_source_runtime_mapping: ok -> promo metrics reach STATUS and DATA_VITRINA via runtime source")
    print(f"accepted_current_preservation: ok -> {preserved_note}")
    print(f"historical_interval_replay: ok -> {replay_note}")
    print("smoke-check passed")


def _assert_cross_year_parse_rule() -> None:
    start_at, end_at, confidence = parse_period_text(
        "28 декабря 02:00 → 04 января 01:59",
        reference_year=2026,
    )
    if (start_at, end_at, confidence) != (None, None, "low"):
        raise AssertionError(
            "cross-year short labels must not invent exact promo_start_at/promo_end_at"
        )


def _assert_promo_source_runtime_mapping(bundle: dict[str, Any], requested_nm_ids: list[int]) -> None:
    yesterday_items = [
        PromoLiveSourceItem(snapshot_date=AS_OF_DATE, nm_id=requested_nm_ids[0], promo_count_by_price=1.0, promo_entry_price_best=500.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=AS_OF_DATE, nm_id=requested_nm_ids[1], promo_count_by_price=0.0, promo_entry_price_best=450.0, promo_participation=0.0),
        PromoLiveSourceItem(snapshot_date=AS_OF_DATE, nm_id=requested_nm_ids[2], promo_count_by_price=0.0, promo_entry_price_best=0.0, promo_participation=0.0),
    ]
    today_items = [
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[0], promo_count_by_price=2.0, promo_entry_price_best=600.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[1], promo_count_by_price=1.0, promo_entry_price_best=550.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[2], promo_count_by_price=0.0, promo_entry_price_best=0.0, promo_participation=0.0),
    ]
    with TemporaryDirectory(prefix="sheet-vitrina-promo-source-map-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        runtime.save_temporal_source_snapshot(
            source_key="promo_by_price",
            snapshot_date=AS_OF_DATE,
            captured_at="2026-04-19T18:00:00Z",
            payload=PromoLiveSourceSuccess(
                kind="success",
                snapshot_date=AS_OF_DATE,
                date_from=AS_OF_DATE,
                date_to=AS_OF_DATE,
                requested_count=len(requested_nm_ids),
                covered_count=len(requested_nm_ids),
                items=yesterday_items,
                detail="trace_run_dir=/tmp/promo-yesterday; current_promos=2; current_promos_downloaded=2",
                trace_run_dir="/tmp/promo-yesterday",
                current_promos=2,
                current_promos_downloaded=2,
                current_promos_blocked=0,
                future_promos=1,
                skipped_past_promos=4,
                ambiguous_promos=0,
                current_download_export_kinds=["eligible_items_report"],
            ),
        )

        entrypoint = _build_entrypoint(
            runtime=runtime,
            promo_source_block=_SyntheticPromoSourceBlock(
                mode="success",
                snapshot_items=today_items,
                snapshot_items_by_date={
                    AS_OF_DATE: yesterday_items,
                    CURRENT_DATE: today_items,
                },
            ),
            now_factory=_MutableNowFactory("2026-04-20T08:00:00+00:00"),
        )
        refresh_payload = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if refresh_payload["status"] != "success":
            raise AssertionError(f"refresh must succeed, got {refresh_payload}")

        plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        status_rows = _status_rows(plan)
        yesterday_status = status_rows["promo_by_price[yesterday_closed]"]
        today_status = status_rows["promo_by_price[today_current]"]
        if yesterday_status[1] != "success":
            raise AssertionError(f"promo yesterday_closed must materialize from corrective replay, got {yesterday_status}")
        if "accepted_closed_from_interval_replay" not in str(yesterday_status[10]):
            raise AssertionError(f"promo yesterday_closed note must expose interval replay semantics, got {yesterday_status}")
        if today_status[1] != "success":
            raise AssertionError(f"promo today_current must be success, got {today_status}")
        if "trace_run_dir=/tmp/promo-today" not in str(today_status[10]):
            raise AssertionError(f"promo STATUS note must expose collector trace run dir, got {today_status}")
        if "current_promos_downloaded=2" not in str(today_status[10]):
            raise AssertionError(f"promo STATUS note must expose current download counts, got {today_status}")

        rows = _data_rows(plan)
        if _today_value(rows[f"SKU:{requested_nm_ids[0]}|promo_count_by_price"]) != 2.0:
            raise AssertionError("promo_count_by_price today_current must materialize from promo source")
        if _today_value(rows[f"SKU:{requested_nm_ids[1]}|promo_participation"]) != 1.0:
            raise AssertionError("promo_participation today_current must materialize from promo source")
        if _yesterday_value(rows[f"SKU:{requested_nm_ids[1]}|promo_entry_price_best"]) != 450.0:
            raise AssertionError("promo_entry_price_best yesterday_closed must materialize from runtime cache")


def _assert_accepted_current_preserved_after_invalid_attempt(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
) -> str:
    valid_items = [
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[0], promo_count_by_price=2.0, promo_entry_price_best=600.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[1], promo_count_by_price=1.0, promo_entry_price_best=550.0, promo_participation=1.0),
        PromoLiveSourceItem(snapshot_date=CURRENT_DATE, nm_id=requested_nm_ids[2], promo_count_by_price=0.0, promo_entry_price_best=0.0, promo_participation=0.0),
    ]
    with TemporaryDirectory(prefix="sheet-vitrina-promo-source-preserve-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp) / "runtime")
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")

        promo_source = _SyntheticPromoSourceBlock(mode="success", snapshot_items=valid_items)
        now_factory = _MutableNowFactory("2026-04-20T08:00:00+00:00")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            promo_source_block=promo_source,
            now_factory=now_factory,
        )
        first = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if first["status"] != "success":
            raise AssertionError("first promo refresh must succeed")
        morning_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        morning_rows = _data_rows(morning_plan)
        baseline = _today_value(morning_rows[f"SKU:{requested_nm_ids[0]}|promo_count_by_price"])
        if baseline != 2.0:
            raise AssertionError(f"expected baseline promo_count_by_price=2.0, got {baseline}")

        promo_source.mode = "incomplete"
        promo_source.detail = "trace_run_dir=/tmp/promo-invalid; current_promos=2; current_promos_downloaded=1; current_promos_blocked=1"
        now_factory.value = "2026-04-20T14:00:00+00:00"
        second = entrypoint._run_sheet_refresh(
            as_of_date=AS_OF_DATE,
            log=None,
            execution_mode=EXECUTION_MODE_AUTO_DAILY,
        )
        if second["status"] != "success":
            raise AssertionError("later invalid promo refresh must still persist a ready snapshot")
        evening_plan = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=AS_OF_DATE)
        evening_status = _status_rows(evening_plan)["promo_by_price[today_current]"]
        if evening_status[1] != "success":
            raise AssertionError(f"invalid later promo attempt must preserve accepted current snapshot, got {evening_status}")
        note = str(evening_status[10])
        if "accepted_current_preserved_after_invalid_attempt" not in note:
            raise AssertionError(f"preserved promo current snapshot must explain accepted preservation, got {evening_status}")
        promo_slot_diagnostics = _promo_source_slot_diagnostics(evening_plan, "promo_by_price", "today_current")
        fallback = promo_slot_diagnostics.get("fallback") or {}
        if fallback.get("candidate_rejected") is not True:
            raise AssertionError(f"promo fallback diagnostics must mark candidate_rejected, got {fallback}")
        if fallback.get("fallback_reason") != "accepted_current_preserved_after_invalid_attempt":
            raise AssertionError(f"promo fallback diagnostics reason mismatch, got {fallback}")
        if (promo_slot_diagnostics.get("dry_run_skip") or {}).get("would_skip_if_fingerprint_unchanged") is not False:
            raise AssertionError(f"promo dry-run skip marker must stay observation-only false, got {promo_slot_diagnostics}")
        evening_rows = _data_rows(evening_plan)
        if _today_value(evening_rows[f"SKU:{requested_nm_ids[0]}|promo_count_by_price"]) != baseline:
            raise AssertionError("invalid later promo attempt must not overwrite accepted current values")
        closure_state = runtime.load_temporal_source_closure_state(
            source_key="promo_by_price",
            target_date=CURRENT_DATE,
            slot_kind=TEMPORAL_SLOT_TODAY_CURRENT,
        )
        if closure_state is None or closure_state.state != "success":
            raise AssertionError(f"accepted promo current snapshot must keep success closure state, got {closure_state}")
        return note


def _assert_historical_interval_replay_cache_fill(requested_nm_ids: list[int]) -> str:
    with TemporaryDirectory(prefix="sheet-vitrina-promo-archive-replay-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        promo_run_dir = runtime_dir / "promo_xlsx_collector_runs" / "2026-04-20__fixture" / "promos" / "2287__2236__fixture-promo"
        promo_run_dir.mkdir(parents=True, exist_ok=True)
        workbook_path = promo_run_dir / "workbook.xlsx"
        _write_interval_fixture_workbook(workbook_path, requested_nm_ids)
        _seed_daily_price_truth(
            runtime=runtime,
            snapshot_date=INTERVAL_REPLAY_DATE,
            price_by_nm_id={
                requested_nm_ids[0]: 900.0,
                requested_nm_ids[1]: 1000.0,
                requested_nm_ids[2]: 1700.0,
            },
        )
        metadata = PromoMetadata(
            collected_at="2026-04-20T08:00:00+05:00",
            trace_run_dir=str(runtime_dir / "promo_xlsx_collector_runs" / "2026-04-20__fixture"),
            source_tab="Доступные",
            source_filter_code="AVAILABLE",
            calendar_url="https://seller.wildberries.ru/dp-promo-calendar?action=2287",
            promo_id=2287,
            period_id=2236,
            promo_title="Fixture promo",
            promo_period_text="16 апреля 02:00 → 25 апреля 01:59",
            promo_start_at="2026-04-16T02:00",
            promo_end_at="2026-04-25T01:59",
            period_parse_confidence="high",
            temporal_classification="current",
            promo_status="Акция идёт",
            promo_status_text="Автоакция: участие подтверждено",
            eligible_count=3,
            participating_count=2,
            excluded_count=1,
            export_kind="eligible_items_report",
            original_suggested_filename="fixture.xlsx",
            saved_filename="workbook.xlsx",
            saved_path=str(workbook_path),
            workbook_sheet_names=["Promo"],
            workbook_row_count=4,
            workbook_col_count=2,
            workbook_header_summary=["Артикул WB", "Плановая цена для акции"],
            workbook_has_date_fields=False,
            workbook_item_status_distinct_values=[],
        )
        (promo_run_dir / "metadata.json").write_text(
            json.dumps(metadata.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        runtime.save_temporal_source_slot_snapshot(
            source_key="promo_by_price",
            snapshot_date=INTERVAL_REPLAY_DATE,
            snapshot_role="accepted_closed_day_snapshot",
            captured_at="2026-04-16T18:00:00Z",
            payload=PromoLiveSourceSuccess(
                kind="success",
                snapshot_date=INTERVAL_REPLAY_DATE,
                date_from=INTERVAL_REPLAY_DATE,
                date_to=INTERVAL_REPLAY_DATE,
                requested_count=len(requested_nm_ids),
                covered_count=len(requested_nm_ids),
                items=[
                    PromoLiveSourceItem(
                        snapshot_date=INTERVAL_REPLAY_DATE,
                        nm_id=nm_id,
                        promo_count_by_price=9.0,
                        promo_entry_price_best=9999.0,
                        promo_participation=1.0,
                    )
                    for nm_id in requested_nm_ids
                ],
                detail="stale accepted closed promo snapshot",
                trace_run_dir="/tmp/stale-promo-accepted",
                current_promos=0,
                current_promos_downloaded=0,
                current_promos_blocked=0,
                future_promos=0,
                skipped_past_promos=0,
                ambiguous_promos=0,
                current_download_export_kinds=[],
            ),
        )

        plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=_MutableNowFactory("2026-04-20T08:00:00+00:00"),
            promo_live_source_block=PromoLiveSourceBlock(
                runtime_dir=runtime_dir,
                now_factory=_MutableNowFactory("2026-04-20T08:00:00+00:00"),
            ),
            current_web_source_sync=_NoopCurrentWebSourceSync(),
            seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot"),
            web_source_block=_SyntheticSuccessBlock("web_source_snapshot"),
            sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
            prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
            sf_period_block=_SyntheticSuccessBlock("sf_period"),
            spp_block=_SyntheticSuccessBlock("spp"),
            ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
            stocks_block=_SyntheticSuccessBlock("stocks"),
            ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
            fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
        )

        status, payload = plan_block._capture_promo_closed_day_from_cache(
            source_key="promo_by_price",
            temporal_slot="yesterday_closed",
            temporal_policy="dual_day_capable",
            column_date=INTERVAL_REPLAY_DATE,
            requested_nm_ids=requested_nm_ids,
        )
        if status.kind != "success":
            raise AssertionError(f"interval replay must materialize historical promo success, got {status}")
        if "accepted_closed_from_interval_replay" not in str(status.note):
            raise AssertionError(f"historical replay note must expose interval replay acceptance, got {status}")
        if payload is None:
            raise AssertionError("interval replay must persist payload")
        payload_items = {item.nm_id: item for item in payload.items}
        if payload_items[requested_nm_ids[0]].promo_participation != 1.0:
            raise AssertionError("covered nm_id with current < planned must participate via interval replay")
        if payload_items[requested_nm_ids[1]].promo_participation != 0.0:
            raise AssertionError("covered nm_id with current >= planned must stay non-participating")
        exact_payload, _ = runtime.load_temporal_source_snapshot(
            source_key="promo_by_price",
            snapshot_date=INTERVAL_REPLAY_DATE,
        )
        if exact_payload is None:
            raise AssertionError("interval replay must fill exact-date runtime seam")
        accepted_payload, _ = runtime.load_temporal_source_slot_snapshot(
            source_key="promo_by_price",
            snapshot_date=INTERVAL_REPLAY_DATE,
            snapshot_role="accepted_closed_day_snapshot",
        )
        if accepted_payload is None:
            raise AssertionError("interval replay must accept closed-day slot snapshot")
        accepted_items = {item.nm_id: item for item in accepted_payload.items}
        if accepted_items[requested_nm_ids[0]].promo_entry_price_best != 1000.0:
            raise AssertionError("interval replay must overwrite stale accepted closed snapshot")
        probe = payload_items[requested_nm_ids[0]]
        return (
            f"date={INTERVAL_REPLAY_DATE} "
            f"SKU={requested_nm_ids[0]} "
            f"price_seller_discounted=900.0 "
            f"eligible_campaigns=['2287:2236'] "
            f"eligible_plan_prices=[1000.0] "
            f"participation={probe.promo_participation} "
            f"count_by_plan_price={probe.promo_count_by_price} "
            f"beneficial_entry_price={probe.promo_entry_price_best}"
        )


def _assert_candidate_entry_price_metric_cases() -> str:
    ineligible = _evaluate_metric_truth_case(
        nm_id=210183919,
        plan_prices=[483.0, 488.0, 498.0, 493.0],
        price_seller_discounted=508.0,
    )
    if (
        ineligible.promo_participation,
        ineligible.promo_count_by_price,
        ineligible.promo_entry_price_best,
    ) != (0.0, 0.0, 498.0):
        raise AssertionError(f"ineligible candidate entry-price case mismatch, got {ineligible}")

    multi_eligible = _evaluate_metric_truth_case(
        nm_id=391659990,
        plan_prices=[566.0, 571.0, 498.0, 493.0],
        price_seller_discounted=508.0,
    )
    if (
        multi_eligible.promo_participation,
        multi_eligible.promo_count_by_price,
        multi_eligible.promo_entry_price_best,
    ) != (1.0, 2.0, 571.0):
        raise AssertionError(f"multi-eligible candidate entry-price case mismatch, got {multi_eligible}")

    no_candidate = _evaluate_metric_truth_case(
        nm_id=210000000,
        plan_prices=[],
        price_seller_discounted=508.0,
    )
    if (
        no_candidate.promo_participation,
        no_candidate.promo_count_by_price,
        no_candidate.promo_entry_price_best,
    ) != (0.0, 0.0, 0.0):
        raise AssertionError(f"no-candidate case mismatch, got {no_candidate}")

    missing_price = _evaluate_metric_truth_case(
        nm_id=210183919,
        plan_prices=[483.0, 488.0, 498.0, 493.0],
        price_seller_discounted=None,
    )
    if (
        missing_price.promo_participation,
        missing_price.promo_count_by_price,
        missing_price.promo_entry_price_best,
    ) != (0.0, 0.0, 498.0):
        raise AssertionError(f"missing seller price candidate entry-price case mismatch, got {missing_price}")

    return (
        "ineligible_candidate_entry=498.0; "
        "multi_eligible_count=2_entry=571.0; "
        "no_candidate_entry=0.0; "
        "missing_price_entry=498.0"
    )


def _evaluate_metric_truth_case(
    *,
    nm_id: int,
    plan_prices: list[float],
    price_seller_discounted: float | None,
):
    return evaluate_candidate_rows(
        candidate_rows=[
            PromoCandidateRow(
                nm_id=nm_id,
                campaign_identity=f"fixture:{index}",
                plan_price=plan_price,
            )
            for index, plan_price in enumerate(plan_prices, start=1)
        ],
        price_seller_discounted=price_seller_discounted,
    )


def _assert_promo_internal_diagnostics(result: object, *, expected_snapshot_date: str) -> None:
    diagnostics = getattr(result, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        raise AssertionError(f"promo result diagnostics must be a dict, got {diagnostics}")
    if diagnostics.get("schema_version") != "promo_by_price_diagnostics_v1":
        raise AssertionError(f"promo diagnostics schema mismatch, got {diagnostics}")
    if diagnostics.get("snapshot_date") != expected_snapshot_date:
        raise AssertionError(f"promo diagnostics snapshot date mismatch, got {diagnostics}")
    phase_keys = {
        str(item.get("phase_key") or "")
        for item in diagnostics.get("phase_summary", [])
        if isinstance(item, dict)
    }
    required = {
        "promo_total",
        "collector_total",
        "archive_lookup",
        "archive_sync",
        "workbook_inspection",
        "metadata_validation",
        "price_truth_lookup",
        "price_truth_join",
        "source_payload_build",
        "acceptance_decision",
        "fallback_preserve",
    }
    missing = sorted(required - phase_keys)
    if missing:
        raise AssertionError(f"promo diagnostics missing phases {missing}: {diagnostics}")
    counters = diagnostics.get("counters") or {}
    if counters.get("candidate_row_count") is None:
        raise AssertionError(f"promo diagnostics must expose candidate_row_count, got {counters}")
    if counters.get("price_truth_available_count") is None:
        raise AssertionError(f"promo diagnostics must expose price truth count, got {counters}")
    if "collector_reuse_count" not in counters:
        raise AssertionError(f"promo diagnostics must distinguish collector reuse, got {counters}")
    if counters.get("validated_workbook_usable_count") is None:
        raise AssertionError(f"promo diagnostics must expose validated workbook count, got {counters}")
    for key in (
        "manifest_campaign_seen_count",
        "manifest_timeline_match_count",
        "manifest_drawer_avoid_count",
        "manifest_match_duration_ms",
    ):
        if key not in counters:
            raise AssertionError(f"promo diagnostics must expose campaign manifest counter {key}, got {counters}")
    artifact_summary = diagnostics.get("artifact_validation_summary") or {}
    if artifact_summary.get("schema_version") != "promo_artifact_validation_v1":
        raise AssertionError(f"promo artifact validation summary missing, got {diagnostics}")
    artifact_state_counts = diagnostics.get("artifact_state_counts") or {}
    if "complete" not in artifact_state_counts:
        raise AssertionError(f"promo artifact state counts missing, got {diagnostics}")
    dry_run = diagnostics.get("dry_run_skip") or {}
    if dry_run.get("would_skip_if_fingerprint_unchanged") is not False:
        raise AssertionError(f"promo dry-run skip marker must not change behavior, got {dry_run}")


def _assert_canonical_eligible_set_cases(requested_nm_ids: list[int]) -> str:
    with TemporaryDirectory(prefix="sheet-vitrina-promo-canonical-cases-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        _seed_neighbor_date_archive(runtime_dir, requested_nm_ids)
        diagnostics_by_date = _seed_neighbor_date_price_truth(runtime, requested_nm_ids)
        block = PromoLiveSourceBlock(
            runtime_dir=runtime_dir,
            now_factory=_MutableNowFactory("2026-04-25T08:00:00+00:00"),
        )

        single_result = block.execute(
            PromoLiveSourceRequest(snapshot_date="2026-04-20", nm_ids=requested_nm_ids)
        ).result
        multiple_result = block.execute(
            PromoLiveSourceRequest(snapshot_date="2026-04-21", nm_ids=requested_nm_ids)
        ).result
        next_day_result = block.execute(
            PromoLiveSourceRequest(snapshot_date="2026-04-22", nm_ids=requested_nm_ids)
        ).result
        outside_result = block.execute(
            PromoLiveSourceRequest(snapshot_date="2026-04-23", nm_ids=requested_nm_ids)
        ).result
        missing_price_result = block.execute(
            PromoLiveSourceRequest(snapshot_date="2026-04-24", nm_ids=requested_nm_ids)
        ).result

        if single_result.kind != "success":
            raise AssertionError(f"single eligible case must be success, got {single_result}")
        if multiple_result.kind != "success":
            raise AssertionError(f"multiple eligible case must be success, got {multiple_result}")
        if next_day_result.kind != "success":
            raise AssertionError(f"neighbor-day replay must be success, got {next_day_result}")
        if outside_result.kind != "success":
            raise AssertionError(f"outside-interval case must stay truthful empty success, got {outside_result}")
        if missing_price_result.kind != "incomplete":
            raise AssertionError(f"missing price truth case must be incomplete, got {missing_price_result}")
        _assert_promo_internal_diagnostics(single_result, expected_snapshot_date="2026-04-20")
        _assert_promo_internal_diagnostics(missing_price_result, expected_snapshot_date="2026-04-24")

        single_items = {item.nm_id: item for item in single_result.items}
        multiple_items = {item.nm_id: item for item in multiple_result.items}
        next_day_items = {item.nm_id: item for item in next_day_result.items}
        missing_price_items = {item.nm_id: item for item in missing_price_result.items}

        single_probe = single_items[requested_nm_ids[0]]
        if (single_probe.promo_participation, single_probe.promo_count_by_price, single_probe.promo_entry_price_best) != (
            1.0,
            1.0,
            1000.0,
        ):
            raise AssertionError(f"single eligible case mismatch, got {single_probe}")

        multiple_probe = multiple_items[requested_nm_ids[0]]
        if (multiple_probe.promo_participation, multiple_probe.promo_count_by_price, multiple_probe.promo_entry_price_best) != (
            1.0,
            2.0,
            1200.0,
        ):
            raise AssertionError(f"multiple eligible case mismatch, got {multiple_probe}")

        ineligible_probe = multiple_items[requested_nm_ids[1]]
        if (ineligible_probe.promo_participation, ineligible_probe.promo_count_by_price, ineligible_probe.promo_entry_price_best) != (
            0.0,
            0.0,
            1100.0,
        ):
            raise AssertionError(f"in-interval non-eligible case must preserve candidate entry price, got {ineligible_probe}")

        next_day_probe = next_day_items[requested_nm_ids[0]]
        if (next_day_probe.promo_participation, next_day_probe.promo_count_by_price, next_day_probe.promo_entry_price_best) != (
            1.0,
            2.0,
            1200.0,
        ):
            raise AssertionError(f"neighbor date replay mismatch, got {next_day_probe}")

        outside_items = {item.nm_id: item for item in outside_result.items}
        outside_probe = outside_items[requested_nm_ids[0]]
        if (
            outside_probe.promo_participation,
            outside_probe.promo_count_by_price,
            outside_probe.promo_entry_price_best,
        ) != (0.0, 0.0, 0.0):
            raise AssertionError(f"outside-interval case must be truthful empty, got {outside_probe}")
        if "no_covering_campaign_rows_for_requested_nm_ids=true" not in outside_result.detail:
            raise AssertionError(f"outside-interval detail mismatch, got {outside_result.detail}")

        missing_price_probe = missing_price_items[requested_nm_ids[0]]
        if (
            missing_price_probe.promo_participation,
            missing_price_probe.promo_count_by_price,
            missing_price_probe.promo_entry_price_best,
        ) != (1.0, 1.0, 1000.0):
            raise AssertionError(f"covered price truth case mismatch, got {missing_price_probe}")
        missing_price_candidate_probe = missing_price_items[requested_nm_ids[2]]
        if (
            missing_price_candidate_probe.promo_participation,
            missing_price_candidate_probe.promo_count_by_price,
            missing_price_candidate_probe.promo_entry_price_best,
        ) != (0.0, 0.0, 1300.0):
            raise AssertionError(
                "SKU with campaign rows but missing price truth must preserve candidate entry price, "
                f"got {missing_price_candidate_probe}"
            )
        if missing_price_result.missing_nm_ids != [requested_nm_ids[2]]:
            raise AssertionError(f"missing price truth ids mismatch, got {missing_price_result.missing_nm_ids}")
        if "promo_metric_missing_price_truth_nm_ids" not in missing_price_result.detail:
            raise AssertionError(f"missing price truth detail must be surfaced, got {missing_price_result.detail}")

        return "; ".join(
            [
                (
                    f"date={date} "
                    f"SKU={requested_nm_ids[0]} "
                    f"price_seller_discounted={diagnostics_by_date[date]['price_seller_discounted']} "
                    f"eligible_campaigns={diagnostics_by_date[date]['eligible_campaigns']} "
                    f"eligible_plan_prices={diagnostics_by_date[date]['eligible_plan_prices']} "
                    f"participation={diagnostics_by_date[date]['participation']} "
                    f"count_by_plan_price={diagnostics_by_date[date]['count_by_plan_price']} "
                    f"beneficial_entry_price={diagnostics_by_date[date]['beneficial_entry_price']}"
                )
                for date in ("2026-04-20", "2026-04-21", "2026-04-22")
            ]
            + [
                (
                    f"date=2026-04-23 "
                    f"SKU={requested_nm_ids[0]} "
                    f"price_seller_discounted=not_needed "
                    f"eligible_campaigns=[] "
                    f"eligible_plan_prices=[] "
                    f"participation={outside_probe.promo_participation} "
                    f"count_by_plan_price={outside_probe.promo_count_by_price} "
                    f"beneficial_entry_price={outside_probe.promo_entry_price_best}"
                )
            ]
            + [
                (
                    f"date=2026-04-24 "
                    f"SKU={requested_nm_ids[2]} "
                    f"price_seller_discounted=missing "
                    f"candidate_campaigns=['2290:2239'] "
                    f"candidate_plan_prices=[1300.0] "
                    f"eligible_campaigns=[] "
                    f"eligible_plan_prices=[] "
                    f"participation={missing_price_candidate_probe.promo_participation} "
                    f"count_by_plan_price={missing_price_candidate_probe.promo_count_by_price} "
                    f"beneficial_entry_price={missing_price_candidate_probe.promo_entry_price_best}"
                )
            ]
        )


def _assert_web_vitrina_period_payload_for_promo_dates(
    bundle: dict[str, Any],
    requested_nm_ids: list[int],
) -> str:
    with TemporaryDirectory(prefix="sheet-vitrina-promo-web-vitrina-period-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")
        _seed_neighbor_date_archive(runtime_dir, requested_nm_ids)
        _seed_neighbor_date_price_truth(runtime, requested_nm_ids)
        now_factory = _MutableNowFactory("2026-04-25T08:00:00+00:00")
        entrypoint = _build_entrypoint(
            runtime=runtime,
            promo_source_block=PromoLiveSourceBlock(
                runtime_dir=runtime_dir,
                now_factory=_MutableNowFactory("2099-01-01T08:00:00+00:00"),
            ),
            now_factory=now_factory,
        )

        for snapshot_date in ("2026-04-20", "2026-04-21", "2026-04-22", "2026-04-23", "2026-04-24"):
            refresh_payload = entrypoint._run_sheet_refresh(
                as_of_date=snapshot_date,
                log=None,
                execution_mode=EXECUTION_MODE_AUTO_DAILY,
            )
            if refresh_payload["status"] not in {"success", "warning"}:
                raise AssertionError(f"refresh for {snapshot_date} must materialize, got {refresh_payload}")

        contract_payload = entrypoint.handle_sheet_web_vitrina_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            date_from="2026-04-20",
            date_to="2026-04-24",
        )
        if contract_payload["meta"]["date_columns"] != [
            "2026-04-20",
            "2026-04-21",
            "2026-04-22",
            "2026-04-23",
            "2026-04-24",
        ]:
            raise AssertionError(f"web-vitrina period dates mismatch, got {contract_payload['meta']}")
        rows = {row["row_id"]: row for row in contract_payload["rows"]}
        positive_row = rows[f"SKU:{requested_nm_ids[0]}|promo_participation"]
        truthful_zero_row = rows[f"SKU:{requested_nm_ids[1]}|promo_participation"]
        missing_price_row = rows[f"SKU:{requested_nm_ids[2]}|promo_participation"]
        truthful_zero_entry_row = rows[f"SKU:{requested_nm_ids[1]}|promo_entry_price_best"]
        missing_price_entry_row = rows[f"SKU:{requested_nm_ids[2]}|promo_entry_price_best"]
        positive_values = positive_row["values_by_date"]
        if positive_values != {
            "2026-04-20": 1.0,
            "2026-04-21": 1.0,
            "2026-04-22": 1.0,
            "2026-04-23": 0.0,
            "2026-04-24": 1.0,
        }:
            raise AssertionError(f"positive promo row mismatch, got {positive_values}")
        truthful_zero_values = truthful_zero_row["values_by_date"]
        if any(value != 0.0 for value in truthful_zero_values.values()):
            raise AssertionError(f"truthful zero promo row mismatch, got {truthful_zero_values}")
        truthful_zero_entry_values = truthful_zero_entry_row["values_by_date"]
        if truthful_zero_entry_values["2026-04-21"] != 1100.0:
            raise AssertionError(
                "ineligible SKU must keep candidate entry price in web-vitrina payload, "
                f"got {truthful_zero_entry_values}"
            )
        missing_price_values = missing_price_row["values_by_date"]
        if missing_price_values["2026-04-24"] != 0.0:
            raise AssertionError(
                f"missing price truth must not fake-positive participation, got {missing_price_values}"
            )
        missing_price_entry_values = missing_price_entry_row["values_by_date"]
        if missing_price_entry_values["2026-04-24"] != 1300.0:
            raise AssertionError(
                "missing price truth must preserve candidate entry price in web-vitrina payload, "
                f"got {missing_price_entry_values}"
            )

        composition_payload = entrypoint.handle_sheet_web_vitrina_page_composition_request(
            page_route="/sheet-vitrina-v1/vitrina",
            read_route="/v1/sheet-vitrina-v1/web-vitrina",
            operator_route="/sheet-vitrina-v1/operator",
            date_from="2026-04-20",
            date_to="2026-04-24",
        )
        if composition_payload.get("composition_name") != "web_vitrina_page_composition":
            raise AssertionError(f"page composition identity mismatch, got {composition_payload}")
        if composition_payload.get("table_surface", {}).get("total_row_count") != contract_payload["meta"]["row_count"]:
            raise AssertionError(f"page composition row count mismatch, got {composition_payload['table_surface']}")
        status_rows = _status_rows(runtime.load_sheet_vitrina_ready_snapshot(as_of_date="2026-04-24"))
        promo_status = status_rows["promo_by_price[yesterday_closed]"]
        if promo_status[1] != "incomplete" or str(requested_nm_ids[2]) not in str(promo_status[9]):
            raise AssertionError(f"missing price truth must surface in STATUS, got {promo_status}")
        if "promo_metric_missing_price_truth_nm_ids" not in str(promo_status[10]):
            raise AssertionError(f"missing price detail must surface in STATUS note, got {promo_status}")

        return (
            "dates=2026-04-20..2026-04-24 "
            f"nonzero_SKU={requested_nm_ids[0]} "
            f"truthful_zero_SKU={requested_nm_ids[1]} "
            f"missing_price_candidate_entry_SKU={requested_nm_ids[2]}"
        )


def _seed_neighbor_date_archive(runtime_dir: Path, requested_nm_ids: list[int]) -> None:
    _write_promo_run_fixture(
        runtime_dir=runtime_dir,
        run_name="2026-04-20__fixture",
        promo_folder="2287__2236__single-eligible",
        promo_id=2287,
        period_id=2236,
        promo_title="Single eligible promo",
        promo_period_text="20 апреля 02:00 → 20 апреля 23:59",
        promo_start_at="2026-04-20T02:00",
        promo_end_at="2026-04-20T23:59",
        workbook_rows=[
            {
                "nm_id": requested_nm_ids[0],
                "plan_price": 1000.0,
            },
            {
                "nm_id": requested_nm_ids[1],
                "plan_price": 1000.0,
            },
        ],
    )
    _write_promo_run_fixture(
        runtime_dir=runtime_dir,
        run_name="2026-04-21__fixture",
        promo_folder="2288__2237__multi-eligible-a",
        promo_id=2288,
        period_id=2237,
        promo_title="Multi eligible promo A",
        promo_period_text="21 апреля 02:00 → 22 апреля 23:59",
        promo_start_at="2026-04-21T02:00",
        promo_end_at="2026-04-22T23:59",
        workbook_rows=[
            {
                "nm_id": requested_nm_ids[0],
                "plan_price": 1000.0,
            },
            {
                "nm_id": requested_nm_ids[1],
                "plan_price": 1100.0,
            },
        ],
    )
    _write_promo_run_fixture(
        runtime_dir=runtime_dir,
        run_name="2026-04-24__fixture",
        promo_folder="2290__2239__missing-price-truth",
        promo_id=2290,
        period_id=2239,
        promo_title="Missing price truth promo",
        promo_period_text="24 апреля 02:00 → 24 апреля 23:59",
        promo_start_at="2026-04-24T02:00",
        promo_end_at="2026-04-24T23:59",
        workbook_rows=[
            {
                "nm_id": requested_nm_ids[0],
                "plan_price": 1000.0,
            },
            {
                "nm_id": requested_nm_ids[2],
                "plan_price": 1300.0,
            },
        ],
    )
    _write_promo_run_fixture(
        runtime_dir=runtime_dir,
        run_name="2026-04-21__fixture",
        promo_folder="2289__2238__multi-eligible-b",
        promo_id=2289,
        period_id=2238,
        promo_title="Multi eligible promo B",
        promo_period_text="21 апреля 02:00 → 22 апреля 23:59",
        promo_start_at="2026-04-21T02:00",
        promo_end_at="2026-04-22T23:59",
        workbook_rows=[
            {
                "nm_id": requested_nm_ids[0],
                "plan_price": 1200.0,
            },
            {
                "nm_id": requested_nm_ids[2],
                "plan_price": 1500.0,
            },
        ],
    )


def _seed_neighbor_date_price_truth(
    runtime: RegistryUploadDbBackedRuntime,
    requested_nm_ids: list[int],
) -> dict[str, dict[str, object]]:
    price_truth_by_date = {
        "2026-04-20": {
            requested_nm_ids[0]: 900.0,
            requested_nm_ids[1]: 1000.0,
            requested_nm_ids[2]: 1700.0,
        },
        "2026-04-21": {
            requested_nm_ids[0]: 950.0,
            requested_nm_ids[1]: 1110.0,
            requested_nm_ids[2]: 1700.0,
        },
        "2026-04-22": {
            requested_nm_ids[0]: 950.0,
            requested_nm_ids[1]: 1110.0,
            requested_nm_ids[2]: 1700.0,
        },
        "2026-04-24": {
            requested_nm_ids[0]: 900.0,
        },
    }
    for snapshot_date, price_by_nm_id in price_truth_by_date.items():
        _seed_daily_price_truth(
            runtime=runtime,
            snapshot_date=snapshot_date,
            price_by_nm_id=price_by_nm_id,
        )
    return {
        "2026-04-20": {
            "price_seller_discounted": 900.0,
            "eligible_campaigns": ["2287:2236"],
            "eligible_plan_prices": [1000.0],
            "participation": 1.0,
            "count_by_plan_price": 1.0,
            "beneficial_entry_price": 1000.0,
        },
        "2026-04-21": {
            "price_seller_discounted": 950.0,
            "eligible_campaigns": ["2288:2237", "2289:2238"],
            "eligible_plan_prices": [1000.0, 1200.0],
            "participation": 1.0,
            "count_by_plan_price": 2.0,
            "beneficial_entry_price": 1200.0,
        },
        "2026-04-22": {
            "price_seller_discounted": 950.0,
            "eligible_campaigns": ["2288:2237", "2289:2238"],
            "eligible_plan_prices": [1000.0, 1200.0],
            "participation": 1.0,
            "count_by_plan_price": 2.0,
            "beneficial_entry_price": 1200.0,
        },
    }


def _seed_daily_price_truth(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    snapshot_date: str,
    price_by_nm_id: dict[int, float],
) -> None:
    runtime.save_temporal_source_slot_snapshot(
        source_key="prices_snapshot",
        snapshot_date=snapshot_date,
        snapshot_role=PRICES_ACCEPTED_CURRENT_ROLE,
        captured_at=f"{snapshot_date}T18:00:00Z",
        payload=SimpleNamespace(
            kind="success",
            snapshot_date=snapshot_date,
            items=[
                SimpleNamespace(
                    nm_id=nm_id,
                    price_seller=price_seller_discounted,
                    price_seller_discounted=price_seller_discounted,
                )
                for nm_id, price_seller_discounted in sorted(price_by_nm_id.items())
            ],
        ),
    )


def _write_promo_run_fixture(
    *,
    runtime_dir: Path,
    run_name: str,
    promo_folder: str,
    promo_id: int,
    period_id: int,
    promo_title: str,
    promo_period_text: str,
    promo_start_at: str,
    promo_end_at: str,
    workbook_rows: list[dict[str, float]],
) -> None:
    promo_run_dir = runtime_dir / "promo_xlsx_collector_runs" / run_name / "promos" / promo_folder
    promo_run_dir.mkdir(parents=True, exist_ok=True)
    workbook_path = promo_run_dir / "workbook.xlsx"
    _write_plan_price_fixture_workbook(workbook_path, workbook_rows)
    metadata = PromoMetadata(
        collected_at=f"{run_name[:10]}T08:00:00+05:00",
        trace_run_dir=str(runtime_dir / "promo_xlsx_collector_runs" / run_name),
        source_tab="Доступные",
        source_filter_code="AVAILABLE",
        calendar_url=f"https://seller.wildberries.ru/dp-promo-calendar?action={promo_id}",
        promo_id=promo_id,
        period_id=period_id,
        promo_title=promo_title,
        promo_period_text=promo_period_text,
        promo_start_at=promo_start_at,
        promo_end_at=promo_end_at,
        period_parse_confidence="high",
        temporal_classification="current",
        promo_status="Акция идёт",
        promo_status_text="Автоакция: участие подтверждено",
        eligible_count=len(workbook_rows),
        participating_count=len(workbook_rows),
        excluded_count=0,
        export_kind="eligible_items_report",
        original_suggested_filename=f"{promo_folder}.xlsx",
        saved_filename="workbook.xlsx",
        saved_path=str(workbook_path),
        workbook_sheet_names=["Promo"],
        workbook_row_count=len(workbook_rows) + 1,
        workbook_col_count=2,
        workbook_header_summary=[
            "Артикул WB",
            "Плановая цена для акции",
        ],
        workbook_has_date_fields=False,
        workbook_item_status_distinct_values=[],
    )
    (promo_run_dir / "metadata.json").write_text(
        json.dumps(metadata.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_plan_price_fixture_workbook(path: Path, rows: list[dict[str, float]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Promo"
    sheet.append(["Артикул WB", "Плановая цена для акции"])
    for row in rows:
        sheet.append([row["nm_id"], row["plan_price"]])
    workbook.save(path)


def _write_interval_fixture_workbook(path: Path, requested_nm_ids: list[int]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Promo"
    sheet.append(["Артикул WB", "Плановая цена для акции"])
    sheet.append([requested_nm_ids[0], 1000.0])
    sheet.append([requested_nm_ids[1], 1000.0])
    sheet.append([requested_nm_ids[2], 1500.0])
    workbook.save(path)


def _build_entrypoint(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    promo_source_block: object,
    now_factory: "_MutableNowFactory",
) -> RegistryUploadHttpEntrypoint:
    entrypoint = RegistryUploadHttpEntrypoint(
        runtime_dir=runtime.runtime_dir,
        runtime=runtime,
        activated_at_factory=lambda: ACTIVATED_AT,
        refreshed_at_factory=lambda: "2026-04-20T08:05:00Z",
        now_factory=now_factory,
    )
    entrypoint.sheet_plan_block = SheetVitrinaV1LivePlanBlock(
        runtime=runtime,
        now_factory=now_factory,
        promo_live_source_block=promo_source_block,
        current_web_source_sync=_NoopCurrentWebSourceSync(),
        seller_funnel_block=_SyntheticSuccessBlock("seller_funnel_snapshot"),
        web_source_block=_SyntheticSuccessBlock("web_source_snapshot"),
        sales_funnel_history_block=_SyntheticSuccessBlock("sales_funnel_history"),
        prices_snapshot_block=_SyntheticSuccessBlock("prices_snapshot"),
        sf_period_block=_SyntheticSuccessBlock("sf_period"),
        spp_block=_SyntheticSuccessBlock("spp"),
        ads_bids_block=_SyntheticSuccessBlock("ads_bids"),
        stocks_block=_SyntheticSuccessBlock("stocks"),
        ads_compact_block=_SyntheticSuccessBlock("ads_compact"),
        fin_report_daily_block=_SyntheticSuccessBlock("fin_report_daily"),
    )
    return entrypoint


class _NoopCurrentWebSourceSync:
    def ensure_snapshot(self, snapshot_date: str) -> None:
        return


class _SyntheticSuccessBlock:
    def __init__(self, source_key: str) -> None:
        self.source_key = source_key

    def execute(self, request: object) -> SimpleNamespace:
        request_date = _request_date(request)
        return SimpleNamespace(
            result=SimpleNamespace(
                kind="success",
                items=[],
                snapshot_date=request_date,
                date=request_date,
                date_from=request_date,
                date_to=request_date,
                detail=f"{self.source_key} synthetic success for {request_date}",
                storage_total=None,
            )
        )


class _SyntheticPromoSourceBlock:
    def __init__(
        self,
        *,
        mode: str,
        snapshot_items: list[PromoLiveSourceItem],
        snapshot_items_by_date: dict[str, list[PromoLiveSourceItem]] | None = None,
    ) -> None:
        self.mode = mode
        self.snapshot_items = snapshot_items
        self.snapshot_items_by_date = dict(snapshot_items_by_date or {})
        self.detail = "trace_run_dir=/tmp/promo-today; current_promos=2; current_promos_downloaded=2; current_promos_blocked=0"

    def execute(self, request: PromoLiveSourceRequest) -> PromoLiveSourceEnvelope:
        items = self.snapshot_items_by_date.get(request.snapshot_date, self.snapshot_items)
        if self.mode == "success":
            return PromoLiveSourceEnvelope(
                result=PromoLiveSourceSuccess(
                    kind="success",
                    snapshot_date=request.snapshot_date,
                    date_from=request.snapshot_date,
                    date_to=request.snapshot_date,
                    requested_count=len(request.nm_ids),
                    covered_count=len(request.nm_ids),
                    items=items,
                    detail=self.detail,
                    trace_run_dir="/tmp/promo-today",
                    current_promos=2,
                    current_promos_downloaded=2,
                    current_promos_blocked=0,
                    future_promos=1,
                    skipped_past_promos=4,
                    ambiguous_promos=0,
                    current_download_export_kinds=["eligible_items_report"],
                )
            )
        return PromoLiveSourceEnvelope(
            result=PromoLiveSourceIncomplete(
                kind="incomplete",
                snapshot_date=request.snapshot_date,
                date_from=request.snapshot_date,
                date_to=request.snapshot_date,
                requested_count=len(request.nm_ids),
                covered_count=0,
                items=[],
                detail=self.detail,
                trace_run_dir="/tmp/promo-invalid",
                current_promos=2,
                current_promos_downloaded=1,
                current_promos_blocked=1,
                future_promos=1,
                skipped_past_promos=4,
                ambiguous_promos=0,
                missing_nm_ids=list(request.nm_ids),
                current_download_export_kinds=["eligible_items_report"],
            )
        )


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic request must carry a date field")


def _status_rows(plan) -> dict[str, list[Any]]:
    status_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "STATUS")
    return {row[0]: row for row in status_sheet.rows}


def _data_rows(plan) -> dict[str, list[Any]]:
    data_sheet = next(sheet for sheet in plan.sheets if sheet.sheet_name == "DATA_VITRINA")
    return {row[1]: row for row in data_sheet.rows}


def _promo_source_slot_diagnostics(plan, source_key: str, slot_kind: str) -> dict[str, Any]:
    refresh_diagnostics = (plan.metadata or {}).get("refresh_diagnostics") or {}
    for item in refresh_diagnostics.get("source_slots", []):
        if not isinstance(item, dict):
            continue
        if item.get("source_key") == source_key and item.get("slot_kind") == slot_kind:
            diagnostics = item.get("promo_diagnostics")
            if isinstance(diagnostics, dict):
                return diagnostics
    raise AssertionError(f"promo diagnostics not found for {source_key}[{slot_kind}]")


def _yesterday_value(row: list[Any]) -> Any:
    return row[2]


def _today_value(row: list[Any]) -> Any:
    return row[3]


if __name__ == "__main__":
    main()
