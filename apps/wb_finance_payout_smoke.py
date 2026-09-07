"""Regression: official settlement, signed loyalty, preserved COGS, and CAS."""

from __future__ import annotations
from datetime import date
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from packages.application.wb_finance_payout import (
    loyalty,
    patch_settlement,
    reconcile,
)
from packages.application.wb_finance_payout_apply import FinancePayoutAdapter, digest


def run() -> None:
    template = (ROOT / "packages/adapters/templates/sheet_vitrina_v1_operator.html").read_text()
    assert "За товар до удержаний" in template
    assert "Расчётная выплата" in template
    assert "calculated_payout" in template
    from apps.wb_finance_weekly_smoke import _fixture_rows, _seed_canonical_cost
    from packages.application.wb_finance_weekly import WbFinanceWeeklyBlock
    from packages.application.finance_raw_storage import (
        ensure_raw_schema,
        ensure_operational_schema,
        bind_generation_identity,
    )
    from packages.application.storage_registry import (
        build_manifest,
        atomic_write_manifest,
    )
    from packages.adapters.wb_finance_api import (
        WbFinanceApiClient,
        FinanceHttpResult,
        FinanceApiError,
    )

    assert loyalty(
        {
            "docTypeName": "Возврат",
            "cashbackAmount": "40",
            "cashbackCommissionChange": "4",
        }
    ) == (D("-40"), D("-4"))
    try:
        loyalty({"cashbackAmount": "1"})
    except ValueError:
        pass
    else:
        raise AssertionError("unsigned loyalty accepted")
    with TemporaryDirectory() as tmp:
        runtime = Path(tmp) / "state"
        runtime.mkdir()
        (Path(tmp) / "app").mkdir()
        (Path(tmp) / "app" / ".wb-core-runtime-sha").write_text("a" * 40)
        block = WbFinanceWeeklyBlock(runtime, seller_id="seller-1")
        block.ensure_schema()
        _seed_canonical_cost(block.db_path)
        gen = runtime / "generations" / "test"
        gen.mkdir(parents=True)
        op = gen / "operational.sqlite3"
        raw = gen / "finance_raw.sqlite3"
        with sqlite3.connect(block.db_path) as src, sqlite3.connect(op) as dst:
            src.backup(dst)
            dst.row_factory = sqlite3.Row
            dst.execute("DROP TABLE wb_finance_weekly_raw_rows")
            ensure_operational_schema(dst)
            bind_generation_identity(
                dst,
                logical_store="operational",
                generation_id="op",
                generation_epoch="test",
                source_fingerprint="sha256:" + "a" * 64,
            )
            dst.commit()
        with sqlite3.connect(raw) as conn:
            conn.row_factory = sqlite3.Row
            ensure_raw_schema(conn)
            bind_generation_identity(
                conn,
                logical_store="finance_raw",
                generation_id="raw",
                generation_epoch="test",
                source_fingerprint="sha256:" + "a" * 64,
            )
            conn.commit()
        manifest = build_manifest(
            state="cutover",
            canonical_source="split",
            generation_epoch="test",
            raw_generation_id="raw",
            raw_relative_path=str(raw.relative_to(runtime)),
            raw_watermark="0",
            operational_generation_id="op",
            operational_relative_path=str(op.relative_to(runtime)),
            operational_watermark="fixture",
            rollback_generation_id="monolith",
            source_fingerprint="sha256:" + "a" * 64,
        )
        atomic_write_manifest(runtime / "storage_generation_manifest.json", manifest)
        block = WbFinanceWeeklyBlock(runtime, seller_id="seller-1")
        rows = _fixture_rows()
        rows.append(
            {
                **rows[0],
                "rrdId": 999991,
                "quantity": 0,
                "retailPriceWithDisc": "0",
                "forPay": "0",
                "acquiringFee": "0",
                "docTypeName": "Возврат",
                "cashbackAmount": "40",
                "cashbackCommissionChange": "4",
                "sellerOperName": "Стоимость участия в программе лояльности",
            }
        )
        ws, we = date(2026, 6, 22), date(2026, 6, 28)
        m = block.ingest_week(ws, we, rows)["aggregate"]
        assert m["loyalty_points"] == "-40.0000" and m["loyalty_fee"] == "-4.0000"
        assert m["cogs"] == "200.0000"
        with sqlite3.connect(op) as conn:
            conn.row_factory = sqlite3.Row
            stored = conn.execute(
                "SELECT * FROM wb_finance_weekly_aggregates"
            ).fetchone()
            ids = json.loads(stored["report_ids_json"])
            net = (
                D(m["net_revenue"])
                - D(m["total_wb_expenses"])
                + D(m["positive_adjustments"])
            )
            summaries = []
            for i, rid in enumerate(ids):
                r = {
                    "reportId": rid,
                    "dateFrom": str(ws),
                    "dateTo": str(we),
                    "currency": "RUB",
                    "reportType": i + 1,
                    "createDate": "2026-06-29",
                }
                for api, key in [
                    ("forPaySum", "to_seller"),
                    ("deliveryServiceSum", "logistics"),
                    ("paidStorageSum", "storage"),
                    ("paidAcceptanceSum", "acceptance"),
                    ("penaltySum", "penalties"),
                    ("additionalPaymentSum", "wb_remuneration_adjustment"),
                    ("cashbackAmountSum", "loyalty_points"),
                    ("cashbackCommissionChangeSum", "loyalty_fee"),
                ]:
                    r[api] = m[key] if i == 0 else "0"
                r["deductionSum"] = (
                    str(
                        sum(
                            D(m[k])
                            for k in (
                                "marketing",
                                "transit_logistics",
                                "subscriptions",
                                "paid_services",
                                "review_points",
                                "other_deductions",
                            )
                        )
                    )
                    if i == 0
                    else "0"
                )
                r["bankPaymentSum"] = str(net) if i == 0 else "0"
                summaries.append(r)
            assert (
                reconcile(m, summaries, ids, str(ws), str(we))["payout_status"] == "ok"
            )
            assert (
                reconcile(m, summaries[:-1], ids, str(ws), str(we))[
                    "official_bank_payment_sum"
                ]
                is None
            )
            assert (
                reconcile(m, summaries + [summaries[0]], ids, str(ws), str(we))[
                    "payout_status"
                ]
                == "incomplete"
            )
            missing = [dict(r) for r in summaries]
            missing[0]["bankPaymentSum"] = None
            assert (
                reconcile(m, missing, ids, str(ws), str(we))[
                    "official_bank_payment_sum"
                ]
                is None
            )
            bad = [dict(r) for r in summaries]
            bad[0]["bankPaymentSum"] = "99999"
            assert (
                reconcile(m, bad, ids, str(ws), str(we))["payout_status"] == "mismatch"
            )
            # Simulate legacy persisted metrics in total and SKU rows.
            for table in (
                "wb_finance_weekly_aggregates",
                "wb_finance_weekly_sku_aggregates",
            ):
                for row in conn.execute(
                    "SELECT rowid,metrics_json FROM " + table
                ).fetchall():
                    original = json.loads(row["metrics_json"])
                    legacy = patch_settlement(
                        original,
                        D(0),
                        D(0),
                        D(original["corrections"]),
                        D(original["positive_adjustments"]),
                    )
                    legacy.pop("loyalty_points")
                    legacy.pop("loyalty_fee")
                    conn.execute(
                        "UPDATE " + table + " SET metrics_json=? WHERE rowid=?",
                        (json.dumps(legacy), row["rowid"]),
                    )
            conn.commit()
        source = {"status": 200, "data": summaries}
        path = Path(tmp) / "source.json"
        path.write_text(json.dumps(source))
        req = {
            "runtime_dir": str(runtime),
            "runtime_sha": "a" * 40,
            "storage_manifest_sha256": manifest.manifest_sha256,
            "source_path": str(path),
            "source_sha256": digest(source),
            "weeks": [str(ws)],
            "seller_id": "seller-1",
        }
        adapter = FinancePayoutAdapter()
        preview = adapter.preview(req, "payout-test")
        assert adapter.readback(req, "payout-test")["state"] == "not_submitted"
        drift = {**preview, "candidate_sha256": "sha256:" + "0" * 64}
        try:
            adapter.apply(req, "payout-test", drift)
        except ValueError:
            pass
        else:
            raise AssertionError("drift accepted")
        adapter.apply(req, "payout-test", preview)
        assert adapter.readback(req, "payout-test")["state"] == "applied"
        with sqlite3.connect(op) as conn:
            now = json.loads(
                conn.execute(
                    "SELECT metrics_json FROM wb_finance_weekly_aggregates"
                ).fetchone()[0]
            )
            assert now["cogs"] == m["cogs"]
            assert now["profit_after_cogs"] == m["profit_after_cogs"]
            assert now["calculated_payout"] == str(net.quantize(D(".0001")))
        # No official API dependency: normal recalculation derives the same payout.
        after = block.recalculate_week(ws, we)
        assert after["calculated_payout"] == now["calculated_payout"]
        assert "bank_payment_sum" not in after


if __name__ == "__main__":
    run()
    print(
        "finance payout: calculated settlement, signed loyalty, CAS, COGS preservation: ok"
    )
