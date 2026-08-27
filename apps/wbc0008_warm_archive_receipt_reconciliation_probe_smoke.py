#!/usr/bin/env python3
"""Deterministic fail-closed coverage for WBC0008 terminal reconciliation."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps import production_apply_runner as runner
from apps import wbc0008_warm_archive_receipt_reconciliation_probe as probe


MERGE = "7" * 40
OPERATION = "production-goal-v1-" + "8" * 32
JOB = "d" * 64
MANIFEST = (
    "/opt/wb-core-runtime/state/private-evidence/production-goals/"
    f"{OPERATION}/root-warm-archive-plan-20260827T101213Z.json"
)
RECLAIMED = 27_591_725_056
FLOOR = 42_198_454_272


def write_json(root: Path, logical: str | Path, value: object) -> bytes:
    path = root / str(logical).lstrip("/")
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = probe.canonical_json_bytes(value) + b"\n"
    path.write_bytes(raw)
    return raw


def expect_blocked(callable_: object, label: str) -> None:
    try:
        callable_()  # type: ignore[operator]
    except (probe.ProbeError, runner.ApplyError):
        return
    raise AssertionError(f"expected fail-closed rejection: {label}")


def job_fixture(root: Path) -> dict[str, object]:
    manifest_raw = write_json(root, MANIFEST, {"contract": "qualified"})
    manifest_sha = probe._sha256_bytes(manifest_raw)
    request_material = {
        "contract_name": probe.JOB_CONTRACT,
        "job_id": JOB,
        "deployed_sha": MERGE,
        "operation": "warm-archive-apply",
        "manifest": MANIFEST,
        "manifest_sha256": manifest_sha,
        "goal_operation_id": OPERATION,
        "approval_reference": "github:exact",
    }
    request_digest = probe.payload_digest(request_material)
    journal = {
        "contract_name": probe.ARCHIVE_CONTRACT,
        "status": "complete",
        "applied": True,
        "operation_id": OPERATION,
        "deployed_sha": MERGE,
        "manifest_path": MANIFEST,
        "manifest_sha256": manifest_sha,
        "mutation_submit_count": 1,
        "promo_action_count": 0,
        "business_data_mutation_count": 0,
    }
    result_digest = probe.payload_digest(journal)
    evidence_dir = str(Path(MANIFEST).parent)
    write_json(root, f"{evidence_dir}/root-warm-archive-apply.json", journal)
    job_dir = f"/opt/wb-core-runtime/state/storage-recovery-sanitation-jobs/{JOB}"
    write_json(
        root,
        f"{job_dir}/request.json",
        {**request_material, "request_digest": request_digest, "created_at": "now"},
    )
    write_json(
        root,
        f"{job_dir}/status.json",
        {
            "contract_name": probe.JOB_CONTRACT,
            "job_id": JOB,
            "request_digest": request_digest,
            "status": "succeeded",
            "terminal": True,
            "attempt": 1,
            "result_digest": result_digest,
        },
    )
    write_json(
        root,
        f"{job_dir}/result.json",
        {
            "contract_name": probe.JOB_CONTRACT,
            "job_id": JOB,
            "request_digest": request_digest,
            "result_digest": result_digest,
            "result": journal,
        },
    )
    return {
        "operation_id": OPERATION,
        "job_id": JOB,
        "deployed_sha": MERGE,
        "manifest_path": MANIFEST,
        "manifest_sha256": manifest_sha,
        "job_request_digest": request_digest,
        "job_result_digest": result_digest,
        "expected_reclaimed_allocated_bytes": RECLAIMED,
        "required_backup_floor_bytes": FLOOR,
    }


def archive_fixture(root: Path) -> dict[str, object]:
    sources = [f"/root/source-{index}.sqlite3" for index in range(6)]
    outputs: list[str] = []
    items: list[dict[str, object]] = []
    base = RECLAIMED // 6
    for index in range(6):
        key = f"item-{index}"
        archive = str(probe.DESTINATION / f"{index:02d}.sqlite3.zst")
        manifest = archive + ".manifest.json"
        archive_raw = f"archive-{index}".encode()
        archive_path = root / archive.lstrip("/")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(archive_raw)
        archive_sha = "sha256:" + hashlib.sha256(archive_raw).hexdigest()
        source_raw = f"source-{index}".encode()
        source_sha = "sha256:" + hashlib.sha256(source_raw).hexdigest()
        pair = {
            "contract_name": probe.ARCHIVE_CONTRACT,
            "operation_id": OPERATION,
            "target_key": key,
            "archive_path": archive,
            "archive_sha256": archive_sha,
            "lifecycle_state": "retained",
            "source_removed": True,
            "source": {
                "path": sources[index],
                "sha256": source_sha,
                "apparent_size_bytes": len(source_raw),
            },
            "unlink_receipt": {"count": 1, "source_absent": True},
            "stream_verification": {
                "decompressed_sha256": source_sha,
                "decompressed_size_bytes": len(source_raw),
            },
            "restore_proof": {
                "quick_check": "ok",
                "integrity_check": "ok",
                "restored_sha256": source_sha,
                "restored_size_bytes": len(source_raw),
                "schema_identity_sha256": "sha256:" + "a" * 64,
            },
            "published_pair_readback": {"zstd_test": "ok"},
        }
        manifest_raw = write_json(root, manifest, pair)
        value = base if index < 5 else RECLAIMED - base * 5
        items.append(
            {
                "key": key,
                "archive_path": archive,
                "manifest_path": manifest,
                "phase": "unlink_done",
                "source_absent": True,
                "unlink_count": 1,
                "reclaimed_allocated_bytes": value,
                "archive_proof": {
                    "archive_identity": {"sha256": archive_sha},
                    "manifest_identity": {"sha256": probe._sha256_bytes(manifest_raw)},
                },
            }
        )
        outputs.extend((archive, manifest))
    return {
        "operation_id": OPERATION,
        "mutation_scope_reconciliation": {
            "exact": True,
            "expected_literal_unlink_paths": sources,
            "expected_destination_output_paths": sorted(outputs),
        },
        "items": items,
        "expected_reclaimed_allocated_bytes": RECLAIMED,
        "reclaimed_allocated_bytes": RECLAIMED,
        "raw_unlink_count": 6,
    }


def valid_probe_payload() -> dict[str, object]:
    value: dict[str, object] = {
        "schema": probe.SCHEMA,
        "status": "reconciled",
        "query_only": True,
        "pythondontwritebytecode": True,
        "operation_id": OPERATION,
        "job_id": JOB,
        "deployed_sha": MERGE,
        "manifest_path": MANIFEST,
        "manifest_sha256": "sha256:" + "b" * 64,
        "production_mutation_count": 0,
        "mutation_submit_count_observed": 1,
        "promo_action_count": 0,
        "business_data_mutation_count": 0,
        "active_sanitation_job_count": 0,
        "held_lock_count": 0,
        "archive_reconciliation": {
            "source_absent_count": 6,
            "destination_object_count": 12,
            "archive_count": 6,
            "manifest_count": 6,
            "foreign_object_count": 0,
            "temporary_object_count": 0,
            "partial_object_count": 0,
            "pending_object_count": 0,
            "raw_unlink_count": 6,
            "reclaimed_allocated_bytes": RECLAIMED,
            "archives": [{} for _ in range(6)],
        },
        "capacity_reconciliation": {
            "sample_count": 3,
            "root_stable": True,
            "backup_stable": True,
            "root_min_available_bytes": 37 * 1024**3,
            "backup_min_available_bytes": 50 * 1024**3,
        },
        "finance_reconciliation": {
            "healthy": True,
            "required_available_floor_bytes": FLOOR,
        },
        "natural_root_monitor": {"fresh": True, "normal": True},
        "systemd_service_gate": {"healthy": True, "unit_count": 27, "pair_count": 12},
        "non_target_reconciliation": {"preserved": True},
        "journald_reconciliation": {"preserved": True},
        "remote_action_counts": {name: 0 for name in (
            "readiness", "submit", "apply", "job_creation", "archive_worker",
            "readback_batch", "full_restore", "temporary_file_creation",
            "lock_acquisition", "service_start_or_restart", "timer_change",
            "sql_or_file_write", "unlink",
        )},
    }
    value["evidence_digest"] = runner.payload_digest(value)
    return value


def source_receipt_fixture() -> tuple[dict[str, object], dict[str, object], str]:
    pr = 1075
    comment_id = 5437409674
    body = (
        "/wb-core authorize-goal-v1 task WBC0008 profile root-warm-archive-six "
        "target wb_core_eu_hosted_runtime_active sources 6 archives 6 manifests 6 "
        "unlinks 6 reclaimed-allocated-bytes 27591725056 "
        "root-minimum-bytes 26843545600 backup-floor-bytes 42198454272"
    )
    goal = runner.validate_authorization(
        {
            "id": comment_id,
            "created_at": "2026-08-27T10:00:00Z",
            "author_association": "OWNER",
            "issue_url": f"https://api.github.com/repos/orenvlad-ai/wb-core/issues/{pr}",
            "body": body,
        },
        repository="orenvlad-ai/wb-core",
        pr=pr,
    )
    operation = runner.operation_id("orenvlad-ai/wb-core", pr, comment_id, goal)
    manifest = (
        "/opt/wb-core-runtime/state/private-evidence/production-goals/"
        f"{operation}/root-warm-archive-plan-20260827T101213Z.json"
    )
    manifest_sha = "sha256:" + "b" * 64
    job_id = "d" * 64
    job_result = {
        "status": "complete",
        "operation_id": operation,
        "manifest_path": manifest,
        "manifest_sha256": manifest_sha,
        "mutation_submit_count": 1,
        "raw_unlink_count": 6,
        "reclaimed_allocated_bytes": RECLAIMED,
        "promo_action_count": 0,
        "business_data_mutation_count": 0,
    }
    job_result_digest = runner.payload_digest(job_result)
    request = {
        "operation": "warm-archive-apply",
        "goal_operation_id": operation,
        "manifest": manifest,
        "manifest_sha256": manifest_sha,
        "deployed_sha": MERGE,
    }
    receipt: dict[str, object] = {
        "schema": runner.APPLY_RECEIPT_SCHEMA,
        "state": "blocked",
        "operation_id": operation,
        "repository": "orenvlad-ai/wb-core",
        "pull_request": pr,
        "merge_sha": MERGE,
        "deployed_sha": MERGE,
        "authorization_comment_id": comment_id,
        "apply_count": 1,
        "goal": goal,
        "release_operation_id": "release-v2-" + "a" * 32,
        "warm_archive_readiness": {
            "schema": runner.WARM_READINESS_RECEIPT_SCHEMA,
            "state": "ready",
            "query_only": True,
            "database_written": False,
            "goal_operation_id": operation,
            "deployed_sha": MERGE,
            "readiness_id": "readiness-v2-" + "1" * 32 + "-a01",
        },
        "evidence": {
            "state": "blocked",
            "reason": "post-submit-readback-not-reconciled",
            "apply_count": 1,
            "qualified_manifest": {"path": manifest, "sha256": manifest_sha},
            "apply": {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "job_id": job_id,
                    "status": "queued",
                    "terminal": False,
                    "submit_idempotent": False,
                    "request": request,
                },
            },
            "readback": {
                "return_code": 0,
                "transport_ambiguous": False,
                "result": {
                    "status": "blocked",
                    "query_only": True,
                    "deployed_sha": MERGE,
                    "operation_id": operation,
                    "manifest_sha256": manifest_sha,
                    "source_count": 6,
                    "source_absent_count": 6,
                    "archive_count": 6,
                    "manifest_count": 6,
                    "raw_unlink_count": 6,
                    "reclaimed_allocated_bytes": RECLAIMED,
                    "root_minimum_passed": True,
                    "backup_capacity_guard_passed": False,
                    "services_healthy": True,
                    "non_target_preserved": True,
                    "promo_action_count": 0,
                    "business_data_mutation_count": 0,
                    "exact_manifest_apply_receipt_count": 1,
                    "mutation_scope_reconciliation": {
                        "exact": True,
                        "non_target_unlink_move_write_count": 0,
                    },
                    "job": {
                        "job_id": job_id,
                        "status": "succeeded",
                        "terminal": True,
                        "attempt": 1,
                        "request": request,
                        "request_digest": "sha256:" + "c" * 64,
                        "result": job_result,
                        "result_digest": job_result_digest,
                    },
                },
            },
        },
    }
    return receipt, goal, operation


def refresh_digest(value: dict[str, object]) -> None:
    value.pop("evidence_digest", None)
    value["evidence_digest"] = runner.payload_digest(value)


def test_job_and_archive_contracts() -> None:
    with tempfile.TemporaryDirectory(prefix="warm-reconciliation-job-") as directory:
        root = Path(directory)
        config = job_fixture(root)
        journal, evidence = probe._job_and_journal(config, root_prefix=root)
        assert evidence["status"] == "succeeded" and journal["mutation_submit_count"] == 1
        status = root / f"opt/wb-core-runtime/state/storage-recovery-sanitation-jobs/{JOB}/status.json"
        payload = json.loads(status.read_text())
        payload["attempt"] = 2
        write_json(root, f"/opt/wb-core-runtime/state/storage-recovery-sanitation-jobs/{JOB}/status.json", payload)
        expect_blocked(lambda: probe._job_and_journal(config, root_prefix=root), "job attempt drift")
    with tempfile.TemporaryDirectory(prefix="warm-reconciliation-manifest-") as directory:
        root = Path(directory)
        config = job_fixture(root)
        write_json(root, MANIFEST, {"contract": "drift"})
        expect_blocked(lambda: probe._job_and_journal(config, root_prefix=root), "manifest hash drift")
    with tempfile.TemporaryDirectory(prefix="warm-reconciliation-archive-") as directory:
        root = Path(directory)
        journal = archive_fixture(root)
        result = probe._exact_archive_set(journal, root_prefix=root)
        assert result["destination_object_count"] == 12 and result["raw_unlink_count"] == 6
        source = root / "root/source-0.sqlite3"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"present")
        expect_blocked(lambda: probe._exact_archive_set(journal, root_prefix=root), "source present")
        source.unlink()
        foreign = root / str(probe.DESTINATION).lstrip("/") / ".restore.tmp.sqlite3"
        foreign.write_bytes(b"temp")
        expect_blocked(lambda: probe._exact_archive_set(journal, root_prefix=root), "foreign/temp object")
        foreign.unlink()
        archive = root / str(journal["items"][0]["archive_path"]).lstrip("/")  # type: ignore[index]
        original_archive = archive.read_bytes()
        archive.unlink()
        expect_blocked(lambda: probe._exact_archive_set(journal, root_prefix=root), "missing object")
        archive.write_bytes(original_archive)
        archive.write_bytes(b"hash-drift")
        expect_blocked(lambda: probe._exact_archive_set(journal, root_prefix=root), "archive hash drift")
        archive.write_bytes(original_archive)
        manifest_logical = str(journal["items"][0]["manifest_path"])  # type: ignore[index]
        manifest_path = root / manifest_logical.lstrip("/")
        pair = json.loads(manifest_path.read_text(encoding="utf-8"))
        pair["restore_proof"]["quick_check"] = "drift"
        manifest_raw = write_json(root, manifest_logical, pair)
        journal["items"][0]["archive_proof"]["manifest_identity"]["sha256"] = probe._sha256_bytes(manifest_raw)  # type: ignore[index]
        expect_blocked(lambda: probe._exact_archive_set(journal, root_prefix=root), "saved restore proof drift")


def test_jobs_locks_and_capacity() -> None:
    with tempfile.TemporaryDirectory(prefix="warm-reconciliation-lock-") as directory:
        root = Path(directory)
        jobs = root / "opt/wb-core-runtime/state/storage-recovery-sanitation-jobs"
        write_json(root, f"/opt/wb-core-runtime/state/storage-recovery-sanitation-jobs/{JOB}/status.json", {"terminal": True, "status": "succeeded"})
        (jobs / "worker.lock").write_bytes(b"")
        proc = root / "proc/locks"
        proc.parent.mkdir(parents=True, exist_ok=True)
        proc.write_text("", encoding="utf-8")
        assert probe._active_jobs_and_locks(JOB, root_prefix=root)["held_lock_count"] == 0
        status = jobs / JOB / "status.json"
        status.write_text(json.dumps({"terminal": False, "status": "running"}), encoding="utf-8")
        expect_blocked(lambda: probe._active_jobs_and_locks(JOB, root_prefix=root), "active job")
        status.write_text(json.dumps({"terminal": True, "status": "succeeded"}), encoding="utf-8")
        st = (jobs / "worker.lock").stat()
        proc.write_text(
            f"1: POSIX ADVISORY WRITE 1 {os.major(st.st_dev):x}:{os.minor(st.st_dev):x}:{st.st_ino} 0 EOF\n",
            encoding="utf-8",
        )
        expect_blocked(lambda: probe._active_jobs_and_locks(JOB, root_prefix=root), "held lock")

    original = probe.os.statvfs
    values = iter([30 * 1024**3, 50 * 1024**3] * 3)
    probe.os.statvfs = lambda _path: SimpleNamespace(
        f_bavail=next(values), f_bfree=60 * 1024**3, f_blocks=100 * 1024**3, f_frsize=1
    )
    try:
        current = lambda: datetime(2026, 8, 27, tzinfo=timezone.utc)
        assert probe._capacity_samples(FLOOR, root_prefix=None, sleep_fn=lambda _v: None, now_fn=current)["sample_count"] == 3
    finally:
        probe.os.statvfs = original
    values = iter([20 * 1024**3, 50 * 1024**3] * 3)
    probe.os.statvfs = lambda _path: SimpleNamespace(
        f_bavail=next(values), f_bfree=60 * 1024**3, f_blocks=100 * 1024**3, f_frsize=1
    )
    try:
        expect_blocked(lambda: probe._capacity_samples(FLOOR, root_prefix=None, sleep_fn=lambda _v: None, now_fn=lambda: datetime.now(timezone.utc)), "capacity below floor")
    finally:
        probe.os.statvfs = original
    values = iter(
        [30 * 1024**3, 50 * 1024**3, 31 * 1024**3, 50 * 1024**3, 30 * 1024**3, 50 * 1024**3]
    )
    probe.os.statvfs = lambda _path: SimpleNamespace(
        f_bavail=next(values), f_bfree=60 * 1024**3, f_blocks=100 * 1024**3, f_frsize=1
    )
    try:
        expect_blocked(lambda: probe._capacity_samples(FLOOR, root_prefix=None, sleep_fn=lambda _v: None, now_fn=lambda: datetime.now(timezone.utc)), "unstable capacity")
    finally:
        probe.os.statvfs = original


def test_runner_receiver_and_command() -> None:
    payload = valid_probe_payload()
    context = {"source": {
        "operation_id": OPERATION, "job_id": JOB, "deployed_sha": MERGE,
        "manifest_path": MANIFEST, "manifest_sha256": "sha256:" + "b" * 64,
        "expected_reclaimed_allocated_bytes": RECLAIMED,
        "required_backup_floor_bytes": FLOOR,
    }}
    assert runner._valid_warm_reconciliation_probe(payload, context=context)
    mutations = (
        ("operation_id", "wrong"),
        ("active_sanitation_job_count", 1),
        ("held_lock_count", 1),
        ("production_mutation_count", 1),
        ("mutation_submit_count_observed", 2),
    )
    for field, value in mutations:
        changed = deepcopy(payload); changed[field] = value; refresh_digest(changed)
        assert not runner._valid_warm_reconciliation_probe(changed, context=context)
    for section, field, value in (
        ("archive_reconciliation", "source_absent_count", 5),
        ("archive_reconciliation", "foreign_object_count", 1),
        ("archive_reconciliation", "temporary_object_count", 1),
        ("capacity_reconciliation", "root_stable", False),
        ("capacity_reconciliation", "backup_min_available_bytes", FLOOR - 1),
        ("natural_root_monitor", "fresh", False),
        ("systemd_service_gate", "unit_count", 26),
        ("systemd_service_gate", "pair_count", 11),
        ("non_target_reconciliation", "preserved", False),
        ("journald_reconciliation", "preserved", False),
    ):
        changed = deepcopy(payload); changed[section][field] = value; refresh_digest(changed)  # type: ignore[index]
        assert not runner._valid_warm_reconciliation_probe(changed, context=context)
    binding = {key: context["source"][key] for key in (
        "operation_id", "job_id", "deployed_sha", "manifest_path", "manifest_sha256",
    )}
    binding.update({
        "job_request_digest": "sha256:" + "c" * 64,
        "job_result_digest": "sha256:" + "d" * 64,
        "expected_reclaimed_allocated_bytes": RECLAIMED,
        "required_backup_floor_bytes": FLOOR,
    })
    command = runner._warm_reconciliation_probe_command(
        target={"ssh_destination": "canonical-host"}, binding=binding
    )
    assert command[0] == "ssh" and command[-2] == "canonical-host"
    assert "PYTHONDONTWRITEBYTECODE=1" in command[-1]
    forbidden = ("submit", "readback_batch", "restore.tmp", "systemctl start", "systemctl restart", "sqlite3")
    assert not any(token in command[-1] for token in forbidden)
    source = (ROOT / "apps/wbc0008_warm_archive_receipt_reconciliation_probe.py").read_text()
    for token in ("import sqlite3", "import tempfile", "import fcntl", ".unlink(", "systemctl\", \"start", "systemctl\", \"restart", "readback_batch("):
        assert token not in source
    calls: list[list[str]] = []
    original = runner.subprocess.run
    runner.subprocess.run = lambda cmd, **_kwargs: (calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, b"{}\n", b""))
    try:
        runner._query_only_probe_evidence(command, b"probe")
    finally:
        runner.subprocess.run = original
    assert calls == [command]


def test_exact_source_receipt_gate() -> None:
    receipt, goal, operation = source_receipt_fixture()
    binding = runner._validate_warm_reconciliation_source_receipt(
        receipt,
        repository="orenvlad-ai/wb-core",
        pr=1075,
        merge_sha=MERGE,
        authorization_comment_id=5437409674,
        expected_operation=operation,
        goal=goal,
    )
    assert binding["job_id"] == "d" * 64
    for path, value in (
        (("state",), "done"),
        (("evidence", "reason"), "wrong-reason"),
        (("evidence", "qualified_manifest", "sha256"), "sha256:" + "f" * 64),
        (("evidence", "readback", "result", "source_absent_count"), 5),
        (("evidence", "readback", "result", "job", "attempt"), 2),
        (("evidence", "readback", "result", "job", "result_digest"), "sha256:" + "e" * 64),
    ):
        changed = deepcopy(receipt)
        cursor: dict[str, object] = changed
        for key in path[:-1]:
            cursor = cursor[key]  # type: ignore[assignment,index]
        cursor[path[-1]] = value
        expect_blocked(
            lambda changed=changed: runner._validate_warm_reconciliation_source_receipt(
                changed,
                repository="orenvlad-ai/wb-core",
                pr=1075,
                merge_sha=MERGE,
                authorization_comment_id=5437409674,
                expected_operation=operation,
                goal=goal,
            ),
            "source state/reason/hash/job drift",
        )


def test_terminal_marker_idempotency() -> None:
    source = {
        "operation_id": OPERATION,
        "job_id": JOB,
        "deployed_sha": MERGE,
        "manifest_path": MANIFEST,
        "manifest_sha256": "sha256:" + "b" * 64,
        "expected_reclaimed_allocated_bytes": RECLAIMED,
        "required_backup_floor_bytes": FLOOR,
    }
    release = {
        "pull_request": 1100,
        "release_operation_id": "release-v2-" + "a" * 32,
        "release_kind": "repo_only",
        "merge_sha": MERGE,
        "workflow_run_id": 100,
        "plan_hash": "sha256:" + "f" * 64,
        "deployed_sha": None,
        "probe_source_sha256": "sha256:" + "e" * 64,
    }
    receipt: dict[str, object] = {
        "schema": runner.WARM_RECONCILIATION_RECEIPT_SCHEMA,
        "state": "done",
        "reason": "reconciled-existing-terminal-operation",
        "terminal_disposition": "done/reconciled_existing_operation",
        "query_only": True,
        "production_mutation_count": 0,
        "source": source,
        "reconciliation_release": release,
        "probe": {"result": valid_probe_payload()},
    }
    receipt["evidence_digest"] = runner.payload_digest(receipt)
    raw = runner.canonical_json_bytes(receipt) + b"\n"
    receipt_sha = hashlib.sha256(raw).hexdigest()
    artifact_name = "root-warm-archive-reconciliation-pr-1075-run-123"
    summary = {
        "schema": runner.WARM_RECONCILIATION_SUMMARY_SCHEMA,
        "state": "done",
        "reason": receipt["reason"],
        "terminal_disposition": receipt["terminal_disposition"],
        "operation_id": OPERATION,
        "query_only": True,
        "production_mutation_count": 0,
        "source": source,
        "reconciliation_release": release,
        "evidence_digest": receipt["evidence_digest"],
        "terminal_facts": {},
        "artifact": {
            "name": artifact_name,
            "file": runner.WARM_RECONCILIATION_ARTIFACT_FILE,
            "sha256": "sha256:" + receipt_sha,
            "size_bytes": len(raw),
            "retention_days": 90,
        },
    }
    comment = {
        "id": 999,
        "user": {"login": "github-actions[bot]"},
        "body": runner.warm_reconciliation_marker(OPERATION)
        + "\n```json\n"
        + json.dumps(summary, sort_keys=True, separators=(",", ":"))
        + "\n```",
    }
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        archive.writestr(runner.WARM_RECONCILIATION_ARTIFACT_FILE, raw)

    class Client:
        repository = "orenvlad-ai/wb-core"

        def get(self, path: str) -> object:
            assert path == "/actions/runs/123/artifacts?per_page=100"
            return {"artifacts": [{
                "id": 777,
                "name": artifact_name,
                "expired": False,
                "workflow_run": {"id": 123, "head_branch": "main", "head_sha": MERGE},
            }]}

        def request(self, method: str, path: str, **_kwargs: object) -> bytes:
            assert method == "GET" and path == "/actions/artifacts/777/zip"
            return archive_bytes.getvalue()

    context = {"source": source, "reconciliation_release": release, "client": Client()}
    assert runner._existing_warm_reconciliation_marker([comment], context=context) == comment
    expect_blocked(
        lambda: runner._existing_warm_reconciliation_marker([comment, comment], context=context),
        "duplicate terminal marker",
    )
    drift = deepcopy(comment)
    drift_payload = json.loads(drift["body"].split("```json", 1)[1].split("```", 1)[0])
    drift_payload["artifact"]["sha256"] = "sha256:" + "0" * 64
    drift["body"] = runner.warm_reconciliation_marker(OPERATION) + "\n```json\n" + json.dumps(drift_payload) + "\n```"
    expect_blocked(
        lambda: runner._existing_warm_reconciliation_marker([drift], context=context),
        "different terminal artifact digest",
    )


def main() -> None:
    test_job_and_archive_contracts()
    test_jobs_locks_and_capacity()
    test_runner_receiver_and_command()
    test_exact_source_receipt_gate()
    test_terminal_marker_idempotency()
    print("wbc0008_warm_archive_receipt_reconciliation_probe_smoke: ok")


if __name__ == "__main__":
    main()
