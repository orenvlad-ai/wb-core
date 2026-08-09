"""Guarded official-API reconcile for the unproven mature buyout window."""

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
    DB_FILENAME,
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
    SALES_FUNNEL_HISTORY_SOURCE_KEY,
    buyout_snapshot_has_enabled_sku_coverage,
    split_sales_funnel_success_payload_by_date,
    trusted_buyout_cutoff,
)
from packages.business_time import current_business_date_iso  # noqa: E402
from packages.contracts.sales_funnel_history_block import (  # noqa: E402
    SalesFunnelHistoryItem,
    SalesFunnelHistoryRequest,
    SalesFunnelHistorySuccess,
)


SCHEMA_VERSION = "sheet_vitrina_v1_buyout_mature_backfill_v3"
BACKFILL_DATE_FROM = "2026-07-06"
BACKFILL_DATE_TO = "2026-07-12"
MAX_WINDOW_DAYS = 31


class BuyoutMatureBackfillError(RuntimeError):
    """A guarded plan/apply condition failed closed."""


def run_backfill(
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
    approval_reference: str | None = None,
    history_block: SalesFunnelHistoryBlock | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    runtime_dir = runtime_dir.expanduser().resolve()
    evidence_dir = evidence_dir.expanduser().resolve()
    _validate_scope(date_from=date_from, date_to=date_to)
    _require_evidence_outside_repo(evidence_dir)
    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    if not (runtime_dir / DB_FILENAME).is_file():
        raise BuyoutMatureBackfillError("canonical runtime SQLite DB is missing")
    effective_now = now or datetime.now(timezone.utc)
    business_date = datetime.strptime(
        current_business_date_iso(effective_now), "%Y-%m-%d"
    ).date()
    cutoff = trusted_buyout_cutoff(business_date)
    if date_to > cutoff.isoformat():
        raise BuyoutMatureBackfillError(
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
        raise BuyoutMatureBackfillError(
            "--apply requires a reviewed manifest/fingerprint, exact deployed SHA and human approval reference"
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
                expected_manifest_sha256=expected_manifest_sha256,
                deployed_sha=deployed_sha,
                deployed_sha_file=sha_file,
                approval_reference=str(approval_reference).strip(),
                business_date=business_date.isoformat(),
                trusted_cutoff_date=cutoff.isoformat(),
                applied_at=_timestamp(effective_now),
            )
    except WarehouseSyncBusyError as exc:
        raise BuyoutMatureBackfillError(
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
        raise BuyoutMatureBackfillError("current registry has no enabled SKU targets")
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
    authoritative = {
        snapshot_date: _plain_payload(exact_payloads[snapshot_date])
        for snapshot_date in required_dates
        if snapshot_date in exact_payloads
    }
    ready = not source_errors and not missing_dates and not incomplete_dates
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
            "source_key": SALES_FUNNEL_HISTORY_SOURCE_KEY,
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
                "source_key": SALES_FUNNEL_HISTORY_SOURCE_KEY,
                "date_from": date_from,
                "date_to": date_to,
            },
            "non_target_invariant": "all temporal_source_snapshots outside the allowlisted source/date window remain byte-identical",
        },
        "blockers": {
            "source_errors": source_errors,
            "missing_dates": missing_dates,
            "incomplete_dates": incomplete_dates,
        },
        "idempotency": "desired exact-date content digest; repeated apply returns already_applied",
        "recovery": "verified coherent SQLite backup is created before the atomic replacement; restore it only under the canonical maintenance/write-barrier procedure",
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
    approval_reference: str,
    business_date: str,
    trusted_cutoff_date: str,
    applied_at: str,
) -> dict[str, Any]:
    if not manifest_path.is_file():
        raise BuyoutMatureBackfillError("reviewed manifest is missing")
    actual_manifest_sha256 = _file_digest(manifest_path)
    if actual_manifest_sha256 != expected_manifest_sha256:
        raise BuyoutMatureBackfillError("reviewed manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != SCHEMA_VERSION or manifest.get("status") != "ready":
        raise BuyoutMatureBackfillError("reviewed manifest is not a ready v3 manifest")
    if not approval_reference or len(approval_reference) > 500:
        raise BuyoutMatureBackfillError("human approval reference is missing or invalid")
    scope = dict(manifest.get("scope") or {})
    date_from = str(scope.get("date_from") or "")
    date_to = str(scope.get("date_to") or "")
    _validate_scope(date_from=date_from, date_to=date_to)
    if date_to > trusted_cutoff_date:
        raise BuyoutMatureBackfillError("trusted cutoff regressed after dry-run")
    enabled_nm_ids = _enabled_nm_ids(runtime)
    if enabled_nm_ids != [int(item) for item in scope.get("enabled_nm_ids") or []]:
        raise BuyoutMatureBackfillError("enabled SKU target set changed after dry-run")
    if business_date < str(manifest.get("business_date") or ""):
        raise BuyoutMatureBackfillError("business date moved backwards after dry-run")

    before = _window_snapshot_state(runtime, date_from=date_from, date_to=date_to)
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
        raise BuyoutMatureBackfillError("authoritative payload digest changed inside manifest")
    if before["content_digest"] == desired_digest:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "apply",
            "status": "already_applied",
            "database_written": False,
            "manifest_sha256": actual_manifest_sha256,
            "deployed_sha": deployed_sha,
            "approval_reference": approval_reference,
            "post_change_content_digest": desired_digest,
            "non_target_preserved": True,
            "idempotent_noop": True,
        }
    expected_pre = str((manifest.get("pre_change") or {}).get("row_digest") or "")
    if before["row_digest"] != expected_pre:
        raise BuyoutMatureBackfillError(
            "target window changed after dry-run; create and review a fresh manifest"
        )
    expected_non_target = str(
        (manifest.get("pre_change") or {}).get("non_target_digest") or ""
    )
    current_non_target = _temporal_non_target_digest(
        runtime.db_path,
        date_from=date_from,
        date_to=date_to,
    )
    if current_non_target != expected_non_target:
        raise BuyoutMatureBackfillError("non-target temporal snapshots changed after dry-run")
    _validate_exact_deployment(
        expected_deployed_sha=deployed_sha,
        deployed_sha_file=deployed_sha_file,
    )

    evidence_dir.mkdir(parents=True, exist_ok=True)
    backup_path = evidence_dir / "backups" / (
        f"registry-{applied_at.replace(':', '').replace('-', '')}-{actual_manifest_sha256[:12]}.sqlite3"
    )
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_sha256 = _backup_and_verify(runtime.db_path, backup_path)
    replace_summary = runtime.replace_temporal_source_snapshot_window(
        source_key=SALES_FUNNEL_HISTORY_SOURCE_KEY,
        date_from=date_from,
        date_to=date_to,
        captured_at=applied_at,
        exact_date_payloads=desired_payloads,
    )
    after = _window_snapshot_state(runtime, date_from=date_from, date_to=date_to)
    post_non_target = _temporal_non_target_digest(
        runtime.db_path,
        date_from=date_from,
        date_to=date_to,
    )
    if after["content_digest"] != desired_digest:
        raise BuyoutMatureBackfillError("post-apply target readback digest mismatch")
    if post_non_target != expected_non_target:
        raise BuyoutMatureBackfillError("post-apply non-target digest mismatch")

    reconciliation = {
        "schema_version": SCHEMA_VERSION,
        "status": "reconciled",
        "applied_at": applied_at,
        "manifest_path": str(manifest_path),
        "manifest_sha256": actual_manifest_sha256,
        "deployed_sha": deployed_sha,
        "deployed_sha_file": str(deployed_sha_file),
        "approval_reference": approval_reference,
        "backup_path": str(backup_path),
        "backup_sha256": backup_sha256,
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
            SELECT snapshot_date,captured_at,payload_json
            FROM temporal_source_snapshots
            WHERE source_key = ?
              AND snapshot_date >= ?
              AND snapshot_date <= ?
            ORDER BY snapshot_date
            """,
            (SALES_FUNNEL_HISTORY_SOURCE_KEY, date_from, date_to),
        ).fetchall()
    for snapshot_date, captured_at, payload_json in snapshot_rows:
        plain = json.loads(str(payload_json))
        rows.append(
            {
                "snapshot_date": str(snapshot_date),
                "captured_at": str(captured_at or ""),
                "payload": plain,
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
    }


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
            (SALES_FUNNEL_HISTORY_SOURCE_KEY, date_from, date_to),
        ).fetchall()
        return _digest([list(row) for row in rows])


def _backup_and_verify(db_path: Path, backup_path: Path) -> str:
    source = sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)
    target = sqlite3.connect(str(backup_path))
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    os.chmod(backup_path, 0o600)
    descriptor = os.open(backup_path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    verify = sqlite3.connect(f"file:{quote(str(backup_path))}?mode=ro", uri=True)
    try:
        verify.execute("PRAGMA query_only=ON")
        integrity = str(verify.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise BuyoutMatureBackfillError(
                f"backup integrity_check failed: {integrity}"
            )
        foreign_keys = verify.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise BuyoutMatureBackfillError("backup foreign_key_check failed")
    finally:
        verify.close()
    return _file_digest(backup_path)


def _validate_exact_deployment(
    *,
    expected_deployed_sha: str,
    deployed_sha_file: Path,
) -> str:
    expected = str(expected_deployed_sha or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{40}", expected) is None:
        raise BuyoutMatureBackfillError("expected deployed SHA must be exactly 40 hex characters")
    if not deployed_sha_file.is_file():
        raise BuyoutMatureBackfillError("deployed runtime SHA marker is missing")
    actual = deployed_sha_file.read_text(encoding="utf-8").strip().lower()
    if actual != expected:
        raise BuyoutMatureBackfillError(
            f"deployed runtime SHA mismatch: expected={expected}, actual={actual or '<missing>'}"
        )
    return expected


def _enabled_nm_ids(runtime: RegistryUploadDbBackedRuntime) -> list[int]:
    with _query_only_connection(runtime.db_path) as conn:
        row = conn.execute(
            "SELECT bundle_version FROM registry_upload_current_state WHERE slot=1"
        ).fetchone()
        if row is None:
            raise BuyoutMatureBackfillError("runtime current registry state is missing")
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
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _validate_scope(*, date_from: str, date_to: str) -> None:
    try:
        start = datetime.strptime(date_from, "%Y-%m-%d").date()
        end = datetime.strptime(date_to, "%Y-%m-%d").date()
    except ValueError as exc:
        raise BuyoutMatureBackfillError("date scope must use ISO YYYY-MM-DD") from exc
    if (date_from, date_to) != (BACKFILL_DATE_FROM, BACKFILL_DATE_TO):
        raise BuyoutMatureBackfillError(
            f"this runner is bounded to {BACKFILL_DATE_FROM}..{BACKFILL_DATE_TO}"
        )
    if end < start or (end - start).days + 1 > MAX_WINDOW_DAYS:
        raise BuyoutMatureBackfillError("backfill window is invalid or too wide")


def _require_evidence_outside_repo(evidence_dir: Path) -> None:
    try:
        evidence_dir.relative_to(ROOT)
    except ValueError:
        return
    raise BuyoutMatureBackfillError("production evidence directory must be outside Git")


def _iter_dates(date_from: str, date_to: str):
    current = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date()
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BuyoutMatureBackfillError("now must be timezone-aware")
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--date-from", default=BACKFILL_DATE_FROM)
    parser.add_argument("--date-to", default=BACKFILL_DATE_TO)
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
        result = run_backfill(
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
    except BuyoutMatureBackfillError as exc:
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
