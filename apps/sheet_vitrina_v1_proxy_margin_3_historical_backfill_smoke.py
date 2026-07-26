"""Comprehensive smoke for guarded historical proxy margin 3 backfill."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (  # noqa: E402
    BackfillExecutionError,
    run_backfill,
)
from packages.application.registry_upload_db_backed_runtime import (  # noqa: E402
    DB_FILENAME,
    RegistryUploadDbBackedRuntime,
)
from packages.application.sheet_vitrina_v1_proxy_margin_3_historical_backfill import (  # noqa: E402
    ReadySnapshotInput,
    transform_ready_snapshot,
)
from packages.application.sheet_vitrina_v1_web_vitrina import (  # noqa: E402
    SheetVitrinaV1WebVitrinaBlock,
)
from packages.contracts.sheet_vitrina_v1 import (  # noqa: E402
    SheetVitrinaV1Envelope,
    SheetVitrinaV1TemporalSlot,
    SheetVitrinaWriteTarget,
)


BUNDLE_FIXTURE = (
    ROOT / "artifacts" / "registry_upload_http_entrypoint" / "input" / "registry_upload_bundle__fixture.json"
)
STATUS_HEADER = [
    "source_key",
    "kind",
    "freshness",
    "snapshot_date",
    "date",
    "date_from",
    "date_to",
    "requested_count",
    "covered_count",
    "missing_nm_ids",
    "note",
]


def main() -> None:
    with TemporaryDirectory(prefix="proxy-margin-3-historical-backfill-") as tempdir:
        runtime_dir = Path(tempdir) / "runtime"
        runtime = RegistryUploadDbBackedRuntime(runtime_dir=runtime_dir)
        base_bundle = json.loads(BUNDLE_FIXTURE.read_text(encoding="utf-8"))

        generation_one = _bundle_generation(
            base_bundle,
            bundle_version="proxy_margin_3_backfill_generation_1",
            uploaded_at="2026-06-28T08:00:00Z",
        )
        accepted = runtime.ingest_bundle(generation_one, activated_at="2026-06-28T08:00:00Z")
        _assert(accepted.status == "accepted", "first bundle generation must be accepted")
        state_one = runtime.load_current_state()
        enabled_one = [item for item in state_one.config_v2 if item.enabled]
        first_nm = int(enabled_one[0].nm_id)
        second_nm = int(enabled_one[1].nm_id)
        third_nm = int(enabled_one[2].nm_id)

        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state_one,
            refreshed_at="2026-06-28T09:00:00Z",
            plan=_plan_without_operands(
                as_of_date="2026-06-28",
                date_columns=["2026-06-28"],
                nm_ids=[first_nm, second_nm],
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state_one,
            refreshed_at="2026-06-29T09:00:00Z",
            plan=_pre_boundary_fallback_plan(
                as_of_date="2026-06-29",
                nm_ids=[first_nm, second_nm],
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state_one,
            refreshed_at="2026-06-30T09:00:00Z",
            plan=_ratio_plan(
                as_of_date="2026-06-30",
                date_columns=["2026-06-30", "2026-07-01"],
                nm_ids=[first_nm, second_nm],
                values={
                    "2026-06-30": {
                        first_nm: (10.0, 20.0),
                        second_nm: (5.0, 0.0),
                        "TOTAL": (15.0, 20.0),
                    },
                    "2026-07-01": {
                        first_nm: (None, None),
                        second_nm: (6.0, 30.0),
                        "TOTAL": (6.0, 30.0),
                    },
                },
                existing_targets={
                    f"SKU:{first_nm}|proxy_margin_3_pct": ["", ""],
                    f"SKU:{second_nm}|proxy_margin_3_pct": [0.0, 0.2],
                    "TOTAL|proxy_margin_3_pct_total": [0.75, 0.2],
                },
            ),
        )

        generation_two = _bundle_generation(
            base_bundle,
            bundle_version="proxy_margin_3_backfill_generation_2",
            uploaded_at="2026-07-01T08:00:00Z",
        )
        accepted = runtime.ingest_bundle(generation_two, activated_at="2026-07-01T08:00:00Z")
        _assert(accepted.status == "accepted", "second bundle generation must be accepted")
        state_two = runtime.load_current_state()
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state_two,
            refreshed_at="2026-07-01T09:00:00Z",
            plan=_ratio_plan(
                as_of_date="2026-07-01",
                date_columns=["2026-07-01"],
                nm_ids=[first_nm, second_nm, third_nm],
                values={
                    "2026-07-01": {
                        first_nm: (20.0, 100.0),
                        second_nm: (90.0, 300.0),
                        third_nm: (5.0, 100.0),
                        "TOTAL": (115.0, 500.0),
                    }
                },
                existing_targets={},
            ),
        )
        runtime.save_sheet_vitrina_ready_snapshot(
            current_state=state_two,
            refreshed_at="2026-07-02T09:00:00Z",
            plan=_ratio_plan(
                as_of_date="2026-07-02",
                date_columns=["2026-07-02", "2026-07-03"],
                nm_ids=[first_nm, second_nm, third_nm],
                values={
                    "2026-07-02": {
                        first_nm: (30.0, 100.0),
                        second_nm: (80.0, 400.0),
                        third_nm: (0.0, 0.0),
                        "TOTAL": (110.0, 500.0),
                    },
                    "2026-07-03": {
                        first_nm: (40.0, 100.0),
                        second_nm: (90.0, 300.0),
                        third_nm: (10.0, 100.0),
                        "TOTAL": (140.0, 500.0),
                    },
                },
                existing_targets={
                    f"SKU:{first_nm}|proxy_margin_3_pct": [0.3, 0.4],
                    f"SKU:{second_nm}|proxy_margin_3_pct": [0.2, 0.3],
                    f"SKU:{third_nm}|proxy_margin_3_pct": [0.0, 0.1],
                    "TOTAL|proxy_margin_3_pct_total": [0.22, 0.28],
                },
            ),
        )

        db_path = runtime_dir / DB_FILENAME
        _pure_conflict_and_non_finite_guards(db_path)
        original_db_hash = _file_sha256(db_path)
        original_identity = _snapshot_identity_rows(db_path)

        dry_run = run_backfill(
            runtime_dir=runtime_dir,
            all_available=True,
            apply=False,
            expected_fingerprint=None,
        )
        _assert(dry_run["status"] == "success", f"dry-run must pass: {dry_run.get('blockers')}")
        _assert(_file_sha256(db_path) == original_db_hash, "dry-run must not write SQLite")
        _assert(dry_run["bundle_count"] == 2, "dry-run must scan multiple bundle generations")
        _assert(dry_run["first_available_date"] == "2026-06-28", "earliest date must be discovered")
        _assert(dry_run["last_available_date"] == "2026-07-03", "latest tail date must be discovered")
        _assert(dry_run["unique_date_columns"] == 6, "multi-date tail must be included")
        _assert(dry_run["pre_boundary_margin2_fallbacks"] >= 3, "pre-boundary Proxy 2 preservation count mismatch")
        _assert(dry_run["zero_denominator_cells"] >= 1, "zero denominator must be counted")
        _assert(dry_run["blank_operand_cells"] >= 3, "missing operands must stay explicit")
        _assert(dry_run["non_target_preserved"], "dry-run non-target deep digest must match")
        fingerprint = str(dry_run["expected_fingerprint"])

        try:
            run_backfill(
                runtime_dir=runtime_dir,
                all_available=True,
                apply=True,
                expected_fingerprint=fingerprint,
            )
        except BackfillExecutionError as exc:
            _assert(
                "mutation entrypoint is disabled" in str(exc),
                "legacy apply must fail closed before backup or mutation",
            )
        else:
            raise AssertionError("disabled legacy apply unexpectedly ran")
        _assert(
            _snapshot_identity_rows(db_path) == original_identity,
            "disabled apply must preserve snapshot identity",
        )
        _assert(
            _file_sha256(db_path) == original_db_hash,
            "disabled apply must preserve DB bytes",
        )
        backup_root = runtime_dir / "backups"
        _assert(
            not backup_root.exists(),
            "disabled legacy apply must create zero recovery artifacts",
        )

        print("proxy_margin_3_backfill_dry_run: ok ->", dry_run["changed_snapshots"], "snapshots")
        print("proxy_margin_3_backfill_legacy_apply_disabled: ok ->", fingerprint[:12])


def _pure_conflict_and_non_finite_guards(db_path: Path) -> None:
    record = _first_snapshot_record(db_path, as_of_date="2026-06-30")
    plan = json.loads(record.plan_json)
    data = next(sheet for sheet in plan["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
    target = next(row for row in data["rows"] if str(row[1]).endswith("|proxy_margin_3_pct"))
    target[2] = 0.123456
    repaired = transform_ready_snapshot(
        ReadySnapshotInput(**{**record.__dict__, "plan_json": json.dumps(plan, ensure_ascii=False)})
    )
    _assert(not repaired.blockers and repaired.changed, "nonblank mismatching target value must be repaired")

    nan_plan = json.loads(record.plan_json)
    nan_data = next(sheet for sheet in nan_plan["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
    profit = next(row for row in nan_data["rows"] if str(row[1]).endswith("|proxy_profit_3_rub"))
    profit[2] = float("nan")
    non_finite = transform_ready_snapshot(
        ReadySnapshotInput(**{**record.__dict__, "plan_json": json.dumps(nan_plan, ensure_ascii=False)})
    )
    _assert(non_finite.blockers, "NaN operand must block")


def _verify_full_period_web_contract(
    *,
    runtime: RegistryUploadDbBackedRuntime,
    first_nm: int,
    second_nm: int,
    third_nm: int,
) -> None:
    payload = SheetVitrinaV1WebVitrinaBlock(
        runtime=runtime,
        now_factory=lambda: datetime(2026, 7, 3, 15, 0, tzinfo=timezone.utc),
    ).build(
        page_route="/sheet-vitrina-v1/vitrina",
        read_route="/v1/sheet-vitrina-v1/web-vitrina",
        date_from="2026-06-28",
        date_to="2026-07-03",
    )
    rows = {row.row_id: row for row in payload.rows}
    total = rows["TOTAL|proxy_margin_3_pct_total"]
    first = rows[f"SKU:{first_nm}|proxy_margin_3_pct"]
    second = rows[f"SKU:{second_nm}|proxy_margin_3_pct"]
    third = rows[f"SKU:{third_nm}|proxy_margin_3_pct"]
    _assert(total.format == "percent", "TOTAL margin 3 web row must use percent format")
    _assert(first.format == "percent", "SKU margin 3 web row must use percent format")
    _assert(total.values_by_date["2026-06-28"] in ("", None), "missing operands must stay blank")
    _assert(first.values_by_date["2026-06-29"] == 0.5, "pre-boundary SKU must copy margin2")
    _assert(second.values_by_date["2026-06-29"] == 0.1, "second pre-boundary SKU fallback mismatch")
    _assert(total.values_by_date["2026-06-29"] == 0.18, "pre-boundary TOTAL fallback mismatch")
    _assert(first.values_by_date["2026-06-30"] == 0.5, "SKU ratio mismatch")
    _assert(second.values_by_date["2026-06-30"] == 0.0, "zero denominator must produce 0.0")
    _assert(total.values_by_date["2026-06-30"] == 0.75, "TOTAL must be ratio of aggregates")
    _assert(total.values_by_date["2026-07-01"] == 0.252747, "post-boundary expected-buyout denominator mismatch")
    _assert(third.values_by_date["2026-07-02"] in ("", None), "current-universe zero denominator must be blank")


def _verify_order_and_timestamp_map(db_path: Path, *, first_nm: int) -> None:
    record = _first_snapshot_record(db_path, as_of_date="2026-06-29")
    plan = json.loads(record.plan_json)
    data = next(sheet for sheet in plan["sheets"] if sheet["sheet_name"] == "DATA_VITRINA")
    row_ids = [str(row[1]) for row in data["rows"]]
    target_total = "TOTAL|proxy_margin_3_pct_total"
    target_sku = f"SKU:{first_nm}|proxy_margin_3_pct"
    anchor_indices = [
        row_ids.index("TOTAL|proxy_margin_2_pct_total"),
        row_ids.index(f"SKU:{first_nm}|proxy_margin_2_pct"),
    ]
    _assert(row_ids.index(target_total) == max(anchor_indices) + 2, "target block must follow full margin2 fallback block")
    _assert(row_ids.index(target_sku) == row_ids.index(target_total) + 1, "SKU target must follow TOTAL target")
    timestamp_map = plan["metadata"]["row_last_updated_at_by_row_id"]
    _assert(target_total in timestamp_map and target_sku in timestamp_map, "inserted rows need timestamp-map entries")


def _plan_without_operands(*, as_of_date: str, date_columns: list[str], nm_ids: list[int]) -> SheetVitrinaV1Envelope:
    rows: list[list[Any]] = [["Итого: Сумма заказов всего", "TOTAL|total_orderSum", *([""] * len(date_columns))]]
    for nm_id in nm_ids:
        rows.append([f"SKU {nm_id}: Сумма заказов", f"SKU:{nm_id}|orderSum", *([""] * len(date_columns))])
        rows.append([f"SKU {nm_id}: Прибыль прокси, ₽", f"SKU:{nm_id}|proxy_profit_rub", *([""] * len(date_columns))])
    rows.insert(1, ["Итого: Прибыль прокси всего, ₽", "TOTAL|total_proxy_profit_rub", *([""] * len(date_columns))])
    return _plan(as_of_date=as_of_date, date_columns=date_columns, rows=rows, timestamp_map=False)


def _pre_boundary_fallback_plan(*, as_of_date: str, nm_ids: list[int]) -> SheetVitrinaV1Envelope:
    first_nm, second_nm = nm_ids
    rows = [
        ["Итого: Прокси маржинальность 2 всего, %", "TOTAL|proxy_margin_2_pct_total", 0.18],
        [f"SKU {first_nm}: Прокси маржинальность 2, %", f"SKU:{first_nm}|proxy_margin_2_pct", 0.5],
        [f"SKU {second_nm}: Прокси маржинальность 2, %", f"SKU:{second_nm}|proxy_margin_2_pct", 0.1],
        [f"SKU {first_nm}: Прокси маржинальность 3, %", f"SKU:{first_nm}|proxy_margin_3_pct", ""],
        ["Итого: Сумма заказов всего", "TOTAL|total_orderSum", 500.0],
        [f"SKU {first_nm}: Сумма заказов", f"SKU:{first_nm}|orderSum", 100.0],
        [f"SKU {second_nm}: Сумма заказов", f"SKU:{second_nm}|orderSum", 400.0],
    ]
    return _plan(as_of_date=as_of_date, date_columns=[as_of_date], rows=rows, timestamp_map=True)


def _ratio_plan(
    *,
    as_of_date: str,
    date_columns: list[str],
    nm_ids: list[int],
    values: dict[str, dict[Any, tuple[float | None, float | None]]],
    existing_targets: dict[str, list[Any]],
) -> SheetVitrinaV1Envelope:
    rows: list[list[Any]] = []
    rows.append(
        [
            "Итого: proxy прибыль 3",
            "TOTAL|total_proxy_profit_3_rub",
            *[_blankable(values[date]["TOTAL"][0]) for date in date_columns],
        ]
    )
    for nm_id in nm_ids:
        rows.append(
            [
                f"SKU {nm_id}: proxy прибыль 3",
                f"SKU:{nm_id}|proxy_profit_3_rub",
                *[_blankable(values[date][nm_id][0]) for date in date_columns],
            ]
        )
    for row_id, target_values in existing_targets.items():
        prefix = "Итого" if row_id.startswith("TOTAL|") else f"SKU {row_id.split(':', 1)[1].split('|', 1)[0]}"
        label = "Прокси маржинальность 3 всего, %" if row_id.startswith("TOTAL|") else "Прокси маржинальность 3, %"
        rows.append([f"{prefix}: {label}", row_id, *target_values])
    rows.append(
        [
            "Итого: Сумма заказов всего",
            "TOTAL|total_orderSum",
            *[_blankable(values[date]["TOTAL"][1]) for date in date_columns],
        ]
    )
    for nm_id in nm_ids:
        rows.append(
            [
                f"SKU {nm_id}: Сумма заказов",
                f"SKU:{nm_id}|orderSum",
                *[_blankable(values[date][nm_id][1]) for date in date_columns],
            ]
        )
    rows.append(
        [
            "Итого: Прокси маржинальность 2 всего, %",
            "TOTAL|proxy_margin_2_pct_total",
            *[_ratio_or_blank(*values[date]["TOTAL"]) for date in date_columns],
        ]
    )
    for nm_id in nm_ids:
        rows.append(
            [
                f"SKU {nm_id}: Прокси маржинальность 2, %",
                f"SKU:{nm_id}|proxy_margin_2_pct",
                *[_ratio_or_blank(*values[date][nm_id]) for date in date_columns],
            ]
        )
    return _plan(as_of_date=as_of_date, date_columns=date_columns, rows=rows, timestamp_map=True)


def _plan(
    *,
    as_of_date: str,
    date_columns: list[str],
    rows: list[list[Any]],
    timestamp_map: bool,
) -> SheetVitrinaV1Envelope:
    metadata: dict[str, Any] = {}
    if timestamp_map:
        metadata["row_last_updated_at_by_row_id"] = {
            str(row[1]): f"{as_of_date}T09:00:00Z"
            for row in rows
            if len(row) > 1
        }
    return SheetVitrinaV1Envelope(
        plan_version="delivery_contract_v1__sheet_scaffold_v1",
        snapshot_id=f"proxy-margin-3-backfill-{as_of_date}",
        as_of_date=as_of_date,
        date_columns=date_columns,
        temporal_slots=[
            SheetVitrinaV1TemporalSlot(
                slot_key="yesterday_closed" if index == 0 else "today_current",
                slot_label=column_date,
                column_date=column_date,
            )
            for index, column_date in enumerate(date_columns)
        ],
        source_temporal_policies={"stocks": "yesterday_closed_only"},
        sheets=[
            SheetVitrinaWriteTarget(
                sheet_name="DATA_VITRINA",
                write_start_cell="A1",
                write_rect=f"A1:{_column_letters(2 + len(date_columns))}{len(rows) + 1}",
                clear_range="A:ZZ",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=["label", "key", *date_columns],
                rows=rows,
                row_count=len(rows),
                column_count=2 + len(date_columns),
            ),
            SheetVitrinaWriteTarget(
                sheet_name="STATUS",
                write_start_cell="A1",
                write_rect="A1:K2",
                clear_range="A:ZZ",
                write_mode="overwrite",
                partial_update_allowed=False,
                header=STATUS_HEADER,
                rows=[
                    [
                        "stocks[yesterday_closed]",
                        "success",
                        "fresh",
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        as_of_date,
                        len(rows),
                        len(rows),
                        "",
                        "fixture",
                    ]
                ],
                row_count=1,
                column_count=len(STATUS_HEADER),
            ),
        ],
        metadata=metadata,
    )


def _bundle_generation(base: dict[str, Any], *, bundle_version: str, uploaded_at: str) -> dict[str, Any]:
    payload = deepcopy(base)
    payload["bundle_version"] = bundle_version
    payload["uploaded_at"] = uploaded_at
    return payload


def _first_snapshot_record(db_path: Path, *, as_of_date: str) -> ReadySnapshotInput:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT bundle_version, activated_at, as_of_date, snapshot_id, plan_version, refreshed_at, plan_json
            FROM sheet_vitrina_v1_ready_snapshots
            WHERE as_of_date = ?
            ORDER BY activated_at
            LIMIT 1
            """,
            (as_of_date,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise AssertionError(f"fixture snapshot missing: {as_of_date}")
    return ReadySnapshotInput(**dict(row))


def _snapshot_identity_rows(db_path: Path) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            """
            SELECT bundle_version, activated_at, as_of_date, snapshot_id, plan_version, refreshed_at
            FROM sheet_vitrina_v1_ready_snapshots
            ORDER BY bundle_version, as_of_date
            """
        ).fetchall()
    finally:
        conn.close()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _blankable(value: float | None) -> Any:
    return "" if value is None else value


def _ratio_or_blank(numerator: float | None, denominator: float | None) -> Any:
    if numerator is None or denominator is None:
        return ""
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 6)


def _column_letters(number: int) -> str:
    letters: list[str] = []
    while number:
        number, remainder = divmod(number - 1, 26)
        letters.append(chr(ord("A") + remainder))
    return "".join(reversed(letters))


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
