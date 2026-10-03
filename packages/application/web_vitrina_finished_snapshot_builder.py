"""Build the two ready periods using the existing evaluator, outside HTTP."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
import os
import shutil
from uuid import uuid4

from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.registry_upload_http_entrypoint import build_web_vitrina_page_read
from packages.application.sheet_vitrina_v1_health import build_web_vitrina_health_operator_surface
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.storage_registry import StoreRegistry
from packages.application.web_vitrina_snapshot_pilot import (
    provision_store, publish_current_periods, retain_current_periods,
)
from packages.application.web_vitrina_window_read_context import window_read_context
from packages.business_time import current_business_date_iso


def build_current_periods(runtime_dir: Path, store: Path, *, now: datetime) -> dict:
    # Both periods share one pinned input transaction and the same captured time.
    # RegistryUploadDbBackedRuntime's constructor resolves paths only; the HTTP
    # entrypoint constructor (which starts unrelated workers) is never invoked.
    registry = StoreRegistry(runtime_dir)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir, store_registry=registry)
    for source in (runtime.db_path, registry.resolve("finance_raw"), runtime_dir / "fbs-snapshot-accounting.sqlite3"):
        if store.resolve() == source.resolve() or (store.exists() and source.exists() and os.path.samefile(store, source)):
            raise ValueError("finished snapshot store must be separate from business stores")
    if store.is_symlink():
        raise ValueError("finished snapshot store must not be a symlink")
    end = current_business_date_iso(now)
    results = {}
    with window_read_context(runtime.db_path, runtime_dir=runtime_dir):
        block = SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: now)
        for days in (14, 31):
            start = (date.fromisoformat(end) - timedelta(days=days - 1)).isoformat()
            results[(start, end)] = build_web_vitrina_page_read(
                runtime=runtime, web_vitrina_block=block, now_factory=lambda: now,
                health_surface_factory=lambda: build_web_vitrina_health_operator_surface(runtime=runtime, now=now),
                page_route="/sheet-vitrina-v1/vitrina", read_route="/v1/sheet-vitrina-v1/web-vitrina",
                operator_route="/sheet-vitrina-v1/operator", date_from=start, date_to=end,
                include_table_data=True, table_format="indexed_cells_v2",
            )
    # Provision only the explicitly configured derived store, never on GET.
    initial = not store.exists()
    if initial and store.with_name(store.name + ".initialized").exists():
        raise ValueError("initialized finished snapshot store is missing")
    target = store.with_name(store.name + ".building-" + uuid4().hex)
    try:
        if not initial:
            # This is only our own closed derived DB, never a business source.
            # Existing HTTP readers keep their old immutable inode.
            shutil.copyfile(store, target)
        provision_store(target)
        generations = publish_current_periods(target, results)
        removed = retain_current_periods(target)
        os.chmod(target, 0o640)
        with target.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(target, store)
        descriptor = os.open(store.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # Sentinel records completed publication, never mere preparation.
        # A kill after rename still leaves a usable ready pair; the next
        # successful build fills in a missing sentinel without rebuilding.
        sentinel = store.with_name(store.name + ".initialized")
        if not sentinel.exists():
            with sentinel.open("x") as stream:
                stream.write("finished pair published\n")
                stream.flush()
                os.fsync(stream.fileno())
            descriptor = os.open(store.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        target.unlink(missing_ok=True)
        Path(str(target) + "-journal").unlink(missing_ok=True)
    return {"status": "published", "business_date": end, "generation_ids": generations,
            "expired_generations": removed}
