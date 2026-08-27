"""Recompute promo eligibility metrics after currency-safe equality semantics."""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.promo_campaign_archive import (  # noqa: E402
    PromoCampaignArchiveSyncSummary,
    load_promo_campaign_archive,
    materialize_promo_result_from_archive,
)
from packages.application.root_storage_policy import (  # noqa: E402
    admit_root_write,
    predict_sqlite_backup_bytes,
    storage_destination_root,
)


DB_FILENAME = "registry_upload_runtime.sqlite3"
PROMO_SOURCE_KEY = "promo_by_price"
ROLE_CLOSED = "accepted_closed_day_snapshot"
ROLE_CURRENT = "accepted_current_snapshot"
PROMO_METRICS = ("promo_participation", "promo_count_by_price")
TOTAL_ROWS = {
    "promo_participation": "TOTAL|total_promo_participation",
    "promo_count_by_price": "TOTAL|total_promo_count_by_price",
}


def main() -> None:
    args = _parse_args()
    runtime_dir = Path(args.runtime_dir).expanduser().resolve()
    db_path = runtime_dir / DB_FILENAME
    if not db_path.exists():
        raise SystemExit(f"runtime DB missing: {db_path}")

    date_from, date_to, all_available = _resolve_requested_dates(args, db_path)
    apply = args.command == "apply"
    if apply and not args.backup and not args.allow_no_backup_for_temp_fixture:
        raise SystemExit("apply requires --backup unless --allow-no-backup-for-temp-fixture is set")

    summary = recompute_promo_eligibility(
        runtime_dir=runtime_dir,
        date_from=date_from,
        date_to=date_to,
        all_available=all_available,
        apply=apply,
        backup=bool(args.backup),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))


def recompute_promo_eligibility(
    *,
    runtime_dir: Path,
    date_from: str | None,
    date_to: str | None,
    all_available: bool,
    apply: bool,
    backup: bool,
) -> dict[str, Any]:
    db_path = runtime_dir / DB_FILENAME
    with _connect(db_path) as conn:
        current_state = _current_state(conn)
        enabled_nm_ids = _enabled_nm_ids(conn, current_state["bundle_version"])
        dates = _select_dates(conn, date_from=date_from, date_to=date_to, all_available=all_available)
        archive_records = load_promo_campaign_archive(runtime_dir)

    sync_summary = PromoCampaignArchiveSyncSummary(
        scanned_promo_dirs=len(archive_records),
        unchanged_records=len(archive_records),
    )
    captured_at = _now_utc()
    recomputed_by_date_role: dict[tuple[str, str], dict[str, Any]] = {}
    date_summaries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    with _connect(db_path) as conn:
        for snapshot_date in dates:
            roles = _existing_roles(conn, snapshot_date)
            if not roles:
                skipped.append({"date": snapshot_date, "reason": "promo_slot_snapshot_missing"})
                continue

            result = materialize_promo_result_from_archive(
                runtime_dir=runtime_dir,
                snapshot_date=snapshot_date,
                requested_nm_ids=enabled_nm_ids,
                sync_summary=sync_summary,
                trace_run_dir=str(runtime_dir / "promo_campaign_archive"),
                detail_prefix="promo_eligibility_recompute=normalized_lte",
            )
            if getattr(result, "kind", "") != "success":
                skipped.append({"date": snapshot_date, "reason": f"materialization_{getattr(result, 'kind', 'unknown')}"})
                continue

            new_payload = _to_jsonable(result)
            for role in roles:
                old_payload = _load_slot_payload(conn, snapshot_date, role)
                role_summary = _payload_change_summary(
                    snapshot_date=snapshot_date,
                    role=role,
                    old_payload=old_payload,
                    new_payload=new_payload,
                )
                date_summaries.append(role_summary)
                if _role_summary_changed(role_summary):
                    recomputed_by_date_role[(snapshot_date, role)] = new_payload

    ready_updates = _build_ready_updates(
        db_path=db_path,
        current_bundle=current_state["bundle_version"],
        recomputed_by_date_role=recomputed_by_date_role,
    )

    backup_path = None
    if apply:
        if backup:
            backup_path = _backup_sqlite(db_path)
        _apply_updates(
            db_path=db_path,
            current_bundle=current_state["bundle_version"],
            captured_at=captured_at,
            recomputed_by_date_role=recomputed_by_date_role,
            ready_updates=ready_updates,
        )

    changed_dates = sorted(
        {
            item["date"]
            for item in date_summaries
            if item["changed_items"] or item["old_participation_sum"] != item["new_participation_sum"]
        }
        | {item["as_of_date"] for item in ready_updates if item["metric_cells_changed"] > 0}
    )
    return {
        "schema_version": "promo_metric_eligibility_recompute_v1",
        "mode": "apply" if apply else "dry-run",
        "runtime_dir": str(runtime_dir),
        "db_path": str(db_path),
        "backup_path": backup_path,
        "date_from": dates[0] if dates else date_from,
        "date_to": dates[-1] if dates else date_to,
        "all_available": all_available,
        "dates_scanned": len(dates),
        "dates_changed": len(changed_dates),
        "changed_dates": changed_dates,
        "slot_payloads_scanned": len(date_summaries),
        "slot_payloads_changed": sum(1 for item in date_summaries if item["changed_items"]),
        "ready_snapshots_scanned": len(ready_updates),
        "ready_snapshots_changed": sum(1 for item in ready_updates if item["metric_cells_changed"] > 0),
        "metric_cells_changed": sum(int(item["metric_cells_changed"]) for item in ready_updates),
        "skipped": skipped,
        "date_summaries": date_summaries,
        "ready_updates": [
            {
                "as_of_date": item["as_of_date"],
                "changed_dates": item["changed_dates"],
                "metric_cells_changed": item["metric_cells_changed"],
            }
            for item in ready_updates
        ],
        "tables": [
            "temporal_source_slot_snapshots",
            "temporal_source_snapshots",
            "sheet_vitrina_v1_ready_snapshots",
        ],
    }


def _build_ready_updates(
    *,
    db_path: Path,
    current_bundle: str,
    recomputed_by_date_role: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT as_of_date, plan_json
            FROM sheet_vitrina_v1_ready_snapshots
            WHERE bundle_version = ?
            ORDER BY as_of_date
            """,
            (current_bundle,),
        ).fetchall()
    for row in rows:
        plan = json.loads(row["plan_json"])
        original = json.loads(row["plan_json"])
        date_columns = list(plan.get("date_columns") or [])
        temporal_slots = list(plan.get("temporal_slots") or [])
        metric_cells_changed = 0
        changed_dates: list[str] = []
        for column_index, column_date in enumerate(date_columns):
            role = _role_for_column(column_index, temporal_slots)
            payload = recomputed_by_date_role.get((column_date, role))
            if payload is None and role == ROLE_CLOSED:
                payload = recomputed_by_date_role.get((column_date, ROLE_CURRENT))
            if payload is None:
                continue
            column_changed = _update_plan_column(plan, column_index=column_index, payload=payload)
            if column_changed:
                changed_dates.append(column_date)
                metric_cells_changed += column_changed
        if metric_cells_changed:
            updates.append(
                {
                    "as_of_date": row["as_of_date"],
                    "changed_dates": changed_dates,
                    "metric_cells_changed": metric_cells_changed,
                    "plan_json": json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                    "old_plan_json": json.dumps(original, ensure_ascii=False, separators=(",", ":")),
                }
            )
        else:
            updates.append(
                {
                    "as_of_date": row["as_of_date"],
                    "changed_dates": [],
                    "metric_cells_changed": 0,
                }
            )
    return updates


def _update_plan_column(plan: dict[str, Any], *, column_index: int, payload: dict[str, Any]) -> int:
    data_sheet = _sheet(plan, "DATA_VITRINA")
    status_sheet = _sheet(plan, "STATUS")
    if data_sheet is None:
        return 0
    data_rows = {str(row[1]): row for row in data_sheet.get("rows") or [] if len(row) > 1}
    item_values = {
        int(item["nm_id"]): item
        for item in payload.get("items") or []
        if isinstance(item, dict) and item.get("nm_id") is not None
    }
    value_index = 2 + column_index
    changed = 0
    totals = {metric: 0.0 for metric in PROMO_METRICS}
    for nm_id, item in item_values.items():
        for metric in PROMO_METRICS:
            row = data_rows.get(f"SKU:{nm_id}|{metric}")
            if row is None or value_index >= len(row):
                continue
            new_value = float(item.get(metric) or 0.0)
            totals[metric] += new_value
            if _numeric(row[value_index]) != new_value:
                row[value_index] = new_value
                changed += 1
    for metric, row_id in TOTAL_ROWS.items():
        row = data_rows.get(row_id)
        if row is None or value_index >= len(row):
            continue
        new_value = round(totals[metric], 6)
        if _numeric(row[value_index]) != new_value:
            row[value_index] = new_value
            changed += 1

    slot_key = _slot_key_for_column(plan, column_index)
    if status_sheet is not None and slot_key:
        status_rows = {str(row[0]): row for row in status_sheet.get("rows") or [] if row}
        status_row = status_rows.get(f"{PROMO_SOURCE_KEY}[{slot_key}]")
        if status_row is not None:
            _ensure_row_len(status_row, 11)
            status_row[1] = payload.get("kind", "success")
            status_row[2] = payload.get("snapshot_date", "")
            status_row[7] = payload.get("requested_count", len(item_values))
            status_row[8] = payload.get("covered_count", len(item_values))
            status_row[10] = (
                str(payload.get("detail") or "")
                + "; resolution_rule=promo_eligibility_recompute_normalized_lte"
            )
    return changed


def _apply_updates(
    *,
    db_path: Path,
    current_bundle: str,
    captured_at: str,
    recomputed_by_date_role: dict[tuple[str, str], dict[str, Any]],
    ready_updates: list[dict[str, Any]],
) -> None:
    with _connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            for (snapshot_date, role), payload in sorted(recomputed_by_date_role.items()):
                payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                conn.execute(
                    """
                    UPDATE temporal_source_slot_snapshots
                    SET captured_at = ?, payload_json = ?
                    WHERE source_key = ? AND snapshot_date = ? AND snapshot_role = ?
                    """,
                    (captured_at, payload_json, PROMO_SOURCE_KEY, snapshot_date, role),
                )
                if _exact_snapshot_exists(conn, snapshot_date):
                    conn.execute(
                        """
                        UPDATE temporal_source_snapshots
                        SET captured_at = ?, payload_json = ?
                        WHERE source_key = ? AND snapshot_date = ?
                        """,
                        (captured_at, payload_json, PROMO_SOURCE_KEY, snapshot_date),
                    )
            for update in ready_updates:
                if not update.get("metric_cells_changed"):
                    continue
                conn.execute(
                    """
                    UPDATE sheet_vitrina_v1_ready_snapshots
                    SET plan_json = ?
                    WHERE bundle_version = ? AND as_of_date = ?
                    """,
                    (update["plan_json"], current_bundle, update["as_of_date"]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _payload_change_summary(
    *,
    snapshot_date: str,
    role: str,
    old_payload: dict[str, Any] | None,
    new_payload: dict[str, Any],
) -> dict[str, Any]:
    old_items = _payload_items(old_payload)
    new_items = _payload_items(new_payload)
    examples = []
    for nm_id in sorted(set(old_items) | set(new_items)):
        old = old_items.get(nm_id, {})
        new = new_items.get(nm_id, {})
        old_pair = (
            float(old.get("promo_participation") or 0.0),
            float(old.get("promo_count_by_price") or 0.0),
        )
        new_pair = (
            float(new.get("promo_participation") or 0.0),
            float(new.get("promo_count_by_price") or 0.0),
        )
        if old_pair != new_pair:
            examples.append(
                {
                    "nm_id": nm_id,
                    "old_participation": old_pair[0],
                    "new_participation": new_pair[0],
                    "old_count_by_price": old_pair[1],
                    "new_count_by_price": new_pair[1],
                    "entry_price_best": float(new.get("promo_entry_price_best") or old.get("promo_entry_price_best") or 0.0),
                }
            )
    return {
        "date": snapshot_date,
        "role": role,
        "old_participation_sum": _sum_metric(old_items, "promo_participation"),
        "new_participation_sum": _sum_metric(new_items, "promo_participation"),
        "old_count_by_price_sum": _sum_metric(old_items, "promo_count_by_price"),
        "new_count_by_price_sum": _sum_metric(new_items, "promo_count_by_price"),
        "changed_items": len(examples),
        "examples": examples[:8],
    }


def _role_summary_changed(summary: dict[str, Any]) -> bool:
    return bool(summary["changed_items"]) or (
        summary["old_participation_sum"],
        summary["old_count_by_price_sum"],
    ) != (
        summary["new_participation_sum"],
        summary["new_count_by_price_sum"],
    )


def _payload_items(payload: dict[str, Any] | None) -> dict[int, dict[str, Any]]:
    if not isinstance(payload, dict):
        return {}
    return {
        int(item["nm_id"]): item
        for item in payload.get("items") or []
        if isinstance(item, dict) and item.get("nm_id") is not None
    }


def _sum_metric(items: dict[int, dict[str, Any]], metric: str) -> float:
    return round(sum(float(item.get(metric) or 0.0) for item in items.values()), 6)


def _sheet(plan: dict[str, Any], sheet_name: str) -> dict[str, Any] | None:
    for sheet in plan.get("sheets") or []:
        if sheet.get("sheet_name") == sheet_name or sheet.get("name") == sheet_name:
            return sheet
    return None


def _role_for_column(column_index: int, temporal_slots: list[dict[str, Any]]) -> str:
    slot = temporal_slots[column_index] if column_index < len(temporal_slots) else {}
    return ROLE_CURRENT if slot.get("slot_key") == "today_current" else ROLE_CLOSED


def _slot_key_for_column(plan: dict[str, Any], column_index: int) -> str:
    slots = list(plan.get("temporal_slots") or [])
    if column_index >= len(slots):
        return ""
    return str(slots[column_index].get("slot_key") or "")


def _current_state(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute(
        "SELECT bundle_version, activated_at FROM registry_upload_current_state WHERE slot = 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("registry_upload_current_state missing")
    return {"bundle_version": row["bundle_version"], "activated_at": row["activated_at"]}


def _enabled_nm_ids(conn: sqlite3.Connection, bundle_version: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT nm_id
        FROM registry_upload_config_v2
        WHERE bundle_version = ? AND enabled = 1
        ORDER BY nm_id
        """,
        (bundle_version,),
    ).fetchall()
    return [int(row["nm_id"]) for row in rows]


def _select_dates(
    conn: sqlite3.Connection,
    *,
    date_from: str | None,
    date_to: str | None,
    all_available: bool,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT snapshot_date
        FROM temporal_source_slot_snapshots
        WHERE source_key = ?
        ORDER BY snapshot_date
        """,
        (PROMO_SOURCE_KEY,),
    ).fetchall()
    dates = [str(row["snapshot_date"]) for row in rows]
    if all_available:
        return dates
    if date_from is None or date_to is None:
        raise RuntimeError("date range required unless all_available=true")
    return [date for date in dates if date_from <= date <= date_to]


def _existing_roles(conn: sqlite3.Connection, snapshot_date: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT snapshot_role
        FROM temporal_source_slot_snapshots
        WHERE source_key = ? AND snapshot_date = ?
        ORDER BY snapshot_role
        """,
        (PROMO_SOURCE_KEY, snapshot_date),
    ).fetchall()
    return [str(row["snapshot_role"]) for row in rows if row["snapshot_role"] in {ROLE_CLOSED, ROLE_CURRENT}]


def _load_slot_payload(conn: sqlite3.Connection, snapshot_date: str, role: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT payload_json
        FROM temporal_source_slot_snapshots
        WHERE source_key = ? AND snapshot_date = ? AND snapshot_role = ?
        """,
        (PROMO_SOURCE_KEY, snapshot_date, role),
    ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def _exact_snapshot_exists(conn: sqlite3.Connection, snapshot_date: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM temporal_source_snapshots
        WHERE source_key = ? AND snapshot_date = ?
        LIMIT 1
        """,
        (PROMO_SOURCE_KEY, snapshot_date),
    ).fetchone()
    return row is not None


def _to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def _backup_sqlite(db_path: Path) -> str:
    canonical_runtime = Path("/opt/wb-core-runtime/state")
    backup_dir = (
        storage_destination_root("promo_metric_eligibility_recompute")
        if db_path.resolve().is_relative_to(canonical_runtime)
        else db_path.parent / "backups" / "promo_metric_eligibility_recompute"
    )
    backup_path = backup_dir / f"{db_path.stem}__{_now_stamp()}.sqlite3"
    admit_root_write(
        owner="promo_metric_eligibility_recompute",
        destination=backup_path,
        predicted_output_bytes=predict_sqlite_backup_bytes(db_path),
    )
    backup_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as source, sqlite3.connect(str(backup_path)) as target:
        source.backup(target)
    return str(backup_path)


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _numeric(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _ensure_row_len(row: list[Any], size: int) -> None:
    while len(row) < size:
        row.append("")


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_requested_dates(args: argparse.Namespace, db_path: Path) -> tuple[str | None, str | None, bool]:
    if args.all_available:
        with _connect(db_path) as conn:
            dates = _select_dates(conn, date_from=None, date_to=None, all_available=True)
        if dates:
            print(f"resolved_all_available_date_range={dates[0]}..{dates[-1]}", file=sys.stderr)
        return None, None, True
    if not args.date_from or not args.date_to:
        raise SystemExit("--date-from and --date-to are required unless --all-available is used")
    if args.date_to < args.date_from:
        raise SystemExit("--date-to must be >= --date-from")
    return args.date_from, args.date_to, False


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("dry-run", "apply"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--runtime-dir", required=True)
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--all-available", action="store_true")
        group.add_argument("--date-from")
        sub.add_argument("--date-to")
        sub.add_argument("--backup", action="store_true")
        sub.add_argument("--allow-no-backup-for-temp-fixture", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    main()
