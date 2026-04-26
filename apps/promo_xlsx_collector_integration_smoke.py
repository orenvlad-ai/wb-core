"""Bounded live integration smoke for repo-owned promo XLSX collector block."""

from __future__ import annotations

import json
from tempfile import mkdtemp
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.promo_xlsx_collector_block import DEFAULT_SESSION_STATE_PATH, PlaywrightPromoCollectorDriver  # noqa: E402
from packages.application.promo_xlsx_collector_block import PromoXlsxCollectorBlock  # noqa: E402
from packages.contracts.promo_xlsx_collector_block import PromoXlsxCollectorRequest  # noqa: E402


def main() -> None:
    tmp = mkdtemp(prefix="wb-core-promo-xlsx-collector-smoke-")
    request = PromoXlsxCollectorRequest(
        output_root=tmp,
        storage_state_path=DEFAULT_SESSION_STATE_PATH,
        headless=True,
        max_candidates=5,
    )
    block = PromoXlsxCollectorBlock(PlaywrightPromoCollectorDriver(Path(tmp)))
    result = block.execute(request)
    if result.status not in {"success", "partial"}:
        raise AssertionError(f"integration smoke must reach bounded success/partial state, got {result.status}")
    handled_count = result.downloaded_count + result.reused_archive_count
    if handled_count < 1:
        raise AssertionError("integration smoke must prove at least one current/future workbook via download or archive reuse")
    if not result.hydration_attempts or not any(attempt.hydrated_success for attempt in result.hydration_attempts):
        raise AssertionError("canonical direct_open hydration sequence must succeed")
    handled = [promo for promo in result.promos if promo.status in {"downloaded", "reused_archive"}]
    if not handled:
        raise AssertionError("handled promo outcome list must not be empty")
    first_handled = handled[0]
    if not first_handled.metadata_path or not Path(first_handled.metadata_path).exists():
        raise AssertionError("handled promo must persist metadata.json")
    metadata_payload = json.loads(Path(first_handled.metadata_path).read_text(encoding="utf-8"))
    for key in (
        "ui_status",
        "ui_status_confidence",
        "download_action_state",
        "status_evidence_sources",
        "collector_ui_schema_version",
        "early_preflight_decision",
        "heavy_flow_required",
        "heavy_flow_reason",
        "collector_preflight_schema_version",
        "timeline_status",
        "timeline_status_confidence",
        "timeline_evidence_sources",
        "timeline_classification_decision",
        "drawer_opened",
        "timeline_classifier_schema_version",
    ):
        if key not in metadata_payload:
            raise AssertionError(f"handled promo metadata missing UI status field {key}: {metadata_payload}")
    if not first_handled.saved_path or not Path(first_handled.saved_path).exists():
        raise AssertionError("handled promo must resolve workbook.xlsx")
    print(f"run_dir: {tmp}")
    print(f"downloaded_count: {result.downloaded_count}")
    print(f"reused_archive_count: {result.reused_archive_count}")
    print(f"skipped_past_count: {result.skipped_past_count}")
    print(f"first_handled: {first_handled.promo_title}")
    print("integration-smoke passed")


if __name__ == "__main__":
    main()
