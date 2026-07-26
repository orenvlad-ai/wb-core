#!/usr/bin/env python3
"""API and operator-template smoke for unified warehouse recovery status."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    WarehouseRecoveryRegistry,
)


def main() -> int:
    with TemporaryDirectory(prefix="warehouse-recovery-http-") as raw:
        runtime_dir = Path(raw)
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.executescript(
                """
                CREATE TABLE bounded_http_rows(
                    row_id TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO bounded_http_rows VALUES('terminal','before');
                INSERT INTO bounded_http_rows VALUES('failed','before');
                CREATE TABLE sheet_vitrina_v1_warehouse_wb_sync_status(
                    slot INTEGER PRIMARY KEY,
                    last_attempt_at TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    active_version_id TEXT,
                    updated_at TEXT
                );
                INSERT INTO sheet_vitrina_v1_warehouse_wb_sync_status
                VALUES(1,'2026-07-26T00:00:00Z','2026-07-26T00:00:00Z',
                       NULL,'v1','2026-07-26T00:00:00Z');
                """
            )
            conn.commit()
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir,
            db_path=db_path,
            operational_reserve_bytes=0,
        )
        terminal = registry.prepare_t1(
            mutation_kind="supplier_cost_queue_replay",
            closure_kind="shipment",
            plan_fingerprint="sha256:http-terminal",
            scope={"shipment_ids": ["26GN582"], "nm_ids": [101]},
            before_images=[
                {
                    "table": "bounded_http_rows",
                    "key": {"row_id": "terminal"},
                    "before": {"row_id": "terminal", "value": "before"},
                    "after": {"row_id": "terminal", "value": "after"},
                }
            ],
        )
        registry.begin_mutation(terminal["operation_id"])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE bounded_http_rows SET value='after' "
                "WHERE row_id='terminal'"
            )
            conn.commit()
        registry.retain(terminal["operation_id"], after_digest="sha256:after")

        failed = registry.prepare_t1(
            mutation_kind="ff_ledger_operation",
            closure_kind="document",
            plan_fingerprint="sha256:http-failed",
            scope={"preview_id": "fixture", "nm_ids": [202]},
            before_images=[
                {
                    "table": "bounded_http_rows",
                    "key": {"row_id": "failed"},
                    "before": {"row_id": "failed", "value": "before"},
                    "after": {"row_id": "failed", "value": "after"},
                }
            ],
        )
        registry.fail_recoverable(
            failed["operation_id"],
            error="fixture capacity/readback failure",
            next_action="resume_or_rollback_fixture",
        )

        entrypoint = object.__new__(RegistryUploadHttpEntrypoint)
        entrypoint.runtime = SimpleNamespace(
            runtime_dir=runtime_dir,
            db_path=db_path,
        )
        lock_path = runtime_dir / ".warehouse-functional-sync.lock"
        before_digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
        payload = entrypoint.handle_warehouse_recovery_status_request()
        after_digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
        if before_digest != after_digest or lock_path.exists():
            raise AssertionError("recovery status GET must not mutate DB or lock files")
        if payload["status"] != "attention_required":
            raise AssertionError("failed operation must be visible as actionable")
        if [item["lifecycle"] for item in payload["operations"][:2]] != [
            "failed_recoverable",
            "retained",
        ]:
            raise AssertionError("active/failed recovery ordering changed")
        required_operation_fields = {
            "tier",
            "scope",
            "planned_bytes",
            "actual_bytes",
            "read_bytes",
            "lifecycle",
            "next_action",
            "writer_state",
            "timer_state",
            "orphan_status",
            "rollback",
            "artifacts",
        }
        if not required_operation_fields <= set(payload["operations"][0]):
            raise AssertionError("recovery API omitted required operation fields")
        if not {
            "capacity",
            "writer",
            "timer",
            "orphan_scanner",
            "tiers",
        } <= set(payload):
            raise AssertionError("recovery API omitted operator status fields")
        if not {
            "policy_activation_at",
            "pre_policy_legacy_count",
            "pre_policy_legacy_paths",
        } <= set(payload["orphan_scanner"]):
            raise AssertionError(
                "recovery API omitted pre-policy baseline evidence"
            )

    adapter = (
        ROOT / "packages/adapters/registry_upload_http_entrypoint.py"
    ).read_text(encoding="utf-8")
    template = (
        ROOT
        / "packages/adapters/templates/sheet_vitrina_v1_web_vitrina.html"
    ).read_text(encoding="utf-8")
    if 'DEFAULT_WAREHOUSES_RECOVERY_PATH = f"{DEFAULT_WAREHOUSES_PATH}/recovery"' not in adapter:
        raise AssertionError("warehouse recovery HTTP route is missing")
    for marker in (
        "data-warehouse-recovery-status",
        "data-warehouse-recovery-capacity",
        "data-warehouse-recovery-writer",
        "data-warehouse-recovery-orphans",
        "data-warehouse-recovery-next",
        "data-warehouse-recovery-operations",
        "data-warehouse-recovery-technical",
        'fetch(basePath + "/recovery"',
        "planned_bytes",
        "actual_bytes",
        "read_bytes",
            "rollback.available",
            "quarantine_candidates",
            "expired_reservations",
            "pre_policy_legacy_count",
            "pre-policy baseline",
        ):
        if marker not in template:
            raise AssertionError(f"recovery operator UI marker is missing: {marker}")
    print("warehouse_recovery_policy_http_smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
