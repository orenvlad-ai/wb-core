#!/usr/bin/env python3
"""Production-shaped smoke for the exact WBC0027 split hot-journal recovery."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.business_data_maintenance as maintenance
import apps.hosted_runtime_deploy_barrier as deploy_barrier
import apps.sqlite_hot_journal_recovery as recovery
import apps.storage_recovery_sanitation_job as jobs


OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
WINDOW = "wbc0027-s047-live-last-good-freeze-v2-896b02c0"
PLAN_FP = "sha256:" + "c" * 64
STATE_FP = "sha256:" + "d" * 64


def _crashed_database(root: Path) -> tuple[Path, Path]:
    database = root / "operational.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA journal_mode=delete")
    connection.execute("CREATE TABLE rows(id INTEGER PRIMARY KEY, payload BLOB)")
    connection.executemany(
        "INSERT INTO rows(payload) VALUES (?)",
        [(os.urandom(3500),) for _ in range(300)],
    )
    connection.commit()
    connection.close()
    journal = Path(str(database) + "-journal")
    page_number = 2
    with database.open("r+b") as handle:
        handle.seek((page_number - 1) * recovery.EXPECTED_PAGE_SIZE)
        original = handle.read(recovery.EXPECTED_PAGE_SIZE)
        handle.seek((page_number - 1) * recovery.EXPECTED_PAGE_SIZE)
        handle.write(bytes(value ^ 0x5A for value in original))
        handle.flush()
        os.fsync(handle.fileno())
    nonce = 0x12345678
    checksum = nonce
    offset = recovery.EXPECTED_PAGE_SIZE - 200
    while offset > 0:
        checksum = (checksum + original[offset]) & 0xFFFFFFFF
        offset -= 200
    header = bytearray(512)
    header[:8] = recovery.EXPECTED_JOURNAL_MAGIC
    header[8:12] = (1).to_bytes(4, "big")
    header[12:16] = nonce.to_bytes(4, "big")
    header[16:20] = (
        database.stat().st_size // recovery.EXPECTED_PAGE_SIZE
    ).to_bytes(4, "big")
    header[20:24] = (512).to_bytes(4, "big")
    header[24:28] = recovery.EXPECTED_PAGE_SIZE.to_bytes(4, "big")
    with journal.open("wb") as handle:
        handle.write(header)
        handle.write(page_number.to_bytes(4, "big"))
        handle.write(original)
        handle.write(checksum.to_bytes(4, "big"))
        handle.flush()
        os.fsync(handle.fileno())
    assert journal.read_bytes()[:8] == recovery.EXPECTED_JOURNAL_MAGIC
    return database, journal


def _hot_recovery_smoke(root: Path) -> None:
    root.mkdir()
    runtime = root / "runtime"
    backup = root / "backup"
    runtime.mkdir()
    backup.mkdir()
    database, journal = _crashed_database(root)
    raw = root / "finance_raw.sqlite3"
    raw.write_bytes(b"raw-stable")
    storage_manifest = runtime / "storage_generation_manifest.json"
    storage_manifest.write_text('{"stable":true}\n', encoding="utf-8")
    overlay = recovery._journal_overlay(database, journal)
    assert overlay["record_count"] > 0
    assert overlay["pages_different_from_main"] > 0
    before = recovery._file_identity(database)
    plan = {
        "contract_name": recovery.CONTRACT_NAME,
        "read_only": True,
        "operation_id": "e" * 64,
        "deployed_sha": NEW_SHA,
        "source_epoch_deployed_sha": OLD_SHA,
        "runtime_dir": str(runtime),
        "barrier": {
            "active": True,
            "phase": "acquiring",
            "hold_confirmed": False,
            "window_id": WINDOW,
            "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance": {
            "partial_epoch_fingerprint": "sha256:" + "f" * 64,
        },
        "database": before,
        "journal": recovery._file_identity(journal),
        "journal_overlay": overlay,
        "raw_database": recovery._file_identity(raw),
        "storage_generation_manifest_file": recovery._file_identity(
            storage_manifest
        ),
        "compressed_measurement": {
            "database": recovery._zstd_measure(database),
            "journal": recovery._zstd_measure(journal),
        },
        "backup": {
            "directory": str(backup / "capsule"),
            "reserve_bytes": 0,
            "evidence_envelope_bytes": 1,
        },
        "expected_effect": {
            "database_sha256": overlay["expected_recovered_database_sha256"],
        },
        "created_at": "2026-09-02T00:00:00Z",
    }
    plan["fingerprint"] = recovery._plan_fingerprint(plan)
    plan_path = root / "plan.json"
    plan_path.write_text(recovery._canonical_json(plan) + "\n", encoding="utf-8")
    plan_sha = "sha256:" + recovery.file_sha256(plan_path)
    with (
        mock.patch.object(recovery, "DEFAULT_BACKUP_ROOT", backup),
        mock.patch.object(recovery, "build_plan", return_value=dict(plan)),
        mock.patch.object(recovery, "_preflight", return_value=dict(plan)),
        mock.patch.object(
            recovery.maintenance,
            "_prepared_abort_breakglass_counters",
            return_value={"operations": 0, "submits": 0},
        ),
    ):
        stream = recovery._stream_zstd
        interrupted = {"done": False}

        def interrupt_once(source: Path, destination: Path):
            compressed = stream(source, destination)
            if not interrupted["done"]:
                interrupted["done"] = True
                raise RuntimeError("simulated compression worker crash")
            return compressed

        with mock.patch.object(
            recovery, "_stream_zstd", side_effect=interrupt_once
        ):
            try:
                recovery.apply_plan(
                    plan_path=plan_path,
                    plan_sha256=plan_sha,
                    fingerprint=plan["fingerprint"],
                    deployed_sha_file=root / "unused-sha",
                )
            except RuntimeError as exc:
                assert "simulated compression worker crash" in str(exc)
            else:
                raise AssertionError("compression crash did not interrupt worker")
        assert (backup / "capsule" / "database.zst.partial").is_file()
        assert journal.is_file()
        result = recovery.apply_plan(
            plan_path=plan_path,
            plan_sha256=plan_sha,
            fingerprint=plan["fingerprint"],
            deployed_sha_file=root / "unused-sha",
        )
        (backup / "capsule" / "result.json").unlink()
        (runtime / recovery.MARKER_FILENAME).unlink()
        recovery_state_path = backup / "capsule" / "recovery-state.json"
        recovery_state = json.loads(recovery_state_path.read_text())
        recovery_state["phase"] = "sqlite_recovery_returned"
        recovery._atomic_json(recovery_state_path, recovery_state)
        resumed = recovery.apply_plan(
            plan_path=plan_path,
            plan_sha256=plan_sha,
            fingerprint=plan["fingerprint"],
            deployed_sha_file=root / "unused-sha",
        )
    assert result["status"] == "recovered"
    assert resumed["database_after"]["sha256"] == result["database_after"]["sha256"]
    assert result["logical_business_delta"] == 0
    assert result["database_after"]["sha256"] == overlay[
        "expected_recovered_database_sha256"
    ]
    assert not journal.exists()
    assert recovery.file_sha256(raw) == plan["raw_database"]["sha256"]
    assert result["capsule"]["capsule"]["database"]["decompressed"] == {
        "size_bytes": before["size_bytes"],
        "sha256": before["sha256"],
    }
    assert result["capsule"]["capsule"]["journal"]["decompressed"] == {
        "size_bytes": plan["journal"]["size_bytes"],
        "sha256": plan["journal"]["sha256"],
    }


def _deploy_barrier_smoke(root: Path) -> None:
    enabled = sorted(deploy_barrier.ACTIVE_BARRIER_ENABLE_UNITS)
    restarted = sorted(deploy_barrier.ACTIVE_BARRIER_RESTART_UNITS)
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")

    with (
        mock.patch.object(
            deploy_barrier, "barrier_status", return_value={"active": False}
        ),
        mock.patch.object(deploy_barrier.subprocess, "run", side_effect=run),
    ):
        normal = deploy_barrier.reconcile(
            runtime_dir=root, enable=enabled, restart=restarted
        )
    assert normal["preserved_pause_owned_units"] == []
    assert calls == [
        ["systemctl", "enable", *enabled],
        ["systemctl", "restart", *restarted],
    ]

    state = {
        "phase": "abort_quiescing",
        "prepared_abort_partial_restore_recovery_epoch": {
            "schema_version": recovery.EXPECTED_PARTIAL_EPOCH_SCHEMA,
            "epoch": 2,
            "window_id": WINDOW,
            "plan_fingerprint": PLAN_FP,
            "barrier_state_fingerprint": STATE_FP,
            "timer_units_to_disable": list(
                recovery.RECOVERY_PAUSE_OWNED_TIMERS
            ),
            "disabled_timer_units": list(
                recovery.RECOVERY_PAUSE_OWNED_TIMERS
            ),
            "pending_disable_unit": "",
        },
    }
    (root / ".business-data-maintenance.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    calls.clear()

    def state_for(unit: str):
        if unit.endswith(".timer"):
            return {
                "LoadState": "loaded", "UnitFileState": "disabled",
                "ActiveState": "inactive", "SubState": "dead", "MainPID": "0",
            }
        return {
            "LoadState": "loaded", "UnitFileState": "static",
            "ActiveState": "inactive", "SubState": "dead", "MainPID": "0",
        }

    with (
        mock.patch.object(
            deploy_barrier,
            "barrier_status",
            return_value={
                "active": True, "phase": "acquiring", "hold_confirmed": False,
                "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
                "state_fingerprint": STATE_FP,
            },
        ),
        mock.patch.object(deploy_barrier, "_unit_state", side_effect=state_for),
        mock.patch.object(deploy_barrier.subprocess, "run", side_effect=run),
    ):
        held = deploy_barrier.reconcile(
            runtime_dir=root, enable=enabled, restart=restarted
        )
    assert held["preserved_pause_owned_units"] == sorted(
        recovery.RECOVERY_PAUSE_OWNED_TIMERS
    )
    assert calls == [
        [
            "systemctl", "enable",
            *[unit for unit in enabled if unit not in recovery.RECOVERY_PAUSE_OWNED_TIMERS],
        ],
        [
            "systemctl", "restart",
            *[unit for unit in restarted if unit not in recovery.RECOVERY_PAUSE_OWNED_TIMERS],
        ],
    ]
    calls.clear()
    with (
        mock.patch.object(
            deploy_barrier,
            "barrier_status",
            return_value={
                "active": True, "phase": "acquiring", "hold_confirmed": False,
                "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
                "state_fingerprint": STATE_FP,
            },
        ),
        mock.patch.object(deploy_barrier, "_unit_state", side_effect=state_for),
        mock.patch.object(deploy_barrier.subprocess, "run", side_effect=run),
    ):
        preflight = deploy_barrier.reconcile(
            runtime_dir=root, enable=enabled, restart=restarted, mutate=False
        )
    assert preflight["status"] == "validated"
    assert calls == []

    with (
        mock.patch.object(
            deploy_barrier,
            "barrier_status",
            return_value={
                "active": True, "phase": "acquiring", "hold_confirmed": False,
                "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
                "state_fingerprint": STATE_FP,
            },
        ),
        mock.patch.object(deploy_barrier, "_unit_state", side_effect=state_for),
        mock.patch.object(deploy_barrier.subprocess, "run", side_effect=run),
    ):
        try:
            deploy_barrier.reconcile(
                runtime_dir=root,
                enable=[*enabled, "wb-core-unknown-writer.timer"],
                restart=restarted,
            )
        except deploy_barrier.DeployBarrierError:
            pass
        else:
            raise AssertionError("unknown managed unit was accepted under barrier")
    assert calls == []

    calls.clear()
    with (
        mock.patch.object(
            deploy_barrier,
            "barrier_status",
            return_value={"active": True, "phase": "unknown"},
        ),
        mock.patch.object(deploy_barrier.subprocess, "run", side_effect=run),
    ):
        try:
            deploy_barrier.reconcile(
                runtime_dir=root, enable=enabled, restart=restarted
            )
        except deploy_barrier.DeployBarrierError:
            pass
        else:
            raise AssertionError("ambiguous barrier was accepted")
    assert calls == []


def _one_submit_smoke(root: Path) -> None:
    runtime = root / "job-runtime"
    root_backups = root / "root-backups"
    runtime.mkdir()
    root_backups.mkdir()
    sha_file = root / "sha"
    sha_file.write_text(NEW_SHA + "\n", encoding="utf-8")
    kwargs = dict(
        runtime_dir=runtime,
        root_backups=root_backups,
        deployed_sha_file=sha_file,
        job_id="1" * 64,
        deployed_sha=NEW_SHA,
        operation="sqlite-hot-journal-recovery-apply",
        root_name="",
        family="",
        reviewed_plan=(
            "/opt/wb-core-runtime/state/private-evidence/production-goals/"
            f"wbc0027-s047-hot-journal-plan-{NEW_SHA}.json"
        ),
        reviewed_plan_sha256="sha256:" + "2" * 64,
        confirm_fingerprint="sha256:" + "3" * 64,
        approval_reference="ROOT callback exact manifest",
        starter=lambda job_id: {"name": job_id, "start": "requested"},
    )
    original_read_json = jobs._read_json

    def read_json(path: Path, *, label: str):
        if label == "hot journal reviewed plan":
            return {
                "contract_name": recovery.CONTRACT_NAME,
                "operation_id": kwargs["job_id"],
                "deployed_sha": NEW_SHA,
                "fingerprint": kwargs["confirm_fingerprint"],
            }
        return original_read_json(path, label=label)

    with (
        mock.patch.object(jobs, "_read_json", side_effect=read_json),
        mock.patch.object(jobs, "file_sha256", return_value="2" * 64),
    ):
        first = jobs.submit_job(**kwargs)
        second = jobs.submit_job(**kwargs)
        assert first["unit_start_requested"] is True
        assert second["submit_idempotent"] is True
        with mock.patch.object(
            recovery,
            "apply_plan",
            return_value={"status": "recovered", "logical_business_delta": 0},
        ) as apply:
            terminal = jobs.run_worker(
                runtime_dir=runtime,
                root_backups=root_backups,
                deployed_sha_file=sha_file,
                job_id="1" * 64,
            )
        assert terminal["status"] == "succeeded"
        apply.assert_called_once_with(
            plan_path=Path(kwargs["reviewed_plan"]),
            plan_sha256=kwargs["reviewed_plan_sha256"],
            fingerprint=kwargs["confirm_fingerprint"],
            deployed_sha_file=sha_file,
        )
        changed = dict(kwargs)
        changed["confirm_fingerprint"] = "sha256:" + "4" * 64
        try:
            jobs.submit_job(**changed)
        except jobs.SanitationJobError:
            pass
        else:
            raise AssertionError("job id was rebound")


def _marker_smoke() -> None:
    partial = {"deployed_sha": OLD_SHA, "epoch": 2}
    result = {
        "contract_name": maintenance.HOT_JOURNAL_RECOVERY_RESULT_CONTRACT,
        "operation_id": "5" * 64,
        "source_epoch_deployed_sha": OLD_SHA,
        "deployed_sha": NEW_SHA,
        "barrier": {
            "window_id": WINDOW,
            "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance_partial_epoch_fingerprint": maintenance._stable_fingerprint(
            partial
        ),
        "database_after": {"sha256": "6" * 64},
        "journal_absent": True,
        "sqlite_readback": {
            "integrity_check": "ok",
            "foreign_key_violation_count": 0,
        },
        "business_operation_counters": {"operations": 0},
        "logical_business_delta": 0,
    }
    result["result_fingerprint"] = maintenance._stable_fingerprint(result)
    marker = {
        "contract_name": maintenance.HOT_JOURNAL_RECOVERY_RESULT_CONTRACT,
        "operation_id": result["operation_id"],
        "source_epoch_deployed_sha": OLD_SHA,
        "deployed_sha": NEW_SHA,
        "barrier": {
            "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
            "state_fingerprint": STATE_FP,
        },
        "maintenance_partial_epoch_fingerprint": maintenance._stable_fingerprint(
            partial
        ),
        "database_after": result["database_after"],
        "journal_absent": True,
        "sqlite_readback": {
            "integrity_check": "ok", "foreign_key_violation_count": 0,
        },
        "business_operation_counters": {"operations": 0},
        "result_path": (
            "/opt/wb-core-runtime/state/backups/private-evidence/production-goals/"
            "wbc0027-s047-hot-journal-recovery-bbbbbbbb/"
            + result["operation_id"]
            + "/result.json"
        ),
        "result_fingerprint": result["result_fingerprint"],
    }
    marker["marker_fingerprint"] = maintenance._stable_fingerprint(marker)

    def load(path: Path):
        return marker if path.name.endswith("recovery.json") else result

    with mock.patch.object(maintenance, "_load_json_object", side_effect=load):
        maintenance._validate_hot_journal_recovery_marker(
            Path("/runtime"),
            partial_epoch=partial,
            deployed_sha=NEW_SHA,
            barrier={
                "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
                "state_fingerprint": STATE_FP,
            },
        )
    drifted = dict(marker)
    drifted["journal_absent"] = False
    drifted["marker_fingerprint"] = maintenance._stable_fingerprint(
        {key: value for key, value in drifted.items() if key != "marker_fingerprint"}
    )
    with mock.patch.object(
        maintenance,
        "_load_json_object",
        side_effect=lambda path: drifted if path.name.endswith("recovery.json") else result,
    ):
        try:
            maintenance._validate_hot_journal_recovery_marker(
                Path("/runtime"), partial_epoch=partial, deployed_sha=NEW_SHA,
                barrier={
                    "window_id": WINDOW, "plan_fingerprint": PLAN_FP,
                    "state_fingerprint": STATE_FP,
                },
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid recovery marker was accepted")


def main() -> None:
    assert recovery.EXPECTED_JOURNAL_RECORDS == 169
    assert recovery.EXPECTED_RECOVERED_DATABASE_SHA256 == (
        "92d2f05c503afed742f58f0b318eff7b78ce32e1be2979275a205e31ac26f70f"
    )
    with tempfile.TemporaryDirectory(prefix="wbc0027-hot-journal-") as raw:
        root = Path(raw)
        _hot_recovery_smoke(root / "recovery")
        (root / "deploy").mkdir()
        _deploy_barrier_smoke(root / "deploy")
        _one_submit_smoke(root)
    _marker_smoke()
    print("sqlite_hot_journal_recovery_smoke: ok")


if __name__ == "__main__":
    main()
