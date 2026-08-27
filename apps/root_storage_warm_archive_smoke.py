#!/usr/bin/env python3
"""Deterministic restore/publish guards for WBC0008 block 006."""

from __future__ import annotations

import fcntl
import json
import os
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


class _BodyFailure(RuntimeError):
    pass


class _MutationBoundary(RuntimeError):
    pass


def _seed(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE evidence(key TEXT PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO evidence VALUES('scope', 'wbc0008-006')")
        connection.commit()


def _healthy_systemd_snapshot() -> dict[str, dict[str, object]]:
    snapshot: dict[str, dict[str, object]] = {}
    for unit_index, name in enumerate(warm.SERVICE_NAMES, start=1):
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
                    "MainPID": str(1000 + unit_index),
                    "UnitFileState": "enabled",
                }
            )
        snapshot[name] = values
    return snapshot


def _open_fd_count() -> int | None:
    for candidate in (Path("/proc/self/fd"), Path("/dev/fd")):
        if candidate.is_dir():
            return len(os.listdir(candidate))
    return None


def _assert_lock_available(path: Path) -> None:
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _exercise_lock_contexts(root: Path) -> None:
    runtime = root / "lock-runtime"
    runtime.mkdir()
    fd_count_before = _open_fd_count()

    finance = warm._exclusive_finance_lock(runtime)
    with finance as finance_handle:
        assert finance_handle is finance.handle
        assert finance_handle.closed is False
        contender = warm._exclusive_finance_lock(runtime)
        try:
            contender.__enter__()
        except warm.WarmArchiveError as exc:
            assert str(exc) == "Finance storage operation/reservation is active"
        else:
            raise AssertionError("Finance lock contention was not rejected")
        assert contender.handle is not None and contender.handle.closed
    assert finance_handle.closed
    _assert_lock_available(finance.path)

    finance_body = warm._exclusive_finance_lock(runtime)
    try:
        with finance_body:
            raise _BodyFailure("finance body failure")
    except _BodyFailure as exc:
        assert str(exc) == "finance body failure"
    else:
        raise AssertionError("Finance lock suppressed the body exception")
    assert finance_body.handle.closed
    with warm._exclusive_finance_lock(runtime) as repeated_finance:
        assert repeated_finance.closed is False
    assert repeated_finance.closed

    finance_symlink_runtime = root / "finance-symlink-runtime"
    finance_symlink_runtime.mkdir()
    symlink_target = finance_symlink_runtime / "symlink-target"
    symlink_target.touch()
    (finance_symlink_runtime / warm.FINANCE_STORAGE_LOCK_FILENAME).symlink_to(
        symlink_target
    )
    try:
        with warm._exclusive_finance_lock(finance_symlink_runtime):
            raise AssertionError("Finance symlink lock unexpectedly entered")
    except warm.WarmArchiveError as exc:
        assert str(exc) == "Finance storage lock is a symlink"

    lifecycle = warm._exclusive_other_lifecycle_locks(runtime)
    unlock_order: list[int] = []
    original_flock = warm.fcntl.flock

    def traced_flock(descriptor: int, operation: int) -> object:
        if operation == fcntl.LOCK_UN:
            unlock_order.append(os.fstat(descriptor).st_ino)
        return original_flock(descriptor, operation)

    warm.fcntl.flock = traced_flock
    try:
        with lifecycle:
            lifecycle_handles = list(lifecycle.handles)
            acquired_order = [os.fstat(item.fileno()).st_ino for item in lifecycle_handles]
    finally:
        warm.fcntl.flock = original_flock
    assert unlock_order == list(reversed(acquired_order))
    assert lifecycle.handles == []
    assert all(handle.closed for handle in lifecycle_handles)

    lifecycle_body = warm._exclusive_other_lifecycle_locks(runtime)
    lifecycle_body_handles: list[object] = []
    try:
        with lifecycle_body:
            lifecycle_body_handles = list(lifecycle_body.handles)
            raise _BodyFailure("lifecycle body failure")
    except _BodyFailure as exc:
        assert str(exc) == "lifecycle body failure"
    else:
        raise AssertionError("lifecycle locks suppressed the body exception")
    assert lifecycle_body.handles == []
    assert all(handle.closed for handle in lifecycle_body_handles)

    partial_runtime = root / "partial-runtime"
    partial_runtime.mkdir()
    partial_paths = [partial_runtime / name for name in warm.OTHER_LIFECYCLE_LOCKS]
    with partial_paths[1].open("a+b") as blocking_handle:
        fcntl.flock(blocking_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        partial = warm._exclusive_other_lifecycle_locks(partial_runtime)
        try:
            partial.__enter__()
        except warm.WarmArchiveError as exc:
            assert str(exc) == "another storage lifecycle operation is active"
        else:
            raise AssertionError("partial lifecycle contention was not rejected")
        assert partial.handles == []
        _assert_lock_available(partial_paths[0])
        fcntl.flock(blocking_handle.fileno(), fcntl.LOCK_UN)
    with warm._exclusive_other_lifecycle_locks(partial_runtime):
        pass
    for path in partial_paths:
        _assert_lock_available(path)

    lifecycle_symlink_runtime = root / "lifecycle-symlink-runtime"
    lifecycle_symlink_runtime.mkdir()
    lifecycle_symlink_target = lifecycle_symlink_runtime / "symlink-target"
    lifecycle_symlink_target.touch()
    (lifecycle_symlink_runtime / warm.OTHER_LIFECYCLE_LOCKS[0]).symlink_to(
        lifecycle_symlink_target
    )
    symlink_lifecycle = warm._exclusive_other_lifecycle_locks(
        lifecycle_symlink_runtime
    )
    try:
        symlink_lifecycle.__enter__()
    except warm.WarmArchiveError as exc:
        assert str(exc).startswith("lifecycle lock is a symlink:")
    else:
        raise AssertionError("lifecycle symlink lock unexpectedly entered")
    assert symlink_lifecycle.handles == []

    fd_count_after = _open_fd_count()
    if fd_count_before is not None and fd_count_after is not None:
        assert fd_count_after == fd_count_before


def _exercise_apply_lock_path(root: Path) -> None:
    runtime = root / "apply-runtime"
    runtime.mkdir()
    root_backups = root / "apply-root-backups"
    root_backups.mkdir()
    evidence_dir = root / "apply-evidence"
    evidence_dir.mkdir()
    deployed_sha = "b" * 40
    readiness_id = "readiness-v2-" + "c" * 32 + "-a01"
    fresh_material = {
        "material_partition": "immutable_safety_v1",
        "expected_reclaimed_allocated_bytes": 512,
        "immutable_non_target_digest": "sha256:" + "d" * 64,
        "mutable_canonical_topology_digest": "sha256:" + "e" * 64,
        "targets": [{"key": "fixture"}],
    }
    material_digest = warm._digest(fresh_material)
    manifest = {
        "deployed_sha": deployed_sha,
        "material": fresh_material,
        "material_qualification_digest": material_digest,
        "readiness_projection": {
            "readiness_id": readiness_id,
            "path": str(root / "projection.json"),
            "sha256": "sha256:" + "f" * 64,
            "material_qualification_digest": material_digest,
        },
    }
    observations = {
        "filesystems_before": {"root": {}, "backup": {}},
        "journald": {"service": {}, "effective": {}, "inventory": []},
        "services": {},
        "systemd_service_gate": {"healthy": True, "classification": "healthy"},
        "activity_gates": [{"blockers": []} for _ in range(6)],
        "finance": {
            "healthy": True,
            "required_available_floor_bytes": 1,
        },
        "capacity_stages": [
            {"projected_available_at_peak_bytes": 2} for _ in range(6)
        ],
        "projected_root_available_bytes": warm.ROOT_MINIMUM_AFTER_BYTES,
        "active_sanitation_jobs": [],
        "lifecycle_locks": [
            {"path": name, "present": True, "locked": True, "held_by_batch": True}
            for name in warm.OTHER_LIFECYCLE_LOCKS
        ],
        "non_target": {
            "immutable_digest": "sha256:" + "d" * 64,
            "mutable_canonical_topology_digest": "sha256:" + "e" * 64,
            "mutable_canonical": {"observation_rows": []},
        },
    }
    original_functions = {
        name: getattr(warm, name)
        for name in (
            "_verify_deployed_sha",
            "_load_manifest",
            "_load_readiness_projection",
            "_journal_path",
            "_material_snapshot",
            "_atomic_write_json",
            "_ensure_destination_family",
            "_process_target",
        )
    }
    mutation_calls: list[tuple[Path, object]] = []

    def first_mutation(path: Path, payload: object) -> None:
        mutation_calls.append((path, payload))
        raise _MutationBoundary("first durable mutation reached")

    warm._verify_deployed_sha = lambda **_kwargs: None
    warm._load_manifest = lambda **_kwargs: manifest
    warm._load_readiness_projection = lambda **_kwargs: {
        "readiness_id": readiness_id,
        "material_qualification_digest": material_digest,
    }
    warm._journal_path = lambda _evidence_dir: root / "first-mutation.json"
    warm._material_snapshot = lambda **_kwargs: (fresh_material, observations)
    warm._atomic_write_json = first_mutation
    warm._ensure_destination_family = lambda: (_ for _ in ()).throw(
        AssertionError("destination mutation reached before material guards")
    )
    warm._process_target = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("archive mutation reached before material guards")
    )
    apply_kwargs = {
        "runtime_dir": runtime,
        "root_backups": root_backups,
        "deployed_sha": deployed_sha,
        "deployed_sha_file": root / ".wb-core-runtime-sha",
        "evidence_dir": evidence_dir,
        "operation_id": OPERATION,
        "manifest_path": root / "manifest.json",
        "manifest_sha256": "sha256:" + "1" * 64,
        "approval_reference": "github:owner:bounded-wbc0008-006",
    }
    try:
        try:
            warm.apply_batch(**apply_kwargs)
        except _MutationBoundary as exc:
            assert str(exc) == "first durable mutation reached"
        else:
            raise AssertionError("apply_batch did not reach its first mutation boundary")
        assert len(mutation_calls) == 1
        for lock_name in (
            warm.FINANCE_STORAGE_LOCK_FILENAME,
            *warm.OTHER_LIFECYCLE_LOCKS,
        ):
            _assert_lock_available(runtime / lock_name)

        mutation_calls.clear()
        drifted_material = json.loads(json.dumps(fresh_material))
        drifted_material["targets"][0]["identity"] = {
            "path": "/safe/fixture.sqlite3",
            "device": 1,
            "inode": 2,
            "mtime_ns": 3,
            "sha256": "sha256:" + "4" * 64,
        }
        warm._material_snapshot = lambda **_kwargs: (
            drifted_material,
            observations,
        )
        try:
            warm.apply_batch(**apply_kwargs)
        except warm.WarmArchiveError as exc:
            assert str(exc) == "immutable material CAS drifted after qualification"
            diff = exc.evidence["component_diff"]
            assert diff["changed_json_paths"] == ["/targets/0/identity"]
            assert exc.evidence["mutation_journal_created"] is False
            assert exc.evidence["archive_mutation_started"] is False
        else:
            raise AssertionError("mutation-start target drift was not rejected")
        failure_path = evidence_dir / warm.MATERIAL_CAS_FAILURE_FILENAME
        assert failure_path.is_file()
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["component_diff"]["changed_json_paths"] == [
            "/targets/0/identity"
        ]
        assert failure["mutation_journal_created"] is False
        assert not (root / "first-mutation.json").exists()
        assert mutation_calls == []

        def fail_before_boundary(**_kwargs: object) -> None:
            raise warm.WarmArchiveError("fixture pre-boundary failure")

        warm._verify_deployed_sha = fail_before_boundary
        try:
            warm.apply_batch(**apply_kwargs)
        except warm.WarmArchiveError as exc:
            assert str(exc) == "fixture pre-boundary failure"
        else:
            raise AssertionError("apply_batch pre-boundary failure was suppressed")
        assert mutation_calls == []
        for lock_name in (
            warm.FINANCE_STORAGE_LOCK_FILENAME,
            *warm.OTHER_LIFECYCLE_LOCKS,
        ):
            _assert_lock_available(runtime / lock_name)
    finally:
        for name, value in original_functions.items():
            setattr(warm, name, value)


def _access_role(
    service: str,
    declared_role: str,
) -> dict[str, object]:
    modes = {
        "reader": ["read_only"],
        "writer": ["read_write"],
        "reader_writer": ["read_only", "read_write"],
    }[declared_role]
    return {
        "service": service,
        "declared_role": declared_role,
        "allowed_access_modes": modes,
    }


def _mutable_fixture_policy(
    path: Path,
    *,
    resolver_type: str = "literal",
    access_roles: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    resolver: dict[str, object]
    if resolver_type == "literal":
        resolver = {"type": "literal", "path": str(path)}
    else:
        resolver = {"type": "store_registry", "logical_store": "operational"}
    return {
        "non_target_cas": {
            "contract_version": "wb_core_non_target_cas_v2",
            "active_mutable_canonical_stores": [
                {
                    "key": "fixture_current",
                    "owner": "fixture_operational_store",
                    "classification": "essential_bounded_business_writer",
                    "resolver": resolver,
                    "access_roles": access_roles
                    or [
                        _access_role(
                            "wb-core-autoanswers-worker.service",
                            "reader_writer",
                        )
                    ],
                    "allow_no_open_handles": True,
                }
            ],
        },
        "producers": [
            {
                "owner": "fixture_operational_store",
                "classification": "essential_bounded_business_writer",
                "path_patterns": [str(path)] if resolver_type == "literal" else [],
            }
        ],
    }


def _fd_opener(
    path: Path,
    *,
    pid: int,
    access_mode: str,
    device: int | None = None,
    inode: int | None = None,
) -> dict[str, object]:
    value = path.stat()
    return {
        "source_path": str(path),
        "pid": pid,
        "fd": 7,
        "access_mode": access_mode,
        "comm": "python3",
        "fd_target": str(path),
        "real_fd_target": str(path.resolve()),
        "target_device": int(value.st_dev if device is None else device),
        "target_device_major": int(os.major(value.st_dev)),
        "target_device_minor": int(os.minor(value.st_dev)),
        "target_inode": int(value.st_ino if inode is None else inode),
        "binds_source_device_inode": True,
    }


def _non_target_fixture(
    snapshot: dict[str, object], *, immutable_digest: str = "sha256:" + "1" * 64
) -> dict[str, object]:
    return {
        "immutable_digest": immutable_digest,
        "mutable_canonical": snapshot,
        "mutable_canonical_topology_digest": snapshot["topology_digest"],
    }


def _exercise_non_target_cas_split(root: Path) -> None:
    root = root.resolve()
    original_mount_identity = warm._mount_identity
    warm._mount_identity = lambda path: {
        "mount_id": 1,
        "mount_point": str(root.anchor),
        "filesystem_type": "fixture",
        "source": "fixture-device",
        "options": "rw",
    }
    mutable = root / "wb_autoanswers_runtime.sqlite3"
    _seed(mutable)
    policy = _mutable_fixture_policy(mutable)
    service_snapshot = _healthy_systemd_snapshot()
    registry = {"stores": {}}
    baseline = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=policy,
        store_registry=registry,
        service_snapshot=service_snapshot,
        opener_reader=lambda _path: [],
    )
    baseline_inode = mutable.stat().st_ino
    with mutable.open("ab") as handle:
        handle.write(b"\0" * 4096)
        handle.flush()
        os.fsync(handle.fileno())
    grown = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=policy,
        store_registry=registry,
        service_snapshot=service_snapshot,
        opener_reader=lambda _path: [],
    )
    assert mutable.stat().st_ino == baseline_inode
    assert baseline["topology_digest"] == grown["topology_digest"]
    growth_reconciliation = warm._reconcile_non_target(
        _non_target_fixture(baseline),
        _non_target_fixture(grown),
        phase="readiness_to_jit_autoanswers_growth",
    )
    assert growth_reconciliation["immutable_preserved"] is True
    assert growth_reconciliation["mutable_canonical_topology_preserved"] is True
    assert growth_reconciliation["mutable_canonical_evolution"][0][
        "ordinary_content_evolution_observed"
    ] is True

    before_material = {
        "immutable_non_target_digest": "sha256:" + "1" * 64,
        "mutable_canonical_topology": baseline["topology_rows"],
        "mutable_canonical_topology_digest": baseline["topology_digest"],
    }
    after_material = {
        **before_material,
        "mutable_canonical_topology": grown["topology_rows"],
        "mutable_canonical_topology_digest": grown["topology_digest"],
    }
    assert warm._digest(before_material) == warm._digest(after_material)

    replacement = root / "replacement.sqlite3"
    _seed(replacement)
    os.replace(replacement, mutable)
    replaced = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=policy,
        store_registry=registry,
        service_snapshot=service_snapshot,
        opener_reader=lambda _path: [],
    )
    try:
        warm._reconcile_non_target(
            _non_target_fixture(baseline),
            _non_target_fixture(replaced),
            phase="inode_replacement",
        )
    except warm.WarmArchiveError as exc:
        assert "topology reconciliation failed" in str(exc)
    else:
        raise AssertionError("mutable canonical inode replacement was admitted")

    mutable.unlink()
    mutable.symlink_to(root / "missing.sqlite3")
    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=policy,
            store_registry=registry,
            service_snapshot=service_snapshot,
            opener_reader=lambda _path: [],
        )
    except warm.WarmArchiveError as exc:
        assert "path is unsafe" in str(exc)
    else:
        raise AssertionError("mutable canonical symlink replacement was admitted")
    mutable.unlink()
    mutable.mkdir()
    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=policy,
            store_registry=registry,
            service_snapshot=service_snapshot,
            opener_reader=lambda _path: [],
        )
    except warm.WarmArchiveError as exc:
        assert "path is unsafe" in str(exc)
    else:
        raise AssertionError("mutable canonical type replacement was admitted")
    mutable.rmdir()
    _seed(mutable)

    for field, changed in (
        ("path", str(root / "moved.sqlite3")),
        ("device", 999999),
        ("mount", {"mount_id": 999, "source": "/dev/other"}),
        ("inode", baseline_inode + 1000),
        ("kind", "symlink"),
        ("uid", 99),
    ):
        drifted = json.loads(json.dumps(grown))
        drifted["topology_rows"][0]["topology"][field] = changed
        drifted["topology_digest"] = warm._digest(drifted["topology_rows"])
        try:
            warm._reconcile_non_target(
                _non_target_fixture(grown),
                _non_target_fixture(drifted),
                phase=f"mutable_{field}_drift",
            )
        except warm.WarmArchiveError:
            pass
        else:
            raise AssertionError(f"mutable canonical {field} drift was admitted")

    unknown_policy = _mutable_fixture_policy(mutable)
    unknown_policy["non_target_cas"]["active_mutable_canonical_stores"][0][
        "owner"
    ] = "unknown-owner"
    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=unknown_policy,
            store_registry=registry,
            service_snapshot=service_snapshot,
            opener_reader=lambda _path: [],
        )
    except warm.WarmArchiveError as exc:
        assert "unknown/unregistered" in str(exc)
    else:
        raise AssertionError("unknown mutable canonical owner was admitted")

    store_policy = _mutable_fixture_policy(mutable, resolver_type="store_registry")
    store_registry = {
        "stores": {
            "operational": {
                "logical_store": "operational",
                "path": str(mutable),
                "generation_id": "generation-a",
                "generation_epoch": "epoch-a",
                "relative_path": mutable.name,
                "schema_revision": "operational_v1",
                "source_fingerprint": "sha256:" + "2" * 64,
                "manifest_sha256": "sha256:" + "3" * 64,
            }
        }
    }
    registry_before = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=store_policy,
        store_registry=store_registry,
        service_snapshot=service_snapshot,
        opener_reader=lambda _path: [],
    )
    changed_registry = json.loads(json.dumps(store_registry))
    changed_registry["stores"]["operational"]["generation_id"] = "generation-b"
    registry_after = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=store_policy,
        store_registry=changed_registry,
        service_snapshot=service_snapshot,
        opener_reader=lambda _path: [],
    )
    try:
        warm._reconcile_non_target(
            _non_target_fixture(registry_before),
            _non_target_fixture(registry_after),
            phase="store_registry_identity_replacement",
        )
    except warm.WarmArchiveError:
        pass
    else:
        raise AssertionError("StoreRegistry identity replacement was admitted")

    access_policy = _mutable_fixture_policy(
        mutable,
        access_roles=[
            _access_role("wb-core-registry-http.service", "reader"),
            _access_role("wb-core-autoanswers-worker.service", "writer"),
        ],
    )
    registry_pid = int(
        service_snapshot["wb-core-registry-http.service"]["MainPID"]
    )
    registry_reader = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=access_policy,
        store_registry=registry,
        service_snapshot=service_snapshot,
        opener_reader=lambda _path: [
            _fd_opener(mutable, pid=registry_pid, access_mode="read_only")
        ],
    )
    accepted_reader = registry_reader["observation_rows"][0][
        "open_handle_relationships"
    ][0]
    assert accepted_reader["accepted"] is True
    assert accepted_reader["matched_unit"] == "wb-core-registry-http.service"
    assert accepted_reader["service_main_pid"] == registry_pid
    assert accepted_reader["service_health"]["healthy"] is True
    assert accepted_reader["declared_role"] == "reader"
    assert accepted_reader["accepted_reason"] == (
        "exact_healthy_declared_mainpid_and_access_mode"
    )

    data_mcp_pid = int(service_snapshot["wb-core-data-mcp.service"]["MainPID"])
    for opener, reason in (
        (
            _fd_opener(mutable, pid=data_mcp_pid, access_mode="read_only"),
            "undeclared_service",
        ),
        (
            _fd_opener(mutable, pid=999999, access_mode="read_only"),
            "undeclared_or_non_main_pid",
        ),
        (
            _fd_opener(mutable, pid=registry_pid, access_mode="read_write"),
            "access_mode_not_allowed",
        ),
        (
            _fd_opener(
                mutable,
                pid=registry_pid,
                access_mode="read_only",
                device=mutable.stat().st_dev + 1,
            ),
            "fd_device_inode_binding_mismatch",
        ),
        (
            _fd_opener(mutable, pid=registry_pid, access_mode="unknown"),
            "unknown_access_mode",
        ),
    ):
        try:
            warm._active_mutable_canonical_snapshot(
                runtime_dir=root,
                policy=access_policy,
                store_registry=registry,
                service_snapshot=service_snapshot,
                opener_reader=lambda _path, value=opener: [value],
            )
        except warm.WarmArchiveError as exc:
            assert "invalid open-handle access relationship" in str(exc)
            assert exc.evidence["openers"][0]["rejected_reason"] == reason
        else:
            raise AssertionError(f"invalid mutable opener was admitted: {reason}")

    active_writer_snapshot = json.loads(json.dumps(service_snapshot))
    writer_pid = 42002
    active_writer_snapshot["wb-core-autoanswers-worker.service"].update(
        {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": str(writer_pid),
        }
    )
    active_writer_snapshot["wb-core-autoanswers-worker.timer"].update(
        {"ActiveState": "active", "SubState": "running"}
    )
    accepted_writer = warm._active_mutable_canonical_snapshot(
        runtime_dir=root,
        policy=access_policy,
        store_registry=registry,
        service_snapshot=active_writer_snapshot,
        opener_reader=lambda _path: [
            _fd_opener(mutable, pid=writer_pid, access_mode="read_write")
        ],
    )
    writer_relationship = accepted_writer["observation_rows"][0][
        "open_handle_relationships"
    ][0]
    assert writer_relationship["accepted"] is True
    assert writer_relationship["declared_role"] == "writer"

    unhealthy_writer_snapshot = json.loads(json.dumps(active_writer_snapshot))
    unhealthy_writer_snapshot["wb-core-autoanswers-worker.service"].update(
        {"Result": "exit-code", "ExecMainStatus": "1"}
    )
    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=access_policy,
            store_registry=registry,
            service_snapshot=unhealthy_writer_snapshot,
            opener_reader=lambda _path: [
                _fd_opener(mutable, pid=writer_pid, access_mode="read_write")
            ],
        )
    except warm.WarmArchiveError as exc:
        assert exc.evidence["openers"][0]["rejected_reason"] == (
            "matched_service_unhealthy"
        )
    else:
        raise AssertionError("unhealthy writer MainPID was admitted")

    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=access_policy,
            store_registry=registry,
            service_snapshot=active_writer_snapshot,
            opener_reader=lambda _path: [
                _fd_opener(mutable, pid=writer_pid + 1, access_mode="read_write")
            ],
        )
    except warm.WarmArchiveError as exc:
        assert exc.evidence["openers"][0]["rejected_reason"] == (
            "undeclared_or_non_main_pid"
        )
    else:
        raise AssertionError("non-MainPID writer was admitted")

    ambiguous_snapshot = json.loads(json.dumps(active_writer_snapshot))
    ambiguous_snapshot["wb-core-autoanswers-readonly-sync.service"].update(
        {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": str(writer_pid),
        }
    )
    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=access_policy,
            store_registry=registry,
            service_snapshot=ambiguous_snapshot,
            opener_reader=lambda _path: [
                _fd_opener(mutable, pid=writer_pid, access_mode="read_write")
            ],
        )
    except warm.WarmArchiveError as exc:
        assert exc.evidence["openers"][0]["rejected_reason"] == (
            "multiple_unit_mainpid_ambiguity"
        )
    else:
        raise AssertionError("multiple-unit MainPID ambiguity was admitted")

    unexpected_opener = lambda _path: [
        _fd_opener(mutable, pid=999999, access_mode="read_write")
    ]
    try:
        warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=policy,
            store_registry=registry,
            service_snapshot=service_snapshot,
            opener_reader=unexpected_opener,
        )
    except warm.WarmArchiveError as exc:
        assert "invalid open-handle access relationship" in str(exc)
    else:
        raise AssertionError("unexpected mutable canonical opener was admitted")

    access_policy_drift = json.loads(json.dumps(grown))
    access_policy_drift["topology_rows"][0]["access_roles"][0][
        "allowed_access_modes"
    ] = ["read_only"]
    access_policy_drift["topology_digest"] = warm._digest(
        access_policy_drift["topology_rows"]
    )
    try:
        warm._reconcile_non_target(
            _non_target_fixture(grown),
            _non_target_fixture(access_policy_drift),
            phase="mutable_access_policy_drift",
        )
    except warm.WarmArchiveError:
        pass
    else:
        raise AssertionError("mutable canonical access-policy drift was admitted")

    for reason in ("add", "remove", "content", "stat"):
        try:
            warm._reconcile_non_target(
                _non_target_fixture(grown),
                _non_target_fixture(
                    grown, immutable_digest="sha256:" + reason[0] * 64
                ),
                phase=f"immutable_{reason}_drift",
            )
        except warm.WarmArchiveError:
            pass
        else:
            raise AssertionError(f"immutable non-target {reason} drift was admitted")

    exact_policy = warm.TARGET_POLICIES[0]
    exact_target = {
        "key": exact_policy["key"],
        "source_path": exact_policy["source_path"],
        "sidecars": [],
    }
    warm._assert_exact_source_unlink_authority(
        Path(exact_policy["source_path"]), exact_target
    )
    try:
        warm._assert_exact_source_unlink_authority(
            root / "non-target.sqlite3", exact_target
        )
    except warm.WarmArchiveError as exc:
        assert "literal six-path authority" in str(exc)
    else:
        raise AssertionError("non-target unlink path was admitted")
    exact_journal = {
        "items": [
            {
                "key": item["key"],
                "unlink_count": 1,
                "archive_path": str(
                    warm.DESTINATION_ROOT
                    / warm.DESTINATION_FAMILY_NAME
                    / item["archive_name"]
                ),
                "manifest_path": str(
                    warm.DESTINATION_ROOT
                    / warm.DESTINATION_FAMILY_NAME
                    / (item["archive_name"] + ".manifest.json")
                ),
            }
            for item in warm.TARGET_POLICIES
        ],
        "non_target_before": {
            "mutable_canonical": {
                "topology_rows": grown["topology_rows"],
            }
        },
        "promo_action_count": 0,
        "business_data_mutation_count": 0,
    }
    mutation_scope = warm._mutation_scope_reconciliation(exact_journal)
    assert mutation_scope["exact"] is True
    assert mutation_scope["non_target_unlink_move_write_count"] == 0
    escaped_journal = json.loads(json.dumps(exact_journal))
    escaped_journal["items"][0]["archive_path"] = str(mutable)
    assert warm._mutation_scope_reconciliation(escaped_journal)["exact"] is False
    warm._mount_identity = original_mount_identity


def _exercise_readiness_to_jit_autoanswers_growth(root: Path) -> None:
    root = root.resolve()
    readiness_id = "readiness-v2-" + "d" * 32 + "-a01"
    operation_id = "production-goal-v1-" + "e" * 32
    readiness_root = root / "readiness"
    production_root = root / "production-goals"
    readiness_dir = readiness_root / readiness_id
    evidence_dir = production_root / operation_id
    readiness_dir.mkdir(parents=True, mode=0o700)
    evidence_dir.mkdir(parents=True, mode=0o700)
    os.chmod(readiness_dir, 0o700)
    os.chmod(evidence_dir, 0o700)
    runtime = root / "runtime"
    root_backups = root / "root-backups"
    runtime.mkdir()
    root_backups.mkdir()
    deployed_sha = "f" * 40
    deployed_marker = root / ".wb-core-runtime-sha"
    deployed_marker.write_text(deployed_sha + "\n", encoding="utf-8")
    autoanswers = root / "wb_autoanswers_runtime.sqlite3"
    autoanswers.unlink(missing_ok=True)
    _seed(autoanswers)
    policy = _mutable_fixture_policy(
        autoanswers,
        access_roles=[
            _access_role("wb-core-registry-http.service", "reader"),
            _access_role("wb-core-autoanswers-worker.service", "writer"),
        ],
    )
    service_snapshot = _healthy_systemd_snapshot()
    registry_pid = int(
        service_snapshot["wb-core-registry-http.service"]["MainPID"]
    )
    original_mount_identity = warm._mount_identity
    original_material_snapshot = warm._material_snapshot
    original_service_gate = warm._systemd_service_gate_with_resample
    original_stabilize = warm._stabilize_activity
    original_readiness_root = warm.READINESS_EVIDENCE_ROOT
    original_production_root = warm.PRODUCTION_GOAL_EVIDENCE_ROOT
    original_atomic_write = warm._atomic_write_json
    warm._mount_identity = lambda path: {
        "mount_id": 1,
        "mount_point": str(root.anchor),
        "filesystem_type": "fixture",
        "source": "fixture-device",
        "options": "rw",
    }
    warm.READINESS_EVIDENCE_ROOT = readiness_root
    warm.PRODUCTION_GOAL_EVIDENCE_ROOT = production_root
    healthy_gate = {
        "healthy": True,
        "classification": "healthy",
        "expected_unit_count": 27,
        "observed_unit_count": 27,
        "expected_pair_count": 12,
        "observed_pair_count": 12,
        "units": [],
        "pairs": [],
        "failing_unit_count": 0,
        "failing_pair_count": 0,
        "resample_required_pair_names": [],
        "pair_resample_evidence": {"samples": []},
    }
    calls = 0

    def material_snapshot(**_kwargs: object) -> tuple[dict[str, object], dict[str, object]]:
        nonlocal calls
        calls += 1
        service_for_call = json.loads(json.dumps(service_snapshot))
        if calls == 2:
            with autoanswers.open("ab") as handle:
                handle.write(b"\0" * 4096)
                handle.flush()
                os.fsync(handle.fileno())
            service_for_call["wb-core-registry-http.service"]["MainPID"] = str(
                registry_pid + 5000
            )
            timer_name = next(
                name for name in warm.SERVICE_NAMES if name.endswith(".timer")
            )
            service_for_call[timer_name]["LastTriggerUSec"] = (
                "Wed 2026-08-26 17:18:00 UTC"
            )
        mutable_snapshot = warm._active_mutable_canonical_snapshot(
            runtime_dir=root,
            policy=policy,
            store_registry={"stores": {}},
            service_snapshot=service_for_call,
            opener_reader=lambda _path: [
                _fd_opener(
                    autoanswers,
                    pid=int(
                        service_for_call["wb-core-registry-http.service"][
                            "MainPID"
                        ]
                    ),
                    access_mode="read_only",
                )
            ],
        )
        targets = [
            {
                "key": item["key"],
                "source_path": item["source_path"],
                "archive_name": item["archive_name"],
                "owner": item["owner"],
                "family": item["family"],
                "restore_role": item["restore_role"],
                "projected_archive_size_bytes": 1,
                "identity": dict(item["expected_identity"]),
            }
            for item in warm.TARGET_POLICIES
        ]
        immutable_digest = "sha256:" + "1" * 64
        material = {
            "contract_name": warm.CONTRACT_NAME,
            "profile": warm.PROFILE,
            "material_partition": "immutable_safety_v1",
            "source_count": 6,
            "destination_root": str(warm.DESTINATION_ROOT),
            "destination_family": str(
                warm.DESTINATION_ROOT / warm.DESTINATION_FAMILY_NAME
            ),
            "targets": targets,
            "filesystems": {},
            "store_registry": {"identity_digest": "sha256:" + "2" * 64},
            "root_policy": {
                "policy_sha256": "sha256:" + "3" * 64,
                "target_rows": [],
                "protected_path_topology": [],
                "protected_path_topology_digest": warm._digest([]),
            },
            "immutable_non_target_digest": immutable_digest,
            "mutable_canonical_topology": mutable_snapshot["topology_rows"],
            "mutable_canonical_topology_digest": mutable_snapshot[
                "topology_digest"
            ],
            "expected_unlink_count": 6,
            "expected_reclaimed_allocated_bytes": 27_591_725_056,
            "root_minimum_after_bytes": warm.ROOT_MINIMUM_AFTER_BYTES,
            "control_artifact_reserve_bytes": warm.CONTROL_ARTIFACT_RESERVE_BYTES,
            "compression": "zstd-level-1-single-thread",
        }
        activity = [
            {
                "classification": "clean",
                "read_only_opener_count": 0,
                "write_capable_or_unknown_opener_count": 0,
                "kernel_locks": [],
                "hold_evidence": {"marker_paths": [], "hold_xattr_names": []},
            }
            for _ in range(6)
        ]
        non_target = {
            "immutable_digest": immutable_digest,
            "mutable_canonical": mutable_snapshot,
            "mutable_canonical_topology_digest": mutable_snapshot[
                "topology_digest"
            ],
        }
        observations = {
            "activity_gates": activity,
            "filesystems_before": {"root": {}, "backup": {}},
            "journald": {},
            "services": service_for_call,
            "systemd_service_gate": healthy_gate,
            "finance": {
                "status": "healthy",
                "healthy": True,
                "blockers": [],
                "retained_backup_id": f"fixture-{calls}",
                "retained_count": 1,
                "retained_bytes": 1,
                "next_replacement_required_bytes": 1,
                "required_available_floor_bytes": 2,
                "available_bytes": 10,
            },
            "non_target": non_target,
            "capacity_stages": [
                {
                    "key": item["key"],
                    "projected_available_at_peak_bytes": 10,
                    "sufficient": True,
                }
                for item in warm.TARGET_POLICIES
            ],
            "projected_root_available_bytes": warm.ROOT_MINIMUM_AFTER_BYTES,
            "active_sanitation_jobs": [],
            "lifecycle_locks": [
                {"path": name, "present": False, "locked": False}
                for name in warm.OTHER_LIFECYCLE_LOCKS
            ],
        }
        return material, observations

    warm._material_snapshot = material_snapshot
    warm._systemd_service_gate_with_resample = lambda *_args, **_kwargs: healthy_gate
    warm._stabilize_activity = lambda **_kwargs: {
        "status": "clean",
        "samples": [],
        "callback": [],
    }
    try:
        ready = warm.readiness(
            runtime_dir=runtime,
            root_backups=root_backups,
            deployed_sha=deployed_sha,
            deployed_sha_file=deployed_marker,
            evidence_dir=readiness_dir,
            readiness_id=readiness_id,
        )
        assert ready["status"] == "ready"
        assert calls == 2
        assert ready["mutable_canonical_observations"][0][
            "ordinary_mutable_fields"
        ]["apparent_size_bytes"] > 0
        live_shape_opener = ready["mutable_canonical_observations"][0][
            "open_handle_relationships"
        ][0]
        assert live_shape_opener["matched_unit"] == (
            "wb-core-registry-http.service"
        )
        assert live_shape_opener["declared_role"] == "reader"
        assert live_shape_opener["access_mode"] == "read_only"
        assert live_shape_opener["accepted"] is True
        dry = warm.dry_run(
            runtime_dir=runtime,
            root_backups=root_backups,
            deployed_sha=deployed_sha,
            deployed_sha_file=deployed_marker,
            evidence_dir=evidence_dir,
            operation_id=operation_id,
            projection_manifest=Path(ready["projection_manifest_path"]),
            projection_manifest_sha256=str(ready["projection_manifest_sha256"]),
        )
        assert dry["status"] == "ready"
        assert dry["material_qualification_digest"] == ready[
            "material_qualification_digest"
        ]
        first_mutations: list[Path] = []

        def first_mutation(path: Path, _payload: object) -> None:
            first_mutations.append(path)
            raise _MutationBoundary("readiness-jit first durable mutation reached")

        warm._atomic_write_json = first_mutation
        try:
            warm.apply_batch(
                runtime_dir=runtime,
                root_backups=root_backups,
                deployed_sha=deployed_sha,
                deployed_sha_file=deployed_marker,
                evidence_dir=evidence_dir,
                operation_id=operation_id,
                manifest_path=Path(dry["manifest_path"]),
                manifest_sha256=str(dry["manifest_sha256"]),
                approval_reference="github:owner:wbc0008-012",
            )
        except _MutationBoundary as exc:
            assert str(exc) == "readiness-jit first durable mutation reached"
        else:
            raise AssertionError("qualified readiness/JIT did not reach apply boundary")
        assert first_mutations == [
            evidence_dir / "root-warm-archive-apply.json"
        ]
    finally:
        warm._mount_identity = original_mount_identity
        warm._material_snapshot = original_material_snapshot
        warm._systemd_service_gate_with_resample = original_service_gate
        warm._stabilize_activity = original_stabilize
        warm.READINESS_EVIDENCE_ROOT = original_readiness_root
        warm.PRODUCTION_GOAL_EVIDENCE_ROOT = original_production_root
        warm._atomic_write_json = original_atomic_write


def _exercise_material_partition_and_failure_evidence(root: Path) -> None:
    material = {
        "contract_name": warm.CONTRACT_NAME,
        "profile": warm.PROFILE,
        "material_partition": "immutable_safety_v1",
        "source_count": 6,
        "destination_root": str(warm.DESTINATION_ROOT),
        "destination_family": str(
            warm.DESTINATION_ROOT / warm.DESTINATION_FAMILY_NAME
        ),
        "targets": [
            {
                "key": "fixture",
                "source_path": "/safe/fixture.sqlite3",
                "archive_name": "fixture.sqlite3.zst",
                "owner": "fixture-owner",
                "family": "fixture-family",
                "restore_role": "evidence",
                "identity": {
                    "path": "/safe/fixture.sqlite3",
                    "device": 1,
                    "inode": 2,
                    "apparent_size_bytes": 3,
                    "allocated_bytes": 4096,
                    "mtime_ns": 4,
                    "sha256": "sha256:" + "1" * 64,
                },
                "sidecars": [],
                "sqlite": {"integrity_check": "ok"},
                "provenance": {"digest": "sha256:" + "2" * 64},
                "hold_evidence": {
                    "classification": "clear",
                    "marker_paths": [],
                    "hold_xattr_names": [],
                },
                "projected_archive_size_bytes": 2,
            }
        ],
        "filesystems": {
            "root": {"path": "/", "device": 1, "mount": {"source": "/dev/sda1"}},
            "backup": {
                "path": str(warm.DESTINATION_ROOT),
                "device": 2,
                "mount": {"source": "/dev/sdb1"},
            },
            "generation": {
                "path": str(warm.GENERATION_ROOT),
                "device": 3,
                "mount": {"source": "/dev/sdc1"},
            },
        },
        "store_registry": {
            "identity_digest": "sha256:" + "3" * 64,
            "active_paths": ["/safe/current.sqlite3"],
            "manifest_file_sha256": "sha256:" + "4" * 64,
        },
        "root_policy": {
            "policy_sha256": "sha256:" + "5" * 64,
            "target_rows": [],
            "protected_path_topology": [
                {
                    "path": "/safe/non-target.sqlite3",
                    "device": 1,
                    "inode": 9,
                    "owner": "other",
                    "classification": "essential",
                    "registered": True,
                }
            ],
            "protected_path_topology_digest": "sha256:" + "6" * 64,
        },
        "immutable_non_target_digest": "sha256:" + "7" * 64,
        "mutable_canonical_topology": [
            {
                "key": "current",
                "owner": "current-owner",
                "classification": "essential",
                "resolver": {"type": "literal", "path": "/safe/current.sqlite3"},
                "access_roles": [],
                "allow_no_open_handles": True,
                "registry_identity": None,
                "topology": {
                    "path": "/safe/current.sqlite3",
                    "device": 1,
                    "inode": 10,
                    "mode": "0o600",
                    "uid": 1000,
                    "gid": 1000,
                },
            }
        ],
        "mutable_canonical_topology_digest": "sha256:" + "8" * 64,
        "expected_unlink_count": 6,
        "expected_reclaimed_allocated_bytes": 27_591_725_056,
        "root_minimum_after_bytes": warm.ROOT_MINIMUM_AFTER_BYTES,
        "control_artifact_reserve_bytes": warm.CONTROL_ARTIFACT_RESERVE_BYTES,
        "compression": "zstd-level-1-single-thread",
    }
    healthy_gate = {
        "classification": "healthy",
        "healthy": True,
        "units": [{"name": "service", "MainPID": "100", "healthy": True}],
        "pairs": [],
        "failing_units": [],
        "failing_pairs": [],
    }
    observations = {
        "systemd_service_gate": healthy_gate,
        "finance": {
            "status": "healthy",
            "healthy": True,
            "blockers": [],
            "retained_backup_id": "backup-a",
            "retained_count": 1,
            "retained_bytes": 10,
            "next_replacement_required_bytes": 10,
            "required_available_floor_bytes": 20,
            "available_bytes": 100,
        },
        "capacity_stages": [
            {"key": str(index), "projected_available_at_peak_bytes": 30}
            for index in range(6)
        ],
        "filesystems_before": {
            "root": {"available_bytes": 1},
            "backup": {"available_bytes": 100},
        },
        "projected_root_available_bytes": warm.ROOT_MINIMUM_AFTER_BYTES,
        "activity_gates": [
            {
                "source_path": f"/safe/{index}.sqlite3",
                "classification": "clean",
                "blockers": [],
                "fd_openers": [],
                "kernel_locks": [],
                "hold_evidence": {},
            }
            for index in range(6)
        ],
        "root_policy_protected_path_observations": [
            {
                "path": "/safe/non-target.sqlite3",
                "device": 1,
                "inode": 9,
                "owner": "other",
                "classification": "essential",
                "registered": True,
                "ordinary_mutable_fields": {"size_bytes": 10, "mtime_ns": 1},
            }
        ],
        "non_target": {
            "mutable_canonical": {
                "observation_rows": [
                    {
                        **material["mutable_canonical_topology"][0],
                        "ordinary_mutable_fields": {
                            "apparent_size_bytes": 10,
                            "mtime_ns": 1,
                        },
                        "open_handle_relationships": [],
                    }
                ]
            }
        },
        "active_sanitation_jobs": [],
        "lifecycle_locks": [
            {"path": name, "present": False, "locked": False}
            for name in warm.OTHER_LIFECYCLE_LOCKS
        ],
        "journald": {"service": {"MainPID": "200"}, "effective": {}, "inventory": []},
    }

    one_component = json.loads(json.dumps(material))
    one_component["targets"][0]["identity"]["mtime_ns"] = 5
    exact_diff = warm._material_cas_diff(material, one_component)
    assert exact_diff["changed_json_paths"] == ["/targets/0/identity"]
    assert exact_diff["components"][0]["classification"] == "exact_target_source"
    assert exact_diff["components"][0]["before_component_digest"] == warm._digest(
        material["targets"][0]["identity"]
    )
    assert exact_diff["components"][0]["after_component_digest"] == warm._digest(
        one_component["targets"][0]["identity"]
    )

    immutable_cases = (
        ("sidecars", "/targets/0/sidecars", "target_sidecar"),
        ("destination", "/filesystems/backup", "destination_or_mount_topology"),
        ("policy", "/root_policy", "immutable_non_target_or_policy"),
    )
    for case, path, classification in immutable_cases:
        changed = json.loads(json.dumps(material))
        if case == "sidecars":
            changed["targets"][0]["sidecars"] = [
                {"suffix": "-wal", "present": True}
            ]
        elif case == "destination":
            changed["filesystems"]["backup"]["mount"]["source"] = "/dev/other"
        else:
            changed["root_policy"]["policy_sha256"] = "sha256:" + "9" * 64
        diff = warm._material_cas_diff(material, changed)
        assert path in diff["changed_json_paths"]
        row = next(item for item in diff["components"] if item["json_path"] == path)
        assert row["classification"] == classification
        assert diff["exact_immutable_match"] is False

    evolved_observations = json.loads(json.dumps(observations))
    evolved_observations["systemd_service_gate"]["units"][0]["MainPID"] = "101"
    evolved_observations["finance"]["retained_backup_id"] = "backup-b"
    evolved_observations["finance"]["available_bytes"] = 110
    evolved_observations["capacity_stages"][0][
        "projected_available_at_peak_bytes"
    ] = 31
    evolved_observations["root_policy_protected_path_observations"][0][
        "ordinary_mutable_fields"
    ] = {"size_bytes": 11, "mtime_ns": 2}
    evolved_observations["non_target"]["mutable_canonical"][
        "observation_rows"
    ][0]["ordinary_mutable_fields"] = {
        "apparent_size_bytes": 11,
        "mtime_ns": 2,
    }
    observation_diff = warm._material_cas_diff(
        material,
        material,
        expected_observations=observations,
        observed_observations=evolved_observations,
    )
    assert observation_diff["exact_immutable_match"] is True
    classifications = {
        item["classification"] for item in observation_diff["observation_changes"]
    }
    assert "service_health_observation" in classifications
    assert "capacity_observation" in classifications
    assert "non_target_live_observation" in classifications
    assert warm._mutable_safety_predicates(
        evolved_observations, minimum_backup_floor_bytes=20
    )["passed"] is True

    for field in (
        "service_health_passed",
        "capacity_passed",
        "target_activity_passed",
        "lifecycle_locks_passed",
        "no_other_sanitation_job",
    ):
        blocked = json.loads(json.dumps(observations))
        if field == "service_health_passed":
            blocked["systemd_service_gate"]["healthy"] = False
        elif field == "capacity_passed":
            blocked["capacity_stages"][0]["projected_available_at_peak_bytes"] = 19
        elif field == "target_activity_passed":
            blocked["activity_gates"][0]["blockers"] = [
                {"code": "sqlite_sidecar_present"}
            ]
        elif field == "lifecycle_locks_passed":
            blocked["lifecycle_locks"][0]["locked"] = True
        else:
            blocked["active_sanitation_jobs"] = [
                {"job_id": "1" * 64, "status": "running"}
            ]
        predicates = warm._mutable_safety_predicates(
            blocked, minimum_backup_floor_bytes=20
        )
        assert predicates["passed"] is False
        assert predicates["predicates"][field] is False

    evidence_dir = root / "immutable-failure"
    evidence_dir.mkdir(mode=0o700)
    first = warm._persist_material_cas_failure(
        evidence_dir=evidence_dir,
        phase="mutation_start_immutable_material_cas",
        readiness_id="readiness-v2-" + "1" * 32 + "-a01",
        operation_id=OPERATION,
        job_id="2" * 64,
        deployed_sha="3" * 40,
        manifest_path=evidence_dir / "manifest.json",
        manifest_sha256="sha256:" + "4" * 64,
        component_diff=exact_diff,
    )
    original_raw = Path(first["artifact_path"]).read_bytes()
    later_matching = warm._persist_material_cas_failure(
        evidence_dir=evidence_dir,
        phase="later_matching_snapshot",
        readiness_id="readiness-v2-" + "1" * 32 + "-a01",
        operation_id=OPERATION,
        job_id="2" * 64,
        deployed_sha="3" * 40,
        manifest_path=evidence_dir / "manifest.json",
        manifest_sha256="sha256:" + "4" * 64,
        component_diff=warm._material_cas_diff(material, material),
    )
    assert later_matching["original_failure_preserved"] is True
    assert Path(first["artifact_path"]).read_bytes() == original_raw
    preserved = json.loads(original_raw)
    assert preserved["phase"] == "mutation_start_immutable_material_cas"
    assert preserved["component_diff"]["changed_json_paths"] == [
        "/targets/0/identity"
    ]


def _exercise_scoped_non_target_writer_progress(root: Path) -> None:
    hold_root = root / "scoped-non-target"
    hold_root.mkdir()
    target = hold_root / "target.sqlite3"
    unrelated = hold_root / "unrelated.sqlite3"
    _seed(target)
    _seed(unrelated)
    destination_root = root / "warm-destination"
    original_targets = warm.TARGET_POLICIES
    original_destination = warm.DESTINATION_ROOT
    warm.TARGET_POLICIES = (
        {
            "source_path": str(target),
            "hold_root": str(hold_root),
            "archive_name": "target.sqlite3.zst",
        },
    )
    warm.DESTINATION_ROOT = destination_root
    try:
        baseline = warm._immutable_non_target_snapshot()
        baseline_inode = unrelated.stat().st_ino
        with unrelated.open("ab") as handle:
            handle.write(b"writer-progress")
            handle.flush()
            os.fsync(handle.fileno())
        evolved = warm._immutable_non_target_snapshot()
        assert unrelated.stat().st_ino == baseline_inode
        assert baseline["immutable_digest"] == evolved["immutable_digest"]
        assert baseline["exact_family_observation_digest"] != evolved[
            "exact_family_observation_digest"
        ]

        replacement = hold_root / "replacement.sqlite3"
        _seed(replacement)
        os.replace(replacement, unrelated)
        replaced = warm._immutable_non_target_snapshot()
        assert replaced["immutable_digest"] != baseline["immutable_digest"]
    finally:
        warm.TARGET_POLICIES = original_targets
        warm.DESTINATION_ROOT = original_destination

    protected = root / "registered-large-output.sqlite3"
    _seed(protected)
    original_load_policy = warm.load_policy
    original_collect_status = warm.collect_root_storage_status
    original_registered = warm.registered_producer_for_path
    policy = {"contract": "fixture"}
    policy_sha = "sha256:" + "a" * 64

    def status_with(rows: list[dict[str, object]]) -> dict[str, object]:
        return {
            "policy_sha256": policy_sha,
            "large_root_files": rows,
            "unregistered_large_root_files": [],
            "status": "normal",
            "filesystems": {"root": {"available_bytes": 1}},
        }

    value = protected.stat()
    row = {
        "path": str(protected),
        "device": value.st_dev,
        "inode": value.st_ino,
        "size_bytes": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "registered": True,
        "owner": "fixture-owner",
        "classification": "essential",
    }
    warm.load_policy = lambda: policy
    warm.collect_root_storage_status = lambda **_kwargs: status_with([row])
    warm.registered_producer_for_path = lambda *_args, **_kwargs: {
        "owner": "fixture-owner",
        "classification": "essential",
    }
    try:
        baseline_policy = warm._root_policy_snapshot([], require_targets=False)
        with protected.open("ab") as handle:
            handle.write(b"below-or-above-scan-threshold-progress")
            handle.flush()
            os.fsync(handle.fileno())
        warm.collect_root_storage_status = lambda **_kwargs: status_with([])
        evolved_policy = warm._root_policy_snapshot(
            [],
            require_targets=False,
            expected_protected_topology=baseline_policy[
                "protected_path_topology"
            ],
        )
        assert evolved_policy["protected_path_topology_digest"] == baseline_policy[
            "protected_path_topology_digest"
        ]
        assert evolved_policy["protected_path_observations"] != baseline_policy[
            "protected_path_observations"
        ]
    finally:
        warm.load_policy = original_load_policy
        warm.collect_root_storage_status = original_collect_status
        warm.registered_producer_for_path = original_registered


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
    with tempfile.TemporaryDirectory(prefix="root-warm-archive-lock-smoke-") as raw:
        lock_root = Path(raw)
        _exercise_lock_contexts(lock_root)
        _exercise_apply_lock_path(lock_root)
        _exercise_non_target_cas_split(lock_root)
        _exercise_material_partition_and_failure_evidence(lock_root)
        _exercise_scoped_non_target_writer_progress(lock_root)
        _exercise_readiness_to_jit_autoanswers_growth(lock_root)
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

    failed_autoanswers_snapshot = _healthy_systemd_snapshot()
    failed_autoanswers_snapshot["wb-core-autoanswers-worker.service"].update(
        {"Result": "exit-code", "ExecMainStatus": "1"}
    )
    failed_autoanswers = warm._systemd_service_gate(failed_autoanswers_snapshot)
    assert failed_autoanswers["healthy"] is False
    assert failed_autoanswers["failing_pair_count"] == 1
    assert failed_autoanswers["resample_required_pair_names"] == []
    failed_autoanswers_row = next(
        item
        for item in failed_autoanswers["units"]
        if item["name"] == "wb-core-autoanswers-worker.service"
    )
    assert failed_autoanswers_row["classification"] == (
        "real_unhealthy_owning_service"
    )
    assert failed_autoanswers_row["reason_codes"][:2] == [
        "failed_result",
        "nonzero_or_invalid_exec_main_status",
    ]
    naturally_terminalized_autoanswers = warm._systemd_service_gate(
        _healthy_systemd_snapshot()
    )
    assert naturally_terminalized_autoanswers["healthy"] is True
    assert naturally_terminalized_autoanswers["failing_unit_count"] == 0
    assert naturally_terminalized_autoanswers["failing_pair_count"] == 0

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
    target_drift = dict(clean_activity)
    target_drift["identity_matches_expected"] = False
    assert warm._classify_activity_evidence(target_drift)[0]["code"] == (
        "source_identity_drift"
    )
    sidecar_drift = dict(clean_activity)
    sidecar_drift["sidecars"] = [
        {"suffix": "-wal", "path": "/fixture-wal", "present": True}
    ]
    assert warm._classify_activity_evidence(sidecar_drift)[0]["code"] == (
        "sqlite_sidecar_present"
    )
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
