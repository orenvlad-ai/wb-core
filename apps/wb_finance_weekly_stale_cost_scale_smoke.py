#!/usr/bin/env python3
"""Production-scale regression for lock-free Finance planning and short CAS."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apps.wb_finance_weekly_canonical_scale_smoke as scale_fixture  # noqa: E402
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock  # noqa: E402


RAW_ROW_COUNT = 295_919
MAX_INTERACTIVE_WRITER_MS = 1_500
MAX_FINANCE_WRITER_MS = 1_500


def main() -> None:
    with TemporaryDirectory(prefix="wb-finance-stale-scale-") as tmp:
        runtime = Path(tmp)
        block = WbFinanceWeeklyBlock(
            runtime,
            seller_id="seller-scale",
            now_factory=lambda: datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        scale_fixture.RAW_ROW_COUNT = RAW_ROW_COUNT
        scale_fixture._seed_required_sources(block.db_path)
        scale_fixture._seed_raw_history(block.db_path)
        _seed_scale_boundaries(block.db_path)

        plan = block.plan_stale_cost_weeks(date_from=date(2026, 1, 1))
        _assert(plan["stale_week_count"] == scale_fixture.WEEK_COUNT, "week scope drifted")

        projection_entered = threading.Event()
        release_projection = threading.Event()
        original_projection = block._build_week_target_projection
        original_dependency = block._finance_source_dependency_fingerprint
        dependency_contexts: list[tuple[bool, bool]] = []
        projection_calls = 0

        def observed_dependency(
            conn: sqlite3.Connection, **kwargs: object
        ) -> dict[str, object]:
            dependency_contexts.append(
                (
                    bool(conn.execute("PRAGMA query_only").fetchone()[0]),
                    bool(conn.in_transaction),
                )
            )
            return original_dependency(conn, **kwargs)

        def paused_projection(
            conn: sqlite3.Connection, *, week_start: date, week_end: date
        ) -> dict[str, object]:
            nonlocal projection_calls
            projection_calls += 1
            _assert(
                bool(conn.execute("PRAGMA query_only").fetchone()[0])
                and not conn.in_transaction,
                "production-scale projection is not query-only autocommit",
            )
            result = original_projection(
                conn, week_start=week_start, week_end=week_end
            )
            if projection_calls == 1:
                projection_entered.set()
                _assert(
                    release_projection.wait(timeout=30),
                    "production-scale contention probe timed out",
                )
            return result

        block._finance_source_dependency_fingerprint = observed_dependency  # type: ignore[method-assign]
        block._build_week_target_projection = paused_projection  # type: ignore[method-assign]
        background_result: list[dict[str, object]] = []
        background_errors: list[Exception] = []

        def apply_in_background() -> None:
            try:
                background_result.append(
                    block.apply_stale_cost_weeks(
                        expected_fingerprint=str(plan["fingerprint"]),
                        date_from=date(2026, 1, 1),
                    )
                )
            except Exception as exc:  # pragma: no cover - asserted below
                background_errors.append(exc)

        worker = threading.Thread(target=apply_in_background, daemon=True)
        worker.start()
        _assert(
            projection_entered.wait(timeout=90),
            "production-scale Finance projection did not start",
        )
        interactive_started = time.monotonic()
        with sqlite3.connect(block.db_path, timeout=2) as interactive:
            interactive.execute(
                "INSERT INTO ff_interactive_status_probe(probe_id,created_at) VALUES(?,?)",
                ("production-scale-status", "2026-07-20T08:00:00Z"),
            )
            interactive.commit()
        interactive_ms = (time.monotonic() - interactive_started) * 1000
        release_projection.set()
        worker.join(timeout=300)
        block._build_week_target_projection = original_projection  # type: ignore[method-assign]
        block._finance_source_dependency_fingerprint = original_dependency  # type: ignore[method-assign]

        _assert(not worker.is_alive(), "production-scale Finance apply did not terminate")
        _assert(not background_errors, f"production-scale apply failed: {background_errors}")
        result = background_result[0]
        timings = dict(result["phase_timings_ms"])
        _assert(result["status"] == "applied", f"apply status drifted: {result}")
        _assert(
            interactive_ms < MAX_INTERACTIVE_WRITER_MS,
            f"interactive writer waited {interactive_ms:.1f}ms",
        )
        _assert(
            float(timings["query_projection"]) > 5_000,
            f"fixture did not exercise heavy projection: {timings}",
        )
        _assert(
            float(timings["writer_lock_hold"]) < MAX_FINANCE_WRITER_MS,
            f"Finance writer held the lock too long: {timings}",
        )
        _assert(
            dependency_contexts
            and all(query_only and not in_tx for query_only, in_tx in dependency_contexts),
            f"dependency fingerprint entered a writer transaction: {dependency_contexts}",
        )
        repeat = block.plan_stale_cost_weeks(date_from=date(2026, 1, 1))
        _assert(repeat["stale_week_count"] == 0, f"repeat is not a no-op: {repeat}")
        print(
            "wb_finance_weekly_stale_cost_scale: ok -> "
            f"raw_rows={RAW_ROW_COUNT}, interactive_ms={interactive_ms:.1f}, "
            f"projection_ms={float(timings['query_projection']):.1f}, "
            f"writer_ms={float(timings['writer_lock_hold']):.1f}"
        )


def _seed_scale_boundaries(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES
                (1,2,'VC2','BAR2','["BAR2"]','other'),
                (1,10,'VC10','BAR10','["BAR10"]','other');
            INSERT INTO sheet_vitrina_v1_warehouse_wb_daily_cost VALUES
                ('warehouse_functional_cutover_v1','2026-07-01',2,'10','91','910',
                 'certified','{}','sha256:scale-cost-2','2026-07-01T00:00:00Z'),
                ('warehouse_functional_cutover_v1','2026-07-01',10,'10','92','920',
                 'certified','{}','sha256:scale-cost-10','2026-07-01T00:00:00Z');
            UPDATE wb_finance_weekly_raw_rows
               SET nm_id='2',vendor_code='VC2',barcode='BAR2',
                   row_hash='sha256:scale-order-2',
                   raw_json=json_set(raw_json,'$.nmId',2,'$.vendorCode','VC2','$.sku','BAR2')
             WHERE rrd_id='scale-1';
            UPDATE wb_finance_weekly_raw_rows
               SET nm_id='10',vendor_code='VC10',barcode='BAR10',
                   row_hash='sha256:scale-order-10',
                   raw_json=json_set(raw_json,'$.nmId',10,'$.vendorCode','VC10','$.sku','BAR10')
             WHERE rrd_id='scale-2';
            CREATE TABLE ff_interactive_status_probe(
                probe_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL
            );
            INSERT INTO wb_finance_weekly_sync(
                seller_id,week_start,week_end,status,attempt_count,
                report_count,raw_row_count
            )
            SELECT seller_id,week_start,week_end,'completed',1,0,COUNT(*)
              FROM wb_finance_weekly_raw_rows
             GROUP BY seller_id,week_start,week_end;
            """
        )
        conn.commit()


def _assert(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


if __name__ == "__main__":
    main()
