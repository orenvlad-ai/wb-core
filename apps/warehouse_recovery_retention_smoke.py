#!/usr/bin/env python3
"""Soak, CAS, crash and quarantine smoke for bounded recovery retention."""

from __future__ import annotations

import argparse
from contextlib import contextmanager

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.warehouse_recovery_policy import (  # noqa: E402
    RecoveryPolicyError,
    T2_RETENTION_MAX_COUNT,
    WarehouseRecoveryRegistry,
)


def main() -> int:
    _preconditions_are_readonly()
    _complete_consistent_operation_artifact_snapshot()
    _soak_and_restart()
    _byte_cap_is_independent_from_count()
    _digest_drift_quarantines_only_candidate()
    _capacity_hard_stop()
    print("warehouse_recovery_retention_smoke: ok")
    return 0


def _preconditions_are_readonly() -> None:
    from apps import warehouse_recovery_retention as runner
    from packages.application.storage_registry import (
        StoreRegistry, StorageRegistryError, atomic_write_manifest, build_manifest,
    )

    with TemporaryDirectory() as raw:
        runtime = Path(raw)
        selected = runtime / "operational.sqlite3"
        legacy = runtime / "registry_upload_runtime.sqlite3"
        _seed_domain(selected)
        _seed_domain(legacy)
        manifest = build_manifest(
            state="cutover", canonical_source="split", generation_epoch="epoch-1",
            raw_generation_id="raw-1", raw_relative_path="raw.sqlite3", raw_watermark="",
            operational_generation_id="op-1", operational_relative_path=selected.name,
            operational_watermark="", rollback_generation_id="legacy",
            source_fingerprint="source-1",
        )
        atomic_write_manifest(StoreRegistry(runtime).manifest_path, manifest)
        with sqlite3.connect(selected) as conn:
            conn.execute("""CREATE TABLE finance_operational_schema_meta (
                singleton INTEGER, schema_revision TEXT, logical_store TEXT,
                generation_id TEXT, generation_epoch TEXT, source_fingerprint TEXT)""")
            conn.execute("INSERT INTO finance_operational_schema_meta VALUES (1,?,?,?,?,?)",
                         ("operational_v1", "operational", "op-1", "epoch-1", "source-1"))
        registry = WarehouseRecoveryRegistry(runtime_dir=runtime, db_path=selected)
        legacy_before, selected_before = legacy.read_bytes(), selected.read_bytes()
        sha = "a" * 40
        (runtime / ".wb-core-runtime-sha").write_text(sha)
        args = argparse.Namespace(runtime_dir=str(runtime), deployed_sha=sha,
                                  mode="status", fingerprint="")
        with mock.patch.object(runner, "ROOT", runtime), mock.patch(
            "packages.application.warehouse_recovery_policy._connect",
            side_effect=AssertionError("status opened a writable connection"),
        ):
            result = runner.run(args)
            _assert(result["status"]["registry_initialized"] is False, "uninitialized RO status")
            args.mode = "dry-run"
            plan = runner.run(args)
        _assert(plan["target_identity"]["path"] == str(selected.resolve()), "CLI selects operational")
        _assert(selected.read_bytes() == selected_before, "status/dry-run preserve schema")
        for target, fingerprint in ((legacy, str(plan["fingerprint"])),
                                    (selected, "sha256:" + "0" * 64)):
            try:
                WarehouseRecoveryRegistry(runtime_dir=runtime, db_path=target).apply_retention(
                    plan_fingerprint=fingerprint)
            except RecoveryPolicyError:
                pass
            else:
                raise AssertionError("wrong target or stale fingerprint accepted")
            _assert(legacy.read_bytes() == legacy_before, "wrong target leaves legacy byte-identical")
            _assert(selected.read_bytes() == selected_before, "failed precondition leaves schema byte-identical")
        with sqlite3.connect(selected) as conn:
            conn.execute("UPDATE finance_operational_schema_meta SET generation_id='wrong'")
        drifted_before = selected.read_bytes()
        try:
            registry.apply_retention(plan_fingerprint=str(plan["fingerprint"]))
        except StorageRegistryError:
            pass
        else:
            raise AssertionError("wrong file generation identity accepted")
        _assert(selected.read_bytes() == drifted_before, "identity mismatch precedes schema write")
        with sqlite3.connect(selected) as conn:
            conn.execute("UPDATE finance_operational_schema_meta SET generation_id='op-1'")
        current_plan = registry.plan_retention()
        replacement = selected.with_suffix(".replacement")
        replacement.write_bytes(selected.read_bytes())
        replacement.replace(selected)
        replacement_before = selected.read_bytes()
        try:
            registry.apply_retention(plan_fingerprint=str(current_plan["fingerprint"]))
        except RecoveryPolicyError:
            pass
        else:
            raise AssertionError("replaced operational inode accepted with stale fingerprint")
        _assert(selected.read_bytes() == replacement_before, "inode replacement fails before schema write")
        selected.unlink()
        try:
            registry.apply_retention(plan_fingerprint=str(plan["fingerprint"]))
        except StorageRegistryError:
            pass
        else:
            raise AssertionError("missing target accepted")
        _assert(not selected.exists(), "missing target is not created")


def _complete_consistent_operation_artifact_snapshot() -> None:
    from packages.application import warehouse_recovery_policy as policy

    with TemporaryDirectory() as raw:
        runtime = Path(raw)
        db_path = runtime / "registry_upload_runtime.sqlite3"
        _seed_domain(db_path)
        registry = WarehouseRecoveryRegistry(runtime_dir=runtime, db_path=db_path,
                    clock=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
                    operational_reserve_bytes=0)
        _create_t2(registry, index=0)
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            operation = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_recovery_operations").fetchone())
            artifact = dict(conn.execute("SELECT * FROM sheet_vitrina_v1_recovery_artifacts LIMIT 1").fetchone())
            for index in range(1105):
                state = "failed_recoverable" if index == 1103 else "mutation_running" if index == 1104 else "retained"
                row = {**operation, "operation_id": f"bulk-{index:04}", "lifecycle_state": state,
                       "updated_at": "2000-01-01T00:00:00Z" if index >= 1103 else operation["updated_at"]}
                conn.execute("INSERT INTO sheet_vitrina_v1_recovery_operations VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))
                item = {**artifact, "artifact_id": f"artifact-{index}", "operation_id": row["operation_id"],
                        "path": str(runtime / f"checkpoint-{index}.sqlite3")}
                conn.execute("INSERT INTO sheet_vitrina_v1_recovery_artifacts VALUES(" + ",".join("?" for _ in item) + ")", tuple(item.values()))
        # The two oldest active/failed rows rank first by lifecycle, but would
        # previously lose their artifacts in the independently sorted subquery.
        page = registry.list_operations(limit=2)
        _assert({r["operation_id"] for r in page} == {"bulk-1103", "bulk-1104"}, "priority page membership")
        _assert(all(len(r["artifacts"]) == 1 for r in page), "priority page has exact artifacts")
        all_rows = registry.list_operations(limit=None)
        _assert(len(all_rows) == 1106, "full registry exceeds 1000")
        _assert(all(r["artifacts"] and all(a["operation_id"] == r["operation_id"] for a in r["artifacts"]) for r in all_rows), "complete matching artifact sets")
        plan = registry.plan_retention()
        _assert(plan["retained_t2_count"] == 1104, "retention counts beyond first page")
        _assert(plan["candidate_count"] == 1101, "retention includes all eligible rows")
        _assert(not {"bulk-1103", "bulk-1104"}.intersection(plan["operation_ids"]), "failed and active protected")
        original_connect = policy._connect_readonly
        changed = []

        @contextmanager
        def concurrent_read(path: Path):
            with original_connect(path) as conn:
                def trace(sql: str) -> None:
                    if "SELECT * FROM sheet_vitrina_v1_recovery_artifacts" in sql and not changed:
                        changed.append(True)
                        with sqlite3.connect(db_path) as writer:
                            writer.execute("UPDATE sheet_vitrina_v1_recovery_artifacts SET size_bytes=123456 WHERE operation_id='bulk-1103'")
                conn.set_trace_callback(trace)
                yield conn

        with mock.patch.object(policy, "_connect_readonly", concurrent_read):
            snapshot = registry.list_operations(limit=2)
        failed = next(row for row in snapshot if row["operation_id"] == "bulk-1103")
        _assert(changed and failed["artifacts"][0]["size_bytes"] != 123456, "one snapshot survives concurrent artifact change")
        _assert(registry.get_operation("bulk-1103")["artifacts"][0]["size_bytes"] == 123456, "concurrent write actually committed")


def _byte_cap_is_independent_from_count() -> None:
    with TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        _seed_domain(db_path)
        now = [datetime(2026, 7, 27, tzinfo=timezone.utc)]
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir,
            db_path=db_path,
            clock=lambda: now[0],
            operational_reserve_bytes=0,
        )
        operation_ids = []
        for index in range(3):
            _create_t2(registry, index=index)
            operation_ids.append(
                str(registry.list_operations(limit=1)[0]["operation_id"])
            )
            now[0] += timedelta(hours=1)
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE sheet_vitrina_v1_recovery_operations
                SET actual_bytes=?
                WHERE operation_id IN (?,?,?)
                """,
                (900 * 1024 * 1024, *operation_ids),
            )
            conn.commit()
        plan = registry.plan_retention()
        _assert(plan["candidate_count"] == 1, "byte cap releases optional third")
        _assert(
            "projected_byte_cap" in plan["candidates"][0]["reasons"],
            "byte cap reason is explicit",
        )


def _soak_and_restart() -> None:
    with TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        _seed_domain(db_path)
        now = [datetime(2026, 7, 27, tzinfo=timezone.utc)]
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir,
            db_path=db_path,
            clock=lambda: now[0],
            operational_reserve_bytes=0,
        )
        non_target = (
            runtime_dir
            / "backups"
            / "warehouse-recovery"
            / "domain-checkpoints"
            / "foreign.keep"
        )
        non_target.parent.mkdir(parents=True, exist_ok=True)
        non_target.write_text("not owned by retention", encoding="utf-8")

        for index in range(30):
            _create_t2(registry, index=index)
            plan = registry.plan_retention()
            if plan["would_change"]:
                result = registry.apply_retention(
                    plan_fingerprint=str(plan["fingerprint"])
                )
                _assert(result["status"] == "applied", "soak retention applied")
            status = registry.plan_retention()
            _assert(
                status["retained_t2_count"] <= T2_RETENTION_MAX_COUNT,
                "soak retained count is bounded",
            )
            _assert(
                status["projection"]["projected_30d_growth_bytes"] == 0,
                "30-day projection is steady-state bounded",
            )
            now[0] += timedelta(hours=1)

        _assert(non_target.is_file(), "retention preserves foreign non-target")
        checkpoints = list(non_target.parent.glob("recovery_*.sqlite3"))
        _assert(
            len(checkpoints) == T2_RETENTION_MAX_COUNT,
            "soak leaves exactly the minimum restore set",
        )
        _assert(
            all(
                str(path).startswith(
                    str(runtime_dir / "backups" / "warehouse-recovery")
                )
                for path in checkpoints
            ),
            "new T2 artifacts route through the backup filesystem root",
        )

        stale = registry.plan_retention()
        _create_t2(registry, index=31)
        try:
            registry.apply_retention(
                plan_fingerprint=str(stale["fingerprint"])
            )
        except RecoveryPolicyError as exc:
            _assert("stale" in str(exc), "concurrent writer invalidates stale plan")
        else:
            raise AssertionError("stale retention plan unexpectedly applied")

        crash_plan = registry.plan_retention()

        def crash_once(_operation_id: str, boundary: str) -> None:
            if boundary.startswith("after_retention_unlink:"):
                raise RuntimeError("simulated retention crash")

        crashing = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir,
            db_path=db_path,
            clock=lambda: now[0],
            fault_injector=crash_once,
            operational_reserve_bytes=0,
        )
        try:
            crashing.apply_retention(
                plan_fingerprint=str(crash_plan["fingerprint"])
            )
        except RuntimeError as exc:
            _assert("simulated retention crash" in str(exc), "crash injected")
        else:
            raise AssertionError("retention crash injection did not fire")

        resumed = registry.apply_retention(
            plan_fingerprint=str(crash_plan["fingerprint"])
        )
        _assert(resumed["status"] == "applied", "retention resumes after unlink")
        repeated = registry.apply_retention(
            plan_fingerprint=str(crash_plan["fingerprint"])
        )
        _assert(repeated["idempotent"] is True, "retention repeat is idempotent")
        _assert(non_target.is_file(), "restart preserves non-target")


def _digest_drift_quarantines_only_candidate() -> None:
    with TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        _seed_domain(db_path)
        now = [datetime(2026, 7, 27, tzinfo=timezone.utc)]
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir,
            db_path=db_path,
            clock=lambda: now[0],
            operational_reserve_bytes=0,
        )
        for index in range(4):
            _create_t2(registry, index=index)
            now[0] += timedelta(hours=1)
        plan = registry.plan_retention()
        candidate = plan["candidates"][0]
        checkpoint = next(
            Path(item["path"])
            for item in candidate["artifacts"]
            if item["artifact_kind"] == "domain_checkpoint"
        )
        with checkpoint.open("ab") as handle:
            handle.write(b"drift")
        result = registry.apply_retention(
            plan_fingerprint=str(plan["fingerprint"])
        )
        _assert(result["status"] == "partial_failure", "digest drift is visible")
        operation = registry.get_operation(str(candidate["operation_id"])) or {}
        _assert(
            operation.get("lifecycle") == "quarantined",
            "drifted candidate is quarantined",
        )
        _assert(checkpoint.is_file(), "drifted candidate is never deleted")


def _capacity_hard_stop() -> None:
    with TemporaryDirectory() as raw:
        runtime_dir = Path(raw) / "state"
        db_path = runtime_dir / "registry_upload_runtime.sqlite3"
        _seed_domain(db_path)
        registry = WarehouseRecoveryRegistry(
            runtime_dir=runtime_dir,
            db_path=db_path,
            operational_reserve_bytes=0,
        )
        with mock.patch(
            "packages.application.warehouse_recovery_policy.shutil.disk_usage",
            return_value=mock.Mock(free=1024),
        ):
            try:
                _create_t2(registry, index=1)
            except RecoveryPolicyError as exc:
                _assert("capacity hard stop" in str(exc), "capacity fails closed")
            else:
                raise AssertionError("T2 checkpoint ignored hard capacity watermark")


def _seed_domain(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE sheet_vitrina_v1_warehouse_fixture(
                id INTEGER PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO sheet_vitrina_v1_warehouse_fixture(payload) VALUES(?)",
            ("domain-only",),
        )
        conn.execute(
            "CREATE TABLE wb_finance_weekly_raw_rows(id INTEGER PRIMARY KEY,raw TEXT)"
        )
        conn.execute(
            "INSERT INTO wb_finance_weekly_raw_rows(raw) VALUES('must-not-open')"
        )


def _create_t2(registry: WarehouseRecoveryRegistry, *, index: int) -> None:
    fingerprint = f"sha256:{index:064x}"
    operation = registry.prepare_t2(
        mutation_kind="hourly_warehouse_sync",
        plan_fingerprint=fingerprint,
        scope={"cycle": index},
        source_digest=f"sha256:source-{index}",
        non_target_digest="sha256:non-target",
        source_watermarks={"cycle": index},
        schema_revision="fixture-v1",
    )
    registry.retain(
        str(operation["operation_id"]),
        after_digest=f"sha256:after-{index}",
        non_target_digest="sha256:non-target",
    )


def _assert(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


if __name__ == "__main__":
    raise SystemExit(main())
