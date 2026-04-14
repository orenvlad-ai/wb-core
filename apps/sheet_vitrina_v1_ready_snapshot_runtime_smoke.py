"""Targeted smoke-check для persisted ready snapshot sheet_vitrina_v1."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import DB_FILENAME, RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1 import SheetVitrinaV1Block
from packages.contracts.sheet_vitrina_v1 import SheetVitrinaV1Request

INPUT_BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
ACTIVATED_AT = "2026-04-13T12:00:03Z"
REFRESHED_AT = "2026-04-13T12:10:00Z"


def main() -> None:
    bundle = _load_json(INPUT_BUNDLE_FIXTURE)
    plan = SheetVitrinaV1Block().execute(SheetVitrinaV1Request(bundle_type="sheet_vitrina_v1"))

    with TemporaryDirectory(prefix="sheet-vitrina-ready-snapshot-runtime-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        accepted = runtime.ingest_bundle(bundle, activated_at=ACTIVATED_AT)
        if accepted.status != "accepted":
            raise AssertionError(f"fixture bundle must be accepted, got {accepted.status}")

        current_state = runtime.load_current_state()
        refresh_result = runtime.save_sheet_vitrina_ready_snapshot(
            current_state=current_state,
            refreshed_at=REFRESHED_AT,
            plan=plan,
        )
        if refresh_result.bundle_version != current_state.bundle_version:
            raise AssertionError("refresh result bundle_version mismatch")
        if refresh_result.snapshot_id != plan.snapshot_id:
            raise AssertionError("refresh result snapshot_id mismatch")

        exact_snapshot = runtime.load_sheet_vitrina_ready_snapshot(as_of_date=plan.as_of_date)
        latest_snapshot = runtime.load_sheet_vitrina_ready_snapshot()
        if exact_snapshot.snapshot_id != plan.snapshot_id:
            raise AssertionError("exact ready snapshot mismatch")
        if latest_snapshot.snapshot_id != plan.snapshot_id:
            raise AssertionError("latest ready snapshot mismatch")

        next_bundle = dict(bundle)
        next_bundle["bundle_version"] = "sheet_vitrina_v1_snapshot_runtime__2026-04-13T12:20:00Z"
        next_bundle["uploaded_at"] = "2026-04-13T12:20:00Z"
        second_accept = runtime.ingest_bundle(next_bundle, activated_at="2026-04-13T12:20:00Z")
        if second_accept.status != "accepted":
            raise AssertionError("second bundle must also be accepted")
        try:
            runtime.load_sheet_vitrina_ready_snapshot()
        except ValueError as exc:
            if "ready snapshot missing" not in str(exc):
                raise AssertionError(f"unexpected ready-snapshot error: {exc}") from exc
        else:
            raise AssertionError("ready snapshot must not silently reuse stale snapshot after current bundle changes")

        print(f"runtime_db: ok -> {runtime_dir / DB_FILENAME}")
        print(f"refresh_result: ok -> {refresh_result.snapshot_id}")
        print(f"latest_read: ok -> {latest_snapshot.as_of_date}")
        print("stale_snapshot_guard: ok -> current bundle requires explicit refresh")
        print("smoke-check passed")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
