"""Readbacks and fault boundary for the isolated, already-posted FBS expense."""

from collections import Counter
from contextlib import contextmanager
from decimal import Decimal
import json
from unittest.mock import patch

from packages.application import ff_pool_documents as documents
from packages.application import operator_ff_overhead as overhead
from packages.application.ready_publication import readonly
from packages.application.warehouse_update_journal import PHASES
from .full_cycle import expect, SCENARIO
from .full_sources import bind_warehouse_sources, FullHttpTape, ReplayClock
from .sources import Replay
from .stage_replay import posted_ledger
from .state import read_json
from .full_warehouse import NM, QUANTITY, EXPENSE, expected

COMMITTED_PHASES = {
    "recovery_retention_before",
    "wb_supply_registry",
    "transit_enrichment",
    "cost_materialization",
    "ff_ledger_reservations",
}


def journal_state(entry, run_id):
    with readonly(entry.runtime.db_path) as conn:
        run = conn.execute(
            "SELECT status,trigger_source FROM sheet_vitrina_v1_warehouse_update_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        phases = dict(
            conn.execute(
                "SELECT phase_key,status FROM sheet_vitrina_v1_warehouse_update_phases WHERE run_id=?",
                (run_id,),
            )
        )
    expect(
        run and run["trigger_source"] == "cycle" and set(phases) == set(PHASES),
        "Warehouse journal association missing",
    )
    return dict(run_id=run_id, status=run["status"], phases=phases)


def read_post(entry, request_id):
    """Read committed financial identities separately from their mutable completion state."""
    with readonly(entry.runtime.db_path) as conn:
        request = conn.execute(
            f"SELECT {overhead.COMPLETION_REQUEST_COLUMNS},state FROM {documents.REQUESTS_TABLE} "
            "WHERE request_id=?",
            (request_id,),
        ).fetchone()
        expect(request and request["posted_document_id"], "Native posted request missing")
        overhead.assert_native_confirmation(conn, request)
        (doc,) = conn.execute(f"SELECT * FROM {documents.DOCUMENTS_TABLE} WHERE request_id=?", (request_id,))
        (move,) = conn.execute(
            "SELECT * FROM sheet_vitrina_v1_ff_pool_movement_lines WHERE operation_id=?",
            (doc["operation_id"],),
        )
        (line,) = conn.execute(
            f"SELECT amount_rub FROM {documents.EXPENSE_LINES_TABLE} WHERE document_id=?",
            (doc["document_id"],),
        )
        (pool,) = conn.execute(
            "SELECT quantity,capital_rub FROM sheet_vitrina_v1_ff_pool_balances "
            "WHERE facility_id=? AND pool=? AND nm_id=?",
            (move["facility_id"], move["pool"], move["nm_id"]),
        )
        (confirmation,) = conn.execute(
            f"SELECT source_digest,state,reason_code FROM {overhead.TABLE} WHERE request_id=?", (request_id,)
        )
        (queue,) = conn.execute(
            f"SELECT queue_id,status FROM {documents.TARGETED_RECALC_QUEUE_TABLE} WHERE stable_source_id=?",
            ("pool_overhead:" + doc["document_id"],),
        )
        (recovery,) = conn.execute(
            "SELECT lifecycle_state,source_digest FROM sheet_vitrina_v1_recovery_operations "
            "WHERE operation_id=?",
            (request["recovery_operation_id"],),
        )
        (event,) = conn.execute(
            f"SELECT event_id FROM {documents.WORKFLOW_EVENTS_TABLE} WHERE identity=? AND "
            "stage='document_committed' AND status='complete'",
            (request_id,),
        )
    manifest = json.loads(doc["posted_manifest_json"])
    expect(
        doc["posted_manifest_sha256"] == documents._fingerprint(manifest)
        and manifest["request_id"] == request_id
        and manifest["document_id"] == request["posted_document_id"]
        and manifest["domain"]["scope"] == "FBS"
        and Decimal(manifest["domain"]["amount_rub"]) == EXPENSE
        and doc["source_revision"] == request["source_revision"]
        and doc["business_date"] == request["business_date"]
        and doc["document_kind"] == "pool_overhead"
        and doc["document_id"] == request["posted_document_id"]
        and recovery["source_digest"] == request["request_identity"]
        and move["pool"] == "FBS"
        and move["nm_id"] == NM
        and Decimal(str(move["quantity_delta"])) == 0
        and Decimal(str(move["capital_delta_rub"])) == Decimal(line["amount_rub"]) == EXPENSE
        and pool["quantity"] == QUANTITY
        and Decimal(pool["capital_rub"]) == Decimal(expected()["fbs_capital_rub"]),
        "Committed expense, quantity or capital differs from the accepted case",
    )
    return dict(
        identity=dict(
            request_id=request_id,
            source_digest=confirmation["source_digest"],
            document_id=doc["document_id"],
            operation_id=doc["operation_id"],
            manifest_sha256=doc["posted_manifest_sha256"],
            recovery_id=request["recovery_operation_id"],
            queue_id=queue["queue_id"],
            commit_event_id=event["event_id"],
            movement=dict(move),
            quantity=pool["quantity"],
            capital_rub=pool["capital_rub"],
            expense_rub=line["amount_rub"],
        ),
        request_state=request["state"],
        confirmation=confirmation["state"],
        reason=confirmation["reason_code"],
        recovery=recovery["lifecycle_state"],
        queue_status=queue["status"],
    )


def unfinished(entry, receipt, request_id):
    row = next(s for s in receipt["stages"] if s["stage"] == "warehouse")
    journal = journal_state(entry, row["durable_ref"])
    post = read_post(entry, request_id)
    expect(
        journal["status"] == "running"
        and journal["phases"] == {key: "success" if key in COMMITTED_PHASES else "pending" for key in PHASES}
        and post["request_state"] == "posted"
        and post["confirmation"] == "processing"
        and post["reason"] == "native_post_pending"
        and post["recovery"] == "mutation_running"
        and post["queue_status"] == "queued",
        "Unexpected post-commit boundary",
    )
    return dict(post=post, warehouse=journal)


def completed(entry, before, attempt):
    row = next(s for s in attempt["stages"] if s["stage"] == "warehouse")
    original = journal_state(entry, before["warehouse"]["run_id"])
    fresh = journal_state(entry, row["durable_ref"])
    post = read_post(entry, before["post"]["identity"]["request_id"])
    expect(
        original == {**before["warehouse"], "status": "interrupted"}
        and fresh["run_id"] != original["run_id"]
        and fresh["run_id"] == row["versions"]["warehouse_run"]
        and fresh["status"] == "success"
        and all(v == "success" for v in fresh["phases"].values()),
        "Warehouse recovery journals differ",
    )
    expect(
        post["identity"] == before["post"]["identity"]
        and post["confirmation"] == "completed"
        and post["request_state"] == post["queue_status"] == "complete"
        and post["recovery"] == "retained",
        "Warehouse did not finish the same posting",
    )
    return dict(post=post, original_warehouse=original, recovery_warehouse=fresh)


@contextmanager
def replay_sources(entry, source, before, ledger, reject):
    replay = Replay(read_json(SCENARIO), "normal")
    tape = FullHttpTape(replay)
    original_warehouse = entry._cycle_warehouse

    def open_source(*args, **kwargs):
        if replay.phase != "warehouse":
            return reject("warehouse source outside stage")()
        return tape.open(*args, **kwargs)

    def run_warehouse(*args):
        expect(replay.phase == "setup", "Repeated warehouse recovery")
        request_id = before["post"]["identity"]["request_id"]
        expect(
            unfinished(entry, source, request_id) == before and posted_ledger(entry.runtime) == ledger,
            "Warehouse changed before admitted recovery",
        )
        now = entry.now_factory()
        clock = ReplayClock(now, entry.runtime.runtime_dir / "lab-provider-clock.json")
        bind_warehouse_sources(entry, open_source, clock, now)
        replay.phase = "warehouse"
        try:
            with patch("urllib.request.urlopen", open_source):
                return original_warehouse(*args)
        finally:
            replay.phase = "finished"

    with (
        patch.object(entry, "_cycle_warehouse", run_warehouse),
        patch.object(
            documents.FfPoolDocumentService, "_post_once_under_writer_lock", reject("duplicate posting")
        ),
    ):
        yield replay
    expect(
        replay.phase == "finished"
        and not replay.unexpected
        and set(replay.totals)
        == {
            "canonical_supply_warehouses",
            "canonical_supplies",
            "fbs_offices",
            "canonical_box_tariffs",
            "canonical_warehouse_stocks",
        },
        f"Unexpected warehouse source calls: {list(replay.totals)}",
    )


@contextmanager
def observe_control(entry, replay, counts):
    original = entry._cycle_warehouse

    def run(*args):
        before = Counter({k: c["calls"] for k, c in replay.totals.items()})
        proof = original(*args)
        after = Counter({k: c["calls"] for k, c in replay.totals.items()})
        counts.update(after - before)
        return proof

    with patch.object(entry, "_cycle_warehouse", run):
        yield
