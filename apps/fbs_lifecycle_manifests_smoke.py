#!/usr/bin/env python3
"""General manifest-driven FBS mapping, impact and recovery smoke."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_fbs_lifecycle_quality_recovery as recovery_module  # noqa: E402
from apps import wbc0027_fbs_mapping_extension as mapping_module  # noqa: E402
from apps.ff_pool_fbs_forward_recovery_smoke import (  # noqa: E402
    SHA,
    _RecoveryClock,
    _insert_backlog,
    _prepared_runtime,
)
from apps.ff_pool_fbs_lifecycle_smoke import (  # noqa: E402
    _insert_post_t_order,
)
from apps.wbc0027_fbs_lifecycle_quality_recovery_smoke import (  # noqa: E402
    _Clock,
    _add_later_canonical_identity,
    _component,
    _insert_custom_order,
    _mapping_non_target_counts,
    _seed_mapping_owner,
    _seed_second_facility_and_balances,
)
from packages.application.fbs_lifecycle_manifests import (  # noqa: E402
    FbsManifestError,
    attach_digest,
    digest,
    mapping_tuple_digest,
    parse_incident_passport,
    parse_mapping_manifest,
)
from packages.application.ff_pool_fbs_forward_recovery import (  # noqa: E402
    FfPoolFbsForwardRecoveryMutation,
    _active_manifest,
)
from packages.application.ff_pool_fbs_lifecycle import (  # noqa: E402
    IDENTITY_PENDING_RESOLUTIONS_TABLE,
    IDENTITY_PENDING_TABLE,
    ensure_ff_pool_fbs_lifecycle_schema,
    process_post_t_fbs_lifecycle,
)
from packages.application.sheet_vitrina_v1_inventory_history import (  # noqa: E402
    COMPONENTS_TABLE,
    append_inventory_history_capture,
    append_inventory_history_finalization,
    ensure_inventory_history_schema,
)


def main() -> int:
    _strict_parser_negatives()
    with TemporaryDirectory(prefix="fbs-manifest-v2-") as raw:
        root = Path(raw)
        runtime = _prepared_runtime(root / "runtime")
        _insert_backlog(runtime.db_path)
        forward = FfPoolFbsForwardRecoveryMutation(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            timestamp_factory=_RecoveryClock(),
        )
        forward_plan = forward.build_plan()
        forward.apply(
            forward_plan,
            fingerprint=str(forward_plan["fingerprint"]),
            approval_reference="synthetic-forward-gate",
            actor="smoke",
            evidence_dir=root / "forward-evidence",
        )
        with sqlite3.connect(runtime.db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_ff_pool_fbs_lifecycle_schema(conn)
            ensure_inventory_history_schema(conn)
            _seed_second_facility_and_balances(conn)
            _insert_post_t_order(
                conn,
                order_id=9701,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T01:00:00Z",
                observed_at="2026-08-17T01:01:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=998,
                source_chrt_id=1998,
                seller_sku="seller-998",
                barcode="sku-998",
            )
            _insert_post_t_order(
                conn,
                order_id=9702,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T02:00:00Z",
                observed_at="2026-08-17T02:01:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=997,
                source_chrt_id=1997,
                seller_sku="seller-997",
                barcode="sku-997",
            )
            _insert_custom_order(
                conn,
                order_id=9703,
                warehouse_id=502,
                source_nm_id=996,
                source_chrt_id=1996,
                seller_sku="seller-996",
                barcode="sku-996",
                source_created_at="2026-08-17T03:00:00Z",
                observed_at="2026-08-17T03:01:00Z",
            )
            _insert_post_t_order(
                conn,
                order_id=9704,
                supplier="new",
                wb="waiting",
                source_created_at="2026-08-17T03:10:00Z",
                observed_at="2026-08-17T03:11:00Z",
                identity_outcome="unmatched_identity",
                source_nm_id=996,
                source_chrt_id=1996,
                seller_sku="seller-996",
                barcode="sku-996",
            )
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            processed = process_post_t_fbs_lifecycle(
                conn,
                occurred_at="2026-08-17T04:00:00Z",
                limit=100,
                schema_ready=True,
            )
            conn.commit()
            assert processed["summary"]["identity_pending"] == 4
            _add_later_canonical_identity(conn)
            _seed_mapping_owner(conn)
            _seed_history_with_four_unsupported_cells(conn)
            source_cursor_max = int(
                conn.execute(
                    "SELECT MAX(source_status_observation_sequence) "
                    f"FROM {IDENTITY_PENDING_TABLE}"
                ).fetchone()[0]
            )
            active = _active_manifest(conn)
            conn.commit()
        storage_probe = recovery_module.Wbc0027FbsLifecycleQualityRecovery.__new__(
            recovery_module.Wbc0027FbsLifecycleQualityRecovery
        )
        del storage_probe
        provisional = _passport(
            runtime=runtime,
            active=active,
            source_cursor_max=source_cursor_max,
        )
        mapping_runner = mapping_module.Wbc0027ExactFbsSkuMappingExtension(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            incident_passport=provisional,
            timestamp_factory=_Clock(),
        )
        mapping_plan = mapping_runner.build_plan()
        assert mapping_plan["contract"] == "fbs_identity_mapping_manifest/v2"
        assert mapping_plan["apply_allowed"] is True, mapping_plan["blockers"]
        assert set(mapping_plan["storage"]) == {
            "manifest_sha256",
            "operational_generation_id",
            "operational_schema_revision",
            "sqlite_schema_version",
        }
        _assert_mapping_manifest_has_no_lifecycle_scope(mapping_plan)
        rehearsal = mapping_runner.rehearse()
        assert rehearsal["accepted"] is True, {
            "blockers": rehearsal["blockers"],
            "matrix": rehearsal.get("matrix"),
        }
        assert rehearsal["mapping_insert_count"] == 0
        assert rehearsal["recovery_write_count"] == 0
        assert rehearsal["history_write_count"] == 0
        impact = rehearsal["impact_manifest"]
        recovery = rehearsal["recovery_manifest"]
        assert impact["unresolved_scan"]["full_scan"] is True
        assert len(recovery["scope"]["groups"]) == 4
        assert recovery["history"]["classification_counts"][
            "remain_missing_no_same_date_evidence"
        ] == 4
        assert recovery["history"]["blockers"] == []
        before = _mapping_non_target_counts(runtime.db_path)
        evidence_dir = root / "mapping-evidence"
        evidence_dir.mkdir(mode=0o700)
        mapping_result = mapping_runner.apply(
            mapping_plan,
            fingerprint=str(mapping_plan["manifest_digest"]),
            approval_reference="synthetic-mapping-gate",
            actor="smoke",
            evidence_dir=evidence_dir,
        )
        assert mapping_result["mapping_insert_count"] == 1
        assert _mapping_non_target_counts(runtime.db_path) == before
        readback = mapping_runner.readback()
        assert readback["status"] == "completed"
        assert readback["query_only"] is True
        recovery_runner = recovery_module.Wbc0027FbsLifecycleQualityRecovery(
            runtime_dir=runtime.runtime_dir,
            deployed_sha=SHA,
            incident_passport=provisional,
            timestamp_factory=_Clock(),
        )
        impact_after, recovery_after = recovery_runner.build_manifests(
            mapping_readback_digest=str(readback["readback_digest"])
        )
        assert impact_after["mapping_readback_digest"] == readback["readback_digest"]
        assert recovery_after["impact_digest"] == impact_after["impact_digest"]
        assert recovery_after["apply_allowed"] is True, recovery_after["blockers"]
        assert recovery_after["history"]["classification_counts"][
            "remain_missing_no_same_date_evidence"
        ] == 4
        result = recovery_runner.apply(
            recovery_after,
            fingerprint=str(recovery_after["recovery_digest"]),
            approval_reference="synthetic-recovery-gate",
            actor="smoke",
            evidence_dir=root / "recovery-evidence",
        )
        assert result["status"] == "completed"
        receipt = recovery_runner.readback(
            fingerprint=str(recovery_after["recovery_digest"])
        )
        assert receipt["target_count"] == receipt["target_readback_count"] == 5
        with sqlite3.connect(runtime.db_path) as conn:
            unresolved = int(
                conn.execute(
                    f"""SELECT COUNT(*) FROM {IDENTITY_PENDING_TABLE} pending
                        LEFT JOIN {IDENTITY_PENDING_RESOLUTIONS_TABLE} resolution
                          ON resolution.pending_id=pending.pending_id
                        WHERE resolution.pending_id IS NULL"""
                ).fetchone()[0]
            )
        assert unresolved == 0
    print("fbs_lifecycle_manifests_smoke: OK")
    return 0


def _passport(*, runtime: object, active: dict[str, object], source_cursor_max: int) -> dict[str, object]:
    probe = recovery_module.Wbc0027FbsLifecycleQualityRecovery(
        runtime_dir=runtime.runtime_dir,  # type: ignore[attr-defined]
        deployed_sha=SHA,
        incident_passport=_bootstrap_passport(active, source_cursor_max),
        timestamp_factory=_Clock(),
    )
    storage = probe._storage_identity()
    passport = _bootstrap_passport(active, source_cursor_max)
    passport["storage"] = {
        key: storage[key]
        for key in (
            "manifest_sha256",
            "operational_generation_id",
            "operational_schema_revision",
            "sqlite_schema_version",
        )
    }
    with sqlite3.connect(runtime.db_path) as conn:  # type: ignore[attr-defined]
        generation_id = str(
            conn.execute(
                "SELECT generation_id FROM sheet_vitrina_v1_ff_pool_fbs_forward_generations "
                "WHERE cutover_id=?",
                (str(active["cutover_id"]),),
            ).fetchone()[0]
        )
    passport["cutover"]["forward_generation_id"] = generation_id
    return parse_incident_passport(passport)


def _bootstrap_passport(
    active: dict[str, object], source_cursor_max: int
) -> dict[str, object]:
    tuple_value = {
        "tuple_contract": "synthetic_fbs_identity_tuple/v1",
        "source_nm_id": 996,
        "source_chrt_id": 1996,
        "source_barcode": "sku-996",
        "source_sku": "seller-996",
        "target_nm_id": 103,
    }
    tuple_value["tuple_digest"] = mapping_tuple_digest(tuple_value)
    return {
        "contract": "fbs_lifecycle_incident_passport/v1",
        "operation_id": "synthetic-fbs-manifest-smoke-v2",
        "target": {
            "target_id": mapping_module.CANONICAL_TARGET_ID,
            "source_runtime_sha": SHA,
            "release_runtime_contract": "exact_release_runtime",
        },
        "storage": {
            "manifest_sha256": "sha256:" + "0" * 64,
            "operational_generation_id": "bootstrap",
            "operational_schema_revision": "bootstrap_v1",
            "sqlite_schema_version": 1,
        },
        "cutover": {
            "cutover_id": str(active["cutover_id"]),
            "forward_generation_id": "fbs-forward-generation-v1",
            "source_cursor_max": source_cursor_max,
        },
        "tuple": tuple_value,
        "evidence": {"external_identity_digest": "sha256:" + "c" * 64},
        "mapping_expectation": {
            "owner_count": 1,
            "active_mapping_count": 0,
            "all_mapping_count": 0,
            "insert_count": 1,
        },
        "rehearsal_snapshot_digest": "sha256:" + "d" * 64,
    }


def _seed_history_with_four_unsupported_cells(conn: sqlite3.Connection) -> None:
    roster = [
        {"facility_id": "fac_moscow", "code": "MSK", "name": "FF Москва", "active": True, "applicable": True, "effective_from": "2026-08-14", "display_order": 1},
        {"facility_id": "fac_orenburg", "code": "ORE", "name": "FF Оренбург", "active": True, "applicable": True, "effective_from": "2026-08-17", "display_order": 2},
    ]
    for day in range(14, 32):
        business_date = f"2026-08-{day:02d}"
        components: list[dict[str, object]] = []
        for scope_key, nm_id in (("TOTAL", None), ("SKU:101", 101), ("SKU:102", 102), ("SKU:103", 103)):
            scope_kind = "TOTAL" if nm_id is None else "SKU"
            components.append(_component(scope_kind, scope_key, nm_id, "WB", "WB", "WB", 50))
            for facility_id, label in (("fac_moscow", "FF Москва"), ("fac_orenburg", "FF Оренбург")):
                component = _component(
                    scope_kind,
                    scope_key,
                    nm_id,
                    "FBS_FACILITY",
                    facility_id,
                    label,
                    300 if nm_id is None else 100,
                )
                if day in {17, 18} and nm_id == 103:
                    component["state"] = "missing"
                    component["quantity"] = None
                components.append(component)
        capture = append_inventory_history_capture(
            conn,
            business_date=business_date,
            capture_kind="historical_backfill",
            formula_version="inventory_planning_v1",
            facility_roster=roster,
            source_manifest={"contract": "synthetic_exact_same_date", "date": business_date},
            components=components,
            captured_at=f"{business_date}T20:00:00Z",
        )
        if day < 31:
            append_inventory_history_finalization(
                conn,
                business_date=business_date,
                capture_id=str(capture["capture_id"]),
                finalization_identity=f"synthetic:{business_date}",
                finalized_at=f"{business_date}T21:00:00Z",
                provenance={"source": "smoke"},
            )


def _assert_mapping_manifest_has_no_lifecycle_scope(value: object) -> None:
    forbidden = {"orders", "statuses", "groups", "dates", "date_from", "date_to"}

    def walk(item: object) -> None:
        if isinstance(item, dict):
            assert forbidden.isdisjoint(item)
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)

    walk(value)


def _strict_parser_negatives() -> None:
    tuple_value = {
        "tuple_contract": "synthetic/v1",
        "source_nm_id": 1,
        "source_chrt_id": 2,
        "source_barcode": "3",
        "source_sku": "sku",
        "target_nm_id": 4,
    }
    tuple_value["tuple_digest"] = mapping_tuple_digest(tuple_value)
    passport = _bootstrap_passport({"cutover_id": "cutover"}, 1)
    parse_incident_passport(passport)
    unknown = copy.deepcopy(passport)
    unknown["unknown"] = True
    _expect_error("invalid_fields", lambda: parse_incident_passport(unknown))
    bad_tuple = copy.deepcopy(passport)
    bad_tuple["tuple"]["tuple_digest"] = "sha256:" + "0" * 64
    _expect_error("tuple_digest_mismatch", lambda: parse_incident_passport(bad_tuple))
    mapping = {
        "contract": "fbs_identity_mapping_manifest/v2",
        "operation_id": "synthetic",
        "target": {"target_id": "target", "runtime_sha": SHA, "source_runtime_sha": SHA},
        "storage": {
            "manifest_sha256": "sha256:" + "1" * 64,
            "operational_generation_id": "generation",
            "operational_schema_revision": "operational_v1",
            "sqlite_schema_version": 1,
        },
        "cutover": {
            "cutover_id": "cutover",
            "cutover_manifest_digest": "sha256:" + "2" * 64,
            "forward_generation_id": "forward",
            "forward_generation_manifest_digest": "sha256:" + "3" * 64,
        },
        "tuple": tuple_value,
        "evidence": {
            "external_identity_digest": "sha256:" + "4" * 64,
            "owner_digest": "sha256:" + "5" * 64,
            "warehouse_evidence_digest": "sha256:" + "6" * 64,
            "facility_admission_digest": "sha256:" + "7" * 64,
        },
        "expectation": {"owner_count": 1, "active_mapping_count": 0, "all_mapping_count": 0, "insert_count": 1},
        "proposed_mapping": {"mapping_id": "mapping", "mapping_digest": tuple_value["tuple_digest"]},
        "material_cas": {"component": "value"},
        "safety": {},
        "apply_allowed": True,
        "blockers": [],
    }
    mapping["material_cas"] = attach_digest(mapping["material_cas"], "digest")
    mapping = attach_digest(mapping, "manifest_digest")
    parse_mapping_manifest(mapping)
    forbidden = copy.deepcopy(mapping)
    forbidden["safety"]["groups"] = []
    forbidden["manifest_digest"] = digest(
        {key: raw for key, raw in forbidden.items() if key != "manifest_digest"}
    )
    _expect_error(
        "mapping_scope_field_forbidden", lambda: parse_mapping_manifest(forbidden)
    )


def _expect_error(code: str, fn: object) -> None:
    try:
        fn()  # type: ignore[operator]
    except FbsManifestError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected {code}")


if __name__ == "__main__":
    raise SystemExit(main())
