#!/usr/bin/env python3
"""Synthetic/fixture checks for the root-storage and journald contracts."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import root_storage_policy as app
from packages.application import root_storage_policy as policy_module
from packages.application.root_storage_policy import (
    GIB,
    RootStoragePolicyError,
    admit_root_write,
    collect_root_storage_status,
    load_policy,
    predict_sqlite_backup_bytes,
    storage_level,
)


def main() -> int:
    policy = load_policy()
    _assert_thresholds(policy)
    _assert_admission(policy)
    _assert_unregistered_detection(policy)
    _assert_journal_manifest(policy)
    _assert_one_shot_activation(policy)
    _assert_reconciliation(policy)
    _assert_static_safety()
    print("root_storage_policy_smoke: ok")
    return 0


def _assert_thresholds(policy: dict[str, object]) -> None:
    assert storage_level(25 * GIB) == "normal"
    assert storage_level(24 * GIB) == "below_normal"
    assert storage_level(19 * GIB) == "warning"
    assert storage_level(14 * GIB) == "critical"
    assert storage_level(11 * GIB) == "hard"
    assert policy["thresholds_bytes"] == {
        "normal_available": 25 * GIB,
        "warning_below": 20 * GIB,
        "critical_below": 15 * GIB,
        "hard_deny_below": 12 * GIB,
        "large_output": 256 * 1024**2,
        "large_predicted_free_after_floor": 15 * GIB,
    }


def _assert_admission(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        destination = root / "out" / "backup.sqlite3"
        (root / "out").mkdir()
        sqlite_source = root / "source.sqlite3"
        sqlite_source.write_bytes(b"m" * 10)
        Path(str(sqlite_source) + "-wal").write_bytes(b"w" * 7)
        assert predict_sqlite_backup_bytes(sqlite_source) == 17

        with mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=11 * GIB, f_frsize=1),
        ):
            try:
                admit_root_write(
                    owner="ads_historical_recovery",
                    destination=destination,
                    predicted_output_bytes=1,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError as exc:
                assert "below_hard_deny" in str(exc)
            else:
                raise AssertionError("hard root threshold admitted discretionary output")
            essential = admit_root_write(
                owner="registry_operational_store",
                destination=destination,
                predicted_output_bytes=1,
                policy=policy,
                root_path=root,
            )
            assert essential["allowed"] is True
            assert essential["storage_level"] == "hard"

        with mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(f_bavail=16 * GIB, f_frsize=1),
        ):
            try:
                admit_root_write(
                    owner="buyout_mature_backfill",
                    destination=destination,
                    predicted_output_bytes=2 * GIB,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError as exc:
                assert "predicted_free_after_below_critical_floor" in str(exc)
            else:
                raise AssertionError("large predicted-free floor admitted unsafe output")

        for owner, predicted, destination_value in (
            ("", 1, destination),
            ("unknown-owner", 1, destination),
            ("ads_historical_recovery", None, destination),
            ("ads_historical_recovery", 1, Path("relative.sqlite3")),
        ):
            try:
                admit_root_write(
                    owner=owner,
                    destination=destination_value,
                    predicted_output_bytes=predicted,
                    policy=policy,
                    root_path=root,
                )
            except RootStoragePolicyError:
                pass
            else:
                raise AssertionError("unknown admission identity/output/destination did not fail closed")


def _assert_unregistered_detection(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        scan = root / "scan"
        scan.mkdir()
        registered = scan / "registered" / "known.bin"
        registered.parent.mkdir()
        unregistered = scan / "new-producer" / "unknown.bin"
        unregistered.parent.mkdir()
        _sparse_file(registered, 256 * 1024**2)
        _sparse_file(unregistered, 256 * 1024**2)
        fixture = deepcopy(policy)
        fixture["filesystems"] = {"root": str(root)}
        fixture["scan_roots"] = [str(scan)]
        fixture["producers"] = [
            {
                "owner": "known",
                "classification": "discretionary_root_writer",
                "path_patterns": [str(scan / "registered" / "**")],
            }
        ]
        with mock.patch.object(
            policy_module,
            "_mountinfo_for_path",
            return_value={
                "mount_id": 1,
                "mount_point": str(root),
                "source": "/dev/fixture",
                "filesystem_type": "ext4",
                "mount_options": "rw",
            },
        ), mock.patch.object(policy_module, "_filesystem_uuid", return_value="fixture-uuid"), mock.patch.object(
            policy_module.os,
            "statvfs",
            return_value=SimpleNamespace(
                f_bavail=30 * GIB,
                f_bfree=30 * GIB,
                f_blocks=40 * GIB,
                f_frsize=1,
                f_files=1000,
                f_ffree=900,
                f_favail=900,
            ),
        ):
            status = collect_root_storage_status(policy=fixture, root_path=root)
        assert len(status["large_root_files"]) == 2
        assert [item["path"] for item in status["unregistered_large_root_files"]] == [
            str(unregistered)
        ]
        assert status["alerts"] == [
            {"code": "unregistered_large_root_producer", "severity": "critical", "count": 1}
        ]


def _assert_journal_manifest(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        machine_id = "a" * 32
        machine_path = root / "machine-id"
        machine_path.write_text(machine_id + "\n", encoding="utf-8")
        journal_root = root / "journal"
        current_dir = journal_root / machine_id
        foreign_dir = journal_root / ("b" * 32)
        current_dir.mkdir(parents=True)
        foreign_dir.mkdir(parents=True)
        eligible = current_dir / "system@eligible.journal"
        held = current_dir / "system@held.journal"
        opened = current_dir / "system@opened.journal"
        younger = current_dir / "system@younger.journal"
        current = current_dir / "system.journal"
        foreign = foreign_dir / "system.journal"
        non_journal = current_dir / "corrupt.journal~"
        for path in (eligible, held, opened, younger, current, foreign, non_journal):
            path.write_bytes(path.name.encode("utf-8"))
        now = datetime(2026, 8, 26, tzinfo=timezone.utc)
        cutoff = int(now.timestamp() * 1_000_000) - 14 * 24 * 60 * 60 * 1_000_000
        old_tail = cutoff - 1
        new_tail = cutoff + 1
        headers = {
            eligible: _header("ARCHIVED", machine_id, old_tail),
            held: _header("ARCHIVED", machine_id, old_tail),
            opened: _header("ARCHIVED", machine_id, old_tail),
            younger: _header("ARCHIVED", machine_id, new_tail),
            current: _header("ONLINE", machine_id, new_tail),
            foreign: _header("ONLINE", "b" * 32, old_tail),
        }
        opened_stat = opened.stat()
        hold_registry = root / "holds.json"
        hold_registry.write_text(
            json.dumps(
                {
                    "schema_version": app.JOURNAL_HOLD_CONTRACT,
                    "holds": [
                        {
                            "hold_id": "incident-1",
                            "kind": "incident",
                            "active": True,
                            "paths": [str(held)],
                            "reference": "INC-1",
                            "reason": "preserve exact incident evidence",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fixture = deepcopy(policy)
        fixture["journald"]["hold_registry"] = str(hold_registry)
        manifest = app.build_journal_preflight(
            fixture,
            now=now,
            machine_id_path=machine_path,
            journal_root=journal_root,
            header_reader=lambda path: headers[path],
            opener_reader=lambda: {
                (int(opened_stat.st_dev), int(opened_stat.st_ino)): [
                    {"pid": 10, "comm": "systemd-journald", "fd": "7", "target": str(opened)}
                ]
            },
        )
        assert [item["path"] for item in manifest["eligible_entries"]] == [
            str(eligible),
            str(opened),
        ]
        assert [item["path"] for item in manifest["held_eligible_entries"]] == [str(held)]
        classifications = {item["path"]: item["archive_classification"] for item in manifest["entries"]}
        assert classifications[str(opened)] == "eligible_expired_archived_journal"
        assert classifications[str(younger)] == "archived_younger_than_retention"
        assert classifications[str(current)] == "current_or_online_journal"
        assert classifications[str(foreign)] == "foreign_machine_journal"
        assert classifications[str(non_journal)] == "non_journal_file"
        assert manifest["manifest_digest"].startswith("sha256:")
        app._assert_preflight_fresh(fixture, manifest, now=now)


def _assert_one_shot_activation(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.conf"
        destination = root / "etc" / "journald.conf.d" / "60-wb.conf"
        evidence = root / "evidence"
        source.write_text("[Journal]\nSystemMaxUse=2G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n", encoding="utf-8")
        fixture = deepcopy(policy)
        fixture["journald"]["configuration_source"] = "fixture.conf"
        fixture["journald"]["configuration_destination"] = str(destination)
        fixture["journald"]["evidence_directory"] = str(evidence)
        preflight = {
            "contract_version": app.JOURNAL_ACTIVATION_CONTRACT,
            "eligible_entries": [],
            "held_eligible_entries": [],
            "protected_entries": [],
            "eligible_count": 0,
            "eligible_bytes": 0,
            "protected_non_target_digest": app._protected_digest([]),
        }
        preflight["manifest_digest"] = app._digest_payload(preflight)
        completed = subprocess.CompletedProcess(
            ["systemctl", "restart", "systemd-journald.service"], 0, "", ""
        )
        readback = {"ok": True, "operation_id": "fixture", "service_after": {"main_pid": 2}}
        with mock.patch.object(app, "_resolve_repo_path", return_value=source), mock.patch.object(
            app, "_systemd_semantics_evidence", return_value={"systemd_version": "systemd 255"}
        ), mock.patch.object(app, "build_journal_preflight", return_value=preflight), mock.patch.object(
            app, "_assert_preflight_fresh"
        ), mock.patch.object(
            app, "_journald_service_identity", return_value={"main_pid": 1, "exec_main_start_timestamp": "before"}
        ), mock.patch.object(app.subprocess, "run", return_value=completed) as runner, mock.patch.object(
            app, "_wait_for_journald_readback", return_value=readback
        ), mock.patch.object(app, "readback_journald_retention", return_value=readback):
            first = app.activate_journald_retention(fixture)
            second = app.activate_journald_retention(fixture)
        restart_calls = [
            call
            for call in runner.call_args_list
            if call.args and call.args[0] == ["systemctl", "restart", "systemd-journald.service"]
        ]
        assert len(restart_calls) == 1
        assert first["activation_retried"] is False
        assert second["idempotent"] is True


def _assert_reconciliation(policy: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "source.conf"
        destination = root / "destination.conf"
        config = b"[Journal]\nSystemMaxUse=2G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n"
        source.write_bytes(config)
        destination.write_bytes(config)
        desired_digest = app._sha256_file(source)
        operation_id = f"journald-retention-{desired_digest.removeprefix('sha256:')[:24]}"
        evidence = root / "evidence"
        operation_dir = evidence / "activations" / operation_id
        operation_dir.mkdir(parents=True)
        deleted_path = root / "deleted.journal"
        protected_path = root / "protected.journal"
        protected_path.write_bytes(b"protected")
        protected_stat = protected_path.stat()
        eligible = {
            "path": str(deleted_path),
            "device": int(protected_stat.st_dev),
            "inode": 999999,
            "size_bytes": 123,
            "mtime_ns": 1,
            "tail_realtime_epoch_us": 1,
            "archive_classification": "eligible_expired_archived_journal",
        }
        protected = {
            "path": str(protected_path),
            "device": int(protected_stat.st_dev),
            "inode": int(protected_stat.st_ino),
            "size_bytes": int(protected_stat.st_size),
            "mtime_ns": int(protected_stat.st_mtime_ns),
            "tail_realtime_epoch_us": 2,
            "archive_classification": "archived_younger_than_retention",
        }
        manifest = {
            "contract_version": app.JOURNAL_ACTIVATION_CONTRACT,
            "journal_root": str(root),
            "eligible_entries": [eligible],
            "held_eligible_entries": [],
            "protected_entries": [protected],
            "eligible_count": 1,
            "eligible_bytes": 123,
            "protected_non_target_digest": app._protected_digest([protected]),
        }
        manifest["manifest_digest"] = app._digest_payload(manifest)
        state = {
            "contract_version": app.JOURNAL_ACTIVATION_CONTRACT,
            "operation_id": operation_id,
            "phase": "restart_submit_intent",
            "desired_config_sha256": desired_digest,
            "manifest_digest": manifest["manifest_digest"],
            "restart_submit_count": 1,
            "service_before": {"main_pid": 1, "exec_main_start_timestamp": "before"},
        }
        app._write_json_atomic(operation_dir / "preflight-manifest.json", manifest, mode=0o600)
        app._write_json_atomic(operation_dir / "state.json", state, mode=0o600)
        fixture = deepcopy(policy)
        fixture["journald"]["configuration_source"] = "fixture.conf"
        fixture["journald"]["configuration_destination"] = str(destination)
        fixture["journald"]["evidence_directory"] = str(evidence)
        with mock.patch.object(app, "_resolve_repo_path", return_value=source), mock.patch.object(
            app,
            "_effective_journald_config",
            return_value={"matches_expected": True},
        ), mock.patch.object(
            app,
            "_journald_service_identity",
            return_value={"main_pid": 2, "exec_main_start_timestamp": "after"},
        ), mock.patch.object(
            app,
            "collect_root_storage_status",
            return_value={"status": "hard"},
        ), mock.patch.object(
            app.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(["journalctl", "--disk-usage"], 0, "Archived journals take up 1G", ""),
        ):
            readback = app.readback_journald_retention(fixture)
        assert readback["ok"] is True
        assert readback["deleted_count"] == 1
        assert readback["deleted_bytes"] == 123
        assert readback["protected_non_target_digest_matches"] is True
        assert not readback["protected_missing"]


def _assert_static_safety() -> None:
    source = Path(app.__file__).read_text(encoding="utf-8")
    app._validate_journald_config_bytes(
        b"[Journal]\nSystemMaxUse=2G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n"
    )
    try:
        app._validate_journald_config_bytes(
            b"[Journal]\nSystemMaxUse=3G\nSystemKeepFree=15G\nMaxRetentionSec=14day\n"
        )
    except RootStoragePolicyError:
        pass
    else:
        raise AssertionError("journald setting drift did not fail before activation")
    assert "journalctl\", \"--vacuum" not in source
    assert "unlink" not in source.split("def activate_journald_retention", 1)[1].split("def build_journal_preflight", 1)[0]
    assert source.count('["systemctl", "restart", "systemd-journald.service"]') == 1


def _header(state: str, machine_id: str, tail: int) -> dict[str, object]:
    return {
        "state": state,
        "file_id": f"file-{tail}",
        "machine_id": machine_id,
        "head_realtime_epoch_us": tail - 1,
        "tail_realtime_epoch_us": tail,
    }


def _sparse_file(path: Path, size: int) -> None:
    with path.open("wb") as handle:
        handle.seek(size - 1)
        handle.write(b"\0")


if __name__ == "__main__":
    raise SystemExit(main())
