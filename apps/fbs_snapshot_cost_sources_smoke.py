#!/usr/bin/env python3
"""Saved-source contract: complete stock, exact baseline, independent document money."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_official_fbs_smoke import fixture as stock_fixture
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.application.fbs_snapshot_cost import evaluate_candidate, initialize_candidate
from packages.application.warehouse_functional import _watermark

DAY = "2026-09-05"
NOW = datetime(2026, 9, 5, 10, 10, tzinfo=timezone.utc)
P = "sheet_vitrina_v1_"


def fixture(path):
    conn = stock_fixture(path)
    conn.executescript(f"""
        CREATE TABLE {P}nomenclature_items(item_id,nm_id,is_active,is_hidden,updated_at);
        INSERT INTO {P}nomenclature_items VALUES('main',1,1,0,'today');
        INSERT INTO {P}nomenclature_items VALUES('hidden',2,1,1,'today');
        CREATE TABLE {P}warehouse_functional_active(slot,version_id);
        INSERT INTO {P}warehouse_functional_active VALUES(1,'v1');
        CREATE TABLE {P}ff_pool_documents(document_id,document_kind,root_document_id,operation_id,
            source_system,source_type,source_id,source_revision,business_date,posted_at,posted_manifest_sha256);
        CREATE TABLE {P}ff_pool_document_lines(document_id,line_no,line_role,facility_id,pool,nm_id,
            quantity,capital_rub,expense_rub,metadata_json);
        CREATE TABLE {P}ff_pool_document_expense_lines(document_id,expense_line_no,amount_rub,basis,
            source_file_sha256,metadata_json);
        CREATE TABLE {P}ff_pool_document_relations(parent_document_id,child_document_id,relation_type);
        CREATE TABLE {P}ff_pool_movement_lines(operation_id,line_no,facility_id,pool,nm_id,quantity_delta,capital_delta_rub);
        CREATE TABLE {P}warehouse_business_operations(operation_id);
    """)
    for facility, pool, quantity, capital in [("A", "FBS", 10, "200"), ("B", "FBS", 20, "600"),
                                            ("B", "FBO", 4, "120")]:
        conn.execute(f"INSERT INTO {P}ff_pool_balances VALUES(?,?,?,?,?,?,?,?,?)",
                     (facility, pool, 1, 1, quantity, capital, str(int(capital)//quantity),
                      "opening", "2026-09-05T09:00:00Z"))
    bind_pool(conn)
    conn.commit()
    return conn


def bind_pool(conn):
    columns = ("facility_id", "pool", "nm_id", "projection_epoch", "quantity", "capital_rub",
               "wac_rub", "source_watermark", "updated_at")
    rows = [dict(zip(columns, row)) for row in conn.execute(
        f"SELECT {','.join(columns)} FROM {P}ff_pool_balances ORDER BY facility_id,pool,nm_id")]
    conn.execute(f"UPDATE {P}warehouse_functional_versions SET source_watermarks_json=?",
                 (json.dumps({"ff_pool_detail": _watermark(rows, "updated_at")}),))


def document(conn, identity, kind, *, role, q, capital, expense="0", facility="A", pool="FBS", day=DAY):
    conn.execute(f"INSERT INTO {P}ff_pool_documents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, kind, identity, identity, "fixture", kind, identity, "rev:" + identity,
                  day, "2026-09-05T10:05:00Z", "sha256:" + identity))
    conn.execute(f"INSERT INTO {P}warehouse_business_operations VALUES(?)", (identity,))
    conn.execute(f"INSERT INTO {P}ff_pool_document_lines VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (identity, 1, role, facility, pool, 1, q, capital, expense, "{}"))
    movement_capital = Decimal(capital) + (Decimal(expense) if kind == "china_acceptance" else 0)
    conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES(?,?,?,?,?,?,?)",
                 (identity, 1, facility, pool, 1, 0 if kind == "pool_overhead" else q, str(movement_capital)))


def check_legacy_expense_candidate_boundary(directory):
    path = Path(directory) / "legacy-expense-boundary.sqlite3"
    conn = fixture(path)
    document(conn, "absorbed-fee", "pool_overhead", role="overhead_allocation", q=987654321,
             capital="100", expense="100")
    conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('absorbed-fee',1,'100',"
                 "'Approved','sha256:old-evidence','{}')")
    conn.commit()
    initial_capture = capture_current(path, now=NOW)
    initial = initialize_candidate(initial_capture)
    initial_manifest = initial["baseline"]["absorbed_documents"].copy()
    assert "absorbed-fee" in initial_manifest
    next_day = "2026-09-06"
    next_now = NOW + timedelta(days=1)
    conn.execute(f"UPDATE {P}wb_fbs_stock_snapshot_runs SET snapshot_at=?", (next_day + "T10:00:00Z",))
    conn.commit()
    before_expense = evaluate_candidate(initial, capture_current(path, now=next_now, include_baseline=False))
    before_row = before_expense["periods"][next_day]["rows"]["A:1"]
    assert before_row["wac_rub"] == "20" and before_row["expense_capital_rub"] == "0"
    assert before_expense["pending_documents"] == []  # the old fee stays absorbed

    document(conn, "new-legacy-fee", "pool_overhead", role="overhead_allocation", q=123456789,
             capital="1000000", expense="1000000", day=next_day)
    conn.execute(f"UPDATE {P}ff_pool_documents SET posted_at=? WHERE document_id='new-legacy-fee'",
                 (next_day + "T10:05:00Z",))
    conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('new-legacy-fee',1,'1000000',"
                 "'Approved','sha256:new-evidence','{}')")
    conn.commit()
    captured = capture_current(path, now=next_now, include_baseline=False)
    event = next(d for d in captured["documents"] if d["document_id"] == "new-legacy-fee")["events"][0]
    assert event["kind"] == "unsupported" and event["capital_rub"] == "0" and event["quantity"] == 0
    assert event["reason"] == "new_expense_allocation_basis_required"
    blocked = evaluate_candidate(before_expense, captured)
    blocked_row = blocked["periods"][next_day]["rows"]["A:1"]
    assert blocked_row["opening_wac_rub"] == "20" and blocked_row["expense_capital_rub"] == "0"
    assert blocked_row["wac_rub"] is None and blocked_row["capital_rub"] is None
    assert blocked["periods"][next_day]["quality"] == "incomplete"
    assert blocked["periods"][next_day]["rows"]["B:1"]["wac_rub"] == "30"
    assert blocked["baseline"]["absorbed_documents"] == initial_manifest
    assert blocked["pending_documents"][0]["document_id"] == "new-legacy-fee"
    assert evaluate_candidate(blocked, captured) == blocked
    conn.close()


def main():
    with TemporaryDirectory(prefix="fbs-cost-sources-") as tmp:
        check_legacy_expense_candidate_boundary(tmp)
        path = Path(tmp) / "source.sqlite3"
        conn = fixture(path)
        document(conn, "receipt", "china_acceptance", role="accepted_pool_allocation", q=1000,
                 capital="200000", expense="10")
        document(conn, "fee", "pool_overhead", role="overhead_allocation", q=987654321,
                 capital="100", expense="100")
        conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('fee',1,'100','Approved',"
                     "'sha256:evidence','{}')")
        document(conn, "incoming", "transfer_receipt", role="received", q=999,
                 capital="99999999999999999999")
        document(conn, "outgoing", "transfer_shipment", role="shipped", q=2,
                 capital="99999999999999999999")
        # Lifecycle records are not business-document cost inputs.
        conn.execute(f"INSERT INTO {P}warehouse_business_operations VALUES('lifecycle')")
        conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES('lifecycle',1,'A','FBS',1,-12345,'-12345')")
        conn.commit()
        before = path.read_bytes()
        model = capture_current(path, now=NOW)
        assert model["quantity_snapshot"]["complete"] and model["documents_complete"], model
        assert model["baseline_costs"]["available"], model["baseline_costs"]
        assert path.read_bytes() == before
        assert [(r["nm_id"], r["facility_id"], r["quantity"]) for r in model["quantity_snapshot"]["rows"]] == [
            (1, "A", 3), (1, "B", 5), (2, "A", 0), (2, "B", 0)]
        costs = {(r["nm_id"], r["facility_id"]): r["wac_rub"] for r in model["baseline_costs"]["rows"]}
        assert costs == {(1, "A"): "20", (1, "B"): "30", (2, "A"): None, (2, "B"): None}, costs
        documents = {d["document_id"]: d for d in model["documents"]}
        assert set(documents) == {"receipt", "fee", "incoming", "outgoing"}
        receipt = documents["receipt"]["events"][0]
        assert (receipt["kind"], receipt["quantity"], receipt["capital_rub"]) == ("receipt", 1000, "200010")
        fee = documents["fee"]["events"][0]
        assert (fee["kind"], fee["quantity"], fee["capital_rub"]) == ("unsupported", 0, "0")
        assert fee["reason"] == "new_expense_allocation_basis_required"
        assert documents["incoming"]["events"][0]["reason"] == "new_source_cost_reference_required"
        assert documents["incoming"]["events"][0]["capital_rub"] == "0"
        assert documents["outgoing"]["events"][0]["capital_rub"] == "0"
        repeated = capture_current(path, now=NOW + timedelta(minutes=1))
        assert repeated["source_digest"] == model["source_digest"]
        assert repeated["baseline_costs"]["document_manifest"] == model["baseline_costs"]["document_manifest"]
        conn.execute(f"DELETE FROM {P}warehouse_business_projection_current_rows WHERE nm_id=2")
        conn.commit()
        assert capture_current(path, now=NOW)["baseline_costs"]["available"]  # hidden zero still covered

        # A document with an older business date remains a dated document; the
        # adapter never assigns it to today's quantity or infers consumption.
        document(conn, "late", "china_acceptance", role="accepted_pool_allocation", q=5,
                 capital="50", day="2026-09-01")
        conn.commit()
        late = capture_current(path, now=NOW)
        assert next(d for d in late["documents"] if d["document_id"] == "late")["business_date"] == "2026-09-01"
        assert late["quantity_snapshot"] == model["quantity_snapshot"]
        assert set(late["baseline_costs"]["document_manifest"]) == set(documents) | {"late"}

        # Old costs/quantities cannot become new operands after initialization.
        # Drift makes ONLY a fresh baseline unavailable; documents stay usable.
        conn.execute(f"UPDATE {P}ff_pool_balances SET quantity=111111,capital_rub='99999999',wac_rub='999' "
                     "WHERE nm_id=1 AND facility_id='A'")
        conn.commit()
        changed = capture_current(path, now=NOW)
        assert not changed["baseline_costs"]["available"]
        assert changed["baseline_costs"]["reason"] == "published_pool_detail_digest_mismatch"
        assert changed["documents_complete"] and changed["documents"] == late["documents"]
        assert changed["quantity_snapshot"] == model["quantity_snapshot"]
        # After initialization the adapter must work even with every old cost
        # table gone. This checks absence of reads, not just ignored output.
        conn.execute(f"DROP TABLE {P}ff_pool_balances")
        conn.execute(f"DROP TABLE {P}warehouse_functional_balances")
        conn.execute(f"DROP TABLE {P}warehouse_functional_versions")
        conn.execute(f"DROP TABLE {P}warehouse_business_projection_current_rows")
        conn.commit()
        no_baseline = capture_current(path, now=NOW, include_baseline=False)
        assert no_baseline["documents_complete"] and no_baseline["quantity_snapshot"]["complete"]
        assert no_baseline["source_digest"] == changed["source_digest"]
        assert no_baseline["baseline_costs"]["reason"] == "not_requested_after_initialization"

        conn.execute(f"UPDATE {P}ff_pool_movement_lines SET capital_delta_rub='200009' WHERE operation_id='receipt'")
        conn.commit()
        assert capture_current(path, now=NOW, include_baseline=False)["documents_reason"] == "document_money_movement_mismatch"
        conn.execute(f"UPDATE {P}ff_pool_movement_lines SET capital_delta_rub='200010' WHERE operation_id='receipt'")
        conn.execute(f"UPDATE {P}ff_pool_movement_lines SET facility_id='B' WHERE operation_id='receipt'")
        conn.commit()
        assert capture_current(path, now=NOW, include_baseline=False)["documents_reason"] == "document_money_movement_mismatch"
        conn.execute(f"UPDATE {P}ff_pool_movement_lines SET facility_id='A' WHERE operation_id='receipt'")
        conn.execute(f"DELETE FROM {P}ff_pool_movement_lines WHERE operation_id='fee'")
        conn.commit()
        assert capture_current(path, now=NOW, include_baseline=False)["documents_reason"] == "document_money_movement_mismatch"
        conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES('fee',1,'A','FBS',1,0,'100')")
        document(conn, "unmapped", "correction", role="correction", q=1, capital="5")
        conn.execute(f"DELETE FROM {P}ff_pool_document_lines WHERE document_id='unmapped'")
        conn.commit()
        unmapped = capture_current(path, now=NOW, include_baseline=False)
        event = next(d for d in unmapped["documents"] if d["document_id"] == "unmapped")["events"][0]
        assert event["kind"] == "unsupported" and event["capital_rub"] == "0"

        # Header conservation, missing operations and newly added catalog SKUs
        # are material failures, not synthetic zeros or empty successful feeds.
        conn.execute(f"UPDATE {P}ff_pool_document_expense_lines SET amount_rub='101'")
        conn.commit()
        invalid_fee = capture_current(path, now=NOW)
        assert not invalid_fee["documents_complete"]
        assert invalid_fee["documents_reason"] == "approved_expense_allocations_incomplete"
        conn.execute(f"UPDATE {P}ff_pool_document_expense_lines SET amount_rub='100'")
        conn.execute(f"DELETE FROM {P}warehouse_business_operations WHERE operation_id='receipt'")
        conn.commit()
        assert not capture_current(path, now=NOW)["documents_complete"]
        conn.execute(f"INSERT INTO {P}warehouse_business_operations VALUES('receipt')")
        conn.execute(f"INSERT INTO {P}nomenclature_items VALUES('new',3,1,0,'today')")
        conn.commit()
        new_sku = capture_current(path, now=NOW)
        assert not new_sku["quantity_snapshot"]["complete"] and new_sku["documents_complete"]
        assert not capture_current(path, now=NOW + timedelta(hours=1))["quantity_snapshot"]["complete"]
        conn.close()
        absent = Path(tmp) / "does-not-exist.sqlite3"
        assert not capture_current(absent, now=NOW)["documents_complete"] and not absent.exists()
    print("fbs_snapshot_cost_sources_smoke: ok")


if __name__ == "__main__":
    main()
