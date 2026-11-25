"""Run the 123.45 RUB control twice in a disposable private Linux guest."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import os
from pathlib import Path
import secrets
import time
from unittest.mock import patch

from .cycle_warnings import check_warnings
from .full_history import isolation_evidence, prepare_history, published_history
from .state import atomic_json, read_json

SCENARIO = Path(__file__).with_name("scenario.json")


def expect(condition, message):
    if not condition:
        raise ValueError(message)


def manual_cycle(entry, config, output, number):
    started = time.monotonic()
    receipt = entry._start_sheet_cycle_job(request_key=f"manual-cycle:local-expense-{number}",
        slot_utc=entry.now_factory().isoformat(), history_config=config)
    expect(receipt.get("cycle_id") and receipt.get("job_id"), f"Native admission refused: {receipt}")
    return finish_cycle(entry, output, number, receipt, started)


def finish_cycle(entry, output, number, receipt, started):
    from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore
    store = CycleReceiptStore(entry.runtime.runtime_dir, entry.activated_at_factory)
    thread = entry.operator_jobs._threads[receipt["job_id"]]
    last = None
    while thread.is_alive():
        current = store.read(receipt["cycle_id"])
        stage = current.get("current_stage")
        if stage and stage != last:
            print(f"Cycle {number}: {stage}", flush=True)
            last = stage
        thread.join(0.25)
    receipt = store.read(receipt["cycle_id"])
    atomic_json(output / f"cycle-{number}.json", receipt)
    log, _ = entry.operator_jobs.get_text(receipt["job_id"])
    (output / f"cycle-{number}.log").write_text(log)
    expect(receipt.get("status") in {"complete", "degraded"},
           f"Cycle {number} stopped at {receipt.get('current_stage')}: {receipt.get('error_code')}")
    check_warnings(receipt, number)
    expect(receipt["trigger_kind"] == "manual", "Manual cycle identity missing")
    versions = receipt["final_versions"]
    expect(versions.get("fbs_accounting_status") != "unavailable" and versions.get("fbs_book")
           and versions.get("fbs_ready_operation"),
           "Current FBS accounting proof missing")
    entry._cycle_verify_ready(versions)
    return receipt, round(time.monotonic() - started, 3)


def stored_ready_cells(entry, expected, day, *, as_of_date=None):
    expect(any(key.startswith("SKU:") for key in expected) and
           any(key.startswith("TOTAL|") for key in expected),
           "The control must include SKU and total amounts")
    plan = entry.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of_date)
    sheet = next(s for s in plan.sheets if s.sheet_name == "DATA_VITRINA")
    offset = plan.date_columns.index(day) + 2
    raw = {r[1]: r[offset] for r in sheet.rows if r[1] in expected}
    return plan.snapshot_id, raw


def publication_cells(entry, config, receipt, expected, day):
    """Read the stored Ready, rendered native table, and exact published History edition."""
    from packages.adapters import registry_upload_http_entrypoint as web
    from packages.application.web_vitrina_history_compiler import unpack_table

    versions = receipt["final_versions"]
    snapshot_id, raw = stored_ready_cells(entry, expected, day, as_of_date=versions["as_of_date"])
    page = entry.handle_sheet_web_vitrina_page_composition_request(
        page_route=web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH, read_route=web.DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
        operator_route=web.DEFAULT_SHEET_OPERATOR_UI_PATH, date_from=day, date_to=day,
        include_table_data=True, table_format="indexed_cells_v2")
    _, dated = unpack_table(page["table_surface"])
    visible = {key: dated[key][day] for key in expected}
    history, edition_id, edition = published_history(config)
    expect(edition_id == versions["history_edition_id"], "Published History pointer does not match receipt")
    dates = [(datetime.fromisoformat(day) - timedelta(days=i)).date().isoformat() for i in range(14)]
    expect(set(dates) <= set(edition["days"]), "Rolling 14-day edition is incomplete")
    saved = {}
    for scope, prefix in (("sku", "SKU:"), ("total", "TOTAL|")):
        keys = [key for key in expected if key.startswith(prefix)]
        if keys:
            rows = history.read(date_from=day, date_to=day, scope=scope, row_ids=keys, edition_id=edition_id)
            expect(rows["availability"][day], "Published day has no accepted Ready")
            saved.update({row["row_id"]: row["cells"][day] for row in rows["rows"]})
    return {"ready_snapshot": snapshot_id, "ready_publication": versions["publication_operation_id"],
            "ready_cells": raw, "vitrina_cells": visible, "history_cells": saved,
            "history_edition": edition_id, "history_day": edition["days"][day],
            "history_dates": sorted(edition["days"])}


def verify_surface(surface, cells, expected):
    expect(set(cells) == set(expected), f"{surface}: incomplete control rows")
    for key, value in expected.items():
        cell = cells[key]
        numeric = cell if surface == "ready_cells" else cell[0]
        expect(numeric not in ("", None), f"{surface}: missing {key}")
        actual = Decimal(str(numeric))
        wanted = Decimal(str(value))
        if key.endswith(("_rub", "_rub_v1")):
            actual = actual.quantize(Decimal("0.01"), ROUND_HALF_UP)
            wanted = wanted.quantize(Decimal("0.01"), ROUND_HALF_UP)
        expect(actual == wanted, f"{surface} {key}: expected {wanted}, observed {actual}")


def verify_cells(publication, expected):
    # The rendered table and History store use [numeric value, display text, ...].
    for surface in ("ready_cells", "vitrina_cells", "history_cells"):
        verify_surface(surface, publication[surface], expected)
    expect(publication["history_cells"] == publication["vitrina_cells"],
           "Stored History cells and their presentation differ from the operator table")


def execute(work, *, repeat=True, runtime_path=None, prepare_only=False,
            history_config=None, select_profile=None):
    from packages.adapters import registry_upload_http_entrypoint as web
    from packages.application import operator_ff_overhead
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
    from .full_form import enter_expense
    from .full_sources import configure_sources
    from .full_warehouse import prepare, verify
    from .sources import Replay

    isolation = isolation_evidence()
    expect(not isolation["host_mounts"], "Unexpected host mount in guest")
    os.umask(0o077)
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime_path = runtime_path or work / "runtime"
    origin = {"fixture_sha256": hashlib.sha256(SCENARIO.read_bytes()).hexdigest(), "variant": "normal",
              "lab_code_sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in sorted(SCENARIO.parent.glob("*.py"))}}
    scenario = read_json(SCENARIO)
    replay = Replay(scenario, "normal")
    anchor = datetime.fromisoformat(scenario["today"] + "T08:00:00+00:00")
    now = lambda: datetime.now(timezone.utc)
    expect(abs((now() - anchor).total_seconds()) < 60, "Full cycle requires the fixture business clock")
    stamp = lambda: now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    username, password = "local_operator", secrets.token_urlsafe(24)
    os.environ.update(WB_API_TOKEN="lab-only", ONEC_STOCKS_ACCOUNT_ID="000000001",
        WB_CORE_WEB_AUTH_REQUIRED="1", WB_CORE_WEB_AUTH_USERNAME=username,
        WB_CORE_WEB_AUTH_PASSWORD_HASH=web._hash_pbkdf2_password(password),
        WB_CORE_WEB_AUTH_SESSION_SECRET=secrets.token_urlsafe(48),
        REGISTRY_UPLOAD_RUNTIME_DIR=str(runtime_path))
    from .__main__ import run
    from .checks import verify as verify_initial_sources
    from .http import HttpTape
    bootstrap = Replay(scenario, "normal")
    with patch("urllib.request.urlopen", HttpTape(bootstrap).open):
        seed = run(runtime_path, scenario, bootstrap, True, work / "initial-rate-gate")
    origin["initial_ready"] = verify_initial_sources(seed, scenario, "normal")
    atomic_json(runtime_path / "wbc-local-scenario.json",
                {"fixture": origin["fixture_sha256"], "variant": "normal"})
    atomic_json(work / "input.json", origin)
    runtime = RegistryUploadDbBackedRuntime(runtime_path)
    from packages.application.business_data_procedure_admission import initialize_admission
    initialize_admission(runtime.runtime_dir)
    config = history_config or prepare_history(runtime.runtime_dir)
    entry = RegistryUploadHttpEntrypoint(runtime_dir=runtime.runtime_dir, runtime=runtime,
            now_factory=now, activated_at_factory=stamp, refreshed_at_factory=stamp)
    entry.operator_jobs.enable_snapshot_admission(runtime.runtime_dir)
    expect(operator_ff_overhead.journal(runtime.db_path)["total"] == 0, "Expected empty source journal")
    with configure_sources(entry, replay, anchor):
        print("Preparing opening stock and cost", flush=True)
        opening = prepare(entry)
        atomic_json(work / "opening.json", opening)
        if select_profile:
            atomic_json(work / "profile.json", select_profile(entry))
        operation = enter_expense(entry, facility_id=opening["facility_id"], now=anchor,
                                  username=username, password=password, evidence_dir=work)
        before = operator_ff_overhead.read_acceptance(runtime.db_path, operation)
        expect(before and before["state"] == "accepted" and before["document"] is None and
               before["business_date"] == scenario["today"] and before["summary"]["scope"] == "FBS"
               and before["summary"]["amount_rub"] == "123.45", "Unexpected initial source receipt")
        atomic_json(work / "before.json", before)
        if prepare_only:
            return before
        results = []
        for number in range(1, 3 if repeat else 2):
            receipt, seconds = manual_cycle(entry, config, work, number)
            business = verify(entry, operation)
            expected = opening["expected_cells"]
            publication = publication_cells(entry, config, receipt, expected, scenario["today"])
            item = {"cycle_id": receipt["cycle_id"], "job_id": receipt["job_id"], "seconds": seconds,
                    "status": receipt["status"], "business": business, "publication": publication}
            atomic_json(work / f"after-{number}.json", item)
            verify_cells(publication, expected)
            current = operator_ff_overhead.read_acceptance(runtime.db_path, operation)
            expect(current["state"] == "completed" and current["document"] and
                   current["source_document"] == before["source_document"], "Source posting not completed")
            expect(operator_ff_overhead.journal(runtime.db_path)["total"] == 1, "Duplicate source confirmation")
            results.append(item)
        if repeat:
            first, second = results
            expect(first["cycle_id"] != second["cycle_id"] and first["job_id"] != second["job_id"],
                   "Second request reused the first job")
            expect(first["business"] == second["business"], "Business documents or cost allocation changed on repeat")
            expect(first["publication"]["ready_cells"] == second["publication"]["ready_cells"],
                   "Published expense values changed on repeat")
        expect(not replay.unexpected, f"Unexpected source requests: {replay.unexpected}")
        result = {"status": "passed", "source": origin, "isolation": isolation, "operation_id": operation,
                  "opening": opening, "cycles": results, "calls": replay.totals}
        atomic_json(work / "comparison.json", result)
        print(f"Verified {len(results)} full cycle(s) for {operation}", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="New private guest directory for data and evidence")
    args = parser.parse_args()
    execute(args.work.resolve())


if __name__ == "__main__":
    main()
