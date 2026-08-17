#!/usr/bin/env python3
"""Guarded append-only retirement of the WB incident policy.

The default mode is a query-only dry-run.  Apply accepts only an exact reviewed
manifest, verifies the deployed SHA and immutable owner gate, creates a coherent
SQLite backup, appends one inactive revision effective 2026-08-16, and proves
that stock history, incident evidence, projection cache and rematerialization
audit did not change.  It never invokes incident rematerialization.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402


PLAN_CONTRACT = "wb_incident_policy_legacy_disable_plan_v1"
RESULT_CONTRACT = "wb_incident_policy_legacy_disable_result_v1"
EFFECTIVE_FROM = "2026-08-16"
POLICY_TABLE = "sheet_vitrina_v1_wb_incident_policy_revisions"
POLICY_SOURCE = "incident_policy_legacy_disable_v1"
POLICY_REASON = "Legacy mode: incident policy disabled effective 2026-08-16"
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
INVARIANT_TABLES = (
    "sheet_vitrina_v1_ready_snapshots",
    "sheet_vitrina_v1_wb_incident_projection_cache",
    "sheet_vitrina_v1_incident_rematerialization_audit",
    "sheet_vitrina_v1_wb_incident_quantity_evidence",
    "sheet_vitrina_v1_wb_incident_quantity_evidence_lines",
    "sheet_vitrina_v1_warehouse_wb_snapshots",
)
POLICY_COLUMNS = (
    "seller_id",
    "revision",
    "active",
    "warehouse_ids_json",
    "warehouse_identities_json",
    "warehouse_entries_json",
    "reason",
    "effective_from",
    "effective_to",
    "policy_status",
    "actor",
    "created_at",
    "source",
    "legacy_payloads_json",
)


class LegacyDisableError(ValueError):
    """The bounded policy-retirement gate is invalid or has drifted."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise LegacyDisableError("now must be timezone-aware")
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


def _table_exists(conn: Any, table_name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone() is not None


def _table_contract(conn: Any, table_name: str) -> dict[str, Any]:
    """Stream one exact logical table digest with constant memory."""

    if not _table_exists(conn, table_name):
        return {"exists": False, "row_count": 0, "digest": _digest([])}
    table_info = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    columns = [str(row[1]) for row in table_info]
    primary = [
        str(row[1])
        for row in sorted(table_info, key=lambda item: int(item[5] or 0))
        if int(row[5] or 0) > 0
    ]
    order_columns = primary or columns
    selected = ",".join(f'"{column}"' for column in columns)
    order = ",".join(f'"{column}"' for column in order_columns)
    digest = hashlib.sha256()
    row_count = 0
    for row in conn.execute(f'SELECT {selected} FROM "{table_name}" ORDER BY {order}'):
        encoded = _canonical_json([_jsonable(row[column]) for column in columns]).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        row_count += 1
    return {
        "exists": True,
        "row_count": row_count,
        "digest": "sha256:" + digest.hexdigest(),
    }


def _policy_row(conn: Any, *, seller_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {','.join(POLICY_COLUMNS)} FROM {POLICY_TABLE} "
        "WHERE seller_id=? ORDER BY revision DESC LIMIT 1",
        (seller_id,),
    ).fetchone()
    return {column: row[column] for column in POLICY_COLUMNS} if row is not None else None


def _policy_history_contract(
    conn: Any,
    *,
    seller_id: str,
    before_revision: int | None = None,
) -> dict[str, Any]:
    where = "WHERE seller_id=?"
    parameters: list[Any] = [seller_id]
    if before_revision is not None:
        where += " AND revision<?"
        parameters.append(int(before_revision))
    rows = conn.execute(
        f"SELECT {','.join(POLICY_COLUMNS)} FROM {POLICY_TABLE} {where} ORDER BY revision",
        tuple(parameters),
    )
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        encoded = _canonical_json([_jsonable(row[column]) for column in POLICY_COLUMNS]).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return {"row_count": count, "digest": "sha256:" + digest.hexdigest()}


def _policy_row_digest(row: Mapping[str, Any]) -> str:
    return _digest({column: _jsonable(row.get(column)) for column in POLICY_COLUMNS})


def _validate_policy_schema(conn: Any) -> None:
    if not _table_exists(conn, POLICY_TABLE):
        raise LegacyDisableError(f"required policy table is missing: {POLICY_TABLE}")
    actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({POLICY_TABLE})")}
    missing = sorted(set(POLICY_COLUMNS) - actual)
    if missing:
        raise LegacyDisableError(f"policy table schema drifted; missing columns: {missing}")


def _deployed_runtime_evidence(*, expected_sha: str, deployed_root: Path) -> dict[str, Any]:
    expected = str(expected_sha or "").strip().lower()
    if not SHA_RE.fullmatch(expected):
        raise LegacyDisableError("expected deployed SHA must be exactly 40 lowercase hex characters")
    runtime_sha_path = deployed_root / ".wb-core-runtime-sha"
    metadata_path = deployed_root / ".wb-core-deploy.json"
    if not runtime_sha_path.is_file() or not metadata_path.is_file():
        raise LegacyDisableError("canonical deployed-SHA evidence is missing")
    runtime_sha = runtime_sha_path.read_text(encoding="utf-8").strip().lower()
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata_sha = str(metadata.get("commit") or "").strip().lower()
    if runtime_sha != expected or metadata_sha != expected or metadata.get("deployment_complete") is not True:
        raise LegacyDisableError("canonical deployed-SHA evidence does not match the expected release")
    return {
        "runtime_sha": runtime_sha,
        "deploy_metadata_sha": metadata_sha,
        "deployment_complete": True,
        "deployed_at": str(metadata.get("deployed_at") or ""),
    }


def _is_exact_disable_row(row: Mapping[str, Any] | None) -> bool:
    return bool(
        row
        and not bool(row.get("active"))
        and str(row.get("effective_from") or "") == EFFECTIVE_FROM
        and str(row.get("effective_to") or "") == ""
        and str(row.get("policy_status") or "") == "disabled"
        and str(row.get("source") or "") == POLICY_SOURCE
        and str(row.get("reason") or "") == POLICY_REASON
    )


def _target_projection(source: Mapping[str, Any], *, actor: str, created_at: str) -> dict[str, Any]:
    return {
        "seller_id": str(source["seller_id"]),
        "revision": int(source["revision"]) + 1,
        "active": 0,
        "warehouse_ids_json": str(source["warehouse_ids_json"]),
        "warehouse_identities_json": str(source["warehouse_identities_json"]),
        "warehouse_entries_json": str(source["warehouse_entries_json"]),
        "reason": POLICY_REASON,
        "effective_from": EFFECTIVE_FROM,
        "effective_to": "",
        "policy_status": "disabled",
        "actor": str(actor).strip(),
        "created_at": created_at,
        "source": POLICY_SOURCE,
        "legacy_payloads_json": str(source["legacy_payloads_json"]),
    }


def build_plan(
    *,
    runtime_dir: Path,
    seller_id: str,
    actor: str,
    expected_deployed_sha: str,
    deployed_root: Path = ROOT,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the dry-run manifest through the canonical query-only store path."""

    owner = str(seller_id or "").strip()
    normalized_actor = str(actor or "").strip()
    if not owner or not normalized_actor:
        raise LegacyDisableError("seller_id and actor are required")
    deployed = _deployed_runtime_evidence(
        expected_sha=expected_deployed_sha,
        deployed_root=Path(deployed_root),
    )
    registry = StoreRegistry(Path(runtime_dir))
    generation = registry.load(require_files=True)
    with registry.connect(
        "operational",
        mode="ro",
        operation="wb_incident_policy_legacy_disable_dry_run",
        manifest=generation,
    ) as conn:
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise LegacyDisableError("dry-run connection is not query-only")
        _validate_policy_schema(conn)
        latest = _policy_row(conn, seller_id=owner)
        if latest is None:
            raise LegacyDisableError("seller has no incident policy history to retire")
        if _is_exact_disable_row(latest):
            return {
                "contract_name": PLAN_CONTRACT,
                "status": "already_applied",
                "mode": "query_only_dry_run",
                "seller_id": owner,
                "effective_from": EFFECTIVE_FROM,
                "deployed": deployed,
                "current_revision": int(latest["revision"]),
                "current_row_digest": _policy_row_digest(latest),
                "expected_affected_records": 0,
                "idempotent_noop": True,
            }
        latest_effective_from = str(latest.get("effective_from") or "")
        if latest_effective_from >= EFFECTIVE_FROM:
            raise LegacyDisableError(
                "a non-target policy revision already starts on/after 2026-08-16; owner decision required"
            )
        invariants = {
            table: _table_contract(conn, table)
            for table in INVARIANT_TABLES
        }
        history = _policy_history_contract(conn, seller_id=owner)
        target = _target_projection(
            latest,
            actor=normalized_actor,
            created_at=_timestamp(now),
        )
        basis = {
            "contract_name": PLAN_CONTRACT,
            "status": "ready",
            "mode": "query_only_dry_run",
            "seller_id": owner,
            "effective_from": EFFECTIVE_FROM,
            "deployed": deployed,
            "storage_generation": {
                "state": generation.state,
                "canonical_source": generation.canonical_source,
                "generation_id": generation.operational.generation_id,
                "generation_epoch": generation.operational.generation_epoch,
                "schema_revision": generation.operational.schema_revision,
                "manifest_sha256": generation.manifest_sha256,
            },
            "pre_change": {
                "source_revision": int(latest["revision"]),
                "source_row_digest": _policy_row_digest(latest),
                "policy_history": history,
                "non_target_tables": invariants,
            },
            "target_revision": {
                key: _jsonable(value) for key, value in target.items()
            },
            "expected_affected_records": 1,
            "mutation_contract": {
                "only_table": POLICY_TABLE,
                "operation": "one append-only INSERT",
                "incident_rematerialization": False,
                "ready_snapshot_rewrite": False,
                "incident_quantity_evidence_write": False,
            },
            "backup_and_recovery": {
                "backup_kind": "coherent_sqlite_backup_with_integrity_check_and_sha256",
                "backup_created_only_during_apply": True,
                "recovery": (
                    "Fail closed and retain backup/evidence. Do not DELETE or UPDATE revisions. "
                    "After a separately reviewed owner gate, recover forward with a new append-only revision; "
                    "restore the coherent backup only while all writers are quiesced and no later valid writes exist."
                ),
            },
        }
    return {**basis, "manifest_fingerprint": _digest(basis), "idempotent_noop": False}


def _validate_manifest(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("contract_name") != PLAN_CONTRACT:
        raise LegacyDisableError(f"exact {PLAN_CONTRACT} is required")
    if payload.get("status") != "ready" or payload.get("mode") != "query_only_dry_run":
        raise LegacyDisableError("reviewed manifest is not ready")
    supplied = str(payload.get("manifest_fingerprint") or "")
    basis = {key: value for key, value in payload.items() if key not in {"manifest_fingerprint", "idempotent_noop"}}
    if supplied != _digest(basis):
        raise LegacyDisableError("reviewed manifest fingerprint mismatch")
    if str(payload.get("effective_from") or "") != EFFECTIVE_FROM:
        raise LegacyDisableError("reviewed manifest has the wrong effective date")
    return payload


def _load_reviewed_manifest(path: Path, *, expected_file_digest: str) -> dict[str, Any]:
    if not DIGEST_RE.fullmatch(str(expected_file_digest or "").strip().lower()):
        raise LegacyDisableError("exact sha256 reviewed-manifest digest is required")
    if not path.is_file() or _file_digest(path) != str(expected_file_digest).strip().lower():
        raise LegacyDisableError("reviewed-manifest file digest mismatch")
    return _validate_manifest(json.loads(path.read_text(encoding="utf-8")))


def _require_evidence_outside_repo(evidence_dir: Path) -> None:
    try:
        evidence_dir.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return
    raise LegacyDisableError("production evidence directory must be outside Git")


def _current_contracts(
    *,
    registry: StoreRegistry,
    generation: Any,
    seller_id: str,
    operation: str,
) -> dict[str, Any]:
    with registry.connect(
        "operational",
        mode="ro",
        operation=operation,
        manifest=generation,
    ) as conn:
        _validate_policy_schema(conn)
        latest = _policy_row(conn, seller_id=seller_id)
        return {
            "latest": latest,
            "history": _policy_history_contract(conn, seller_id=seller_id),
            "invariants": {
                table: _table_contract(conn, table)
                for table in INVARIANT_TABLES
            },
        }


def apply_reviewed_plan(
    *,
    runtime_dir: Path,
    manifest_path: Path,
    manifest_file_digest: str,
    expected_deployed_sha: str,
    approval_reference: str,
    approval_digest: str,
    evidence_dir: Path,
    deployed_root: Path = ROOT,
) -> dict[str, Any]:
    """Append the exact reviewed revision and reconcile every protected table."""

    if not str(approval_reference or "").strip():
        raise LegacyDisableError("immutable owner apply-gate reference is required")
    if not DIGEST_RE.fullmatch(str(approval_digest or "").strip().lower()):
        raise LegacyDisableError("exact sha256 owner apply-gate digest is required")
    reviewed = _load_reviewed_manifest(
        Path(manifest_path),
        expected_file_digest=manifest_file_digest,
    )
    deployed = _deployed_runtime_evidence(
        expected_sha=expected_deployed_sha,
        deployed_root=Path(deployed_root),
    )
    if deployed != reviewed.get("deployed"):
        raise LegacyDisableError("deployed evidence drifted from the reviewed manifest")
    evidence_root = Path(evidence_dir).expanduser().resolve()
    _require_evidence_outside_repo(evidence_root)
    registry = StoreRegistry(Path(runtime_dir))
    generation = registry.load(require_files=True)
    storage = dict(reviewed.get("storage_generation") or {})
    if (
        generation.operational.generation_id != storage.get("generation_id")
        or generation.operational.generation_epoch != storage.get("generation_epoch")
        or generation.manifest_sha256 != storage.get("manifest_sha256")
    ):
        raise LegacyDisableError("operational storage generation drifted from the reviewed manifest")
    seller_id = str(reviewed["seller_id"])
    before = _current_contracts(
        registry=registry,
        generation=generation,
        seller_id=seller_id,
        operation="wb_incident_policy_legacy_disable_apply_preflight",
    )
    latest = before["latest"]
    target = dict(reviewed["target_revision"])
    if _is_exact_disable_row(latest):
        if (
            int(latest["revision"]) != int(target["revision"])
            or any(latest[column] != target[column] for column in POLICY_COLUMNS)
        ):
            raise LegacyDisableError("an unrelated later disable revision replaced the reviewed target")
        return {
            "contract_name": RESULT_CONTRACT,
            "status": "already_applied",
            "database_written": False,
            "idempotent_noop": True,
            "manifest_fingerprint": reviewed["manifest_fingerprint"],
            "deployed_sha": expected_deployed_sha,
            "current_revision": int(latest["revision"]),
        }
    pre_change = dict(reviewed["pre_change"])
    if (
        latest is None
        or int(latest["revision"]) != int(pre_change["source_revision"])
        or _policy_row_digest(latest) != pre_change["source_row_digest"]
        or before["history"] != pre_change["policy_history"]
        or before["invariants"] != pre_change["non_target_tables"]
    ):
        raise LegacyDisableError("production state drifted; create and review a fresh dry-run manifest")

    evidence_root.mkdir(parents=True, exist_ok=True)
    backup_dir = evidence_root / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        "wb-incident-policy-legacy-disable-"
        + str(reviewed["manifest_fingerprint"]).split(":", 1)[1]
        + ".sqlite3"
    )
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(runtime_dir))
    backup = runtime.backup_database(backup_path)

    with registry.connect(
        "operational",
        mode="rw",
        operation="wb_incident_policy_legacy_disable_apply",
        manifest=generation,
        isolation_level=None,
    ) as conn:
        conn.execute("BEGIN IMMEDIATE")
        locked_latest = _policy_row(conn, seller_id=seller_id)
        if (
            locked_latest is None
            or int(locked_latest["revision"]) != int(pre_change["source_revision"])
            or _policy_row_digest(locked_latest) != pre_change["source_row_digest"]
        ):
            conn.rollback()
            raise LegacyDisableError("policy revision changed while acquiring the write lock")
        conn.execute(
            f"INSERT INTO {POLICY_TABLE}({','.join(POLICY_COLUMNS)}) "
            f"VALUES({','.join('?' for _ in POLICY_COLUMNS)})",
            tuple(target[column] for column in POLICY_COLUMNS),
        )
        conn.commit()

    after = _current_contracts(
        registry=registry,
        generation=generation,
        seller_id=seller_id,
        operation="wb_incident_policy_legacy_disable_reconciliation",
    )
    post_latest = after["latest"]
    with registry.connect(
        "operational",
        mode="ro",
        operation="wb_incident_policy_legacy_disable_history_readback",
        manifest=generation,
    ) as conn:
        prior_history = _policy_history_contract(
            conn,
            seller_id=seller_id,
            before_revision=int(target["revision"]),
        )
    failures: list[str] = []
    if post_latest is None or any(post_latest[column] != target[column] for column in POLICY_COLUMNS):
        failures.append("target revision readback mismatch")
    if prior_history != pre_change["policy_history"]:
        failures.append("pre-existing append-only policy history changed")
    if after["invariants"] != pre_change["non_target_tables"]:
        failures.append("protected stock/history/evidence tables changed")
    status = "reconciled" if not failures else "reconciliation_failed"
    result = {
        "contract_name": RESULT_CONTRACT,
        "status": status,
        "database_written": True,
        "idempotent_noop": False,
        "manifest_fingerprint": reviewed["manifest_fingerprint"],
        "reviewed_manifest_sha256": manifest_file_digest,
        "deployed_sha": expected_deployed_sha,
        "approval_reference": str(approval_reference).strip(),
        "approval_digest": str(approval_digest).strip().lower(),
        "backup": backup,
        "appended_revision": int(target["revision"]),
        "effective_from": EFFECTIVE_FROM,
        "policy_active": bool(post_latest and post_latest["active"]),
        "policy_status": str((post_latest or {}).get("policy_status") or ""),
        "prior_history_preserved": prior_history == pre_change["policy_history"],
        "non_target_invariants_preserved": after["invariants"] == pre_change["non_target_tables"],
        "incident_rematerialization_invoked": False,
        "failures": failures,
        "recovery_instruction": reviewed["backup_and_recovery"]["recovery"],
    }
    result["evidence_fingerprint"] = _digest(result)
    reconciliation_path = evidence_root / (
        "wb-incident-policy-legacy-disable-reconciliation-"
        + str(target["created_at"]).replace(":", "").replace("-", "")
        + ".json"
    )
    reconciliation_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(reconciliation_path, 0o600)
    result["reconciliation_path"] = str(reconciliation_path)
    result["reconciliation_sha256"] = _file_digest(reconciliation_path)
    if failures:
        raise LegacyDisableError("; ".join(failures))
    return result


def readback(
    *,
    runtime_dir: Path,
    seller_id: str,
    expected_deployed_sha: str,
    deployed_root: Path = ROOT,
) -> dict[str, Any]:
    """Perform a standalone query-only canonical readback of the terminal state."""

    deployed = _deployed_runtime_evidence(
        expected_sha=expected_deployed_sha,
        deployed_root=Path(deployed_root),
    )
    registry = StoreRegistry(Path(runtime_dir))
    generation = registry.load(require_files=True)
    current = _current_contracts(
        registry=registry,
        generation=generation,
        seller_id=str(seller_id).strip(),
        operation="wb_incident_policy_legacy_disable_standalone_readback",
    )
    latest = current["latest"]
    if not _is_exact_disable_row(latest):
        raise LegacyDisableError("legacy-disable terminal revision is not the current policy state")
    result = {
        "contract_name": RESULT_CONTRACT,
        "status": "readback_ok",
        "mode": "query_only",
        "deployed": deployed,
        "seller_id": str(seller_id).strip(),
        "revision": int(latest["revision"]),
        "active": bool(latest["active"]),
        "policy_status": str(latest["policy_status"]),
        "effective_from": str(latest["effective_from"]),
        "source": str(latest["source"]),
        "history": current["history"],
        "protected_tables": current["invariants"],
        "incident_rematerialization_invoked": False,
    }
    result["evidence_fingerprint"] = _digest(result)
    return result


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--seller-id", default="canonical")
    parser.add_argument("--actor", default="")
    parser.add_argument("--expected-deployed-sha", required=True)
    parser.add_argument("--deployed-root", default=str(ROOT))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--readback", action="store_true")
    parser.add_argument("--reviewed-manifest", default="")
    parser.add_argument("--reviewed-manifest-sha256", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--approval-digest", default="")
    parser.add_argument("--evidence-dir", default="")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.apply and args.readback:
        raise LegacyDisableError("choose only one of --apply or --readback")
    if args.apply:
        required = (args.reviewed_manifest, args.reviewed_manifest_sha256, args.evidence_dir)
        if not all(str(item or "").strip() for item in required):
            raise LegacyDisableError(
                "apply requires reviewed manifest, its sha256 and an outside-Git evidence directory"
            )
        payload = apply_reviewed_plan(
            runtime_dir=Path(args.runtime_dir),
            manifest_path=Path(args.reviewed_manifest),
            manifest_file_digest=args.reviewed_manifest_sha256,
            expected_deployed_sha=args.expected_deployed_sha,
            approval_reference=args.approval_reference,
            approval_digest=args.approval_digest,
            evidence_dir=Path(args.evidence_dir),
            deployed_root=Path(args.deployed_root),
        )
    elif args.readback:
        payload = readback(
            runtime_dir=Path(args.runtime_dir),
            seller_id=args.seller_id,
            expected_deployed_sha=args.expected_deployed_sha,
            deployed_root=Path(args.deployed_root),
        )
    else:
        forbidden = (
            args.reviewed_manifest,
            args.reviewed_manifest_sha256,
            args.approval_reference,
            args.approval_digest,
            args.evidence_dir,
        )
        if any(str(item or "").strip() for item in forbidden):
            raise LegacyDisableError("dry-run does not accept apply-only arguments")
        payload = build_plan(
            runtime_dir=Path(args.runtime_dir),
            seller_id=args.seller_id,
            actor=args.actor,
            expected_deployed_sha=args.expected_deployed_sha,
            deployed_root=Path(args.deployed_root),
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
