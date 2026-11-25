"""Ручной сбор и публикация витрины на сохранённых внешних входах WBC."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time

from unittest.mock import patch

from .http import HttpTape
from .checks import verify
from .sources import Replay

ROOT = Path(__file__).resolve().parents[2]
SCENARIO = Path(__file__).with_name("scenario.json")


def offline_guard(violations):
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen",
                     "os.system", "os.exec", "os.posix_spawn"}:
            violations.append(event)
            raise RuntimeError("Недоступно в автономном сценарии: " + event)
    sys.addaudithook(audit)


class ReplayClock:
    def __init__(self, now):
        self.seconds = now.timestamp()

    def time(self):
        return self.seconds

    def advance(self, seconds):
        self.seconds += seconds


def seed_catalog(runtime, scenario, stamp):
    bundle = json.loads((ROOT / "artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json").read_text())
    bundle["config_v2"] = scenario["catalog"]
    bundle["bundle_version"] = "local-cycle-lab-v1"
    runtime.ingest_bundle(bundle, activated_at=stamp)
    for item in scenario["catalog"]:
        key = f"test-{item['nm_id']}"
        runtime.save_nomenclature_item(dict(item_id=key, nm_id=item["nm_id"], our_sku=key,
            is_active=item["enabled"], created_at=stamp, updated_at=stamp))


def seed_buyer(runtime, replay):
    from packages.application import wb_buyer_authenticated_observations as buyer
    for day in replay.scenario["days"]:
        stamp = day + "T08:00:00Z"
        run_id = "lab-buyer-" + day
        buyer.begin_run(runtime, run_id=run_id, started_at=stamp, requested_nm_ids=replay.nm_ids)
        for nm in replay.nm_ids:
            raw = {**replay.scenario["authenticated_buyer"], "status": "observed",
                   "session_status": "authenticated_surface", "authenticated_session_proof": True,
                   "measured_at": stamp, "session_checked_at": stamp, "profile_reference": "offline",
                   "auth_run_reference": run_id}
            replay.observe("wb_buyer_authenticated", raw, 1, boundary="buyer_observation")
            buyer.append_observation(runtime, run_id=run_id,
                observation=buyer.classify_observation(nm_id=nm, buyer=raw, seller=None))
        buyer.finish_run(runtime, run_id=run_id, finished_at=stamp, status="completed")


def build_sources(runtime, replay, now, finance):
    from packages.application import sheet_vitrina_v1_live_plan as live
    from packages.application.promo_live_source import PromoLiveSourceBlock
    from packages.adapters.fin_report_daily_block import HttpBackedFinReportDailySource

    return live.SheetVitrinaV1LivePlanBlock(
        runtime, now_factory=lambda: now, current_web_source_sync=replay, closed_day_web_source_sync=replay,
        seller_funnel_block=live.SellerFunnelSnapshotBlock(replay.source("seller_funnel_snapshot")),
        web_source_block=live.WebSourceSnapshotBlock(replay.source("web_source_snapshot")),
        sales_funnel_history_block=live.SalesFunnelHistoryBlock(replay.source("sales_funnel_history")),
        spp_block=live.SppBlock(replay.source("spp")),
        spp_proxy_block=live.SppProxyBlock(replay.source("spp_proxy")),
        stocks_block=live.StocksBlock(replay.source("stocks")),
        onec_stocks_block=live.OnecStocksBlock(replay.source("onec_stocks"), stage_mapping=live.DEFAULT_ONEC_STAGE_MAPPING),
        fin_report_daily_block=live.FinReportDailyBlock(HttpBackedFinReportDailySource(client=finance)),
        promo_live_source_block=PromoLiveSourceBlock(runtime_dir=runtime.runtime_dir, collector_block=replay, now_factory=lambda: now),
    )


def run(path, scenario, replay, fresh, gate):
    from packages.adapters.wb_finance_api import WbFinanceApiClient
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from packages.application.sheet_vitrina_v1_cycle_sources import SheetVitrinaCycleSources
    from packages.application.sheet_vitrina_v1_cycle import validate_collection

    now = datetime.fromisoformat(scenario["today"] + "T08:00:00+00:00")
    stamp = now.isoformat().replace("+00:00", "Z")
    clock = ReplayClock(now)
    finance = WbFinanceApiClient("lab-only", request=replay.finance_request, rate_gate_root=gate,
                  wall_time=clock.time, monotonic=clock.time, sleep=clock.advance)
    runtime = RegistryUploadDbBackedRuntime(path)
    if fresh:
        seed_catalog(runtime, scenario, stamp)
        seed_buyer(runtime, replay)
        # Capture yesterday as a real current day. The next collection must
        # roll these accepted observations forward; never relabel today's data.
        previous = scenario["days"][0]
        prior_now = datetime.fromisoformat(previous + "T08:00:00+00:00")
        replay.phase = "bootstrap"
        with replay.on_day(previous):
            bootstrap = SheetVitrinaCycleSources(build_sources(runtime, replay, prior_now, finance))
            handle = bootstrap.collect_sources(
                as_of_date=(date.fromisoformat(previous) - timedelta(days=1)).isoformat(),
                execution_mode="auto_daily",
                source_keys=("card_rating", "prices_snapshot", "spp", "spp_proxy", "ads_bids"),
            )
            validate_collection(bootstrap.collected_source_summary(handle))
    replay.phase = "adapter_probes"
    from packages.adapters.wb_content import HttpBackedWbContentSource
    from packages.adapters.wb_fbs_orders import HttpBackedWbFbsOrdersSource
    catalog = HttpBackedWbContentSource().fetch_catalog_snapshot()
    fbs = HttpBackedWbFbsOrdersSource()
    warehouses = fbs.list_seller_warehouses()
    offices = fbs.list_offices()
    stock = fbs.list_stocks(warehouse_id=warehouses[0].warehouse_id, chrt_ids=catalog.cards[0].chrt_ids)
    weekly = finance.fetch_week(*[date.fromisoformat(day) for day in scenario["finance_week"]])
    replay.phase = "collect_and_publish"
    block = build_sources(runtime, replay, now, finance)
    sources = SheetVitrinaCycleSources(block)
    as_of = scenario["days"][0]
    handle = sources.collect_sources(as_of_date=as_of, execution_mode="auto_daily")
    summary = sources.collected_source_summary(handle)
    validate_collection(summary, require_full_scope=True)
    if replay.unexpected:
        raise ValueError(replay.unexpected)
    plan = sources.derive_collected(handle)
    state = runtime.load_current_state()
    expected = runtime.prepare_sheet_vitrina_ready_publication(bundle_version=state.bundle_version, as_of_date=as_of)
    runtime.save_sheet_vitrina_ready_snapshot(current_state=state, refreshed_at=stamp, plan=plan,
                                               expected=expected, build_inputs=plan.metadata["publication_inputs"])
    published = runtime.load_sheet_vitrina_ready_snapshot()
    if published.snapshot_id != plan.snapshot_id:
        raise ValueError("Прочитана другая версия витрины")
    data = next(sheet for sheet in published.sheets if sheet.sheet_name == "DATA_VITRINA")
    cells = {row[1]: dict(zip(published.date_columns, row[2:])) for row in data.rows}
    finance_daily = {}
    for day in scenario["days"]:
        payload, _ = runtime.load_temporal_source_slot_snapshot(
            source_key="fin_report_daily", snapshot_date=day, snapshot_role="accepted_closed_day_snapshot"
            if day != scenario["today"] else "accepted_current_snapshot")
        finance_daily[day] = {"storage_total": payload.storage_total.fin_storage_fee_total,
                             "buyout_rub": {str(item.nm_id): item.fin_buyout_rub for item in payload.items}}
    return {"as_of": published.as_of_date, "snapshot": published.snapshot_id,
            "date_columns": published.date_columns,
            "source_slots": [{k: slot[k] for k in ("source_key","date","kind","accepted","policy")}
                             | {k: slot[k] for k in ("requested_count", "covered_count", "missing_count") if k in slot}
                             for slot in summary["slots"]],
            "finance_week_rows": len(weekly), "fbs": {"offices":len(offices), "chrt_id":stock[0].chrt_id, "amount":stock[0].amount}, "inputs": replay.totals,
            "finance_daily": finance_daily, "cells": cells,
            "cell_presentation": published.metadata.get("server_cell_presentation", {})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("normal","zero","missing"), default="normal")
    parser.add_argument("--scenario", type=Path, default=SCENARIO, help="JSON со связанными тестовыми входами и ожиданиями")
    parser.add_argument("--state", type=Path, help="Отдельная новая папка для сохранения тестовой базы")
    args = parser.parse_args()
    violations = []
    offline_guard(violations)
    scenario_bytes = args.scenario.read_bytes()
    scenario = json.loads(scenario_bytes)
    # The simulator never uses the host's production environment.
    os.environ.clear()
    os.environ.update(TZ="UTC", ONEC_STOCKS_ACCOUNT_ID="000000001", WB_API_TOKEN="lab-only")
    started = time.monotonic()
    with ExitStack() as stack:
        tmp = Path(stack.enter_context(TemporaryDirectory(prefix="wbc-lab-")))
        path = tmp / "state" if args.state is None else args.state.absolute()
        # Manual UI runtimes have a lifetime writer lock and may also live
        # inside a reset archive. Only the supervisor can open their live DB.
        if path.resolve().name == "runtime" and (path.resolve().parent / "lab.json").exists():
            parser.error("Эта база принадлежит ручному стенду. Используйте local serve и кнопку проверки.")
        fresh = not path.exists()
        marker = path / "wbc-local-scenario.json"
        identity = {"fixture": hashlib.sha256(scenario_bytes).hexdigest(), "variant":args.variant}
        if fresh:
            path.mkdir(parents=True, mode=0o700)
        elif not marker.is_file() or json.loads(marker.read_text()) != identity:
            parser.error("Папка уже существует и не принадлежит этому сценарию. Выбери новую папку.")
        replay = Replay(scenario, args.variant)
        with patch("urllib.request.urlopen", HttpTape(replay).open):
            result = run(path, scenario, replay, fresh, tmp / "rate-gate")
        if violations:
            raise ValueError(f"Непредусмотренная внешняя операция: {violations}")
        result["verification"] = verify(result, scenario, args.variant)
        if fresh:
            marker.write_text(json.dumps(identity))
        result["seconds"] = round(time.monotonic()-started,3)
        result["state_bytes"] = sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        result["fixture_bytes"] = len(scenario_bytes)
        result["input_boundaries"] = replay.boundaries
        result["phase_totals"] = replay.phase_totals
        result["volume_method"] = "UTF-8 compact JSON payloads; calls include injected inputs; pages include terminal responses; excludes headers, TLS and browser traffic"
        print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    main()
