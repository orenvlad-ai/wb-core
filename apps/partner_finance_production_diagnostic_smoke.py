#!/usr/bin/env python3
"""Deterministic smoke for the read-only Partner/Finance diagnostic."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.application.partner_finance_production_diagnostic import (  # noqa: E402
    DiagnosticScope,
    run_partner_finance_diagnostic,
)
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock  # noqa: E402


WEEK = date(2026, 7, 6)
SELLER = "seller-fixture"
TARGET_NM = "101101"


def main() -> None:
    with TemporaryDirectory(prefix="partner-finance-production-diagnostic-") as tmp:
        runtime = Path(tmp)
        database = runtime / "registry_upload_runtime.sqlite3"
        finance = WbFinanceWeeklyBlock(
            runtime,
            seller_id=SELLER,
            now_factory=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
        finance.ensure_schema()
        _seed_supporting_schema(database)
        finance.ingest_week(WEEK, WEEK + timedelta(days=6), _finance_rows())
        _seed_settings_and_ads(database)
        _make_logical_duplicate_evidence(database)

        digest_before = _sha256(database)
        explicit_scope = DiagnosticScope(
            database=database,
            seller_id=SELLER,
            nm_id=TARGET_NM,
            weeks=(WEEK.isoformat(),),
            max_groups=100,
        )
        first = run_partner_finance_diagnostic(explicit_scope)
        second = run_partner_finance_diagnostic(explicit_scope)
        if first["status"] != "ready" or first["blockers"]:
            raise AssertionError(f"complete fixture was not ready: {first['blockers']}")
        if first["fingerprint"] != second["fingerprint"]:
            raise AssertionError("diagnostic fingerprint is not deterministic")
        if first["source_digest"] != second["source_digest"]:
            raise AssertionError("diagnostic source digest is not deterministic")
        if first["generated_at"] == "":
            raise AssertionError("diagnostic omitted generation time")

        week = first["weeks"][0]
        expected = {
            "ads_compact_marketing_rub": "35.0000",
            "direct_finance_marketing_rub": "30.0000",
            "account_finance_marketing_rub": "40.0000",
            "allocated_finance_marketing_rub": "20.0000",
            "current_other_withholdings_rub": "58.5000",
            "residual_without_finance_marketing_rub": "38.5000",
            "parsed_current_other_withholdings_rub": "58.5000",
            "parsed_reconciliation_delta_rub": "0.0000",
        }
        for key, value in expected.items():
            if week.get(key) != value:
                raise AssertionError(f"{key}: {week.get(key)!r} != {value!r}")
        if week["allocation_coefficient"] != "0.500000000000":
            raise AssertionError(f"allocation coefficient mismatch: {week}")

        negative = first["negative_deduction_evidence"]
        if (
            negative["row_count"] != 1
            or negative["signed_deduction_sum_rub"] != "-5.0000"
            or negative["current_system_abs_sum_rub"] != "5.0000"
            or negative["abs_vs_signed_expense_uplift_rub"] != "10.0000"
        ):
            raise AssertionError(f"negative deduction evidence mismatch: {negative}")
        candidates = first["unknown_marketing_name_candidates"]
        if (
            len(candidates) != 1
            or candidates[0]["operation_name"] != "Marketing service fee / Удержание"
            or candidates[0]["system_abs_amount_rub"] != "9.0000"
        ):
            raise AssertionError(f"unknown marketing candidate mismatch: {candidates}")
        duplicates = first["duplicates"]
        if duplicates["logical_duplicate_identity_count"] != 1:
            raise AssertionError(f"logical duplicate was not detected: {duplicates}")
        marketing_group = next(
            item
            for item in first["operation_groups"]
            if item["finance_classifier_bucket"] == "marketing"
            and item["accounting_path"] == "allocated_account"
        )
        if (
            marketing_group["signed_source_sum_rub"] != "40.0000"
            or marketing_group["allocated_amount_sum_rub"] != "20.0000"
            or marketing_group["current_other_contribution_rub"] != "20.0000"
            or marketing_group["semantic_partner_target"]
            != "Исключить из Partner Finance; канонический источник — ads_compact"
        ):
            raise AssertionError(f"marketing group mismatch: {marketing_group}")

        server = run_partner_finance_diagnostic(
            DiagnosticScope(
                database=database,
                seller_id=SELLER,
                server_settings=True,
                max_groups=100,
            )
        )
        if (
            server["selection"]["mode"] != "server_settings"
            or server["selection"]["week_selection"] != "all_finance_weeks"
            or server["nm_id"] != TARGET_NM
            or server["weeks"][0]["current_other_withholdings_rub"] != "58.5000"
        ):
            raise AssertionError(f"server-settings selection mismatch: {server}")

        env_file = runtime / "diagnostic.env"
        env_file.write_text(
            f"SELLER_PORTAL_CANONICAL_SUPPLIER_ID={SELLER}\n",
            encoding="utf-8",
        )
        cli = subprocess.run(
            [
                sys.executable,
                str(ROOT / "apps" / "partner_finance_production_diagnostic.py"),
                "--database",
                str(database),
                "--env-file",
                str(env_file),
                "--server-settings",
                "--max-groups",
                "100",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if cli.returncode != 0:
            raise AssertionError(f"diagnostic CLI failed: {cli.stdout}\n{cli.stderr}")
        cli_payload = json.loads(cli.stdout)
        if cli_payload["fingerprint"] != server["fingerprint"]:
            raise AssertionError("CLI and application diagnostic fingerprints differ")
        if _sha256(database) != digest_before:
            raise AssertionError("read-only diagnostic changed the runtime SQLite database")

    print(
        "partner_finance_production_diagnostic: ok -> explicit/server settings, "
        "ads/Finance marketing reconciliation, classified row groups, duplicate/storno/"
        "unknown-name evidence, deterministic fingerprint, SQLite unchanged"
    )


def _seed_supporting_schema(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE sheet_vitrina_v1_nomenclature_items(
                is_active INTEGER,nm_id INTEGER,vendor_code TEXT,barcode TEXT,
                barcodes_json TEXT,product_type TEXT,nomenclature_name TEXT,
                wb_title TEXT,is_hidden INTEGER,created_at TEXT,our_sku TEXT,
                aliases_json TEXT,match_key TEXT
            );
            INSERT INTO sheet_vitrina_v1_nomenclature_items VALUES
              (1,101101,'VC101','BAR101','["BAR101"]','other','Target','Target',0,
               '2026-01-01','OUR101','[]','target'),
              (1,202202,'VC202','BAR202','["BAR202"]','other','Other','Other',0,
               '2026-01-01','OUR202','[]','other');
            """
        )
        conn.commit()


def _seed_settings_and_ads(database: Path) -> None:
    parameters = {
        "partner_share_pct": "40",
        "invested_capital_rub": "1000000",
        "replenishment_reserve_pct": "20",
        "weekly_office_expense_rub": "10000",
        "tax_rate_pct": "6",
        "common_expense_rule": "net_revenue_share",
    }
    with sqlite3.connect(database) as conn:
        conn.executescript(
            """
            CREATE TABLE partner_report_settings_versions(
                settings_version_id TEXT PRIMARY KEY,seller_id TEXT,nm_id TEXT,
                product_name TEXT,parameters_json TEXT,fingerprint TEXT,
                created_at TEXT,created_by TEXT
            );
            CREATE TABLE partner_report_settings_current(
                seller_id TEXT,nm_id TEXT,settings_version_id TEXT,
                PRIMARY KEY(seller_id,nm_id)
            );
            CREATE TABLE temporal_source_slot_snapshots(
                source_key TEXT,snapshot_date TEXT,snapshot_role TEXT,
                captured_at TEXT,payload_json TEXT,
                PRIMARY KEY(source_key,snapshot_date,snapshot_role)
            );
            """
        )
        conn.execute(
            "INSERT INTO partner_report_settings_versions VALUES(?,?,?,?,?,?,?,?)",
            (
                "prs-fixture",
                SELLER,
                TARGET_NM,
                "Target",
                json.dumps(parameters, sort_keys=True),
                "sha256:settings-fixture",
                "2026-07-20T00:00:00Z",
                "fixture",
            ),
        )
        conn.execute(
            "INSERT INTO partner_report_settings_current VALUES(?,?,?)",
            (SELLER, TARGET_NM, "prs-fixture"),
        )
        for offset in range(7):
            day = (WEEK + timedelta(days=offset)).isoformat()
            payload = (
                {
                    "kind": "success",
                    "snapshot_date": day,
                    "items": [{"nm_id": TARGET_NM, "ads_sum": "35"}],
                }
                if offset == 0
                else {"kind": "empty", "snapshot_date": day, "items": []}
            )
            conn.execute(
                "INSERT INTO temporal_source_slot_snapshots VALUES(?,?,?,?,?)",
                (
                    "ads_compact",
                    day,
                    "accepted_closed_day_snapshot",
                    day + "T23:00:00Z",
                    json.dumps(payload, sort_keys=True),
                ),
            )
        conn.commit()


def _make_logical_duplicate_evidence(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            """SELECT raw_json FROM wb_finance_weekly_raw_rows
               WHERE seller_id=? AND report_id='701' AND rrd_id='7'""",
            (SELLER,),
        ).fetchone()
        payload = json.loads(str(row[0]))
        payload["reportId"] = 701
        payload["rrdId"] = 6
        conn.execute(
            """UPDATE wb_finance_weekly_raw_rows SET raw_json=?
               WHERE seller_id=? AND report_id='701' AND rrd_id='7'""",
            (json.dumps(payload, sort_keys=True), SELLER),
        )
        conn.commit()


def _finance_rows() -> list[dict]:
    rows = [
        _sale(1, TARGET_NM, "VC101", "BAR101", revenue="1000", for_pay="800"),
        _sale(2, "202202", "VC202", "BAR202", revenue="1000", for_pay="850"),
        _deduction(3, TARGET_NM, "VC101", "BAR101", "30", "WB Продвижение"),
        _deduction(4, TARGET_NM, "VC101", "BAR101", "10", "Удержание гарантийное"),
        _deduction(5, "0", "", "", "40", "Маркетинг WB"),
        _deduction(6, "0", "", "", "20", "Компенсация обработки"),
        _deduction(7, "0", "", "", "-5", "Сторно удержания"),
        _deduction(8, "0", "", "", "9", "Marketing service fee"),
        {
            **_base(9, "0", "", ""),
            "sellerOperName": "Общие услуги",
            "deliveryService": "12",
            "paidStorage": "8",
            "paidAcceptance": "6",
            "penalty": "4",
        },
        _deduction(10, "0", "", "", "2", "Подписка Jamm"),
        _deduction(11, "0", "", "", "3", "Платный сервис"),
        _deduction(12, "0", "", "", "5", "Услуги доставки транзитных поставок"),
        {
            **_base(13, "0", "", ""),
            "sellerOperName": "Корректировка",
            "additionalPayment": "7",
        },
    ]
    return rows


def _base(rrd_id: int, nm_id: str, vendor: str, barcode: str) -> dict:
    return {
        "dateFrom": WEEK.isoformat(),
        "dateTo": (WEEK + timedelta(days=6)).isoformat(),
        "reportId": 701,
        "reportType": 1,
        "rrdId": rrd_id,
        "nmId": int(nm_id),
        "vendorCode": vendor,
        "sku": barcode,
        "rrDate": (WEEK + timedelta(days=1)).isoformat(),
        "docTypeName": "",
        "sellerOperName": "",
        "quantity": 0,
    }


def _sale(
    rrd_id: int,
    nm_id: str,
    vendor: str,
    barcode: str,
    *,
    revenue: str,
    for_pay: str,
) -> dict:
    return {
        **_base(rrd_id, nm_id, vendor, barcode),
        "docTypeName": "Продажа",
        "sellerOperName": "Продажа",
        "quantity": 1,
        "retailPriceWithDisc": revenue,
        "forPay": for_pay,
        "acquiringFee": "0",
    }


def _deduction(
    rrd_id: int,
    nm_id: str,
    vendor: str,
    barcode: str,
    amount: str,
    name: str,
) -> dict:
    return {
        **_base(rrd_id, nm_id, vendor, barcode),
        "sellerOperName": "Удержание",
        "bonusTypeName": name,
        "deduction": amount,
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    main()
