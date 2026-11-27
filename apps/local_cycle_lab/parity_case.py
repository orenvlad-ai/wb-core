"""Real manual dispatch and explicit stage recovery of a known pre-publication interruption."""

import argparse
from contextlib import contextmanager, ExitStack, nullcontext
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from urllib import request

from packages.application import business_data_cycle_dispatch as dispatch
from packages.application.sheet_vitrina_v1_cycle import CycleReceiptStore, STAGES
from packages.application.sheet_vitrina_v1_cycle_sources import SheetVitrinaCycleSources
from packages.application.sheet_vitrina_v1_closed_backlog import ClosedBacklog
from .cycle_warnings import check_warnings, UnexpectedCycleWarning
from .full_cycle import execute, expect, finish_cycle, publication_cells, verify_cells, SCENARIO
from .full_form import serving
from .full_history import isolation_evidence, published_history
from .full_sources import configure_sources
from .full_warehouse import verify
from .sources import Replay
from . import publication_checkpoint, warehouse_post
from .stage_replay import invoke, posted_ledger
from .state import atomic_json, read_json

WORK = Path("/var/lib/wbc-lab/parity-case/evidence")
REQUEST = "local-expense-1"
CRASH = 84
BOUNDARIES = dict(
    rolling14="before_history_effects",
    daily_projection="before_daily_projection_effects",
    warehouse="after_overhead_post_commit",
)


def add_interruption_argument(parser):
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--interrupt-before", dest="stage", choices=("rolling14", "daily_projection"), default="rolling14"
    )
    group.add_argument(
        "--interrupt-after-warehouse-post", dest="stage", action="store_const", const="warehouse"
    )


def db_identity(entry):
    f = Path(entry.runtime.db_path).stat()
    return [f.st_dev, f.st_ino]


def checkpoint(entry, receipt, captures):
    stage = receipt["current_stage"]
    marker = dict(
        observed_at=datetime.now(timezone.utc).isoformat(),
        pid=os.getpid(),
        db=db_identity(entry),
        cycle=receipt["cycle_id"],
        job=receipt["job_id"],
        stage=receipt["current_stage"],
        boundary=BOUNDARIES[stage],
        ledger=posted_ledger(entry.runtime),
        predecessors=receipt["final_versions"],
    )
    if stage == "warehouse":
        marker["warehouse_post"] = warehouse_post.unfinished(
            entry, receipt, read_json(WORK / "before.json")["operation_id"]
        )
    if stage != "rolling14":
        marker["collection_sha256"] = publication_checkpoint.save(
            entry, receipt, captures, WORK / "collection.json"
        )
    atomic_json(WORK / "interruption.json", marker)
    os._exit(CRASH)


@contextmanager
def interrupt_cycle(entry, stage):
    # Native dispatch persists the running stage before it calls its first action.
    original_write = CycleReceiptStore.write
    capture = publication_checkpoint.capture_collection() if stage != "rolling14" else nullcontext()
    with capture as captures, ExitStack() as hooks:
        warehouse_receipt = None

        def write(store, receipt):
            nonlocal warehouse_receipt
            original_write(store, receipt)
            if receipt["status"] != "running" or receipt["current_stage"] != stage:
                return
            row = next(s for s in receipt["stages"] if s["stage"] == stage)
            if row["status"] == "running":
                if stage == "warehouse":
                    if row.get("durable_ref"):
                        warehouse_receipt = receipt
                else:
                    checkpoint(entry, receipt, captures)

        hooks.enter_context(patch.object(CycleReceiptStore, "write", write))
        if stage == "warehouse":
            service = warehouse_post.documents.FfPoolDocumentService
            original_post = service._post_once_under_writer_lock
            request_id = read_json(WORK / "before.json")["operation_id"]

            def post(owner, identity, **kwargs):
                result = original_post(owner, identity, **kwargs)
                if identity == request_id and kwargs.get("defer_confirmed_overhead_projection"):
                    expect(warehouse_receipt is not None, "No durable warehouse run at post")
                    try:
                        checkpoint(entry, warehouse_receipt, captures)
                    except Exception:
                        # The native drainer handles ordinary posting errors. A broken
                        # test hook must stop this disposable branch immediately.
                        import traceback

                        traceback.print_exc()
                        os._exit(85)
                return result

            hooks.enter_context(patch.object(service, "_post_once_under_writer_lock", post))
        yield


def history_values(config):
    history, edition_id, edition = published_history(config)
    days = sorted(edition["days"])
    cells = {}
    for scope in ("sku", "total"):
        offset = 0
        while True:
            page = history.read(
                date_from=days[0],
                date_to=days[-1],
                scope=scope,
                edition_id=edition_id,
                offset=offset,
                limit=40,
            )
            for row in page["rows"]:
                # Provider timestamps and snapshot IDs legitimately differ across independent cycles.
                # Compare every row ID, date, raw value and display value including missing cells.
                expect(row["row_id"] not in cells, "Duplicate History row")
                cells[row["row_id"]] = {day: cell[:2] for day, cell in row["cells"].items()}
            offset += len(page["rows"])
            if len(page["rows"]) < 40:
                break
    return cell_digest(cells, days)


def cell_digest(cells, days):
    expect(
        cells and days and all(set(values) == set(days) for values in cells.values()),
        "Incomplete publication rows",
    )
    digest = hashlib.sha256(json.dumps(cells, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return dict(rows=len(cells), dates=len(days), cells=sum(map(len, cells.values())), sha256=digest)


def published_tables(entry, as_of, history_days):
    from packages.adapters import registry_upload_http_entrypoint as web
    from packages.application.web_vitrina_history_compiler import unpack_table

    plan = entry.runtime.load_sheet_vitrina_ready_snapshot(as_of_date=as_of)
    rows = next(s.rows for s in plan.sheets if s.sheet_name == "DATA_VITRINA")
    ready = {row[1]: dict(zip(plan.date_columns, row[2:], strict=True)) for row in rows}
    expect(len(ready) == len(rows), "Duplicate Ready row")
    page = entry.handle_sheet_web_vitrina_page_composition_request(
        page_route=web.DEFAULT_SHEET_WEB_VITRINA_UI_PATH,
        read_route=web.DEFAULT_SHEET_WEB_VITRINA_READ_PATH,
        operator_route=web.DEFAULT_SHEET_OPERATOR_UI_PATH,
        date_from=history_days[0],
        date_to=history_days[-1],
        include_table_data=True,
        table_format="indexed_cells_v2",
    )
    _, dated = unpack_table(page["table_surface"])
    visible = {row: {day: cell[:2] for day, cell in by_day.items()} for row, by_day in dated.items()}
    return dict(ready=cell_digest(ready, plan.date_columns), vitrina=cell_digest(visible, history_days))


def values(entry, receipt, config):
    from packages.application.operator_ff_overhead import journal

    operation = read_json(WORK / "before.json")["operation_id"]
    business = verify(entry, operation)
    expected = read_json(WORK / "opening.json")["expected_cells"]
    publication = publication_cells(entry, config, receipt, expected, read_json(SCENARIO)["today"])
    verify_cells(publication, expected)
    expect(journal(entry.runtime.db_path)["total"] == 1, "Expense confirmation duplicated")
    history = history_values(config)
    tables = published_tables(entry, receipt["final_versions"]["as_of_date"], publication["history_dates"])
    expect(tables["vitrina"] == history, "Visible window differs from the published History values")
    return dict(business=business, publication=publication, history_values=history, tables=tables)


def recover(entry, config, store, stage, marker):
    with serving(entry, 8765):
        status = dispatch.manual_status(dispatch.RUNTIME, REQUEST)
    source = store.read(marker["cycle"])
    index = STAGES.index(stage)
    expect(
        status["status"] == source["status"] == "interrupted"
        and source["error_code"] == "cycle_owner_lost"
        and status["receipt"]["job_id"] == source["job_id"] == marker["job"]
        and source["current_stage"] == marker["stage"] == stage
        and marker["pid"] != os.getpid()
        and marker["boundary"] == BOUNDARIES[stage]
        and marker["db"] == db_identity(entry)
        and source["final_versions"] == marker["predecessors"]
        and source["stages"][index]["error_code"] == "outcome_uncertain"
        and all(row["status"] == "pending" for row in source["stages"][index + 1 :]),
        "Interrupted request, process or database identity differs",
    )
    check_warnings(source, 1, before_stage=stage)
    expect(
        not (config.candidate_root / "history/CURRENT.json").exists(), "Possible prior History publication"
    )
    ledger = posted_ledger(entry.runtime)
    if stage != "rolling14" or "ledger" in marker:
        expect(ledger == marker["ledger"], "Posted ledger changed across restart")
    warehouse = stage == "warehouse"
    if warehouse:
        before_post = warehouse_post.unfinished(
            entry, source, read_json(WORK / "before.json")["operation_id"]
        )
        expect(before_post == marker["warehouse_post"], "Committed expense or warehouse receipt changed")
    if stage != "rolling14":
        publication_checkpoint.read_sealed(WORK / "collection.json", marker["collection_sha256"])
    atomic_json(WORK / "interrupted-receipt.json", source)
    # An unknown outcome of this repair itself must be examined before another attempt.
    claim = "history-repair.claim" if stage == "rolling14" else "publication-repair.claim"
    with (WORK / claim).open("x") as f:
        f.write(source["cycle_id"])
        f.flush()
        os.fsync(f.fileno())
    folder = os.open(WORK, os.O_DIRECTORY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)
    forbidden = []

    def reject(name):
        def denied(*args, **kwargs):
            forbidden.append(name)
            raise AssertionError("Recovery repeated " + name)

        return denied

    started = time.monotonic()
    with ExitStack() as guards:
        guards.enter_context(patch("urllib.request.urlopen", reject("source HTTP")))
        if warehouse:
            replay = guards.enter_context(
                warehouse_post.replay_sources(entry, source, before_post, ledger, reject)
            )
        else:
            guards.enter_context(patch.object(entry, "_cycle_warehouse", reject("_cycle_warehouse")))
        for name in ("_cycle_finance_sources", "_cycle_fbs_generation"):
            guards.enter_context(patch.object(entry, name, reject(name)))
        guards.enter_context(patch.object(SheetVitrinaCycleSources, "collect_sources", reject("api_sources")))
        original_collect = ClosedBacklog.collect

        def guard_closed(backlog, *args, **kwargs):
            if kwargs.get("retained_only"):
                return original_collect(backlog, *args, **kwargs)
            return reject("closed_sources")()

        guards.enter_context(patch.object(ClosedBacklog, "collect", guard_closed))
        attempt = invoke(
            entry,
            config,
            source,
            stage,
            hashlib.sha256((WORK / "interruption.json").read_bytes()).hexdigest(),
            output=WORK,
            collection_hash=marker.get("collection_sha256"),
        )
    seconds = time.monotonic() - started
    after_ledger = posted_ledger(entry.runtime)
    warehouse_result = {}
    if warehouse:
        finished = warehouse_post.completed(entry, before_post, attempt)
        warehouse_result = dict(
            before=before_post, after=finished, source_calls={k: c["calls"] for k, c in replay.totals.items()}
        )
        # The confirmation has now advanced to completed. Its immutable identity
        # and count are checked separately from its legitimate receipt update.
        for name in ("ff_pool_documents", "ff_pool_movement_lines", "ff_pool_balances"):
            expect(ledger[name] == after_ledger[name], "Recovered warehouse changed posted rows: " + name)
        expect(
            ledger["ff_pool_overhead_confirmations"]["rows"]
            == after_ledger["ff_pool_overhead_confirmations"]["rows"]
            == 1,
            "Confirmation count changed",
        )
    else:
        expect(ledger == after_ledger, "Publication replay changed the FF ledger")
    expect(
        not forbidden
        and attempt["job_id"] != source["job_id"]
        and [s["stage"] for s in attempt["stages"]] == list(STAGES[index:]),
        "Unexpected repair effects",
    )
    joined = source["stages"][:index] + attempt["stages"]
    check_warnings(
        dict(
            stages=joined,
            status="degraded" if any(s["warnings"] for s in joined) else "complete",
            business_date=source["business_date"],
        ),
        1,
    )
    expect(
        attempt["status"] == ("degraded" if any(s["warnings"] for s in attempt["stages"]) else "complete"),
        "Recovery terminal status differs",
    )
    result = values(entry, attempt, config)
    expect(store.read(source["cycle_id"])["status"] == "interrupted", "Original failure receipt overwritten")
    atomic_json(
        WORK / "recovered.json",
        dict(
            **result,
            stage=stage,
            seconds=seconds,
            completed_prefix=list(STAGES[:index]),
            executed=[dict(stage=s["stage"], status=s["status"]) for s in attempt["stages"]],
            forbidden_calls=forbidden,
            warehouse_post=warehouse_result,
            collection_sha256=marker.get("collection_sha256"),
            closed_backfill_dates=json.loads(attempt["final_versions"]["closed_backfill_dates"]),
            original_status=source["status"],
            original_job=source["job_id"],
            recovery_job=attempt["job_id"],
            db=marker["db"],
            ledger=ledger,
            original_pid=marker["pid"],
            recovery_pid=os.getpid(),
            observed_at=entry.now_factory().isoformat(),
        ),
    )


def reject_changed_post(entry, config, store, marker):
    from copy import deepcopy

    expect(
        not (WORK / "attempt.json").exists() and not (WORK / "publication-repair.claim").exists(),
        "Negative control requires the original interrupted case",
    )
    changed = deepcopy(marker)
    changed["warehouse_post"]["post"]["identity"]["document_id"] += "-other"
    try:
        recover(entry, config, store, "warehouse", changed)
    except ValueError as exc:
        if str(exc) != "Committed expense or warehouse receipt changed":
            raise
    else:
        raise ValueError("Recovery accepted a different posted document")
    original = store.read(marker["cycle"])
    expect(
        not (WORK / "attempt.json").exists()
        and not (WORK / "publication-repair.claim").exists()
        and posted_ledger(entry.runtime) == marker["ledger"]
        and warehouse_post.unfinished(entry, original, read_json(WORK / "before.json")["operation_id"])
        == marker["warehouse_post"],
        "Rejected evidence affected the case",
    )
    atomic_json(
        WORK / "rejected-post.json",
        dict(
            changed="document_id",
            before_repair=True,
            original_job=marker["job"],
            original_run=marker["warehouse_post"]["warehouse"]["run_id"],
        ),
    )


def run(action, stage):
    from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
    from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
    from packages.adapters import registry_upload_http_entrypoint as web
    from . import parity_profile
    from .captured_fullstats import verify as verify_fullstats
    from apps.web_vitrina_history_candidate_build import runtime_storage_admission

    expect(not isolation_evidence()["host_mounts"], "Unexpected host mount")
    os.environ["SELLER_PORTAL_CANONICAL_SUPPLIER_ID"] = "canonical"
    config = dispatch.history_config()
    runtime_storage_admission(config.candidate_root, config.runtime_contract, config.formula_epoch)
    if action == "seed":
        for name in (".web-vitrina-finished-builder.lock", ".wb-finance-daily-worker.lock"):
            (dispatch.RUNTIME / name).touch(mode=0o600)
        execute(
            WORK,
            runtime_path=dispatch.RUNTIME,
            history_config=config,
            select_profile=parity_profile.select,
            prepare_only=True,
        )
        atomic_json(WORK / "auth.json", os.environ["WB_CORE_WEB_AUTH_SESSION_SECRET"])
        atomic_json(
            WORK / "capture-result.json", verify_fullstats(Path("/var/lib/wbc-lab/parity-input.json"))
        )
        atomic_json(WORK / "seed-clock.json", dict(observed_at=datetime.now(timezone.utc).isoformat()))
        return
    secret = read_json(WORK / "auth.json")
    os.environ.update(
        WB_CORE_WEB_AUTH_REQUIRED="1",
        WB_CORE_WEB_AUTH_USERNAME="local_operator",
        WB_CORE_WEB_AUTH_PASSWORD_HASH=web._hash_pbkdf2_password("lab-only"),
        WB_CORE_WEB_AUTH_SESSION_SECRET=secret,
        WB_API_TOKEN="lab-only",
        ONEC_STOCKS_ACCOUNT_ID="000000001",
        REGISTRY_UPLOAD_RUNTIME_DIR=str(dispatch.RUNTIME),
    )
    now = lambda: datetime.now(timezone.utc)
    stamp = lambda: now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    entry = RegistryUploadHttpEntrypoint(
        runtime_dir=dispatch.RUNTIME,
        runtime=RegistryUploadDbBackedRuntime(dispatch.RUNTIME),
        now_factory=now,
        activated_at_factory=stamp,
        refreshed_at_factory=stamp,
    )
    entry.operator_jobs.enable_snapshot_admission(dispatch.RUNTIME)
    store = CycleReceiptStore(dispatch.RUNTIME, stamp)
    if action == "reject-post":
        reject_changed_post(entry, config, store, read_json(WORK / "interruption.json"))
        return
    if action == "recover":
        recover(entry, config, store, stage, read_json(WORK / "interruption.json"))
        return
    day = read_json(SCENARIO)
    replay = Replay(day, "missing" if action == "negative" else "normal")
    original = request.urlopen
    number = 2 if action == "repeat" else 1
    request_id = f"local-expense-{number}"
    started = time.monotonic()
    with configure_sources(entry, replay, datetime.fromisoformat(day["today"] + "T08:00:00+00:00")) as io:

        def transport(req, **kwargs):
            if req.full_url.partition("?")[0] == dispatch.BASE_URL + dispatch.PATH:
                return original(req, **kwargs)
            return io.tape.open(req, **kwargs)

        source_counts = {}
        fault = nullcontext()
        if action == "crash":
            fault = interrupt_cycle(entry, stage)
        elif stage == "warehouse" and action == "control":
            fault = warehouse_post.observe_control(entry, replay, source_counts)
        with patch("urllib.request.urlopen", transport), serving(entry, 8765), fault:
            accepted = dispatch.launch(dispatch.RUNTIME, manual_request_id=request_id)
            expect(
                accepted.get("accepted") is True, f"Native manual dispatch refused: {accepted.get('status')}"
            )
            try:
                receipt, seconds = finish_cycle(entry, WORK, number, accepted["receipt"], started)
            except UnexpectedCycleWarning as error:
                if action != "negative" or error.stage != "api_sources":
                    raise
                receipt = store.read(accepted["receipt"]["cycle_id"])
                check_warnings(receipt, 1, missing_ads=True)
                expect(
                    not replay.unexpected
                    and set(io.tape.injected_errors) == {("ads_compact", day["days"][0], 503)},
                    "Unexpected transport failure or missing deliberate HTTP 503",
                )
                entry._cycle_verify_ready(receipt["final_versions"])
                values(entry, receipt, config)
                atomic_json(
                    WORK / "rejected.json",
                    dict(
                        stage=error.stage,
                        unexpected=[json.loads(w) for w in error.unexpected],
                        missing=[json.loads(w) for w in error.missing],
                        injected_http=io.tape.injected_errors,
                        native_status=dispatch.manual_status(dispatch.RUNTIME, REQUEST)["status"],
                    ),
                )
                return
            expect(action in ("control", "repeat"), "Fault failed to trigger")
            status = dispatch.manual_status(dispatch.RUNTIME, request_id)
            expect(
                status["status"] == receipt["status"] and status["receipt"]["job_id"] == receipt["job_id"],
                "Native HTTP readback differs",
            )
            expect(not replay.unexpected, "Unexpected provider request")
            destination = "repeat.json" if number == 2 else "uninterrupted.json"
            atomic_json(
                WORK / destination,
                dict(
                    **values(entry, receipt, config),
                    seconds=seconds,
                    warehouse_source_calls=source_counts,
                    job=receipt["job_id"],
                    status=receipt["status"],
                    dispatch=dict(
                        route=dispatch.PATH,
                        request_id=request_id,
                        trigger=receipt["trigger_kind"],
                        accepted=status["accepted"],
                        cycle_id=status["receipt"]["cycle_id"],
                    ),
                ),
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("seed", "control", "crash", "recover", "negative", "repeat", "reject-post")
    )
    add_interruption_argument(parser)
    args = parser.parse_args()
    run(args.action, args.stage)
