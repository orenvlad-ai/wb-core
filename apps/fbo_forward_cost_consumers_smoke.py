"""Intraday late expenses preserve earlier COGS and observed order segments."""
from copy import deepcopy
from contextlib import closing
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import fbo_forward_cost_consumers as consumer
from packages.application.vitrina_economics import project_catalog_economics, METRICS, TOTALS
from packages.application.wb_finance_weekly import cost_economic_signature
from apps import shared_sku_cost_reports_smoke as fixtures
from apps.shared_sku_cost_reports_smoke import DAY, WEEK_ONE, TARGET_NM, sale

PROXY_DAY = "2026-11-09"


def effect(*, identity="e1", day=DAY.isoformat(), amount="200", after="1200", count="2", included=()):
    return dict(effect_id=identity, nm_id=TARGET_NM, amount_rub=amount, status="posted",
        posted_at=day + "T10:00:00+00:00", snapshot=dict(wb_quantity="10",
            before_capital_rub="1000", after_capital_rub=after,
            before_wac_rub="100", after_wac_rub=str(Decimal(after) / 10),
            order_boundary=dict(status="ready", business_date=day, common_quantity="20",
                ff_quantity="10", ff_capital_rub="1000", included_effect_ids=list(included),
                operands=dict(orderCount=count, orderSum=str(Decimal(count) * 500),
                    our_wb_unit_cost_rub="100"), parameter_versions={"3":"p", "4":"p"})))


def plan(*, count=2, revenue=1000, cost=110, day=PROXY_DAY):
    values = dict(orderCount=count, orderSum=revenue, ads_sum=0, our_wb_unit_cost_rub=cost)
    rows = [["sku", f"SKU:{TARGET_NM}|{key}", value] for key, value in values.items()]
    rows += [["sku", f"SKU:{TARGET_NM}|{key}", ""] for key in METRICS]
    rows += [["total", f"TOTAL|{key}", ""] for key in TOTALS]
    return dict(date_columns=[day], metadata={"fbo_forward_cost_epochs":[effect(day=day)]},
        sheets=[dict(sheet_name="DATA_VITRINA", header=["label", "key", day], rows=rows)])


PARAMETERS = SimpleNamespace(version_id="p", buyout_rate=Decimal(1),
    retained_share=Decimal(1), included_expense_rate=Decimal(0))


class ConsumerTests(unittest.TestCase):
    def test_later_expense_preserves_prior_finance_and_proxy_results(self):
        # Real consumers must keep the prior day's full result unchanged when
        # a later expense exists, including evidence/quality and cache identity.
        fixture = fixtures.SharedCostReportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        block = fixture.candidate({DAY: "100"})
        rows = [sale(94)]
        future = effect(day=(DAY + timedelta(days=1)).isoformat())
        def finance(effects):
            with patch("packages.application.fbo_forward_cost_consumers.load_effects", return_value=effects), \
                 patch("packages.application.canonical_wb_cost_resolver._forward_effects", return_value=effects), \
                 closing(block.finance._connect()) as conn:
                return block.finance._calculate_cogs(conn, rows, WEEK_ONE, include_details=True)
        baseline = finance([])
        self.assertEqual(baseline["cogs_rub"], "100.0000")
        self.assertEqual(finance([future]), baseline)

        source = plan(cost=100)
        source["metadata"]["fbo_forward_cost_epochs"] = []
        baseline = project_catalog_economics(source, day=PROXY_DAY,
            parameters=(PARAMETERS, PARAMETERS))
        source["metadata"]["fbo_forward_cost_epochs"] = [effect(
            day=(date.fromisoformat(PROXY_DAY) + timedelta(days=1)).isoformat())]
        observed = project_catalog_economics(source, day=PROXY_DAY,
            parameters=(PARAMETERS, PARAMETERS))
        self.assertEqual(observed["sheets"], baseline["sheets"])
        self.assertEqual(observed["metadata"]["server_cell_presentation"],
            baseline["metadata"]["server_cell_presentation"])

    def test_epoch_prices_and_date_only_disclosure(self):
        base = dict(status="resolved", unit_cost_rub="160", source_digest="new-shared")
        def resolve(operation):
            return consumer.resolve_epoch_cost(base, effects=[effect()], nm_id=TARGET_NM,
                operation_date=DAY, operation=operation, common=True)
        self.assertEqual(resolve({"rrDate": DAY.isoformat()+"T09:00:00Z"})["unit_cost_rub"], "100")
        self.assertEqual(resolve({"rrDate": DAY.isoformat()+"T11:00:00Z"})["unit_cost_rub"], "110")
        frozen = resolve({"rrDate": DAY.isoformat()})
        self.assertEqual(frozen["unit_cost_rub"], "100")
        self.assertEqual(frozen["valuation_time_precision"], "date_only_frozen")
        self.assertTrue(frozen["valuation_time_reason"])
        self.assertEqual(resolve({"rrDate": DAY.isoformat()+"T11:00:00"})["valuation_time_precision"], "date_only_frozen")
        self.assertEqual(resolve({"rrDate": DAY.isoformat()+"T00:00:00Z"})["valuation_time_precision"], "date_only_frozen")
        self.assertEqual(resolve({"rrDate": DAY.isoformat(), "orderDt": DAY.isoformat()+"T11:00:00Z"})["valuation_time_precision"], "date_only_frozen")
        self.assertEqual(consumer.resolve_epoch_cost(base, effects=[effect()], nm_id=TARGET_NM,
            operation_date=DAY-timedelta(days=1), common=True), base)
        self.assertEqual(consumer.resolve_epoch_cost(base, effects=[effect()], nm_id=int(TARGET_NM)+1,
            operation_date=DAY, common=True), base)

    def test_proxy_preserves_published_orders_and_handles_repetition_regression(self):
        def projected(source):
            return project_catalog_economics(source, day=PROXY_DAY, parameters=(PARAMETERS, PARAMETERS))
        def profit(source):
            return next(r[2] for r in source["sheets"][0]["rows"] if r[1]==f"SKU:{TARGET_NM}|proxy_profit_4_rub")
        self.assertEqual(profit(projected(plan())), 800)
        result = projected(plan(count=3, revenue=1500))
        self.assertEqual(profit(result), 1190)
        precision = result["metadata"]["server_cell_presentation"][f"SKU:{TARGET_NM}|proxy_profit_4_rub"][PROXY_DAY]
        self.assertEqual(precision["quality_state"], "partial")
        self.assertTrue(precision["evidence"]["forward_cost_segments"]["precision_pending"])
        self.assertEqual(projected(result), result)
        source = plan(count=3, revenue=1500, cost=120)
        source["metadata"]["fbo_forward_cost_epochs"].append(effect(identity="e2", after="1400", day=PROXY_DAY))
        self.assertEqual(profit(projected(source)), 1180)
        bad = projected(plan(count=1, revenue=500))
        self.assertEqual(profit(bad), "")
        self.assertIn("после расхода", bad["metadata"]["server_cell_presentation"][f"SKU:{TARGET_NM}|proxy_profit_4_rub"][PROXY_DAY]["reason"])
        self.assertEqual(profit(projected(plan(count=2, revenue=1100))), "")

    def test_boundary_is_pinned_to_accepted_ready_and_current_ff(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("CREATE TABLE sheet_vitrina_v1_warehouse_functional_versions(version_id,business_effective_date)")
            conn.execute("CREATE TABLE sheet_vitrina_v1_warehouse_functional_active(slot,version_id)")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_versions VALUES('v',?)", (DAY.isoformat(),))
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_active VALUES(1,'v')")
            conn.execute("CREATE TABLE sheet_vitrina_v1_warehouse_functional_balances(version_id,warehouse_key,nm_id,quantity,capital_rub,cost_covered_quantity)")
            conn.execute("INSERT INTO sheet_vitrina_v1_warehouse_functional_balances VALUES('v','ff',?,10,'1000',10)", (TARGET_NM,))
            conn.execute("CREATE TABLE registry_upload_current_state(slot,bundle_version)")
            conn.execute("INSERT INTO registry_upload_current_state VALUES(1,'current-bundle')")
            conn.execute("CREATE TABLE sheet_vitrina_v1_ready_snapshots(snapshot_id,refreshed_at,plan_json,as_of_date,bundle_version)")
            self.assertEqual(consumer.capture_order_boundary(conn,TARGET_NM)["status"], "pending")
            source = plan(cost=100, day=DAY.isoformat())
            source["metadata"] = {}
            source["sheets"][0]["rows"] += [["sku",f"SKU:{TARGET_NM}|own_capital_{stage}_qty",10] for stage in ("WB","FF")]
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES('r','current',?,?, 'current-bundle')", (json.dumps(source),DAY.isoformat()))
            wrong = deepcopy(source)
            wrong['sheets'][0]['rows'][0][2] = 999
            conn.execute("INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES('wrong','zz-later',?,?, 'old-bundle')", (json.dumps(wrong),DAY.isoformat()))
            boundary = consumer.capture_order_boundary(conn,TARGET_NM)
            self.assertEqual(boundary["status"], "ready")
            self.assertEqual(boundary["ff_capital_rub"], "1000")
            self.assertEqual(boundary["ready_snapshot_id"], "r")
            self.assertEqual(boundary["bundle_version"], "current-bundle")
            self.assertEqual(boundary, consumer.capture_order_boundary(conn,TARGET_NM))
            self.assertEqual(boundary["proof_digest"], consumer.digest({k:v for k,v in boundary.items() if k!="proof_digest"}))

    def test_same_timestamp_uses_immutable_posting_sequence(self):
        first, second = effect(identity='z-first'), effect(identity='a-second', amount='400', after='1600')
        first['snapshot']['posting_sequence'] = 1
        second['snapshot']['posting_sequence'] = 2
        second['snapshot']['before_wac_rub'] = '120'
        result = consumer.resolve_epoch_cost(dict(status='resolved',unit_cost_rub='999'),
            effects=[second,first],nm_id=TARGET_NM,operation_date=DAY,
            operation={'rrDate':DAY.isoformat()+'T11:00:00Z'},common=True)
        self.assertEqual(result['unit_cost_rub'],'130')
        before = consumer.resolve_epoch_cost(dict(status='resolved',unit_cost_rub='999'),
            effects=[second,first],nm_id=TARGET_NM,operation_date=DAY,
            operation={'rrDate':DAY.isoformat()+'T09:00:00Z'},common=True)
        self.assertEqual(before['unit_cost_rub'],'100')

    def test_finance_cache_and_partner_signature_share_intraday_epochs(self):
        fixture = fixtures.SharedCostReportTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        block = fixture.candidate({DAY:"160"})
        rows = [sale(91, rrDate=DAY.isoformat()+"T09:00:00Z"),
                sale(92, rrDate=DAY.isoformat()+"T11:00:00Z"), sale(93)]
        patches = [patch("packages.application.fbo_forward_cost_consumers.load_effects", return_value=[effect()]),
                   patch("packages.application.canonical_wb_cost_resolver._forward_effects", return_value=[effect()])]
        with patches[0], patches[1], closing(block.finance._connect()) as conn:
            detail = block.finance._calculate_cogs(conn, rows, WEEK_ONE, include_details=True)
            self.assertEqual(detail["cogs_rub"], "310.0000")
            self.assertEqual([d["unit_cost_rub"] for d in detail["detail_rows"]], ["100.0000","110.0000","100.0000"])
            self.assertEqual(detail["quality"]["forward_date_only_units"], 1)
            self.assertEqual(detail["estimated_units"], 1)
            buckets, signature = cost_economic_signature(detail["detail_rows"])
            current, _, reason = block._current_cost_economic_signature(conn,nm_id=str(TARGET_NM),
                coverage=dict(cost_economic_buckets=buckets,cost_economic_signature_version="wb_finance_cost_economic_signature_v1"),
                shared=block.finance.shared_cost_snapshot, canonical_snapshot=None)
            self.assertEqual(reason, "")
            self.assertEqual(signature,current)
            # A date's old cost is not invalidated merely by a new empty
            # optional epoch field. Different future epochs remain separate.
            baseline = deepcopy(detail["detail_rows"][0])
            no_epoch = {k:v for k,v in baseline.items() if k not in ("forward_cost_epoch","cost_effective_at")}
            self.assertEqual(cost_economic_signature([baseline]),cost_economic_signature([no_epoch]))


if __name__ == "__main__":
    unittest.main()
