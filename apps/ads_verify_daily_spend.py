"""Read-only preview of daily WB spend. Submit through the ads_sku_daily_spend_v1 production adapter.

Run python -m apps.ads_verify_daily_spend --help for the operator contract.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.application.ads_spend_verification import preview
from packages.application.wb_incident_policy import canonical_seller_id


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Use the trusted capture receipt from the authenticated WB export. Required fields: "
        "source=wb_promotion_product_statistics, export_id, seller_id, date_from, date_to, "
        "scope=all_products_all_campaigns, currency=RUB, received_at (with offset), sha256. "
        "A workbook or filename alone does not establish its cabinet or reporting period."
    ))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--nm-id", help="Restrict the preview to a single product")
    args = parser.parse_args()
    raw = args.workbook.read_bytes()
    receipt = json.loads(args.receipt.read_text())
    seller = canonical_seller_id()
    uri = args.db.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        now = datetime.now(timezone.utc)
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        outcome, _ = preview(conn, raw, receipt, seller, now, nm_id=args.nm_id)
    print(json.dumps(outcome, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
