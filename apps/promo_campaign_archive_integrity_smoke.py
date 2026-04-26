"""Dry-run promo campaign archive artifact integrity smoke."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.promo_campaign_archive import (  # noqa: E402
    ARTIFACT_STATE_AMBIGUOUS_DATE,
    ARTIFACT_STATE_COMPLETE,
    ARTIFACT_STATE_CORRUPTED,
    ARTIFACT_STATE_ENDED_WITHOUT_DOWNLOAD,
    ARTIFACT_STATE_METADATA_ONLY,
    ARTIFACT_STATE_WORKBOOK_WITHOUT_METADATA,
    audit_promo_campaign_archive,
    materialize_promo_result_from_archive,
    promo_campaign_archive_root,
    sync_promo_campaign_archive,
)
from packages.contracts.promo_xlsx_collector_block import PromoMetadata  # noqa: E402


def main() -> None:
    with TemporaryDirectory(prefix="promo-campaign-archive-integrity-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__complete",
            promo_folder="1001__2001__complete",
            promo_id=1001,
            period_id=2001,
            title="Complete artifact",
            confidence="high",
            workbook_kind="valid",
        )
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__metadata-only",
            promo_folder="1002__2002__metadata-only",
            promo_id=1002,
            period_id=2002,
            title="Metadata only artifact",
            confidence="high",
            workbook_kind="missing",
        )
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__ambiguous",
            promo_folder="1003__2003__ambiguous",
            promo_id=1003,
            period_id=2003,
            title="Ambiguous artifact",
            confidence="low",
            workbook_kind="valid",
        )
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__corrupted",
            promo_folder="1004__2004__corrupted",
            promo_id=1004,
            period_id=2004,
            title="Corrupted artifact",
            confidence="high",
            workbook_kind="corrupted",
        )
        _write_promo_fixture(
            runtime_dir=runtime_dir,
            run_name="2026-04-26__ended-no-download",
            promo_folder="1005__pending__ended-no-download",
            promo_id=1005,
            period_id=None,
            title="Ended without download artifact",
            confidence="high",
            workbook_kind="missing",
            ui_status="ended",
            download_action_state="absent",
        )
        sync_summary = sync_promo_campaign_archive(runtime_dir)
        orphan_dir = promo_campaign_archive_root(runtime_dir) / "orphan-workbook"
        orphan_dir.mkdir(parents=True, exist_ok=True)
        _write_valid_workbook(orphan_dir / "workbook.xlsx")

        audit = audit_promo_campaign_archive(runtime_dir, deep_workbook_check=True)
        counts = audit.get("artifact_state_counts") or {}
        expected = {
            ARTIFACT_STATE_COMPLETE: 1,
            ARTIFACT_STATE_METADATA_ONLY: 1,
            ARTIFACT_STATE_AMBIGUOUS_DATE: 1,
            ARTIFACT_STATE_CORRUPTED: 1,
            ARTIFACT_STATE_ENDED_WITHOUT_DOWNLOAD: 1,
            ARTIFACT_STATE_WORKBOOK_WITHOUT_METADATA: 1,
        }
        for state, expected_count in expected.items():
            actual = int(counts.get(state, 0) or 0)
            if actual != expected_count:
                raise AssertionError(f"{state}: expected {expected_count}, got {actual}: {audit}")
        if int(audit.get("validation_failed_count") or 0) != 5:
            raise AssertionError(f"expected 5 validation failures, got {audit}")
        if int(audit.get("non_materializable_expected_count") or 0) != 1:
            raise AssertionError(f"expected one expected non-materializable artifact, got {audit}")
        if len(audit.get("failure_examples") or []) != 5:
            raise AssertionError(f"expected compact failure examples, got {audit}")
        result = materialize_promo_result_from_archive(
            runtime_dir=runtime_dir,
            snapshot_date="2026-04-26",
            requested_nm_ids=[123456],
            sync_summary=sync_summary,
            diagnostics={},
        )
        if result.kind != "incomplete":
            raise AssertionError(f"invalid campaign artifacts must keep materialization incomplete, got {result}")
        result_diagnostics = result.diagnostics
        summary = result_diagnostics.get("artifact_validation_summary") or {}
        if int(summary.get("validation_failed_count") or 0) != 4:
            raise AssertionError(f"expected 4 covering validation failures, got {result_diagnostics}")
        if int(summary.get("non_materializable_expected_count") or 0) != 1:
            raise AssertionError(f"expected one expected non-materializable covering campaign, got {result_diagnostics}")
        missing_campaigns = result_diagnostics.get("missing_campaign_artifacts") or []
        if len(missing_campaigns) != 4:
            raise AssertionError(f"expected campaign-level missing artifacts, got {result_diagnostics}")
        ended = [item for item in missing_campaigns if item.get("artifact_state") == ARTIFACT_STATE_ENDED_WITHOUT_DOWNLOAD]
        if not ended or ended[0].get("workbook_required") is not False:
            raise AssertionError(f"ended no-download campaign must not require workbook, got {result_diagnostics}")
        print(
            "promo campaign archive integrity smoke passed: "
            f"states={counts}; validation_failed_count={audit.get('validation_failed_count')}"
        )


def _write_promo_fixture(
    *,
    runtime_dir: Path,
    run_name: str,
    promo_folder: str,
    promo_id: int,
    period_id: int | None,
    title: str,
    confidence: str,
    workbook_kind: str,
    ui_status: str = "active",
    download_action_state: str = "available",
) -> None:
    promo_dir = runtime_dir / "promo_xlsx_collector_runs" / run_name / "promos" / promo_folder
    promo_dir.mkdir(parents=True, exist_ok=True)
    workbook_path = promo_dir / "workbook.xlsx"
    if workbook_kind == "valid":
        _write_valid_workbook(workbook_path)
    elif workbook_kind == "corrupted":
        workbook_path.write_bytes(b"not an xlsx workbook")
    elif workbook_kind != "missing":
        raise AssertionError(f"unknown workbook_kind={workbook_kind}")
    metadata = PromoMetadata(
        collected_at=f"{run_name[:10]}T08:00:00+05:00",
        trace_run_dir=str(runtime_dir / "promo_xlsx_collector_runs" / run_name),
        source_tab="Доступные",
        source_filter_code="AVAILABLE",
        calendar_url=f"https://seller.wildberries.ru/dp-promo-calendar?action={promo_id}",
        promo_id=promo_id,
        period_id=period_id,
        promo_title=title,
        promo_period_text="26 апреля 02:00 -> 26 апреля 23:59",
        promo_start_at="2026-04-26T02:00",
        promo_end_at="2026-04-26T23:59",
        period_parse_confidence=confidence,
        temporal_classification="past" if ui_status == "ended" else "current",
        promo_status="Акция завершилась" if ui_status == "ended" else "Акция идёт",
        promo_status_text="fixture",
        eligible_count=1,
        participating_count=1,
        excluded_count=0,
        export_kind="eligible_items_report" if workbook_kind != "missing" else None,
        original_suggested_filename=f"{promo_folder}.xlsx",
        saved_filename="workbook.xlsx" if workbook_kind != "missing" else None,
        saved_path=str(workbook_path) if workbook_kind != "missing" else None,
        workbook_sheet_names=["Promo"] if workbook_kind == "valid" else [],
        workbook_row_count=2 if workbook_kind == "valid" else 0,
        workbook_col_count=2 if workbook_kind == "valid" else 0,
        workbook_header_summary=["Артикул WB", "Плановая цена для акции"] if workbook_kind == "valid" else [],
        workbook_has_date_fields=False,
        workbook_item_status_distinct_values=[],
        ui_status=ui_status,
        ui_status_confidence="high",
        ui_status_raw_labels=["Акция завершилась"] if ui_status == "ended" else ["Акция идёт"],
        download_action_state=download_action_state,
        download_action_evidence=(
            "configure_generate_download_buttons_absent"
            if download_action_state == "absent"
            else "configure_button_visible"
        ),
        status_evidence_sources=(
            ["download_button_absent", "drawer_loaded", "footer_label", "title_match"]
            if ui_status == "ended"
            else ["drawer_loaded", "footer_label", "title_match"]
        ),
        ui_loaded_success=True,
        campaign_identity_match=True,
        collector_ui_schema_version="promo_collector_ui_status_v1",
    )
    (promo_dir / "metadata.json").write_text(
        json.dumps(metadata.__dict__, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _write_valid_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Promo"
    sheet.append(["Артикул WB", "Плановая цена для акции"])
    sheet.append([123456, 999.0])
    workbook.save(path)


if __name__ == "__main__":
    main()
