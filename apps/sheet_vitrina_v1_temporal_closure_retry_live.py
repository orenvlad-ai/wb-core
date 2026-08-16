"""Guarded live runner for temporal closure retry and exact-date recovery.

The timer-owned no-date mode remains the ordinary persisted retry cycle.  An
explicit ``--date`` is a production-data recovery boundary: it is query-only by
default and requires a fresh manifest fingerprint, exact deployed SHA, immutable
human gate evidence and a coherent SQLite backup before apply.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.registry_upload_http_entrypoint import (  # noqa: E402
    load_registry_upload_http_entrypoint_config,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    RegistryUploadDbBackedRuntime,
)
from packages.application.registry_upload_http_entrypoint import (  # noqa: E402
    RegistryUploadHttpEntrypoint,
)
from packages.application.sheet_vitrina_v1_live_plan import (  # noqa: E402
    CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS,
    HISTORICAL_CLOSED_DAY_SOURCE_KEYS,
)
from packages.application.storage_registry import StoreRegistry  # noqa: E402
from packages.business_time import current_business_date_iso  # noqa: E402


PLAN_CONTRACT = "sheet_vitrina_v1_exact_date_recovery_plan_v1"
RESULT_CONTRACT = "sheet_vitrina_v1_exact_date_recovery_result_v1"
DEFAULT_SHA_MARKER = Path("/opt/wb-core-runtime/app/.wb-core-runtime-sha")
MAX_EXPLICIT_DATES = 7
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

_TABLE_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "sheet_vitrina_v1_ready_snapshots",
        "as_of_date",
        ("bundle_version", "as_of_date"),
    ),
    (
        "temporal_source_snapshots",
        "snapshot_date",
        ("source_key", "snapshot_date"),
    ),
    (
        "temporal_source_slot_snapshots",
        "snapshot_date",
        ("source_key", "snapshot_date", "snapshot_role"),
    ),
    (
        "temporal_source_closure_state",
        "target_date",
        ("source_key", "target_date", "slot_kind"),
    ),
)


class TemporalRecoveryError(ValueError):
    """The exact-date recovery boundary is invalid or has drifted."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the ordinary timer retry when no --date is supplied. Explicit "
            "dates produce a query-only recovery manifest unless --apply is given."
        ),
    )
    parser.add_argument(
        "--date",
        dest="dates",
        action="append",
        default=[],
        help="Exact closed business date (repeatable, YYYY-MM-DD; max 7).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply explicit dates after every exact recovery gate is supplied.",
    )
    parser.add_argument("--manifest-fingerprint", default="")
    parser.add_argument("--deployed-sha", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--approval-digest", default="")
    parser.add_argument(
        "--runtime-sha-marker",
        default=os.environ.get("WB_CORE_RUNTIME_SHA_MARKER", str(DEFAULT_SHA_MARKER)),
    )
    parser.add_argument(
        "--backup-dir",
        default=os.environ.get("WB_CORE_TEMPORAL_RECOVERY_BACKUP_DIR", ""),
    )
    parser.add_argument(
        "--skip-auto-load-visible",
        action="store_true",
        help="Deprecated no-op: legacy Google Sheets load is archived.",
    )
    return parser.parse_args()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_dates(raw_dates: Sequence[str], *, current_date: str) -> list[str]:
    dates = sorted({str(value or "").strip() for value in raw_dates if str(value or "").strip()})
    if not dates:
        raise TemporalRecoveryError("at least one explicit --date is required")
    if len(dates) > MAX_EXPLICIT_DATES:
        raise TemporalRecoveryError(
            f"explicit recovery is bounded to {MAX_EXPLICIT_DATES} dates"
        )
    for value in dates:
        try:
            normalized = date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise TemporalRecoveryError(f"invalid recovery date: {value!r}") from exc
        if normalized != value:
            raise TemporalRecoveryError(f"recovery date must be canonical YYYY-MM-DD: {value!r}")
        if value >= current_date:
            raise TemporalRecoveryError(
                f"recovery date must be closed before current business date {current_date}: {value}"
            )
    return dates


def _deployed_sha(marker_path: Path) -> str:
    try:
        value = marker_path.read_text(encoding="utf-8").strip().lower()
    except OSError as exc:
        raise TemporalRecoveryError(
            f"deployed SHA marker is unavailable: {marker_path}"
        ) from exc
    if not SHA_RE.fullmatch(value):
        raise TemporalRecoveryError("deployed SHA marker must contain one exact 40-hex SHA")
    return value


def _table_exists(conn: Any, table_name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        is not None
    )


def _table_columns(conn: Any, table_name: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")')]


def _row_contract(
    conn: Any,
    *,
    table_name: str,
    date_column: str,
    key_columns: Sequence[str],
    scope_dates: Sequence[str],
) -> dict[str, Any]:
    if not _table_exists(conn, table_name):
        raise TemporalRecoveryError(f"required recovery table is missing: {table_name}")
    columns = _table_columns(conn, table_name)
    if date_column not in columns or any(key not in columns for key in key_columns):
        raise TemporalRecoveryError(f"recovery table schema drifted: {table_name}")
    placeholders = ",".join("?" for _ in scope_dates)
    order = ",".join(f'"{column}"' for column in key_columns)
    selected = ",".join(f'"{column}"' for column in columns)
    scope_rows = conn.execute(
        f'SELECT {selected} FROM "{table_name}" '
        f'WHERE "{date_column}" IN ({placeholders}) ORDER BY {order}',
        tuple(scope_dates),
    ).fetchall()
    non_target_rows = conn.execute(
        f'SELECT {selected} FROM "{table_name}" '
        f'WHERE "{date_column}" NOT IN ({placeholders}) ORDER BY {order}',
        tuple(scope_dates),
    ).fetchall()

    def plain(rows: Sequence[Any]) -> list[list[Any]]:
        return [[row[column] for column in columns] for row in rows]

    key_hashes = {
        "|".join(str(row[column]) for column in key_columns): _canonical_digest(
            [row[column] for column in columns]
        )
        for row in scope_rows
    }
    return {
        "scope_row_count": len(scope_rows),
        "scope_digest": _canonical_digest(plain(scope_rows)),
        "scope_keys": sorted(key_hashes),
        "scope_key_hashes": key_hashes,
        "non_target_row_count": len(non_target_rows),
        "non_target_digest": _canonical_digest(plain(non_target_rows)),
    }


def _closure_summary(conn: Any, *, scope_dates: Sequence[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in scope_dates)
    rows = conn.execute(
        "SELECT source_key,target_date,slot_kind,state,attempt_count,next_retry_at,"
        "last_attempt_at,last_success_at,accepted_at "
        "FROM temporal_source_closure_state "
        f"WHERE target_date IN ({placeholders}) "
        "ORDER BY target_date,source_key,slot_kind",
        tuple(scope_dates),
    ).fetchall()
    return [dict(row) for row in rows]


def build_explicit_recovery_plan(
    *,
    runtime_dir: Path,
    raw_dates: Sequence[str],
    sha_marker_path: Path,
    backup_dir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build one deterministic query-only plan for explicit closed dates."""

    current_date = current_business_date_iso(now or datetime.now(timezone.utc))
    dates = _validate_dates(raw_dates, current_date=current_date)
    deployed_sha = _deployed_sha(sha_marker_path)
    registry = StoreRegistry(runtime_dir)
    generation = registry.load(require_files=True)
    operational_path = registry.resolve("operational", manifest=generation)
    resolved_backup_dir = (
        Path(backup_dir).expanduser().resolve()
        if backup_dir is not None
        else (Path(runtime_dir).expanduser().resolve() / "temporal-recovery-backups")
    )
    table_contracts: dict[str, Any] = {}
    with registry.connect(
        "operational",
        mode="ro",
        operation="sheet_vitrina_exact_date_recovery_dry_run",
        manifest=generation,
    ) as conn:
        if int(conn.execute("PRAGMA query_only").fetchone()[0]) != 1:
            raise TemporalRecoveryError("dry-run connection is not query-only")
        for table_name, date_column, key_columns in _TABLE_SPECS:
            dates_for_table = dates if table_name == "sheet_vitrina_v1_ready_snapshots" else sorted({*dates, current_date})
            table_contracts[table_name] = _row_contract(
                conn,
                table_name=table_name,
                date_column=date_column,
                key_columns=key_columns,
                scope_dates=dates_for_table,
            )
        closure = _closure_summary(conn, scope_dates=sorted({*dates, current_date}))
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])

    source_keys = sorted(
        HISTORICAL_CLOSED_DAY_SOURCE_KEYS | CURRENT_SNAPSHOT_ONLY_SOURCE_KEYS
    )
    public_tables = {
        table: {key: value for key, value in contract.items() if key != "scope_key_hashes"}
        for table, contract in table_contracts.items()
    }
    basis = {
        "contract_name": PLAN_CONTRACT,
        "mode": "query_only_dry_run",
        "deployed_sha": deployed_sha,
        "current_business_date": current_date,
        "target_closed_dates": dates,
        "incidental_current_scope_date": current_date,
        "operational_generation": {
            "state": generation.state,
            "canonical_source": generation.canonical_source,
            "generation_id": generation.operational.generation_id,
            "generation_epoch": generation.operational.generation_epoch,
            "schema_revision": generation.operational.schema_revision,
            "manifest_sha256": generation.manifest_sha256,
            "schema_version": schema_version,
        },
        "prechange": {
            "tables": public_tables,
            "closure_states": closure,
            "operational_coherent_size_upper_bound": max(
                operational_path.stat().st_size,
                page_count * page_size,
            ),
        },
        "expected_affected_records": {
            "ready_snapshot_upserts_max": len(dates),
            "temporal_source_date_slots_max": len(source_keys) * (len(dates) + 1),
            "closure_state_date_slots_max": len(source_keys) * (len(dates) + 1) * 2,
            "source_keys": source_keys,
            "note": (
                "Exact rows are source-result dependent. The apply may upsert only "
                "requested closed dates plus the explicit current-day slot produced "
                "by the canonical temporal plan."
            ),
        },
        "non_target_invariants": {
            "contract": "all rows outside the declared dates in the four temporal/ready tables retain exact logical digests",
            "table_digests": {
                table: contract["non_target_digest"]
                for table, contract in table_contracts.items()
            },
        },
        "backup_and_recovery": {
            "backup_dir": str(resolved_backup_dir),
            "backup_kind": "coherent_sqlite_backup_with_integrity_check_and_sha256",
            "backup_created_only_during_apply": True,
            "sidecar_policy": (
                "append-only source artifacts may be added by canonical collectors; "
                "they are evidence and are not deleted by rollback"
            ),
            "recovery": (
                "No blind repeat or automatic restore. On partial failure retain the "
                "backup and result evidence, stop writers, re-plan, and either perform "
                "a reviewed forward recovery or restore the exact coherent backup."
            ),
        },
    }
    fingerprint = _canonical_digest(basis)
    return {
        **basis,
        "manifest_fingerprint": fingerprint,
        "backup_destination": str(
            resolved_backup_dir / f"sheet-vitrina-exact-date-{fingerprint.split(':', 1)[1]}.sqlite3"
        ),
        "_internal": {"tables": table_contracts},
    }


def _table_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, int]:
    before_hashes = dict(before.get("scope_key_hashes") or {})
    after_hashes = dict(after.get("scope_key_hashes") or {})
    before_keys = set(before_hashes)
    after_keys = set(after_hashes)
    return {
        "inserted": len(after_keys - before_keys),
        "removed": len(before_keys - after_keys),
        "changed": sum(
            before_hashes[key] != after_hashes[key]
            for key in before_keys & after_keys
        ),
        "unchanged": sum(
            before_hashes[key] == after_hashes[key]
            for key in before_keys & after_keys
        ),
    }


def apply_explicit_recovery(
    *,
    runtime_dir: Path,
    raw_dates: Sequence[str],
    sha_marker_path: Path,
    manifest_fingerprint: str,
    deployed_sha: str,
    approval_reference: str,
    approval_digest: str,
    backup_dir: Path | None = None,
    cycle_runner: Callable[[list[str]], Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one fresh exact manifest after backup; never guesses a gate."""

    if not DIGEST_RE.fullmatch(manifest_fingerprint):
        raise TemporalRecoveryError("exact sha256 manifest fingerprint is required")
    normalized_sha = str(deployed_sha or "").strip().lower()
    if not SHA_RE.fullmatch(normalized_sha):
        raise TemporalRecoveryError("exact deployed 40-hex SHA is required")
    if not str(approval_reference or "").strip():
        raise TemporalRecoveryError("immutable human approval reference is required")
    if not DIGEST_RE.fullmatch(str(approval_digest or "").strip().lower()):
        raise TemporalRecoveryError("exact sha256 approval digest is required")

    before = build_explicit_recovery_plan(
        runtime_dir=runtime_dir,
        raw_dates=raw_dates,
        sha_marker_path=sha_marker_path,
        backup_dir=backup_dir,
        now=now,
    )
    if before["manifest_fingerprint"] != manifest_fingerprint:
        raise TemporalRecoveryError("recovery manifest drifted; build and approve a fresh dry-run")
    if before["deployed_sha"] != normalized_sha:
        raise TemporalRecoveryError("deployed SHA drifted from the approved apply")

    runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
    backup = runtime.backup_database(Path(before["backup_destination"]))
    try:
        if cycle_runner is None:
            activated_at_override = os.environ.get(
                "REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE", ""
            ).strip()
            entrypoint = RegistryUploadHttpEntrypoint(
                runtime_dir=runtime_dir,
                activated_at_factory=(
                    (lambda: activated_at_override)
                    if activated_at_override
                    else None
                ),
            )
            cycle_payload = entrypoint.run_sheet_temporal_closure_retry_cycle(
                target_dates=list(before["target_closed_dates"]),
                auto_load_visible=False,
            )
        else:
            cycle_payload = dict(cycle_runner(list(before["target_closed_dates"])))
    except Exception as exc:  # noqa: BLE001 - preserve exact backup for reviewed recovery.
        evidence = {
            "contract_name": RESULT_CONTRACT,
            "status": "apply_failed_recovery_required",
            "manifest_fingerprint": manifest_fingerprint,
            "deployed_sha": normalized_sha,
            "approval_reference": str(approval_reference).strip(),
            "approval_digest": str(approval_digest).strip().lower(),
            "backup": backup,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "next_action": "stop writers and perform reviewed forward recovery or exact backup restore",
        }
        evidence["evidence_fingerprint"] = _canonical_digest(evidence)
        return evidence

    try:
        after = build_explicit_recovery_plan(
            runtime_dir=runtime_dir,
            raw_dates=raw_dates,
            sha_marker_path=sha_marker_path,
            backup_dir=backup_dir,
            now=now,
        )
    except Exception as exc:  # noqa: BLE001 - mutation happened; backup must surface.
        evidence = {
            "contract_name": RESULT_CONTRACT,
            "status": "reconciliation_failed",
            "manifest_fingerprint": manifest_fingerprint,
            "deployed_sha": normalized_sha,
            "approval_reference": str(approval_reference).strip(),
            "approval_digest": str(approval_digest).strip().lower(),
            "backup": backup,
            "cycle": cycle_payload,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "next_action": "stop writers and perform reviewed readback/forward recovery or exact backup restore",
        }
        evidence["evidence_fingerprint"] = _canonical_digest(evidence)
        return evidence
    before_tables = before["_internal"]["tables"]
    after_tables = after["_internal"]["tables"]
    non_target_drift = {
        table: {
            "before": before_tables[table]["non_target_digest"],
            "after": after_tables[table]["non_target_digest"],
        }
        for table in before_tables
        if before_tables[table]["non_target_digest"]
        != after_tables[table]["non_target_digest"]
    }
    changes = {
        table: _table_changes(before_tables[table], after_tables[table])
        for table in before_tables
    }
    status = "success" if not non_target_drift else "reconciliation_failed"
    result = {
        "contract_name": RESULT_CONTRACT,
        "status": status,
        "manifest_fingerprint": manifest_fingerprint,
        "deployed_sha": normalized_sha,
        "approval_reference": str(approval_reference).strip(),
        "approval_digest": str(approval_digest).strip().lower(),
        "target_closed_dates": list(before["target_closed_dates"]),
        "backup": backup,
        "cycle": cycle_payload,
        "table_changes": changes,
        "non_target_invariants_ok": not non_target_drift,
        "non_target_drift": non_target_drift,
        "postchange_scope_digests": {
            table: contract["scope_digest"] for table, contract in after_tables.items()
        },
        "remaining_closure_states": after["prechange"]["closure_states"],
        "recovery_instruction": before["backup_and_recovery"]["recovery"],
    }
    result["evidence_fingerprint"] = _canonical_digest(result)
    return result


def _public_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "_internal"}


def main() -> None:
    args = parse_args()
    config = load_registry_upload_http_entrypoint_config()
    marker_path = Path(args.runtime_sha_marker).expanduser().resolve()
    backup_dir = (
        Path(args.backup_dir).expanduser().resolve() if args.backup_dir else None
    )

    if not args.dates:
        if args.apply or any(
            (
                args.manifest_fingerprint,
                args.deployed_sha,
                args.approval_reference,
                args.approval_digest,
            )
        ):
            raise TemporalRecoveryError("apply gates are valid only with explicit --date")
        activated_at_override = os.environ.get(
            "REGISTRY_UPLOAD_ACTIVATED_AT_OVERRIDE", ""
        ).strip()
        entrypoint = RegistryUploadHttpEntrypoint(
            runtime_dir=config.runtime_dir,
            activated_at_factory=(
                (lambda: activated_at_override) if activated_at_override else None
            ),
        )
        payload = entrypoint.run_sheet_temporal_closure_retry_cycle(
            target_dates=[],
            auto_load_visible=False,
        )
    elif args.apply:
        payload = apply_explicit_recovery(
            runtime_dir=config.runtime_dir,
            raw_dates=args.dates,
            sha_marker_path=marker_path,
            manifest_fingerprint=args.manifest_fingerprint,
            deployed_sha=args.deployed_sha,
            approval_reference=args.approval_reference,
            approval_digest=args.approval_digest,
            backup_dir=backup_dir,
        )
    else:
        if any(
            (
                args.manifest_fingerprint,
                args.deployed_sha,
                args.approval_reference,
                args.approval_digest,
            )
        ):
            raise TemporalRecoveryError("dry-run does not accept apply-only gate arguments")
        payload = _public_plan(
            build_explicit_recovery_plan(
                runtime_dir=config.runtime_dir,
                raw_dates=args.dates,
                sha_marker_path=marker_path,
                backup_dir=backup_dir,
            )
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if str(payload.get("status") or "") in {
        "apply_failed_recovery_required",
        "reconciliation_failed",
    }:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
