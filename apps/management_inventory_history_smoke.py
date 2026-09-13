"""Eight end-to-end dated management inventory regressions, using local fixtures."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import hashlib
import os
import re
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_inventory_quantity_smoke import book_fixture, plan_fixture, binding
from apps.inventory_retention_publication import InventoryRetentionPublicationAdapter
from packages.application import fbs_accounting_runtime as accounting
from packages.application import sheet_vitrina_v1_inventory_history as history
from packages.application.fbs_snapshot_cost import fingerprint
from packages.application.inventory_quantity import CONTRACT, BOOK_SOURCE, bound_book_quantities
from packages.application.management_inventory_history import read_management_inventory_history
from packages.application.ready_publication import ExpectedReady, record_intent, complete_publication, digest
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime, _serialize_sheet_vitrina_plan
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.sheet_vitrina_v1_inventory_planning import (
    extend_rows_with_inventory_planning, apply_fbs_unavailable_presentation, _public_metric_specs,
    _historical_metric_value, inventory_planning_facility_metric_key,
)
from packages.application.web_vitrina_view_model import build_web_vitrina_view_model
from packages.application.web_vitrina_gravity_table_adapter import build_web_vitrina_gravity_table_adapter

DAY = '2026-09-12'
OBSERVED = DAY + 'T18:16:00Z'
CAPTURED = DAY + 'T18:23:11Z'


class ManagementInventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory(prefix='management-inventory-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = RegistryUploadDbBackedRuntime(runtime_dir=self.root / 'runtime')
        self.runtime.ingest_bundle(json.loads((ROOT / 'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text()), activated_at='2026-09-07T10:00:00Z')
        self.state = self.runtime.load_current_state()
        self.nms = [c.nm_id for c in self.state.config_v2 if c.enabled][:2]
        self.target = {'bundle_version': self.state.bundle_version, 'as_of_date': DAY}
        self.book = json.loads(json.dumps(book_fixture(self.nms)).replace('2026-09-08', DAY).replace('2026-09-09', '2026-09-13'))
        for day in (DAY, '2026-09-13'):
            payload = self.book['presentations'][day]
            for row in payload['quantity_snapshot']['rows']:
                row['quantity'] = (105863 if row['facility_id'] == 'moscow' else 23481) if row['nm_id'] == self.nms[0] else 0
            for row in self.book['wb_days'][day]['rows']:
                row['components']['physical'] = 37845 if row['nm_id'] == self.nms[0] else 0
            for nm, row in payload['rows'].items():
                row['wb_physical'] = 37845 if int(nm) == self.nms[0] else 0
                row['stock_total'] = 167189 if int(nm) == self.nms[0] else 0
            payload['totals'].update(wb_physical=37845, stock_total=167189)
            payload['version_id'] = fingerprint(payload)
            self.book['state']['periods'][day] = {'status': 'open', 'snapshot': deepcopy(payload['quantity_snapshot'])}
        self.book['effective_date'] = '2026-09-08'
        self.plan = plan_fixture(self.nms, DAY)
        self.seed(self.book)

    def seed(self, book):
        previous = accounting.load(self.runtime.runtime_dir)[1]
        self.version = accounting._save_book(self.runtime.runtime_dir, book, expected=previous, operation_id='fixture-' + fingerprint(book)[7:])
        bound = binding(book, DAY, self.target)
        metadata = {**self.plan.metadata, 'ready_publication_target': self.target, 'fbs_accounting_bindings': {DAY: bound}}
        for cells in metadata['server_cell_presentation'].values():
            cells[DAY] = {'source': BOOK_SOURCE}
        self.plan = replace(self.plan, metadata=metadata)
        raw = _serialize_sheet_vitrina_plan(self.plan)
        operands = bound_book_quantities(book=book, book_version=self.version, binding=bound, target=self.target, day=DAY)
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('INSERT OR REPLACE INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?,?,?,?)',
                (self.state.bundle_version, self.state.activated_at, DAY, self.plan.snapshot_id, self.plan.plan_version, CAPTURED, raw))
            self.capture = history.append_inventory_history_capture(conn, business_date=DAY, capture_kind='accepted_refresh',
                formula_version=CONTRACT, bundle_version=self.state.bundle_version, ready_snapshot_id=self.plan.snapshot_id,
                ready_plan_version=self.plan.plan_version, facility_roster=operands['facility_roster'],
                source_manifest=operands['source_manifest'], components=operands['components'], captured_at=CAPTURED)
            operation = 'accepted-' + self.version[7:]
            record_intent(conn, operation_id=operation, attempt_id='1', kind='book_ready',
                expected=ExpectedReady(self.state.bundle_version, DAY, None), inputs={}, expected_book=previous,
                book_required=True, ready_required=True, created_at=CAPTURED)
            complete_publication(conn, operation_id=operation, attempt_id='1', book_version=self.version, after_digest=digest(raw), finished_at=CAPTURED)
        self.operands = operands

    def read(self, current='2026-09-13', plan=None):
        return read_management_inventory_history(self.runtime.db_path, runtime_dir=self.runtime.runtime_dir,
            plan=plan or self.plan, current_date=current,
            lifecycle_quality_resolver=lambda *_: (_ for _ in ()).throw(AssertionError('typed evidence cannot scan Lifecycle')))

    def contract(self, day='2026-09-13'):
        return SheetVitrinaV1WebVitrinaBlock(runtime=self.runtime, now_factory=lambda: datetime.fromisoformat(day+'T10:00:00+00:00')).build(
            page_route='/sheet-vitrina-v1/vitrina', read_route='/v1/sheet-vitrina-v1/web-vitrina', date_from=DAY, date_to=DAY)

    def test_1_d_dplus1_dplus2_keep_exact_source_and_preliminary(self):
        selected = [self.read(day)['dates'][DAY] for day in (DAY, '2026-09-13', '2026-09-14')]
        self.assertEqual(selected[0], selected[1]); self.assertEqual(selected[1], selected[2])
        self.assertEqual(selected[0]['captured_at'], CAPTURED)
        self.assertFalse(selected[0]['finalization_id'])
        self.assertEqual(selected[0]['scopes']['TOTAL']['wb']['source_watermark'], DAY+'T18:19:00Z')

    def test_2_sku_total_api_view_table_and_export_contract(self):
        expected = {'stock_total': 167189, 'inventory_wb_total_qty_v1': 37845,
            inventory_planning_facility_metric_key('moscow'): 105863, inventory_planning_facility_metric_key('orenburg'): 23481}
        for now in (DAY, '2026-09-13', '2026-09-14'):
            contract = self.contract(now)
            rows = {r.row_id: r for r in contract.rows}
            api = json.loads(json.dumps(asdict(contract), ensure_ascii=False))
            view = build_web_vitrina_view_model(contract)
            adapter = asdict(build_web_vitrina_gravity_table_adapter(view))
            for key, value in expected.items():
                for scope, prefix, number in [('TOTAL', 'total_', value), (f'SKU:{self.nms[0]}', '', value), (f'SKU:{self.nms[1]}', '', 0)]:
                    rid = scope+'|'+prefix+key
                    self.assertEqual(rows[rid].values_by_date[DAY], number)
                    self.assertEqual(rows[rid].presentation_by_date[DAY]['publication_state'], 'preliminary')
                    self.assertIn('Предварительно', rows[rid].presentation_by_date[DAY]['quality_label'])
                    self.assertEqual(next(r for r in api['rows'] if r['row_id'] == rid)['values_by_date'][DAY], number)
                    self.assertEqual(next(r for r in adapter['rows'] if r['row_id'] == rid)['values']['date:'+DAY]['value'], number)
            self.assertFalse(contract.capabilities.exportable, 'the public export remains unavailable, never a stale independent data path')
        self.assertEqual(self.read()['dates'][DAY]['scopes']['TOTAL']['fbs_total'], 129344)
        if os.environ.get('WBC_MANAGEMENT_BROWSER') == '1':
            self.browser_check()

    def browser_check(self):
        from apps.sheet_vitrina_v1_web_vitrina_browser_smoke import LocalWebVitrinaFixtureServer
        from playwright.sync_api import sync_playwright, expect
        block = SheetVitrinaV1WebVitrinaBlock(runtime=self.runtime,
            now_factory=lambda: datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
        fixture = LocalWebVitrinaFixtureServer(with_ready_snapshot=False, now=datetime(2026, 9, 14, 10, tzinfo=timezone.utc))
        with fixture as url, sync_playwright() as playwright:
            fixture.entrypoint.web_vitrina_block = block
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                query = '?history_mode=explicit&date_from='+DAY+'&date_to='+DAY
                response = page.request.get(url+'/v1/sheet-vitrina-v1/web-vitrina'+query)
                self.assertEqual(response.status, 200)
                self.assertEqual(next(r for r in response.json()['rows'] if r['row_id']=='TOTAL|total_inventory_wb_total_qty_v1')['values_by_date'][DAY], 37845)
                page.goto(url+'/sheet-vitrina-v1/vitrina'+query)
                wb = page.locator('td[data-row-id="TOTAL|total_inventory_wb_total_qty_v1"][data-cell-date="'+DAY+'"]')
                expect(wb).to_have_text(re.compile(r'37\s845'), timeout=30000)
                self.assertIn('Предварительно', wb.get_attribute('title'))
                page.locator('[data-metrics-settings-open]').click()
                page.locator('[data-total-metric-key="total_'+inventory_planning_facility_metric_key('moscow')+'"] [data-metric-display-select]').select_option('shown')
                page.locator('[data-metrics-settings-close]').first.click()
                fbs = page.locator('td[data-row-id="TOTAL|total_'+inventory_planning_facility_metric_key('moscow')+'"][data-cell-date="'+DAY+'"]')
                expect(fbs).to_have_text(re.compile(r'105\s863'), timeout=30000)
                self.assertIn(OBSERVED, fbs.get_attribute('title'))
                if os.environ.get('WBC_MANAGEMENT_SCREENSHOT'):
                    page.screenshot(path=os.environ['WBC_MANAGEMENT_SCREENSHOT'])
            finally:
                browser.close()

    def test_3_finalization_wins_and_neighbor_unchanged(self):
        before = self.read()['dates'][DAY]
        components = deepcopy(self.operands['components'])
        for c in components:
            if c['component_kind'] == 'WB' and c['scope_key'] in ('TOTAL', f'SKU:{self.nms[0]}'):
                c['quantity'] += 1
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            final = history.append_inventory_history_capture(conn, business_date=DAY, capture_kind='historical_backfill',
                formula_version=CONTRACT, facility_roster=self.operands['facility_roster'], source_manifest=self.operands['source_manifest'],
                components=components, captured_at='2026-09-14T10:00:00Z')
            history.append_inventory_history_finalization(conn, business_date=DAY, capture_id=final['capture_id'],
                finalization_identity='new-confirmed-facts', finalized_at='2026-09-14T10:00:00Z', provenance={})
        self.assertNotEqual(self.read()['dates'][DAY]['capture_id'], before['capture_id'])
        rows = {r.row_id:r for r in self.contract().rows}
        self.assertEqual(rows['TOTAL|total_inventory_wb_total_qty_v1'].values_by_date[DAY], 37846)
        self.assertEqual(self.read(), self.read())
        self.assertNotIn('2026-09-11', self.read()['dates'])

    def test_4_unaccepted_capture_binding_and_content_drift_rejected(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            history.append_inventory_history_capture(conn, business_date=DAY, capture_kind='accepted_refresh',
                formula_version=CONTRACT, facility_roster=self.operands['facility_roster'], source_manifest={'unpublished': True},
                components=self.operands['components'], captured_at='2026-09-13T10:00:00Z')
        self.assertEqual(self.read(DAY)['dates'][DAY]['capture_id'], self.capture['capture_id'])
        for field in ('book_version', 'date', 'presentation_version', 'ready_target'):
            broken = deepcopy(self.plan.metadata); broken['fbs_accounting_bindings'][DAY][field] = 'wrong'
            self.assertNotIn(DAY, self.read(plan=replace(self.plan, metadata=broken))['dates'])
        bad = {(DAY, self.capture['capture_id'], self.capture['source_digest'])}
        with patch.object(history, 'KNOWN_BAD_CAPTURES', bad):
            self.assertIsNone(self.read()['dates'][DAY]['scopes']['TOTAL']['wb']['value'])
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            row = json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?', (DAY,)).fetchone()[0])
            row['metadata']['unreceipted_change'] = True
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?', (json.dumps(row), DAY))
        self.assertNotIn(DAY, self.read()['dates'])

    def test_5_missing_zero_partial_are_distinct(self):
        self.assertEqual(self.read()['dates'][DAY]['scopes'][f'SKU:{self.nms[1]}']['wb']['state'], 'exact_zero')
        book = deepcopy(self.book)
        book['presentations'][DAY]['quantity_snapshot']['complete'] = False
        book['state']['periods'][DAY]['snapshot'] = deepcopy(book['presentations'][DAY]['quantity_snapshot'])
        book['presentations'][DAY]['version_id'] = fingerprint(book['presentations'][DAY])
        self.seed(book)
        scope = self.read()['dates'][DAY]['scopes']['TOTAL']
        self.assertEqual(scope['quality'], 'partial'); self.assertEqual(scope['total'], 37845)
        self.assertEqual(scope['facilities']['moscow']['state'], 'missing')
        rows = {r.row_id:r for r in self.contract().rows}
        self.assertEqual(rows['TOTAL|total_stock_total'].presentation_by_date[DAY]['quality_state'], 'inventory_history_partial')
        self.assertEqual(rows['TOTAL|total_'+inventory_planning_facility_metric_key('moscow')].values_by_date[DAY], '')
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.execute('DELETE FROM sheet_vitrina_v1_ready_publications')
        self.assertNotIn(DAY, self.read()['dates'])

    def test_6_legacy_wb_only_and_combined_are_distinct(self):
        row = next(r for r in self.contract().rows if r.row_id == 'TOTAL|total_stock_total')
        for source, value, expected in [('ready_snapshot.stock_total.wb_only', 15, 15), (BOOK_SOURCE, 167189, ''),
                                       ('official_fbs_management_inventory_v1', 167189, ''), ('', 15, '')]:
            legacy = replace(row, values_by_date={DAY:value}, presentation_by_date={DAY:{'source':source}})
            rows = extend_rows_with_inventory_planning([legacy], planning={}, history={}, date_columns=[DAY], enabled_config=[])
            self.assertEqual(next(r for r in rows if r.metric_key == 'total_inventory_wb_total_qty_v1').values_by_date[DAY], expected)

    def test_7_paused_lifecycle_failed_new_attempt_and_get_readonly(self):
        real_connect = sqlite3.connect
        writes = []
        def ro_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            def authorize(action, table, *_):
                if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE): writes.append(table)
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorize)
            return conn
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            record_intent(conn, operation_id='failed-new-attempt', attempt_id='1', kind='book_ready',
                expected=ExpectedReady(self.state.bundle_version, DAY, None), inputs={}, expected_book='other',
                book_required=True, ready_required=True, created_at='2026-09-14T10:00:00Z')
        before = {p:p.read_bytes() for p in self.runtime.runtime_dir.rglob('*') if p.is_file()}
        with patch.object(sqlite3, 'connect', side_effect=ro_connect):
            contract = self.contract()
            certified = [r for r in contract.rows if r.presentation_by_date.get(DAY, {}).get('accepted_publication')]
            self.assertEqual(certified, apply_fbs_unavailable_presentation(certified, reason_ru='old Lifecycle paused'))
            self.assertEqual(self.read(), self.read())
        self.assertEqual(writes, [])
        self.assertEqual(before, {p:p.read_bytes() for p in before})

    def test_8_strict_consumers_stay_strict(self):
        self.assertNotIn(DAY, history.read_inventory_history_window(self.runtime.db_path, dates=[DAY], current_date='2026-09-13')['dates'])
        with self.assertRaisesRegex(ValueError, 'closed_period_required'):
            bound_book_quantities(book=self.book, book_version=self.version,
                binding=self.plan.metadata['fbs_accounting_bindings'][DAY], target=self.target, day=DAY, require_closed=True)
        self.assertEqual(self.contract().rows, self.contract().rows)

    def test_retention_pointer_exact_transition_one_submit_and_drift(self):
        from apps.production_apply_launcher import execute
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            before = dict(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots WHERE as_of_date=?', (DAY,)).fetchone())
            after = deepcopy(before)
            changed = json.loads(after['plan_json']); changed['metadata']['unrelated_recovery'] = 'accepted'
            after['plan_json'] = json.dumps(changed)
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?', (after['plan_json'], DAY))
        self.assertNotIn(DAY, self.read()['dates'])
        transition = {'operation_id': 'scoped-ready-recovery', 'phase': 'publication',
            'before_images': {'ready': [before]}, 'after_images': {'ready': [after]}}
        evidence = self.runtime.runtime_dir / 'evidence' / 'scoped-ready-recovery.before.json'
        evidence.parent.mkdir(exist_ok=True)
        evidence.write_text(json.dumps({'candidate': transition}))
        request = {'dates': [DAY], 'ready_target': self.target, 'transition_evidence': str(evidence),
            'transition_evidence_sha256': 'sha256:' + hashlib.sha256(evidence.read_bytes()).hexdigest(),
            'transition_candidate_sha256': fingerprint(transition), 'transition_operation_id': transition['operation_id']}
        outer = self
        class Adapter(InventoryRetentionPublicationAdapter):
            def target(self, request):
                return outer.runtime.runtime_dir.resolve(), outer.runtime.db_path.resolve()
        adapter = Adapter(); op = 'inventory-retention-fixture'; preview = adapter.preview(request, op)
        args = dict(adapter_name='fixture', operation_id=op, request=request, adapters={'fixture': adapter},
            expected_prestate=preview['prestate_sha256'], expected_candidate=preview['candidate_sha256'])
        receipt = execute(action='apply', **args)
        self.assertEqual(receipt['state'], 'applied')
        self.assertEqual(self.read()['dates'][DAY]['scopes']['TOTAL']['total'], 167189)
        with patch.object(adapter, 'apply', side_effect=AssertionError('never a second submit')):
            self.assertEqual(execute(action='apply', **args)['state'], 'applied')
        with closing(sqlite3.connect(self.runtime.db_path)) as conn, conn:
            changed['metadata']['another_unaccepted_change'] = True
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE as_of_date=?', (json.dumps(changed), DAY))
        self.assertNotIn(DAY, self.read()['dates'])


if __name__ == '__main__':
    unittest.main()
