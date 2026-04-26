"""Bounded integration smoke for archive-first promo runtime across neighbor dates."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.promo_live_source import PromoLiveSourceBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock
from packages.contracts.promo_live_source import PromoLiveSourceItem, PromoLiveSourceSuccess
from packages.contracts.promo_xlsx_collector_block import PromoMetadata


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-23T06:00:00Z"
CHECK_DATES = ("2026-04-20", "2026-04-21", "2026-04-22")
PRICES_ACCEPTED_CURRENT_ROLE = "accepted_current_snapshot"


def main() -> None:
    bundle = json.loads(INPUT_BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    requested_nm_ids = [int(item["nm_id"]) for item in bundle["config_v2"] if item["enabled"]][:3]
    if len(requested_nm_ids) < 3:
        raise AssertionError("fixture bundle must contain at least three enabled nm_ids")

    with TemporaryDirectory(prefix="sheet-vitrina-promo-live-source-integration-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        result = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if result.status != "accepted":
            raise AssertionError(f"fixture ingest must be accepted, got {result}")
        _seed_neighbor_date_archive(runtime_dir, requested_nm_ids)
        diagnostics_by_date = _seed_neighbor_date_price_truth(runtime, requested_nm_ids)

        plan_block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            now_factory=_MutableNowFactory("2026-04-23T08:00:00+00:00"),
            promo_live_source_block=PromoLiveSourceBlock(
                runtime_dir=runtime_dir,
                now_factory=_MutableNowFactory("2026-04-23T08:00:00+00:00"),
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

        for column_date in CHECK_DATES:
            runtime.save_temporal_source_slot_snapshot(
                source_key="promo_by_price",
                snapshot_date=column_date,
                snapshot_role="accepted_closed_day_snapshot",
                captured_at=f"{column_date}T07:00:00Z",
                payload=PromoLiveSourceSuccess(
                    kind="success",
                    snapshot_date=column_date,
                    date_from=column_date,
                    date_to=column_date,
                    requested_count=len(requested_nm_ids),
                    covered_count=len(requested_nm_ids),
                    items=[
                        PromoLiveSourceItem(
                            snapshot_date=column_date,
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
            status, payload = plan_block._capture_promo_closed_day_from_cache(
                source_key="promo_by_price",
                temporal_slot="yesterday_closed",
                temporal_policy="dual_day_capable",
                column_date=column_date,
                requested_nm_ids=requested_nm_ids,
            )
            if status.kind != "success":
                raise AssertionError(f"{column_date}: expected success status, got {status}")
            if payload is None:
                raise AssertionError(f"{column_date}: expected payload")
            _assert_payload_diagnostics(payload, column_date)
            item_index = {item.nm_id: item for item in payload.items}
            probe = item_index[requested_nm_ids[0]]
            expected = diagnostics_by_date[column_date]
            if probe.promo_participation != expected["participation"]:
                raise AssertionError(f"{column_date}: participation mismatch, got {probe}")
            if probe.promo_count_by_price != expected["count_by_plan_price"]:
                raise AssertionError(f"{column_date}: count mismatch, got {probe}")
            if probe.promo_entry_price_best != expected["beneficial_entry_price"]:
                raise AssertionError(f"{column_date}: probe mismatch, got {probe}")
            ineligible = item_index[requested_nm_ids[1]]
            expected_ineligible_entry = {
                "2026-04-20": 1000.0,
                "2026-04-21": 1100.0,
                "2026-04-22": 1100.0,
            }[column_date]
            if (
                ineligible.promo_participation,
                ineligible.promo_count_by_price,
                ineligible.promo_entry_price_best,
            ) != (0.0, 0.0, expected_ineligible_entry):
                raise AssertionError(
                    f"{column_date}: ineligible row must preserve candidate entry price, got {ineligible}"
                )

            exact_payload, _ = runtime.load_temporal_source_snapshot(
                source_key="promo_by_price",
                snapshot_date=column_date,
            )
            if exact_payload is None:
                raise AssertionError(f"{column_date}: exact-date runtime payload missing")
            accepted_payload, _ = runtime.load_temporal_source_slot_snapshot(
                source_key="promo_by_price",
                snapshot_date=column_date,
                snapshot_role="accepted_closed_day_snapshot",
            )
            if accepted_payload is None:
                raise AssertionError(f"{column_date}: accepted closed-day payload missing")
            accepted_probe = {item.nm_id: item for item in accepted_payload.items}[requested_nm_ids[0]]
            if accepted_probe.promo_entry_price_best != expected["beneficial_entry_price"]:
                raise AssertionError(
                    f"{column_date}: corrective replay must overwrite stale accepted snapshot, got {accepted_probe}"
                )
            print(
                f"{column_date}: "
                f"SKU={requested_nm_ids[0]} "
                f"price_seller_discounted={expected['price_seller_discounted']} "
                f"eligible_campaigns={expected['eligible_campaigns']} "
                f"eligible_plan_prices={expected['eligible_plan_prices']} "
                f"participation={probe.promo_participation} "
                f"count_by_plan_price={probe.promo_count_by_price} "
                f"beneficial_entry_price={probe.promo_entry_price_best}"
            )
        print("integration-smoke passed")


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


def _request_date(request: object) -> str:
    for field in ("snapshot_date", "date", "date_to"):
        value = getattr(request, field, None)
        if isinstance(value, str) and value:
            return value
    raise AssertionError("synthetic request must carry a date field")


def _assert_payload_diagnostics(payload: object, snapshot_date: str) -> None:
    diagnostics = getattr(payload, "diagnostics", None)
    if not isinstance(diagnostics, dict):
        raise AssertionError(f"{snapshot_date}: promo diagnostics must be a dict, got {diagnostics}")
    phase_keys = {
        str(item.get("phase_key") or "")
        for item in diagnostics.get("phase_summary", [])
        if isinstance(item, dict)
    }
    for required in (
        "promo_total",
        "collector_total",
        "archive_sync",
        "archive_lookup",
        "workbook_inspection",
        "price_truth_lookup",
        "price_truth_join",
        "source_payload_build",
    ):
        if required not in phase_keys:
            raise AssertionError(f"{snapshot_date}: promo diagnostics missing phase {required}: {diagnostics}")
    counters = diagnostics.get("counters") or {}
    if counters.get("candidate_row_count") is None or counters.get("eligible_row_count") is None:
        raise AssertionError(f"{snapshot_date}: promo diagnostics counters missing, got {counters}")
    if counters.get("validated_workbook_usable_count") is None:
        raise AssertionError(f"{snapshot_date}: validated workbook counter missing, got {counters}")
    if "collector_reuse_count" not in counters:
        raise AssertionError(f"{snapshot_date}: collector reuse counter missing, got {counters}")
    artifact_summary = diagnostics.get("artifact_validation_summary") or {}
    if artifact_summary.get("schema_version") != "promo_artifact_validation_v1":
        raise AssertionError(f"{snapshot_date}: artifact validation summary missing, got {diagnostics}")
    artifact_state_counts = diagnostics.get("artifact_state_counts") or {}
    if int(artifact_state_counts.get("complete", 0) or 0) <= 0:
        raise AssertionError(f"{snapshot_date}: complete artifact count missing, got {artifact_state_counts}")
    fingerprints = diagnostics.get("fingerprints") or {}
    if not fingerprints.get("promo_archive_fingerprint"):
        raise AssertionError(f"{snapshot_date}: promo archive fingerprint missing, got {fingerprints}")


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
            {"nm_id": requested_nm_ids[0], "plan_price": 1000.0},
            {"nm_id": requested_nm_ids[1], "plan_price": 1000.0},
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
            {"nm_id": requested_nm_ids[0], "plan_price": 1000.0},
            {"nm_id": requested_nm_ids[1], "plan_price": 1100.0},
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
            {"nm_id": requested_nm_ids[0], "plan_price": 1200.0},
            {"nm_id": requested_nm_ids[2], "plan_price": 1500.0},
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
    }
    for snapshot_date, price_by_nm_id in price_truth_by_date.items():
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


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)


if __name__ == "__main__":
    main()
