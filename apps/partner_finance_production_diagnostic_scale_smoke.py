#!/usr/bin/env python3
"""Production-scale memory regression for the Partner/Finance diagnostic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.partner_finance_production_diagnostic_smoke import (  # noqa: E402
    SELLER,
    TARGET_NM,
    WEEK,
    _finance_rows,
    _seed_settings_and_ads,
    _seed_supporting_schema,
    _sha256,
)
from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock  # noqa: E402


SCALE_ROWS = 300_000
MAX_PEAK_RSS_MIB = 768


def main() -> None:
    with TemporaryDirectory(prefix="partner-finance-diagnostic-scale-") as tmp:
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
        _seed_unrelated_rows(database)
        digest_before = _sha256(database)

        env_file = runtime / "diagnostic.env"
        env_file.write_text(
            f"SELLER_PORTAL_CANONICAL_SUPPLIER_ID={SELLER}\n",
            encoding="utf-8",
        )
        rss_before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        completed = subprocess.run(
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
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        if completed.returncode != 0:
            raise AssertionError(
                f"scale diagnostic failed: {completed.stdout}\n{completed.stderr}"
            )
        payload = json.loads(completed.stdout)
        expected_rows = SCALE_ROWS + len(_finance_rows())
        if payload["status"] != "ready" or payload["blockers"]:
            raise AssertionError(f"scale diagnostic was not ready: {payload['blockers']}")
        if payload["scanned_finance_raw_row_count"] != expected_rows:
            raise AssertionError(
                "streamed row count mismatch: "
                f"{payload['scanned_finance_raw_row_count']} != {expected_rows}"
            )
        if _sha256(database) != digest_before:
            raise AssertionError("scale diagnostic changed the runtime SQLite database")

        _seed_bound_pressure_rows(database)
        bounded_digest = _sha256(database)
        bounded = subprocess.run(
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
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        rss_after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        if bounded.returncode != 3:
            raise AssertionError(
                f"bound-pressure diagnostic did not fail closed: "
                f"{bounded.stdout}\n{bounded.stderr}"
            )
        bounded_payload = json.loads(bounded.stdout)
        bounded_codes = {item["code"] for item in bounded_payload["blockers"]}
        expected_bound_codes = {
            "finance_operation_group_bound_exceeded",
            "finance_marketing_candidate_bound_exceeded",
            "finance_raw_json_invalid",
        }
        if not expected_bound_codes.issubset(bounded_codes):
            raise AssertionError(
                f"accumulator bounds were not exercised: {bounded_payload['blockers']}"
            )
        if (
            bounded_payload["operation_group_count"] > 10_000
            or bounded_payload["unknown_marketing_candidate_count"] > 10_000
        ):
            raise AssertionError("diagnostic retained more groups than its declared bounds")
        invalid = bounded_payload["invalid_raw_json_evidence"]
        if invalid["row_count"] != 12_000 or len(invalid["examples"]) > 3:
            raise AssertionError(f"invalid-JSON evidence was not bounded: {invalid}")
        if _sha256(database) != bounded_digest:
            raise AssertionError("bound-pressure diagnostic changed the runtime SQLite database")

        peak_rss_mib = _rss_mib(max(rss_before, rss_after))
        if peak_rss_mib > MAX_PEAK_RSS_MIB:
            raise AssertionError(
                f"diagnostic peak RSS {peak_rss_mib:.1f} MiB exceeds "
                f"{MAX_PEAK_RSS_MIB} MiB"
            )

    print(
        "partner_finance_production_diagnostic_scale: ok -> "
        f"rows={expected_rows}, peak_rss_mib={peak_rss_mib:.1f}, "
        f"limit_mib={MAX_PEAK_RSS_MIB}, accumulator_bounds=fail_closed, "
        "SQLite unchanged"
    )


def _seed_unrelated_rows(database: Path) -> None:
    inserted_at = "2026-07-20T00:00:00Z"
    week_end = (WEEK + timedelta(days=6)).isoformat()
    with sqlite3.connect(database) as conn:
        batch: list[tuple[object, ...]] = []
        for offset in range(SCALE_ROWS):
            rrd_id = 1_000_000 + offset
            raw = {
                "dateFrom": WEEK.isoformat(),
                "dateTo": week_end,
                "reportId": 990001,
                "reportType": 1,
                "rrdId": rrd_id,
                "nmId": 202202,
                "vendorCode": "VC202",
                "sku": "BAR202",
                "rrDate": WEEK.isoformat(),
                "docTypeName": "",
                "sellerOperName": "",
                "quantity": 0,
            }
            raw_json = json.dumps(
                raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            batch.append(
                (
                    SELLER,
                    "990001",
                    str(rrd_id),
                    1,
                    WEEK.isoformat(),
                    week_end,
                    "202202",
                    "VC202",
                    "BAR202",
                    "",
                    "",
                    hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                    raw_json,
                    inserted_at,
                    inserted_at,
                )
            )
            if len(batch) == 5_000:
                _insert_batch(conn, batch)
                batch.clear()
        if batch:
            _insert_batch(conn, batch)
        conn.commit()


def _insert_batch(conn: sqlite3.Connection, rows: list[tuple[object, ...]]) -> None:
    conn.executemany(
        """INSERT INTO wb_finance_weekly_raw_rows
           (seller_id,report_id,rrd_id,report_type,week_start,week_end,nm_id,
            vendor_code,barcode,doc_type_name,seller_oper_name,row_hash,raw_json,
            first_seen_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )


def _seed_bound_pressure_rows(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        for offset in range(0, 12_000, 1_000):
            updates: list[tuple[str, str, str, str, str, str, str]] = []
            for index in range(offset, offset + 1_000):
                rrd_id = 1_000_000 + index
                raw = {
                    "dateFrom": WEEK.isoformat(),
                    "dateTo": (WEEK + timedelta(days=6)).isoformat(),
                    "reportId": 990001,
                    "reportType": 1,
                    "rrdId": rrd_id,
                    "nmId": int(TARGET_NM),
                    "vendorCode": "VC101",
                    "sku": "BAR101",
                    "rrDate": WEEK.isoformat(),
                    "docTypeName": "",
                    "sellerOperName": "Удержание",
                    "bonusTypeName": f"Marketing service fee {index:05d}",
                    "deduction": "0.01",
                    "quantity": 0,
                }
                raw_json = json.dumps(
                    raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                updates.append(
                    (
                        TARGET_NM,
                        "VC101",
                        "BAR101",
                        hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
                        raw_json,
                        SELLER,
                        str(rrd_id),
                    )
                )
            conn.executemany(
                """UPDATE wb_finance_weekly_raw_rows
                   SET nm_id=?,vendor_code=?,barcode=?,row_hash=?,raw_json=?
                   WHERE seller_id=? AND report_id='990001' AND rrd_id=?""",
                updates,
            )
        invalid_raw = "{"
        invalid_hash = hashlib.sha256(invalid_raw.encode("utf-8")).hexdigest()
        conn.executemany(
            """UPDATE wb_finance_weekly_raw_rows
               SET row_hash=?,raw_json=?
               WHERE seller_id=? AND report_id='990001' AND rrd_id=?""",
            [
                (invalid_hash, invalid_raw, SELLER, str(1_000_000 + index))
                for index in range(12_000, 24_000)
            ],
        )
        conn.commit()


def _rss_mib(value: float) -> float:
    # getrusage is bytes on Darwin and KiB on Linux.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return float(value) / divisor


if __name__ == "__main__":
    main()
