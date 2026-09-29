#!/usr/bin/env python3
"""Focused daily Finance source, freshness, allocation and SPP regressions."""

from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.wb_finance_weekly_smoke import _fixture_rows, _seed_canonical_cost
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
from packages.adapters.wb_finance_api import FinanceFetchResult, FinanceRateLimited
from packages.application.wb_finance_daily import WbFinanceDailyBlock, closed_daily_dates
from packages.application.wb_finance_spp import project_spp
from packages.application.finance_storage_migration import (
    FinanceStorageMigrationError, _assert_no_daily_raw_monolith, _table_owner,
)
from packages.application.finance_raw_storage import (
    bind_generation_identity, ensure_operational_schema, ensure_raw_schema,
)
from packages.application.storage_registry import atomic_write_manifest, build_manifest
from apps import wb_finance_daily as daily_cli


NOW = datetime(2026, 9, 29, 9, tzinfo=timezone.utc)


def _rows(day: date) -> list[dict]:
    rows = _fixture_rows()
    for row in rows:
        row.update(dateFrom=day.isoformat(), dateTo=day.isoformat(),
                   country="Россия", officeName="Склад WB", spp=11.5)
        row["reportId"] = str(131887220260928 + int(row["reportId"]))
        row["rrdId"] = str(9007199254740993 + int(row["rrdId"]))
    return rows


class _Client:
    def __init__(self, rows: list[dict] | None = None, *, rate_limited: bool = False):
        self.rows = rows or []
        self.rate_limited = rate_limited
        self.calls: list[tuple[str, str, str]] = []

    def fetch_report(self, *, date_from: str, date_to: str, period: str):
        self.calls.append((date_from, date_to, period))
        if self.rate_limited:
            raise FinanceRateLimited("rate_limited", date_from=date_from,
                date_to=date_to, period=period, cursor=0, pages=0, http_status=429)
        return FinanceFetchResult(self.rows, 1, 0, 204, "sha256:fixture")


def _spp_contract() -> None:
    base = dict(docTypeName="Продажа", sellerOperName="Продажа",
                country="Россия", quantity=1, officeName="Склад WB", spp=10)
    rows = [
        dict(base, quantity=2, spp=0),
        dict(base, quantity=1, spp=30),
        dict(base, quantity=4, spp=None),
        dict(base, quantity=5, officeName="Склад поставщика - везу на склад WB", spp=20),
        dict(base, quantity=7, officeName="Коледино", spp=50),
        dict(base, quantity=3, deliveryMethod="FBS", spp=40),  # conflict
        dict(base, quantity=2, deliveryMethod="DBS", spp=50),
        dict(base, quantity=2, country="Казахстан", spp=50),
        dict(base, quantity=2, docTypeName="Возврат", spp=50),
        dict(base, quantity=2, sellerOperName="Логистика", spp=50),
    ]
    result = project_spp(rows)
    assert result["spp_fbo_pct"] == "10.0000", result
    assert result["spp_fbs_pct"] == "20.0000", result
    assert result["coverage"]["valid_quantity"] == 8, result
    assert result["coverage"]["missing_spp_quantity"] == 4, result
    assert result["coverage"]["unknown_channel_quantity"] == 12, result
    assert result["coverage"]["non_russian_sale_rows"] == 1, result


def _daily_contract() -> None:
    day = date(2026, 9, 28)
    assert len(closed_daily_dates(NOW)) == 14
    assert closed_daily_dates(NOW)[0] == date(2026, 9, 15)
    with TemporaryDirectory(prefix="wb-finance-daily-") as tmp:
        block = WbFinanceDailyBlock(Path(tmp), seller_id="seller-1", now_factory=lambda: NOW)
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        rows = _rows(day)
        assert block.due_days(max_days=2) == [date(2026, 9, 15), date(2026, 9, 16)]
        assert block.sync_day(day, _Client())['status'] == "waiting"
        assert block.sync_day(day, _Client(rate_limited=True))['status'] == "rate_limited"
        assert block.build_daily_payload()["days"][-1]["metrics"] == {}
        raw = block._store_complete_raw(day, rows, "sha256:fixture")
        assert raw["changed"]
        # A process may die after raw commit. The pointer is recoverable but not published.
        assert block.build_daily_payload()["days"][-1]["status"] == "stale_projection"
        projected = block.project_pointer(day)
        assert projected["report_types"] == [1, 2]
        assert projected["report_ids"][0].startswith("131887")
        current = block.build_daily_payload()["days"][-1]
        assert current["status"] == "loaded_preliminary", current
        assert current["metrics"]["spp_fbo_pct"] == "11.5000", current
        assert current["metrics"]["revenue_before_returns"] == "360.0000", current
        assert current["metrics"]["cogs"] == "200.0000", current
        assert len(current["report_ids"]) == 2
        with sqlite3.connect(block.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM wb_finance_weekly_raw_rows").fetchone()[0] == 0
            assert conn.execute("SELECT COUNT(*) FROM wb_finance_daily_raw_rows").fetchone()[0] == len(rows)
            conn.execute("UPDATE wb_finance_daily_aggregates SET content_hash='wrong'")
            conn.commit()
        assert block.build_daily_payload()["days"][-1]["status"] == "stale_projection"
        block.project_pointer(day)
        with sqlite3.connect(block.db_path) as conn:
            conn.execute("""UPDATE sheet_vitrina_v1_warehouse_wb_daily_cost
                           SET wac_rub='222', fingerprint='changed' WHERE nm_id=101""")
            conn.commit()
        assert block.build_daily_payload()["days"][-1]["status"] == "stale_projection"
        block.project_pointer(day)
        changed = block.build_daily_payload()["days"][-1]
        assert changed["status"] == "loaded_preliminary", changed
        assert changed["metrics"]["cogs"] != current["metrics"]["cogs"], changed

        # A later report can carry an earlier operation and alter prior capped addbacks.
        late = day - timedelta(days=1)
        late_rows = [dict(rows[0], dateFrom=late.isoformat(), dateTo=late.isoformat(),
                          reportId="131887220260927", rrdId="9007199254741993")]
        block._store_complete_raw(late, late_rows, "sha256:retro")
        assert block.build_daily_payload()["days"][-1]["status"] == "stale_projection"
        block.project_pointer(late)
        block.project_pointer(day)
        assert block.build_daily_payload()["days"][-1]["status"] == "loaded_preliminary"

        # Frozen lower bound survives passage of time; old incomplete days stay due.
        block.now_factory = lambda: NOW + timedelta(days=15)
        assert block.due_days(max_days=1) == [date(2026, 9, 15)]
        assert block.sync_day(date(2026, 9, 15), _Client())['status'] == "waiting"
        assert len(block.build_daily_payload()["days"]) <= 14

        assert _table_owner("wb_finance_daily_raw_rows")["owner"] == "finance_raw"
        with sqlite3.connect(block.db_path) as conn:
            try:
                _assert_no_daily_raw_monolith(conn)
            except FinanceStorageMigrationError:
                pass
            else:
                raise AssertionError("legacy monolith migration accepted nonempty daily raw")


def _split_backup_contract() -> None:
    with TemporaryDirectory(prefix="wb-finance-daily-split-") as tmp:
        runtime = Path(tmp)
        bootstrap = WbFinanceDailyBlock(runtime, seller_id="seller-1", now_factory=lambda: NOW)
        bootstrap.ensure_schema()
        _seed_canonical_cost(bootstrap.db_path)
        generation = runtime / "generations" / "daily-smoke"
        generation.mkdir(parents=True)
        raw_path = generation / "finance_raw.sqlite3"
        op_path = generation / "operational.sqlite3"
        with sqlite3.connect(bootstrap.db_path) as source, sqlite3.connect(op_path) as op:
            source.backup(op)
            for name in ("wb_finance_daily_current_rows",):
                op.execute(f"DROP VIEW IF EXISTS {name}")
            for name in ("wb_finance_daily_pointers", "wb_finance_daily_raw_rows",
                         "wb_finance_daily_batches"):
                op.execute(f"DROP TABLE IF EXISTS {name}")
            op.execute("DROP INDEX IF EXISTS wb_finance_raw_by_week")
            op.execute("DROP INDEX IF EXISTS wb_finance_raw_by_sku_week")
            op.execute("DROP TABLE wb_finance_weekly_raw_rows")
            op.row_factory = sqlite3.Row
            ensure_operational_schema(op)
            bind_generation_identity(op, logical_store="operational",
                generation_id="op-daily-smoke", generation_epoch="daily-smoke",
                source_fingerprint="sha256:" + "a" * 64)
            op.commit()
        with sqlite3.connect(raw_path) as raw:
            raw.row_factory = sqlite3.Row
            ensure_raw_schema(raw)
            bind_generation_identity(raw, logical_store="finance_raw",
                generation_id="raw-daily-smoke", generation_epoch="daily-smoke",
                source_fingerprint="sha256:" + "a" * 64)
            raw.commit()
        manifest = build_manifest(state="cutover", canonical_source="split",
            generation_epoch="daily-smoke", raw_generation_id="raw-daily-smoke",
            raw_relative_path=str(raw_path.relative_to(runtime)), raw_watermark="0",
            operational_generation_id="op-daily-smoke",
            operational_relative_path=str(op_path.relative_to(runtime)),
            operational_watermark="fixture", rollback_generation_id="monolith",
            source_fingerprint="sha256:" + "a" * 64)
        atomic_write_manifest(runtime / "storage_generation_manifest.json", manifest)
        block = WbFinanceDailyBlock(runtime, seller_id="seller-1", now_factory=lambda: NOW)
        block.ensure_schema()
        day = date(2026, 9, 28)
        block._store_complete_raw(day, _rows(day), "sha256:split")
        assert block.project_pointer(day)["status"] == "loaded_preliminary"
        assert block.build_daily_payload()["days"][-1]["metrics"]["cogs"] == "200.0000"
        with sqlite3.connect(op_path) as op:
            assert "wb_finance_daily_raw_rows" not in {
                row[0] for row in op.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        backup_path = runtime / "raw-backup.sqlite3"
        with sqlite3.connect(raw_path) as raw, sqlite3.connect(backup_path) as backup:
            raw.backup(backup)
        with sqlite3.connect(backup_path) as backup:
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert backup.execute("SELECT COUNT(*) FROM wb_finance_daily_current_rows").fetchone()[0] == len(_rows(day))


def _retro_cap_repair_contract() -> None:
    with TemporaryDirectory(prefix="wb-finance-daily-cap-") as tmp:
        block = WbFinanceDailyBlock(Path(tmp), seller_id="seller-1", now_factory=lambda: NOW)
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        with sqlite3.connect(block.db_path) as conn:
            conn.executescript("""
                CREATE TABLE sheet_vitrina_v1_wb_supply_cost_layers(
                    wb_supply_cost_layer_id TEXT,wb_supply_id TEXT,nm_id TEXT,
                    transit_cost_status TEXT,transit_amount_total TEXT,
                    wb_acceptance_amount_total TEXT,inputs_hash TEXT,
                    version INTEGER,is_current INTEGER
                );
                INSERT INTO sheet_vitrina_v1_wb_supply_cost_layers VALUES(
                    'layer-77','77','101','confirmed','0','10','sha256:layer-77',1,1
                );
            """)
            conn.commit()
        old_day, later_day = date(2026, 9, 27), date(2026, 9, 28)
        old = dict(_rows(old_day)[0], paidAcceptance="10", giId="77",
                   saleDt="2026-06-24", rrdId="9007199254743001")
        later = dict(_rows(later_day)[0], paidAcceptance="10", giId="77",
                     saleDt="2026-06-23", reportId="1318872202609289",
                     rrdId="9007199254743002")
        block._store_complete_raw(old_day, [old], "sha256:old")
        block.project_pointer(old_day)
        before = next(item for item in block.build_daily_payload()["days"] if item["day"] == str(old_day))
        assert before["metrics"]["capitalized_acceptance"] == "10.0000", before
        block._store_complete_raw(later_day, [later], "sha256:later")
        block.project_pointer(later_day)  # same Python instance, pointer ahead of sync
        stale = next(item for item in block.build_daily_payload()["days"] if item["day"] == str(old_day))
        assert stale["status"] == "stale_projection", stale
        block.tick(_Client(), max_days=1)
        payload = block.build_daily_payload()["days"]
        old_after = next(item for item in payload if item["day"] == str(old_day))
        later_after = next(item for item in payload if item["day"] == str(later_day))
        assert old_after["status"] != "stale_projection", old_after
        assert old_after["metrics"]["capitalized_acceptance"] == "0.0000", old_after
        assert later_after["metrics"]["capitalized_acceptance"] == "10.0000", later_after


def _weekly_spp_bound_contract() -> None:
    with TemporaryDirectory(prefix="wb-finance-spp-bound-") as tmp:
        block = WbFinanceWeeklyBlock(Path(tmp), seller_id="seller-1", now_factory=lambda: NOW)
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        first = date(2026, 7, 6)
        for offset in range(11):
            start = first + timedelta(days=7 * offset)
            end = start + timedelta(days=6)
            row = dict(_fixture_rows()[0], dateFrom=start.isoformat(),
                       dateTo=end.isoformat(), reportId=5000+offset,
                       rrdId=7000+offset, country="Россия",
                       officeName="Склад WB", spp="7", quantity=1,
                       retailPriceWithDisc="120", saleDt="2026-06-23")
            block.ingest_week(start, end, [row])
        refreshed = block.refresh_recent_spp()
        assert len(refreshed["weeks"]) == 10, refreshed
        weeks = block.build_payload()["weeks"]
        assert weeks[0]["metrics"]["spp_fbo_pct"] is None, weeks[0]
        assert all(week["metrics"]["spp_fbo_pct"] == "7.0000" for week in weeks[1:])


def _benchmark_admission() -> None:
    """Synthetic read bound: 14 official-like days, 10k rows/day, one SKU."""
    with TemporaryDirectory(prefix="wb-finance-daily-benchmark-") as tmp:
        block = WbFinanceDailyBlock(Path(tmp), seller_id="seller-1", now_factory=lambda: NOW)
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        for offset, day in enumerate(closed_daily_dates(NOW)):
            rows = [dict(dateFrom=day.isoformat(), dateTo=day.isoformat(),
                         reportId=str(131887220260900+offset),
                         rrdId=str(9007199254740993+offset*10000+index),
                         reportType=1, nmId=101, vendorCode="VC101",
                         sku="4600000000101", saleDt="2026-06-23",
                         docTypeName="Продажа", sellerOperName="Продажа",
                         quantity=1, retailPriceWithDisc="120", forPay="90",
                         country="Россия", officeName="Склад WB", spp="10")
                    for index in range(10_000)]
            block._store_complete_raw(day, rows, "sha256:benchmark")
        with sqlite3.connect(block.db_path) as conn:
            conn.executemany(
                """INSERT INTO wb_finance_daily_sync
                   (seller_id,report_day,week_start,week_end,status)
                   VALUES('seller-1',?,?,?,'waiting')""",
                [(day.isoformat(),)*3 for day in closed_daily_dates(NOW)],
            )
            conn.commit()
        started = time.monotonic()
        payload = block.build_daily_payload()
        seconds = time.monotonic() - started
        assert len(payload["days"]) == 14
        print(f"daily admission benchmark: 140000 rows / 14 days / 1 SKU -> {seconds:.3f}s")


def _weekly_refresh_wiring_contract() -> None:
    calls: list[tuple[str, int | None]] = []

    class Daily:
        def tick(self, _client, *, max_days):
            calls.append(("daily_tick", max_days))
            return {"status": "ok", "days": []}

        def sync_day(self, _day, _client):
            calls.append(("daily_sync", None))
            return {"status": "waiting"}

    class Weekly:
        def refresh_recent_spp(self):
            calls.append(("weekly_spp", None))
            return {"status": "ok", "weeks": []}

    with TemporaryDirectory(prefix="wb-finance-daily-cli-") as tmp:
        with (patch.object(daily_cli, "daily_block_from_env", return_value=Daily()),
              patch.object(daily_cli, "weekly_block_from_env", return_value=Weekly()),
              patch.object(daily_cli, "WbFinanceApiClient", return_value=object())):
            for command, expected in (("tick", 2), ("bootstrap", 14)):
                with redirect_stdout(io.StringIO()) as output:
                    assert daily_cli.main([command, "--runtime-dir", tmp,
                                           "--env-file", str(Path(tmp)/"absent")]) == 0
                assert json.loads(output.getvalue())["weekly_spp_refresh"]["status"] == "ok"
                assert calls[-2:] == [("daily_tick", expected), ("weekly_spp", None)], calls
            with redirect_stdout(io.StringIO()):
                daily_cli.main(["sync-day", "--day", "2026-09-28",
                                "--runtime-dir", tmp,
                                "--env-file", str(Path(tmp)/"absent")])
            assert calls[-1] == ("daily_sync", None), calls
            with daily_cli._worker_lock(Path(tmp)) as acquired:
                assert acquired
                with redirect_stdout(io.StringIO()) as output:
                    daily_cli.main(["tick", "--runtime-dir", tmp,
                                    "--env-file", str(Path(tmp)/"absent")])
                assert json.loads(output.getvalue())["status"] == "busy"
                assert calls[-1] == ("daily_sync", None), calls


def main() -> None:
    _spp_contract()
    _daily_contract()
    _split_backup_contract()
    _retro_cap_repair_contract()
    _weekly_spp_bound_contract()
    _weekly_refresh_wiring_contract()
    print("wb_finance_daily: ok -> SPP, 204/429, replay, parity, cost freshness, allocation, due policy")


if __name__ == "__main__":
    if "--benchmark" in sys.argv[1:]:
        _benchmark_admission()
    else:
        main()
