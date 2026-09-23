"""Guarded one-off CSV confirmation overlay for September 2026 buyout recovery."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.sales_funnel_history_block import (  # noqa: E402
    DetailHistoryCsvBackedSalesFunnelHistorySource,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.sales_funnel_history_block import (  # noqa: E402
    SalesFunnelHistoryBlock,
)
from packages.application.warehouse_sync_lock import (  # noqa: E402
    WarehouseSyncBusyError,
    warehouse_sync_lock,
)
from packages.application.sheet_vitrina_v1_buyout_percent import (  # noqa: E402
    BUYOUT_PERCENT_MATURITY_DAYS,
    BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
    SALES_FUNNEL_HISTORY_SOURCE_KEY,
    buyout_snapshot_has_enabled_sku_coverage,
    mature_buyout_capture_proof,
    split_sales_funnel_success_payload_by_date,
    trusted_buyout_cutoff,
)
from packages.business_time import current_business_date_iso  # noqa: E402
from packages.contracts.sales_funnel_history_block import (  # noqa: E402
    SalesFunnelHistoryItem,
    SalesFunnelHistoryRequest,
    SalesFunnelHistorySuccess,
)


SCHEMA_VERSION = "sheet_vitrina_v1_buyout_confirmation_recovery_v1"
RECOVERY_DATE_FROM = "2026-09-03"
RECOVERY_DATE_TO = "2026-09-17"
MAX_WINDOW_DAYS = 31


class BuyoutConfirmationRecoveryError(RuntimeError):
    """A guarded plan/apply condition failed closed."""


def run_recovery(
    *,
    runtime_dir: Path,
    evidence_dir: Path,
    date_from: str,
    date_to: str,
    apply: bool,
    manifest_path: Path | None = None,
    expected_manifest_sha256: str | None = None,
    expected_deployed_sha: str | None = None,
    deployed_sha_file: Path | None = None,
    deployment_receipt_file: Path | None = None,
    approval_reference: str | None = None,
    history_block: SalesFunnelHistoryBlock | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    evidence_dir = evidence_dir.expanduser().resolve()
    _validate_scope(date_from=date_from, date_to=date_to)
    _require_evidence_outside_repo(evidence_dir)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    effective_now = now or datetime.now(timezone.utc)
    business_date = datetime.strptime(
        current_business_date_iso(effective_now), "%Y-%m-%d"
    ).date()
    cutoff = trusted_buyout_cutoff(business_date)
    if date_to > cutoff.isoformat():
        raise BuyoutConfirmationRecoveryError(
            f"requested date_to={date_to} exceeds trusted cutoff={cutoff.isoformat()}"
        )

    if not apply:
        source_evidence_getter: Callable[[], Mapping[str, Any]] | None = None
        if history_block is None:
            detail_history_source = DetailHistoryCsvBackedSalesFunnelHistorySource()
            history_block = SalesFunnelHistoryBlock(detail_history_source)
            source_evidence_getter = lambda: detail_history_source.last_fetch_evidence
        return _build_manifest(
            runtime=runtime,
            evidence_dir=evidence_dir,
            date_from=date_from,
            date_to=date_to,
            business_date=business_date.isoformat(),
            trusted_cutoff_date=cutoff.isoformat(),
            history_block=history_block,
            source_evidence_getter=source_evidence_getter,
            created_at=_timestamp(effective_now),
        )
    if (
        manifest_path is None
        or not expected_manifest_sha256
        or not expected_deployed_sha
        or not approval_reference
    ):
        raise BuyoutConfirmationRecoveryError(
            "--apply requires a reviewed manifest/fingerprint, exact deployed SHA and human approval reference"
        )
    sha_file = (
        deployed_sha_file.expanduser().resolve()
        if deployed_sha_file is not None
        else runtime_dir.parent / "app" / ".wb-core-runtime-sha"
    )
    receipt_file = (
        deployment_receipt_file.expanduser().resolve()
        if deployment_receipt_file is not None
        else sha_file.with_name(".wb-core-deploy.json")
    )
    deployed_sha = _validate_governed_deployment(
        expected_deployed_sha=expected_deployed_sha,
        deployed_sha_file=sha_file,
        deployment_receipt_file=receipt_file,
    )
    try:
        with warehouse_sync_lock(runtime.runtime_dir, blocking=False):
            return _apply_manifest(
                runtime=runtime,
                evidence_dir=evidence_dir,
                manifest_path=manifest_path.expanduser().resolve(),
                expected_manifest_sha256=expected_manifest_sha256,
                deployed_sha=deployed_sha,
                deployed_sha_file=sha_file,
                deployment_receipt_file=receipt_file,
                approval_reference=str(approval_reference).strip(),
                business_date=business_date.isoformat(),
                trusted_cutoff_date=cutoff.isoformat(),
                applied_at=_timestamp(effective_now),
            )
    except WarehouseSyncBusyError as exc:
        raise BuyoutConfirmationRecoveryError(
            "canonical warehouse writer is busy; no historical buyout mutation was attempted"
        ) from exc


def _build_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    date_from: str,
    date_to: str,
    business_date: str,
    trusted_cutoff_date: str,
    history_block: SalesFunnelHistoryBlock,
    source_evidence_getter: Callable[[], Mapping[str, Any]] | None,
    created_at: str,
) -> dict[str, Any]:
    enabled_nm_ids = _enabled_nm_ids(runtime)
    if not enabled_nm_ids:
        raise BuyoutConfirmationRecoveryError("current registry has no enabled SKU targets")
    before = _window_snapshot_state(
        runtime,
        date_from=date_from,
        date_to=date_to,
    )
    non_target_digest = _temporal_non_target_digest(
        runtime.db_path,
        date_from=date_from,
        date_to=date_to,
    )
    original_snapshot_digest = _source_window_digest(
        runtime.db_path,
        source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
        date_from=date_from,
        date_to=date_to,
    )
    schema_digest = _schema_digest(runtime.db_path)
    proxy_v4_digest = _proxy_v4_digest(runtime.db_path)
    operational_store_identity = _operational_store_identity(runtime)
    source_errors: list[dict[str, str]] = []
    source_evidence: dict[str, Any] = {}
    try:
        result = history_block.execute(
            SalesFunnelHistoryRequest(
                snapshot_type=SALES_FUNNEL_HISTORY_SOURCE_KEY,
                date_from=date_from,
                date_to=date_to,
                nm_ids=enabled_nm_ids,
            )
        ).result
        exact_payloads = split_sales_funnel_success_payload_by_date(result)
        if source_evidence_getter is not None:
            source_evidence = dict(source_evidence_getter())
    except Exception as exc:  # noqa: BLE001 - dry-run records the upstream blocker.
        exact_payloads = {}
        source_errors.append(
            {
                "error_type": type(exc).__name__,
                "detail": str(exc)[:1000],
            }
        )
    required_dates = list(_iter_dates(date_from, date_to))
    missing_dates = [item for item in required_dates if item not in exact_payloads]
    incomplete_dates = [
        snapshot_date
        for snapshot_date, payload in sorted(exact_payloads.items())
        if snapshot_date in required_dates
        and not buyout_snapshot_has_enabled_sku_coverage(
            payload,
            snapshot_date=snapshot_date,
            enabled_nm_ids=set(enabled_nm_ids),
        )
    ]
    unexpected_metric_dates = [
        snapshot_date
        for snapshot_date, payload in sorted(exact_payloads.items())
        if snapshot_date in required_dates
        and {
            str(item.metric)
            for item in getattr(payload, "items", ())
        }
        - {"buyoutPercent", "orderCount"}
    ]
    authoritative = {
        snapshot_date: _plain_payload(exact_payloads[snapshot_date])
        for snapshot_date in required_dates
        if snapshot_date in exact_payloads
    }
    ready = (
        not source_errors
        and not missing_dates
        and not incomplete_dates
        and not unexpected_metric_dates
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "ready" if ready else "blocked",
        "mode": "dry-run",
        "database_written": False,
        "created_at": created_at,
        "business_date": business_date,
        "trusted_cutoff": trusted_cutoff_date,
        "maturity_days": BUYOUT_PERCENT_MATURITY_DAYS,
        "scope": {
            "source_key": BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
            "date_from": date_from,
            "date_to": date_to,
            "required_dates": required_dates,
            "enabled_nm_ids": enabled_nm_ids,
            "enabled_nm_ids_sha256": _digest(enabled_nm_ids),
        },
        "pre_change": {
            **before["summary"],
            "row_digest": before["row_digest"],
            "content_digest": before["content_digest"],
            "non_target_digest": non_target_digest,
            "overlay_rows": before["rows"],
            "original_snapshot_digest": original_snapshot_digest,
            "schema_digest": schema_digest,
            "proxy_v4_digest": proxy_v4_digest,
            "operational_store_identity": operational_store_identity,
        },
        "authoritative": {
            "source": {
                "endpoint_chain": [
                    "POST /api/v2/nm-report/downloads",
                    "GET /api/v2/nm-report/downloads",
                    "GET /api/v2/nm-report/downloads/file/{downloadId}",
                ],
                "report_type": "DETAIL_HISTORY_REPORT",
                "acquisition_evidence": source_evidence,
            },
            "snapshot_count": len(authoritative),
            "item_count": sum(
                len((payload or {}).get("items") or [])
                for payload in authoritative.values()
            ),
            "content_digest": _digest(authoritative),
            "exact_date_payloads": authoritative,
        },
        "expected_effect": {
            "replace_snapshot_count": len(required_dates),
            "existing_snapshot_count": before["summary"]["snapshot_count"],
            "write_allowlist": {
                "table": "temporal_source_snapshots",
                "source_key": BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
                "date_from": date_from,
                "date_to": date_to,
            },
            "non_target_invariant": "all temporal_source_snapshots outside the allowlisted source/date window remain byte-identical",
        },
        "blockers": {
            "source_errors": source_errors,
            "missing_dates": missing_dates,
            "incomplete_dates": incomplete_dates,
            "unexpected_metric_dates": unexpected_metric_dates,
        },
        "idempotency": "desired exact-date content digest; repeated apply returns already_applied",
        "recovery": "reviewed manifest contains the exact pre-change overlay rows; the bounded replacement is atomic and rollback requires a fresh governed restore plan",
    }
    evidence_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence_dir / (
        f"buyout-mature-backfill-plan-{created_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(manifest_path, manifest)
    manifest_sha256 = _file_digest(manifest_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "dry-run",
        "status": manifest["status"],
        "database_written": False,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_sha256,
        "date_from": date_from,
        "date_to": date_to,
        "trusted_cutoff": trusted_cutoff_date,
        "enabled_nm_id_count": len(enabled_nm_ids),
        "authoritative_snapshot_count": len(authoritative),
        "authoritative_item_count": manifest["authoritative"]["item_count"],
        "missing_dates": missing_dates,
        "incomplete_dates": incomplete_dates,
        "unexpected_metric_dates": unexpected_metric_dates,
        "source_errors": source_errors,
        "pre_change_row_digest": before["row_digest"],
        "desired_content_digest": manifest["authoritative"]["content_digest"],
        "non_target_digest": non_target_digest,
    }


def _apply_manifest(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    evidence_dir: Path,
    manifest_path: Path,
    expected_manifest_sha256: str,
    deployed_sha: str,
    deployed_sha_file: Path,
    deployment_receipt_file: Path,
    approval_reference: str,
    business_date: str,
    trusted_cutoff_date: str,
    applied_at: str,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise BuyoutConfirmationRecoveryError("reviewed manifest is missing")
    actual_manifest_sha256 = _file_digest(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise BuyoutConfirmationRecoveryError("reviewed manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "ready":
        raise BuyoutConfirmationRecoveryError("reviewed manifest is not a ready recovery manifest")
    if not approval_reference or len(approval_reference) > 500:
        raise BuyoutConfirmationRecoveryError("human approval reference is missing or invalid")
    scope = dict(manifest.get("scope") or {})
    date_from = str(scope.get("date_from") or "")
    date_to = str(scope.get("date_to") or "")
    _validate_scope(date_from=date_from, date_to=date_to)
    if date_to > trusted_cutoff_date:
        raise BuyoutConfirmationRecoveryError("trusted cutoff regressed after dry-run")
    enabled_nm_ids = _enabled_nm_ids(runtime)
    if enabled_nm_ids != [int(item) for item in scope.get("enabled_nm_ids") or []]:
        raise BuyoutConfirmationRecoveryError("enabled SKU target set changed after dry-run")
    if business_date < str(manifest.get("business_date") or ""):
        raise BuyoutConfirmationRecoveryError("business date moved backwards after dry-run")

    before = _window_snapshot_state(runtime, date_from=date_from, date_to=date_to)
    pre_change = dict(manifest.get("pre_change") or {})
    if _schema_digest(runtime.db_path) != str(pre_change.get("schema_digest") or ""):
        raise BuyoutConfirmationRecoveryError("SQLite schema changed after dry-run")
    if _source_window_digest(
        runtime.db_path,
        source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
        date_from=date_from,
        date_to=date_to,
    ) != str(pre_change.get("original_snapshot_digest") or ""):
        raise BuyoutConfirmationRecoveryError("original sales-funnel snapshots changed after dry-run")
    if _proxy_v4_digest(runtime.db_path) != str(pre_change.get("proxy_v4_digest") or ""):
        raise BuyoutConfirmationRecoveryError("frozen Proxy V4 parameters changed after dry-run")
    desired_payloads = {
        snapshot_date: _success_payload(raw_payload)
        for snapshot_date, raw_payload in dict(
            (manifest.get("authoritative") or {}).get("exact_date_payloads") or {}
        ).items()
    }
    desired_plain = {
        snapshot_date: _plain_payload(payload)
        for snapshot_date, payload in sorted(desired_payloads.items())
    }
    desired_digest = _digest(desired_plain)
    if desired_digest != str((manifest.get("authoritative") or {}).get("content_digest") or ""):
        raise BuyoutConfirmationRecoveryError("authoritative payload digest changed inside manifest")
    expected_pre = str(pre_change.get("row_digest") or "")
    if before["content_digest"] != desired_digest and before["row_digest"] != expected_pre:
        raise BuyoutConfirmationRecoveryError(
            "target window changed after dry-run; create and review a fresh manifest"
        )
    expected_non_target = str(
        pre_change.get("non_target_digest") or ""
    )
    current_non_target = _temporal_non_target_digest(
        runtime.db_path,
        date_from=date_from,
        date_to=date_to,
    )
    if current_non_target != expected_non_target:
        raise BuyoutConfirmationRecoveryError("non-target temporal snapshots changed after dry-run")
    _validate_governed_deployment(
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
        deployment_receipt_file=deployment_receipt_file,
    )

    rollback_rows = list(pre_change.get("overlay_rows") or [])
    atomic = _atomic_replace_overlay(
        runtime=runtime,
        db_path=runtime.db_path,
        date_from=date_from,
        date_to=date_to,
        captured_at=applied_at,
        desired_payloads=desired_payloads,
        desired_digest=desired_digest,
        pre_change=pre_change,
        expected_non_target_digest=expected_non_target,
        expected_roster=enabled_nm_ids,
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
        deployment_receipt_file=deployment_receipt_file,
    )
    replace_summary = atomic["replace_summary"]
    after = atomic["after"]
    post_non_target = atomic["post_non_target_digest"]
    if atomic["already_applied"]:
        reconciliation = {
            "schema_version": SCHEMA_VERSION,
            "status": "already_applied",
            "applied_at": applied_at,
            "manifest_path": str(manifest_path),
            "manifest_sha256": actual_manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "post_change_content_digest": after["content_digest"],
            "post_change_row_digest": after["row_digest"],
            "non_target_digest_after": post_non_target,
            "idempotent_noop": True,
        }
        reconciliation_path = evidence_dir / (
            f"buyout-confirmation-recovery-noop-{actual_manifest_sha256[7:19]}-{os.getpid()}.json"
        )
        _write_private_json(reconciliation_path, reconciliation)
        return {
            **reconciliation,
            "mode": "apply",
            "database_written": False,
            "reconciliation_path": str(reconciliation_path),
            "reconciliation_sha256": _file_digest(reconciliation_path),
        }
    if after["content_digest"] != desired_digest:
        raise BuyoutConfirmationRecoveryError("post-apply target readback digest mismatch")
    if post_non_target != expected_non_target:
        raise BuyoutConfirmationRecoveryError("post-apply non-target digest mismatch")
    if _source_window_digest(
        runtime.db_path,
        source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
        date_from=date_from,
        date_to=date_to,
    ) != str(pre_change.get("original_snapshot_digest") or ""):
        raise BuyoutConfirmationRecoveryError("post-apply original snapshot digest mismatch")
    if _proxy_v4_digest(runtime.db_path) != str(pre_change.get("proxy_v4_digest") or ""):
        raise BuyoutConfirmationRecoveryError("post-apply frozen Proxy V4 digest mismatch")

    reconciliation = {
        "schema_version": SCHEMA_VERSION,
        "status": "reconciled",
        "applied_at": applied_at,
        "manifest_path": str(manifest_path),
        "manifest_sha256": actual_manifest_sha256,
        "deployed_sha": deployed_sha,
        "deployed_sha_file": str(deployed_sha_file),
        "approval_reference": approval_reference,
        "rollback_overlay_rows": rollback_rows,
        "scope": scope,
        "replace_summary": replace_summary,
        "pre_change_row_digest": before["row_digest"],
        "post_change_row_digest": after["row_digest"],
        "post_change_content_digest": after["content_digest"],
        "non_target_digest_before": expected_non_target,
        "non_target_digest_after": post_non_target,
        "non_target_preserved": True,
        "idempotent_noop": False,
    }
    reconciliation_path = evidence_dir / (
        f"buyout-mature-backfill-reconciliation-{applied_at.replace(':', '').replace('-', '')}.json"
    )
    _write_private_json(reconciliation_path, reconciliation)
    reconciliation_sha256 = _file_digest(reconciliation_path)
    evidence_sha256 = _digest(
        {
            "manifest_sha256": actual_manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "rollback_overlay_rows_digest": _digest(rollback_rows),
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


def _window_snapshot_state(
    runtime: RegistryUploadDbBackedRuntime,
    *,
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    content: dict[str, Any] = {}
    item_count = 0
    with _query_only_connection(runtime.db_path) as conn:
        snapshot_rows = conn.execute(
            """
            SELECT source_key,snapshot_date,captured_at,payload_json
            FROM temporal_source_snapshots
            WHERE source_key = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
            ORDER BY snapshot_date
            """,
            (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, date_from, date_to),
        ).fetchall()
    for source_key, snapshot_date, captured_at, payload_json in snapshot_rows:
        plain = json.loads(str(payload_json))
        rows.append(
            {
                "source_key": str(source_key),
                "snapshot_date": str(snapshot_date),
                "captured_at": str(captured_at or ""),
                "payload_json": str(payload_json),
            }
        )
        content[str(snapshot_date)] = plain
        item_count += len((plain or {}).get("items") or [])
    return {
        "summary": {
            "snapshot_count": len(rows),
            "item_count": item_count,
        },
        "row_digest": _digest(rows),
        "content_digest": _digest(content),
        "rows": rows,
    }


def _atomic_replace_overlay(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    db_path: Path,
    date_from: str,
    date_to: str,
    captured_at: str,
    desired_payloads: Mapping[str, SalesFunnelHistorySuccess],
    desired_digest: str,
    pre_change: Mapping[str, Any],
    expected_non_target_digest: str,
    expected_roster: list[int],
    expected_deployed_sha: str,
    deployed_sha_file: Path,
    deployment_receipt_file: Path,
) -> dict[str, Any]:
    """Recheck every mutable precondition and replace only overlay rows atomically."""

    if not db_path.is_file():
        raise BuyoutConfirmationRecoveryError("canonical operational SQLite DB is missing")
    conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=rw", uri=True)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _validate_governed_deployment(
            expected_deployed_sha=expected_deployed_sha,
            deployed_sha_file=deployed_sha_file,
            deployment_receipt_file=deployment_receipt_file,
        )
        if _operational_store_identity(runtime) != pre_change.get("operational_store_identity"):
            raise BuyoutConfirmationRecoveryError("canonical operational generation changed after dry-run")
        if _schema_digest_conn(conn) != str(pre_change.get("schema_digest") or ""):
            raise BuyoutConfirmationRecoveryError("SQLite schema changed after dry-run")
        if _enabled_nm_ids_conn(conn) != expected_roster:
            raise BuyoutConfirmationRecoveryError("enabled SKU target set changed after dry-run")
        before = _window_snapshot_state_conn(conn, date_from=date_from, date_to=date_to)
        already_applied = (
            before["content_digest"] == desired_digest
            and _overlay_has_mature_roster_proof_conn(
                conn,
                date_from=date_from,
                date_to=date_to,
                enabled_nm_ids=expected_roster,
            )
        )
        if not already_applied and before["row_digest"] != str(pre_change.get("row_digest") or ""):
            raise BuyoutConfirmationRecoveryError("target window changed after dry-run; create and review a fresh manifest")
        if _source_window_digest_conn(
            conn, source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY, date_from=date_from, date_to=date_to
        ) != str(pre_change.get("original_snapshot_digest") or ""):
            raise BuyoutConfirmationRecoveryError("original sales-funnel snapshots changed after dry-run")
        if _proxy_v4_digest_conn(conn) != str(pre_change.get("proxy_v4_digest") or ""):
            raise BuyoutConfirmationRecoveryError("frozen Proxy V4 parameters changed after dry-run")
        if _temporal_non_target_digest_conn(conn, date_from=date_from, date_to=date_to) != expected_non_target_digest:
            raise BuyoutConfirmationRecoveryError("non-target temporal snapshots changed after dry-run")
        if already_applied:
            _validate_governed_deployment(
                expected_deployed_sha=expected_deployed_sha,
                deployed_sha_file=deployed_sha_file,
                deployment_receipt_file=deployment_receipt_file,
            )
            if _operational_store_identity(runtime) != pre_change.get("operational_store_identity"):
                raise BuyoutConfirmationRecoveryError("canonical operational generation changed before terminal readback")
            conn.commit()
            return {
                "already_applied": True,
                "before": before,
                "after": before,
                "post_non_target_digest": expected_non_target_digest,
                "replace_summary": {"deleted_snapshot_count": 0, "saved_snapshot_count": 0},
            }
        deleted = conn.execute(
            """DELETE FROM temporal_source_snapshots WHERE source_key=?
               AND snapshot_date>=? AND snapshot_date<=?""",
            (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, date_from, date_to),
        ).rowcount
        for snapshot_date, payload in sorted(desired_payloads.items()):
            conn.execute(
                """INSERT INTO temporal_source_snapshots(source_key,snapshot_date,captured_at,payload_json)
                   VALUES(?,?,?,?)""",
                (
                    BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY,
                    snapshot_date,
                    captured_at,
                    json.dumps(_plain_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                ),
            )
        after = _window_snapshot_state_conn(conn, date_from=date_from, date_to=date_to)
        post_non_target = _temporal_non_target_digest_conn(conn, date_from=date_from, date_to=date_to)
        if after["content_digest"] != desired_digest or post_non_target != expected_non_target_digest:
            raise BuyoutConfirmationRecoveryError("atomic post-apply digest mismatch")
        if _source_window_digest_conn(
            conn, source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY, date_from=date_from, date_to=date_to
        ) != str(pre_change.get("original_snapshot_digest") or ""):
            raise BuyoutConfirmationRecoveryError("post-apply original snapshot digest mismatch")
        if _proxy_v4_digest_conn(conn) != str(pre_change.get("proxy_v4_digest") or ""):
            raise BuyoutConfirmationRecoveryError("post-apply frozen Proxy V4 digest mismatch")
        _validate_governed_deployment(
            expected_deployed_sha=expected_deployed_sha,
            deployed_sha_file=deployed_sha_file,
            deployment_receipt_file=deployment_receipt_file,
        )
        if _operational_store_identity(runtime) != pre_change.get("operational_store_identity"):
            raise BuyoutConfirmationRecoveryError("canonical operational generation changed before commit")
        conn.commit()
        return {
            "already_applied": False,
            "before": before,
            "after": after,
            "post_non_target_digest": post_non_target,
            "replace_summary": {"deleted_snapshot_count": int(deleted or 0), "saved_snapshot_count": len(desired_payloads)},
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _source_window_digest(
    db_path: Path,
    *,
    source_key: str,
    date_from: str,
    date_to: str,
) -> str:
    with _query_only_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT snapshot_date,captured_at,payload_json
            FROM temporal_source_snapshots
            WHERE source_key=? AND snapshot_date>=? AND snapshot_date<=?
            ORDER BY snapshot_date
            """,
            (source_key, date_from, date_to),
        ).fetchall()
    return _digest([list(row) for row in rows])


def _window_snapshot_state_conn(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> dict[str, Any]:
    rows = []
    content: dict[str, Any] = {}
    item_count = 0
    for source_key, snapshot_date, captured_at, payload_json in conn.execute(
        """SELECT source_key,snapshot_date,captured_at,payload_json FROM temporal_source_snapshots
           WHERE source_key=? AND snapshot_date>=? AND snapshot_date<=? ORDER BY snapshot_date""",
        (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, date_from, date_to),
    ):
        plain = json.loads(str(payload_json))
        rows.append({"source_key": str(source_key), "snapshot_date": str(snapshot_date), "captured_at": str(captured_at or ""), "payload_json": str(payload_json)})
        content[str(snapshot_date)] = plain
        item_count += len((plain or {}).get("items") or [])
    return {"summary": {"snapshot_count": len(rows), "item_count": item_count}, "row_digest": _digest(rows), "content_digest": _digest(content), "rows": rows}


def _overlay_has_mature_roster_proof_conn(
    conn: sqlite3.Connection,
    *,
    date_from: str,
    date_to: str,
    enabled_nm_ids: list[int],
) -> bool:
    expected_dates = list(_iter_dates(date_from, date_to))
    rows = {
        str(snapshot_date): (str(captured_at or ""), str(payload_json))
        for snapshot_date, captured_at, payload_json in conn.execute(
            """SELECT snapshot_date,captured_at,payload_json FROM temporal_source_snapshots
               WHERE source_key=? AND snapshot_date>=? AND snapshot_date<=?""",
            (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, date_from, date_to),
        )
    }
    return all(
        snapshot_date in rows
        and mature_buyout_capture_proof(
            payload=_success_payload(json.loads(rows[snapshot_date][1])),
            captured_at=rows[snapshot_date][0],
            snapshot_date=snapshot_date,
            enabled_nm_ids=enabled_nm_ids,
        )
        for snapshot_date in expected_dates
    )


def _source_window_digest_conn(
    conn: sqlite3.Connection, *, source_key: str, date_from: str, date_to: str
) -> str:
    return _digest([list(row) for row in conn.execute(
        """SELECT snapshot_date,captured_at,payload_json FROM temporal_source_snapshots
           WHERE source_key=? AND snapshot_date>=? AND snapshot_date<=? ORDER BY snapshot_date""",
        (source_key, date_from, date_to),
    )])


def _schema_digest(db_path: Path) -> str:
    with _query_only_connection(db_path) as conn:
        rows = conn.execute(
            """SELECT type,name,tbl_name,sql FROM sqlite_master
               WHERE tbl_name IN (
                   'temporal_source_snapshots',
                   'sheet_vitrina_v1_proxy_v4_parameter_versions'
               ) OR name IN (
                   'temporal_source_snapshots',
                   'sheet_vitrina_v1_proxy_v4_parameter_versions'
               )
               ORDER BY type,name"""
        ).fetchall()
    return _digest([list(row) for row in rows])


def _schema_digest_conn(conn: sqlite3.Connection) -> str:
    return _digest([list(row) for row in conn.execute(
        """SELECT type,name,tbl_name,sql FROM sqlite_master
           WHERE tbl_name IN ('temporal_source_snapshots','sheet_vitrina_v1_proxy_v4_parameter_versions')
              OR name IN ('temporal_source_snapshots','sheet_vitrina_v1_proxy_v4_parameter_versions')
           ORDER BY type,name"""
    )])


def _proxy_v4_digest(db_path: Path) -> str:
    with _query_only_connection(db_path) as conn:
        exists = conn.execute(
            """SELECT 1 FROM sqlite_master WHERE type='table'
               AND name='sheet_vitrina_v1_proxy_v4_parameter_versions'"""
        ).fetchone()
        if exists is None:
            return _digest([])
        rows = conn.execute(
            """SELECT version_id,block_key,revision,effective_date,source_window_from,
                      source_window_to,source_window_fingerprint,parameters_json,
                      fingerprint,version_kind,created_by,created_at
               FROM sheet_vitrina_v1_proxy_v4_parameter_versions
               ORDER BY revision,version_id"""
        ).fetchall()
    return _digest([list(row) for row in rows])


def _proxy_v4_digest_conn(conn: sqlite3.Connection) -> str:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sheet_vitrina_v1_proxy_v4_parameter_versions'").fetchone()
    if exists is None:
        return _digest([])
    return _digest([list(row) for row in conn.execute(
        """SELECT version_id,block_key,revision,effective_date,source_window_from,source_window_to,
                  source_window_fingerprint,parameters_json,fingerprint,version_kind,created_by,created_at
           FROM sheet_vitrina_v1_proxy_v4_parameter_versions ORDER BY revision,version_id"""
    )])


def _temporal_non_target_digest(
    db_path: Path,
    *,
    date_from: str,
    date_to: str,
) -> str:
    with _query_only_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT source_key,snapshot_date,captured_at,payload_json
            FROM temporal_source_snapshots
            WHERE NOT (
                source_key = ?
                AND snapshot_date >= ?
                AND snapshot_date <= ?
            )
            ORDER BY source_key,snapshot_date
            """,
            (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, date_from, date_to),
        ).fetchall()
        return _digest([list(row) for row in rows])


def _temporal_non_target_digest_conn(
    conn: sqlite3.Connection, *, date_from: str, date_to: str
) -> str:
    return _digest([list(row) for row in conn.execute(
        """SELECT source_key,snapshot_date,captured_at,payload_json
           FROM temporal_source_snapshots
           WHERE NOT (source_key=? AND snapshot_date>=? AND snapshot_date<=?)
           ORDER BY source_key,snapshot_date""",
        (BUYOUT_CONFIRMATION_OVERLAY_SOURCE_KEY, date_from, date_to),
    )])


def _validate_governed_deployment(
    *,
    expected_deployed_sha: str,
    deployed_sha_file: Path,
    deployment_receipt_file: Path,
) -> str:
    expected = str(expected_deployed_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", expected) is None:
        raise BuyoutConfirmationRecoveryError("expected deployed SHA must be exactly 40 hex characters")
    if not deployed_sha_file.is_file():
        raise BuyoutConfirmationRecoveryError("deployed runtime SHA marker is missing")
    actual = deployed_sha_file.read_text(encoding="utf-8").strip().lower()
    if actual != expected:
        raise BuyoutConfirmationRecoveryError(
            f"deployed runtime SHA mismatch: expected={expected}, actual={actual or '<missing>'}"
        )
    if not deployment_receipt_file.is_file():
        raise BuyoutConfirmationRecoveryError("governed deployment receipt is missing")
    try:
        receipt = json.loads(deployment_receipt_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuyoutConfirmationRecoveryError("governed deployment receipt is unreadable") from exc
    if (
        receipt.get("commit") != expected
        or receipt.get("deployment_complete") is not True
    ):
        raise BuyoutConfirmationRecoveryError(
            "governed deployment receipt is incomplete or does not bind the expected SHA"
        )
    return expected


def _enabled_nm_ids(runtime: RegistryUploadDbBackedRuntime) -> list[int]:
    with _query_only_connection(runtime.db_path) as conn:
        row = conn.execute(
            "SELECT bundle_version FROM registry_upload_current_state WHERE slot=1"
        ).fetchone()
        if row is None:
            raise BuyoutConfirmationRecoveryError("runtime current registry state is missing")
        return [
            int(item[0])
            for item in conn.execute(
                """
                SELECT nm_id
                FROM registry_upload_config_v2
                WHERE bundle_version=? AND enabled=1
                ORDER BY nm_id
                """,
                (str(row[0]),),
            ).fetchall()
        ]


def _operational_store_identity(runtime: RegistryUploadDbBackedRuntime) -> dict[str, str]:
    manifest = runtime.store_registry.load(require_files=True)
    generation = runtime.store_registry.generation("operational", manifest=manifest)
    resolved = runtime.store_registry.resolve("operational", manifest=manifest)
    if resolved.resolve() != runtime.db_path.resolve():
        raise BuyoutConfirmationRecoveryError("runtime operational store identity drifted")
    return {
        "manifest_sha256": str(manifest.manifest_sha256),
        "generation_id": str(generation.generation_id),
        "generation_epoch": str(generation.generation_epoch),
        "relative_path": str(generation.relative_path),
        "schema_revision": str(generation.schema_revision),
        "db_path": str(resolved),
    }


def _enabled_nm_ids_conn(conn: sqlite3.Connection) -> list[int]:
    row = conn.execute(
        "SELECT bundle_version FROM registry_upload_current_state WHERE slot=1"
    ).fetchone()
    if row is None:
        raise BuyoutConfirmationRecoveryError("runtime current registry state is missing")
    return [
        int(item[0])
        for item in conn.execute(
            """SELECT nm_id FROM registry_upload_config_v2
               WHERE bundle_version=? AND enabled=1 ORDER BY nm_id""",
            (str(row[0]),),
        ).fetchall()
    ]


@contextmanager
def _query_only_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _success_payload(raw: Mapping[str, Any]) -> SalesFunnelHistorySuccess:
    items = [
        SalesFunnelHistoryItem(
            date=str(item["date"]),
            nm_id=int(item["nm_id"]),
            metric=str(item["metric"]),
            value=float(item["value"]),
        )
        for item in raw.get("items") or []
    ]
    return SalesFunnelHistorySuccess(
        kind="success",
        date_from=str(raw.get("date_from") or ""),
        date_to=str(raw.get("date_to") or ""),
        count=len(items),
        items=items,
    )


def _plain_payload(value: Any) -> Any:
    if is_dataclass(value):
        return _plain_payload(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _plain_payload(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_plain_payload(item) for item in value]
    if hasattr(value, "__dict__"):
        return _plain_payload(vars(value))
    return value


def _digest(value: Any) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    body = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise BuyoutConfirmationRecoveryError("evidence path already exists") from exc
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _validate_scope(*, date_from: str, date_to: str) -> None:
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise BuyoutConfirmationRecoveryError("date scope must use ISO YYYY-MM-DD") from exc
    if (date_from, date_to) != (RECOVERY_DATE_FROM, RECOVERY_DATE_TO):
        raise BuyoutConfirmationRecoveryError(
            f"this runner is bounded to {RECOVERY_DATE_FROM}..{RECOVERY_DATE_TO}"
        )
    if end < start or (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise BuyoutConfirmationRecoveryError("backfill window is invalid or too wide")


def _require_evidence_outside_repo(evidence_dir: Path) -> None:
    try:
        evidence_dir.relative_to(ROOT)
    except ValueError:
        return
    raise BuyoutConfirmationRecoveryError("production evidence directory must be outside Git")


def _iter_dates(date_from: str, date_to: str):
    current = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BuyoutConfirmationRecoveryError("now must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--date-from", default=RECOVERY_DATE_FROM)
    parser.add_argument("--date-to", default=RECOVERY_DATE_TO)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--manifest")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--expected-deployed-sha")
    parser.add_argument("--deployed-sha-file")
    parser.add_argument("--approval-reference")
    args = parser.parse_args()
    if args.apply and (
        not args.manifest
        or not args.expected_manifest_sha256
        or not args.expected_deployed_sha
        or not args.approval_reference
    ):
        parser.error(
            "--apply requires --manifest, --expected-manifest-sha256, "
            "--expected-deployed-sha and --approval-reference"
        )
    if not args.apply:
        args.dry_run = True
    return args


def main() -> None:
    args = _parse_args()
    try:
        result = run_recovery(
            runtime_dir=Path(args.runtime_dir),
            evidence_dir=Path(args.evidence_dir),
            date_from=args.date_from,
            date_to=args.date_to,
            apply=bool(args.apply),
            manifest_path=Path(args.manifest) if args.manifest else None,
            expected_manifest_sha256=args.expected_manifest_sha256,
            expected_deployed_sha=args.expected_deployed_sha,
            deployed_sha_file=(
                Path(args.deployed_sha_file) if args.deployed_sha_file else None
            ),
            approval_reference=args.approval_reference,
        )
    except BuyoutConfirmationRecoveryError as exc:
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
