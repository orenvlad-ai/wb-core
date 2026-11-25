"""Portable disposable smoke for real source clients and canonical finance."""
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timedelta

if __package__ in (None, ""):
    # Replace the script directory: its http.py must not shadow stdlib http.
    sys.path[0] = str(Path(__file__).resolve().parents[2])

from apps.local_cycle_lab.__main__ import offline_guard, seed_catalog
from apps.local_cycle_lab.full_sources import configure_sources
from apps.local_cycle_lab.sources import Replay


def main():
    violations = []
    offline_guard(violations)
    os.environ.clear()
    os.environ.update(WB_API_TOKEN="lab-only", ONEC_STOCKS_ACCOUNT_ID="000000001", TZ="UTC")
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
    from packages.contracts.stocks_block import StocksRequest
    from packages.application.ready_publication import readonly
    from packages.application.wb_fbs_warehouse_registry import REGISTRY_RUNS_TABLE
    scenario = json.loads(Path(__file__).with_name("scenario.json").read_text())
    now = datetime.fromisoformat(scenario["today"] + "T08:00:00+00:00")
    counter = 0
    def stamp():
        nonlocal counter
        counter += 1
        return (now + timedelta(microseconds=counter)).isoformat().replace("+00:00", "Z")
    with TemporaryDirectory(prefix="wbc-full-sources-") as folder:
        entry = RegistryUploadHttpEntrypoint(Path(folder), now_factory=lambda: now,
                                             activated_at_factory=stamp)
        seed_catalog(entry.runtime, scenario, stamp())
        replay = Replay(scenario, "normal")
        with configure_sources(entry, replay, now):
            from packages.adapters.wb_promotion import HttpBackedWbPromotionSource
            promotion = HttpBackedWbPromotionSource()
            for day, bid in (("2026-11-23", 14000), ("2026-11-24", 15000)):
                with replay.on_day(day):
                    adverts = promotion.fetch_adverts([601], statuses=[4, 9, 11])["adverts"]
                assert len(adverts) == 1 and adverts[0]["id"] == 601 and adverts[0]["status"] == 9
                assert adverts[0]["nm_settings"][0]["bids_kopecks"]["search"] == bid
            supplies = entry.wb_supplies_block.sync_functional_sources(record_ff_movements=False)["sync"]
            assert supplies["status"] == "ok" and not supplies["warnings"]
            assert supplies["active_reconciliation_complete"] and not supplies["partial_status_slices"]
            stocks = entry.warehouse_functional_block.opening.stocks_block.execute(
                StocksRequest(snapshot_date=scenario["today"], snapshot_type="current", nm_ids=[101])).result
            assert stocks.pagination_complete and stocks.raw_rows_digest and stocks.items[0].stock_total == 12
            assert entry.wb_fbs_orders.collect()["status"] == "success"
            # Genuine collection without a facility mapping must remain partial.
            entry.wb_fbs_warehouse_registry.collect()
            with readonly(entry.runtime.db_path) as conn:
                generation = conn.execute(f"SELECT status,complete FROM {REGISTRY_RUNS_TABLE} ORDER BY run_sequence DESC LIMIT 1").fetchone()
            assert generation["status"] == "partial" and not generation["complete"]
            first = entry._cycle_finance_sources()
            second = entry._cycle_finance_sources()
            assert first.versions["daily_raw"] == second.versions["daily_raw"]
            with readonly(entry.runtime.db_path) as conn:
                assert conn.execute("SELECT count(*) FROM wb_finance_weekly_raw_rows").fetchone()[0] == 2
                assert conn.execute("SELECT count(*) FROM wb_finance_daily_raw_rows").fetchone()[0] == 4
        assert not replay.unexpected and not violations
        print(json.dumps({"status": "passed", "scope": "native source collectors and canonical finance",
            "supplies_complete": True, "stock": 12, "unmapped_fbs_rejected": True,
            "finance_no_duplicate_raw_second_attempt": True, "warnings": list(first.warnings),
            "inputs": replay.totals}, ensure_ascii=False))


if __name__ == "__main__":
    main()
