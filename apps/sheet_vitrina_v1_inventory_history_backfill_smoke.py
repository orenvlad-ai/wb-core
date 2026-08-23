#!/usr/bin/env python3
"""End-to-end smoke for compact inventory history and guarded backfill."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_inventory_history_backfill import (  # noqa: E402
    InventoryHistoryBackfillError,
    run_backfill,
)
from packages.application.ff_pool_cutover import (  # noqa: E402
    ALLOCATIONS_TABLE,
    MANIFESTS_TABLE,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    MAPPING_EXTENSION_ALLOCATIONS_TABLE,
    MAPPING_EXTENSIONS_TABLE,
)
from packages.application.ff_pool_foundation import (  # noqa: E402
    FACILITIES_TABLE,
    FACILITY_PROFILES_TABLE,
    FEATURE_EPOCHS_TABLE,
)
from packages.application.finance_raw_storage import (  # noqa: E402
    bind_generation_identity,
    ensure_operational_schema,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    CAPTURES_TABLE,
    COMPONENTS_TABLE,
    FINALIZATIONS_TABLE,
    read_inventory_history_window,
)
from packages.application.sheet_vitrina_v1_inventory_planning import (  # noqa: E402
    COMBINED_TOTAL_ALIAS_KEY,
    INVENTORY_WB_TOTAL_KEY,
    extend_rows_with_inventory_planning,
    inventory_planning_facility_metric_key,
    inventory_planning_total_metric_key,
)
from packages.application.storage_registry import (  # noqa: E402
    StoreRegistry,
    atomic_write_manifest,
    build_manifest,
)
from packages.application.wb_fbs_orders import (  # noqa: E402
    OBSERVATIONS_TABLE,
    WAREHOUSE_MAPPINGS_TABLE,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow  # noqa: E402


DEPLOYED_SHA = "a" * 40
BUNDLE_FIXTURE = (
    ROOT
    / "artifacts"
    / "registry_upload_http_entrypoint"
    / "input"
    / "registry_upload_bundle__fixture.json"
)


def main() -> int:
    with TemporaryDirectory(prefix="inventory-history-backfill-") as raw:
        root = Path(raw)
        runtime_dir = root / "runtime"
        evidence_dir = root / "evidence"
        sha_file = root / "runtime-sha"
        sha_file.write_text(DEPLOYED_SHA + "\n", encoding="utf-8")
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))
        bundle["bundle_version"] = "inventory-history-smoke-v1"
        bundle["uploaded_at"] = "2026-08-09T08:00:00Z"
        accepted = runtime.ingest_bundle(bundle, activated_at="2026-08-09T08:00:00Z")
        assert accepted.status == "accepted"
        state = runtime.load_current_state()
        enabled = [item for item in state.config_v2 if item.enabled]
        first_nm_id, second_nm_id = int(enabled[0].nm_id), int(enabled[1].nm_id)
        for business_date in _dates("2026-08-09", "2026-08-21"):
            runtime.save_sheet_vitrina_ready_snapshot(
                current_state=state,
                refreshed_at=business_date + "T20:00:00Z",
                plan=_ready_plan(
                    business_date=business_date,
                    first_nm_id=first_nm_id,
                    second_nm_id=second_nm_id,
                ),
            )
        _seed_fbs_sources(
            runtime.db_path,
            first_nm_id=first_nm_id,
            second_nm_id=second_nm_id,
        )
        monolith_path = runtime.db_path
        _activate_split_generation(
            runtime_dir=runtime_dir,
            monolith_path=monolith_path,
        )
        monolith_digest = _file_digest(monolith_path)
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        assert runtime.db_path != monolith_path
        original_digest = _file_digest(runtime.db_path)
        dry_run = run_backfill(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            apply=False,
            deployed_sha=DEPLOYED_SHA,
            date_from="2026-08-09",
            date_to="2026-08-21",
            deployed_sha_file=sha_file,
            now=datetime(2026, 8, 22, 12, tzinfo=timezone.utc),
        )
        assert dry_run["status"] == "ready", dry_run
        assert dry_run["database_written"] is False
        assert _file_digest(runtime.db_path) == original_digest
        assert dry_run["target_capture_count"] == 13
        assert dry_run["target_sku_count"] == 2
        assert dry_run["partial"] > 0
        assert dry_run["full"] > 0
        assert dry_run["inapplicable"] > 0
        assert dry_run["partition_units"] == {
            "full": "scopes",
            "partial": "scopes",
            "unavailable": "dates",
            "inapplicable": "components",
        }
        manifest = json.loads(Path(dry_run["manifest_path"]).read_text(encoding="utf-8"))
        by_date = {item["business_date"]: item for item in manifest["captures"]}
        assert by_date["2026-08-10"]["proposed_values_by_scope"]["TOTAL"][
            "quality"
        ] == "partial"
        assert by_date["2026-08-10"]["before"]["values_by_scope"] == {}
        assert by_date["2026-08-10"]["finalization_identity"].startswith(
            "backfill:sheet_vitrina_v1_inventory_history_backfill_v1:"
        )
        assert _combined(by_date["2026-08-09"], "TOTAL") == (30, "full")
        assert _combined(by_date["2026-08-10"], "TOTAL") == (30, "partial")
        assert _combined(by_date["2026-08-14"], "TOTAL") == (35, "full")
        assert _combined(by_date["2026-08-19"], "TOTAL") == (35, "partial")
        assert _combined(by_date["2026-08-20"], "TOTAL") == (42, "full")

        reviewed_storage = StoreRegistry(runtime_dir).load(require_files=True)
        drifted_storage = build_manifest(
            state=reviewed_storage.state,
            canonical_source=reviewed_storage.canonical_source,
            generation_epoch=reviewed_storage.generation_epoch,
            raw_generation_id=reviewed_storage.raw.generation_id,
            raw_relative_path=reviewed_storage.raw.relative_path,
            raw_watermark=reviewed_storage.raw.watermark,
            operational_generation_id=reviewed_storage.operational.generation_id,
            operational_relative_path=reviewed_storage.operational.relative_path,
            operational_watermark="sha256:" + "5" * 64,
            rollback_generation_id=reviewed_storage.rollback_generation_id,
            source_fingerprint=reviewed_storage.source_fingerprint,
            created_at=reviewed_storage.created_at,
        )
        atomic_write_manifest(
            runtime_dir / "storage_generation_manifest.json",
            drifted_storage,
        )
        try:
            run_backfill(
                runtime_dir=runtime_dir,
                evidence_dir=evidence_dir,
                apply=True,
                deployed_sha=DEPLOYED_SHA,
                manifest_path=Path(dry_run["manifest_path"]),
                expected_manifest_sha256=dry_run["manifest_sha256"],
                approval_reference="synthetic-smoke-owner-gate",
                deployed_sha_file=sha_file,
                now=datetime(2026, 8, 22, 12, 2, tzinfo=timezone.utc),
            )
        except InventoryHistoryBackfillError as exc:
            assert "schema/generation changed after dry-run" in str(exc)
        else:
            raise AssertionError("storage generation drift must block apply")
        finally:
            atomic_write_manifest(
                runtime_dir / "storage_generation_manifest.json",
                reviewed_storage,
            )
        assert _file_digest(runtime.db_path) == original_digest

        tampered = dict(manifest)
        tampered["expected_effect"] = dict(manifest["expected_effect"])
        tampered["expected_effect"]["inserted_capture_count"] += 1
        tampered.pop("plan_fingerprint", None)
        tampered["plan_fingerprint"] = _json_digest(tampered)
        tampered_path = evidence_dir / "tampered-reviewed-manifest.json"
        tampered_path.write_text(
            json.dumps(tampered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        try:
            run_backfill(
                runtime_dir=runtime_dir,
                evidence_dir=evidence_dir,
                apply=True,
                deployed_sha=DEPLOYED_SHA,
                manifest_path=tampered_path,
                expected_manifest_sha256="sha256:" + _file_digest(tampered_path),
                approval_reference="synthetic-smoke-owner-gate",
                deployed_sha_file=sha_file,
                now=datetime(2026, 8, 22, 12, 4, tzinfo=timezone.utc),
            )
        except InventoryHistoryBackfillError as exc:
            assert "row-count reconciliation" in str(exc)
        else:
            raise AssertionError("tampered expected row counts must roll back")
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {CAPTURES_TABLE}").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {FINALIZATIONS_TABLE}").fetchone()[0] == 0

        applied = run_backfill(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            apply=True,
            deployed_sha=DEPLOYED_SHA,
            manifest_path=Path(dry_run["manifest_path"]),
            expected_manifest_sha256=dry_run["manifest_sha256"],
            approval_reference="synthetic-smoke-owner-gate",
            deployed_sha_file=sha_file,
            now=datetime(2026, 8, 22, 12, 5, tzinfo=timezone.utc),
        )
        assert applied["status"] == "reconciled", applied
        assert applied["database_written"] is True
        assert applied["recovery_evidence_path"].endswith(".json")
        assert Path(applied["recovery_evidence_path"]).is_file()
        history = read_inventory_history_window(
            runtime.db_path,
            dates=["2026-08-09", "2026-08-10", "2026-08-14", "2026-08-19", "2026-08-20"],
            current_date="2026-08-22",
        )
        assert history["dates"]["2026-08-09"]["scopes"]["TOTAL"]["total"] == 30
        assert history["dates"]["2026-08-10"]["scopes"]["TOTAL"]["quality"] == "partial"
        assert history["dates"]["2026-08-14"]["scopes"]["TOTAL"]["total"] == 35
        assert history["dates"]["2026-08-19"]["scopes"]["TOTAL"]["missing_components"] == [
            "Оренбург"
        ]
        assert history["dates"]["2026-08-20"]["scopes"]["TOTAL"]["total"] == 42
        _assert_historical_projection(
            history=history,
            enabled_config=enabled,
            first_nm_id=first_nm_id,
        )
        with sqlite3.connect(runtime.db_path) as conn:
            assert conn.execute(f"SELECT COUNT(*) FROM {CAPTURES_TABLE}").fetchone()[0] == 13
            assert conn.execute(f"SELECT COUNT(*) FROM {FINALIZATIONS_TABLE}").fetchone()[0] == 13
            component_count = conn.execute(f"SELECT COUNT(*) FROM {COMPONENTS_TABLE}").fetchone()[0]
            assert component_count == dry_run["target_component_count"]
        replay = run_backfill(
            runtime_dir=runtime_dir,
            evidence_dir=evidence_dir,
            apply=True,
            deployed_sha=DEPLOYED_SHA,
            manifest_path=Path(dry_run["manifest_path"]),
            expected_manifest_sha256=dry_run["manifest_sha256"],
            approval_reference="synthetic-smoke-owner-gate",
            deployed_sha_file=sha_file,
            now=datetime(2026, 8, 22, 12, 10, tzinfo=timezone.utc),
        )
        assert replay["status"] == "already_applied"
        assert replay["database_written"] is False
        assert _file_digest(monolith_path) == monolith_digest
    print("sheet_vitrina_v1_inventory_history_backfill_smoke: OK")
    return 0


def _ready_plan(
    *,
    business_date: str,
    first_nm_id: int,
    second_nm_id: int,
) -> SheetVitrinaV1Envelope:
    rows = [
        ["Остатки", "TOTAL|total_stock_total", 30],
        ["Остатки", f"SKU:{first_nm_id}|stock_total", 10],
        ["Остатки", f"SKU:{second_nm_id}|stock_total", 20],
    ]
    return SheetVitrinaV1Envelope(
        plan_version="inventory-history-smoke-plan-v1",
        snapshot_id="inventory-history-" + business_date,
        as_of_date=business_date,
        date_columns=[business_date],
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="historical_import",
                slot_label="Historical import",
                column_date=business_date,
            )
        ],
        source_temporal_policies={},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:C{len(rows) + 1}",
                clear_range="A:C",
                write_mode="values",
                partial_update_allowed=False,
                header=["label", "key", business_date],
                rows=rows,
                row_count=len(rows),
                column_count=3,
            )
        ],
    )


def _activate_split_generation(*, runtime_dir: Path, monolith_path: Path) -> None:
    generation_epoch = "1" * 20
    generation_root = runtime_dir / "generations" / generation_epoch
    generation_root.mkdir(parents=True)
    raw_path = generation_root / "finance-raw.sqlite3"
    operational_path = generation_root / "operational.sqlite3"
    with sqlite3.connect(raw_path):
        pass
    shutil.copy2(monolith_path, operational_path)
    manifest = build_manifest(
        state="cutover",
        canonical_source="split",
        generation_epoch=generation_epoch,
        raw_generation_id="finance-raw-" + generation_epoch,
        raw_relative_path=str(raw_path.relative_to(runtime_dir)),
        raw_watermark="sha256:" + "2" * 64,
        operational_generation_id="operational-" + generation_epoch,
        operational_relative_path=str(operational_path.relative_to(runtime_dir)),
        operational_watermark="sha256:" + "3" * 64,
        rollback_generation_id="monolith",
        source_fingerprint="sha256:" + "4" * 64,
        created_at="2026-08-22T11:59:00Z",
    )
    with sqlite3.connect(operational_path) as conn:
        ensure_operational_schema(conn)
        bind_generation_identity(
            conn,
            logical_store="operational",
            generation_id=manifest.operational.generation_id,
            generation_epoch=manifest.generation_epoch,
            source_fingerprint=manifest.source_fingerprint,
        )
        conn.commit()
    atomic_write_manifest(runtime_dir / "storage_generation_manifest.json", manifest)


def _seed_fbs_sources(db_path: Path, *, first_nm_id: int, second_nm_id: int) -> None:
    with sqlite3.connect(db_path) as conn:
        epoch = 71
        conn.execute(
            f"INSERT INTO {FEATURE_EPOCHS_TABLE}(epoch,writer_enabled,reader_enabled,source_revision,created_at,metadata_json) VALUES(?,1,1,'inventory-history-smoke','2026-08-14T08:00:00Z','{{}}')",
            (epoch,),
        )
        for facility_id, code, name in (
            ("moscow", "FF-MOSCOW", "Москва"),
            ("orenburg", "FF-ORENBURG", "Оренбург"),
        ):
            conn.execute(
                f"INSERT INTO {FACILITIES_TABLE}(facility_id,code,name,active,display_timezone,created_at,updated_at) VALUES(?,?,?,1,'Asia/Yekaterinburg','2026-08-01T00:00:00Z','2026-08-20T10:00:00Z')",
                (facility_id, code, name),
            )
            conn.execute(
                f"INSERT INTO {FACILITY_PROFILES_TABLE}(facility_id,city,future_fields_json,created_at,updated_at) VALUES(?,?,'{{}}','2026-08-01T00:00:00Z','2026-08-20T10:00:00Z')",
                (facility_id, name),
            )
        for mapping_id, warehouse_id, facility_id in (
            ("map-moscow", 1988668, "moscow"),
            ("map-orenburg", 854205, "orenburg"),
        ):
            conn.execute(
                f"INSERT INTO {WAREHOUSE_MAPPINGS_TABLE}(mapping_id,seller_warehouse_id,facility_id,mapping_digest,active,created_at,created_by) VALUES(?,?,?,'sha256:mapping',1,'2026-08-20T10:00:00Z','smoke')",
                (mapping_id, warehouse_id, facility_id),
            )
        for sequence, order_id, created_at, warehouse_id, nm_id in (
            (1, 1001, "2026-08-10T08:00:00Z", 1988668, first_nm_id),
            (2, 1002, "2026-08-19T08:00:00Z", 854205, second_nm_id),
        ):
            conn.execute(
                f"""INSERT INTO {OBSERVATIONS_TABLE}(
                        observation_sequence,observation_id,order_id,source_revision,
                        supply_id,delivery_type,source_created_at,warehouse_id,office_id,
                        nm_id,chrt_id,seller_sku,rid_sha256,order_uid_sha256,skus_json,
                        cargo_type,cross_border_type,is_zero_order,observed_at,
                        collector_date_from,collector_date_to,collector_cursor
                    ) VALUES(?,?,?,'revision-123','','fbs',?,?,1,?,1,'','sha256:rid',
                             'sha256:uid','[]',1,0,0,?,1,2,0)""",
                (
                    sequence,
                    f"observation-{sequence}",
                    order_id,
                    created_at,
                    warehouse_id,
                    nm_id,
                    created_at,
                ),
            )
        conn.execute(
            f"""INSERT INTO {MANIFESTS_TABLE}(
                    cutover_id,manifest_digest,deployed_sha,cutover_at,business_date,
                    feature_epoch,aggregate_revision,aggregate_digest,detail_digest,
                    observation_watermark_sequence,observation_watermark_digest,
                    mapping_digest,fbw_origins_digest,control_evidence_digest,
                    non_target_digest,opening_document_id,source_snapshot_digest,
                    created_at,manifest_json
                ) VALUES('cutover-moscow','sha256:cutover-moscow',?,'2026-08-14T08:00:00Z',
                         '2026-08-14',?,'aggregate','sha256:aggregate','sha256:detail',2,
                         'sha256:orders','sha256:mapping','sha256:origins','sha256:control',
                         'sha256:non-target','opening-moscow','sha256:snapshot',
                         '2026-08-14T08:00:00Z','{{}}')""",
            (DEPLOYED_SHA, epoch),
        )
        conn.execute(
            f"""INSERT INTO {ALLOCATIONS_TABLE}(
                    cutover_id,line_no,facility_id,pool,nm_id,quantity,
                    capital_rub,wac_rub,allocation_digest
                ) VALUES('cutover-moscow',1,'moscow','FBS',?,5,'0',NULL,'sha256:alloc')""",
            (first_nm_id,),
        )
        boundary = json.dumps({"local_boundary_at": "2026-08-20T08:00:00Z"})
        conn.execute(
            f"""INSERT INTO {MAPPING_EXTENSIONS_TABLE}(
                    extension_id,cutover_id,warehouse_mapping_id,seller_warehouse_id,
                    official_office_id,facility_id,source_receipt_document_id,
                    source_receipt_root_document_id,source_receipt_digest,mapping_digest,
                    official_evidence_digest,frozen_boundary_json,frozen_rows_digest,
                    plan_fingerprint,deployed_sha,approval_reference,created_by,created_at
                ) VALUES('extension-orenburg','cutover-moscow','map-orenburg',854205,1,
                         'orenburg','receipt','root','sha256:receipt','sha256:mapping',
                         'sha256:official',?,'sha256:rows','sha256:plan',?,
                         'synthetic-smoke','smoke','2026-08-20T08:00:00Z')""",
            (boundary, DEPLOYED_SHA),
        )
        conn.execute(
            f"""INSERT INTO {MAPPING_EXTENSION_ALLOCATIONS_TABLE}(
                    extension_id,nm_id,opening_quantity,opening_capital_rub,
                    frozen_wac_rub,source_balance_watermark,allocation_digest,created_at
                ) VALUES('extension-orenburg',?,7,'0','0','watermark','sha256:allocation',
                         '2026-08-20T08:00:00Z')""",
            (second_nm_id,),
        )
        conn.commit()


def _combined(capture: dict[str, object], scope_key: str) -> tuple[int, str]:
    components = [
        item
        for item in capture["components"]  # type: ignore[index]
        if item["scope_key"] == scope_key  # type: ignore[index]
    ]
    known = [
        int(item["quantity"])
        for item in components
        if item["state"] in {"exact", "exact_zero"}
    ]
    missing = [item for item in components if item["state"] == "missing"]
    return sum(known), "partial" if missing else "full"


def _assert_historical_projection(
    *,
    history: dict[str, object],
    enabled_config: list[object],
    first_nm_id: int,
) -> None:
    business_date = "2026-08-10"
    source_rows = [
        WebVitrinaContractRow(
            row_id="TOTAL|total_stock_total",
            row_order=1,
            scope_kind="TOTAL",
            scope_key="TOTAL",
            scope_label="ИТОГО",
            metric_key="total_stock_total",
            metric_label="Legacy stock",
            row_last_updated_at="2026-08-10T20:00:00Z",
            section="Остатки",
            group=None,
            nm_id=None,
            format="integer",
            values_by_date={business_date: 30},
        ),
        WebVitrinaContractRow(
            row_id=f"SKU:{first_nm_id}|stock_total",
            row_order=2,
            scope_kind="SKU",
            scope_key=f"SKU:{first_nm_id}",
            scope_label=str(first_nm_id),
            metric_key="stock_total",
            metric_label="Legacy stock",
            row_last_updated_at="2026-08-10T20:00:00Z",
            section="Остатки",
            group="fixture",
            nm_id=first_nm_id,
            format="integer",
            values_by_date={business_date: 10},
        ),
    ]
    projected = extend_rows_with_inventory_planning(
        source_rows,
        planning={"formula": {"version": "inventory_planning_v1"}},
        history=history,
        date_columns=[business_date],
        enabled_config=enabled_config,  # type: ignore[arg-type]
    )
    rows = {item.row_id: item for item in projected}
    total_combined = rows[
        "TOTAL|" + inventory_planning_total_metric_key(COMBINED_TOTAL_ALIAS_KEY)
    ]
    assert total_combined.values_by_date[business_date] == 30
    assert total_combined.presentation_by_date[business_date][
        "quality_state"
    ] == "inventory_history_partial"
    assert rows[
        "TOTAL|" + inventory_planning_total_metric_key(INVENTORY_WB_TOTAL_KEY)
    ].values_by_date[business_date] == 30
    assert rows[
        f"SKU:{first_nm_id}|{inventory_planning_facility_metric_key('moscow')}"
    ].values_by_date[business_date] == ""

    legacy_only = extend_rows_with_inventory_planning(
        source_rows,
        planning={"formula": {"version": "inventory_planning_v1"}},
        history={},
        date_columns=[business_date],
        enabled_config=enabled_config,  # type: ignore[arg-type]
    )
    legacy_rows = {item.row_id: item for item in legacy_only}
    assert legacy_rows[
        "TOTAL|" + inventory_planning_total_metric_key(INVENTORY_WB_TOTAL_KEY)
    ].values_by_date[business_date] == 30
    assert legacy_rows[
        "TOTAL|" + inventory_planning_total_metric_key(COMBINED_TOTAL_ALIAS_KEY)
    ].values_by_date[business_date] == ""


def _dates(date_from: str, date_to: str) -> list[str]:
    start = datetime.fromisoformat(date_from)
    end = datetime.fromisoformat(date_to)
    result: list[str] = []
    current = start
    while current <= end:
        result.append(current.date().isoformat())
        current = current.replace(day=current.day + 1)
    return result


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
