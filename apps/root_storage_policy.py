#!/usr/bin/env python3
"""Root-storage status/admission and versioned journald operations."""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.root_storage_policy import (
    CONTRACT_VERSION,
    RootStoragePolicyError,
    admit_root_write,
    collect_root_storage_status,
    load_policy,
    read_root_storage_status_artifact,
)


JOURNAL_ACTIVATION_CONTRACT = "wb_core_journald_retention_activation_v1"
JOURNAL_CORRECTION_CONTRACT = "wb_core_journald_retention_correction_v1"
JOURNAL_CORRECTION_MODE = "remove_block_003_dropin"
JOURNAL_HOLD_CONTRACT = "wb_core_journal_retention_holds_v1"
_HEADER_HEX_TIMESTAMP = re.compile(
    r"^(Head|Tail) realtime timestamp: .*\(([0-9a-fA-F]+)\)$"
)
_SYSTEMD_VERSION = re.compile(r"^systemd\s+(\d+)\b")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-file", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--output", type=Path)
    status.add_argument("--fail-on-unregistered", action="store_true")
    status.add_argument(
        "--allow-recovery-scratch-bootstrap-pending",
        action="store_true",
    )
    status.add_argument("--recovery-scratch-release-bridge")

    status_readback = subparsers.add_parser("status-readback")
    status_readback.add_argument(
        "--allow-recovery-scratch-bootstrap-pending",
        action="store_true",
    )
    status_readback.add_argument("--recovery-scratch-release-bridge")

    admission = subparsers.add_parser("admission")
    admission.add_argument("--owner", required=True)
    admission.add_argument("--destination", type=Path, required=True)
    admission.add_argument("--predicted-output-bytes", type=int, required=True)
    admission.add_argument("--predicted-temporary-bytes", type=int, default=0)
    admission.add_argument("--predicted-readback-bytes", type=int, default=0)
    admission.add_argument("--control-reserve-bytes", type=int, default=0)

    subparsers.add_parser("journald-activate")
    subparsers.add_parser("journald-readback")
    subparsers.add_parser("journald-corrective-remove")
    subparsers.add_parser("journald-corrective-readback")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        policy = load_policy(args.policy_file)
        bridge = None
        if getattr(args, "recovery_scratch_release_bridge", None):
            try:
                bridge = json.loads(
                    base64.b64decode(
                        args.recovery_scratch_release_bridge,
                        validate=True,
                    ).decode("utf-8")
                )
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RootStoragePolicyError(
                    "recovery scratch release bridge encoding is invalid"
                ) from exc
            if not isinstance(bridge, dict):
                raise RootStoragePolicyError(
                    "recovery scratch release bridge must be an object"
                )
        if args.command == "status":
            result = collect_root_storage_status(
                policy=policy,
                allow_recovery_scratch_bootstrap_pending=(
                    args.allow_recovery_scratch_bootstrap_pending
                ),
                recovery_scratch_release_bridge=bridge,
            )
            if args.output:
                _write_json_atomic(args.output, result, mode=0o644)
            print(_canonical_json(result))
            if args.fail_on_unregistered and result["unregistered_large_root_files"]:
                return 2
            return 0
        if args.command == "status-readback":
            result = read_root_storage_status_artifact(
                policy=policy,
                allow_recovery_scratch_bootstrap_pending=(
                    args.allow_recovery_scratch_bootstrap_pending
                ),
                recovery_scratch_release_bridge=bridge,
            )
            print(_canonical_json(result))
            return 0 if result.get("ok") else 3
        if args.command == "admission":
            result = admit_root_write(
                owner=args.owner,
                destination=args.destination,
                predicted_output_bytes=args.predicted_output_bytes,
                predicted_temporary_bytes=args.predicted_temporary_bytes,
                predicted_readback_bytes=args.predicted_readback_bytes,
                control_reserve_bytes=args.control_reserve_bytes,
                policy=policy,
            )
            print(_canonical_json(result))
            return 0
        if args.command == "journald-activate":
            print(_canonical_json(activate_journald_retention(policy)))
            return 0
        if args.command == "journald-readback":
            result = readback_journald_retention(policy)
            print(_canonical_json(result))
            return 0 if result.get("ok") else 3
        if args.command == "journald-corrective-remove":
            print(_canonical_json(remove_journald_retention_dropin(policy)))
            return 0
        if args.command == "journald-corrective-readback":
            result = readback_journald_correction(policy)
            print(_canonical_json(result))
            return 0 if result.get("ok") else 3
    except (RootStoragePolicyError, ValueError) as exc:
        print(_canonical_json({"ok": False, "error": str(exc), "command": args.command}))
        return 2
    raise AssertionError("unreachable root storage policy command")


def remove_journald_retention_dropin(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the exact block-003 drop-in and submit one journald restart."""

    correction = _journald_correction_policy(policy)
    destination = Path(str(correction["configuration_destination"]))
    evidence_dir = Path(str(correction["evidence_directory"]))
    correction_digest = _digest_payload(correction)
    operation_id = f"journald-correction-{correction_digest.removeprefix('sha256:')[:24]}"
    operation_dir = evidence_dir / "corrections" / operation_id
    state_path = operation_dir / "state.json"
    manifest_path = operation_dir / "preflight-manifest.json"
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evidence_dir, 0o700)
    lock_path = evidence_dir / "correction.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if state_path.exists():
            state = _read_json(state_path)
            if state.get("correction_digest") != correction_digest:
                raise RootStoragePolicyError("journald correction operation digest drift")
            readback = readback_journald_correction(policy)
            if readback.get("ok"):
                return {**readback, "idempotent": True, "operation_retried": False}
            raise RootStoragePolicyError(
                "journald correction is already submitted and did not reconcile; "
                "do not retry removal or restart, inspect journald-corrective-readback"
            )

        operation_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        preflight = build_journald_correction_preflight(policy)
        _write_json_atomic(manifest_path, preflight, mode=0o600)
        prepared = {
            "contract_version": JOURNAL_CORRECTION_CONTRACT,
            "operation_id": operation_id,
            "phase": "prepared",
            "created_at": _utc_now(),
            "correction_digest": correction_digest,
            "legacy_activation_operation_id": correction[
                "legacy_activation_operation_id"
            ],
            "configuration_destination": str(destination),
            "legacy_configuration_sha256": correction[
                "legacy_configuration_sha256"
            ],
            "manifest_path": str(manifest_path),
            "manifest_digest": preflight["manifest_digest"],
            "journal_inventory_digest_before": preflight[
                "journal_inventory_digest"
            ],
            "protected_identity_digest_before": preflight[
                "protected_identity_digest"
            ],
            "service_before": preflight["service_before"],
            "dropin_unlink_submit_count": 0,
            "restart_submit_count": 0,
        }
        _write_json_atomic(state_path, prepared, mode=0o600)
        _assert_journald_correction_preflight_fresh(policy, preflight)
        removal_intent = {
            **prepared,
            "phase": "dropin_removal_submit_intent",
            "dropin_unlink_submit_count": 1,
            "dropin_removal_submit_recorded_at": _utc_now(),
        }
        _write_json_atomic(state_path, removal_intent, mode=0o600)
        destination.unlink()
        _fsync_directory(destination.parent)
        removed = {
            **removal_intent,
            "phase": "dropin_removed",
            "dropin_removed_at": _utc_now(),
        }
        _write_json_atomic(state_path, removed, mode=0o600)
        restart_intent = {
            **removed,
            "phase": "restart_submit_intent",
            "restart_submit_count": 1,
            "restart_submit_recorded_at": _utc_now(),
        }
        _write_json_atomic(state_path, restart_intent, mode=0o600)
        completed = subprocess.run(
            ["systemctl", "restart", "systemd-journald.service"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RootStoragePolicyError(
                "journald corrective restart returned nonzero after one submit; do not retry: "
                + _bounded_text(completed.stderr or completed.stdout)
            )
        readback = _wait_for_journald_correction_readback(
            policy,
            before_service=preflight["service_before"],
        )
        if not readback.get("ok"):
            raise RootStoragePolicyError(
                "journald correction did not reconcile after one submit; "
                "do not retry removal or restart"
            )
        done = {
            **restart_intent,
            "phase": "done",
            "completed_at": _utc_now(),
            "service_after": readback["service_after_attributed"],
            "readback_digest": _digest_payload(readback),
            "completion_readback": readback,
        }
        _write_json_atomic(state_path, done, mode=0o600)
        return {
            **readback,
            "phase": "done",
            "idempotent": False,
            "operation_retried": False,
        }


def build_journald_correction_preflight(
    policy: Mapping[str, Any],
    *,
    inventory_reader: Any = None,
) -> dict[str, Any]:
    correction = _journald_correction_policy(policy)
    destination = Path(str(correction["configuration_destination"]))
    _assert_exact_legacy_dropin(destination, correction)
    effective = _effective_journald_config(
        expected=dict(correction["legacy_effective_values"])
    )
    if not effective["matches_expected"]:
        raise RootStoragePolicyError(
            "journald effective configuration drifted before corrective removal"
        )
    journal_root = Path(str(correction["journal_root"]))
    entries = (inventory_reader or _collect_correction_journal_inventory)(journal_root)
    service_before = _journald_service_identity()
    if (
        service_before.get("active_state") != "active"
        or not service_before.get("main_pid")
    ):
        raise RootStoragePolicyError("journald must be active before corrective removal")
    root_status = collect_root_storage_status(policy=policy)
    payload = {
        "contract_version": JOURNAL_CORRECTION_CONTRACT,
        "observed_at": _utc_now(),
        "legacy_activation_operation_id": correction[
            "legacy_activation_operation_id"
        ],
        "configuration_destination": str(destination),
        "legacy_configuration_sha256": correction["legacy_configuration_sha256"],
        "effective_config_before": effective,
        "journal_root": str(journal_root),
        "journal_entries": entries,
        "journal_entry_count": len(entries),
        "journal_file_count": sum(1 for item in entries if item["is_journal_file"]),
        "non_journal_file_count": sum(
            1 for item in entries if not item["is_journal_file"]
        ),
        "journal_total_bytes": sum(int(item["size_bytes"]) for item in entries),
        "journal_inventory_digest": _journal_inventory_digest(entries),
        "protected_identity_digest": _journal_identity_digest(entries),
        "service_before": service_before,
        "root_storage_status_before": root_status,
    }
    payload["manifest_digest"] = _digest_payload(payload)
    return payload


def readback_journald_correction(policy: Mapping[str, Any]) -> dict[str, Any]:
    correction = _journald_correction_policy(policy)
    correction_digest = _digest_payload(correction)
    operation_id = f"journald-correction-{correction_digest.removeprefix('sha256:')[:24]}"
    operation_dir = (
        Path(str(correction["evidence_directory"])) / "corrections" / operation_id
    )
    state_path = operation_dir / "state.json"
    manifest_path = operation_dir / "preflight-manifest.json"
    if not state_path.is_file() or not manifest_path.is_file():
        return {
            "ok": False,
            "contract_version": JOURNAL_CORRECTION_CONTRACT,
            "operation_id": operation_id,
            "reason": "correction_evidence_absent",
        }
    state = _read_json(state_path)
    manifest = _read_json(manifest_path)
    if state.get("operation_id") != operation_id:
        raise RootStoragePolicyError("journald correction operation identity drift")
    if state.get("correction_digest") != correction_digest:
        raise RootStoragePolicyError("journald correction policy digest drift")
    if manifest.get("manifest_digest") != _digest_payload(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    ):
        raise RootStoragePolicyError("journald correction preflight manifest digest mismatch")
    if state.get("manifest_digest") != manifest.get("manifest_digest"):
        raise RootStoragePolicyError("journald correction state/manifest mismatch")

    if state.get("phase") == "done":
        completion = state.get("completion_readback")
        if not isinstance(completion, dict) or not completion.get("ok"):
            raise RootStoragePolicyError(
                "journald correction durable completion readback is invalid"
            )
        if state.get("readback_digest") != _digest_payload(completion):
            raise RootStoragePolicyError(
                "journald correction durable completion readback digest mismatch"
            )
        destination = Path(str(correction["configuration_destination"]))
        destination_absent = not destination.exists() and not destination.is_symlink()
        effective = _effective_journald_config(
            expected=dict(correction["expected_effective_values_after"])
        )
        service_current = _journald_service_identity()
        disk_usage = subprocess.run(
            ["journalctl", "--disk-usage"],
            text=True,
            capture_output=True,
            check=False,
        )
        current_ok = bool(
            destination_absent
            and effective["matches_expected"]
            and service_current.get("active_state") == "active"
            and service_current.get("sub_state") == "running"
        )
        return {
            **completion,
            "ok": bool(completion.get("ok") and current_ok),
            "phase": "done",
            "dropin_absent": destination_absent,
            "effective_config": effective,
            "service_current": service_current,
            "journal_disk_usage": _bounded_text(
                disk_usage.stdout or disk_usage.stderr
            ),
            "root_storage_status_after": collect_root_storage_status(policy=policy),
            "durable_completion_readback_digest": state["readback_digest"],
            "durable_completion_reused": True,
        }

    destination = Path(str(correction["configuration_destination"]))
    destination_absent = not destination.exists() and not destination.is_symlink()
    effective = _effective_journald_config(
        expected=dict(correction["expected_effective_values_after"])
    )
    service_current = _journald_service_identity()
    service_before = dict(manifest.get("service_before") or {})
    service_after_attributed = dict(state.get("service_after") or service_current)
    pid_transition = bool(
        service_before.get("main_pid")
        and service_after_attributed.get("main_pid")
        and (
            service_after_attributed.get("main_pid") != service_before.get("main_pid")
            or service_after_attributed.get("exec_main_start_timestamp")
            != service_before.get("exec_main_start_timestamp")
        )
    )
    entries_after = _collect_correction_journal_inventory(
        Path(str(correction["journal_root"]))
    )
    reconciliation = _reconcile_correction_journal_inventory(
        list(manifest.get("journal_entries") or []),
        entries_after,
    )
    disk_usage = subprocess.run(
        ["journalctl", "--disk-usage"],
        text=True,
        capture_output=True,
        check=False,
    )
    root_status = collect_root_storage_status(policy=policy)
    removal_recorded_at = str(state.get("dropin_removed_at") or "")
    restart_recorded_at = str(state.get("restart_submit_recorded_at") or "")
    removal_precedes_restart = bool(
        removal_recorded_at
        and restart_recorded_at
        and removal_recorded_at <= restart_recorded_at
    )
    ok = bool(
        state.get("dropin_unlink_submit_count") == 1
        and state.get("restart_submit_count") == 1
        and destination_absent
        and effective["matches_expected"]
        and removal_precedes_restart
        and pid_transition
        and service_current.get("active_state") == "active"
        and service_current.get("sub_state") == "running"
        and not reconciliation["deleted_entries"]
        and not reconciliation["protected_drift"]
        and reconciliation["protected_identity_digest_matches"]
    )
    return {
        "ok": ok,
        "contract_version": JOURNAL_CORRECTION_CONTRACT,
        "operation_id": operation_id,
        "phase": state.get("phase"),
        "legacy_activation_operation_id": correction[
            "legacy_activation_operation_id"
        ],
        "manifest_path": str(manifest_path),
        "manifest_digest": manifest["manifest_digest"],
        "dropin_path": str(destination),
        "dropin_absent": destination_absent,
        "dropin_unlink_submit_count": state.get("dropin_unlink_submit_count"),
        "dropin_removed_at": state.get("dropin_removed_at"),
        "restart_submit_count": state.get("restart_submit_count"),
        "restart_submit_recorded_at": state.get("restart_submit_recorded_at"),
        "dropin_removal_precedes_restart": removal_precedes_restart,
        "effective_config": effective,
        "service_before": service_before,
        "service_after_attributed": service_after_attributed,
        "service_current": service_current,
        "pid_transition_count": 1 if pid_transition else 0,
        "journal_inventory_before": {
            "entry_count": manifest["journal_entry_count"],
            "journal_file_count": manifest["journal_file_count"],
            "non_journal_file_count": manifest["non_journal_file_count"],
            "total_bytes": manifest["journal_total_bytes"],
            "inventory_digest": manifest["journal_inventory_digest"],
            "protected_identity_digest": manifest["protected_identity_digest"],
        },
        "journal_inventory_after": {
            "entry_count": len(entries_after),
            "journal_file_count": sum(
                1 for item in entries_after if item["is_journal_file"]
            ),
            "non_journal_file_count": sum(
                1 for item in entries_after if not item["is_journal_file"]
            ),
            "total_bytes": sum(int(item["size_bytes"]) for item in entries_after),
            "inventory_digest": _journal_inventory_digest(entries_after),
            "protected_identity_digest": reconciliation[
                "protected_identity_digest_after"
            ],
        },
        **reconciliation,
        "journal_disk_usage": _bounded_text(disk_usage.stdout or disk_usage.stderr),
        "root_storage_status_before": manifest["root_storage_status_before"],
        "root_storage_status_after": root_status,
    }


def activate_journald_retention(policy: Mapping[str, Any]) -> dict[str, Any]:
    journald = _journald_policy(policy)
    source = _resolve_repo_path(str(journald["configuration_source"]))
    destination = Path(str(journald["configuration_destination"]))
    evidence_dir = Path(str(journald["evidence_directory"]))
    desired_bytes = source.read_bytes()
    _validate_journald_config_bytes(desired_bytes)
    desired_digest = _sha256_bytes(desired_bytes)
    operation_id = f"journald-retention-{desired_digest.removeprefix('sha256:')[:24]}"
    operation_dir = evidence_dir / "activations" / operation_id
    state_path = operation_dir / "state.json"
    lock_path = evidence_dir / "activation.lock"
    evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(evidence_dir, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "r+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        if state_path.exists():
            state = _read_json(state_path)
            if state.get("desired_config_sha256") != desired_digest:
                raise RootStoragePolicyError("journald activation operation digest drift")
            readback = readback_journald_retention(policy)
            if readback.get("ok"):
                return {**readback, "idempotent": True, "activation_retried": False}
            raise RootStoragePolicyError(
                "journald activation is already submitted and did not reconcile; "
                "do not retry activation, inspect journald-readback"
            )

        operation_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        systemd_evidence = _systemd_semantics_evidence(journald)
        preflight = build_journal_preflight(policy)
        manifest_path = operation_dir / "preflight-manifest.json"
        _write_json_atomic(manifest_path, preflight, mode=0o600)
        if preflight["held_eligible_entries"]:
            callback = {
                "action": "clear_or_expire_the_exact_active_journal_hold_then_start_a_new_versioned_activation",
                "held_entries": [
                    {
                        "path": item["path"],
                        "inode": item["inode"],
                        "holds": item["hold_evidence"]["matching_holds"],
                    }
                    for item in preflight["held_eligible_entries"]
                ],
            }
            blocked = {
                "contract_version": JOURNAL_ACTIVATION_CONTRACT,
                "operation_id": operation_id,
                "phase": "blocked_by_active_hold",
                "desired_config_sha256": desired_digest,
                "manifest_path": str(manifest_path),
                "manifest_digest": preflight["manifest_digest"],
                "callback": callback,
            }
            _write_json_atomic(state_path, blocked, mode=0o600)
            raise RootStoragePolicyError(_canonical_json(callback))

        before_service = _journald_service_identity()
        prepared = {
            "contract_version": JOURNAL_ACTIVATION_CONTRACT,
            "operation_id": operation_id,
            "phase": "prepared",
            "created_at": _utc_now(),
            "desired_config_sha256": desired_digest,
            "configuration_destination": str(destination),
            "manifest_path": str(manifest_path),
            "manifest_digest": preflight["manifest_digest"],
            "eligible_count": len(preflight["eligible_entries"]),
            "eligible_bytes": sum(int(item["size_bytes"]) for item in preflight["eligible_entries"]),
            "protected_non_target_digest": preflight["protected_non_target_digest"],
            "systemd_semantics": systemd_evidence,
            "service_before": before_service,
            "restart_submit_count": 0,
        }
        _write_json_atomic(state_path, prepared, mode=0o600)
        _assert_preflight_fresh(policy, preflight)
        _install_exact_config(destination, desired_bytes)
        submit_intent = {
            **prepared,
            "phase": "restart_submit_intent",
            "config_installed_sha256": _sha256_file(destination),
            "restart_submit_count": 1,
            "restart_submit_recorded_at": _utc_now(),
        }
        _write_json_atomic(state_path, submit_intent, mode=0o600)
        completed = subprocess.run(
            ["systemctl", "restart", "systemd-journald.service"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RootStoragePolicyError(
                "journald restart returned nonzero after one submit; do not retry: "
                + _bounded_text(completed.stderr or completed.stdout)
            )
        readback = _wait_for_journald_readback(policy, before_service=before_service)
        if not readback.get("ok"):
            raise RootStoragePolicyError(
                "journald activation did not reconcile after one submit; do not retry activation"
            )
        done = {
            **submit_intent,
            "phase": "done",
            "completed_at": _utc_now(),
            "readback": readback,
        }
        _write_json_atomic(state_path, done, mode=0o600)
        return {**readback, "idempotent": False, "activation_retried": False}


def build_journal_preflight(
    policy: Mapping[str, Any],
    *,
    now: datetime | None = None,
    machine_id_path: Path = Path("/etc/machine-id"),
    journal_root: Path = Path("/var/log/journal"),
    header_reader: Any = None,
    opener_reader: Any = None,
) -> dict[str, Any]:
    journald = _journald_policy(policy)
    machine_id = machine_id_path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{32}", machine_id):
        raise RootStoragePolicyError("machine-id is invalid")
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    cutoff_epoch_us = int(observed_at.timestamp() * 1_000_000) - int(
        journald["max_retention_seconds"]
    ) * 1_000_000
    hold_registry = _load_hold_registry(Path(str(journald["hold_registry"])), now=observed_at)
    openers = (opener_reader or _journal_openers)()
    entries: list[dict[str, Any]] = []
    for path in sorted(journal_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        stat_before = path.stat()
        is_journal_file = path.name.endswith(".journal")
        header = (
            (header_reader or _journal_header)(path)
            if is_journal_file
            else {
                "state": None,
                "file_id": None,
                "machine_id": None,
                "head_realtime_epoch_us": None,
                "tail_realtime_epoch_us": None,
            }
        )
        stat_after = path.stat()
        if _stat_identity(stat_before) != _stat_identity(stat_after):
            raise RootStoragePolicyError(f"journal changed during preflight: {path}")
        file_openers = openers.get((int(stat_before.st_dev), int(stat_before.st_ino)), [])
        archived_filename = "@" in path.name
        current_machine_directory = path.parent.name == machine_id
        header_current_machine = header["machine_id"] == machine_id
        archived_state = header["state"] == "ARCHIVED"
        tail_before_cutoff = (
            header["tail_realtime_epoch_us"] is not None
            and int(header["tail_realtime_epoch_us"]) < cutoff_epoch_us
        )
        matching_holds = _matching_holds(
            hold_registry,
            path=path,
            device=int(stat_before.st_dev),
            inode=int(stat_before.st_ino),
        )
        aged_archived = bool(
            archived_filename
            and current_machine_directory
            and header_current_machine
            and archived_state
            and tail_before_cutoff
        )
        eligible = is_journal_file and aged_archived and not matching_holds
        if not is_journal_file:
            classification = "non_journal_file"
        elif not current_machine_directory or not header_current_machine:
            classification = "foreign_machine_journal"
        elif not archived_filename or not archived_state:
            classification = "current_or_online_journal"
        elif not tail_before_cutoff:
            classification = "archived_younger_than_retention"
        elif matching_holds:
            classification = "archived_aged_held"
        else:
            classification = "eligible_expired_archived_journal"
        entries.append(
            {
                "path": str(path),
                "device": int(stat_before.st_dev),
                "inode": int(stat_before.st_ino),
                "size_bytes": int(stat_before.st_size),
                "mtime_ns": int(stat_before.st_mtime_ns),
                "journal_state": header["state"],
                "journal_file_id": header["file_id"],
                "journal_machine_id": header["machine_id"],
                "head_realtime_epoch_us": header["head_realtime_epoch_us"],
                "tail_realtime_epoch_us": header["tail_realtime_epoch_us"],
                "cutoff_epoch_us": cutoff_epoch_us,
                "tail_before_cutoff": tail_before_cutoff,
                "archived_filename": archived_filename,
                "current_machine_directory": current_machine_directory,
                "header_current_machine": header_current_machine,
                "archive_classification": classification,
                "opener_evidence": {
                    "method": "proc_fd_device_inode_snapshot",
                    "openers": file_openers,
                    "no_openers": not file_openers,
                },
                "hold_evidence": {
                    "registry_path": hold_registry["path"],
                    "registry_present": hold_registry["present"],
                    "registry_sha256": hold_registry["sha256"],
                    "matching_holds": matching_holds,
                    "no_active_hold": not matching_holds,
                },
                "eligible": eligible,
            }
        )
    eligible_entries = [item for item in entries if item["eligible"]]
    held_eligible_entries = [
        item
        for item in entries
        if item["archive_classification"] == "archived_aged_held"
    ]
    protected_entries = [item for item in entries if not item["eligible"]]
    protected_digest = _protected_digest(protected_entries)
    payload = {
        "contract_version": JOURNAL_ACTIVATION_CONTRACT,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
        "machine_id": machine_id,
        "journal_root": str(journal_root),
        "cutoff_epoch_us": cutoff_epoch_us,
        "retention_seconds": int(journald["max_retention_seconds"]),
        "entries": entries,
        "eligible_entries": eligible_entries,
        "held_eligible_entries": held_eligible_entries,
        "protected_entries": protected_entries,
        "eligible_count": len(eligible_entries),
        "eligible_bytes": sum(int(item["size_bytes"]) for item in eligible_entries),
        "protected_non_target_digest": protected_digest,
        "hold_registry": hold_registry,
    }
    payload["manifest_digest"] = _digest_payload(payload)
    return payload


def readback_journald_retention(policy: Mapping[str, Any]) -> dict[str, Any]:
    journald = _journald_policy(policy)
    source = _resolve_repo_path(str(journald["configuration_source"]))
    desired_digest = _sha256_file(source)
    operation_id = f"journald-retention-{desired_digest.removeprefix('sha256:')[:24]}"
    operation_dir = Path(str(journald["evidence_directory"])) / "activations" / operation_id
    state_path = operation_dir / "state.json"
    manifest_path = operation_dir / "preflight-manifest.json"
    if not state_path.is_file() or not manifest_path.is_file():
        return {
            "ok": False,
            "contract_version": JOURNAL_ACTIVATION_CONTRACT,
            "operation_id": operation_id,
            "reason": "activation_evidence_absent",
        }
    state = _read_json(state_path)
    manifest = _read_json(manifest_path)
    if manifest.get("manifest_digest") != _digest_payload(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    ):
        raise RootStoragePolicyError("journald preflight manifest digest mismatch")
    if state.get("manifest_digest") != manifest.get("manifest_digest"):
        raise RootStoragePolicyError("journald activation state/manifest mismatch")
    destination = Path(str(journald["configuration_destination"]))
    installed_digest = _sha256_file(destination) if destination.is_file() else None
    effective = _effective_journald_config(journald)
    service_after = _journald_service_identity()
    service_before = dict(state.get("service_before") or {})
    service_restarted = bool(
        service_before
        and service_after.get("main_pid")
        and (
            service_after.get("main_pid") != service_before.get("main_pid")
            or service_after.get("exec_main_start_timestamp")
            != service_before.get("exec_main_start_timestamp")
        )
    )
    deleted: list[dict[str, Any]] = []
    retained: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    post_identities = _regular_file_identities(
        Path(str(manifest.get("journal_root") or "/var/log/journal"))
    )
    for item in manifest["eligible_entries"]:
        path = Path(str(item["path"]))
        if not path.exists():
            moved_to = post_identities.get((int(item["device"]), int(item["inode"])), [])
            if moved_to:
                ambiguous.append(
                    {
                        **_entry_identity(item),
                        "reason": "eligible_identity_moved_not_deleted",
                        "observed_paths": moved_to,
                    }
                )
                continue
            deleted.append(_entry_identity(item))
            continue
        current = path.stat()
        if _same_manifest_identity(item, current):
            retained.append(_entry_identity(item))
        else:
            ambiguous.append({**_entry_identity(item), "reason": "eligible_path_identity_drift"})
    protected_missing: list[dict[str, Any]] = []
    protected_drift: list[dict[str, Any]] = []
    protected_after: list[dict[str, Any]] = []
    for item in manifest["protected_entries"]:
        path = Path(str(item["path"]))
        if not path.exists():
            moved_to = post_identities.get((int(item["device"]), int(item["inode"])), [])
            protected_missing.append(
                {
                    **_entry_identity(item),
                    "reason": (
                        "protected_path_moved"
                        if moved_to
                        else "protected_path_missing"
                    ),
                    "observed_paths": moved_to,
                }
            )
            continue
        current = path.stat()
        if int(current.st_dev) != int(item["device"]) or int(current.st_ino) != int(item["inode"]):
            protected_drift.append({**_entry_identity(item), "reason": "protected_path_identity_drift"})
            continue
        if item["archive_classification"] in {
            "archived_younger_than_retention",
            "archived_aged_held",
            "foreign_machine_journal",
            "non_journal_file",
        } and (
            int(current.st_size) != int(item["size_bytes"])
            or int(current.st_mtime_ns) != int(item["mtime_ns"])
        ):
            protected_drift.append({**_entry_identity(item), "reason": "protected_immutable_file_drift"})
            continue
        if int(current.st_size) < int(item["size_bytes"]):
            protected_drift.append({**_entry_identity(item), "reason": "protected_current_file_shrank"})
            continue
        protected_after.append(
            {
                **_entry_identity(item),
                "size_bytes": int(current.st_size),
                "mtime_ns": int(current.st_mtime_ns),
                "archive_classification": item["archive_classification"],
            }
        )
    protected_digest_after = _protected_digest(protected_after)
    protected_digest_matches = (
        protected_digest_after == manifest["protected_non_target_digest"]
    )
    disk_usage = subprocess.run(
        ["journalctl", "--disk-usage"],
        text=True,
        capture_output=True,
        check=False,
    )
    root_status = collect_root_storage_status(policy=policy)
    ok = bool(
        state.get("restart_submit_count") == 1
        and installed_digest == desired_digest
        and effective["matches_expected"]
        and service_restarted
        and not ambiguous
        and not protected_missing
        and not protected_drift
        and protected_digest_matches
    )
    return {
        "ok": ok,
        "contract_version": JOURNAL_ACTIVATION_CONTRACT,
        "operation_id": operation_id,
        "phase": state.get("phase"),
        "activation_submit_count": state.get("restart_submit_count"),
        "manifest_path": str(manifest_path),
        "manifest_digest": manifest["manifest_digest"],
        "manifest_eligible_count": manifest["eligible_count"],
        "manifest_eligible_bytes": manifest["eligible_bytes"],
        "deleted_entries": deleted,
        "deleted_count": len(deleted),
        "deleted_bytes": sum(int(item["size_bytes"]) for item in deleted),
        "retained_eligible_entries": retained,
        "ambiguous_eligible_entries": ambiguous,
        "protected_non_target_digest_before": manifest["protected_non_target_digest"],
        "protected_non_target_digest_after": protected_digest_after,
        "protected_non_target_digest_matches": protected_digest_matches,
        "protected_missing": protected_missing,
        "protected_drift": protected_drift,
        "held_eligible_entries": manifest["held_eligible_entries"],
        "desired_config_sha256": desired_digest,
        "installed_config_sha256": installed_digest,
        "effective_config": effective,
        "service_before": service_before,
        "service_after": service_after,
        "service_restarted": service_restarted,
        "journal_disk_usage": _bounded_text(disk_usage.stdout or disk_usage.stderr),
        "root_storage_status": root_status,
    }


def _assert_preflight_fresh(
    policy: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> None:
    """Recheck every destructive-scope fact immediately before activation."""

    observed_at = now or datetime.now(timezone.utc)
    journald = _journald_policy(policy)
    hold_registry = _load_hold_registry(
        Path(str(journald["hold_registry"])), now=observed_at
    )
    if hold_registry != manifest.get("hold_registry"):
        raise RootStoragePolicyError("journal hold evidence changed after preflight")
    fresh_cutoff = int(observed_at.timestamp() * 1_000_000) - int(
        journald["max_retention_seconds"]
    ) * 1_000_000
    for item in list(manifest.get("entries") or []):
        path = Path(str(item["path"]))
        if not path.exists():
            raise RootStoragePolicyError(f"journal identity disappeared after preflight: {path}")
        current = path.stat()
        if int(current.st_dev) != int(item["device"]) or int(current.st_ino) != int(item["inode"]):
            raise RootStoragePolicyError(f"journal identity changed after preflight: {path}")
        mutable_current = item["archive_classification"] == "current_or_online_journal"
        if mutable_current:
            if int(current.st_size) < int(item["size_bytes"]):
                raise RootStoragePolicyError(f"current journal shrank after preflight: {path}")
        elif (
            int(current.st_size) != int(item["size_bytes"])
            or int(current.st_mtime_ns) != int(item["mtime_ns"])
        ):
            raise RootStoragePolicyError(f"journal non-target/eligible identity drifted: {path}")
        if (
            item["archive_classification"] == "archived_younger_than_retention"
            and item.get("tail_realtime_epoch_us") is not None
            and int(item["tail_realtime_epoch_us"]) < fresh_cutoff
        ):
            raise RootStoragePolicyError(
                f"archived journal crossed the retention cutoff after preflight: {path}"
            )


def _assert_journald_correction_preflight_fresh(
    policy: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    correction = _journald_correction_policy(policy)
    destination = Path(str(correction["configuration_destination"]))
    _assert_exact_legacy_dropin(destination, correction)
    effective = _effective_journald_config(
        expected=dict(correction["legacy_effective_values"])
    )
    if not effective["matches_expected"]:
        raise RootStoragePolicyError(
            "journald effective configuration drifted after corrective preflight"
        )
    current_service = _journald_service_identity()
    if current_service != manifest.get("service_before"):
        raise RootStoragePolicyError(
            "journald service identity drifted after corrective preflight"
        )
    entries_after = _collect_correction_journal_inventory(
        Path(str(correction["journal_root"]))
    )
    reconciliation = _reconcile_correction_journal_inventory(
        list(manifest.get("journal_entries") or []), entries_after
    )
    if (
        reconciliation["deleted_entries"]
        or reconciliation["moved_current_entries"]
        or reconciliation["protected_drift"]
        or reconciliation["new_entries"]
        or not reconciliation["protected_identity_digest_matches"]
    ):
        raise RootStoragePolicyError(
            "journal inventory drifted after corrective preflight"
        )


def _wait_for_journald_correction_readback(
    policy: Mapping[str, Any], *, before_service: Mapping[str, Any]
) -> dict[str, Any]:
    for _ in range(40):
        after = _journald_service_identity()
        changed = bool(
            after.get("main_pid")
            and (
                after.get("main_pid") != before_service.get("main_pid")
                or after.get("exec_main_start_timestamp")
                != before_service.get("exec_main_start_timestamp")
            )
        )
        if changed and after.get("active_state") == "active":
            return readback_journald_correction(policy)
        time.sleep(0.25)
    return readback_journald_correction(policy)


def _collect_correction_journal_inventory(journal_root: Path) -> list[dict[str, Any]]:
    if not journal_root.is_dir():
        raise RootStoragePolicyError("journal root is absent before corrective operation")
    openers = _journal_openers()
    entries: list[dict[str, Any]] = []
    for path in sorted(journal_root.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat_before = path.stat()
            is_journal_file = path.name.endswith(".journal")
            header = (
                _journal_header(path)
                if is_journal_file
                else {
                    "state": None,
                    "file_id": None,
                    "machine_id": None,
                    "head_realtime_epoch_us": None,
                    "tail_realtime_epoch_us": None,
                }
            )
            stat_after = path.stat()
        except FileNotFoundError as exc:
            raise RootStoragePolicyError(
                f"journal inventory changed during capture: {path}"
            ) from exc
        mutable_current = bool(
            is_journal_file
            and (header["state"] != "ARCHIVED" or "@" not in path.name)
        )
        if not mutable_current and _stat_identity(stat_before) != _stat_identity(stat_after):
            raise RootStoragePolicyError(
                f"immutable journal inventory changed during capture: {path}"
            )
        selected_stat = stat_after if mutable_current else stat_before
        file_openers = openers.get(
            (int(selected_stat.st_dev), int(selected_stat.st_ino)), []
        )
        entries.append(
            {
                "path": str(path),
                "device": int(selected_stat.st_dev),
                "inode": int(selected_stat.st_ino),
                "size_bytes": int(selected_stat.st_size),
                "mtime_ns": int(selected_stat.st_mtime_ns),
                "is_journal_file": is_journal_file,
                "mutable_current": mutable_current,
                "journal_state": header["state"],
                "journal_file_id": header["file_id"],
                "journal_machine_id": header["machine_id"],
                "head_realtime_epoch_us": header["head_realtime_epoch_us"],
                "tail_realtime_epoch_us": header["tail_realtime_epoch_us"],
                "opener_evidence": {
                    "method": "proc_fd_device_inode_snapshot",
                    "openers": file_openers,
                    "no_openers": not file_openers,
                },
            }
        )
    return entries


def _reconcile_correction_journal_inventory(
    entries_before: list[Mapping[str, Any]], entries_after: list[Mapping[str, Any]]
) -> dict[str, Any]:
    after_by_identity: dict[tuple[int, int], list[Mapping[str, Any]]] = {}
    for item in entries_after:
        after_by_identity.setdefault(
            (int(item["device"]), int(item["inode"])), []
        ).append(item)
    before_identities = {
        (int(item["device"]), int(item["inode"])) for item in entries_before
    }
    deleted: list[dict[str, Any]] = []
    moved_current: list[dict[str, Any]] = []
    protected_drift: list[dict[str, Any]] = []
    retained_identities: list[Mapping[str, Any]] = []
    for before in entries_before:
        identity = (int(before["device"]), int(before["inode"]))
        candidates = after_by_identity.get(identity, [])
        if not candidates:
            deleted.append(
                {
                    **_entry_identity(before),
                    "reason": "preexisting_journal_root_file_deleted",
                }
            )
            continue
        same_path = next(
            (item for item in candidates if item["path"] == before["path"]), None
        )
        selected = same_path or candidates[0]
        retained_identities.append(selected)
        if same_path is None:
            if before.get("mutable_current"):
                moved_current.append(
                    {
                        **_entry_identity(before),
                        "observed_paths": sorted(str(item["path"]) for item in candidates),
                        "reason": "current_journal_rotated_without_deletion",
                    }
                )
            else:
                protected_drift.append(
                    {
                        **_entry_identity(before),
                        "observed_paths": sorted(str(item["path"]) for item in candidates),
                        "reason": "immutable_journal_root_file_moved",
                    }
                )
                continue
        if before.get("mutable_current"):
            if int(selected["size_bytes"]) < int(before["size_bytes"]):
                protected_drift.append(
                    {
                        **_entry_identity(before),
                        "reason": "current_journal_identity_shrank",
                    }
                )
        elif (
            int(selected["size_bytes"]) != int(before["size_bytes"])
            or int(selected["mtime_ns"]) != int(before["mtime_ns"])
            or bool(selected["is_journal_file"]) != bool(before["is_journal_file"])
        ):
            protected_drift.append(
                {
                    **_entry_identity(before),
                    "reason": "immutable_journal_root_file_drift",
                }
            )
    new_entries = [
        _entry_identity(item)
        for item in entries_after
        if (int(item["device"]), int(item["inode"])) not in before_identities
    ]
    protected_before = _journal_identity_digest(entries_before)
    protected_after = _journal_identity_digest(retained_identities)
    return {
        "deleted_entries": deleted,
        "deleted_count": len(deleted),
        "deleted_bytes": sum(int(item["size_bytes"]) for item in deleted),
        "moved_current_entries": moved_current,
        "protected_drift": protected_drift,
        "new_entries": new_entries,
        "protected_identity_digest_before": protected_before,
        "protected_identity_digest_after": protected_after,
        "protected_identity_digest_matches": protected_before == protected_after,
    }


def _journal_inventory_digest(entries: list[Mapping[str, Any]]) -> str:
    material = [
        {
            "path": item["path"],
            "device": int(item["device"]),
            "inode": int(item["inode"]),
            "size_bytes": int(item["size_bytes"]),
            "mtime_ns": int(item["mtime_ns"]),
            "is_journal_file": bool(item["is_journal_file"]),
            "mutable_current": bool(item["mutable_current"]),
            "journal_state": item.get("journal_state"),
            "journal_file_id": item.get("journal_file_id"),
            "journal_machine_id": item.get("journal_machine_id"),
            "head_realtime_epoch_us": item.get("head_realtime_epoch_us"),
            "tail_realtime_epoch_us": item.get("tail_realtime_epoch_us"),
        }
        for item in entries
    ]
    return _digest_payload(material)


def _journal_identity_digest(entries: list[Mapping[str, Any]]) -> str:
    material = sorted(
        [
            {
                "device": int(item["device"]),
                "inode": int(item["inode"]),
            }
            for item in entries
        ],
        key=lambda item: (item["device"], item["inode"]),
    )
    return _digest_payload(material)


def _assert_exact_legacy_dropin(
    destination: Path, correction: Mapping[str, Any]
) -> None:
    if destination.is_symlink() or not destination.is_file():
        raise RootStoragePolicyError(
            "exact block-003 journald drop-in is absent or not a regular file"
        )
    if _sha256_file(destination) != correction["legacy_configuration_sha256"]:
        raise RootStoragePolicyError("block-003 journald drop-in digest drift")


def _wait_for_journald_readback(
    policy: Mapping[str, Any],
    *,
    before_service: Mapping[str, Any],
) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for _ in range(20):
        last = readback_journald_retention(policy)
        after = dict(last.get("service_after") or {})
        changed = bool(
            after.get("main_pid")
            and (
                after.get("main_pid") != before_service.get("main_pid")
                or after.get("exec_main_start_timestamp")
                != before_service.get("exec_main_start_timestamp")
            )
        )
        if changed and last.get("ok"):
            return last
        time.sleep(0.25)
    return last


def _journald_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    value = policy.get("journald")
    if not isinstance(value, dict):
        raise RootStoragePolicyError("journald root storage policy is missing")
    required = {
        "expected_systemd_major",
        "configuration_source",
        "configuration_destination",
        "evidence_directory",
        "hold_registry",
        "system_max_use_bytes",
        "system_keep_free_bytes",
        "max_retention_seconds",
    }
    if set(value) != required:
        raise RootStoragePolicyError("journald root storage policy schema drift")
    if (
        int(value["system_max_use_bytes"]) != 2 * 1024**3
        or int(value["system_keep_free_bytes"]) != 15 * 1024**3
        or int(value["max_retention_seconds"]) != 14 * 24 * 60 * 60
    ):
        raise RootStoragePolicyError("journald root storage settings drift")
    return dict(value)


def _journald_correction_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    value = policy.get("journald")
    if not isinstance(value, dict):
        raise RootStoragePolicyError("journald correction policy is missing")
    required = {
        "contract_version",
        "mode",
        "configuration_destination",
        "evidence_directory",
        "journal_root",
        "legacy_activation_operation_id",
        "legacy_configuration_sha256",
        "legacy_effective_values",
        "expected_effective_values_after",
    }
    if set(value) != required:
        raise RootStoragePolicyError("journald correction policy schema drift")
    if value.get("contract_version") != JOURNAL_CORRECTION_CONTRACT:
        raise RootStoragePolicyError("journald correction contract version mismatch")
    if value.get("mode") != JOURNAL_CORRECTION_MODE:
        raise RootStoragePolicyError("journald correction mode mismatch")
    for field in ("configuration_destination", "evidence_directory", "journal_root"):
        if not Path(str(value[field])).is_absolute():
            raise RootStoragePolicyError(f"journald correction {field} must be absolute")
    if not re.fullmatch(r"journald-retention-[0-9a-f]{24}", str(value["legacy_activation_operation_id"])):
        raise RootStoragePolicyError("legacy journald activation operation id is invalid")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(value["legacy_configuration_sha256"])):
        raise RootStoragePolicyError("legacy journald configuration digest is invalid")
    legacy_expected = {
        "SystemMaxUse": "2G",
        "SystemKeepFree": "15G",
        "MaxRetentionSec": "14day",
    }
    if value.get("legacy_effective_values") != legacy_expected:
        raise RootStoragePolicyError("legacy journald effective settings drift")
    if value.get("expected_effective_values_after") != {}:
        raise RootStoragePolicyError(
            "corrective journald operation must restore the unoverridden settings"
        )
    return dict(value)


def _systemd_semantics_evidence(journald: Mapping[str, Any]) -> dict[str, Any]:
    version_result = subprocess.run(
        ["systemd", "--version"], text=True, capture_output=True, check=False
    )
    if version_result.returncode != 0 or not version_result.stdout.splitlines():
        raise RootStoragePolicyError("systemd version read failed before activation")
    version = version_result.stdout.splitlines()[0]
    match = _SYSTEMD_VERSION.match(version)
    if not match or int(match.group(1)) != int(journald["expected_systemd_major"]):
        raise RootStoragePolicyError(
            f"systemd major must be exactly {journald['expected_systemd_major']}: {version}"
        )
    timespan_result = subprocess.run(
        ["systemd-analyze", "timespan", "14day"],
        text=True,
        capture_output=True,
        check=False,
    )
    timespan = timespan_result.stdout
    if timespan_result.returncode != 0:
        raise RootStoragePolicyError("systemd 14day parsing failed before activation")
    if "1209600000000" not in timespan:
        raise RootStoragePolicyError("systemd 14day parsing did not equal 1209600 seconds")
    man_path = Path("/usr/share/man/man5/journald.conf.5.gz")
    binary_path = Path("/usr/lib/systemd/systemd-journald")
    if not man_path.is_file() or not binary_path.is_file():
        raise RootStoragePolicyError("systemd journald man page/binary evidence is absent")
    with gzip.open(man_path, "rb") as handle:
        man_payload = handle.read()
    required_semantics = (
        b"will respect both limits and use the smaller",
        b"only archived files are deleted",
        b"will not be removing existing files",
        b"equal to 1024",
        b"MaxRetentionSec",
        b"containing entries older than the specified time span are deleted",
    )
    if any(phrase not in man_payload for phrase in required_semantics):
        raise RootStoragePolicyError("installed journald.conf semantics evidence drift")
    return {
        "systemd_version": version,
        "expected_major": int(journald["expected_systemd_major"]),
        "retention_timespan_seconds": 14 * 24 * 60 * 60,
        "retention_timespan_output": _bounded_text(timespan),
        "journald_conf_man_sha256": _sha256_file(man_path),
        "journald_binary_sha256": _sha256_file(binary_path),
        "verified_man_semantic_phrases": [
            phrase.decode("ascii") for phrase in required_semantics
        ],
        "semantics": {
            "size_limits_use_binary_units": True,
            "max_use_and_keep_free_use_smaller_limit": True,
            "only_archived_files_are_removed_for_space_limits": True,
            "startup_keep_free_violation_does_not_retroactively_delete_to_the_keep_free_floor": True,
            "max_retention_seconds_zero_is_disabled_and_14day_is_1209600_seconds": True,
        },
    }


def _journal_header(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        ["journalctl", "--file", str(path), "--header", "--no-pager"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RootStoragePolicyError(
            f"journal header read failed for {path}: {_bounded_text(completed.stderr)}"
        )
    fields: dict[str, Any] = {
        "state": "",
        "file_id": "",
        "machine_id": "",
        "head_realtime_epoch_us": None,
        "tail_realtime_epoch_us": None,
    }
    for line in completed.stdout.splitlines():
        if line.startswith("State:"):
            fields["state"] = line.split(":", 1)[1].strip()
        elif line.startswith("File ID:"):
            fields["file_id"] = line.split(":", 1)[1].strip()
        elif line.startswith("Machine ID:"):
            fields["machine_id"] = line.split(":", 1)[1].strip()
        else:
            match = _HEADER_HEX_TIMESTAMP.match(line)
            if match:
                key = "head_realtime_epoch_us" if match.group(1) == "Head" else "tail_realtime_epoch_us"
                fields[key] = int(match.group(2), 16)
    if not fields["state"] or not fields["file_id"] or not fields["machine_id"]:
        raise RootStoragePolicyError(f"journal header classification is incomplete: {path}")
    return fields


def _journal_openers() -> dict[tuple[int, int], list[dict[str, Any]]]:
    result: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for process_dir in sorted(Path("/proc").glob("[0-9]*"), key=lambda item: item.name):
        fd_dir = process_dir / "fd"
        try:
            comm = (process_dir / "comm").read_text(encoding="utf-8").strip()
            file_descriptors = list(fd_dir.iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for fd_path in file_descriptors:
            try:
                target = os.readlink(fd_path)
                stat = fd_path.stat()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if not target.startswith("/var/log/journal/"):
                continue
            result.setdefault((int(stat.st_dev), int(stat.st_ino)), []).append(
                {
                    "pid": int(process_dir.name),
                    "comm": comm,
                    "fd": fd_path.name,
                    "target": target,
                }
            )
    for openers in result.values():
        openers.sort(key=lambda item: (item["pid"], int(item["fd"])))
    return result


def _load_hold_registry(path: Path, *, now: datetime) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "present": False,
            "sha256": None,
            "schema_version": JOURNAL_HOLD_CONTRACT,
            "active_holds": [],
            "proof": "supported_hold_registry_absent",
        }
    if path.is_symlink() or not path.is_file():
        raise RootStoragePolicyError("journal hold registry must be a regular file")
    payload = _read_json(path)
    if payload.get("schema_version") != JOURNAL_HOLD_CONTRACT or not isinstance(payload.get("holds"), list):
        raise RootStoragePolicyError("journal hold registry schema is invalid")
    active_holds: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_hold in payload["holds"]:
        if not isinstance(raw_hold, dict):
            raise RootStoragePolicyError("journal hold entry must be a JSON object")
        hold_id = str(raw_hold.get("hold_id") or "").strip()
        kind = str(raw_hold.get("kind") or "").strip()
        if not hold_id or hold_id in seen or kind not in {"incident", "forensic", "legal"}:
            raise RootStoragePolicyError("journal hold identity/kind is invalid")
        seen.add(hold_id)
        if raw_hold.get("active") is not True:
            continue
        expires_at = str(raw_hold.get("expires_at") or "").strip()
        if expires_at:
            expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expires.tzinfo is None:
                raise RootStoragePolicyError("journal hold expiry must include timezone")
            if expires <= now:
                continue
        paths = raw_hold.get("paths") or []
        inodes = raw_hold.get("inodes") or []
        if not isinstance(paths, list) or not isinstance(inodes, list):
            raise RootStoragePolicyError("journal hold paths/inodes must be arrays")
        normalized_paths = sorted(str(value) for value in paths)
        normalized_inodes = sorted(str(value) for value in inodes)
        if any(not Path(value).is_absolute() for value in normalized_paths):
            raise RootStoragePolicyError("journal hold paths must be absolute")
        if any(not re.fullmatch(r"[0-9]+:[0-9]+", value) for value in normalized_inodes):
            raise RootStoragePolicyError("journal hold inode identities must be device:inode")
        if not raw_hold.get("all") and not paths and not inodes:
            raise RootStoragePolicyError("active journal hold has no bounded target")
        active_holds.append(
            {
                "hold_id": hold_id,
                "kind": kind,
                "reference": str(raw_hold.get("reference") or ""),
                "reason": str(raw_hold.get("reason") or ""),
                "expires_at": expires_at or None,
                "all": bool(raw_hold.get("all")),
                "paths": normalized_paths,
                "inodes": normalized_inodes,
            }
        )
    return {
        "path": str(path),
        "present": True,
        "sha256": _sha256_file(path),
        "schema_version": JOURNAL_HOLD_CONTRACT,
        "active_holds": active_holds,
        "proof": "supported_hold_registry_parsed",
    }


def _matching_holds(
    registry: Mapping[str, Any],
    *,
    path: Path,
    device: int,
    inode: int,
) -> list[dict[str, Any]]:
    identity = f"{device}:{inode}"
    matches = []
    for hold in list(registry.get("active_holds") or []):
        if hold.get("all") or str(path) in hold.get("paths", []) or identity in hold.get("inodes", []):
            matches.append(dict(hold))
    return matches


def _effective_journald_config(
    journald: Mapping[str, Any] | None = None,
    *,
    expected: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    completed = subprocess.run(
        ["systemd-analyze", "cat-config", "systemd/journald.conf"],
        text=True,
        capture_output=True,
        check=False,
    )
    values: dict[str, str] = {}
    section = ""
    if completed.returncode == 0:
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line
                continue
            if section != "[Journal]" or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in {"SystemMaxUse", "SystemKeepFree", "MaxRetentionSec"}:
                values[key] = value.strip()
    expected_values = dict(
        expected
        if expected is not None
        else {
            "SystemMaxUse": "2G",
            "SystemKeepFree": "15G",
            "MaxRetentionSec": "14day",
        }
    )
    return {
        "command_returncode": completed.returncode,
        "values": values,
        "expected": expected_values,
        "matches_expected": completed.returncode == 0 and values == expected_values,
        "cat_config_sha256": _sha256_bytes(completed.stdout.encode("utf-8")),
        "stderr": _bounded_text(completed.stderr),
    }


def _validate_journald_config_bytes(payload: bytes) -> None:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RootStoragePolicyError("journald configuration must be UTF-8") from exc
    section = ""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line
            if section != "[Journal]":
                raise RootStoragePolicyError("journald configuration has an unexpected section")
            continue
        if section != "[Journal]" or "=" not in line:
            raise RootStoragePolicyError("journald configuration contains an unexpected directive")
        key, value = line.split("=", 1)
        if key in values:
            raise RootStoragePolicyError("journald configuration contains a duplicate directive")
        values[key] = value
    expected = {
        "SystemMaxUse": "2G",
        "SystemKeepFree": "15G",
        "MaxRetentionSec": "14day",
    }
    if values != expected:
        raise RootStoragePolicyError("journald configuration settings drift")


def _journald_service_identity() -> dict[str, Any]:
    completed = subprocess.run(
        [
            "systemctl",
            "show",
            "systemd-journald.service",
            "-p",
            "MainPID",
            "-p",
            "ExecMainStartTimestamp",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "NeedDaemonReload",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return {
        "returncode": completed.returncode,
        "main_pid": int(values.get("MainPID") or 0),
        "exec_main_start_timestamp": values.get("ExecMainStartTimestamp") or "",
        "active_state": values.get("ActiveState") or "",
        "sub_state": values.get("SubState") or "",
        "need_daemon_reload": values.get("NeedDaemonReload") or "",
    }


def _install_exact_config(destination: Path, payload: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Mapping[str, Any], *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700 if mode == 0o600 else 0o755)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_canonical_json(payload) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RootStoragePolicyError(f"expected JSON object: {path}")
    return payload


def _resolve_repo_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise RootStoragePolicyError("repo-owned configuration source must be relative")
    root = Path(__file__).resolve().parents[1]
    resolved = (root / path).resolve()
    resolved.relative_to(root)
    if not resolved.is_file():
        raise RootStoragePolicyError(f"repo-owned configuration source is absent: {path}")
    return resolved


def _entry_identity(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "path": item["path"],
        "device": int(item["device"]),
        "inode": int(item["inode"]),
        "size_bytes": int(item["size_bytes"]),
        "tail_realtime_epoch_us": item.get("tail_realtime_epoch_us"),
    }


def _regular_file_identities(root: Path) -> dict[tuple[int, int], list[str]]:
    identities: dict[tuple[int, int], list[str]] = {}
    if not root.is_dir():
        return identities
    for path in sorted(root.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
        except FileNotFoundError:
            continue
        identities.setdefault((int(stat.st_dev), int(stat.st_ino)), []).append(str(path))
    return identities


def _same_manifest_identity(item: Mapping[str, Any], stat: os.stat_result) -> bool:
    return bool(
        int(stat.st_dev) == int(item["device"])
        and int(stat.st_ino) == int(item["inode"])
        and int(stat.st_size) == int(item["size_bytes"])
        and int(stat.st_mtime_ns) == int(item["mtime_ns"])
    )


def _stat_identity(stat: os.stat_result) -> tuple[int, int, int, int]:
    return int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns)


def _protected_digest(entries: list[Mapping[str, Any]]) -> str:
    stable = [
        {
            "path": item["path"],
            "device": int(item["device"]),
            "inode": int(item["inode"]),
            "archive_classification": item["archive_classification"],
        }
        for item in entries
    ]
    return _digest_payload(stable)


def _digest_payload(payload: Any) -> str:
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_text(value: str, *, limit: int = 2000) -> str:
    return str(value or "").strip()[:limit]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
