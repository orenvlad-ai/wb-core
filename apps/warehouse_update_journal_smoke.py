"""Durability and last-good semantics for warehouse automatic/manual runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.warehouse_functional import ensure_warehouse_functional_schema  # noqa: E402
from packages.application.warehouse_update_journal import (  # noqa: E402
    PHASES,
    WarehouseUpdateJournal,
)


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 2, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> str:
        result = self.value.isoformat().replace("+00:00", "Z")
        self.value += timedelta(minutes=1)
        return result


def main() -> None:
    with TemporaryDirectory(prefix="warehouse-update-journal-") as tmp:
        db_path = Path(tmp) / "journal.sqlite3"
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            ensure_warehouse_functional_schema(conn)
            conn.commit()
        clock = Clock()
        journal = WarehouseUpdateJournal(db_path=db_path, timestamp_factory=clock)
        first = journal.start(trigger_source="hourly", scheduled_for="2026-08-02T08:00:00Z")
        for phase in PHASES:
            journal.phase_started(first, phase)
            journal.phase_finished(
                first,
                phase,
                item_count=3,
                details={"status": "ok", "raw_rows": list(range(1000))},
            )
        journal.finish(first, status="success", result={"business_date": "2026-08-02"})

        # A fresh object proves that the status is read from SQLite, not memory.
        restarted = WarehouseUpdateJournal(db_path=db_path, timestamp_factory=clock)
        durable = restarted.public_status()
        assert durable["automatic_updates"]["status"] == "success"
        assert durable["automatic_updates"]["last_success_at"]
        assert all(item["status"] == "success" for item in durable["phases"])
        assert all(item["details"]["raw_rows"] == {"item_count": 1000, "details_omitted": True} for item in durable["phases"])

        failed = restarted.start(trigger_source="hourly", scheduled_for="2026-08-02T09:00:00Z")
        restarted.phase_started(failed, PHASES[0])
        restarted.phase_finished(
            failed,
            PHASES[0],
            status="failed",
            error="fixture upstream failure",
        )
        restarted.finish(failed, status="failed", error="fixture upstream failure")
        degraded = restarted.public_status()
        assert degraded["automatic_updates"]["status"] == "failed"
        assert degraded["automatic_updates"]["last_success_at"] == durable["automatic_updates"]["last_success_at"]
        assert degraded["freshness"] == "degraded"
        assert degraded["phases"][0]["last_good_at"]
        assert "fixture upstream failure" in degraded["phases"][0]["last_error"]

        manual = restarted.start(trigger_source="manual")
        restarted.phase_started(manual, PHASES[0])
        restarted.phase_finished(manual, PHASES[0])
        restarted.finish(manual, status="success")
        separated = restarted.public_status()
        assert separated["manual_updates"]["run_id"] == manual
        assert separated["automatic_updates"]["run_id"] == failed
        assert len({first, failed, manual}) == 3

        orphan = restarted.start(trigger_source="manual")
        restarted.phase_started(orphan, PHASES[0])
        replacement = WarehouseUpdateJournal(
            db_path=db_path,
            timestamp_factory=clock,
        ).start(trigger_source="hourly")
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            orphan_row = conn.execute(
                "SELECT status,last_error FROM sheet_vitrina_v1_warehouse_update_runs "
                "WHERE run_id=?",
                (orphan,),
            ).fetchone()
            orphan_phase = conn.execute(
                "SELECT status FROM sheet_vitrina_v1_warehouse_update_phases "
                "WHERE run_id=? AND phase_key=?",
                (orphan, PHASES[0]),
            ).fetchone()
        assert orphan_row["status"] == "interrupted"
        assert "last-good" in orphan_row["last_error"]
        assert orphan_phase["status"] == "failed"
        restarted.finish(replacement, status="success")

    print("warehouse_update_journal_smoke: OK")


if __name__ == "__main__":
    main()
