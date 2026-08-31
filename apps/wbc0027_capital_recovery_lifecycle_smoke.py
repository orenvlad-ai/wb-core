#!/usr/bin/env python3
"""Regression coverage for WBC0027 post-COMMIT lifecycle reconciliation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import wbc0027_capital_recovery as recovery  # noqa: E402
from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryPolicyError,
    WarehouseRecoveryRegistry,
    recovery_operation_id,
)


OLD_SHA = "8" * 40
NEW_SHA = "9" * 40
GENERATION = {
    "generation_id": "operational-regression-generation",
    "manifest_sha256": "sha256:" + "a" * 64,
    "schema_revision": "operational_v1",
}


class RuntimeFixture:
    def __init__(self, *, runtime_dir: Path) -> None:
        self.runtime_dir = runtime_dir.resolve()
        self.db_path = self.runtime_dir / "registry_upload_runtime.sqlite3"


def _payload(*, day: str, target: str, ordinary: str) -> dict:
    return {
        "date_columns": [day],
        "sheets": [
            {
                "sheet_name": "DATA_VITRINA",
                "header": ["metric", "row_id", day],
                "rows": [
                    ["cost", "428853741|our_wb_unit_cost_rub", target],
                    ["stock", "428853741|warehouse_stock_qty", "stable"],
                ],
            }
        ],
        "metadata": {"ordinary_publication": ordinary},
    }


def _fixture(root: Path, *, goal_suffix: str) -> tuple[RuntimeFixture, dict]:
    runtime = RuntimeFixture(runtime_dir=root)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE sheet_vitrina_v1_ready_snapshots("
            "bundle_version TEXT NOT NULL,as_of_date TEXT NOT NULL,"
            "snapshot_id TEXT NOT NULL,plan_json TEXT NOT NULL,"
            "refreshed_at TEXT NOT NULL DEFAULT '',"
            "PRIMARY KEY(bundle_version,as_of_date,snapshot_id))"
        )
        patches = []
        source = recovery.LEGACY_SOURCE_TRANSACTION_BINDING
        for index, day in enumerate(("2026-08-26", "2026-08-26", "2026-08-29")):
            identity = list(source["target_identities"][index])
            before = _payload(day=day, target="", ordinary=f"target-{index}")
            after = _payload(day=day, target=f"117.53716{index + 7}", ordinary=f"target-{index}")
            before_json = recovery._json(before)
            after_json = recovery._json(after)
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_ready_snapshots("
                "bundle_version,as_of_date,snapshot_id,plan_json) VALUES(?,?,?,?)",
                (*identity, before_json),
            )
            cell_count = (174, 174, 124)[index]
            patches.append(
                {
                    "identity": identity,
                    "business_dates": [day],
                    "changed_cells": [f"{day}|cell-{value}" for value in range(cell_count)],
                    "before_plan_json": before_json,
                    "after_plan_json": after_json,
                    "before_sha256": recovery._sha_text(before_json),
                    "after_sha256": recovery._sha_text(after_json),
                }
            )
        for index in range(221):
            payload = _payload(
                day="2026-08-30", target="stable", ordinary=f"ordinary-{index}"
            )
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_ready_snapshots("
                "bundle_version,as_of_date,snapshot_id,plan_json) VALUES(?,?,?,?)",
                (
                    "bundle",
                    "2026-08-24",
                    f"ordinary-{index:03d}",
                    recovery._json(payload),
                ),
            )
        conn.commit()
        rows = conn.execute(
            "SELECT bundle_version,as_of_date,snapshot_id,plan_json "
            "FROM sheet_vitrina_v1_ready_snapshots ORDER BY bundle_version,as_of_date,snapshot_id"
        ).fetchall()
    economics = {
        "target_dates": list(recovery.ECONOMICS_DATES),
        "logical_repair_count": recovery.EXPECTED_ECONOMICS_LOGICAL_REPAIRS,
        "persisted_repair_count": recovery.EXPECTED_ECONOMICS_PERSISTED_REPAIRS,
        "patch_count": 3,
        "source_operation_id": recovery.SOURCE_OPERATION_ID,
        "source_digest": recovery.SOURCE_OPERATION_DIGEST,
        "protected_invariant": {
            "as_of_date": "2026-08-26",
            "nm_id": 428853741,
            "unit_cost_rub": "117.537167",
            "status": "separate_exact_invariant_preserved",
        },
        "evidence_blocked": [f"2026-08-26|blocked-{index}" for index in range(12)],
        "patches": patches,
        "before_digest": recovery._digest([item["before_sha256"] for item in patches]),
        "after_digest": recovery._digest([item["after_sha256"] for item in patches]),
    }
    semantic_patches = recovery._economics_semantic_patches(economics)
    economics["semantic_non_target"] = recovery._economics_semantic_non_target_snapshot(
        rows, semantic_patches
    )
    economics["non_target_digest"] = recovery._raw_unpatched_ready_digest(
        rows, {tuple(item["identity"]) for item in patches}
    )
    product_fingerprint = recovery._digest({"product": goal_suffix})
    product_operation = recovery_operation_id(
        recovery.MUTATION_KIND_PRODUCT, product_fingerprint
    )
    material = recovery._economics_material(
        economics, product_phase_operation_id=product_operation
    )
    goal = "production-goal-v1-" + goal_suffix * 32
    candidate = recovery._phase_envelope(
        phase="economics",
        goal_operation_id=goal,
        deployed_sha=OLD_SHA,
        generation=GENERATION,
        material=material,
    )
    candidate.update(
        {
            "created_at": "2026-08-31T00:00:00+00:00",
            "functional_economics": economics,
            "product_predecessor": {
                "operation_id": product_operation,
                "reconciled": True,
            },
            "_fixture_product_fingerprint": product_fingerprint,
        }
    )
    return runtime, candidate


def _target_json(runtime: RuntimeFixture, candidate: dict) -> list[str]:
    with sqlite3.connect(runtime.db_path) as conn:
        return [
            str(
                conn.execute(
                    "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    tuple(patch["identity"]),
                ).fetchone()[0]
            )
            for patch in candidate["functional_economics"]["patches"]
        ]


def _exercise_post_commit_truth() -> None:
    with tempfile.TemporaryDirectory(prefix="wbc0027-commit-truth-") as raw:
        runtime, candidate = _fixture(Path(raw), goal_suffix="b")
        original_ensure = recovery.ensure_warehouse_business_projection_schema
        original_materialize = recovery.materialize_warehouse_business_projection_reconciliation
        original_retain = WarehouseRecoveryRegistry.retain
        recovery.ensure_warehouse_business_projection_schema = lambda _conn: None
        recovery.materialize_warehouse_business_projection_reconciliation = (
            lambda _conn, **_kwargs: None
        )

        def fail_after_commit(self: WarehouseRecoveryRegistry, *_args: object, **_kwargs: object) -> dict:
            raise RuntimeError("injected failure after committed business transaction")

        WarehouseRecoveryRegistry.retain = fail_after_commit
        try:
            try:
                recovery._t1_economics(runtime, candidate)
            except RuntimeError as exc:
                assert "after committed" in str(exc)
            else:
                raise AssertionError("post-COMMIT exception was not injected")
            truth = recovery._submission_truth(runtime, candidate["phase_operation_id"])
            assert truth == {
                "status": "applied_pending_reconciliation",
                "database_written": True,
                "submit_count": 1,
                "recovery_lifecycle": "mutation_running",
            }
            operation = WarehouseRecoveryRegistry(
                runtime_dir=runtime.runtime_dir, db_path=runtime.db_path
            ).get_operation(candidate["phase_operation_id"])
            assert operation["writer_state"] == "committed_pending_reconciliation"
            assert operation["after_digest"] == candidate["functional_economics"]["after_digest"]
            assert _target_json(runtime, candidate) == [
                item["after_plan_json"] for item in candidate["functional_economics"]["patches"]
            ]
        finally:
            WarehouseRecoveryRegistry.retain = original_retain
            recovery.ensure_warehouse_business_projection_schema = original_ensure
            recovery.materialize_warehouse_business_projection_reconciliation = original_materialize


def _exercise_genuine_non_target_drift() -> None:
    with tempfile.TemporaryDirectory(prefix="wbc0027-nontarget-drift-") as raw:
        runtime, candidate = _fixture(Path(raw), goal_suffix="c")
        before_targets = _target_json(runtime, candidate)
        with sqlite3.connect(runtime.db_path) as conn:
            row = conn.execute(
                "SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots "
                "WHERE snapshot_id='ordinary-000'"
            ).fetchone()
            payload = json.loads(str(row[0]))
            payload["metadata"]["ordinary_publication"] = "concurrent-genuine-change"
            conn.execute(
                "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                "WHERE snapshot_id='ordinary-000'",
                (recovery._json(payload),),
            )
            conn.commit()
        original_ensure = recovery.ensure_warehouse_business_projection_schema
        original_materialize = recovery.materialize_warehouse_business_projection_reconciliation
        recovery.ensure_warehouse_business_projection_schema = lambda _conn: None
        recovery.materialize_warehouse_business_projection_reconciliation = (
            lambda _conn, **_kwargs: None
        )
        try:
            try:
                recovery._t1_economics(runtime, candidate)
            except recovery.Wbc0027RecoveryError as exc:
                assert "semantic non-target changed before submit" in str(exc)
            else:
                raise AssertionError("genuine concurrent non-target change was accepted")
            assert _target_json(runtime, candidate) == before_targets
            operation = WarehouseRecoveryRegistry(
                runtime_dir=runtime.runtime_dir, db_path=runtime.db_path
            ).get_operation(candidate["phase_operation_id"])
            assert operation["after_digest"] == ""
        finally:
            recovery.ensure_warehouse_business_projection_schema = original_ensure
            recovery.materialize_warehouse_business_projection_reconciliation = original_materialize


def _prepare_retained_product(
    runtime: RuntimeFixture, product_operation: str, product_fingerprint: str
) -> None:
    registry = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path)
    fingerprint = product_fingerprint
    assert recovery_operation_id(recovery.MUTATION_KIND_PRODUCT, fingerprint) == product_operation
    row = {"table": "fixture", "key": {"slot": 1}, "before": {"value": 1}, "after": {"value": 2}}
    operation = registry.prepare_t1(
        mutation_kind=recovery.MUTATION_KIND_PRODUCT,
        closure_kind="sku_date",
        plan_fingerprint=fingerprint,
        scope={"fixture": "product"},
        before_images=[row],
        expected_after_images=[row["after"]],
        source_digest=recovery._digest({"source": "product"}),
        non_target_digest=recovery._digest({"non_target": "product"}),
    )
    assert operation["operation_id"] == product_operation
    registry.begin_mutation(product_operation)
    registry.retain(
        product_operation,
        after_digest=recovery._digest({"after": "product"}),
        non_target_digest=recovery._digest({"non_target": "product"}),
    )


def _exercise_false_quarantine_finalize() -> None:
    with tempfile.TemporaryDirectory(prefix="wbc0027-finalize-") as raw:
        root = Path(raw)
        runtime, candidate = _fixture(root, goal_suffix="d")
        economics = candidate["functional_economics"]
        registry = WarehouseRecoveryRegistry(runtime_dir=runtime.runtime_dir, db_path=runtime.db_path)
        _prepare_retained_product(
            runtime,
            candidate["material"]["product_phase_operation_id"],
            candidate["_fixture_product_fingerprint"],
        )
        before_images = [
            {
                "table": "sheet_vitrina_v1_ready_snapshots",
                "key": {
                    "bundle_version": patch["identity"][0],
                    "as_of_date": patch["identity"][1],
                    "snapshot_id": patch["identity"][2],
                },
                "before": {
                    "bundle_version": patch["identity"][0],
                    "as_of_date": patch["identity"][1],
                    "snapshot_id": patch["identity"][2],
                    "plan_json": patch["before_plan_json"],
                },
                "after": {
                    "bundle_version": patch["identity"][0],
                    "as_of_date": patch["identity"][1],
                    "snapshot_id": patch["identity"][2],
                    "plan_json": patch["after_plan_json"],
                },
            }
            for patch in economics["patches"]
        ]
        operation = registry.prepare_t1(
            mutation_kind=recovery.MUTATION_KIND_ECONOMICS,
            closure_kind="sku_date",
            plan_fingerprint=candidate["phase_fingerprint"],
            scope={
                "dates": economics["target_dates"],
                "logical_repair_count": economics["logical_repair_count"],
                "source_operation_id": recovery.SOURCE_OPERATION_ID,
                "profile": recovery.PROFILE,
                "goal_operation_id": candidate["goal_operation_id"],
                "phase": "economics",
                "product_phase_operation_id": candidate["material"]["product_phase_operation_id"],
            },
            before_images=before_images,
            expected_after_images=[item["after"] for item in before_images],
            source_digest=candidate["material_qualification_digest"],
            non_target_digest=economics["non_target_digest"],
        )
        assert operation["operation_id"] == candidate["phase_operation_id"]
        registry.begin_mutation(candidate["phase_operation_id"])
        with sqlite3.connect(runtime.db_path) as conn:
            for patch in economics["patches"]:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    (patch["after_plan_json"], *patch["identity"]),
                )
            conn.commit()
        try:
            registry.retain(
                candidate["phase_operation_id"],
                after_digest=economics["after_digest"],
                non_target_digest=economics["semantic_non_target"]["digest"],
            )
        except RecoveryPolicyError as exc:
            assert "non-target digest changed" in str(exc)
        else:
            raise AssertionError("legacy raw/semantic mismatch did not quarantine")
        quarantined = registry.get_operation(candidate["phase_operation_id"])
        assert quarantined["lifecycle"] == "quarantined"
        assert quarantined["quarantine_reason"] == "non_target_digest_drift_after_mutation"
        assert _target_json(runtime, candidate) == [
            item["after_plan_json"] for item in economics["patches"]
        ]
        del economics["semantic_non_target"]
        del candidate["material"]["semantic_non_target_contract"]
        assert "semantic_non_target" not in economics
        assert "semantic_non_target_contract" not in candidate["material"]
        try:
            recovery._economics_material(
                economics,
                product_phase_operation_id=candidate["material"][
                    "product_phase_operation_id"
                ],
            )
        except recovery.Wbc0027RecoveryError as exc:
            assert "canonical semantic non-target witness is invalid" in str(exc)
        else:
            raise AssertionError("future Apply accepted the legacy source shape")
        with sqlite3.connect(runtime.db_path) as conn:
            later = _payload(
                day="2026-08-30",
                target="stable",
                ordinary="ordinary-publication-2026-08-30T01:39:50Z",
            )
            later["metadata"]["refreshed_at"] = "2026-08-30T01:39:50Z"
            conn.execute(
                "INSERT INTO sheet_vitrina_v1_ready_snapshots("
                "bundle_version,as_of_date,snapshot_id,plan_json,refreshed_at) "
                "VALUES(?,?,?,?,?)",
                (
                    "bundle",
                    "2026-08-30",
                    "ordinary-future",
                    recovery._json(later),
                    "2026-08-30T01:39:50Z",
                ),
            )
            conn.commit()

        evidence_dir = root / "production-goals" / candidate["goal_operation_id"]
        evidence_dir.mkdir(parents=True, mode=0o700)
        evidence_dir.chmod(0o700)
        manifest = evidence_dir / "wbc0027-economics-plan-20260831T000000000000Z-regression.json"
        manifest.write_bytes(recovery._json(candidate).encode("utf-8"))
        manifest.chmod(0o600)
        deployed_file = root / ".wb-core-runtime-sha"
        deployed_file.write_text(NEW_SHA + "\n", encoding="utf-8")

        original_runtime = recovery.RegistryUploadDbBackedRuntime
        original_generation = recovery._generation
        original_product = recovery.reconcile_warehouse_business_projection
        original_hard = recovery._hard_non_target_semantics
        original_target_cells = recovery._target_cells
        original_validate_candidate = recovery._validate_candidate
        original_source_binding = recovery.LEGACY_SOURCE_TRANSACTION_BINDING
        recovery.RegistryUploadDbBackedRuntime = RuntimeFixture
        recovery._generation = lambda _runtime_dir: dict(GENERATION)
        recovery.reconcile_warehouse_business_projection = lambda *_args, **_kwargs: {
            "status": "published_exact",
            "mismatch_count": 0,
            "scope_count": 1152,
            "cell_count": 24192,
        }
        recovery._hard_non_target_semantics = lambda _conn: {
            "from_date": recovery.HARD_NON_TARGET_DATE,
            "all_exact": True,
            "observed_date_count": 2,
        }

        def production_shape(payload: dict, day: str) -> dict[str, object]:
            if day not in payload.get("date_columns", []):
                return {}
            actual = original_target_cells(payload, day)
            if day == "2026-08-26":
                return {**actual, **{f"blocked-{index}": "" for index in range(12)}}
            return {**actual, **{f"exact-{index}": "1" for index in range(12)}}

        recovery._target_cells = production_shape
        source_patches = economics["patches"]
        target_removed = tuple(
            recovery._digest(
                recovery._strip_economics_targets(
                    json.loads(str(patch["before_plan_json"])),
                    list(patch["business_dates"]),
                )
            )
            for patch in source_patches
        )
        fixture_source = {
            **original_source_binding,
            "goal_operation_id": candidate["goal_operation_id"],
            "product_phase_operation_id": candidate["material"][
                "product_phase_operation_id"
            ],
            "economics_phase_operation_id": candidate["phase_operation_id"],
            "source_deployed_sha": OLD_SHA,
            "manifest_sha256": recovery._file_digest(manifest),
            "phase_fingerprint": candidate["phase_fingerprint"],
            "storage_generation": dict(GENERATION),
            "source_raw_non_target_digest": economics["non_target_digest"],
            "target_identities": tuple(
                tuple(patch["identity"]) for patch in source_patches
            ),
            "target_before_hashes": tuple(
                patch["before_sha256"] for patch in source_patches
            ),
            "target_before_digest": economics["before_digest"],
            "target_after_hashes": tuple(
                patch["after_sha256"] for patch in source_patches
            ),
            "target_after_digest": economics["after_digest"],
            "target_removed_digests": target_removed,
            "target_changed_cell_counts": (174, 174, 124),
        }
        recovery.LEGACY_SOURCE_TRANSACTION_BINDING = fixture_source
        recovery._validate_candidate = lambda *_args, **_kwargs: None
        try:
            db_digest_before = hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
            kwargs = {
                "runtime_dir": root,
                "deployed_sha_file": deployed_file,
                "expected_sha": NEW_SHA,
                "goal_operation_id": candidate["goal_operation_id"],
                "source_deployed_sha": OLD_SHA,
                "source_manifest_path": manifest,
                "source_manifest_sha256": recovery._file_digest(manifest),
                "source_phase_operation_id": candidate["phase_operation_id"],
                "source_phase_fingerprint": candidate["phase_fingerprint"],
                "source_storage_generation": GENERATION,
                "source_run_id": 33345644125,
                "source_artifact_id": 9741910399,
                "source_artifact_name": "production-apply-receipt-pr-1129-run-33345644125",
                "source_receipt_sha256": fixture_source["receipt_sha256"],
                "source_comment_id": 5472359912,
                "reconciliation_pr": 1130,
                "reconciliation_release_operation_id": "release-v2-" + "e" * 32,
                "authorization_reference": (
                    "github:orenvlad-ai/wb-core:pr:1129:comment:5472278622:sha256:"
                    + fixture_source["authorization_body_sha256"]
                ),
            }
            first = recovery.finalize_existing_economics_operation(**kwargs)
            second = recovery.finalize_existing_economics_operation(**kwargs)
            assert first == second
            assert first["status"] == "reconciled_existing_operation"
            assert first["qualification_status"] == "qualified"
            assert first["repeat_disposition"] == "already_qualifiable"
            assert first["query_only"] is True
            assert first["database_written"] is False
            assert first["production_mutation_count"] == 0
            assert first["product_replay_count"] == 0
            assert first["economics_replay_count"] == 0
            assert first["undo_row_count"] == 3
            source = first["source_transaction"]
            drift = first["temporal_non_target_drift"]
            assert source["source_ready_row_count"] == 224
            assert source["source_raw_non_target"] == {
                "contract_name": "wbc0027_legacy_raw_non_target_aggregate/v1",
                "row_count": 221,
                "digest": economics["non_target_digest"],
                "binding": "exact_source_manifest_and_recovery_row",
            }
            assert source["source_semantic_components_reconstructable"] is False
            assert source["source_adapter_rehearsal_digest"] == (
                "sha256:3598233834edfdc236bff126dfd9a25f432d36e44a1ed97abad9123d079cf4aa"
            )
            assert source["write_set"] == {
                "row_count": 3,
                "cell_count": 472,
                "undo_row_count": 3,
                "undo_rows_verified": True,
                "undo_artifact_verified": True,
                "expected_after_image_count": 3,
            }
            assert all(
                row["target_removed_before_digest"]
                == row["target_removed_planned_after_digest"]
                for row in source["target_rows"]
            )
            assert drift["changed"] is True
            assert drift["source_ready_row_count"] == 224
            assert drift["current_ready_row_count"] == 225
            assert drift["source_raw_non_target_row_count"] == 221
            assert drift["current_raw_non_target_row_count"] == 222
            assert drift["source_semantic_components_available"] is False
            assert drift["source_semantic_reconstruction_permitted"] is False
            assert drift["equality_gate"] is False
            assert drift["classification"] == "later_non_target_evolution"
            assert drift["diff_derivation"] == "unique_added_row_from_source_raw_aggregate"
            assert drift["derived_added_rows"][0]["identity"][-1] == "ordinary-future"
            assert drift["observed_late_ordinary_rows"] == [
                {
                    "identity": ["bundle", "2026-08-30", "ordinary-future"],
                    "plan_sha256": recovery._sha_text(recovery._json(later)),
                    "refreshed_at": "2026-08-30T01:39:50Z",
                }
            ]
            assert drift["effect"] == "receipt_evidence_only_not_target_approval"

            def assert_binding_blocked(**overrides: object) -> None:
                changed = {**kwargs, **overrides}
                try:
                    recovery.finalize_existing_economics_operation(**changed)
                except recovery.Wbc0027RecoveryError as exc:
                    assert "binding drifted" in str(exc)
                else:
                    raise AssertionError(f"legacy binding accepted {sorted(overrides)}")

            assert_binding_blocked(source_receipt_sha256="sha256:" + "0" * 64)
            assert_binding_blocked(source_deployed_sha="0" * 40)
            assert_binding_blocked(source_manifest_sha256="sha256:" + "0" * 64)
            assert_binding_blocked(
                source_storage_generation={**GENERATION, "schema_revision": "wrong"}
            )
            assert_binding_blocked(goal_operation_id="production-goal-v1-" + "0" * 32)
            assert_binding_blocked(source_phase_operation_id="recovery_" + "0" * 32)

            manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
            for field in ("before_sha256", "after_sha256"):
                changed_manifest = json.loads(json.dumps(manifest_value))
                changed_manifest["functional_economics"]["patches"][0][field] = (
                    "sha256:" + "0" * 64
                )
                manifest.write_bytes(recovery._json(changed_manifest).encode("utf-8"))
                changed_digest = recovery._file_digest(manifest)
                recovery.LEGACY_SOURCE_TRANSACTION_BINDING = {
                    **fixture_source,
                    "manifest_sha256": changed_digest,
                }
                try:
                    recovery.finalize_existing_economics_operation(
                        **{
                            **kwargs,
                            "source_manifest_sha256": changed_digest,
                        }
                    )
                except recovery.Wbc0027RecoveryError as exc:
                    assert "before/planned-after equality drifted" in str(exc)
                else:
                    raise AssertionError(f"source target {field} mismatch was accepted")
            manifest.write_bytes(recovery._json(manifest_value).encode("utf-8"))
            recovery.LEGACY_SOURCE_TRANSACTION_BINDING = fixture_source
            assert (
                hashlib.sha256(runtime.db_path.read_bytes()).hexdigest()
                == db_digest_before
            )
            after = registry.get_operation(candidate["phase_operation_id"])
            assert after == quarantined

            with sqlite3.connect(runtime.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    "UPDATE sheet_vitrina_v1_recovery_operations "
                    "SET quarantine_reason='wrong' WHERE operation_id=?",
                    (candidate["phase_operation_id"],),
                )
                conn.commit()
            try:
                recovery.finalize_existing_economics_operation(**kwargs)
            except recovery.Wbc0027RecoveryError as exc:
                assert "quarantine reason drifted" in str(exc)
            else:
                raise AssertionError("wrong quarantine reason was accepted")
            with sqlite3.connect(runtime.db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute(
                    "UPDATE sheet_vitrina_v1_recovery_operations "
                    "SET quarantine_reason='non_target_digest_drift_after_mutation' "
                    "WHERE operation_id=?",
                    (candidate["phase_operation_id"],),
                )
                deleted = conn.execute(
                    "SELECT * FROM sheet_vitrina_v1_recovery_undo_rows "
                    "WHERE operation_id=? ORDER BY sequence_no DESC LIMIT 1",
                    (candidate["phase_operation_id"],),
                ).fetchone()
                columns = [
                    item[1]
                    for item in conn.execute(
                        "PRAGMA table_info(sheet_vitrina_v1_recovery_undo_rows)"
                    )
                ]
                conn.execute(
                    "DELETE FROM sheet_vitrina_v1_recovery_undo_rows "
                    "WHERE operation_id=? AND sequence_no=?",
                    (candidate["phase_operation_id"], deleted["sequence_no"]),
                )
                conn.commit()
            try:
                recovery.finalize_existing_economics_operation(**kwargs)
            except recovery.Wbc0027RecoveryError as exc:
                assert "undo row count drifted" in str(exc)
            else:
                raise AssertionError("missing undo row was accepted")
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    "INSERT INTO sheet_vitrina_v1_recovery_undo_rows("
                    + ",".join(columns)
                    + ") VALUES("
                    + ",".join("?" for _ in columns)
                    + ")",
                    tuple(deleted[column] for column in columns),
                )
                conn.commit()

            target_patch = economics["patches"][0]
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    (target_patch["before_plan_json"], *target_patch["identity"]),
                )
                conn.commit()
            try:
                recovery.finalize_existing_economics_operation(**kwargs)
            except recovery.Wbc0027RecoveryError as exc:
                assert "current after-image drifted" in str(exc)
            else:
                raise AssertionError("later target drift was accepted")
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    (target_patch["after_plan_json"], *target_patch["identity"]),
                )
                partial = json.loads(str(target_patch["after_plan_json"]))
                partial["metadata"]["ordinary_publication"] = "partial-target-drift"
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    (recovery._json(partial), *target_patch["identity"]),
                )
                conn.commit()
            try:
                recovery.finalize_existing_economics_operation(**kwargs)
            except recovery.Wbc0027RecoveryError as exc:
                assert "current after-image drifted" in str(exc)
            else:
                raise AssertionError("partial current target drift was accepted")
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(
                    "UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? "
                    "WHERE bundle_version=? AND as_of_date=? AND snapshot_id=?",
                    (target_patch["after_plan_json"], *target_patch["identity"]),
                )
                undo = conn.execute(
                    "SELECT sequence_no,before_json FROM sheet_vitrina_v1_recovery_undo_rows "
                    "WHERE operation_id=? ORDER BY sequence_no LIMIT 1",
                    (candidate["phase_operation_id"],),
                ).fetchone()
                conn.execute(
                    "UPDATE sheet_vitrina_v1_recovery_undo_rows SET before_json=? "
                    "WHERE operation_id=? AND sequence_no=?",
                    ("{}", candidate["phase_operation_id"], undo[0]),
                )
                conn.commit()
            try:
                recovery.finalize_existing_economics_operation(**kwargs)
            except recovery.Wbc0027RecoveryError as exc:
                assert "before/after journal drifted" in str(exc)
            else:
                raise AssertionError("source T1 mismatch was accepted")
        finally:
            recovery.RegistryUploadDbBackedRuntime = original_runtime
            recovery._generation = original_generation
            recovery.reconcile_warehouse_business_projection = original_product
            recovery._hard_non_target_semantics = original_hard
            recovery._target_cells = original_target_cells
            recovery._validate_candidate = original_validate_candidate
            recovery.LEGACY_SOURCE_TRANSACTION_BINDING = original_source_binding


def main() -> None:
    _exercise_post_commit_truth()
    _exercise_genuine_non_target_drift()
    _exercise_false_quarantine_finalize()
    print("wbc0027_lifecycle_regression_smoke: OK")


if __name__ == "__main__":
    main()
