#!/usr/bin/env python3
"""Deterministic restore/publish guards for WBC0008 block 006."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import root_storage_warm_archive as warm
from apps.storage_recovery_sanitation_job import submit_job


OPERATION = "production-goal-v1-" + "a" * 32


def _seed(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO evidence VALUES('scope', 'wbc0008-006')")
        connection.commit()


def _healthy_systemd_snapshot() -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for name in warm.SERVICE_NAMES:
        values: dict[str, object] = {
            "Id": name,
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "SubState": "dead",
            "Result": "success",
            "MainPID": "0",
            "ExecMainStatus": "0",
            "UnitFileState": "static",
            "LastTriggerUSec": "",
            "NextElapseUSecRealtime": "",
            "QueryReturnCode": 0,
            "QueryError": None,
            "QueryStderrSha256": "sha256:" + "0" * 64,
        }
        if name.endswith(".timer"):
            values.update(
                {
                    "ActiveState": "active",
                    "SubState": "waiting",
                    "UnitFileState": "enabled",
                    "LastTriggerUSec": "Wed 2026-08-26 17:17:00 UTC",
                    "NextElapseUSecRealtime": "Wed 2026-08-26 18:17:00 UTC",
                }
            )
        elif name in warm.PERSISTENT_SERVICE_NAMES:
            values.update(
                {
                    "ActiveState": "active",
                    "SubState": "running",
                    "MainPID": "101",
                    "UnitFileState": "enabled",
                }
            )
        snapshot[name] = values
    return snapshot


def run() -> None:
    assert len(warm.TARGET_POLICIES) == 6
    assert len({item["source_path"] for item in warm.TARGET_POLICIES}) == 6
    assert len({item["archive_name"] for item in warm.TARGET_POLICIES}) == 6
    assert warm.DESTINATION_ROOT == Path("/opt/wb-core-runtime/state/backups")
    assert warm.ROOT_MINIMUM_AFTER_BYTES == 25 * 1024**3
    assert warm.EMERGENCY_RESERVE_BYTES == 8 * 1024**3
    assert warm.READINESS_REQUIRED_CONSECUTIVE_CLEAN == 3
    assert len(warm.SERVICE_NAMES) == 27
    assert len(warm.TIMER_SERVICE_PAIRS) == 12
    assert {
        owner for _timer, owner in warm.TIMER_SERVICE_PAIRS
    } == set(warm.SERVICE_NAMES) - set(warm.PERSISTENT_SERVICE_NAMES) - {
        name for name in warm.SERVICE_NAMES if name.endswith(".timer")
    }
    healthy_systemd = warm._systemd_service_gate(_healthy_systemd_snapshot())
    assert healthy_systemd["healthy"] is True
    assert healthy_systemd["expected_unit_count"] == 27
    assert len(healthy_systemd["units"]) == 27
    assert healthy_systemd["classification_counts"] == {
        "correct_inactive_oneshot": 12,
        "expected_waiting_timer": 12,
        "healthy_persistent_service": 3,
    }
    assert healthy_systemd["pair_classification_counts"] == {
        "waiting_with_inactive_success_owner": 12,
    }
    for row in healthy_systemd["units"]:
        for field in warm.SYSTEMD_REQUIRED_PROPERTIES:
            assert field in row
        if row["unit_kind"] == "timer":
            for field in warm.SYSTEMD_TIMER_PROPERTIES:
                assert field in row

    for timer_name, owner_name in warm.TIMER_SERVICE_PAIRS:
        activating = _healthy_systemd_snapshot()
        activating[timer_name].update({"ActiveState": "active", "SubState": "running"})
        activating[owner_name].update(
            {
                "ActiveState": "activating",
                "SubState": "start",
                "Result": "",
                "ExecMainStatus": "0",
                "MainPID": "504093",
            }
        )
        activating_gate = warm._systemd_service_gate(activating)
        assert activating_gate["healthy"] is True
        activating_pair = next(
            item for item in activating_gate["pairs"] if item["timer_name"] == timer_name
        )
        assert activating_pair["classification"] == (
            "trigger_in_progress_with_active_owner"
        )

        active = _healthy_systemd_snapshot()
        active[timer_name].update({"ActiveState": "active", "SubState": "running"})
        active[owner_name].update(
            {
                "ActiveState": "active",
                "SubState": "running",
                "Result": "success",
                "ExecMainStatus": "0",
                "MainPID": "504094",
            }
        )
        active_gate = warm._systemd_service_gate(active)
        assert active_gate["healthy"] is True
        assert next(
            item for item in active_gate["pairs"] if item["timer_name"] == timer_name
        )["classification"] == "trigger_in_progress_with_active_owner"

    failed_owner_snapshot = _healthy_systemd_snapshot()
    failed_owner_snapshot["wb-core-warehouse-functional-sync.service"].update(
        {"Result": "exit-code", "ExecMainStatus": "1"}
    )
    failed_owner = warm._systemd_service_gate(failed_owner_snapshot)
    assert failed_owner["healthy"] is False
    assert failed_owner["failing_pair_count"] == 1
    assert failed_owner["resample_required_pair_names"] == []
    failed_owner_row = next(
        item
        for item in failed_owner["units"]
        if item["name"] == "wb-core-warehouse-functional-sync.service"
    )
    assert failed_owner_row["classification"] == "real_unhealthy_owning_service"
    assert failed_owner_row["reason_codes"][:2] == [
        "failed_result",
        "nonzero_or_invalid_exec_main_status",
    ]

    failed_timer_snapshot = _healthy_systemd_snapshot()
    failed_timer_snapshot["wb-core-warehouse-functional-sync.timer"].update(
        {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code"}
    )
    failed_timer = warm._systemd_service_gate(failed_timer_snapshot)
    assert failed_timer["healthy"] is False
    assert failed_timer["failing_pair_count"] == 1
    assert failed_timer["resample_required_pair_names"] == []

    ambiguous_snapshot = _healthy_systemd_snapshot()
    ambiguous_snapshot["wb-core-warehouse-functional-sync.timer"].update(
        {"ActiveState": "active", "SubState": "mystery-transition"}
    )
    ambiguous = warm._systemd_service_gate(ambiguous_snapshot)
    assert ambiguous["healthy"] is False
    assert ambiguous["failing_pairs"][0]["classification"] == (
        "bounded_snapshot_transition"
    )
    unresolved = warm._systemd_service_gate_with_resample(
        ambiguous_snapshot,
        snapshot_reader=lambda names: {
            name: ambiguous_snapshot[name] for name in names
        },
        max_attempts=1,
        max_seconds=1,
        interval_seconds=0,
    )
    assert unresolved["healthy"] is False
    assert unresolved["pair_resample_evidence"]["attempt_count"] == 1
    assert unresolved["pair_resample_evidence"]["resolved_healthy"] is False

    raced_snapshot = _healthy_systemd_snapshot()
    raced_snapshot["wb-core-warehouse-functional-sync.timer"].update(
        {"ActiveState": "active", "SubState": "running"}
    )
    resolved_snapshot = _healthy_systemd_snapshot()
    resolved_snapshot["wb-core-warehouse-functional-sync.timer"].update(
        {"ActiveState": "active", "SubState": "running"}
    )
    resolved_snapshot["wb-core-warehouse-functional-sync.service"].update(
        {
            "ActiveState": "activating",
            "SubState": "start",
            "Result": "success",
            "ExecMainStatus": "0",
            "MainPID": "504093",
        }
    )
    raced = warm._systemd_service_gate_with_resample(
        raced_snapshot,
        snapshot_reader=lambda names: {name: resolved_snapshot[name] for name in names},
        max_attempts=1,
        max_seconds=1,
        interval_seconds=0,
    )
    assert raced["healthy"] is True
    assert raced["pair_resample_evidence"]["attempt_count"] == 1
    assert raced["pair_resample_evidence"]["resolved_healthy"] is True
    assert len(raced["pair_resample_evidence"]["samples"]) == 2

    masked_snapshot = _healthy_systemd_snapshot()
    masked_snapshot["wb-core-autoanswers-worker.timer"].update(
        {"LoadState": "masked", "UnitFileState": "masked"}
    )
    masked = warm._systemd_service_gate(masked_snapshot)
    assert masked["healthy"] is False
    assert next(
        item
        for item in masked["failing_units"]
        if item["name"] == "wb-core-autoanswers-worker.timer"
    )["classification"] == "absent_or_masked"

    missing_snapshot = _healthy_systemd_snapshot()
    missing_snapshot.pop("wb-core-data-mcp.service")
    missing = warm._systemd_service_gate(missing_snapshot)
    assert missing["healthy"] is False
    assert missing["classification"] == "predicate_or_literal_unit_list_defect"
    assert missing["missing_unit_names"] == ["wb-core-data-mcp.service"]
    assert next(
        item
        for item in missing["units"]
        if item["name"] == "wb-core-data-mcp.service"
    )["classification"] == "predicate_or_literal_unit_list_defect"

    clean_activity = {
        "identity_matches_expected": True,
        "material_stable_during_gate": True,
        "sidecars": [
            {"suffix": suffix, "path": "/fixture" + suffix, "present": False}
            for suffix in ("-wal", "-shm", "-journal")
        ],
        "fd_openers": [
            {
                "pid": 101,
                "fd": 7,
                "comm": "sqlite-reader",
                "access_mode": "read_only",
            }
        ],
        "kernel_locks": [],
        "hold_evidence": {"marker_paths": [], "hold_xattr_names": []},
        "provenance_matches_expected": True,
        "related_process_observations": [
            {
                "pid": 202,
                "matches": ["fixture.sqlite3"],
                "classification": "observation_only_without_fd_or_lock_binding",
            }
        ],
    }
    assert warm._classify_activity_evidence(clean_activity) == []
    for mode in ("write_only", "read_write", "unknown"):
        blocked = dict(clean_activity)
        blocked["fd_openers"] = [
            {"pid": 303, "fd": 9, "comm": "writer", "access_mode": mode}
        ]
        blockers = warm._classify_activity_evidence(blocked)
        assert blockers[0]["code"] == "write_capable_or_unknown_fd_opener"
        assert blockers[0]["access_mode"] == mode

    with tempfile.TemporaryDirectory(prefix="root-warm-archive-smoke-") as raw:
        root = Path(raw)
        source = root / "source.sqlite3"
        archive = root / "01-source.sqlite3.zst"
        manifest = archive.with_name(archive.name + ".manifest.json")
        temporary_archive = root / ".owned.archive.tmp"
        restore = root / ".owned.restore.tmp.sqlite3"
        _seed(source)
        identity = warm._file_identity(source)
        sqlite = warm._sqlite_probe(source)
        target = {
            "key": "fixture",
            "source_path": str(source),
            "archive_name": archive.name,
            "identity": identity,
            "sidecars": warm._sidecars(source),
            "sqlite": sqlite,
        }
        compressed = warm._compress(source, temporary_archive)
        assert warm._stream_decompressed_identity(temporary_archive) == {
            "decompressed_size_bytes": identity["apparent_size_bytes"],
            "decompressed_sha256": identity["sha256"],
        }
        restore_proof = warm._full_restore_proof(
            archive=temporary_archive,
            expected_source=identity,
            temporary=restore,
        )
        assert restore_proof["quick_check"] == "ok"
        assert restore_proof["integrity_check"] == "ok"
        temporary_archive.replace(archive)
        payload = {
            "contract_name": warm.CONTRACT_NAME,
            "operation_id": OPERATION,
            "source": identity,
            "archive_path": str(archive),
            "archive_sha256": compressed["archive_sha256"],
            "archive_size_bytes": compressed["archive_size_bytes"],
            "lifecycle_state": "verified_pending_source_removal",
            "source_removed": False,
        }
        warm._atomic_write_json(manifest, payload)
        original_identity = warm._file_identity

        def root_owned(path: Path, *, include_sha256: bool = True) -> dict[str, object]:
            row = original_identity(path, include_sha256=include_sha256)
            if path in {archive, manifest}:
                row.update({"uid": 0, "gid": 0})
            return row

        warm._file_identity = root_owned
        try:
            proof = warm._verify_archive_pair(
                archive=archive,
                manifest_path=manifest,
                operation_id=OPERATION,
                expected_target=target,
                full_restore=True,
                restore_temp=restore,
            )
            assert proof["decompressed_sha256"] == identity["sha256"]
            source.unlink()
            reconciled = warm._reconcile_pending_unlink(
                target=target,
                item_state={"phase": "pending_unlink"},
                archive=archive,
                manifest_path=manifest,
                operation_id=OPERATION,
                restore_temp=restore,
            )
        finally:
            warm._file_identity = original_identity
        assert reconciled and reconciled["unlink_count"] == 1
        final = json.loads(manifest.read_text(encoding="utf-8"))
        assert final["lifecycle_state"] == "retained"
        assert final["source_removed"] is True
        assert final["unlink_receipt"]["reconciled_from_pending_intent"] is True

        runtime = root / "state"
        root_backups = root / "root-backups"
        runtime.mkdir()
        root_backups.mkdir()
        deployed_marker = root / ".wb-core-runtime-sha"
        deployed_marker.write_text("b" * 40, encoding="utf-8")
        exact_manifest = (
            "/opt/wb-core-runtime/state/private-evidence/production-goals/"
            + OPERATION
            + "/root-warm-archive-plan-20260826T120000Z.json"
        )
        submitted = submit_job(
            runtime_dir=runtime,
            root_backups=root_backups,
            deployed_sha_file=deployed_marker,
            job_id="c" * 64,
            deployed_sha="b" * 40,
            operation="warm-archive-apply",
            root_name="",
            family="",
            manifest=exact_manifest,
            manifest_sha256="sha256:" + "d" * 64,
            goal_operation_id=OPERATION,
            approval_reference="github:owner:bounded-wbc0008-006",
            starter=lambda job_id: {"name": job_id, "start": "fixture"},
        )
        assert submitted["status"] == "queued"
        assert submitted["request"]["operation"] == "warm-archive-apply"
        assert submitted["request"]["manifest"] == exact_manifest

        monitor_journal = {"contract_name": warm.CONTRACT_NAME}
        monitor_journal_path = root / "monitor-journal.json"
        original_run = warm.subprocess.run
        original_load_policy = warm.load_policy
        original_monitor_readback = warm.read_root_storage_status_artifact
        start_count = 0

        def start_once(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal start_count
            start_count += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        warm.subprocess.run = start_once
        warm.load_policy = lambda: {}
        warm.read_root_storage_status_artifact = lambda **_kwargs: {
            "ok": True,
            "fresh": True,
            "status": {"collected_at": "2099-01-01T00:00:00Z"},
        }
        try:
            first_monitor = warm._monitor_after_batch(
                journal=monitor_journal,
                journal_path=monitor_journal_path,
            )
            repeated_monitor = warm._monitor_after_batch(
                journal=monitor_journal,
                journal_path=monitor_journal_path,
            )
        finally:
            warm.subprocess.run = original_run
            warm.load_policy = original_load_policy
            warm.read_root_storage_status_artifact = original_monitor_readback
        assert start_count == 1
        assert first_monitor["phase"] == "complete"
        assert repeated_monitor["idempotent"] is True

    print("root_storage_warm_archive_smoke: ok")


if __name__ == "__main__":
    run()
