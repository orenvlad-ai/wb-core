"""Offline source qualification, real admission/consumer/selector and atomic Finance writes."""
from contextlib import closing, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from apps.finance_daily_publication import FinanceDailyPublicationAdapter, CONTROL_FILES
from apps.finance_daily_report_contract_smoke import row as source_row
from packages.application.finance_daily_publication import assemble, project, SKU_METRICS, TOTAL_METRICS, STATUS_KEY
from packages.application.web_vitrina_management_history import digest
from packages.contracts.source_attempt_diagnostics import source_digest
from packages.application.storage_registry import build_manifest, manifest_payload
from packages.domain.finance_daily_report import SELLER_MONEY_FIELDS, SELLER_PRODUCT_FIELDS
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock, _capture_live_source, SOURCE_TEMPORAL_POLICIES
from packages.application.sheet_vitrina_v1_plan_report import _sum_snapshot_metric
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
DAY = '2026-09-10'; OBS = '2026-09-11T12:00:00Z'; NMS = list(range(1001, 1093))


def seller_row(rrd, name, **money):
    return {'rrDate': DAY, 'rrdId': rrd, 'nmId': 0, 'sellerOperName': name,
            **{k: 0 for k in SELLER_MONEY_FIELDS}, **{k: '' for k in SELLER_PRODUCT_FIELDS}, **money}


def source(rows=None):
    if rows is None:
        rows = [source_row(i + 1, n, rrDate=DAY) for i, n in enumerate(NMS[:33])]
        rows += [seller_row(34, 'Возмещение за выдачу и возврат товаров на ПВЗ', ppvzReward=12, vw=-10, vwNds=-2),
                 seller_row(35, 'Удержание', deduction=380)]
    return {'date': DAY, 'date_from': DAY, 'date_to': DAY, 'endpoint': 'POST /api/finance/v1/sales-reports/detailed', 'period': 'daily',
            'report': {'rows': rows, 'pages': int(bool(rows)), 'rrd_id_end': rows[-1]['rrdId'] if rows else 0, 'terminal_status': 204,
                       'source_digest': source_digest(rows), 'source_observed_at': OBS}}


def plan():
    header = ['source_key', 'kind', 'freshness', 'snapshot_date', 'date', 'date_from', 'date_to', 'requested_count', 'covered_count', 'missing_nm_ids', 'note']
    keys = [f'SKU:{n}|{k}' for n in NMS for k in SKU_METRICS] + ['TOTAL|' + k for k in TOTAL_METRICS]
    return {'as_of_date': DAY, 'date_columns': [DAY, '2026-09-11'], 'metadata': {'neighbour': 'retained'}, 'sheets': [
        {'sheet_name': 'DATA_VITRINA', 'header': ['label', 'key', DAY, '2026-09-11'], 'rows': [[k, k, '', 77] for k in keys] + [['proxy', 'SKU:1001|proxy_profit_4_rub', 123, 456]]},
        {'sheet_name': 'STATUS', 'header': header, 'rows': [[STATUS_KEY, 'closure_exhausted', '', '', '', '', '', 92, 0, '', 'old'],
                                                        ['fin_report_daily[today_current]', 'error', '', '', '', '', '', 92, 0, '', 'unchanged today']]}]}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); self.runtime = root / 'state'; self.db = self.runtime / 'generations/g/db.sqlite'
        self.db.parent.mkdir(parents=True); (root / 'app').mkdir(); (root / 'app/.wb-core-runtime-sha').write_text('a' * 40)
        m = build_manifest(state='cutover', canonical_source='split', generation_epoch='g', raw_generation_id='raw-g', raw_relative_path='generations/g/raw.sqlite', raw_watermark='0', operational_generation_id='op-g', operational_relative_path='generations/g/db.sqlite', operational_watermark='0', rollback_generation_id='monolith', source_fingerprint='fixture', created_at=OBS)
        (self.runtime / 'storage_generation_manifest.json').write_text(json.dumps(manifest_payload(m)))
        controls = {name: {'active': False, 'revision': 72} for name in CONTROL_FILES}
        for name, value in controls.items(): (self.runtime / name).write_text(json.dumps(value))
        folder = self.runtime / 'private-evidence'; folder.mkdir(); self.src = source(); self.path = folder / 'source.json'; self.path.write_text(json.dumps(self.src))
        self.request = {'runtime_dir': str(self.runtime), 'storage_manifest_sha256': m.manifest_sha256, 'runtime_sha': 'a' * 40,
            'hostname': socket.gethostname(), 'control_digests': {n: digest(v) for n, v in controls.items()}, 'source_path': str(self.path),
            'source_sha256': digest(self.src), 'date': DAY, 'prepared_at': '2026-09-11T13:00:00Z', 'bundle_version': 'fixture',
            'snapshot_id': 'dated10', 'roster_nm_ids': NMS}
        self.adapter = FinanceDailyPublicationAdapter(now_factory=lambda: datetime(2026, 9, 12, 12, tzinfo=timezone.utc))
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('CREATE TABLE registry_upload_current_state(slot,bundle_version,activated_at);CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,plan_json,refreshed_at,activated_at,snapshot_id,plan_version,PRIMARY KEY(bundle_version,as_of_date));CREATE TABLE temporal_source_slot_snapshots(source_key,snapshot_date,snapshot_role,captured_at,payload_json,PRIMARY KEY(source_key,snapshot_date,snapshot_role));CREATE TABLE temporal_source_closure_state(source_key,target_date,slot_kind,state,attempt_count,next_retry_at,last_reason,last_attempt_at,last_success_at,accepted_at,PRIMARY KEY(source_key,target_date,slot_kind));')
            c.execute('INSERT INTO registry_upload_current_state VALUES(1,?,?)', ('fixture', OBS))
            c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)', ('fixture', DAY, json.dumps(plan()), OBS, OBS, 'dated10', 'v1'))

    def state(self):
        with closing(sqlite3.connect(self.db)) as c:
            return [c.execute('SELECT * FROM ' + t).fetchall() for t in ('sheet_vitrina_v1_ready_snapshots', 'temporal_source_slot_snapshots', 'temporal_source_closure_state')]

    def test_source_seller_and_no_activity(self):
        result = assemble(self.src, NMS); proof = result.diagnostics['finance_report']
        self.assertEqual(len(proof['observed_activity_nm_ids']), 33); self.assertEqual(len(proof['no_activity_nm_ids']), 59)
        groups = result.diagnostics['seller_operation_groups']; self.assertEqual(groups['Удержание']['money']['deduction'], 380)
        self.assertEqual(groups['Возмещение за выдачу и возврат товаров на ПВЗ']['money']['ppvzReward'], 12)
        self.assertEqual(sum(x.fin_deduction for x in result.items), 33)
        for key, value in (('quantity', 1), ('vendorCode', 'product'), ('retailAmount', 1), ('forPay', 1), ('acquiringFee', 1), ('sellerOperName', 'unknown')):
            bad = deepcopy(self.src['report']['rows']); bad[-1][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): assemble(source(bad), NMS)
        with self.assertRaisesRegex(ValueError, 'empty_unconfirmed'): assemble(source([]), NMS)

    def test_live_and_plan_report_validate_saved_proof(self):
        good = assemble(self.src, NMS)
        kw = dict(source_key='fin_report_daily', temporal_slot='yesterday_closed', temporal_policy=SOURCE_TEMPORAL_POLICIES['fin_report_daily'], column_date=DAY, requested_nm_ids=NMS)
        status, payload = _capture_live_source(**kw, loader=lambda: good)
        self.assertEqual(status.covered_count, 92); self.assertIsNotNone(payload)
        expected = sum(x.fin_buyout_rub for x in good.items if x.nm_id in NMS[:33])
        call = dict(expected_snapshot_date=DAY, allowed_nm_ids=set(NMS[:33]), result_kinds={'success'}, field_name='fin_buyout_rub')
        self.assertEqual(_sum_snapshot_metric(payload=good, **call), expected)
        bad = deepcopy(good); bad.diagnostics['covered_count'] = 0
        status, payload = _capture_live_source(**kw, loader=lambda: bad)
        self.assertEqual(status.kind, 'error'); self.assertIsNone(payload)
        with self.assertRaises(ValueError): _sum_snapshot_metric(payload=bad, **call)
        from dataclasses import asdict
        from apps.finance_daily_report_contract_smoke import namespace
        for representation in (lambda x: x, namespace):
            corrupt = asdict(good); corrupt['diagnostics']['finance_report'] = None
            with self.assertRaises(ValueError): _sum_snapshot_metric(payload=representation(corrupt), **call)
            legacy = asdict(good); legacy['diagnostics'].pop('finance_report')
            self.assertEqual(_sum_snapshot_metric(payload=representation(legacy), **call), expected)
        harness = NS(runtime=NS(load_temporal_source_slot_snapshot=lambda **kw: (bad, OBS)))
        self.assertIsNone(SheetVitrinaV1LivePlanBlock._load_slot_snapshot_status(harness, **kw, snapshot_role='accepted_closed_day_snapshot'))

    def test_atomic_apply_receipt_and_non_target(self):
        before = self.state(); preview = self.adapter.preview(self.request, 'fixture-finance-apply'); self.assertEqual(before, self.state())
        self.assertEqual(preview['scope']['target_cells'], 466)
        self.adapter.apply(self.request, 'fixture-finance-apply', preview)
        self.assertEqual(self.adapter.readback(self.request, 'fixture-finance-apply')['verified_cells'], 466)
        after = self.state(); self.assertEqual(after[1][0][2], 'accepted_closed_day_snapshot'); self.assertEqual(after[2][0][3], 'success')
        saved = json.loads(after[0][0][2]); self.assertEqual(saved['sheets'][1]['rows'][1], plan()['sheets'][1]['rows'][1])
        self.assertEqual([r[3] for r in saved['sheets'][0]['rows']], [r[3] for r in plan()['sheets'][0]['rows']])
        with self.assertRaises(ValueError): self.adapter.apply(self.request, 'fixture-finance-apply', preview)
        self.assertEqual(after, self.state())

    def test_source_and_prestate_drift(self):
        preview = self.adapter.preview(self.request, 'fixture-finance-drift')
        bad = deepcopy(self.src); bad['report']['rows'][0]['retailPriceWithDisc'] = 999; self.path.write_text(json.dumps(bad))
        with self.assertRaisesRegex(ValueError, 'source-artifact-drift'): self.adapter.apply(self.request, 'fixture-finance-drift', preview)
        self.path.write_text(json.dumps(self.src))
        with closing(sqlite3.connect(self.db)) as c, c: c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET refreshed_at='newer'")
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'cas-drift'): self.adapter.apply(self.request, 'fixture-finance-drift', preview)
        self.assertEqual(before, self.state())

    def test_failure_is_atomic_and_rollback_checks_later_writes(self):
        before = self.state(); preview = self.adapter.preview(self.request, 'fixture-finance-fail')
        with patch('apps.finance_daily_publication.replace_ready', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError): self.adapter.apply(self.request, 'fixture-finance-fail', preview)
        self.assertEqual(before, self.state()); self.assertTrue(Path(preview['recovery']['path']).is_file())
        preview = self.adapter.preview(self.request, 'fixture-finance-restore'); self.adapter.apply(self.request, 'fixture-finance-restore', preview)
        self.assertEqual(self.adapter.rollback(self.request, 'fixture-finance-restore')['state'], 'restored'); self.assertEqual(before, self.state())
        preview = self.adapter.preview(self.request, 'fixture-finance-newer'); self.adapter.apply(self.request, 'fixture-finance-newer', preview)
        with closing(sqlite3.connect(self.db)) as c, c: c.execute('UPDATE temporal_source_closure_state SET attempt_count=2')
        newer = self.state()
        with self.assertRaisesRegex(ValueError, 'after-image-drift'): self.adapter.rollback(self.request, 'fixture-finance-newer')
        self.assertEqual(newer, self.state())

    def test_ready_timestamp_and_identity_drift_block_readback_and_rollback(self):
        preview = self.adapter.preview(self.request, 'fixture-finance-row-drift')
        self.adapter.apply(self.request, 'fixture-finance-row-drift', preview)
        for column in ('refreshed_at', 'snapshot_id', 'plan_version', 'activated_at'):
            with closing(sqlite3.connect(self.db)) as c, c:
                c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET ' + column + '=?', ('newer-row',))
            newer = self.state()
            self.assertEqual(self.adapter.readback(self.request, 'fixture-finance-row-drift')['state'], 'ambiguous')
            with self.assertRaisesRegex(ValueError, 'after-image-drift'):
                self.adapter.rollback(self.request, 'fixture-finance-row-drift')
            self.assertEqual(newer, self.state())
            with closing(sqlite3.connect(self.db)) as c, c:
                c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET ' + column + '=?', (preview['candidate']['ready_after'][column],))

    def test_topology_and_existing_success_are_not_overwritten(self):
        result = assemble(self.src, NMS)
        for changed in ('duplicate', 'missing', 'date', 'value'):
            p = plan()
            if changed == 'duplicate': p['sheets'][0]['rows'].append(deepcopy(p['sheets'][0]['rows'][0]))
            if changed == 'missing': p['sheets'][0]['rows'].pop(0)
            if changed == 'date': p['as_of_date'] = '2026-09-09'
            if changed == 'value': p['sheets'][0]['rows'][0][2] = 1e9
            with self.subTest(changed=changed), self.assertRaises(ValueError): project(p, result, 'fixture-topology')

    def test_due_query_through_actual_scheduler_call(self):
        rows = []
        for day in ('2026-09-08', '2026-09-09', '2026-09-10', '2026-09-12'):
            rows.append(NS(source_key='fin_report_daily', target_date=day, slot_kind='yesterday_closed', state='closure_exhausted', last_attempt_at='2026-09-13T12:00:00Z'))
        rows.append(NS(source_key='stocks', target_date='2026-09-12', slot_kind='yesterday_closed', state='closure_exhausted', last_attempt_at='2026-09-13T12:00:00Z'))
        queries = []
        def states(**kw):
            queries.append(kw)
            return [s for s in rows if s.source_key in kw['source_keys'] and s.state in kw['states']]
        live = NS(runtime=NS(list_temporal_source_closure_states=states), now_factory=lambda: datetime(2026, 9, 14, 12, tzinfo=timezone.utc))
        due = lambda: SheetVitrinaV1LivePlanBlock.list_due_closed_day_retries(live)
        calls = []
        def refresh(**kw): calls.append(kw); return {'snapshot_id': 'fixture', 'refreshed_at': OBS}
        harness = NS(now_factory=live.now_factory, sheet_plan_block=NS(list_due_closed_day_retries=due, list_due_current_capture_retries=lambda **kw: []),
            _sheet_cycle_lock=nullcontext(), _run_sheet_refresh=refresh, runtime=NS(list_temporal_source_closure_states=lambda **kw: []),
            build_sheet_server_context=lambda: {}, build_sheet_manual_context=lambda: {})
        result = RegistryUploadHttpEntrypoint.run_sheet_temporal_closure_retry_cycle(harness)
        self.assertEqual(result['scheduled_dates'], ['2026-09-12']); self.assertEqual([c['as_of_date'] for c in calls], ['2026-09-12'])
        self.assertEqual(queries[1]['source_keys'], ['fin_report_daily'])
        live.now_factory = lambda: datetime(2026, 9, 13, 12, tzinfo=timezone.utc)
        self.assertEqual(due(), [])


if __name__ == '__main__':
    socket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('network forbidden'))
    unittest.main(verbosity=2)
