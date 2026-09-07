#!/usr/bin/env python3
"""Saved-source contract: complete stock, exact baseline, independent document money."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_official_fbs_smoke import fixture as stock_fixture
from packages.application.fbs_snapshot_cost_sources import capture_current
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
            source_system,source_type,source_id,source_revision,idempotency_epoch,business_date,posted_at,
            posted_manifest_sha256,posted_manifest_json);
        CREATE TABLE {P}ff_pool_document_lines(document_id,line_no,line_role,facility_id,pool,nm_id,
            quantity,capital_rub,expense_rub,metadata_json);
        CREATE TABLE {P}ff_pool_document_expense_lines(document_id,expense_line_no,amount_rub,basis,
            source_file_sha256,metadata_json);
        CREATE TABLE {P}ff_pool_document_relations(parent_document_id,child_document_id,root_document_id,relation_type);
        CREATE TABLE {P}ff_pool_movement_lines(operation_id,line_no,facility_id,pool,nm_id,quantity_delta,capital_delta_rub,metadata_json);
        CREATE TABLE {P}warehouse_business_operations(operation_id);
    """)
    for facility, pool, quantity, capital in [("A", "FBS", 10, "200"), ("B", "FBS", 20, "600"),
                                            ("B", "FBO", 4, "120")]:
        conn.execute(f"INSERT INTO {P}ff_pool_balances VALUES(?,?,?,?,?,?,?,?,?)",
                     (facility, pool, 1, 1, quantity, capital, str(int(capital)//quantity),
                      "opening", "2026-09-05T09:00:00Z"))
    conn.execute(f"INSERT INTO {P}ff_pool_balances VALUES('B','FBO',2,1,0,'0',NULL,'opening','2026-09-05T09:00:00Z')")
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


def document(conn, identity, kind, *, role, q, capital, expense="0", facility="A", pool="FBS", day=DAY,
             domain=None, root_id=None, relation=None, metadata=None, nm_id=1):
    root_id = root_id or identity
    metadata = metadata or {}
    domain = domain if domain is not None else (
        {"facility_id": facility, "scope": pool, "amount_rub": capital}
        if kind == "pool_overhead" else {})
    posted = {"contract_name": "ff_pool_business_documents_v1", "document_id": identity,
              "document_kind": kind, "root_document_id": root_id, "business_date": day,
              "source": {"system": "fixture", "type": kind, "id": identity,
                         "revision": "rev:" + identity, "idempotency_epoch": 1}, "domain": domain}
    posted_json = json.dumps(posted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    posted_hash = "sha256:" + hashlib.sha256(posted_json.encode()).hexdigest()
    conn.execute(f"INSERT INTO {P}ff_pool_documents VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (identity, kind, root_id, identity, "fixture", kind, identity, "rev:" + identity, 1,
                  day, day + "T10:05:00Z", posted_hash, posted_json))
    conn.execute(f"INSERT INTO {P}warehouse_business_operations VALUES(?)", (identity,))
    if relation:
        conn.execute(f"INSERT INTO {P}ff_pool_document_relations VALUES(?,?,?,?)",
                     (root_id, identity, root_id, relation))
    if role is None:
        return
    conn.execute(f"INSERT INTO {P}ff_pool_document_lines VALUES(?,?,?,?,?,?,?,?,?,?)",
                 (identity, 1, role, facility, pool, nm_id, q, capital, expense, json.dumps(metadata)))
    movement_capital = Decimal(capital) + (Decimal(expense) if kind == "china_acceptance" else 0)
    conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES(?,?,?,?,?,?,?,?)",
                 (identity, 1, facility, pool, nm_id, 0 if kind == "pool_overhead" else q, str(movement_capital), "{}"))


def check_authoritative_document_shape(directory):
    path = Path(directory) / "authoritative-document-shape.sqlite3"
    conn = fixture(path)
    # Old allocation had only FBO stock. The authoritative both-pool header must
    # survive so the resolver can allocate against the NEW FBS and FBO masses.
    document(conn, "fee-both", "pool_overhead", role="overhead_allocation", q=987654321,
             capital="100", expense="100", pool="FBO", facility="B",
             domain={"facility_id": "B", "scope": "both", "amount_rub": "100", "source_mode": "manual"})
    conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('fee-both',1,'100',"
                 "'Approved','sha256:old-evidence','{}')")
    transfer_domain = {"source": {"facility_id": "A", "pool": "FBS"},
                       "destination": {"facility_id": "B", "pool": "FBO"}}
    document(conn, "transfer-root", "transfer_root", role=None, q=0, capital="0", domain=transfer_domain)
    document(conn, "shipment", "transfer_shipment", role="shipped", q=10, capital="777", expense="100",
             root_id="transfer-root", relation="shipment_of", domain=transfer_domain)
    conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('shipment',1,'60','Delivery','','{{}}')")
    conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('shipment',2,'40','Loading','','{{}}')")
    document(conn, "partial-receipt", "transfer_receipt", role="received", q=6, capital="466.2", expense="60",
             facility="B", pool="FBO", root_id="transfer-root", relation="receipt_of", domain=transfer_domain,
             metadata={"terminal_quantity_before": 0})
    document(conn, "cancel", "transfer_cancellation", role="cancelled", q=4, capital="310.8", expense="40",
             root_id="transfer-root", relation="cancellation_of",
             metadata={"expense_terminalized_not_capitalized": True})
    # Saved writer bytes preserve the order of original integer keys, which
    # differs from sorting string keys after JSON decoding.
    document(conn, "numeric-manifest-keys", "pool_inventory", role=None, q=0, capital="0",
             domain={"diagnostic": {999: "nine", 1000: "ten"}})
    conn.commit()
    before = path.read_bytes()
    captured = capture_current(path, now=NOW)
    assert captured["documents_complete"] and captured["baseline_costs"]["available"], captured
    assert path.read_bytes() == before
    documents = {doc["document_id"]: doc for doc in captured["documents"]}
    numeric = documents["numeric-manifest-keys"]
    assert numeric["cost_document"]["domain"] == {"diagnostic": {"999": "nine", "1000": "ten"}}
    fee = documents["fee-both"]["cost_document"]
    assert fee["domain"] == {"facility_id": "B", "scope": "both", "amount_rub": "100", "source_mode": "manual"}
    assert {line["pool"] for line in fee["lines"]} == {"FBO"}
    assert fee["expense_lines"][0]["amount_rub"] == "100"
    assert fee["lines"][0]["quantity"] == 987654321  # raw evidence, not a new mass
    root = documents["transfer-root"]["cost_document"]
    assert root["domain"] == transfer_domain and root["lines"] == []
    shipment = documents["shipment"]["cost_document"]
    assert shipment["relations"] == [{"parent_document_id": "transfer-root", "child_document_id": "shipment",
                                      "root_document_id": "transfer-root", "relation_type": "shipment_of"}]
    assert sum(Decimal(row["amount_rub"]) for row in shipment["expense_lines"]) == 100
    partial = documents["partial-receipt"]["cost_document"]
    assert partial["lines"][0]["pool"] == "FBO" and partial["lines"][0]["quantity"] == 6
    assert json.loads(partial["lines"][0]["metadata_json"])["terminal_quantity_before"] == 0
    assert "metadata_json" in partial["movements"][0]
    assert json.loads(documents["cancel"]["cost_document"]["lines"][0]["metadata_json"])["expense_terminalized_not_capitalized"]
    fbo = captured["baseline_costs"]["fbo_rows"]
    assert len(fbo) == 2 and fbo[0]["facility_id"] == "B" and fbo[0]["nm_id"] == 1
    assert (fbo[0]["quantity"], fbo[0]["capital_rub"], fbo[0]["wac_rub"]) == (4, "120", "30")
    assert (fbo[1]["quantity"], fbo[1]["capital_rub"], fbo[1]["wac_rub"]) == (0, "0", None)
    assert fbo[1]["source"]["basis"] == "exact_published_pool_zero"
    # A new relation belongs to its child; it cannot rewrite the fingerprint
    # of an already absorbed root or source shipment.
    document(conn, "late-transfer-fee", "late_expense", role="late_expense_component", q=10,
             capital="0", expense="20", pool="FBO", facility="B", root_id="transfer-root",
             relation="late_expense_for", domain={"root_document_id": "transfer-root", "amount_rub": "20"})
    conn.execute(f"INSERT INTO {P}ff_pool_document_expense_lines VALUES('late-transfer-fee',1,'20','Late delivery','','{{}}')")
    conn.commit()
    after_child = capture_current(path, now=NOW)
    after_documents = {doc["document_id"]: doc for doc in after_child["documents"]}
    assert after_documents["transfer-root"]["fingerprint"] == documents["transfer-root"]["fingerprint"]
    assert after_documents["shipment"]["fingerprint"] == documents["shipment"]["fingerprint"]
    original_json, original_hash = conn.execute(f"SELECT posted_manifest_json,posted_manifest_sha256 "
                                               f"FROM {P}ff_pool_documents WHERE document_id='fee-both'").fetchone()
    # Valid JSON with an unchanged stored hash must not silently alter scope.
    modified = json.loads(original_json)
    modified["domain"]["scope"] = "FBO"
    conn.execute(f"UPDATE {P}ff_pool_documents SET posted_manifest_json=? WHERE document_id='fee-both'", (json.dumps(modified),))
    conn.commit()
    assert capture_current(path, now=NOW)["documents_reason"] == "posted_document_manifest_hash_mismatch"
    conn.execute(f"UPDATE {P}ff_pool_documents SET posted_manifest_json=?,posted_manifest_sha256=? WHERE document_id='fee-both'",
                 (original_json, original_hash))
    conn.execute(f"UPDATE {P}ff_pool_documents SET source_revision='drift' WHERE document_id='fee-both'")
    conn.commit()
    assert capture_current(path, now=NOW)["documents_reason"] == "posted_document_manifest_source_mismatch"
    conn.execute(f"UPDATE {P}ff_pool_documents SET source_revision='rev:fee-both' WHERE document_id='fee-both'")
    # A positive FBO pool row that was never disclosed by the published version
    # cannot silently create a candidate's opening FBO capital.
    conn.execute(f"UPDATE {P}ff_pool_balances SET quantity=1,capital_rub='10',wac_rub='10' WHERE pool='FBO' AND nm_id=2")
    bind_pool(conn)
    conn.commit()
    absent_basis = capture_current(path, now=NOW)
    assert absent_basis["documents_complete"] and not absent_basis["baseline_costs"]["available"]
    assert absent_basis["baseline_costs"]["reason"] == "fbo_initial_published_cost_missing"
    conn.close()


def main():
    with TemporaryDirectory(prefix="fbs-cost-sources-") as tmp:
        check_authoritative_document_shape(tmp)
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
        conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES('lifecycle',1,'A','FBS',1,-12345,'-12345','{{}}')")
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
        assert no_baseline["baseline_costs"]["fbo_rows"] == []
        assert all("cost_document" in doc for doc in no_baseline["documents"])

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
        conn.execute(f"INSERT INTO {P}ff_pool_movement_lines VALUES('fee',1,'A','FBS',1,0,'100','{{}}')")
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
        assert invalid_fee["documents_reason"] == "overhead_authoritative_amount_mismatch"
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
