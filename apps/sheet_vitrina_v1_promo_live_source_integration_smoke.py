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
from packages.contracts.promo_xlsx_collector_block import PromoMetadata


INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-23T06:00:00Z"
CHECK_DATES = ("2026-04-20", "2026-04-21", "2026-04-22")


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

        expected = {
            "2026-04-20": (1.0, 1.0, 1000.0),
            "2026-04-21": (1.0, 2.0, 1200.0),
            "2026-04-22": (1.0, 2.0, 1200.0),
        }
        for column_date in CHECK_DATES:
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
            item_index = {item.nm_id: item for item in payload.items}
            probe = item_index[requested_nm_ids[0]]
            if (
                probe.promo_participation,
                probe.promo_count_by_price,
                probe.promo_entry_price_best,
            ) != expected[column_date]:
                raise AssertionError(f"{column_date}: probe mismatch, got {probe}")
            ineligible = item_index[requested_nm_ids[1]]
            if (
                ineligible.promo_participation,
                ineligible.promo_count_by_price,
                ineligible.promo_entry_price_best,
            ) != (0.0, 0.0, 0.0):
                raise AssertionError(f"{column_date}: ineligible row must stay empty, got {ineligible}")

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
            print(
                f"{column_date}: ok -> "
                f"{probe.promo_participation}/{probe.promo_count_by_price}/{probe.promo_entry_price_best}"
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
            {"nm_id": requested_nm_ids[0], "plan_price": 1000.0, "current_price": 900.0},
            {"nm_id": requested_nm_ids[1], "plan_price": 1000.0, "current_price": 1000.0},
        ],
    )
    _write_promo_run_fixture(
        runtime_dir=runtime_dir,
        run_name="2026-04-21__fixture",
        promo_folder="2288__2237__discount-aware-a",
        promo_id=2288,
        period_id=2237,
        promo_title="Discount aware promo A",
        promo_period_text="21 апреля 02:00 → 22 апреля 23:59",
        promo_start_at="2026-04-21T02:00",
        promo_end_at="2026-04-22T23:59",
        workbook_rows=[
            {"nm_id": requested_nm_ids[0], "plan_price": 1000.0, "current_price": 1500.0, "uploadable_discount": 40.0},
            {"nm_id": requested_nm_ids[1], "plan_price": 1100.0, "current_price": 1500.0, "uploadable_discount": 20.0},
        ],
    )
    _write_promo_run_fixture(
        runtime_dir=runtime_dir,
        run_name="2026-04-21__fixture",
        promo_folder="2289__2238__discount-aware-b",
        promo_id=2289,
        period_id=2238,
        promo_title="Discount aware promo B",
        promo_period_text="21 апреля 02:00 → 22 апреля 23:59",
        promo_start_at="2026-04-21T02:00",
        promo_end_at="2026-04-22T23:59",
        workbook_rows=[
            {"nm_id": requested_nm_ids[0], "plan_price": 1200.0, "current_price": 1500.0, "current_discount": 25.0},
            {"nm_id": requested_nm_ids[2], "plan_price": 1500.0, "current_price": 1700.0},
        ],
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
    _write_discount_aware_fixture_workbook(workbook_path, workbook_rows)
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
        workbook_col_count=5,
        workbook_header_summary=[
            "Артикул WB",
            "Плановая цена для акции",
            "Текущая розничная цена",
            "Текущая скидка на сайте, %",
            "Загружаемая скидка для участия в акции",
        ],
        workbook_has_date_fields=False,
        workbook_item_status_distinct_values=[],
    )
    (promo_run_dir / "metadata.json").write_text(
        json.dumps(metadata.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_discount_aware_fixture_workbook(path: Path, rows: list[dict[str, float]]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Promo"
    sheet.append(
        [
            "Артикул WB",
            "Плановая цена для акции",
            "Текущая розничная цена",
            "Текущая скидка на сайте, %",
            "Загружаемая скидка для участия в акции",
        ]
    )
    for row in rows:
        sheet.append(
            [
                row["nm_id"],
                row["plan_price"],
                row["current_price"],
                row.get("current_discount"),
                row.get("uploadable_discount"),
            ]
        )
    workbook.save(path)


class _MutableNowFactory:
    def __init__(self, value: str) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return datetime.fromisoformat(self.value)


if __name__ == "__main__":
    main()
