#!/usr/bin/env python3
"""Soak, CAS, crash and quarantine smoke for bounded recovery retention."""

from __future__ import annotations

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
    _soak_and_restart()
    _byte_cap_is_independent_from_count()
    _digest_drift_quarantines_only_candidate()
    _capacity_hard_stop()
    print("warehouse_recovery_retention_smoke: ok")
    return 0


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
