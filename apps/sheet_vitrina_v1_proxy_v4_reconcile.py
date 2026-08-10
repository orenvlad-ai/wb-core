"""Guarded bounded reconciliation of frozen Proxy V4 ready-snapshot cells."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_buyout_mature_backfill import (  # noqa: E402
    _backup_and_verify,
    _digest,
    _file_digest,
    _require_evidence_outside_repo,
    _validate_exact_deployment,
    _write_private_json,
)
from apps.sheet_vitrina_v1_proxy_v4_initialize import (  # noqa: E402
    SCHEMA_VERSION as INITIALIZATION_SCHEMA_VERSION,
    ProxyV4InitializationError,
    _load_exact_snapshots,
    _load_version_rows,
    _non_target_ready_snapshot_digest,
    _v3_parameter_digest,
)
from packages.application.proxy_v4_historical_projection import (  # noqa: E402
    proxy_v4_non_target_digest,
    proxy_v4_target_digest,
    proxy_v4_window_digest,
    reconcile_proxy_v4_target_window,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.business_time import current_business_date_iso  # noqa: E402


SCHEMA_VERSION = "sheet_vitrina_v1_proxy_v4_reconciliation_v1"
MAX_TARGET_DAYS = 31


class ProxyV4ReconciliationError(RuntimeError):
    """A guarded Proxy V4 reconciliation condition failed closed."""


def run_reconciliation(
    *,
    runtime_dir: Path,
    evidence_dir: Path,
    source_manifest_path: Path,
    expected_source_manifest_sha256: str,
    date_from: str,
    date_to: str,
    apply: bool,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_deployed_sha: str | None = None,
    deployed_sha_file: Path | None = None,
    approval_reference: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    evidence_dir = evidence_dir.expanduser().resolve()
    source_manifest_path = source_manifest_path.expanduser().resolve()
    _require_evidence_outside_repo(evidence_dir)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    if not runtime.db_path.is_file():
        raise ProxyV4ReconciliationError("canonical runtime SQLite DB is missing")
    effective_now = now or datetime.now(timezone.utc)
    if effective_now.tzinfo is None or effective_now.utcoffset() is None:
        raise ProxyV4ReconciliationError("now must be timezone-aware")
    business_date = current_business_date_iso(effective_now)
    normalized_from, normalized_to = _validate_window(
        date_from=date_from,
        date_to=date_to,
        business_date=business_date,
    )
    source_manifest, source_manifest_sha256 = _load_source_manifest(
        source_manifest_path,
        expected_sha256=expected_source_manifest_sha256,
    )
    created_at = effective_now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    if not apply:
        return _build_manifest(
            runtime=runtime,
            evidence_dir=evidence_dir,
            source_manifest=source_manifest,
            source_manifest_path=source_manifest_path,
            source_manifest_sha256=source_manifest_sha256,
            date_from=normalized_from,
            date_to=normalized_to,
            business_date=business_date,
            created_at=created_at,
        )
    if (
        manifest_path is None
        or not expected_manifest_sha256
        or not expected_deployed_sha
        or not approval_reference
    ):
        raise ProxyV4ReconciliationError(
            "--apply requires a reviewed manifest SHA, exact deployed SHA and human approval reference"
        )
    sha_file = (
        deployed_sha_file.expanduser().resolve()
        if deployed_sha_file is not None
        else runtime_dir.parent / "app" / ".wb-core-runtime-sha"
    )
    deployed_sha = _validate_exact_deployment(
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=sha_file,
    )
    try:
        with warehouse_sync_lock(runtime.runtime_dir, blocking=False):
            return _apply_manifest(
                runtime=runtime,
                evidence_dir=evidence_dir,
                manifest_path=manifest_path.expanduser().resolve(),
                expected_manifest_sha256=str(expected_manifest_sha256),
                source_manifest_sha256=source_manifest_sha256,
                deployed_sha=deployed_sha,
                deployed_sha_file=sha_file,
                approval_reference=str(approval_reference).strip(),
                business_date=business_date,
                applied_at=created_at,
            )
    except WarehouseSyncBusyError as exc:
        raise ProxyV4ReconciliationError(
            "canonical warehouse writer is busy; no Proxy V4 reconciliation was attempted"
        ) from exc


def _build_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    source_manifest: Mapping[str, Any],
    source_manifest_path: Path,
    source_manifest_sha256: str,
    date_from: str,
    date_to: str,
    business_date: str,
    created_at: str,
) -> dict[str, Any]:
    reference_snapshots = _reference_snapshots(
        source_manifest,
        date_from=date_from,
        date_to=date_to,
    )
    expected_dates = _date_range(date_from, date_to)
    actual_dates = [str(item["as_of_date"]) for item in reference_snapshots]
    if actual_dates != expected_dates:
        raise ProxyV4ReconciliationError(
            f"initialization reference must cover every exact target date: expected={expected_dates}, actual={actual_dates}"
        )
    target_keys = [
        (str(item["bundle_version"]), str(item["as_of_date"]))
        for item in reference_snapshots
    ]
    current_versions = _load_version_rows(runtime.db_path)
    _require_reference_versions(current_versions, source_manifest)
    current_snapshots = _load_required_snapshots(runtime.db_path, keys=target_keys)
    current_by_key = {
        (str(item["bundle_version"]), str(item["as_of_date"])): item
        for item in current_snapshots
    }
    desired_repairs: list[dict[str, Any]] = []
    for reference in reference_snapshots:
        key = (str(reference["bundle_version"]), str(reference["as_of_date"]))
        current = current_by_key[key]
        current_window = proxy_v4_window_digest(
            str(current["plan_json"]),
            date_from=date_from,
            date_to=date_to,
        )
        reference_window = proxy_v4_window_digest(
            str(reference["after_plan_json"]),
            date_from=date_from,
            date_to=date_to,
        )
        if current_window == reference_window:
            continue
        transformed = reconcile_proxy_v4_target_window(
            str(current["plan_json"]),
            reference_plan_json=str(reference["after_plan_json"]),
            date_from=date_from,
            date_to=date_to,
            reconciled_at=created_at,
        )
        transformed_window = proxy_v4_window_digest(
            str(transformed["after_plan_json"]),
            date_from=date_from,
            date_to=date_to,
        )
        if transformed_window != reference_window:
            raise ProxyV4ReconciliationError(
                f"V4 reconciliation could not reproduce the reviewed window for {key[1]}"
            )
        if transformed["non_target_before"] != transformed["non_target_after"]:
            raise ProxyV4ReconciliationError(
                f"V4 reconciliation changed non-V4 fields for {key[1]}"
            )
        desired_repairs.append(
            {
                "bundle_version": key[0],
                "as_of_date": key[1],
                "before_plan_sha256": _plan_digest(str(current["plan_json"])),
                "before_window_digest": current_window,
                "reference_window_digest": reference_window,
                "after_plan_json": transformed["after_plan_json"],
                "after_plan_sha256": _plan_digest(str(transformed["after_plan_json"])),
                "after_target_digest": transformed["target_after"],
                "changed_cells": int(transformed["changed_cells"]),
                "inserted_rows": int(transformed["inserted_rows"]),
                "dates": list(transformed["dates"]),
            }
        )
    if not desired_repairs:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "dry-run",
            "status": "already_reconciled",
            "database_written": False,
            "source_manifest_sha256": source_manifest_sha256,
            "date_from": date_from,
            "date_to": date_to,
            "affected_snapshot_count": 0,
            "idempotent_noop": True,
        }

    target_set = set(target_keys)
    pre_change = {
        "v4_version_rows_digest": _digest(current_versions),
        "v3_parameter_digest": _v3_parameter_digest(runtime.db_path),
        "target_snapshot_rows_digest": _snapshot_rows_digest(current_snapshots),
        "target_snapshot_non_v4_digest": _digest(
            [
                [item["bundle_version"], item["as_of_date"], proxy_v4_non_target_digest(str(item["plan_json"]))]
                for item in current_snapshots
            ]
        ),
        "non_target_ready_snapshot_digest": _non_target_ready_snapshot_digest(
            runtime.db_path,
            targets=target_set,
        ),
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready",
        "mode": "dry-run",
        "database_written": False,
        "created_at": created_at,
        "business_date": business_date,
        "source": {
            "contract": "reviewed_proxy_v4_initialization_manifest",
            "manifest_path": str(source_manifest_path),
            "manifest_sha256": source_manifest_sha256,
            "initialization_schema_version": INITIALIZATION_SCHEMA_VERSION,
        },
        "scope": {
            "date_from": date_from,
            "date_to": date_to,
            "target_snapshot_keys": [list(key) for key in target_keys],
            "expected_snapshot_count": len(expected_dates),
        },
        "pre_change": pre_change,
        "desired": {
            "repairs": desired_repairs,
            "repair_digest": _digest(
                [
                    [item["bundle_version"], item["as_of_date"], item["after_plan_sha256"]]
                    for item in desired_repairs
                ]
            ),
        },
        "expected_effect": {
            "affected_snapshot_count": len(desired_repairs),
            "changed_v4_cell_count": sum(int(item["changed_cells"]) for item in desired_repairs),
            "inserted_v4_row_count": sum(int(item["inserted_rows"]) for item in desired_repairs),
            "non_target_invariant": (
                "V3/V4 parameter versions, every non-V4 field and every non-target ready snapshot remain unchanged"
            ),
        },
        "idempotency": "exact repaired plan digest; repeated reviewed apply is already_applied",
        "recovery": "coherent verified SQLite backup before one exact-key transaction",
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / (
        f"proxy-v4-reconciliation-plan-{created_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(manifest_path, manifest)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "status": "ready",
        "database_written": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": _file_digest(manifest_path),
        "source_manifest_sha256": source_manifest_sha256,
        "date_from": date_from,
        "date_to": date_to,
        "target_snapshot_count": len(target_keys),
        "affected_snapshot_count": len(desired_repairs),
        "changed_v4_cell_count": manifest["expected_effect"]["changed_v4_cell_count"],
        "pre_change_target_snapshot_digest": pre_change["target_snapshot_rows_digest"],
        "desired_repair_digest": manifest["desired"]["repair_digest"],
    }


def _apply_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    source_manifest_sha256: str,
    deployed_sha: str,
    deployed_sha_file: Path,
    approval_reference: str,
    business_date: str,
    applied_at: str,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise ProxyV4ReconciliationError("reviewed V4 reconciliation manifest is missing")
    manifest_sha256 = _file_digest(manifest_path)
    if manifest_sha256 != expected_manifest_sha256:
        raise ProxyV4ReconciliationError("reviewed V4 reconciliation manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "ready":
        raise ProxyV4ReconciliationError("reviewed V4 reconciliation manifest is not ready")
    if str((manifest.get("source") or {}).get("manifest_sha256") or "") != source_manifest_sha256:
        raise ProxyV4ReconciliationError("initialization source manifest changed after dry-run")
    if not approval_reference or len(approval_reference) > 500:
        raise ProxyV4ReconciliationError("human approval reference is missing or invalid")
    if business_date < str(manifest.get("business_date") or ""):
        raise ProxyV4ReconciliationError("business date moved backwards after dry-run")
    _validate_exact_deployment(
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )
    scope = dict(manifest.get("scope") or {})
    target_keys = [
        (str(item[0]), str(item[1]))
        for item in (scope.get("target_snapshot_keys") or [])
    ]
    repairs = list((manifest.get("desired") or {}).get("repairs") or [])
    if not target_keys or not repairs:
        raise ProxyV4ReconciliationError("reviewed V4 reconciliation manifest has empty targets")
    current_versions = _load_version_rows(runtime.db_path)
    current_snapshots = _load_required_snapshots(runtime.db_path, keys=target_keys)
    current_by_key = {
        (str(item["bundle_version"]), str(item["as_of_date"])): item
        for item in current_snapshots
    }
    if _repairs_are_applied(current_by_key, repairs):
        _validate_idempotent_readback(
            runtime=runtime,
            manifest=manifest,
            current_versions=current_versions,
            current_snapshots=current_snapshots,
            target_keys=target_keys,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "status": "already_applied",
            "database_written": False,
            "manifest_sha256": manifest_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "idempotent_noop": True,
            "non_target_preserved": True,
        }
    pre = dict(manifest.get("pre_change") or {})
    target_set = set(target_keys)
    if _digest(current_versions) != str(pre.get("v4_version_rows_digest") or ""):
        raise ProxyV4ReconciliationError("V4 parameter versions changed after dry-run")
    if _v3_parameter_digest(runtime.db_path) != str(pre.get("v3_parameter_digest") or ""):
        raise ProxyV4ReconciliationError("V3 parameters changed after dry-run")
    if _snapshot_rows_digest(current_snapshots) != str(pre.get("target_snapshot_rows_digest") or ""):
        raise ProxyV4ReconciliationError("target ready snapshots changed after dry-run")
    if _non_target_ready_snapshot_digest(runtime.db_path, targets=target_set) != str(
        pre.get("non_target_ready_snapshot_digest") or ""
    ):
        raise ProxyV4ReconciliationError("non-target ready snapshots changed after dry-run")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    backup_path = evidence_dir / "backups" / (
        f"proxy-v4-reconciliation-{applied_at.replace(':', '').replace('-', '')}-{manifest_sha256[-12:]}.sqlite3"
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_sha256 = _backup_and_verify(runtime.db_path, backup_path)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for item in repairs:
            key = (str(item["bundle_version"]), str(item["as_of_date"]))
            before = str(current_by_key[key]["plan_json"])
            cursor = conn.execute(
                """UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?
                   WHERE bundle_version=? AND as_of_date=? AND plan_json=?""",
                (str(item["after_plan_json"]), key[0], key[1], before),
            )
            if cursor.rowcount != 1:
                raise ProxyV4ReconciliationError(
                    f"target snapshot compare-and-swap failed: {key[1]}"
                )
        conn.commit()

    after_snapshots = _load_required_snapshots(runtime.db_path, keys=target_keys)
    after_by_key = {
        (str(item["bundle_version"]), str(item["as_of_date"])): item
        for item in after_snapshots
    }
    if not _repairs_are_applied(after_by_key, repairs):
        raise ProxyV4ReconciliationError("post-apply V4 reconciliation readback mismatch")
    if _digest(_load_version_rows(runtime.db_path)) != str(pre["v4_version_rows_digest"]):
        raise ProxyV4ReconciliationError("post-apply V4 parameter version invariant failed")
    if _v3_parameter_digest(runtime.db_path) != str(pre["v3_parameter_digest"]):
        raise ProxyV4ReconciliationError("post-apply V3 invariant failed")
    if _non_target_ready_snapshot_digest(runtime.db_path, targets=target_set) != str(
        pre["non_target_ready_snapshot_digest"]
    ):
        raise ProxyV4ReconciliationError("post-apply non-target ready snapshot invariant failed")
    post_non_v4 = _digest(
        [
            [item["bundle_version"], item["as_of_date"], proxy_v4_non_target_digest(str(item["plan_json"]))]
            for item in after_snapshots
        ]
    )
    if post_non_v4 != str(pre["target_snapshot_non_v4_digest"]):
        raise ProxyV4ReconciliationError("post-apply target non-V4 invariant failed")

    reconciliation = {
        "schema_version": SCHEMA_VERSION,
        "status": "reconciled",
        "applied_at": applied_at,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "deployed_sha": deployed_sha,
        "deployed_sha_file": str(deployed_sha_file),
        "approval_reference": approval_reference,
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha256,
        "affected_snapshot_count": len(repairs),
        "affected_snapshot_dates": [str(item["as_of_date"]) for item in repairs],
        "post_repair_digest": _digest(
            [
                [item["bundle_version"], item["as_of_date"], item["after_plan_sha256"]]
                for item in repairs
            ]
        ),
        "v3_parameter_digest": str(pre["v3_parameter_digest"]),
        "v4_version_rows_digest": str(pre["v4_version_rows_digest"]),
        "non_target_ready_snapshot_digest": str(pre["non_target_ready_snapshot_digest"]),
        "non_target_preserved": True,
        "idempotent_noop": False,
    }
    reconciliation_path = evidence_dir / (
        f"proxy-v4-reconciliation-{applied_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(reconciliation_path, reconciliation)
    reconciliation_sha256 = _file_digest(reconciliation_path)
    evidence_sha256 = _digest(
        {
            "manifest_sha256": manifest_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "backup_sha256": backup_sha256,
            "reconciliation_sha256": reconciliation_sha256,
        }
    )
    return {
        **reconciliation,
        "mode": "apply",
        "database_written": True,
        "reconciliation_path": str(reconciliation_path),
        "reconciliation_sha256": reconciliation_sha256,
        "evidence_sha256": evidence_sha256,
    }


def _validate_window(*, date_from: str, date_to: str, business_date: str) -> tuple[str, str]:
    raw_from = str(date_from).strip()
    raw_to = str(date_to).strip()
    if len(raw_from) != 10 or len(raw_to) != 10:
        raise ProxyV4ReconciliationError("date window must use exact YYYY-MM-DD values")
    try:
        start = date.fromisoformat(raw_from)
        end = date.fromisoformat(raw_to)
    except ValueError as exc:
        raise ProxyV4ReconciliationError("date window must use YYYY-MM-DD") from exc
    if end < start:
        raise ProxyV4ReconciliationError("date_to precedes date_from")
    if (end - start).days + 1 > MAX_TARGET_DAYS:
        raise ProxyV4ReconciliationError("V4 reconciliation window exceeds 31 days")
    if end >= date.fromisoformat(business_date):
        raise ProxyV4ReconciliationError("V4 reconciliation may target only closed past dates")
    return start.isoformat(), end.isoformat()


def _load_source_manifest(path: Path, *, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if not path.is_file():
        raise ProxyV4ReconciliationError("reviewed initialization source manifest is missing")
    actual = _file_digest(path)
    if actual != str(expected_sha256):
        raise ProxyV4ReconciliationError("initialization source manifest SHA-256 mismatch")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != INITIALIZATION_SCHEMA_VERSION or payload.get("status") != "ready":
        raise ProxyV4ReconciliationError("initialization source manifest is not ready")
    return payload, actual


def _reference_snapshots(
    manifest: Mapping[str, Any],
    *,
    date_from: str,
    date_to: str,
) -> list[dict[str, Any]]:
    rows = [
        dict(item)
        for item in ((manifest.get("desired") or {}).get("ready_snapshots") or [])
        if date_from <= str(item.get("as_of_date") or "") <= date_to
    ]
    rows.sort(key=lambda item: (str(item.get("as_of_date") or ""), str(item.get("bundle_version") or "")))
    return rows


def _require_reference_versions(
    current_versions: list[Mapping[str, Any]],
    source_manifest: Mapping[str, Any],
) -> None:
    current_by_id = {str(item.get("version_id") or ""): item for item in current_versions}
    references = list((source_manifest.get("desired") or {}).get("version_rows") or [])
    if not references:
        raise ProxyV4ReconciliationError("initialization source has no V4 parameter versions")
    for reference in references:
        current = current_by_id.get(str(reference.get("version_id") or ""))
        if current is None or _digest(current) != _digest(reference):
            raise ProxyV4ReconciliationError(
                f"initial V4 parameter version drifted: {reference.get('version_id')}"
            )


def _date_range(date_from: str, date_to: str) -> list[str]:
    start = date.fromisoformat(date_from)
    end = date.fromisoformat(date_to)
    return [
        date.fromordinal(day).isoformat()
        for day in range(start.toordinal(), end.toordinal() + 1)
    ]


def _snapshot_rows_digest(rows: list[Mapping[str, Any]]) -> str:
    return _digest(
        [
            [
                str(item["bundle_version"]),
                str(item["as_of_date"]),
                json.loads(str(item["plan_json"])),
            ]
            for item in rows
        ]
    )


def _load_required_snapshots(
    db_path: Path,
    *,
    keys: list[tuple[str, str]],
) -> list[dict[str, Any]]:
    try:
        return _load_exact_snapshots(db_path, keys=keys)
    except ProxyV4InitializationError as exc:
        raise ProxyV4ReconciliationError(str(exc)) from exc


def _validate_idempotent_readback(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    manifest: Mapping[str, Any],
    current_versions: list[Mapping[str, Any]],
    current_snapshots: list[Mapping[str, Any]],
    target_keys: list[tuple[str, str]],
) -> None:
    pre = dict(manifest.get("pre_change") or {})
    if _digest(current_versions) != str(pre.get("v4_version_rows_digest") or ""):
        raise ProxyV4ReconciliationError(
            "idempotent readback found V4 parameter-version drift"
        )
    if _v3_parameter_digest(runtime.db_path) != str(
        pre.get("v3_parameter_digest") or ""
    ):
        raise ProxyV4ReconciliationError("idempotent readback found V3 parameter drift")
    current_non_v4 = _digest(
        [
            [
                item["bundle_version"],
                item["as_of_date"],
                proxy_v4_non_target_digest(str(item["plan_json"])),
            ]
            for item in current_snapshots
        ]
    )
    if current_non_v4 != str(pre.get("target_snapshot_non_v4_digest") or ""):
        raise ProxyV4ReconciliationError(
            "idempotent readback found target non-V4 drift"
        )
    if _non_target_ready_snapshot_digest(
        runtime.db_path,
        targets=set(target_keys),
    ) != str(pre.get("non_target_ready_snapshot_digest") or ""):
        raise ProxyV4ReconciliationError(
            "idempotent readback found non-target ready-snapshot drift"
        )


def _plan_digest(plan_json: str) -> str:
    return _digest(json.loads(str(plan_json)))


def _repairs_are_applied(
    current_by_key: Mapping[tuple[str, str], Mapping[str, Any]],
    repairs: list[Mapping[str, Any]],
) -> bool:
    return all(
        key in current_by_key
        and _plan_digest(str(current_by_key[key]["plan_json"]))
        == str(item.get("after_plan_sha256") or "")
        for item in repairs
        for key in [(str(item.get("bundle_version") or ""), str(item.get("as_of_date") or ""))]
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--expected-source-manifest-sha256", required=True)
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-deployed-sha")
    parser.add_argument("--deployed-sha-file")
    parser.add_argument("--approval-reference")
    args = parser.parse_args()
    if not args.apply:
        args.dry_run = True
    return args


def main() -> None:
    args = _parse_args()
    try:
        result = run_reconciliation(
            runtime_dir=Path(args.runtime_dir),
            evidence_dir=Path(args.evidence_dir),
            source_manifest_path=Path(args.source_manifest),
            expected_source_manifest_sha256=args.expected_source_manifest_sha256,
            date_from=args.date_from,
            date_to=args.date_to,
            apply=bool(args.apply),
            manifest_path=Path(args.manifest) if args.manifest else None,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_deployed_sha=args.expected_deployed_sha,
            deployed_sha_file=Path(args.deployed_sha_file) if args.deployed_sha_file else None,
            approval_reference=args.approval_reference,
        )
    except (ProxyV4ReconciliationError, ValueError) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply" if args.apply else "dry-run",
            "status": "blocked",
            "database_written": False,
            "blocker": str(exc),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("status") == "blocked":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
