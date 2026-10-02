"""The light calendar preserves columns in the default visible ready snapshot."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.ready_publication_fixture import save_ready_fixture
from apps.sheet_vitrina_v1_web_vitrina_page_composition_smoke import BUNDLE_FIXTURE, NOW, _build_plan
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1TemporalSlot
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock


def main() -> None:
    with TemporaryDirectory(prefix="web-vitrina-shell-metadata-") as tmp:
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
        runtime.ingest_bundle(json.loads(BUNDLE_FIXTURE.read_text()), activated_at="2026-04-21T12:00:00Z")
        state = runtime.load_current_state()
        enabled = [item for item in state.config_v2 if item.enabled]
        plan = _build_plan(
            as_of_date="2026-04-20", first_nm_id=enabled[0].nm_id,
            second_nm_id=enabled[1].nm_id, first_group=enabled[0].group,
        )
        data_sheet = plan.sheets[0]
        older_day = "2026-03-01"
        data_sheet = replace(
            data_sheet,
            header=["label", "key", older_day, "2026-04-20"],
            rows=[[row[0], row[1], None, row[2]] for row in data_sheet.rows],
            column_count=4,
            write_rect="A1:D8",
        )
        plan = replace(
            plan,
            date_columns=[older_day, "2026-04-20"],
            temporal_slots=[
                SheetVitrinaV1TemporalSlot(
                    slot_key="historical_import_old",
                    slot_label="Historical import old",
                    column_date=older_day,
                ),
                *plan.temporal_slots,
            ],
            sheets=[data_sheet, *plan.sheets[1:]],
        )
        save_ready_fixture(runtime, current_state=state, refreshed_at="2026-04-20T12:00:00Z", plan=plan)
        block = SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: NOW)
        expected = block.list_readable_dates(descending=True)
        if older_day not in expected:
            raise AssertionError("fixture did not exercise an older covered column")
        original_loader = runtime.load_sheet_vitrina_ready_snapshot
        def forbidden_snapshot(*_args, **_kwargs):
            raise AssertionError("metadata shell decoded the full ready plan")
        runtime.load_sheet_vitrina_ready_snapshot = forbidden_snapshot
        try:
            actual = block.list_readable_dates_metadata(descending=True)
        finally:
            runtime.load_sheet_vitrina_ready_snapshot = original_loader
        if actual != expected:
            raise AssertionError(f"light calendar lost default snapshot columns: {expected} != {actual}")
        print("shell_metadata_calendar: ok ->", len(actual), older_day)


if __name__ == "__main__":
    main()
