#!/usr/bin/env python3
"""Query-only terminal reconciliation for the submitted WBC0008 exact-six job.

The trusted-main Apply Runner sends these exact source bytes to ``python3 -`` on
the canonical host.  The module deliberately has no write, lock-acquisition,
archive, decompression, SQLite or service-control primitive.
"""

from __future__ import annotations

from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Mapping


SCHEMA = "wb-core.root-warm-archive-reconciliation-probe/v3"
ARCHIVE_CONTRACT = "root_storage_warm_archive_wbc0008_006_v7"
JOB_CONTRACT = "storage_recovery_sanitation_job_v1"
EXACT_DEPLOYED_SHA = "7d83c5d0ddf6bf86d6359409ef0f9a7bb4ad4747"
EXACT_OPERATION_ID = "production-goal-v1-8692b24cb2491927bdadd5dec06a15d8"
EXACT_JOB_ID = "d8176c48b41b6d128aa9adacb3aa50f1d464dc318cc9cc8df58d3be637649d2d"
DEPLOYED_APP_ROOT = Path("/opt/wb-core-runtime/app")
DEPLOYED_SHA_FILE = DEPLOYED_APP_ROOT / ".wb-core-runtime-sha"
CANONICAL_SYSTEMD_MODULE = DEPLOYED_APP_ROOT / "apps/root_storage_warm_archive.py"
CANONICAL_SYSTEMD_MODULE_SHA256 = (
    "sha256:24c3a2243338419f755aff78583b978aa0e5197ffc9e6b215466c7d4a11f501d"
)
ROOT_MINIMUM_BYTES = 25 * 1024**3
CAPACITY_STABILITY_TOLERANCE_BYTES = 16 * 1024**2
MONITOR_MAX_AGE_SECONDS = 10 * 60
FINANCE_COPY_OVERHEAD_BYTES = 64 * 1024**2
FINANCE_EMERGENCY_RESERVE_BYTES = 8 * 1024**3
DESTINATION = Path(
    "/opt/wb-core-runtime/state/backups/root-warm-archive-wbc0008-006"
)
RUNTIME = Path("/opt/wb-core-runtime/state")
ROOT_MONITOR = Path("/var/lib/wb-core-root-storage-policy/status.json")
STORE_REGISTRY = RUNTIME / "storage_generation_manifest.json"
FINANCE_BACKUP_ROOT = RUNTIME / "backups/finance-storage-split-snapshots"
EXPECTED_FINANCE_FILES = {
    "backup_manifest.json",
    "finance_raw.sqlite3",
    "operational.sqlite3",
    "storage_generation_manifest.json",
}
LIFECYCLE_LOCKS = (
    ".business-data-maintenance-restore.lock",
    ".finance-storage-split.lock",
    ".finance-storage-stale-writer-recovery.lock",
)
EXPECTED_SERVICE_NAMES = (
    "wb-core-registry-http.service",
    "wb-ai-api.service",
    "wb-core-data-mcp.service",
    "wb-core-sheet-vitrina-refresh.service",
    "wb-core-sheet-vitrina-refresh.timer",
    "wb-core-sheet-vitrina-canary-restore.service",
    "wb-core-sheet-vitrina-canary-restore.timer",
    "wb-core-sheet-vitrina-closure-retry.service",
    "wb-core-sheet-vitrina-closure-retry.timer",
    "wb-core-feedbacks-auto-complaints-tick.service",
    "wb-core-feedbacks-auto-complaints-tick.timer",
    "wb-core-wb-finance-weekly.service",
    "wb-core-wb-finance-weekly.timer",
    "wb-core-finance-backup-rotation.service",
    "wb-core-root-storage-policy.timer",
    "wb-core-finance-backup-rotation.timer",
    "wb-core-warehouse-functional-sync.service",
    "wb-core-warehouse-functional-sync.timer",
    "wb-core-fbs-shadow-collector.service",
    "wb-core-fbs-shadow-collector.timer",
    "wb-core-fbs-warehouse-registry.service",
    "wb-core-fbs-warehouse-registry.timer",
    "wb-core-root-storage-policy.service",
    "wb-core-autoanswers-readonly-sync.service",
    "wb-core-autoanswers-readonly-sync.timer",
    "wb-core-autoanswers-worker.service",
    "wb-core-autoanswers-worker.timer",
)
EXPECTED_PERSISTENT_SERVICE_NAMES = frozenset(
    {
        "wb-core-registry-http.service",
        "wb-ai-api.service",
        "wb-core-data-mcp.service",
    }
)
EXPECTED_TIMER_SERVICE_PAIRS = tuple(
    (name, name.removesuffix(".timer") + ".service")
    for name in EXPECTED_SERVICE_NAMES
    if name.endswith(".timer")
)
CANONICAL_QUERY_ONLY_SYMBOLS = (
    "PERSISTENT_SERVICE_NAMES",
    "SERVICE_NAMES",
    "SYSTEMD_PAIR_RESAMPLE_INTERVAL_SECONDS",
    "SYSTEMD_PAIR_RESAMPLE_MAX_ATTEMPTS",
    "SYSTEMD_PAIR_RESAMPLE_MAX_SECONDS",
    "TIMER_SERVICE_PAIRS",
    "_systemd_service_gate_with_resample",
    "_systemd_snapshot",
    "_systemd_unit_row",
)
JOURNALD_SYSTEMD_PROPERTIES = (
    "Id,LoadState,ActiveState,SubState,Result,ExecMainStatus,MainPID,UnitFileState"
)
TIMER_NEXT_TRIGGER_PROPERTIES = (
    "Id,NextElapseUSecRealtime,NextElapseUSecMonotonic"
)


class ProbeError(RuntimeError):
    """An exact query-only reconciliation predicate failed closed."""


class SystemdGateError(ProbeError):
    """A 27/12 gate failed while retaining every exact pair observation."""

    def __init__(self, message: str, gate: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.gate = dict(gate)


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def payload_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _load_canonical_systemd_contract(
    *,
    app_root: Path = DEPLOYED_APP_ROOT,
    deployed_sha_file: Path | None = None,
) -> dict[str, Any]:
    """Verify and import only the deployed archive module's query-only gate."""

    sha_file = deployed_sha_file or app_root / DEPLOYED_SHA_FILE.name
    module_file = app_root / "apps/root_storage_warm_archive.py"
    for path, label in (
        (sha_file, "deployed SHA marker"),
        (module_file, "canonical systemd classifier module"),
    ):
        if path.is_symlink() or not path.is_file():
            raise ProbeError(f"{label} is unavailable or unsafe: {path}")
    deployed_sha = sha_file.read_text(encoding="utf-8").strip()
    if deployed_sha != EXACT_DEPLOYED_SHA:
        raise ProbeError("deployed SHA does not match the completed operation")
    module_sha256 = _sha256_file(module_file)
    if module_sha256 != CANONICAL_SYSTEMD_MODULE_SHA256:
        raise ProbeError("deployed canonical systemd classifier module drifted")
    app_root_text = str(app_root)
    if app_root_text not in sys.path:
        sys.path.insert(0, app_root_text)
    from apps.root_storage_warm_archive import (  # noqa: PLC0415
        PERSISTENT_SERVICE_NAMES,
        SERVICE_NAMES,
        SYSTEMD_PAIR_RESAMPLE_INTERVAL_SECONDS,
        SYSTEMD_PAIR_RESAMPLE_MAX_ATTEMPTS,
        SYSTEMD_PAIR_RESAMPLE_MAX_SECONDS,
        TIMER_SERVICE_PAIRS,
        _systemd_service_gate_with_resample,
        _systemd_snapshot,
        _systemd_unit_row,
    )

    loaded_module = sys.modules.get("apps.root_storage_warm_archive")
    loaded_file = Path(str(getattr(loaded_module, "__file__", ""))).resolve()
    if loaded_file != module_file.resolve():
        raise ProbeError("canonical systemd classifier imported from a foreign path")
    if (
        tuple(SERVICE_NAMES) != EXPECTED_SERVICE_NAMES
        or frozenset(PERSISTENT_SERVICE_NAMES)
        != EXPECTED_PERSISTENT_SERVICE_NAMES
        or tuple(TIMER_SERVICE_PAIRS) != EXPECTED_TIMER_SERVICE_PAIRS
        or len(SERVICE_NAMES) != 27
        or len(TIMER_SERVICE_PAIRS) != 12
    ):
        raise ProbeError("deployed canonical systemd literal contract drifted")
    identity = {
        "deployed_sha": deployed_sha,
        "deployed_sha_file": str(sha_file),
        "module_path": str(module_file),
        "module_sha256": module_sha256,
        "archive_contract": ARCHIVE_CONTRACT,
        "service_names": list(SERVICE_NAMES),
        "service_names_digest": payload_digest(list(SERVICE_NAMES)),
        "timer_service_pairs": [list(item) for item in TIMER_SERVICE_PAIRS],
        "query_only_symbols": list(CANONICAL_QUERY_ONLY_SYMBOLS),
    }
    return {
        "identity": identity,
        "persistent_service_names": frozenset(PERSISTENT_SERVICE_NAMES),
        "service_names": tuple(SERVICE_NAMES),
        "timer_service_pairs": tuple(TIMER_SERVICE_PAIRS),
        "snapshot": _systemd_snapshot,
        "classify_with_resample": _systemd_service_gate_with_resample,
        "unit_row": _systemd_unit_row,
        "max_attempts": int(SYSTEMD_PAIR_RESAMPLE_MAX_ATTEMPTS),
        "max_seconds": float(SYSTEMD_PAIR_RESAMPLE_MAX_SECONDS),
        "interval_seconds": float(SYSTEMD_PAIR_RESAMPLE_INTERVAL_SECONDS),
    }


def _mapped(path: Path | str, root_prefix: Path | None) -> Path:
    value = Path(path)
    if root_prefix is None:
        return value
    if not value.is_absolute():
        raise ProbeError(f"probe path is not absolute: {value}")
    return root_prefix / str(value).lstrip("/")


def _read_json(path: Path, *, root_prefix: Path | None, label: str) -> tuple[dict[str, Any], bytes]:
    local = _mapped(path, root_prefix)
    if local.is_symlink() or not local.is_file():
        raise ProbeError(f"{label} is unavailable or unsafe: {path}")
    raw = local.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProbeError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ProbeError(f"{label} is not a JSON object")
    return value, raw


def _identity(
    path: Path | str,
    *,
    root_prefix: Path | None,
    include_hash: bool,
) -> dict[str, Any]:
    logical = Path(path)
    local = _mapped(logical, root_prefix)
    if local.is_symlink() or not local.exists():
        raise ProbeError(f"required path is unavailable or unsafe: {logical}")
    value = local.lstat()
    result: dict[str, Any] = {
        "path": str(logical),
        "device": int(value.st_dev),
        "inode": int(value.st_ino),
        "mode": oct(stat.S_IMODE(value.st_mode)),
        "uid": int(value.st_uid),
        "gid": int(value.st_gid),
        "kind": "file" if stat.S_ISREG(value.st_mode) else "directory" if stat.S_ISDIR(value.st_mode) else "other",
    }
    if stat.S_ISREG(value.st_mode):
        result.update(
            {
                "size_bytes": int(value.st_size),
                "allocated_bytes": int(value.st_blocks * 512),
                "nlink": int(value.st_nlink),
            }
        )
        if include_hash:
            result["sha256"] = _sha256_file(local)
    return result


def _require_equal(actual: Any, expected: Any, label: str) -> None:
    if actual != expected:
        raise ProbeError(f"{label} mismatch")


def _require_sha(value: Any, label: str) -> str:
    text = str(value or "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", text) is None:
        raise ProbeError(f"{label} is not an exact SHA-256")
    return text


def _validate_source_binding(config: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "operation_id": r"production-goal-v1-[0-9a-f]{32}",
        "job_id": r"[0-9a-f]{64}",
        "deployed_sha": r"[0-9a-f]{40}",
    }
    result: dict[str, Any] = {}
    for field, pattern in required.items():
        value = str(config.get(field) or "")
        if re.fullmatch(pattern, value) is None:
            raise ProbeError(f"source binding is invalid: {field}")
        result[field] = value
    if (
        result["operation_id"] != EXACT_OPERATION_ID
        or result["job_id"] != EXACT_JOB_ID
        or result["deployed_sha"] != EXACT_DEPLOYED_SHA
    ):
        raise ProbeError("source binding is not the completed exact-six operation")
    for field in ("manifest_sha256", "job_request_digest", "job_result_digest"):
        result[field] = _require_sha(config.get(field), field)
    result["manifest_path"] = str(config.get("manifest_path") or "")
    expected_manifest = (
        f"/opt/wb-core-runtime/state/private-evidence/production-goals/"
        f"{result['operation_id']}/root-warm-archive-plan-"
    )
    if (
        not result["manifest_path"].startswith(expected_manifest)
        or re.fullmatch(
            r"/opt/wb-core-runtime/state/private-evidence/production-goals/"
            r"production-goal-v1-[0-9a-f]{32}/"
            r"root-warm-archive-plan-[0-9]{8}T[0-9]{6}Z(?:-[0-9]+)?\.json",
            result["manifest_path"],
        )
        is None
    ):
        raise ProbeError("source manifest path binding is invalid")
    result["expected_reclaimed_allocated_bytes"] = int(
        config.get("expected_reclaimed_allocated_bytes") or 0
    )
    result["required_backup_floor_bytes"] = int(
        config.get("required_backup_floor_bytes") or 0
    )
    if (
        result["expected_reclaimed_allocated_bytes"] != 27_591_725_056
        or result["required_backup_floor_bytes"] <= 0
    ):
        raise ProbeError("source numeric binding is invalid")
    return result


def _job_and_journal(
    config: Mapping[str, Any], *, root_prefix: Path | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    binding = _validate_source_binding(config)
    manifest, manifest_raw = _read_json(
        Path(binding["manifest_path"]), root_prefix=root_prefix, label="qualified manifest"
    )
    _require_equal(_sha256_bytes(manifest_raw), binding["manifest_sha256"], "manifest SHA-256")
    evidence_dir = Path(binding["manifest_path"]).parent
    journal_path = evidence_dir / "root-warm-archive-apply.json"
    journal, journal_raw = _read_json(
        journal_path, root_prefix=root_prefix, label="durable warm archive journal"
    )
    _require_equal(journal.get("contract_name"), ARCHIVE_CONTRACT, "journal contract")
    _require_equal(journal.get("status"), "complete", "journal terminal state")
    _require_equal(journal.get("operation_id"), binding["operation_id"], "journal operation")
    _require_equal(journal.get("deployed_sha"), binding["deployed_sha"], "journal deployed SHA")
    _require_equal(journal.get("manifest_path"), binding["manifest_path"], "journal manifest path")
    _require_equal(journal.get("manifest_sha256"), binding["manifest_sha256"], "journal manifest digest")
    _require_equal(payload_digest(journal), binding["job_result_digest"], "journal semantic digest")
    if (
        journal.get("applied") is not True
        or int(journal.get("mutation_submit_count") or 0) != 1
        or int(journal.get("promo_action_count", -1)) != 0
        or int(journal.get("business_data_mutation_count", -1)) != 0
    ):
        raise ProbeError("journal mutation ledger is not exact terminal one-submit")

    job_dir = RUNTIME / "storage-recovery-sanitation-jobs" / binding["job_id"]
    request, request_raw = _read_json(
        job_dir / "request.json", root_prefix=root_prefix, label="job request"
    )
    status_payload, status_raw = _read_json(
        job_dir / "status.json", root_prefix=root_prefix, label="job status"
    )
    result_record, result_raw = _read_json(
        job_dir / "result.json", root_prefix=root_prefix, label="job result"
    )
    request_material = {
        "contract_name": request.get("contract_name"),
        "job_id": request.get("job_id"),
        "deployed_sha": request.get("deployed_sha"),
        "operation": request.get("operation"),
        "manifest": request.get("manifest"),
        "manifest_sha256": request.get("manifest_sha256"),
        "goal_operation_id": request.get("goal_operation_id"),
        "approval_reference": request.get("approval_reference"),
    }
    _require_equal(payload_digest(request_material), binding["job_request_digest"], "job request digest")
    _require_equal(request.get("request_digest"), binding["job_request_digest"], "stored job request digest")
    expected_request = {
        "contract_name": JOB_CONTRACT,
        "job_id": binding["job_id"],
        "deployed_sha": binding["deployed_sha"],
        "operation": "warm-archive-apply",
        "manifest": binding["manifest_path"],
        "manifest_sha256": binding["manifest_sha256"],
        "goal_operation_id": binding["operation_id"],
    }
    for field, value in expected_request.items():
        _require_equal(request.get(field), value, f"job request {field}")
    if (
        status_payload.get("contract_name") != JOB_CONTRACT
        or status_payload.get("job_id") != binding["job_id"]
        or status_payload.get("request_digest") != binding["job_request_digest"]
        or status_payload.get("status") != "succeeded"
        or status_payload.get("terminal") is not True
        or int(status_payload.get("attempt") or 0) != 1
        or status_payload.get("result_digest") != binding["job_result_digest"]
    ):
        raise ProbeError("job status is not exact succeeded attempt 1")
    if (
        result_record.get("contract_name") != JOB_CONTRACT
        or result_record.get("job_id") != binding["job_id"]
        or result_record.get("request_digest") != binding["job_request_digest"]
        or result_record.get("result_digest") != binding["job_result_digest"]
        or result_record.get("result") != journal
    ):
        raise ProbeError("job result/journal binding drifted")
    return journal, {
        "manifest_file_sha256": _sha256_bytes(manifest_raw),
        "journal_path": str(journal_path),
        "journal_file_sha256": _sha256_bytes(journal_raw),
        "journal_semantic_digest": payload_digest(journal),
        "request_file_sha256": _sha256_bytes(request_raw),
        "status_file_sha256": _sha256_bytes(status_raw),
        "result_file_sha256": _sha256_bytes(result_raw),
        "request_digest": binding["job_request_digest"],
        "result_digest": binding["job_result_digest"],
        "status": "succeeded",
        "terminal": True,
        "attempt": 1,
    }


def _sha256_bytes(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _exact_archive_set(
    journal: Mapping[str, Any], *, root_prefix: Path | None
) -> dict[str, Any]:
    scope = journal.get("mutation_scope_reconciliation")
    if not isinstance(scope, Mapping) or scope.get("exact") is not True:
        raise ProbeError("journal mutation scope reconciliation is not exact")
    sources = sorted(str(item) for item in scope.get("expected_literal_unlink_paths") or [])
    outputs = sorted(str(item) for item in scope.get("expected_destination_output_paths") or [])
    if len(sources) != 6 or len(outputs) != 12 or len(set(sources)) != 6 or len(set(outputs)) != 12:
        raise ProbeError("journal exact-six source/destination scope is invalid")
    if any(_mapped(path, root_prefix).exists() or _mapped(path, root_prefix).is_symlink() for path in sources):
        raise ProbeError("one or more exact source paths are present")
    destination_local = _mapped(DESTINATION, root_prefix)
    if destination_local.is_symlink() or not destination_local.is_dir():
        raise ProbeError("destination family is unavailable or unsafe")
    observed = sorted(
        str(DESTINATION / item.name)
        for item in destination_local.iterdir()
    )
    _require_equal(observed, outputs, "exact 12-object destination inventory")
    items = journal.get("items")
    if not isinstance(items, list) or len(items) != 6:
        raise ProbeError("journal item count is not six")
    archives: list[dict[str, Any]] = []
    keys: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            raise ProbeError("journal item is malformed")
        key = str(item.get("key") or "")
        archive_path = str(item.get("archive_path") or "")
        manifest_path = str(item.get("manifest_path") or "")
        if (
            not key
            or key in keys
            or archive_path not in outputs
            or manifest_path not in outputs
            or item.get("phase") != "unlink_done"
            or item.get("source_absent") is not True
            or int(item.get("unlink_count") or 0) != 1
        ):
            raise ProbeError("journal unlink completion drifted")
        keys.add(key)
        archive = _identity(archive_path, root_prefix=root_prefix, include_hash=True)
        manifest_identity = _identity(manifest_path, root_prefix=root_prefix, include_hash=True)
        proof = item.get("archive_proof")
        if not isinstance(proof, Mapping):
            raise ProbeError("journal archive proof is missing")
        _require_equal(archive.get("sha256"), (proof.get("archive_identity") or {}).get("sha256"), "archive hash")
        _require_equal(manifest_identity.get("sha256"), (proof.get("manifest_identity") or {}).get("sha256"), "manifest hash")
        pair, _pair_raw = _read_json(
            Path(manifest_path), root_prefix=root_prefix, label=f"archive manifest {key}"
        )
        if (
            pair.get("contract_name") != ARCHIVE_CONTRACT
            or pair.get("operation_id") != journal.get("operation_id")
            or pair.get("target_key") != key
            or pair.get("archive_path") != archive_path
            or pair.get("archive_sha256") != archive.get("sha256")
            or pair.get("lifecycle_state") != "retained"
            or pair.get("source_removed") is not True
            or not isinstance(pair.get("source"), Mapping)
            or str(pair["source"].get("path") or "") not in sources
            or (pair.get("unlink_receipt") or {}).get("count") != 1
            or (pair.get("unlink_receipt") or {}).get("source_absent") is not True
        ):
            raise ProbeError("saved archive manifest proof binding drifted")
        for proof_name in ("stream_verification", "restore_proof", "published_pair_readback"):
            if not isinstance(pair.get(proof_name), Mapping):
                raise ProbeError(f"saved archive proof is missing: {proof_name}")
        restore = pair["restore_proof"]
        if (
            restore.get("quick_check") != "ok"
            or restore.get("integrity_check") != "ok"
            or restore.get("restored_sha256") != pair["source"].get("sha256")
            or int(restore.get("restored_size_bytes") or -1)
            != int(pair["source"].get("apparent_size_bytes") or -2)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", str(restore.get("schema_identity_sha256") or "")) is None
        ):
            raise ProbeError("saved full-restore/SQLite proof drifted")
        stream = pair["stream_verification"]
        if (
            stream.get("decompressed_sha256") != pair["source"].get("sha256")
            or int(stream.get("decompressed_size_bytes") or -1)
            != int(pair["source"].get("apparent_size_bytes") or -2)
        ):
            raise ProbeError("saved stream proof drifted")
        archives.append(
            {
                "key": key,
                "source_path": pair["source"]["path"],
                "archive": archive,
                "manifest": manifest_identity,
                "stream_proof_digest": payload_digest(stream),
                "restore_proof_digest": payload_digest(restore),
                "published_pair_readback_digest": payload_digest(
                    pair["published_pair_readback"]
                ),
                "schema_identity_sha256": restore["schema_identity_sha256"],
                "unlink_count": 1,
                "reclaimed_allocated_bytes": int(
                    item.get("reclaimed_allocated_bytes") or 0
                ),
            }
        )
    reclaimed = sum(item["reclaimed_allocated_bytes"] for item in archives)
    _require_equal(reclaimed, int(journal.get("expected_reclaimed_allocated_bytes") or 0), "reclaimed byte total")
    _require_equal(reclaimed, int(journal.get("reclaimed_allocated_bytes") or 0), "terminal reclaimed byte total")
    _require_equal(int(journal.get("raw_unlink_count") or 0), 6, "raw unlink count")
    return {
        "source_count": 6,
        "source_absent_count": 6,
        "destination_object_count": 12,
        "archive_count": 6,
        "manifest_count": 6,
        "foreign_object_count": 0,
        "temporary_object_count": 0,
        "partial_object_count": 0,
        "pending_object_count": 0,
        "destination_objects": outputs,
        "raw_unlink_count": 6,
        "reclaimed_allocated_bytes": reclaimed,
        "archives": sorted(archives, key=lambda row: row["key"]),
    }


def _validate_expected_identity(
    expected: Mapping[str, Any], *, root_prefix: Path | None
) -> dict[str, Any]:
    path = str(expected.get("path") or "")
    actual = _identity(
        path,
        root_prefix=root_prefix,
        include_hash=expected.get("kind") == "file" and "sha256" in expected,
    )
    fields = ("path", "device", "inode", "kind", "mode", "uid", "gid")
    if expected.get("kind") == "file":
        fields += ("size_bytes", "allocated_bytes")
        if "sha256" in expected:
            fields += ("sha256",)
    for field in fields:
        _require_equal(actual.get(field), expected.get(field), f"non-target {path} {field}")
    return actual


def _non_target_and_registry(
    journal: Mapping[str, Any], *, root_prefix: Path | None
) -> dict[str, Any]:
    before = journal.get("non_target_before")
    terminal = journal.get("terminal_non_target_reconciliation")
    if not isinstance(before, Mapping) or not isinstance(terminal, Mapping):
        raise ProbeError("saved non-target reconciliation is missing")
    if (
        terminal.get("immutable_preserved") is not True
        or terminal.get("mutable_canonical_topology_preserved") is not True
        or journal.get("immutable_non_target_digest_before")
        != journal.get("immutable_non_target_digest_after")
        or journal.get("mutable_canonical_topology_digest_before")
        != journal.get("mutable_canonical_topology_digest_after")
    ):
        raise ProbeError("saved terminal non-target reconciliation is not exact")
    immutable = (before.get("immutable") or {}).get("exact_family_observation_rows")
    if not isinstance(immutable, list) or not immutable:
        raise ProbeError("saved immutable non-target inventory is missing")
    direct_rows = [
        _validate_expected_identity(row, root_prefix=root_prefix)
        for row in immutable
        if isinstance(row, Mapping)
    ]
    if len(direct_rows) != len(immutable):
        raise ProbeError("saved immutable non-target inventory is malformed")
    mutable_rows = (before.get("mutable_canonical") or {}).get("topology_rows")
    if not isinstance(mutable_rows, list) or len(mutable_rows) != 3:
        raise ProbeError("mutable canonical topology inventory is invalid")
    mutable_direct: list[dict[str, Any]] = []
    for row in mutable_rows:
        topology = row.get("topology") if isinstance(row, Mapping) else None
        if not isinstance(topology, Mapping):
            raise ProbeError("mutable canonical topology row is malformed")
        actual = _identity(
            str(topology.get("path") or ""), root_prefix=root_prefix, include_hash=False
        )
        for field in ("path", "device", "inode", "kind", "mode", "uid", "gid"):
            _require_equal(actual.get(field), topology.get(field), f"mutable topology {field}")
        mutable_direct.append({"key": row.get("key"), "identity": actual})
    saved_registry = before.get("store_registry")
    if not isinstance(saved_registry, Mapping):
        raise ProbeError("saved StoreRegistry identity is missing")
    registry, registry_raw = _read_json(
        STORE_REGISTRY, root_prefix=root_prefix, label="StoreRegistry manifest"
    )
    _require_equal(
        _sha256_bytes(registry_raw),
        saved_registry.get("manifest_file_sha256"),
        "StoreRegistry file digest",
    )
    _require_equal(registry, saved_registry.get("manifest"), "StoreRegistry payload")
    active_paths = sorted(str(item) for item in saved_registry.get("active_paths") or [])
    if len(active_paths) != 2:
        raise ProbeError("StoreRegistry active path count drifted")
    return {
        "immutable_digest_before": journal["immutable_non_target_digest_before"],
        "immutable_digest_after": journal["immutable_non_target_digest_after"],
        "immutable_direct_row_count": len(direct_rows),
        "immutable_direct_rows": direct_rows,
        "immutable_direct_rows_digest": payload_digest(direct_rows),
        "mutable_canonical_topology_digest_before": journal[
            "mutable_canonical_topology_digest_before"
        ],
        "mutable_canonical_topology_digest_after": journal[
            "mutable_canonical_topology_digest_after"
        ],
        "mutable_canonical_direct": mutable_direct,
        "store_registry_manifest_sha256": _sha256_bytes(registry_raw),
        "store_registry_identity_digest": saved_registry.get("identity_digest"),
        "store_registry_active_paths": active_paths,
        "preserved": True,
    }


def _query_command(
    command: list[str],
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    allowed = bool(
        command[:2] == ["systemctl", "show"]
        or command[:2] == ["systemd-analyze", "cat-config"]
    )
    if not allowed:
        raise ProbeError("remote probe attempted a non-query command")
    completed = command_runner(
        command,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ProbeError(f"query command failed: {' '.join(command[:2])}")
    return completed


def _systemd_show(
    unit: str,
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, str]:
    completed = _query_command(
        [
            "systemctl",
            "show",
            "--no-pager",
            f"--property={JOURNALD_SYSTEMD_PROPERTIES}",
            unit,
        ],
        command_runner=command_runner,
    )
    values: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    if values.get("Id") != unit or values.get("LoadState") != "loaded":
        raise ProbeError(f"systemd unit identity/loaded state drifted: {unit}")
    return values


def _timer_next_trigger_observations(
    timer_names: tuple[str, ...],
    *,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for timer_name in timer_names:
        completed = _query_command(
            [
                "systemctl",
                "show",
                "--no-pager",
                f"--property={TIMER_NEXT_TRIGGER_PROPERTIES}",
                timer_name,
            ],
            command_runner=command_runner,
        )
        values: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if values.get("Id") != timer_name:
            raise ProbeError("timer next-trigger observation identity drifted")
        rows.append(
            {
                "timer_name": timer_name,
                "NextElapseUSecRealtime": values.get(
                    "NextElapseUSecRealtime", ""
                ),
                "NextElapseUSecMonotonic": values.get(
                    "NextElapseUSecMonotonic", ""
                ),
            }
        )
    return rows


def _service_health(
    job_id: str,
    journal: Mapping[str, Any],
    *,
    canonical_contract: Mapping[str, Any],
    snapshot_reader: Callable[[tuple[str, ...]], Mapping[str, Any]] | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    max_resample_attempts: int | None = None,
    max_resample_seconds: float | None = None,
    resample_interval_seconds: float | None = None,
) -> dict[str, Any]:
    """Invoke the verified deployed 27-unit canonical gate without reclassifying it."""

    service_names = tuple(canonical_contract.get("service_names") or ())
    timer_pairs = tuple(canonical_contract.get("timer_service_pairs") or ())
    persistent_names = frozenset(
        canonical_contract.get("persistent_service_names") or ()
    )
    if (
        service_names != EXPECTED_SERVICE_NAMES
        or timer_pairs != EXPECTED_TIMER_SERVICE_PAIRS
        or persistent_names != EXPECTED_PERSISTENT_SERVICE_NAMES
    ):
        raise ProbeError("canonical systemd contract binding drifted")
    classify = canonical_contract.get("classify_with_resample")
    canonical_snapshot = canonical_contract.get("snapshot")
    unit_row = canonical_contract.get("unit_row")
    if not callable(classify) or not callable(canonical_snapshot) or not callable(unit_row):
        raise ProbeError("canonical systemd query-only symbols are unavailable")

    reader = snapshot_reader or canonical_snapshot
    initial_snapshot = reader(service_names)
    gate = classify(
        initial_snapshot=initial_snapshot,
        snapshot_reader=reader,
        max_attempts=(
            int(canonical_contract["max_attempts"])
            if max_resample_attempts is None
            else max_resample_attempts
        ),
        max_seconds=(
            float(canonical_contract["max_seconds"])
            if max_resample_seconds is None
            else max_resample_seconds
        ),
        interval_seconds=(
            float(canonical_contract["interval_seconds"])
            if resample_interval_seconds is None
            else resample_interval_seconds
        ),
    )
    if not isinstance(gate, Mapping):
        raise ProbeError("canonical systemd gate did not return an object")
    units = gate.get("units")
    pairs = gate.get("pairs")
    resamples = gate.get("pair_resample_evidence")
    if (
        not isinstance(units, list)
        or not isinstance(pairs, list)
        or not isinstance(resamples, Mapping)
        or int(gate.get("observed_unit_count") or 0) != len(service_names)
        or int(gate.get("observed_pair_count") or 0) != len(timer_pairs)
        or [row.get("name") for row in units if isinstance(row, Mapping)]
        != list(service_names)
    ):
        raise ProbeError("canonical systemd 27/12 evidence is incomplete")
    timer_names = tuple(timer for timer, _owner in timer_pairs)
    if snapshot_reader is None:
        next_trigger_observations = _timer_next_trigger_observations(
            timer_names, command_runner=command_runner
        )
    else:
        next_trigger_observations = [
            {
                "timer_name": timer_name,
                "NextElapseUSecRealtime": str(
                    (initial_snapshot.get(timer_name) or {}).get(
                        "NextElapseUSecRealtime", ""
                    )
                ),
                "NextElapseUSecMonotonic": str(
                    (initial_snapshot.get(timer_name) or {}).get(
                        "NextElapseUSecMonotonic", ""
                    )
                ),
            }
            for timer_name in timer_names
        ]

    persistent_rows = [
        dict(row)
        for row in units
        if isinstance(row, Mapping) and row.get("name") in persistent_names
    ]
    job_unit_name = f"wb-core-storage-recovery-sanitation@{job_id}.service"
    job_snapshot = reader((job_unit_name,))
    job_values = (
        job_snapshot.get(job_unit_name)
        if isinstance(job_snapshot, Mapping)
        and isinstance(job_snapshot.get(job_unit_name), Mapping)
        else {}
    )
    job_row = unit_row(job_unit_name, job_values)
    job_unit_healthy = bool(
        isinstance(job_row, Mapping)
        and job_row.get("state_healthy") is True
        and job_row.get("phase") == "oneshot_inactive_success"
    )
    saved_gate = journal.get("systemd_service_gate_after")
    saved_gate_healthy = bool(
        isinstance(saved_gate, Mapping)
        and saved_gate.get("healthy") is True
        and int(saved_gate.get("observed_unit_count") or 0) == len(service_names)
        and int(saved_gate.get("observed_pair_count") or 0) == len(timer_pairs)
    )
    failing_pairs = [
        dict(pair)
        for pair in pairs
        if not isinstance(pair, Mapping) or pair.get("healthy") is not True
    ]
    failing_persistent = [
        row for row in persistent_rows if row.get("healthy") is not True
    ]
    healthy = bool(
        gate.get("healthy") is True
        and not failing_pairs
        and not failing_persistent
        and job_unit_healthy
        and saved_gate_healthy
    )
    result = {
        "schema": "wb-core.systemd-canonical-health-gate/v2",
        "classification": "healthy" if healthy else "required_units_unhealthy",
        "healthy": healthy,
        "canonical_contract": dict(canonical_contract["identity"]),
        "canonical_gate": dict(gate),
        "canonical_gate_digest": payload_digest(gate),
        "raw_initial_snapshot": dict(initial_snapshot),
        "raw_initial_snapshot_digest": payload_digest(initial_snapshot),
        "timer_next_trigger_observations": next_trigger_observations,
        "timer_next_trigger_observations_digest": payload_digest(
            next_trigger_observations
        ),
        "unit_count": len(units),
        "pair_count": len(pairs),
        "units": units,
        "pairs": pairs,
        "persistent_services": persistent_rows,
        "failing_pair_count": len(failing_pairs),
        "failing_pairs": failing_pairs,
        "failing_persistent_service_count": len(failing_persistent),
        "failing_persistent_services": failing_persistent,
        "pair_resample_evidence": dict(resamples),
        "completed_job_unit": {
            "name": job_unit_name,
            "healthy": job_unit_healthy,
            "row": job_row,
            "raw": dict(job_values),
        },
        "saved_terminal_gate": {
            "healthy": saved_gate_healthy,
            "observed_unit_count": (
                saved_gate.get("observed_unit_count")
                if isinstance(saved_gate, Mapping)
                else None
            ),
            "observed_pair_count": (
                saved_gate.get("observed_pair_count")
                if isinstance(saved_gate, Mapping)
                else None
            ),
        },
    }
    result["gate_digest"] = payload_digest(result)
    if not healthy:
        raise SystemdGateError("systemd 27/12 canonical health is not proven", result)
    return result


def _active_jobs_and_locks(
    job_id: str, *, root_prefix: Path | None
) -> dict[str, Any]:
    jobs_root = RUNTIME / "storage-recovery-sanitation-jobs"
    jobs_local = _mapped(jobs_root, root_prefix)
    if jobs_local.is_symlink() or not jobs_local.is_dir():
        raise ProbeError("sanitation job inventory is unavailable")
    rows: list[dict[str, Any]] = []
    for item in sorted(jobs_local.iterdir(), key=lambda value: value.name):
        if item.name == "worker.lock" and item.is_file() and not item.is_symlink():
            continue
        if not item.is_dir() or item.is_symlink() or re.fullmatch(r"[0-9a-f]{64}", item.name) is None:
            raise ProbeError("sanitation job inventory contains a foreign entry")
        payload, _raw = _read_json(
            jobs_root / item.name / "status.json",
            root_prefix=root_prefix,
            label=f"sanitation status {item.name}",
        )
        if payload.get("terminal") is not True or payload.get("status") not in {"succeeded", "failed"}:
            raise ProbeError("an active sanitation job remains")
        rows.append(
            {
                "job_id": item.name,
                "status": payload.get("status"),
                "terminal": payload.get("terminal"),
            }
        )
    own = [row for row in rows if row["job_id"] == job_id]
    if len(own) != 1 or own[0]["status"] != "succeeded":
        raise ProbeError("exact source sanitation job is not uniquely succeeded")
    lock_paths = [
        jobs_root / "worker.lock",
        jobs_root / job_id / "job.lock",
        *(RUNTIME / name for name in LIFECYCLE_LOCKS),
    ]
    lock_identities: list[dict[str, Any]] = []
    for logical in lock_paths:
        local = _mapped(logical, root_prefix)
        if local.is_symlink():
            raise ProbeError(f"lock path is a symlink: {logical}")
        if local.exists():
            value = local.stat()
            if not stat.S_ISREG(value.st_mode):
                raise ProbeError(f"lock path is not a file: {logical}")
            lock_identities.append(
                {
                    "path": str(logical),
                    "device_major": os.major(value.st_dev),
                    "device_minor": os.minor(value.st_dev),
                    "inode": int(value.st_ino),
                }
            )
    proc_locks = _mapped("/proc/locks", root_prefix)
    if not proc_locks.is_file():
        raise ProbeError("kernel lock inventory is unavailable")
    held: list[dict[str, Any]] = []
    identities = {
        (row["device_major"], row["device_minor"], row["inode"]): row["path"]
        for row in lock_identities
    }
    for line in proc_locks.read_text(encoding="utf-8").splitlines():
        for token in line.split():
            match = re.fullmatch(r"([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9]+)", token)
            if match is None:
                continue
            key = (int(match.group(1), 16), int(match.group(2), 16), int(match.group(3)))
            if key in identities:
                held.append({"path": identities[key], "line_sha256": _sha256_bytes(line.encode("utf-8"))})
    if held:
        raise ProbeError("an exact lifecycle/sanitation lock remains held")
    return {
        "observed_job_count": len(rows),
        "active_job_count": 0,
        "jobs": rows,
        "jobs_digest": payload_digest(rows),
        "observed_lock_file_count": len(lock_identities),
        "held_lock_count": 0,
        "lock_identities": lock_identities,
    }


def _capacity_samples(
    required_floor: int,
    *,
    root_prefix: Path | None,
    sleep_fn: Callable[[float], None],
    now_fn: Callable[[], datetime],
) -> dict[str, Any]:
    samples: list[dict[str, Any]] = []
    for index in range(3):
        row: dict[str, Any] = {"sample": index + 1, "observed_at": now_fn().isoformat().replace("+00:00", "Z")}
        for role, logical in (("root", Path("/")), ("backup", DESTINATION.parent)):
            value = os.statvfs(_mapped(logical, root_prefix))
            row[role] = {
                "available_bytes": int(value.f_bavail * value.f_frsize),
                "free_bytes": int(value.f_bfree * value.f_frsize),
                "total_bytes": int(value.f_blocks * value.f_frsize),
            }
        samples.append(row)
        if index < 2:
            sleep_fn(2.0)
    for role in ("root", "backup"):
        values = [int(row[role]["available_bytes"]) for row in samples]
        if max(values) - min(values) > CAPACITY_STABILITY_TOLERANCE_BYTES:
            raise ProbeError(f"{role} capacity is unstable across three samples")
    if min(int(row["root"]["available_bytes"]) for row in samples) < ROOT_MINIMUM_BYTES:
        raise ProbeError("root capacity is below the terminal minimum")
    if min(int(row["backup"]["available_bytes"]) for row in samples) < required_floor:
        raise ProbeError("backup capacity is below the Finance floor")
    return {
        "sample_count": 3,
        "stability_tolerance_bytes": CAPACITY_STABILITY_TOLERANCE_BYTES,
        "samples": samples,
        "root_minimum_bytes": ROOT_MINIMUM_BYTES,
        "backup_floor_bytes": required_floor,
        "root_stable": True,
        "backup_stable": True,
        "root_min_available_bytes": min(int(row["root"]["available_bytes"]) for row in samples),
        "backup_min_available_bytes": min(int(row["backup"]["available_bytes"]) for row in samples),
    }


def _finance_health(
    journal: Mapping[str, Any], capacity: Mapping[str, Any], *, root_prefix: Path | None, now_fn: Callable[[], datetime]
) -> dict[str, Any]:
    saved = journal.get("finance_after")
    if not isinstance(saved, Mapping) or saved.get("healthy") is not True:
        raise ProbeError("saved terminal Finance health is invalid")
    selector, selector_raw = _read_json(
        FINANCE_BACKUP_ROOT / "current.json", root_prefix=root_prefix, label="Finance current selector"
    )
    policy, policy_raw = _read_json(
        FINANCE_BACKUP_ROOT / "retention_policy.json", root_prefix=root_prefix, label="Finance retention policy"
    )
    backup_id = str(selector.get("backup_id") or "")
    if re.fullmatch(r"finance-backup-[0-9a-f]{20}", backup_id) is None:
        raise ProbeError("Finance current backup identity is invalid")
    retained = FINANCE_BACKUP_ROOT / "retained"
    retained_local = _mapped(retained, root_prefix)
    entries = sorted(retained_local.iterdir(), key=lambda value: value.name)
    if [item.name for item in entries] != [backup_id] or not entries[0].is_dir() or entries[0].is_symlink():
        raise ProbeError("Finance retained inventory is not exactly one selected set")
    selected = retained / backup_id
    selected_local = _mapped(selected, root_prefix)
    names = {item.name for item in selected_local.iterdir() if item.is_file() and not item.is_symlink()}
    if names != EXPECTED_FINANCE_FILES or len(list(selected_local.iterdir())) != 4:
        raise ProbeError("Finance retained file inventory drifted")
    manifest, manifest_raw = _read_json(
        selected / "backup_manifest.json", root_prefix=root_prefix, label="Finance backup manifest"
    )
    for payload, label in ((selector, "selector"), (policy, "policy"), (manifest, "manifest")):
        stored = payload.get("fingerprint")
        material = {key: value for key, value in payload.items() if key != "fingerprint"}
        _require_equal(stored, payload_digest(material), f"Finance {label} fingerprint")
    _require_equal(selector.get("backup_manifest_fingerprint"), manifest.get("fingerprint"), "Finance selected manifest")
    retained_bytes = 0
    identities: list[dict[str, Any]] = []
    for name in sorted(EXPECTED_FINANCE_FILES):
        identity = _identity(selected / name, root_prefix=root_prefix, include_hash=False)
        if identity["mode"] != "0o600":
            raise ProbeError("Finance retained file permission drifted")
        retained_bytes += int(identity["allocated_bytes"])
        identities.append(identity)
    _require_equal(retained_bytes, int(saved.get("retained_bytes") or 0), "Finance retained bytes")
    hard_reserve = int((policy.get("policy") or {}).get("hard_reserve_bytes") or FINANCE_EMERGENCY_RESERVE_BYTES)
    next_replacement_required = (
        retained_bytes + FINANCE_COPY_OVERHEAD_BYTES + hard_reserve
    )
    _require_equal(
        next_replacement_required,
        int(saved.get("next_replacement_required_bytes") or 0),
        "Finance next replacement requirement",
    )
    floor = next_replacement_required + FINANCE_EMERGENCY_RESERVE_BYTES
    _require_equal(floor, int(saved.get("required_available_floor_bytes") or 0), "Finance saved floor")
    _require_equal(floor, int(capacity.get("backup_floor_bytes") or 0), "Finance requested floor")
    captured_at = datetime.fromisoformat(str(manifest.get("captured_at") or "").replace("Z", "+00:00"))
    rpo = int((policy.get("policy") or {}).get("rpo_seconds") or 7 * 24 * 60 * 60)
    age = (now_fn().astimezone(timezone.utc) - captured_at.astimezone(timezone.utc)).total_seconds()
    if age < -5 or age > rpo:
        raise ProbeError("Finance retained backup is outside RPO")
    return {
        "healthy": True,
        "retained_backup_id": backup_id,
        "retained_count": 1,
        "retained_bytes": retained_bytes,
        "next_replacement_required_bytes": next_replacement_required,
        "required_available_floor_bytes": floor,
        "available_bytes": int(capacity["backup_min_available_bytes"]),
        "age_seconds": round(age, 3),
        "rpo_seconds": rpo,
        "selector_sha256": _sha256_bytes(selector_raw),
        "policy_sha256": _sha256_bytes(policy_raw),
        "manifest_sha256": _sha256_bytes(manifest_raw),
        "retained_files": identities,
        "retained_files_digest": payload_digest(identities),
    }


def _monitor(
    journal: Mapping[str, Any], capacity: Mapping[str, Any], *, root_prefix: Path | None, now_fn: Callable[[], datetime]
) -> dict[str, Any]:
    payload, raw = _read_json(ROOT_MONITOR, root_prefix=root_prefix, label="natural root monitor")
    if payload.get("contract_version") != "wb_core_root_storage_policy_v1":
        raise ProbeError("root monitor contract drifted")
    collected = datetime.fromisoformat(str(payload.get("collected_at") or "").replace("Z", "+00:00"))
    age = (now_fn().astimezone(timezone.utc) - collected.astimezone(timezone.utc)).total_seconds()
    root = (payload.get("filesystems") or {}).get("root") or {}
    if (
        age < -5
        or age > MONITOR_MAX_AGE_SECONDS
        or payload.get("status") != "normal"
        or payload.get("alerts") != []
        or payload.get("unregistered_large_root_files") != []
        or payload.get("safe_for_discretionary_root_writes") is not True
        or int(root.get("available_bytes") or 0) < ROOT_MINIMUM_BYTES
        or abs(int(root.get("available_bytes") or 0) - int(capacity["root_min_available_bytes"]))
        > 64 * 1024**2
    ):
        raise ProbeError("natural root monitor is stale, non-normal or capacity-inconsistent")
    saved_policy = ((journal.get("non_target_before") or {}).get("root_policy") or {}).get("policy_sha256")
    _require_equal(payload.get("policy_sha256"), saved_policy, "root monitor policy digest")
    return {
        "fresh": True,
        "normal": True,
        "age_seconds": round(age, 3),
        "max_age_seconds": MONITOR_MAX_AGE_SECONDS,
        "available_bytes": int(root["available_bytes"]),
        "artifact_sha256": _sha256_bytes(raw),
        "policy_sha256": payload.get("policy_sha256"),
        "large_root_file_count": len(payload.get("large_root_files") or []),
        "unregistered_large_root_file_count": 0,
    }


def _journald(
    journal: Mapping[str, Any], *, root_prefix: Path | None, command_runner: Callable[..., subprocess.CompletedProcess[str]]
) -> dict[str, Any]:
    before = journal.get("journald_before")
    saved_reconciliation = journal.get("journald_reconciliation")
    if not isinstance(before, Mapping) or not isinstance(saved_reconciliation, Mapping):
        raise ProbeError("saved journald evidence is missing")
    if (
        int(saved_reconciliation.get("deleted_count") or 0) != 0
        or saved_reconciliation.get("protected_drift")
        or saved_reconciliation.get("protected_identity_digest_before")
        != saved_reconciliation.get("protected_identity_digest_after")
    ):
        raise ProbeError("saved journald reconciliation is not preserved")
    service = _systemd_show("systemd-journald.service", command_runner=command_runner)
    if (
        service.get("ActiveState") != "active"
        or service.get("SubState") != "running"
        or service.get("MainPID") != (before.get("service") or {}).get("MainPID")
    ):
        raise ProbeError("journald service identity/PID drifted")
    config = _query_command(
        ["systemd-analyze", "cat-config", "systemd/journald.conf"],
        command_runner=command_runner,
    )
    config_digest = _sha256_bytes(config.stdout.encode("utf-8"))
    _require_equal(config_digest, (before.get("effective") or {}).get("cat_config_sha256"), "journald effective config")
    protected: list[dict[str, Any]] = []
    for row in before.get("inventory") or []:
        if not isinstance(row, Mapping):
            raise ProbeError("saved journald inventory row is malformed")
        logical = str(row.get("path") or "")
        actual = _identity(logical, root_prefix=root_prefix, include_hash=False)
        if actual["device"] != row.get("device") or actual["kind"] != "file":
            raise ProbeError("journald protected inventory device/type drifted")
        if row.get("mutable_current") is not True and (
            actual["inode"] != row.get("inode") or actual["size_bytes"] != row.get("size_bytes")
        ):
            raise ProbeError("journald immutable protected inventory drifted")
        protected.append(
            {
                "path": logical,
                "device": actual["device"],
                "inode": actual["inode"],
                "size_bytes": actual["size_bytes"],
                "mutable_current": row.get("mutable_current"),
            }
        )
    return {
        "preserved": True,
        "service_main_pid": service["MainPID"],
        "effective_config_sha256": config_digest,
        "protected_inventory_count": len(protected),
        "protected_inventory": protected,
        "protected_inventory_digest": payload_digest(protected),
        "deleted_count": 0,
    }


def run_probe(
    config: Mapping[str, Any],
    *,
    root_prefix: Path | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    canonical_systemd_contract: Mapping[str, Any] | None = None,
    canonical_snapshot_reader: (
        Callable[[tuple[str, ...]], Mapping[str, Any]] | None
    ) = None,
) -> dict[str, Any]:
    binding = _validate_source_binding(config)
    canonical_contract = (
        dict(canonical_systemd_contract)
        if canonical_systemd_contract is not None
        else _load_canonical_systemd_contract()
    )
    journal, job = _job_and_journal(config, root_prefix=root_prefix)
    archives = _exact_archive_set(journal, root_prefix=root_prefix)
    non_target = _non_target_and_registry(journal, root_prefix=root_prefix)
    active = _active_jobs_and_locks(binding["job_id"], root_prefix=root_prefix)
    capacity = _capacity_samples(
        binding["required_backup_floor_bytes"],
        root_prefix=root_prefix,
        sleep_fn=sleep_fn,
        now_fn=now_fn,
    )
    finance = _finance_health(
        journal, capacity, root_prefix=root_prefix, now_fn=now_fn
    )
    monitor = _monitor(journal, capacity, root_prefix=root_prefix, now_fn=now_fn)
    services = _service_health(
        binding["job_id"],
        journal,
        canonical_contract=canonical_contract,
        snapshot_reader=canonical_snapshot_reader,
        command_runner=command_runner,
    )
    journald = _journald(
        journal, root_prefix=root_prefix, command_runner=command_runner
    )
    if archives["reclaimed_allocated_bytes"] != binding["expected_reclaimed_allocated_bytes"]:
        raise ProbeError("reclaimed bytes escaped the exact source binding")
    result = {
        "schema": SCHEMA,
        "status": "reconciled",
        "query_only": True,
        "pythondontwritebytecode": os.environ.get("PYTHONDONTWRITEBYTECODE") == "1",
        "observed_at": now_fn().isoformat().replace("+00:00", "Z"),
        "operation_id": binding["operation_id"],
        "job_id": binding["job_id"],
        "deployed_sha": binding["deployed_sha"],
        "manifest_path": binding["manifest_path"],
        "manifest_sha256": binding["manifest_sha256"],
        "job_evidence": job,
        "archive_reconciliation": archives,
        "capacity_reconciliation": capacity,
        "finance_reconciliation": finance,
        "natural_root_monitor": monitor,
        "systemd_service_gate": services,
        "journald_reconciliation": journald,
        "non_target_reconciliation": non_target,
        "active_sanitation_job_count": 0,
        "held_lock_count": 0,
        "production_mutation_count": 0,
        "mutation_submit_count_observed": 1,
        "promo_action_count": 0,
        "business_data_mutation_count": 0,
        "remote_action_counts": {
            "readiness": 0,
            "submit": 0,
            "apply": 0,
            "job_creation": 0,
            "archive_worker": 0,
            "readback_batch": 0,
            "full_restore": 0,
            "decompression_to_file": 0,
            "temporary_file_creation": 0,
            "lock_acquisition": 0,
            "service_start_or_restart": 0,
            "timer_change": 0,
            "sql_or_file_write": 0,
            "unlink": 0,
        },
    }
    result["evidence_digest"] = payload_digest(result)
    return result


def main() -> int:
    if len(sys.argv) != 2 or re.fullmatch(r"[A-Za-z0-9_-]+", sys.argv[1]) is None:
        raise SystemExit("exact base64url probe binding is required")
    try:
        raw = base64.urlsafe_b64decode(sys.argv[1] + "=" * (-len(sys.argv[1]) % 4))
        config = json.loads(raw.decode("utf-8"))
        if not isinstance(config, dict):
            raise ProbeError("probe binding is not a JSON object")
        result = run_probe(config)
    except Exception as exc:  # terminal structured evidence; no retry or write
        result = {
            "schema": SCHEMA,
            "status": "blocked",
            "query_only": True,
            "production_mutation_count": 0,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc)[:1000],
            },
        }
        if isinstance(exc, SystemdGateError):
            result["systemd_service_gate"] = exc.gate
        result["evidence_digest"] = payload_digest(result)
    sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
