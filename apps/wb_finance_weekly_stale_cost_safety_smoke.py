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
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_finance_weekly import _create_sqlite_backup  # noqa: E402
from apps.wb_finance_weekly_cost_cutover_smoke import (  # noqa: E402
    _row,
    _seed_sources,
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
        _seed_sources(block.db_path)
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                """INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES(
                   'warehouse_functional_cutover_v1','2026-07-20',101,'10','150','1500',
                   'certified','{}','sha256:cost-101-jul20','2026-07-20T00:00:00Z')"""
            )
            conn.commit()
        sale = _row(1, "2026-06-23")
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
                "UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='201',fingerprint='changed-701' WHERE as_of_date='2026-07-01' AND nm_id=101"
            )
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='202',fingerprint='changed-720' WHERE as_of_date='2026-07-20' AND nm_id=101"
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

        original = block._build_week_target_projection
        calls = 0

        def fail_second(
            conn: sqlite3.Connection, *, week_start: date, week_end: date
        ) -> dict:
            nonlocal calls
            calls += 1
            result = original(
                conn, week_start=week_start, week_end=week_end
            )
            if calls == 2:
                raise RuntimeError("synthetic second-week failure")
            return result

        block._build_week_target_projection = fail_second  # type: ignore[method-assign]
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
            block._build_week_target_projection = original  # type: ignore[method-assign]
        _assert(
            _metrics(block, "2026-06-29") == mixed_before, "first week escaped rollback"
        )
        _assert(
            _metrics(block, "2026-07-20") == late_before, "second week escaped rollback"
        )

        # Recalculation builds target after-images before BEGIN IMMEDIATE.  An
        # unrelated interactive writer commits promptly and no longer defeats
        # the exact Finance dependency CAS merely by changing data_version.
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS ff_interactive_status_probe("
                "probe_id TEXT PRIMARY KEY,created_at TEXT NOT NULL)"
            )
            conn.commit()
        recalculation_entered = threading.Event()
        release_recalculation = threading.Event()
        calls = 0

        def pause_snapshot_recalculation(
            conn: sqlite3.Connection, *, week_start: date, week_end: date
        ) -> dict:
            nonlocal calls
            _assert(
                int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
                and not conn.in_transaction,
                "heavy Finance projection must use an autocommit query-only connection",
            )
            calls += 1
            result = original(
                conn, week_start=week_start, week_end=week_end
            )
            if calls == 1:
                recalculation_entered.set()
                if not release_recalculation.wait(timeout=5):
                    raise AssertionError("Finance snapshot contention probe timed out")
            return result

        background_errors: list[Exception] = []

        def apply_in_background() -> None:
            try:
                block.apply_stale_cost_weeks(
                    expected_fingerprint=str(plan["fingerprint"]),
                    date_from=date(2026, 6, 29),
                    date_to=date(2026, 7, 26),
                )
            except Exception as exc:
                background_errors.append(exc)

        block._build_week_target_projection = pause_snapshot_recalculation  # type: ignore[method-assign]
        background = threading.Thread(target=apply_in_background, daemon=True)
        background.start()
        _assert(
            recalculation_entered.wait(timeout=5),
            "Finance snapshot recalculation did not start",
        )
        interactive_started = time.monotonic()
        with sqlite3.connect(block.db_path, timeout=2) as interactive:
            interactive.execute(
                "INSERT INTO ff_interactive_status_probe(probe_id,created_at) VALUES(?,?)",
                ("ff-document-status", "2026-07-27T00:00:00Z"),
            )
            interactive.execute(
                "UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost "
                "SET wac_rub='51',fingerprint='unrelated-nm-103' "
                "WHERE as_of_date='2026-07-01' AND nm_id=103"
            )
            interactive.commit()
        interactive_ms = int((time.monotonic() - interactive_started) * 1000)
        release_recalculation.set()
        background.join(timeout=5)
        block._build_week_target_projection = original  # type: ignore[method-assign]
        _assert(not background.is_alive(), "stale Finance CAS did not terminate")
        _assert(
            not background_errors,
            "unrelated status and canonical other-SKU writers must not "
            f"invalidate exact Finance CAS: {background_errors}",
        )
        _assert(
            interactive_ms < 1_500,
            f"interactive document/status writer waited {interactive_ms}ms",
        )

        # A change to an actual canonical cost dependency during the same
        # lock-free projection still fails closed before target replacement.
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost "
                "SET wac_rub='204',fingerprint='exact-dependency-before' "
                "WHERE as_of_date='2026-07-01' AND nm_id=101"
            )
            conn.commit()
        plan = block.plan_stale_cost_weeks(
            date_from=date(2026, 6, 29), date_to=date(2026, 7, 26)
        )
        exact_entered = threading.Event()
        release_exact = threading.Event()
        calls = 0

        def pause_exact_dependency(
            conn: sqlite3.Connection, *, week_start: date, week_end: date
        ) -> dict:
            nonlocal calls
            calls += 1
            result = original(conn, week_start=week_start, week_end=week_end)
            if calls == 1:
                exact_entered.set()
                if not release_exact.wait(timeout=5):
                    raise AssertionError("Finance exact dependency probe timed out")
            return result

        background_errors = []
        block._build_week_target_projection = pause_exact_dependency  # type: ignore[method-assign]
        background = threading.Thread(target=apply_in_background, daemon=True)
        background.start()
        _assert(exact_entered.wait(timeout=5), "exact dependency projection did not start")
        with sqlite3.connect(block.db_path) as concurrent_source:
            concurrent_source.execute(
                "UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost "
                "SET wac_rub='205',fingerprint='exact-dependency-drift' "
                "WHERE as_of_date='2026-07-01' AND nm_id=101"
            )
            concurrent_source.commit()
        release_exact.set()
        background.join(timeout=5)
        block._build_week_target_projection = original  # type: ignore[method-assign]
        _assert(not background.is_alive(), "exact dependency CAS did not terminate")
        _assert(
            background_errors
            and "exact dependency changed after snapshot planning"
            in str(background_errors[0]),
            f"canonical source drift must fail closed: {background_errors}",
        )
        plan = block.plan_stale_cost_weeks(
            date_from=date(2026, 6, 29), date_to=date(2026, 7, 26)
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
            applied["recalculated_week_count"] == plan["stale_week_count"],
            f"apply scope mismatch: {applied}",
        )
        _assert(applied["non_target_preserved"], f"non-target mismatch: {applied}")
        _assert(
            applied["non_target_digest_before"]
            == applied["non_target_digest_after"],
            f"apply-time non-target digest evidence mismatch: {applied}",
        )
        _assert(
            applied["source_dependency"]["contract"]
            == "wb_finance_exact_target_dependency_v2",
            f"target dependency contract mismatch: {applied}",
        )
        timings = applied["phase_timings_ms"]
        _assert(
            set(timings)
            == {
                "query_plan",
                "query_projection",
                "dependency_verify",
                "writer_lock_hold",
                "post_commit_readback",
            },
            f"phase timing evidence missing: {timings}",
        )
        _assert(
            float(timings["writer_lock_hold"]) < 1_500,
            f"Finance writer section includes heavy projection: {timings}",
        )
        _assert(_metrics(block, "2026-06-22") == control_before, "control week changed")

        repeated = block.plan_stale_cost_weeks(
            date_from=date(2026, 6, 29), date_to=date(2026, 7, 26)
        )
        _assert(
            repeated["stale_week_count"] == 0, f"repeat must be zero-change: {repeated}"
        )

        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                "UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost SET wac_rub='203',fingerprint='cli-change-701' WHERE as_of_date='2026-07-01' AND nm_id=101"
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
        cli_apply_process = subprocess.run(
            [
                *cli_base,
                "--apply",
                "--confirm-fingerprint",
                dry_run["fingerprint"],
                "--backup-dir",
                str(root / "cli-backups"),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
        _assert(
            cli_apply_process.returncode == 0,
            "CLI apply failed: "
            + (cli_apply_process.stderr or cli_apply_process.stdout),
        )
        cli_apply = json.loads(cli_apply_process.stdout)
        _assert(
            cli_apply["recalculated_week_count"] == 1,
            f"CLI apply mismatch: {cli_apply}",
        )
        _assert(
            cli_apply["recovery_policy"]["tier"] == "T1"
            and cli_apply["recovery_policy"]["lifecycle"] == "retained"
            and cli_apply["backup"]["copy_bytes"] == 0,
            f"CLI bounded recovery mismatch: {cli_apply}",
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
