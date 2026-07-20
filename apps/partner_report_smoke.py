#!/usr/bin/env python3
"""Targeted Partner Report formulas, immutability, XLSX and ZIP privacy smoke."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.partner_report import (  # noqa: E402
    PARTNER_REPORT_FORMULA_VERSION,
    PartnerReportBlock,
    PartnerReportError,
)


TARGET_NM = "101101"
OTHER_NM = "202202"
WEEK_ONE = date(2026, 7, 6)
WEEK_TWO = date(2026, 7, 13)


def main() -> None:
    with TemporaryDirectory(prefix="partner-report-") as tmp:
        runtime = Path(tmp)
        block = PartnerReportBlock(
            runtime,
            seller_id="seller-1",
            now_factory=lambda: datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc),
        )
        block.ensure_schema()
        _seed_sources(block.db_path)
        _seed_finance(block)
        _seed_ads(block.db_path)

        settings = block.save_settings(
            {
                "nm_id": TARGET_NM,
                "partner_share_pct": "40",
                "invested_capital_rub": "500000",
                "replenishment_reserve_pct": "20",
                "weekly_office_expense_rub": "10000",
                "tax_rate_pct": "6",
                "common_expense_rule": "net_revenue_share",
            },
            actor="smoke-operator",
        )
        if settings["parameters"]["invested_capital_rub"] != "500000.0000":
            raise AssertionError(f"manual invested capital was not persisted: {settings}")
        if block.save_settings(settings["parameters"], actor="smoke-operator") != settings:
            raise AssertionError("identical server-owned settings must be idempotent")

        first = block.preview(
            {"nm_id": TARGET_NM, "selected_weeks": [WEEK_ONE.isoformat()]}
        )
        _assert_reference_fixture(first)

        two_week = block.preview(
            {
                "nm_id": TARGET_NM,
                "selected_weeks": [WEEK_ONE.isoformat(), WEEK_TWO.isoformat()],
            }
        )
        _assert_two_week_formulas(two_week)
        _assert_preview_and_final_selection_policy(block)

        finalized = block.finalize(
            {
                "nm_id": TARGET_NM,
                "selected_weeks": [WEEK_ONE.isoformat(), WEEK_TWO.isoformat()],
            },
            actor="smoke-operator",
        )
        repeated_finalized = block.finalize(
            {
                "nm_id": TARGET_NM,
                "selected_weeks": [WEEK_ONE.isoformat(), WEEK_TWO.isoformat()],
            },
            actor="smoke-operator-retry",
        )
        if repeated_finalized["report_id"] != finalized["report_id"]:
            raise AssertionError("exact finalization retry created a duplicate payout record")
        with sqlite3.connect(block.db_path) as conn:
            finalized_count = conn.execute(
                "SELECT count(*) FROM partner_report_finalized_reports"
            ).fetchone()[0]
        if finalized_count != 1:
            raise AssertionError("exact finalization retry must remain idempotent")
        original_json = json.dumps(finalized, ensure_ascii=False, sort_keys=True)
        package, _filename, verification = block.build_finalized_package(
            finalized["report_id"]
        )
        _assert_package(package, finalized, verification)
        artifact_dir = str(os.environ.get("PARTNER_REPORT_SMOKE_ARTIFACT_DIR") or "").strip()
        if artifact_dir:
            destination = Path(artifact_dir)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "partner-report-fixture.zip").write_bytes(package)
            with zipfile.ZipFile(BytesIO(package)) as archive:
                main_name = next(name for name in archive.namelist() if name.startswith("00_"))
                (destination / "partner-report-fixture.xlsx").write_bytes(archive.read(main_name))

        # Current cost/settings drift must not rewrite the immutable report or package values.
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                """UPDATE sheet_vitrina_v1_wb_cost_daily_state
                   SET our_wb_unit_cost_rub='999999',inputs_hash='drifted'
                   WHERE nm_id=? AND as_of_date=?""",
                (int(TARGET_NM), WEEK_ONE.isoformat()),
            )
            conn.commit()
        block.save_settings(
            {
                "nm_id": TARGET_NM,
                "partner_share_pct": "10",
                "invested_capital_rub": "700000",
                "replenishment_reserve_pct": "5",
                "weekly_office_expense_rub": "1",
                "tax_rate_pct": "1",
                "common_expense_rule": "net_revenue_share",
            },
            actor="later-operator",
        )
        readback = block.finalized_report(finalized["report_id"])
        if json.dumps(readback, ensure_ascii=False, sort_keys=True) != original_json:
            raise AssertionError("finalized report changed after current source/settings drift")
        repeat_package, _repeat_name, repeat_verification = block.build_finalized_package(
            finalized["report_id"]
        )
        _assert_package(repeat_package, finalized, repeat_verification)
        if repeat_verification["source_digest"] != verification["source_digest"]:
            raise AssertionError("repeat finalized package changed source digest")

        _assert_missing_ads_date_blocks(block)
        _assert_missing_ads_value_blocks(block)
        _assert_scanner_rejects_hidden_shared_formula_comment_metadata(block, finalized)

    _assert_persisted_loss_carry_contract()

    print(
        "partner_report: ok -> Decimal fixture, direct/allocated expenses, ads coverage, "
        "continuous payout/loss carry, immutable finalized report, XLSX structure, ZIP privacy/reconciliation"
    )


def _assert_reference_fixture(report: dict) -> None:
    if report["status"] != "ready":
        raise AssertionError(f"reference preview unexpectedly blocked: {report['blockers']}")
    values = report["weeks"][0]["values"]
    expected = {
        "net_revenue": Decimal("476034"),
        "cogs": Decimal("83837"),
        "commission": Decimal("174797"),
        "ads": Decimal("30904"),
        "card_margin": Decimal("186496"),
        "office": Decimal("10000"),
        "estimated_tax": Decimal("28562.04"),
        "replenishment_reserve": Decimal("37299.20"),
        "distributable_profit": Decimal("110634.76"),
        "partner_payout": Decimal("44253.904"),
    }
    for key, wanted in expected.items():
        actual = Decimal(str(values[key]))
        if actual != wanted:
            raise AssertionError(f"reference {key}: expected {wanted}, got {actual}")
    if Decimal(values["card_margin"]) - Decimal("186495.5") not in {
        Decimal("0.5"), Decimal("0.5000")
    }:
        raise AssertionError("reference margin is outside agreed Decimal range")


def _assert_two_week_formulas(report: dict) -> None:
    if report["status"] != "ready":
        raise AssertionError(f"two-week preview unexpectedly blocked: {report['blockers']}")
    second = report["weeks"][1]["values"]
    if Decimal(second["allocated_common_expenses"]) != Decimal("10"):
        raise AssertionError(f"account expense allocation mismatch: {second}")
    if Decimal(second["ads"]) != Decimal("10"):
        raise AssertionError(f"selected nmId ads mismatch: {second}")
    if Decimal(second["card_margin"]) != Decimal("-60"):
        raise AssertionError(f"negative week mismatch: {second}")
    expected_period_payout = (
        Decimal("110634.76") + Decimal(second["distributable_profit"])
    ) * Decimal("0.40")
    if Decimal(report["totals"]["partner_payout"]) != expected_period_payout:
        raise AssertionError(
            "period payout must include the negative week instead of summing weekly payouts"
        )
    expected_roi = expected_period_payout / Decimal("500000") * Decimal("100")
    expected_annualized = expected_roi * Decimal("52") / Decimal("2")
    if Decimal(report["totals"]["period_roi_pct"]) != expected_roi.quantize(Decimal("0.0001")):
        raise AssertionError(f"period ROI mismatch: {report['totals']}")
    if Decimal(report["totals"]["annualized_return_pct"]) != expected_annualized.quantize(Decimal("0.0001")):
        raise AssertionError(f"annualized ROI mismatch: {report['totals']}")


def _assert_preview_and_final_selection_policy(block: PartnerReportBlock) -> None:
    gap = [WEEK_ONE.isoformat(), (WEEK_ONE + timedelta(days=14)).isoformat()]
    parsed = block._validate_selected_weeks(gap, require_continuous=False)
    if parsed != gap:
        raise AssertionError("preview must accept an arbitrary unique week set")
    try:
        block._validate_selected_weeks(gap, require_continuous=True)
    except PartnerReportError as exc:
        if exc.code != "weeks_not_continuous":
            raise
    else:
        raise AssertionError("finalized payout must reject a profitable-only week gap")


def _assert_persisted_loss_carry_contract() -> None:
    with TemporaryDirectory(prefix="partner-report-loss-carry-") as tmp:
        block = PartnerReportBlock(Path(tmp), seller_id="seller-1")
        block.ensure_schema()
        prior_week = date(2026, 6, 29)
        prior_report = {
            "source_manifest": {
                "loss_carry": {
                    "source_report_id": "",
                    "source_digest": "",
                    "loss_carry_in_rub": "0.0000",
                }
            },
            "totals": {"loss_carry_in": "0.0000"},
        }
        with sqlite3.connect(block.db_path) as conn:
            conn.execute(
                """INSERT INTO partner_report_finalized_reports(
                   report_id,seller_id,nm_id,product_name,settings_version_id,
                   formula_version,selected_weeks_json,report_json,provenance_json,
                   source_digest,loss_carry_in_rub,loss_carry_out_rub,
                   total_partner_payout_rub,period_roi_pct,annualized_return_pct,
                   finalized_at,finalized_by
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "prf_prior_loss",
                    "seller-1",
                    TARGET_NM,
                    "Выбранный товар",
                    "prs_prior",
                    PARTNER_REPORT_FORMULA_VERSION,
                    json.dumps([prior_week.isoformat()]),
                    json.dumps(prior_report, sort_keys=True),
                    "{}",
                    "sha256:prior-loss",
                    "0.0000",
                    "500.0000",
                    "0.0000",
                    "0.0000",
                    "0.0000",
                    "2026-07-06T00:00:00Z",
                    "smoke-operator",
                ),
            )
            conn.commit()
        with block._connect() as conn:
            carry, source, blockers = block._loss_carry_context(
                conn,
                nm_id=TARGET_NM,
                selected_weeks=[WEEK_ONE.isoformat()],
                finalization=True,
            )
            _gap_carry, _gap_source, gap_blockers = block._loss_carry_context(
                conn,
                nm_id=TARGET_NM,
                selected_weeks=[WEEK_TWO.isoformat()],
                finalization=True,
            )
        if carry != Decimal("500") or blockers or source["source_report_id"] != "prf_prior_loss":
            raise AssertionError(f"persisted loss carry source mismatch: {carry} {source} {blockers}")
        if not any(item.get("code") == "finalized_period_gap" for item in gap_blockers):
            raise AssertionError(f"finalized period gap did not fail closed: {gap_blockers}")
        period_values = {
            "net_revenue": "0",
            "cogs": "0",
            "commission": "0",
            "logistics": "0",
            "ads": "0",
            "storage": "0",
            "other_direct_expenses": "0",
            "allocated_common_expenses": "0",
            "positive_adjustments": "0",
            "card_margin": "1000",
            "office": "0",
            "estimated_tax": "0",
            "replenishment_reserve": "0",
            "distributable_profit": "1000",
            "partner_payout": "400",
        }
        totals = block._period_totals(
            [{"values": period_values}],
            params={"partner_share_pct": "40", "invested_capital_rub": "500000"},
            loss_carry_in=carry,
        )
        if (
            Decimal(totals["distributable_profit"]) != Decimal("500")
            or Decimal(totals["partner_payout"]) != Decimal("200")
            or Decimal(totals["loss_carry_in"]) != Decimal("500")
        ):
            raise AssertionError(f"persisted loss carry was not applied to payout: {totals}")


def _assert_package(package: bytes, report: dict, verification: dict) -> None:
    if not verification["passed"]:
        raise AssertionError(f"package verification failed: {verification}")
    if verification["finance_file_count"] != len(report["selected_weeks"]):
        raise AssertionError(f"weekly Finance file count mismatch: {verification}")
    with zipfile.ZipFile(BytesIO(package)) as archive:
        names = archive.namelist()
        finance_names = [name for name in names if "Финотчёт_WB_" in name]
        required = ("00_Партнёрский_отчёт_", "Реклама_WB_", "Расчёт_себестоимости_", "Общие_расходы_WB_")
        if len(finance_names) != len(report["selected_weeks"]):
            raise AssertionError(f"ZIP weekly Finance count mismatch: {names}")
        if any(not any(name.startswith(prefix) for name in names) for prefix in required):
            raise AssertionError(f"ZIP evidence composition mismatch: {names}")
        for name in names:
            body = archive.read(name)
            if name.endswith(".xlsx"):
                with zipfile.ZipFile(BytesIO(body)) as workbook_zip:
                    xml = b"\n".join(workbook_zip.read(member) for member in workbook_zip.namelist())
                text = xml.decode("utf-8", errors="ignore")
                for forbidden in (OTHER_NM, "VC202", "4600000202202", "Другой секретный товар"):
                    if forbidden in text:
                        raise AssertionError(f"other SKU leaked into {name}: {forbidden}")
                wb = load_workbook(BytesIO(body), data_only=False)
                if any(ws.sheet_state != "visible" for ws in wb.worksheets):
                    raise AssertionError(f"hidden sheet found in {name}")
                if name.startswith("00_"):
                    ws = wb["Партнёрский отчёт"]
                    if ws.freeze_panes != "C2" or ws.print_area is None:
                        raise AssertionError("main XLSX freeze/print contract mismatch")
                    if ws["B1"].value not in (None, ""):
                        raise AssertionError("main XLSX must preserve the reference's blank label header")
                    payout_cell = next(
                        cell for cell in ws["B"] if cell.value == "Выплата партнёру"
                    )
                    if payout_cell.font.color is None or payout_cell.font.color.rgb not in {
                        "000070C0", "0070C0"
                    }:
                        raise AssertionError("payout row lost the blue reference accent")
                    annualized_cell = next(
                        cell
                        for cell in ws["B"]
                        if cell.value == "Расчётная годовая доходность на вложенный капитал"
                    )
                    if ws.cell(annualized_cell.row, 1).value != 0.4:
                        raise AssertionError("partner share coefficient must stay in the reference row")
                    if ws.cell(payout_cell.row, 1).value is not None:
                        raise AssertionError("payout row must not duplicate the partner share coefficient")
                    if any(cell.value == "####" for row in ws.iter_rows() for cell in row):
                        raise AssertionError("main XLSX contains a visibly truncated value")
                    if wb._external_links:
                        raise AssertionError("main XLSX contains an external workbook link")
                elif name.startswith("Реклама_WB_"):
                    ws = wb["Реклама WB"]
                    headers = {cell.value: cell.column for cell in ws[2]}
                    first = next(
                        row
                        for row in range(3, ws.max_row + 1)
                        if Decimal(str(ws.cell(row, headers["ads_sum"]).value or "0"))
                        == Decimal("30904")
                    )
                    if (
                        ws.cell(first, headers["advert_id"]).value != "adv-target"
                        or ws.cell(first, headers["campaign"]).value != "Target campaign"
                        or ws.cell(first, headers["placement"]).value != "search"
                    ):
                        raise AssertionError("selected-SKU ad disclosure fields were lost")
                elif name.startswith("Расчёт_себестоимости_"):
                    ws = wb["Себестоимость"]
                    headers = {cell.value: cell.column for cell in ws[2]}
                    totals = {
                        str(ws.cell(row, headers["week"]).value): Decimal(
                            str(ws.cell(row, headers["weekly_total_rub"]).value)
                        )
                        for row in range(3, ws.max_row + 1)
                        if ws.cell(row, headers["operation_date"]).value == "ИТОГО НЕДЕЛИ"
                    }
                    expected = {
                        str(week["week_start"]): Decimal(str(week["values"]["cogs"]))
                        for week in report["weeks"]
                    }
                    if totals != expected:
                        raise AssertionError(f"cost workbook weekly totals mismatch: {totals}")
                wb.close()


def _assert_missing_ads_date_blocks(block: PartnerReportBlock) -> None:
    missing_day = (WEEK_ONE + timedelta(days=3)).isoformat()
    with sqlite3.connect(block.db_path) as conn:
        row = conn.execute(
            """SELECT * FROM temporal_source_slot_snapshots
               WHERE source_key='ads_compact' AND snapshot_date=?
                 AND snapshot_role='accepted_closed_day_snapshot'""",
            (missing_day,),
        ).fetchone()
        conn.execute(
            """DELETE FROM temporal_source_slot_snapshots
               WHERE source_key='ads_compact' AND snapshot_date=?
                 AND snapshot_role='accepted_closed_day_snapshot'""",
            (missing_day,),
        )
        conn.commit()
    preview = block.preview(
        {"nm_id": TARGET_NM, "selected_weeks": [WEEK_ONE.isoformat()]}
    )
    if preview["status"] != "incomplete" or not any(
        item.get("code") == "ads_date_missing" and item.get("date") == missing_day
        for item in preview["blockers"]
    ):
        raise AssertionError(f"missing ads date was coerced to zero: {preview}")
    with sqlite3.connect(block.db_path) as conn:
        conn.execute(
            """INSERT INTO temporal_source_slot_snapshots
               (source_key,snapshot_date,snapshot_role,captured_at,payload_json)
               VALUES(?,?,?,?,?)""",
            tuple(row),
        )
        conn.commit()


def _assert_missing_ads_value_blocks(block: PartnerReportBlock) -> None:
    invalid_day = WEEK_ONE.isoformat()
    with sqlite3.connect(block.db_path) as conn:
        row = conn.execute(
            """SELECT payload_json FROM temporal_source_slot_snapshots
               WHERE source_key='ads_compact' AND snapshot_date=?
                 AND snapshot_role='accepted_closed_day_snapshot'""",
            (invalid_day,),
        ).fetchone()
        payload = json.loads(row[0])
        payload["result"]["items"][0].pop("ads_sum")
        conn.execute(
            """UPDATE temporal_source_slot_snapshots SET payload_json=?
               WHERE source_key='ads_compact' AND snapshot_date=?
                 AND snapshot_role='accepted_closed_day_snapshot'""",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), invalid_day),
        )
        conn.commit()
    preview = block.preview(
        {"nm_id": TARGET_NM, "selected_weeks": [WEEK_ONE.isoformat()]}
    )
    if preview["status"] != "incomplete" or not any(
        item.get("code") == "ads_value_invalid" and item.get("date") == invalid_day
        for item in preview["blockers"]
    ):
        raise AssertionError(f"missing ads_sum was coerced to zero: {preview}")
    with sqlite3.connect(block.db_path) as conn:
        conn.execute(
            """UPDATE temporal_source_slot_snapshots SET payload_json=?
               WHERE source_key='ads_compact' AND snapshot_date=?
                 AND snapshot_role='accepted_closed_day_snapshot'""",
            (row[0], invalid_day),
        )
        conn.commit()


def _assert_scanner_rejects_hidden_shared_formula_comment_metadata(
    block: PartnerReportBlock, report: dict
) -> None:
    with block._connect() as conn:
        settings = block._load_settings(conn, nm_id=TARGET_NM)
        _current, provenance = block._calculate_report(
            conn,
            settings=settings,
            selected_weeks=[WEEK_ONE.isoformat(), WEEK_TWO.isoformat()],
            finalization=True,
        )
    wb = Workbook()
    visible = wb.active
    visible.title = "Visible"
    visible["A1"] = f'=IF(1=1,"VC202","")'
    visible["A1"].comment = Comment("4600000202202", "Другой секретный товар")
    hidden = wb.create_sheet("Hidden source")
    hidden["A1"] = f"nmId {OTHER_NM}"
    hidden.sheet_state = "veryHidden"
    wb.properties.subject = "Другой секретный товар"
    output = BytesIO()
    wb.save(output)
    wb.close()
    malicious = BytesIO()
    with zipfile.ZipFile(BytesIO(output.getvalue())) as source, zipfile.ZipFile(
        malicious, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for member in source.namelist():
            target.writestr(member, source.read(member))
        target.writestr("xl/embeddings/leak.bin", f"nmId {OTHER_NM}".encode("utf-8"))
    verification = block._verify_package(
        [("malicious.xlsx", malicious.getvalue())],
        report=report,
        provenance=provenance,
        finance_names=["week-1", "week-2"],
        forbidden_tokens={OTHER_NM, "VC202", "4600000202202", "Другой секретный товар"},
    )
    codes = {item["code"] for item in verification["findings"]}
    if verification["passed"] or not {
        "hidden_sheet",
        "other_sku_leak",
        "embedded_object",
    }.issubset(codes):
        raise AssertionError(f"privacy scanner accepted a malicious workbook: {verification}")


def _seed_sources(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE registry_upload_current_state(
                slot INTEGER PRIMARY KEY,bundle_version TEXT,activated_at TEXT
            );
            CREATE TABLE registry_upload_config_v2(
                bundle_version TEXT,nm_id INTEGER,enabled INTEGER,display_name TEXT,
                group_name TEXT,display_order INTEGER
            );
            CREATE TABLE cost_price_current_state(
                slot INTEGER PRIMARY KEY,dataset_version TEXT,activated_at TEXT
            );
            CREATE TABLE cost_price_upload_rows(
                dataset_version TEXT,row_order INTEGER,group_name TEXT,
                cost_price_rub TEXT,effective_from TEXT
            );
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,
                barcodes_json TEXT,product_type TEXT,nomenclature_name TEXT,
                wb_title TEXT,is_hidden INTEGER,created_at TEXT,our_sku TEXT,
                aliases_json TEXT,match_key TEXT
            );
            CREATE TABLE sheet_vitrina_v1_wb_cost_daily_state(
                as_of_date TEXT NOT NULL,nm_id INTEGER NOT NULL,stock_qty REAL NOT NULL,
                our_wb_unit_cost_rub REAL,confirmed_qty REAL NOT NULL,
                estimated_qty REAL NOT NULL,fallback_qty REAL NOT NULL,
                confirmed_share_pct REAL,source_status TEXT NOT NULL,
                component_status_json TEXT NOT NULL,calculated_at TEXT NOT NULL,
                inputs_hash TEXT NOT NULL,PRIMARY KEY(as_of_date,nm_id)
            );
            CREATE TABLE temporal_source_slot_snapshots(
                source_key TEXT NOT NULL,snapshot_date TEXT NOT NULL,
                snapshot_role TEXT NOT NULL,captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(source_key,snapshot_date,snapshot_role)
            );
            INSERT INTO registry_upload_current_state VALUES(1,'bundle','2026-01-01');
            INSERT INTO registry_upload_config_v2 VALUES
                ('bundle',101101,1,'Выбранный товар','Target',1),
                ('bundle',202202,1,'Другой секретный товар','Other',2);
            INSERT INTO cost_price_current_state VALUES(1,'cost','2026-01-01');
            INSERT INTO cost_price_upload_rows VALUES
                ('cost',1,'Target','10','2026-01-01'),
                ('cost',2,'Other','20','2026-01-01');
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES
                (1,101101,'VC101','4600000101101','["4600000101101"]','other',
                 'Выбранный товар','Выбранный товар WB',0,'2026-01-01','OUR101','["OLD101"]','target'),
                (0,202202,'VC202','4600000202202','["4600000202202"]','other',
                 'Другой секретный товар','Другой секретный товар WB',1,'2026-01-01','OUR202','[]','other');
            """
        )
        for day, target_cost, other_cost in (
            (WEEK_ONE, "83837", "50"),
            (WEEK_TWO, "120", "50"),
        ):
            for nm_id, cost in ((101101, target_cost), (202202, other_cost)):
                conn.execute(
                    """INSERT INTO sheet_vitrina_v1_wb_cost_daily_state VALUES(
                       ?,?,100,?,100,0,0,1,'confirmed','{}',?,'cost-' || ? || '-' || ?)""",
                    (
                        day.isoformat(),
                        nm_id,
                        cost,
                        day.isoformat() + "T23:00:00Z",
                        day.isoformat(),
                        nm_id,
                    ),
                )
        conn.commit()


def _seed_finance(block: PartnerReportBlock) -> None:
    week_one_rows = [
        _finance_row(
            WEEK_ONE,
            report_id=1001,
            rrd_id=1,
            nm_id=int(TARGET_NM),
            vendor_code="VC101",
            barcode="4600000101101",
            doc_type="Продажа",
            quantity=1,
            revenue="476034",
            for_pay="301237",
            acquiring="123",
        ),
        _finance_row(WEEK_ONE, report_id=1001, rrd_id=2, nm_id=int(TARGET_NM), deduction="999", bonus="WB Продвижение"),
        _finance_row(WEEK_ONE, report_id=1001, rrd_id=3, nm_id=int(TARGET_NM), acceptance="111"),
        _finance_row(WEEK_ONE, report_id=1001, rrd_id=4, nm_id=int(TARGET_NM), deduction="222", bonus="Услуги доставки транзитных поставок"),
    ]
    week_two_rows = [
        _finance_row(
            WEEK_TWO,
            report_id=2001,
            rrd_id=1,
            nm_id=int(TARGET_NM),
            vendor_code="VC101",
            barcode="4600000101101",
            doc_type="Продажа",
            quantity=1,
            revenue="100",
            for_pay="80",
            acquiring="5",
        ),
        _finance_row(
            WEEK_TWO,
            report_id=2002,
            rrd_id=2,
            nm_id=int(OTHER_NM),
            vendor_code="VC202",
            barcode="4600000202202",
            doc_type="Продажа",
            quantity=1,
            revenue="100",
            for_pay="70",
        ),
        _finance_row(WEEK_TWO, report_id=2001, rrd_id=3, nm_id=0, deduction="20", bonus="Общий платный сервис"),
    ]
    for start, rows in ((WEEK_ONE, week_one_rows), (WEEK_TWO, week_two_rows)):
        block.finance.ingest_week(start, start + timedelta(days=6), rows)
        block.finance.ingest_week(start, start + timedelta(days=6), rows)


def _finance_row(
    week_start: date,
    *,
    report_id: int,
    rrd_id: int,
    nm_id: int,
    vendor_code: str = "",
    barcode: str = "",
    doc_type: str = "",
    quantity: int = 0,
    revenue: str = "0",
    for_pay: str = "0",
    acquiring: str = "0",
    deduction: str = "0",
    bonus: str = "",
    acceptance: str = "0",
) -> dict:
    return {
        "dateFrom": week_start.isoformat(),
        "dateTo": (week_start + timedelta(days=6)).isoformat(),
        "reportId": report_id,
        "reportType": 1,
        "rrdId": rrd_id,
        "rrDate": week_start.isoformat(),
        "nmId": nm_id,
        "vendorCode": vendor_code,
        "sku": barcode,
        "docTypeName": doc_type,
        "sellerOperName": doc_type or "Удержание",
        "quantity": quantity,
        "retailPriceWithDisc": revenue,
        "forPay": for_pay,
        "acquiringFee": acquiring,
        "deduction": deduction,
        "bonusTypeName": bonus,
        "paidAcceptance": acceptance,
    }


def _seed_ads(db_path: Path) -> None:
    with sqlite3.connect(db_path) as conn:
        for week_start, weekly_target in ((WEEK_ONE, Decimal("30904")), (WEEK_TWO, Decimal("10"))):
            for offset in range(7):
                day = week_start + timedelta(days=offset)
                target = weekly_target if offset == 0 else Decimal("0")
                payload = {
                    "result": {
                        "kind": "success",
                        "items": [
                            {
                                "nm_id": TARGET_NM,
                                "advert_id": "adv-target",
                                "campaign": "Target campaign",
                                "placement": "search",
                                "ads_sum": str(target),
                            },
                            {"nm_id": OTHER_NM, "ads_sum": "777"},
                        ],
                    }
                }
                conn.execute(
                    """INSERT INTO temporal_source_slot_snapshots
                       (source_key,snapshot_date,snapshot_role,captured_at,payload_json)
                       VALUES('ads_compact',?,'accepted_closed_day_snapshot',?,?)""",
                    (
                        day.isoformat(),
                        day.isoformat() + "T23:59:00Z",
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
        conn.commit()


if __name__ == "__main__":
    main()
