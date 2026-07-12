#!/usr/bin/env python3
"""Guarded, atomic stale Finance cost recalculation smoke."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_finance_weekly import _create_sqlite_backup  # noqa: E402
from apps.wb_finance_weekly_cost_cutover_smoke import (  # noqa: E402
    _base_rows,
    _seed_cost_sources,
)
from packages.application.wb_finance_weekly import (  # noqa: E402
    WbFinanceWeeklyBlock,
)


def main() -> None:
    with TemporaryDirectory(prefix="wb-finance-stale-safety-") as tmp:
        root = Path(tmp)
        block = WbFinanceWeeklyBlock(
            root,
            seller_id="canonical",
            now_factory=lambda: datetime(2026, 7, 27, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_cost_sources(block.db_path)
        sale = _base_rows()[0]
        block.ingest_week(
            date(2026, 6, 22),
            date(2026, 6, 28),
            [dict(sale, reportId=622, rrdId=6221, rrDate="2026-06-23")],
        )
        block.ingest_week(
            date(2026, 6, 29),
            date(2026, 7, 5),
            [dict(sale, reportId=701, rrdId=7011, rrDate="2026-07-01")],
        )
        block.ingest_week(
            date(2026, 7, 20),
            date(2026, 7, 26),
            [dict(sale, reportId=720, rrdId=7201, rrDate="2026-07-20")],
        )
        control_before = _metrics(block, "2026-06-22")
        mixed_before = _metrics(block, "2026-06-29")
        late_before = _metrics(block, "2026-07-20")
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_cost_daily_state SET our_wb_unit_cost_rub=201,inputs_hash='changed-701' WHERE as_of_date='2026-07-01' AND nm_id=101"
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_cost_daily_state SET our_wb_unit_cost_rub=202,inputs_hash='changed-720' WHERE as_of_date='2026-07-20' AND nm_id=101"
            )
            conn.commit()

        plan = block.plan_stale_cost_weeks(
            date_from=date(2026, 6, 29), date_to=date(2026, 7, 26)
        )
        _assert(plan["runtime_mutation"] is False, "dry-run must not mutate")
        _assert(plan["stale_week_count"] == 2, f"stale scope mismatch: {plan}")
        _assert(
            [item["week_start"] for item in plan["weeks"]]
            == ["2026-06-29", "2026-07-20"],
            f"bounded stale weeks mismatch: {plan}",
        )
        _assert(
            _metrics(block, "2026-06-29") == mixed_before, "dry-run changed mixed week"
        )

        try:
            block.apply_stale_cost_weeks(
                expected_fingerprint="sha256:wrong",
                date_from=date(2026, 6, 29),
                date_to=date(2026, 7, 26),
            )
        except ValueError as exc:
            _assert("fingerprint changed" in str(exc), f"wrong gate error: {exc}")
        else:
            raise AssertionError("wrong fingerprint unexpectedly applied")

        with sqlite3.connect(block.db_path) as conn:
            original_report_hash = str(
                conn.execute(
                    "SELECT content_hash FROM wb_finance_weekly_reports WHERE seller_id='canonical' AND report_id='701'"
                ).fetchone()[0]
            )
            conn.execute(
                "UPDATE wb_finance_weekly_reports SET content_hash='concurrent-change' WHERE seller_id='canonical' AND report_id='701'"
            )
            conn.commit()
        try:
            block.apply_stale_cost_weeks(
                expected_fingerprint=str(plan["fingerprint"]),
                date_from=date(2026, 6, 29),
                date_to=date(2026, 7, 26),
            )
        except ValueError as exc:
            _assert("fingerprint changed" in str(exc), f"metadata drift error: {exc}")
        else:
            raise AssertionError("report metadata drift unexpectedly applied")
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "UPDATE wb_finance_weekly_reports SET content_hash=? WHERE seller_id='canonical' AND report_id='701'",
                (original_report_hash,),
            )
            conn.commit()

        original = block._recalculate_week_in_connection
        calls = 0

        def fail_second(conn: sqlite3.Connection, start: date, end: date) -> dict:
            nonlocal calls
            calls += 1
            result = original(conn, start, end)
            if calls == 2:
                raise RuntimeError("synthetic second-week failure")
            return result

        block._recalculate_week_in_connection = fail_second  # type: ignore[method-assign]
        try:
            block.apply_stale_cost_weeks(
                expected_fingerprint=str(plan["fingerprint"]),
                date_from=date(2026, 6, 29),
                date_to=date(2026, 7, 26),
            )
        except RuntimeError as exc:
            _assert("second-week" in str(exc), f"atomic failure mismatch: {exc}")
        else:
            raise AssertionError("synthetic partial apply unexpectedly committed")
        finally:
            block._recalculate_week_in_connection = original  # type: ignore[method-assign]
        _assert(
            _metrics(block, "2026-06-29") == mixed_before, "first week escaped rollback"
        )
        _assert(
            _metrics(block, "2026-07-20") == late_before, "second week escaped rollback"
        )

        backup = _create_sqlite_backup(
            block.db_path,
            root / "backups",
            fingerprint=str(plan["fingerprint"]),
        )
        _assert(backup["integrity_check"] == "ok", f"backup failed: {backup}")
        _assert(
            Path(str(backup["path"])).stat().st_mode & 0o777 == 0o600,
            f"backup permissions are not private: {backup}",
        )
        applied = block.apply_stale_cost_weeks(
            expected_fingerprint=str(plan["fingerprint"]),
            date_from=date(2026, 6, 29),
            date_to=date(2026, 7, 26),
        )
        _assert(applied["status"] == "applied", f"apply status mismatch: {applied}")
        _assert(
            applied["recalculated_week_count"] == 2, f"apply scope mismatch: {applied}"
        )
        _assert(applied["non_target_preserved"], f"non-target mismatch: {applied}")
        _assert(_metrics(block, "2026-06-22") == control_before, "control week changed")

        repeated = block.plan_stale_cost_weeks(
            date_from=date(2026, 6, 29), date_to=date(2026, 7, 26)
        )
        _assert(
            repeated["stale_week_count"] == 0, f"repeat must be zero-change: {repeated}"
        )

        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_wb_cost_daily_state SET our_wb_unit_cost_rub=203,inputs_hash='cli-change-701' WHERE as_of_date='2026-07-01' AND nm_id=101"
            )
            conn.commit()
        cli_base = [
            sys.executable,
            str(ROOT / "apps" / "wb_finance_weekly.py"),
            "recalculate-stale-cost",
            "--env-file",
            str(root / "missing.env"),
            "--runtime-dir",
            str(root),
            "--date-from",
            "2026-06-29",
            "--date-to",
            "2026-07-26",
        ]
        env = {**os.environ, "WB_SELLER_ID": "canonical"}
        dry_run = json.loads(
            subprocess.run(
                cli_base,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout
        )
        _assert(dry_run["stale_week_count"] == 1, f"CLI dry-run mismatch: {dry_run}")
        cli_apply = json.loads(
            subprocess.run(
                [
                    *cli_base,
                    "--apply",
                    "--confirm-fingerprint",
                    dry_run["fingerprint"],
                    "--backup-dir",
                    str(root / "cli-backups"),
                ],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout
        )
        _assert(
            cli_apply["recalculated_week_count"] == 1,
            f"CLI apply mismatch: {cli_apply}",
        )
        _assert(
            cli_apply["backup"]["integrity_check"] == "ok",
            f"CLI backup mismatch: {cli_apply}",
        )
        _assert(
            Path(cli_apply["backup"]["path"]).stat().st_mode & 0o777 == 0o600,
            f"CLI backup permissions mismatch: {cli_apply}",
        )
        cli_repeat = json.loads(
            subprocess.run(
                cli_base,
                check=True,
                capture_output=True,
                text=True,
                env=env,
            ).stdout
        )
        _assert(
            cli_repeat["stale_week_count"] == 0,
            f"CLI repeat mismatch: {cli_repeat}",
        )
    print(
        "wb_finance_weekly_stale_cost_safety: ok -> dry-run, fingerprint, backup, atomic rollback, non-target digest, zero-change repeat"
    )


def _metrics(block: WbFinanceWeeklyBlock, week_start: str) -> dict:
    return next(
        week["metrics"]
        for week in block.build_payload()["weeks"]
        if week["week_start"] == week_start
    )


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
