"""Smoke-check the immutable, bounded supply calculation registry."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (
    RegistryUploadDbBackedRuntime,
    SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS,
)


FACTORY_EXPORT = b"factory historical xlsx snapshot"
REGIONAL_EXPORT = b"regional historical zip snapshot"


def main() -> None:
    with TemporaryDirectory(prefix="supply-calculation-registry-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        _seed_legacy_regional_audit(runtime_dir / "registry_upload_runtime.sqlite3")
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)

        _check_legacy_migration(runtime)
        _check_complete_round_trip_and_atomic_collision(runtime)
        _check_retention_and_stable_pagination(runtime)

    with TemporaryDirectory(prefix="supply-calculation-legacy-bound-") as tmp:
        runtime_dir = Path(tmp) / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        _seed_legacy_regional_audit(
            runtime_dir / "registry_upload_runtime.sqlite3",
            count=202,
        )
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        page = runtime.list_supply_calculation_registry(limit=100)
        if (
            page["pagination"]["total"] != 200
            or runtime.load_supply_calculation_registry_record(
                "legacy-regional-audit:1"
            )
            is not None
            or runtime.load_supply_calculation_registry_record(
                "legacy-regional-audit:3"
            )
            is None
        ):
            raise AssertionError(
                "legacy metadata projection must retain only the newest 200 rows"
            )

    print("supply_calculation_registry_smoke: ok")


def _check_legacy_migration(runtime: RegistryUploadDbBackedRuntime) -> None:
    page = runtime.list_supply_calculation_registry(limit=10)
    if page["pagination"]["total"] != 1:
        raise AssertionError(f"legacy audit must migrate into the registry: {page}")
    legacy = page["records"][0]
    if (
        legacy["record_id"] != "legacy-regional-audit:1"
        or legacy["completeness"] != "legacy_metadata"
        or legacy["is_reproducible"]
        or legacy["download_available"]
        or legacy["selected_wb_supply_count"] != 2
        or legacy["selected_wb_supply_qty"] != 18.0
    ):
        raise AssertionError(f"legacy row must stay visibly metadata-only: {legacy}")
    detail = runtime.load_supply_calculation_registry_record(legacy["record_id"])
    if not detail or detail["payload"] is not None or not detail["legacy_note"]:
        raise AssertionError(f"legacy detail must not invent a historical payload: {detail}")
    try:
        runtime.load_supply_calculation_registry_export(legacy["record_id"])
    except ValueError as exc:
        if "metadata-only" not in str(exc):
            raise
    else:
        raise AssertionError("legacy metadata-only row must not expose a download")


def _check_complete_round_trip_and_atomic_collision(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    factory = _factory_payload(
        calculation_id="factory-registry-001",
        calculated_at="2026-07-27T06:00:00Z",
        report_date="2026-07-27",
        total_qty=125,
    )
    evidence = _evidence("factory_order", source_fingerprint="sha256:factory-source")
    runtime.save_factory_order_result_state(
        calculated_at=factory["calculated_at"],
        payload=factory,
        evidence=evidence,
        export_bytes=FACTORY_EXPORT,
        export_filename="factory-registry-001.xlsx",
        export_content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    # Exact replay is idempotent and does not create a second record.
    runtime.save_factory_order_result_state(
        calculated_at=factory["calculated_at"],
        payload=factory,
        evidence=evidence,
        export_bytes=FACTORY_EXPORT,
        export_filename="factory-registry-001.xlsx",
        export_content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )

    page = runtime.list_supply_calculation_registry(
        calculation_type="factory_order",
        report_date_from="2026-07-27",
        report_date_to="2026-07-27",
        limit=10,
    )
    if page["pagination"]["total"] != 1 or len(page["records"]) != 1:
        raise AssertionError(f"exact replay must stay idempotent: {page}")
    item = page["records"][0]
    if (
        item["calculation_id"] != factory["calculation_id"]
        or item["selected_wb_supply_count"] != 2
        or item["selected_wb_supply_qty"] != 18.0
        or item["summary"]["total_qty"] != 125
        or not item["download_available"]
    ):
        raise AssertionError(f"factory registry list projection is incomplete: {item}")

    detail = runtime.load_supply_calculation_registry_record(factory["calculation_id"])
    if (
        not detail
        or detail["payload"] != factory
        or detail["evidence"] != evidence
        or detail["metadata"]["incident_policy"]["revision"] != 7
        or detail["metadata"]["incident_policy"]["snapshot_digest"]
        != "sha256:incident-snapshot"
        or not str(detail["payload_sha256"]).startswith("sha256:")
    ):
        raise AssertionError(f"factory registry detail round-trip failed: {detail}")
    export_bytes, export_name, export_type = (
        runtime.load_supply_calculation_registry_export(factory["calculation_id"])
    )
    if (
        export_bytes != FACTORY_EXPORT
        or export_name != "factory-registry-001.xlsx"
        or "spreadsheetml.sheet" not in export_type
    ):
        raise AssertionError("factory historical export bytes were not preserved")

    collision = json.loads(json.dumps(factory))
    collision["summary"]["total_qty"] = 999
    try:
        runtime.save_factory_order_result_state(
            calculated_at=collision["calculated_at"],
            payload=collision,
            evidence=evidence,
            export_bytes=FACTORY_EXPORT,
            export_filename="factory-registry-001.xlsx",
            export_content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )
    except ValueError as exc:
        if "collision" not in str(exc):
            raise
    else:
        raise AssertionError("changed payload under the same calculation_id must fail closed")
    if runtime.load_factory_order_result_state() != factory:
        raise AssertionError("registry collision must roll back the latest-result update")
    if runtime.load_supply_calculation_registry_record(factory["calculation_id"])["payload"] != factory:
        raise AssertionError("registry collision must not rewrite immutable history")

    regional = _regional_payload(
        calculation_id="regional-registry-001",
        calculated_at="2026-07-27T06:01:00Z",
        report_date="2026-07-27",
        total_qty=84,
    )
    runtime.save_wb_regional_supply_result_state(
        calculated_at=regional["calculated_at"],
        payload=regional,
        evidence=_evidence(
            "wb_regional",
            source_fingerprint="sha256:regional-source",
        ),
        export_bytes=REGIONAL_EXPORT,
        export_filename="regional-registry-001.zip",
        export_content_type="application/zip",
    )
    runtime.save_wb_regional_supply_result_state(
        calculated_at=regional["calculated_at"],
        payload=regional,
        evidence=_evidence(
            "wb_regional",
            source_fingerprint="sha256:regional-source",
        ),
        export_bytes=REGIONAL_EXPORT,
        export_filename="regional-registry-001.zip",
        export_content_type="application/zip",
    )
    regional_audit_rows = runtime.list_wb_regional_supply_calculation_audit(
        limit=200
    )
    if (
        sum(
            1
            for row in regional_audit_rows
            if row["calculation_id"] == regional["calculation_id"]
        )
        != 1
    ):
        raise AssertionError(
            "exact registry replay must not duplicate the compatibility audit"
        )
    regional_detail = runtime.load_supply_calculation_registry_record(
        regional["calculation_id"]
    )
    if not regional_detail or regional_detail["payload"]["districts"] != regional["districts"]:
        raise AssertionError("regional per-district/per-SKU payload did not round-trip")
    if runtime.load_supply_calculation_registry_export(regional["calculation_id"])[0] != REGIONAL_EXPORT:
        raise AssertionError("regional historical ZIP bytes were not preserved")


def _check_retention_and_stable_pagination(
    runtime: RegistryUploadDbBackedRuntime,
) -> None:
    start = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
    for index in range(SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS + 2):
        calculated_at = (start + timedelta(minutes=index)).isoformat().replace(
            "+00:00", "Z"
        )
        payload = _factory_payload(
            calculation_id=f"factory-retention-{index:03d}",
            calculated_at=calculated_at,
            report_date="2026-07-27",
            total_qty=index,
        )
        runtime.save_factory_order_result_state(
            calculated_at=calculated_at,
            payload=payload,
            evidence=_evidence(
                "factory_order",
                source_fingerprint=f"sha256:retention-{index:03d}",
            ),
            export_bytes=f"snapshot-{index:03d}".encode("ascii"),
            export_filename=f"snapshot-{index:03d}.xlsx",
            export_content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
        )

    all_records = runtime.list_supply_calculation_registry(limit=100, offset=0)
    second_page = runtime.list_supply_calculation_registry(limit=100, offset=100)
    complete_records = [
        *[item for item in all_records["records"] if item["is_reproducible"]],
        *[item for item in second_page["records"] if item["is_reproducible"]],
    ]
    if (
        all_records["pagination"]["total"]
        != SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS + 1
        or len(complete_records) != SUPPLY_CALCULATION_REGISTRY_MAX_COMPLETE_RECORDS
    ):
        raise AssertionError(
            "retention must keep exactly 200 complete rows plus bounded legacy metadata"
        )

    factory_first = runtime.list_supply_calculation_registry(
        calculation_type="factory_order",
        limit=17,
        offset=0,
    )
    factory_second = runtime.list_supply_calculation_registry(
        calculation_type="factory_order",
        limit=17,
        offset=17,
    )
    first_ids = [item["record_id"] for item in factory_first["records"]]
    second_ids = [item["record_id"] for item in factory_second["records"]]
    if set(first_ids) & set(second_ids):
        raise AssertionError("stable offset pagination must not overlap adjacent pages")
    if first_ids != sorted(first_ids, reverse=True):
        raise AssertionError(f"same-timestamp tie order must be stable: {first_ids}")

    backdated = _factory_payload(
        calculation_id="factory-backdated-latest",
        calculated_at="2020-01-01T00:00:00Z",
        report_date="2020-01-01",
        total_qty=1,
    )
    runtime.save_factory_order_result_state(
        calculated_at=backdated["calculated_at"],
        payload=backdated,
        evidence=_evidence(
            "factory_order",
            source_fingerprint="sha256:backdated",
        ),
        export_bytes=b"backdated snapshot",
        export_filename="backdated.xlsx",
        export_content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    if (
        runtime.load_factory_order_result_state() != backdated
        or runtime.load_supply_calculation_registry_record(
            backdated["calculation_id"]
        )
        is None
        or runtime.load_supply_calculation_registry_record(
            "regional-registry-001"
        )
        is None
    ):
        raise AssertionError(
            "retention must preserve both latest-result compatibility records"
        )


def _factory_payload(
    *,
    calculation_id: str,
    calculated_at: str,
    report_date: str,
    total_qty: int,
) -> dict[str, object]:
    return {
        "status": "success",
        "calculation_id": calculation_id,
        "calculated_at": calculated_at,
        "report_date": report_date,
        "settings": {
            "sales_avg_period_days": 14,
            "cycle_order_days": 14,
            "selected_wb_supply_ids": ["supply-A", "supply-B"],
        },
        "summary": {
            "total_qty": total_qty,
            "estimated_weight": 31.25,
            "estimated_volume": 0.72,
        },
        "rows": [
            {
                "nm_id": 101,
                "sku_comment": "SKU 101",
                "recommended_order_qty": total_qty,
            }
        ],
        "wb_supply_overlay": {
            "selected_supply_count": 2,
            "stock_ff": {"total_selected_qty": 18.0},
        },
        "wb_warehouse_exclusion": _incident_projection(),
        "warnings": ["bounded warning"],
    }


def _regional_payload(
    *,
    calculation_id: str,
    calculated_at: str,
    report_date: str,
    total_qty: int,
) -> dict[str, object]:
    return {
        "status": "success",
        "calculation_id": calculation_id,
        "calculated_at": calculated_at,
        "report_date": report_date,
        "settings": {
            "sales_avg_period_days": 21,
            "cycle_supply_days": 14,
            "included_district_keys": ["central_north"],
            "selected_wb_supply_ids": ["supply-A"],
        },
        "summary": {
            "total_qty": total_qty,
            "estimated_weight": 21.0,
            "estimated_volume": 0.4,
        },
        "districts": [
            {
                "district_key": "central_north",
                "district_name_ru": "ЦФО Север",
                "total_qty": total_qty,
                "rows": [
                    {
                        "nm_id": 101,
                        "sku_comment": "SKU 101",
                        "allocated_qty": total_qty,
                    }
                ],
            }
        ],
        "wb_supply_overlay": {
            "selected_supply_count": 1,
            "stock_ff": {"total_selected_qty": 7.0},
        },
        "wb_warehouse_exclusion": _incident_projection(),
        "warnings": [],
    }


def _incident_projection() -> dict[str, object]:
    return {
        "policy_revision": 7,
        "policy_active": True,
        "snapshot_date": "2026-07-27",
        "snapshot_digest": "sha256:incident-snapshot",
        "policy": {
            "revision": 7,
            "active": True,
            "effective_from": "2026-07-01",
        },
        "quality": {
            "state": "complete",
            "snapshot_date": "2026-07-27",
        },
    }


def _evidence(
    calculation_type: str,
    *,
    source_fingerprint: str,
) -> dict[str, object]:
    return {
        "contract_name": "sheet_vitrina_v1_supply_calculation_evidence",
        "contract_version": 1,
        "calculation_type": calculation_type,
        "sources": {
            "sales_history": {
                "date_from": "2026-07-01",
                "date_to": "2026-07-26",
                "fingerprint": source_fingerprint,
            }
        },
        "incident_policy": {
            "policy_revision": 7,
            "snapshot_digest": "sha256:incident-snapshot",
        },
    }


def _seed_legacy_regional_audit(db_path: Path, *, count: int = 1) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sheet_vitrina_v1_wb_regional_supply_calculation_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                saved_at TEXT NOT NULL,
                calculated_at TEXT NOT NULL,
                calculation_id TEXT NOT NULL,
                report_date TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        for index in range(1, count + 1):
            calculation_id = f"legacy-regional-{index:03d}"
            metadata = {
                "calculation_id": calculation_id,
                "calculated_at": "2026-07-26T08:00:00Z",
                "report_date": "2026-07-26",
                "status": "success",
                "settings": {
                    "sales_avg_period_days": 21,
                    "selected_wb_supply_ids_count": 2,
                },
                "summary": {"total_qty": 73},
                "wb_supply_overlay_summary": {
                    "selected_supply_count": 2,
                    "stock_ff_total_selected": 18.0,
                },
            }
            conn.execute(
                """
                INSERT INTO sheet_vitrina_v1_wb_regional_supply_calculation_audit(
                    saved_at,
                    calculated_at,
                    calculation_id,
                    report_date,
                    metadata_json
                )
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    "2026-07-26T08:00:00Z",
                    "2026-07-26T08:00:00Z",
                    calculation_id,
                    "2026-07-26",
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )


if __name__ == "__main__":
    main()
