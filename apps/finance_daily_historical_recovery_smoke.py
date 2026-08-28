#!/usr/bin/env python3
"""Fixture-only Finance daily 171-cell plan/apply/readback smoke."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.finance_daily_historical_recovery import (  # noqa: E402
    EXPECTED_TARGET_CELLS,
    SKU_METRICS,
    TOTAL_METRICS,
    apply_finance_daily_recovery,
    build_finance_daily_recovery_plan,
    readback_finance_daily_recovery,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.contracts.fin_report_daily_block import (  # noqa: E402
    FinReportDailyItem,
    FinReportDailyStorageTotal,
    FinReportDailySuccess,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)
TARGET_DATE = "2026-08-26"
SECOND_TARGET_DATE = "2026-08-27"
DEPLOYED_SHA = "a" * 40
STATUS_HEADER = [
    "source_key", "kind", "freshness", "snapshot_date", "date",
    "date_from", "date_to", "requested_count", "covered_count",
    "missing_nm_ids", "note",
]


class FixtureBlock:
    def __init__(self, result: FinReportDailySuccess) -> None:
        self.result = result

    def execute(self, request: object) -> SimpleNamespace:
        return SimpleNamespace(result=self.result)


def _source(
    nm_ids: list[int],
    *,
    target_date: str = TARGET_DATE,
) -> FinReportDailySuccess:
    items = [
        FinReportDailyItem(
            nm_id=nm_id,
            fin_delivery_rub=float(index + 1),
            fin_storage_fee=0.0,
            fin_deduction=0.0,
            fin_commission=0.0,
            fin_penalty=0.0,
            fin_additional_payment=0.0,
            fin_buyout_rub=float((index + 1) * 100),
            fin_commission_wb_portal=float(index + 2),
            fin_acquiring_fee=float(-(index + 1)) if index == 0 else float(index + 1),
            fin_loyalty_rub=(0.1 + 0.2) if index == 0 else float(index) / 10.0,
        )
        for index, nm_id in enumerate(nm_ids)
    ]
    return FinReportDailySuccess(
        kind="success",
        snapshot_date=target_date,
        count=len(items),
        items=items,
        storage_total=FinReportDailyStorageTotal(
            nm_id=0,
            fin_storage_fee_total=77.5,
        ),
        diagnostics={
            "endpoint": "POST /api/finance/v1/sales-reports/detailed",
            "period": "daily",
            "pagination": {
                "pages": 1,
                "rrdid_start": 0,
                "rrdid_end": 123456,
                "terminal_status": 204,
                "complete": True,
            },
            "source_digest": "sha256:" + "b" * 64,
            "source_row_count": 99,
            "exact_date_row_count": 99,
            "target_row_count": 98,
            "requested_count": 33,
            "covered_count": 33,
        },
    )


def _ready_plan(nm_ids: list[int]) -> SheetVitrinaV1Envelope:
    rows: list[list[object]] = []
    for metric in SKU_METRICS:
        rows.extend(
            [
                [f"SKU {nm_id}: {metric}", f"SKU:{nm_id}|{metric}", "", ""]
                for nm_id in nm_ids
            ]
        )
    rows.extend(
        [[f"TOTAL {metric}", f"TOTAL|{metric}", "", ""] for metric in TOTAL_METRICS]
    )
    rows.append(
        ["Proxy gap must remain blank", "SKU:428853741|proxy_profit_3_rub", "", ""]
    )
    status_rows = [
        [
            "fin_report_daily[yesterday_closed]", "missing", "", "", TARGET_DATE,
            "", "", 33, 0, ",".join(str(item) for item in nm_ids), "fixture missing",
        ],
        [
            "stocks[yesterday_closed]", "incomplete", "", "", TARGET_DATE,
            "", "", 33, 32, str(nm_ids[-1]), "another source remains incomplete",
        ],
    ]
    return SheetVitrinaV1Envelope(
        plan_version="fixture-v1",
        snapshot_id="finance-recovery-fixture",
        as_of_date="2026-08-27",
        date_columns=[TARGET_DATE, SECOND_TARGET_DATE],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed",
                slot_label="yesterday_closed",
                column_date=TARGET_DATE,
            ),
            SheetVitrinaV1TemporalSlot(
                slot_key="today_current",
                slot_label="today_current",
                column_date=SECOND_TARGET_DATE,
            ),
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:D{len(rows) + 1}",
                clear_range=f"A1:D{len(rows) + 1}",
                write_mode="replace",
                partial_update_allowed=False,
                header=["label", "key", TARGET_DATE, SECOND_TARGET_DATE],
                rows=rows,
                row_count=len(rows),
                column_count=4,
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect=f"A1:K{len(status_rows) + 1}",
                clear_range=f"A1:K{len(status_rows) + 1}",
                write_mode="replace",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=status_rows,
                row_count=len(status_rows),
                column_count=len(STATUS_HEADER),
            ),
        ],
        metadata={"unrelated": {"digest": "preserve-me"}},
    )


def main() -> None:
    bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="finance-daily-recovery-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-08-27T08:00:00Z")
        assert accepted.status == "accepted"
        current = runtime.load_current_state()
        nm_ids = sorted(int(item.nm_id) for item in current.config_v2 if item.enabled)
        assert len(nm_ids) == 33
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current,
            refreshed_at="2026-08-27T08:10:00Z",
            plan=_ready_plan(nm_ids),
        )
        plan, _, _ = build_finance_daily_recovery_plan(
            runtime,
            target_date=TARGET_DATE,
            deployed_sha=DEPLOYED_SHA,
            block=FixtureBlock(_source(nm_ids)),
            generated_at="2026-08-28T09:00:00Z",
        )
        assert plan["expected_target_cells"] == EXPECTED_TARGET_CELLS == 171
        assert plan["changed_cells"] == 171
        assert plan["source"]["coverage"] == "33/33"
        assert plan["source"]["terminal_status"] == 204
        assert not plan["proxy_gap_exclusion"]["mutated"]
        assert all(
            round(float(cell["expected"]), 6) == float(cell["expected"])
            for cell in plan["after_manifest"]["cells"].values()
        )
        first_loyalty = f"SKU:{nm_ids[0]}|fin_loyalty_rub"
        assert _source(nm_ids).items[0].fin_loyalty_rub != plan["after_manifest"][
            "cells"
        ][first_loyalty]["expected"]

        parity_ready = _ready_plan(nm_ids)
        parity_data = next(
            sheet for sheet in parity_ready.sheets if sheet.sheet_name == "DATA_VITRINA"
        )
        expected = plan["after_manifest"]["cells"]
        date_index = parity_data.header.index(TARGET_DATE)
        for row in parity_data.rows:
            row_id = str(row[1] or "")
            if row_id in expected:
                row[date_index] = float(expected[row_id]["expected"])
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current,
            refreshed_at="2026-08-27T08:11:00Z",
            plan=parity_ready,
        )
        parity_plan, _, _ = build_finance_daily_recovery_plan(
            runtime,
            target_date=TARGET_DATE,
            deployed_sha=DEPLOYED_SHA,
            block=FixtureBlock(_source(nm_ids)),
            generated_at="2026-08-28T09:00:30Z",
        )
        assert parity_plan["changed_cells"] == 0
        assert parity_plan["parity_status"] == "exact"

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current,
            refreshed_at="2026-08-27T08:12:00Z",
            plan=_ready_plan(nm_ids),
        )

        result = apply_finance_daily_recovery(
            runtime,
            reviewed_plan=plan,
            fingerprint=str(plan["fingerprint"]),
            approval_reference="WBC0020 owner accepted exact 2026-08-26 recovery",
            actor="fixture",
            deployed_sha=DEPLOYED_SHA,
            applied_at="2026-08-28T09:01:00Z",
        )
        assert result["status"] == "applied"
        readback = result["readback"]
        assert readback["status"] == "complete"
        assert readback["accepted_cells"] == "171/171"
        assert readback["coverage"] == "33/33"
        assert readback["overall_semantic_status"] != "success"
        assert readback["proxy_gap_exclusion"]["value"] in (None, "")
        assert all(readback["checks"].values())

        repeat = apply_finance_daily_recovery(
            runtime,
            reviewed_plan=plan,
            fingerprint=str(plan["fingerprint"]),
            approval_reference="WBC0020 owner accepted exact 2026-08-26 recovery",
            actor="fixture",
            deployed_sha=DEPLOYED_SHA,
            applied_at="2026-08-28T09:02:00Z",
        )
        assert repeat["status"] == "no_op"
        assert repeat["readback"]["status"] == "complete"
        with sqlite3.connect(runtime.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM sheet_vitrina_v1_finance_daily_recovery_audit"
            ).fetchone()[0]
            assert count == 1

        second_plan, _, _ = build_finance_daily_recovery_plan(
            runtime,
            target_date=SECOND_TARGET_DATE,
            deployed_sha=DEPLOYED_SHA,
            block=FixtureBlock(_source(nm_ids, target_date=SECOND_TARGET_DATE)),
            generated_at="2026-08-28T09:03:00Z",
        )
        second = apply_finance_daily_recovery(
            runtime,
            reviewed_plan=second_plan,
            fingerprint=str(second_plan["fingerprint"]),
            approval_reference="WBC0020 owner accepted exact 2026-08-27 recovery",
            actor="fixture",
            deployed_sha=DEPLOYED_SHA,
            applied_at="2026-08-28T09:04:00Z",
        )
        assert second["status"] == "applied"
        assert second["readback"]["status"] == "complete"
        chained_first = readback_finance_daily_recovery(
            runtime,
            operation_id=str(plan["operation_id"]),
        )
        assert chained_first["status"] == "complete"
        assert chained_first["successor_operations"] == [second_plan["operation_id"]]
        assert all(chained_first["checks"].values())
        try:
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=current,
                refreshed_at="2026-08-28T09:05:00Z",
                plan=_ready_plan(nm_ids),
            )
        except ValueError as exc:
            assert "producer regression" in str(exc)
        else:
            raise AssertionError("ordinary producer regressed an audited Finance recovery")

    print("finance_daily_historical_recovery_smoke: OK")


if __name__ == "__main__":
    main()
