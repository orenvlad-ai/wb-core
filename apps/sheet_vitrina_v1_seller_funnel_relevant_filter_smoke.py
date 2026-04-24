"""Regression smoke for seller_funnel_snapshot relevant-row filtering."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.seller_funnel_snapshot_block import SellerFunnelSnapshotBlock
from packages.application.sheet_vitrina_v1_live_plan import (
    EXECUTION_MODE_MANUAL_OPERATOR,
    SheetVitrinaV1LivePlanBlock,
)
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotRequest


BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
AS_OF_DATE = "2026-04-21"
CURRENT_DATE = "2026-04-22"


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="sheet-vitrina-seller-filter-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-04-20T09:00:00Z")
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted}")

        current_state = runtime.load_current_state()
        enabled_nm_ids = [item.nm_id for item in current_state.config_v2 if item.enabled]
        probe_nm_id = enabled_nm_ids[0]
        source = _IncidentSellerFunnelSource()
        logs: list[str] = []
        block = SheetVitrinaV1LivePlanBlock(
            runtime=runtime,
            seller_funnel_block=SellerFunnelSnapshotBlock(source),
            closed_day_web_source_sync=_NoopClosedDaySync(),
            current_web_source_sync=_NoopClosedDaySync(),
            now_factory=lambda: datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc),
        )

        plan = block.build_plan(
            as_of_date=AS_OF_DATE,
            log=logs.append,
            execution_mode=EXECUTION_MODE_MANUAL_OPERATOR,
            source_keys=["seller_funnel_snapshot"],
            metric_keys=[
                "total_view_count",
                "total_open_card_count",
                "view_count",
                "ctr",
                "open_card_count",
            ],
        )

        data = _sheet_rows(plan, "DATA_VITRINA")
        header = data[0]
        date_index = header.index(AS_OF_DATE)
        rows = {str(row[1]): row for row in data[1:]}
        if rows[f"SKU:{probe_nm_id}|view_count"][date_index] != 1000:
            raise AssertionError(f"probe view_count must materialize from relevant row, got {rows[f'SKU:{probe_nm_id}|view_count']}")
        if rows[f"SKU:{probe_nm_id}|open_card_count"][date_index] != 100:
            raise AssertionError("probe open_card_count must materialize from relevant row")
        if rows[f"SKU:{probe_nm_id}|ctr"][date_index] != 0.11:
            raise AssertionError("probe ctr must keep percent scaling after filtering")
        if rows["TOTAL|total_view_count"][date_index] != source.expected_total_view_count:
            raise AssertionError(
                f"total_view_count must aggregate relevant rows only, got {rows['TOTAL|total_view_count']}"
            )

        status = _sheet_rows(plan, "STATUS")
        status_rows = {str(row[0]): row for row in status[1:]}
        seller_status = status_rows["seller_funnel_snapshot[yesterday_closed]"]
        note = str(seller_status[10])
        for expected in (
            f"raw_rows={len(enabled_nm_ids) + 1}",
            f"relevant_rows={len(enabled_nm_ids)}",
            "ignored_non_relevant_rows=1",
            "ignored_non_relevant_invalid_rows=1",
        ):
            if expected not in note:
                raise AssertionError(f"seller status note missing {expected!r}: {note}")
        log_text = "\n".join(logs)
        if "ignored_non_relevant_invalid_rows=1" not in log_text:
            raise AssertionError(f"source log must expose ignored invalid irrelevant rows: {log_text}")
        print(
            "seller_funnel_relevant_filter: ok ->",
            f"relevant_rows={len(enabled_nm_ids)}",
            "ignored_non_relevant_invalid_rows=1",
        )


class _IncidentSellerFunnelSource:
    def __init__(self) -> None:
        self.expected_total_view_count = 0

    def fetch(self, request: SellerFunnelSnapshotRequest) -> Mapping[str, Any]:
        items: list[dict[str, Any]] = []
        self.expected_total_view_count = 0
        for index, nm_id in enumerate(request.nm_ids):
            view_count = 1000 + index
            self.expected_total_view_count += view_count
            items.append(
                {
                    "nm_id": nm_id,
                    "name": f"SKU {nm_id}",
                    "vendor_code": f"SKU-{nm_id}",
                    "view_count": view_count,
                    "open_card_count": 100 + index,
                    "ctr": 11,
                }
            )
        items.append(
            {
                "nm_id": 999999999,
                "name": "Irrelevant broken SKU",
                "vendor_code": "BROKEN",
                "view_count": None,
                "open_card_count": 1,
                "ctr": None,
            }
        )
        return {
            "kind": "success",
            "date": request.date,
            "count": len(items),
            "items": items,
        }


class _NoopClosedDaySync:
    def ensure_closed_day_snapshot(self, *, source_key: str, snapshot_date: str) -> None:
        return None

    def ensure_current_day_snapshots(self, snapshot_date: str) -> None:
        return None


def _sheet_rows(plan: Any, sheet_name: str) -> list[list[Any]]:
    for sheet in plan.sheets:
        if sheet.sheet_name == sheet_name:
            return [sheet.header, *sheet.rows]
    raise AssertionError(f"missing sheet {sheet_name}")


if __name__ == "__main__":
    main()
