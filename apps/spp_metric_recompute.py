"""Guarded SPP current-visible recompute for runtime DB snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.adapters.spp_block import (  # noqa: E402
    SellerPortalDiscountOnSiteSppSource,
    _current_business_date,
    _discount_on_site_goods_to_spp_items,
)
from packages.contracts.spp_block import SppRequest  # noqa: E402

DB_FILENAME = "registry_upload_runtime.sqlite3"
ACCEPTED_CURRENT_ROLE = "accepted_current_snapshot"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "apply"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--runtime-dir", required=True)
        sub.add_argument("--date-from")
        sub.add_argument("--date-to")
        sub.add_argument("--current-date", default=_current_business_date())
        sub.add_argument("--storage-state-path")
        sub.add_argument("--fixture-goods-json")
        sub.add_argument("--backup", action="store_true")
    args = parser.parse_args()

    summary = run_recompute(
        command=args.command,
        runtime_dir=Path(args.runtime_dir),
        date_from=args.date_from,
        date_to=args.date_to,
        current_date=args.current_date,
        storage_state_path=args.storage_state_path,
        fixture_goods_json=Path(args.fixture_goods_json) if args.fixture_goods_json else None,
        backup=args.backup,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    if summary["status"] == "blocked":
        raise SystemExit(2)


def run_recompute(
    *,
    command: str,
    runtime_dir: Path,
    date_from: str | None,
    date_to: str | None,
    current_date: str,
    storage_state_path: str | None,
    fixture_goods_json: Path | None,
    backup: bool,
) -> dict[str, Any]:
    if command not in {"dry-run", "apply"}:
        raise ValueError("command must be dry-run or apply")
    if command == "apply" and not backup:
        return {"status": "blocked", "blocker": "apply requires --backup"}

    db_path = runtime_dir / DB_FILENAME
    if not db_path.exists():
        return {"status": "blocked", "blocker": f"runtime DB missing: {db_path}"}

    date_range = _resolve_date_range(date_from=date_from, date_to=date_to, current_date=current_date)
    skipped_dates = [
        {
            "date": item,
            "reason": "Seller Portal discountOnSite is current-only; historical SPP cannot be reconstructed",
        }
        for item in date_range
        if item != current_date
    ]

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        current_bundle = _current_bundle(conn)
        enabled_nm_ids = _enabled_nm_ids(conn, current_bundle)
        goods = _load_fixture_goods(fixture_goods_json) if fixture_goods_json else _fetch_seller_portal_goods(
            enabled_nm_ids,
            storage_state_path=storage_state_path,
            current_date=current_date,
        )
        new_items = _discount_on_site_goods_to_spp_items(goods, enabled_nm_ids)
        new_by_nm = {int(item["nmId"]): float(item["spp_avg"]) for item in new_items}
        if not new_by_nm:
            return {
                "status": "blocked",
                "blocker": "Seller Portal current SPP source returned no usable discountOnSite rows",
                "date_range": date_range,
                "skipped_dates": skipped_dates,
            }

        captured_at = _utc_now()
        accepted_diff = _build_accepted_diff(conn, current_date=current_date, new_items=new_items)
        ready_diff = _build_ready_diff(
            conn,
            current_bundle=current_bundle,
            current_date=current_date,
            new_by_nm=new_by_nm,
            captured_at=captured_at,
        )

        dry_run_summary: dict[str, Any] = {
            "status": "success",
            "mode": command,
            "runtime_db": str(db_path),
            "date_range": date_range,
            "current_date": current_date,
            "dates_scanned": len(date_range),
            "dates_changed": 1 if accepted_diff["changed_count"] or ready_diff["changed_count"] else 0,
            "skipped_dates": skipped_dates,
            "source": "Seller Portal discounts-prices discountOnSite",
            "sku_rows_scanned": len(enabled_nm_ids),
            "sku_rows_changed": accepted_diff["changed_count"],
            "metric_cells_changed": ready_diff["changed_count"],
            "accepted_snapshot_changes": accepted_diff,
            "ready_snapshot_changes": ready_diff,
            "backup_path": None,
        }
        ready_plan = ready_diff["new_plan"]
        dry_run_summary["ready_snapshot_changes"].pop("new_plan", None)
        if command == "dry-run":
            return dry_run_summary

        backup_path = _backup_db(db_path)
        _apply_current_spp(
            conn,
            current_date=current_date,
            new_items=new_items,
            ready_plan=ready_plan,
            ready_as_of_date=ready_diff["ready_as_of_date"],
            captured_at=captured_at,
        )
        conn.commit()
        dry_run_summary["backup_path"] = str(backup_path)
        dry_run_summary["applied_at"] = captured_at
        return dry_run_summary
    finally:
        conn.close()


def _resolve_date_range(*, date_from: str | None, date_to: str | None, current_date: str) -> list[str]:
    if not date_from and not date_to:
        return [current_date]
    start = datetime.fromisoformat(date_from or current_date).date()
    end = datetime.fromisoformat(date_to or current_date).date()
    if end < start:
        raise ValueError("date_to must be >= date_from")
    result: list[str] = []
    day = start
    while day <= end:
        result.append(day.isoformat())
        day = day.fromordinal(day.toordinal() + 1)
    return result


def _current_bundle(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT bundle_version FROM registry_upload_current_state WHERE slot = 1"
    ).fetchone()
    if row is None:
        raise ValueError("registry_upload_current_state missing")
    return str(row["bundle_version"])


def _enabled_nm_ids(conn: sqlite3.Connection, bundle_version: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT nm_id
        FROM registry_upload_config_v2
        WHERE bundle_version = ? AND enabled = 1
        ORDER BY display_order, nm_id
        """,
        (bundle_version,),
    ).fetchall()
    return [int(row["nm_id"]) for row in rows]


def _load_fixture_goods(path: Path | None) -> list[Mapping[str, Any]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("--fixture-goods-json must contain a JSON array")
    return [item for item in payload if isinstance(item, Mapping)]


def _fetch_seller_portal_goods(
    nm_ids: list[int],
    *,
    storage_state_path: str | None,
    current_date: str,
) -> list[Mapping[str, Any]]:
    source = SellerPortalDiscountOnSiteSppSource(
        storage_state_path=storage_state_path,
        business_date_factory=lambda: current_date,
    )
    payload = source.fetch(
        SppRequest(snapshot_type="spp", snapshot_date=current_date, nm_ids=nm_ids)
    )
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return []
    # Source.fetch already returns normalized SPP items. Rebuild goods-like rows for one shared diff path.
    return [
        {"nmID": int(item["nmId"]), "discountOnSite": float(item["spp_avg"])}
        for item in data.get("items", [])
        if isinstance(item, Mapping)
    ]


def _build_accepted_diff(
    conn: sqlite3.Connection,
    *,
    current_date: str,
    new_items: list[Mapping[str, Any]],
) -> dict[str, Any]:
    old_payload, captured_at = _load_accepted_payload(conn, current_date)
    old_by_nm = {
        int(item["nm_id"] if "nm_id" in item else item["nmId"]): float(item["spp"])
        for item in old_payload.get("items", [])
        if isinstance(item, Mapping) and ("spp" in item) and ("nm_id" in item or "nmId" in item)
    }
    new_by_nm = {int(item["nmId"]): float(item["spp_avg"]) for item in new_items}
    examples = _changed_examples(old_by_nm, new_by_nm)
    return {
        "old_captured_at": captured_at,
        "old_count": len(old_by_nm),
        "new_count": len(new_by_nm),
        "changed_count": len([nm for nm in set(old_by_nm) | set(new_by_nm) if old_by_nm.get(nm) != new_by_nm.get(nm)]),
        "examples": examples,
    }


def _load_accepted_payload(conn: sqlite3.Connection, current_date: str) -> tuple[dict[str, Any], str | None]:
    row = conn.execute(
        """
        SELECT captured_at, payload_json
        FROM temporal_source_slot_snapshots
        WHERE source_key = 'spp'
          AND snapshot_date = ?
          AND snapshot_role = ?
        """,
        (current_date, ACCEPTED_CURRENT_ROLE),
    ).fetchone()
    if row is None:
        return {"kind": "empty", "snapshot_date": current_date, "items": []}, None
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        return {"kind": "empty", "snapshot_date": current_date, "items": []}, row["captured_at"]
    return payload, row["captured_at"]


def _build_ready_diff(
    conn: sqlite3.Connection,
    *,
    current_bundle: str,
    current_date: str,
    new_by_nm: dict[int, float],
    captured_at: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT as_of_date, refreshed_at, plan_json
        FROM sheet_vitrina_v1_ready_snapshots
        WHERE bundle_version = ?
        ORDER BY refreshed_at DESC, as_of_date DESC
        """,
        (current_bundle,),
    ).fetchall()
    ready_row = None
    for candidate in row:
        payload = json.loads(candidate["plan_json"])
        data = _data_sheet(payload)
        if current_date in data["header"]:
            ready_row = candidate
            plan = payload
            break
    if ready_row is None:
        raise ValueError(f"ready snapshot with date column {current_date} not found")

    data = _data_sheet(plan)
    col_idx = data["header"].index(current_date)
    old_by_nm: dict[int, float | None] = {}
    changed_count = 0
    for line in data["rows"]:
        key = str(line[1])
        if key == "TOTAL|avg_spp":
            old_by_nm[0] = _coerce_float(line[col_idx])
            continue
        if key.startswith("SKU:") and key.endswith("|spp"):
            nm_id = int(key[4:-4])
            old_by_nm[nm_id] = _coerce_float(line[col_idx])
            if nm_id in new_by_nm and old_by_nm[nm_id] != _round_ready(new_by_nm[nm_id]):
                changed_count += 1
                line[col_idx] = _round_ready(new_by_nm[nm_id])

    new_avg = sum(new_by_nm.values()) / len(new_by_nm) if new_by_nm else None
    for line in data["rows"]:
        if str(line[1]) == "TOTAL|avg_spp":
            if _coerce_float(line[col_idx]) != _round_ready(new_avg):
                changed_count += 1
                line[col_idx] = _round_ready(new_avg)
            break

    status = _status_sheet(plan)
    for line in status["rows"]:
        if str(line[0]) == "spp[today_current]":
            line[1] = "success"
            line[2] = current_date
            line[3] = current_date
            line[7] = len(new_by_nm)
            line[8] = len(new_by_nm)
            line[9] = ""
            line[10] = (
                "resolution_rule=seller_portal_discount_on_site_current; "
                f"accepted_at={captured_at}"
            )
            break

    plan["snapshot_id"] = f"{current_date}__spp_discount_on_site_recompute__{captured_at}"
    metadata = plan.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["spp_recompute"] = {
            "source": "seller_portal_discount_on_site",
            "current_date": current_date,
            "applied_at": captured_at,
        }

    examples = _changed_examples(
        {nm: value for nm, value in old_by_nm.items() if nm != 0 and value is not None},
        {nm: _round_ready(value) for nm, value in new_by_nm.items()},
    )
    return {
        "ready_as_of_date": ready_row["as_of_date"],
        "old_refreshed_at": ready_row["refreshed_at"],
        "changed_count": changed_count,
        "new_avg_spp": _round_ready(new_avg),
        "examples": examples,
        "new_plan": plan,
    }


def _data_sheet(plan: Mapping[str, Any]) -> dict[str, Any]:
    return next(sheet for sheet in plan["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")


def _status_sheet(plan: Mapping[str, Any]) -> dict[str, Any]:
    return next(sheet for sheet in plan["sheets"] if sheet["sheet_name"] == "STATUS")


def _changed_examples(old_by_nm: dict[int, float], new_by_nm: dict[int, float], limit: int = 12) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for nm_id in sorted(set(old_by_nm) | set(new_by_nm)):
        old = old_by_nm.get(nm_id)
        new = new_by_nm.get(nm_id)
        if old == new:
            continue
        examples.append({"nm_id": nm_id, "old": old, "new": new})
        if len(examples) >= limit:
            break
    return examples


def _round_ready(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _coerce_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def _backup_db(db_path: Path) -> Path:
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"spp_recompute__{_utc_now().replace(':', '').replace('-', '')}.sqlite3"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def _apply_current_spp(
    conn: sqlite3.Connection,
    *,
    current_date: str,
    new_items: list[Mapping[str, Any]],
    ready_plan: Mapping[str, Any],
    ready_as_of_date: str,
    captured_at: str,
) -> None:
    payload = {
        "kind": "success",
        "snapshot_date": current_date,
        "count": len(new_items),
        "items": [
            {"nm_id": int(item["nmId"]), "spp": float(item["spp_avg"])}
            for item in new_items
        ],
    }
    conn.execute(
        """
        INSERT INTO temporal_source_slot_snapshots(
            source_key, snapshot_date, snapshot_role, captured_at, payload_json
        )
        VALUES('spp', ?, ?, ?, ?)
        ON CONFLICT(source_key, snapshot_date, snapshot_role) DO UPDATE SET
            captured_at = excluded.captured_at,
            payload_json = excluded.payload_json
        """,
        (current_date, ACCEPTED_CURRENT_ROLE, captured_at, json.dumps(payload, ensure_ascii=False)),
    )
    conn.execute(
        """
        UPDATE sheet_vitrina_v1_ready_snapshots
        SET snapshot_id = ?, refreshed_at = ?, plan_json = ?
        WHERE as_of_date = ?
        """,
        (
            ready_plan["snapshot_id"],
            captured_at,
            json.dumps(ready_plan, ensure_ascii=False),
            ready_as_of_date,
        ),
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    main()
