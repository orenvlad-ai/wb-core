"""Regression of closed-day spend verification from WB export through reporting."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
import json
import math
import os
import socket
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.partner_report_smoke import (
    TARGET_NM, WEEK_ONE, _assert_preview, _assert_workbook, _seed_ads, _seed_finance,
    _seed_sources, _settings,
)
from apps.production_apply_launcher import execute
from apps.ads_sku_daily_spend_apply import DailySpendAdapter
from packages.application.ads_spend_presentation import verified_presentation
from packages.application.ads_spend_verification import (
    HEADER, TABLE, amount, snapshot_amount, observed, SpendDecision,
    check_receipt, digest, preview, product_rows, verified_days,
)
from packages.application.partner_report import PartnerReportBlock
from packages.application.vitrina_economics import SOURCE
from packages.contracts.web_vitrina_contract import WebVitrinaContractRow

DAY = WEEK_ONE.isoformat()
NM = str(TARGET_NM)
NOW = datetime(2026, 7, 21, tzinfo=timezone.utc)
SELLER = "seller-1"


def workbook(rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "Статистика 3 товаров"
    sheet.append(list(HEADER))
    for nm, campaign, value in rows:
        sheet.append([nm, campaign, "Прямая", value])
    buffer = BytesIO()
    book.save(buffer)
    book.close()
    return buffer.getvalue()


def receipt(raw, export_id):
    return dict(date_from=DAY, date_to=DAY, currency="RUB", seller_id=SELLER,
                scope="all_products_all_campaigns", source="wb_promotion_product_statistics",
                export_id=export_id, received_at=NOW.isoformat(), sha256=digest(raw))


def refused(operation):
    try:
        operation()
    except ValueError:
        return
    raise AssertionError("invalid input was accepted")


def native_row(key, value, presentation):
    return WebVitrinaContractRow("SKU:" + NM + "|" + key, 0, "SKU", NM, "", key, "", "", "",
                                 None, TARGET_NM, None, {DAY: value}, {DAY: presentation})


def check_vitrina(proofs):
    partial = dict(source_as_of_date=DAY, quality_state="partial", completeness_state="unknown_scope")
    spend = native_row("ads_sum", 30904, dict(partial, source="ads_partial_observed_v1"))
    orders = native_row("ads_orders", 2, spend.presentation_by_date[DAY])
    cell = dict(partial, state="unconfirmed", source=SOURCE, calculation_contract="catalog_economics_v1",
                evidence=dict(operand_date=DAY, partial_operands=["ads_sum"], operands={"ads_sum": 30904}))
    profit = native_row("proxy_profit_4_rub", 7500, cell)
    second_cause = dict(cell, evidence=dict(cell["evidence"], partial_operands=["ads_sum", "orderCount"]))
    other = native_row("proxy_margin_3_pct", 0.15, second_cause)
    old = replace(spend, values_by_date={DAY: 2})
    got = verified_presentation([spend, orders, profit, other, old], proofs)
    assert got[0].presentation_by_date[DAY]["quality_state"] == "confirmed"
    assert got[2].presentation_by_date[DAY]["quality_state"] == "management_estimate"
    assert got[1] == orders and got[3] == other and got[4] == old
    assert [r.values_by_date for r in got] == [r.values_by_date for r in (spend, orders, profit, other, old)]


def main():
    # Persisted native aggregation uses float. Admit its adjacent cent value,
    # while workbook values and meaningful discrepancies remain strict.
    for value, expected in ((2739.8599999999997, "2739.86"), (2904.8199999999997, "2904.82"),
                            (2419.7200000000003, "2419.72"), (2752.3599999999997, "2752.36"),
                            (2608.7799999999997, "2608.78"), (2799.8199999999997, "2799.82"),
                            (2479.2000000000003, "2479.20"), (0.1 + 0.2, "0.30")):
        assert snapshot_amount(value) == Decimal(expected)
        refused(lambda: amount(value))
        refused(lambda: snapshot_amount(str(value)))
        refused(lambda: snapshot_amount(Decimal(str(value))))
        decision = SpendDecision("verified_sku_spend", amount=Decimal(expected))
        from packages.application.ads_spend_presentation import same_spend
        assert same_spend(value, decision)
        assert not same_spend(value + 0.01, decision)
        partial = dict(kind="incomplete", snapshot_date=DAY, diagnostics=dict(source_date=DAY,
            zero_fill_applied=False, partial_observation_contract="ads_partial_observed_v1"),
            items=[dict(nm_id=NM, ads_sum=value)])
        assert observed(partial, DAY) == {NM: Decimal(expected)}
    for value in (1.001, 1.009, -0.01, math.inf, math.nan, 1e100, math.nextafter(0.0, 1.0),
                  math.nextafter(math.nextafter(1.0, math.inf), math.inf), 2**33 + 0.001):
        refused(lambda: snapshot_amount(value))
    assert snapshot_amount(0.0) == Decimal("0.00")
    refused(lambda: product_rows(workbook([(NM, "317", 1.001)])))
    with sqlite3.connect(":memory:") as native:
        native.execute("CREATE TABLE temporal_source_slot_snapshots (source_key, snapshot_role, snapshot_date, payload_json)")
        native.execute("INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?)", ("ads_compact",
            "accepted_closed_day_snapshot", DAY, json.dumps(dict(partial, items=[dict(nm_id=NM, ads_sum=2752.3599999999997)]))))
        for official, status in ((2752.36, "verified_sku_spend"), (2752.37, "spend_mismatch")):
            daily = workbook([(NM, "317", official)])
            assert preview(native, daily, receipt(daily, str(official)), SELLER, NOW, nm_id=NM)[0]["decisions"][NM] == status
    raw = workbook([(NM, "317", 30900), (NM, "318", 4), ("201", "317", 0)])
    info = receipt(raw, "daily-export")
    refused(lambda: check_receipt(dict(info, date_to="2026-07-10"), raw, SELLER, NOW))
    refused(lambda: check_receipt(info, raw, "other-cabinet", NOW))
    refused(lambda: check_receipt(dict(info, sha256="other"), raw, SELLER, NOW))
    refused(lambda: product_rows(workbook([(NM, "317", 100), (NM, "317", 100)])))
    with TemporaryDirectory() as temp, patch.dict(os.environ, {"SELLER_PORTAL_CANONICAL_SUPPLIER_ID": SELLER}):
        block = PartnerReportBlock(Path(temp), seller_id=SELLER, now_factory=lambda: NOW)
        block.ensure_schema()
        _seed_sources(block.db_path)
        _seed_finance(block)
        _seed_ads(block.db_path)
        block.save_settings(_settings(), actor="operator@example.test")
        payload = dict(kind="incomplete", snapshot_date=DAY, diagnostics=dict(source_date=DAY,
                       zero_fill_applied=False, partial_observation_contract="ads_partial_observed_v1"),
                       items=[{"nm_id": NM, "ads_sum": 30904}, {"nm_id": "201", "ads_sum": 0},
                              {"nm_id": "202", "ads_sum": 0}])
        runtime = Path(temp) / "spend-test-runtime"
        evidence = runtime / "evidence"
        evidence.mkdir(parents=True)
        generation = runtime / "generations"
        generation.mkdir()
        target = generation / "operational.sqlite3"
        block.db_path.rename(target)
        block.db_path.symlink_to(target)
        (runtime / "storage_generation_manifest.json").write_text(json.dumps(dict(
            canonical_source="split", manifest_sha256="fixture", operational={"relative_path": "generations/operational.sqlite3"})))
        (runtime.parent / "app").mkdir()
        (runtime.parent / "app" / ".wb-core-runtime-sha").write_text("a" * 40)

        def request(data, capture):
            name = capture["export_id"]
            wb = evidence / (name + ".xlsx")
            sidecar = evidence / (name + ".json")
            wb.write_bytes(data)
            sidecar.write_text(json.dumps(capture))
            return dict(runtime_dir=str(runtime), runtime_sha="a" * 40, storage_manifest_sha256="fixture",
                        hostname=socket.gethostname(), seller_id=SELLER, day=DAY, workbook_path=str(wb),
                        receipt_path=str(sidecar))

        def launch(action, req, pre=None):
            return execute(action=action, adapter_name="ads_sku_daily_spend_v1",
                           operation_id="verify-" + Path(req["receipt_path"]).stem, request=req,
                           expected_prestate=pre["prestate_sha256"] if pre else "",
                           expected_candidate=pre["candidate_sha256"] if pre else "")

        with sqlite3.connect(block.db_path) as conn:
            conn.row_factory = sqlite3.Row
            query = "UPDATE temporal_source_slot_snapshots SET payload_json=? WHERE snapshot_date=?"
            conn.execute(query, (json.dumps(payload), DAY))
            conn.commit()
            result, _, blockers = block._ads_for_week(conn, nm_id=NM, week_start=WEEK_ONE, week_end=WEEK_ONE)
            assert result is None and blockers
            outcome, _ = preview(conn, raw, info, SELLER, NOW)
            assert outcome["decisions"] == {NM: "verified_sku_spend", "201": "verified_sku_spend", "202": "no_product_row"}
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
            first_req = request(raw, info)
            prepared = launch("preview", first_req)
            for _ in range(2):
                assert launch("apply", first_req, prepared)["state"] == "applied"
            assert conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 3
            proofs = verified_days(conn, SELLER, [DAY])
            check_vitrina(proofs)
            assert not verified_days(conn, "other", [DAY])
            total, rows, blockers = block._ads_for_week(conn, nm_id=NM, week_start=WEEK_ONE, week_end=WEEK_ONE)
            assert not blockers and total == sum(Decimal(r["ads_sum"]) for r in rows) == Decimal(30904)
            assert {r["advert_id"] for r in rows} == {"317", "318"}
            wb = load_workbook(BytesIO(block._build_ads_workbook(report={"nm_id": NM}, rows=rows)))
            headings = list(next(wb.active.iter_rows(min_row=2, max_row=2, values_only=True)))
            assert sum(Decimal(str(r[headings.index("ads_sum")])) for r in list(wb.active.values)[2:]) == total
            wb.close()
            report = _assert_preview(block)
            _assert_workbook(block, report)
            wrong = workbook([(NM, "317", 1)])
            preview_wrong, _ = preview(conn, wrong, receipt(wrong, "wrong"), SELLER, NOW)
            assert preview_wrong["decisions"][NM] == "conflicting_exports"
            omission = workbook([("201", "317", 0)])
            next_info = receipt(omission, "second")
            changed, _ = preview(conn, omission, next_info, SELLER, NOW)
            next_req = request(omission, next_info)
            assert changed["decisions"][NM] == "conflicting_exports"
            launch("apply", next_req, launch("preview", next_req))
            assert verified_days(conn, SELLER, [DAY])[DAY][NM].status == "conflicting_exports"
            assert block._ads_for_week(conn, nm_id=NM, week_start=WEEK_ONE, week_end=WEEK_ONE)[0] is None
            adapter = DailySpendAdapter()
            duplicate = adapter.preview(next_req, "verify-repeat")
            adapter.apply(next_req, "verify-repeat", duplicate)
            assert adapter.readback(next_req, "verify-repeat")["state"] == "no_change"
            adapter.rollback(next_req, "verify-second")
            assert adapter.readback(next_req, "verify-second")["reason"] == "rolled_back"
            assert adapter.readback(next_req, "verify-repeat")["state"] == "ambiguous"
            assert verified_days(conn, SELLER, [DAY])[DAY][NM].status == "verified_sku_spend"
            wrong_req = request(wrong, receipt(wrong, "wrong"))
            old_preview = launch("preview", wrong_req)
            conn.execute(query, (json.dumps({"result": payload}), DAY))
            conn.commit()
            assert verified_days(conn, SELLER, [DAY]) == {}
            refused(lambda: DailySpendAdapter().apply(wrong_req, "verify-wrong",
                    {**DailySpendAdapter().preview(wrong_req, "verify-wrong"),
                     "prestate_sha256": old_preview["prestate_sha256"]}))
            different, _ = preview(conn, wrong, receipt(wrong, "wrong"), SELLER, NOW)
            assert different["decisions"][NM] == "spend_mismatch"
    print("Daily SKU spend: verified preview, XLSX, partial economics, omissions, conflicts, zero and stale receipts.")


if __name__ == "__main__":
    main()
