"""Offline regression for review rating through API, collection, storage and UI."""
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.sheet_vitrina_v1_stock_catalog_scope_smoke import seed
from packages.adapters.card_rating import HttpBackedCardRatingSource
from packages.application.card_rating import CardRatingBlock
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_card_rating import extend_metrics_with_card_rating, include_card_rating_rows
from packages.application.sheet_vitrina_v1_live_plan import SheetVitrinaV1LivePlanBlock, _MetricEvaluator
from packages.application.sheet_vitrina_v1_research import _aggregation_method, SheetVitrinaV1ResearchBlock
from packages.application.sheet_vitrina_v1_source_groups import WEB_VITRINA_SOURCE_GROUPS
from packages.application.sheet_vitrina_v1_web_vitrina import _effective_web_vitrina_metrics
from packages.application.web_vitrina_view_model import _resolve_cell_kind_and_formatter, _FORMATTER_LIBRARY
from packages.application.registry_upload_http_entrypoint import _metric_keys_for_source_keys, _merge_source_group_ready_snapshot
from packages.application.sheet_vitrina_v1_auto_refresh import SheetVitrinaV1AutoRefreshSchedulesBlock
from packages.application.web_vitrina_window_v3 import _HeaderScan, _catalog_core_rows, _catalog_finalize_rows, WindowV3Service
from packages.application.sheet_vitrina_v1_web_vitrina import SheetVitrinaV1WebVitrinaBlock
from packages.application.sheet_vitrina_v1_live_plan import bind_local_derive_publication
from threading import Event
from packages.contracts.card_rating import CardRatingRequest

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
KEYS = ['card_rating', 'avg_card_rating']


class SyntheticSource:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.values = [Decimal('4.75123456789123'), Decimal('3.87'), None]
    def fetch(self, request):
        self.calls.append(request)
        if self.fail:
            raise RuntimeError('synthetic unavailable')
        return {'snapshot_date': request.snapshot_date, 'observed_at': NOW.isoformat(),
            'data': {'items': [{'nmId': nm, 'rating': 10, 'sellerRating': 5,
                               'feedbackRating': {'current': self.values[index % len(self.values)]}}
                               for index, nm in enumerate(request.nm_ids)]}}


class CardRatingTests(unittest.TestCase):
    def test_actual_http_request_precision_and_rate_limit(self):
        source = HttpBackedCardRatingSource(now_factory=lambda: NOW)
        bodies = []
        def urlopen(req, timeout):
            self.assertEqual(req.full_url, 'https://seller-analytics-api.wildberries.ru/api/analytics/v2/item-rating')
            self.assertEqual(req.method, 'POST')
            body = json.loads(req.data)
            bodies.append(body)
            return io.BytesIO(json.dumps({'data': {'items': [{'nmId': nm, 'feedbackRating': {'current': 4.75123456789123}, 'rating': 10} for nm in body['nmIds']]}}).encode())
        runtime = SimpleNamespace(base_url='https://seller-analytics-api.wildberries.ru', token='fixture', timeout_seconds=1)
        HttpBackedCardRatingSource._last_request_at = None
        with patch('packages.adapters.card_rating.load_runtime_config', return_value=runtime), patch('packages.adapters.card_rating.urllib_request.urlopen', side_effect=urlopen), patch('packages.adapters.card_rating.time.monotonic', return_value=100), patch('packages.adapters.card_rating.time.sleep') as sleep:
            snapshot = CardRatingBlock(source).execute(CardRatingRequest('2026-10-04', list(range(1, 94)))).result
            self.assertEqual([len(body['nmIds']) for body in bodies], [50, 43])
            self.assertEqual(snapshot.items[0].card_rating_raw, '4.75123456789123')
            self.assertEqual(snapshot.items[0].card_rating, 4.75123456789123)
            sleep.assert_called_once_with(20.0)
        HttpBackedCardRatingSource._last_request_at = None
        for body in bodies:
            self.assertEqual(body['currentPeriod'], {'start': '2026-10-03', 'end': '2026-10-03'})
            self.assertEqual(body['orderBy'], {'field': 'feedbackCount', 'mode': 'desc'})
            self.assertFalse(body['isNotIncludeNmsWithoutSales'])
            self.assertFalse(body['onlyShadowedNms'])
            self.assertEqual(body['offset'], 0)
        with self.assertRaises(ValueError):
            source.fetch(CardRatingRequest('2026-10-03', [1]))

    def test_pagination_and_midnight_guard(self):
        runtime = SimpleNamespace(base_url='', token='', timeout_seconds=1)
        source = HttpBackedCardRatingSource(now_factory=lambda: NOW, page_limit=2)
        responses = [{'data': {'items': [{'nmId': 1}, {'nmId': 2}]}}, {'data': {'items': [{'nmId': 3}]}}]
        with patch('packages.adapters.card_rating.load_runtime_config', return_value=runtime), patch.object(source, '_post', side_effect=responses) as post:
            self.assertEqual(len(source.fetch(CardRatingRequest('2026-10-04', [1, 2, 3]))['data']['items']), 3)
            self.assertEqual([call.args[1]['offset'] for call in post.call_args_list], [0, 2])
        source.now_factory = iter([NOW, datetime(2026, 10, 5, 12, tzinfo=timezone.utc)]).__next__
        with patch('packages.adapters.card_rating.load_runtime_config', return_value=runtime), patch.object(source, '_post', return_value={'data': {'items': []}}), self.assertRaises(RuntimeError):
            source.fetch(CardRatingRequest('2026-10-04', [1]))

    def test_zero_missing_invalid_and_catalog(self):
        source = SyntheticSource()
        source.values = [0, None, 4.86]
        result = CardRatingBlock(source).execute(CardRatingRequest('2026-10-04', [1, 2, 3])).result
        self.assertEqual([item.card_rating for item in result.items], [None, None, 4.86])
        self.assertEqual(result.missing_nm_ids, [1, 2])
        for bad in [True, '4.5', float('nan'), float('inf'), -1, 10]:
            source.values = [bad]
            with self.assertRaises(ValueError):
                CardRatingBlock(source).execute(CardRatingRequest('2026-10-04', [1]))
        metrics = extend_metrics_with_card_rating([])
        self.assertEqual(extend_metrics_with_card_rating(metrics), metrics)
        self.assertEqual(set(_metric_keys_for_source_keys(metrics, source_keys=['card_rating'])), set(KEYS))
        self.assertIn('card_rating', WEB_VITRINA_SOURCE_GROUPS['wb_api']['source_keys'])
        self.assertEqual(_aggregation_method({'metric_format': 'rating'}), 'mean_observed_values')
        self.assertEqual(_resolve_cell_kind_and_formatter(column_id='date:2026-10-04', value=4.75, row_format='rating'), ('number', 'rating'))
        self.assertEqual(_FORMATTER_LIBRARY['rating'].decimals, 2)

    def test_common_cycle_rollover_stale_missing_and_storage(self):
        with TemporaryDirectory() as tmp:
            runtime = RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
            bundle = json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/registry_upload_bundle__fixture.json').read_text())
            self.assertEqual(runtime.ingest_bundle(bundle, activated_at="2026-10-04T12:00:00Z").status, 'accepted')
            seed(runtime, [(item["nm_id"], 1, 0) for item in bundle["config_v2"] if item["enabled"]])
            source = SyntheticSource()
            block = SheetVitrinaV1LivePlanBlock(runtime, card_rating_block=CardRatingBlock(source), now_factory=lambda: NOW)
            # This is the production common collector, including CAS local derivation.
            plan = block.build_plan(as_of_date='2026-10-03', source_keys=['card_rating'], metric_keys=KEYS, execution_mode="manual_operator")
            self.assertEqual(len(source.calls), 1)
            self.assertEqual(source.calls[0].snapshot_date, '2026-10-04')
            rows = {row[1]: row for row in plan.sheets[0].rows}
            nm = source.calls[0].nm_ids[0]
            self.assertEqual(rows[f'SKU:{nm}|card_rating'][2], '')
            self.assertEqual(rows[f'SKU:{nm}|card_rating'][3], 4.75123456789123)
            ratings = [row[3] for key,row in rows.items() if key.startswith('SKU:') and row[3] != '']
            self.assertEqual(rows['TOTAL|avg_card_rating'][3], sum(ratings)/len(ratings))
            stored, _ = runtime.load_temporal_source_slot_snapshot(source_key='card_rating', snapshot_date='2026-10-04', snapshot_role='accepted_current_snapshot')
            self.assertEqual(stored.items[0].card_rating_raw, '4.75123456789123')
            # Publish only into this disposable fixture and read actual ready/V3 cells.
            state = runtime.load_current_state()
            expected = runtime.prepare_sheet_vitrina_ready_publication(bundle_version=state.bundle_version, as_of_date=plan.as_of_date)
            state, expected = bind_local_derive_publication(runtime, plan, state, expected)
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state, refreshed_at='2026-10-04T12:00:00Z',
                plan=plan, expected=expected, build_inputs=plan.metadata.get('publication_inputs'))
            reader = SheetVitrinaV1WebVitrinaBlock(runtime=runtime, now_factory=lambda: NOW)
            contract = reader.build(page_route='/test', read_route='/test', as_of_date='2026-10-03',
                output_row_ids=frozenset([f'SKU:{nm}|card_rating', 'TOTAL|avg_card_rating']))
            read_rows = {row.row_id: row for row in contract.rows}
            research = SheetVitrinaV1ResearchBlock(runtime=runtime, web_vitrina_block=reader, now_factory=lambda: NOW)
            options = research.build_sku_group_comparison_options(page_route='/test', read_route='/test')
            rating_option = next(item for item in options['metric_options'] if item['metric_key'] == 'card_rating')
            self.assertEqual(rating_option['aggregation_method'], 'mean_observed_values')
            self.assertEqual(rating_option['metric_format'], 'rating')
            self.assertEqual(rating_option['metric_label'], 'Рейтинг карточки')

            self.assertEqual(read_rows[f'SKU:{nm}|card_rating'].values_by_date['2026-10-04'], 4.75123456789123)
            self.assertEqual(read_rows['TOTAL|avg_card_rating'].values_by_date['2026-10-03'], '')
            service = WindowV3Service(reader)
            try:
                owner = service._owner_key({'username': 'fixture'})
                manifest = json.loads(service._build_manifest(['2026-10-03', '2026-10-04'], owner, Event()).json_bytes)
                session = service._sessions[manifest['session_id']]
                rating_index = session.row_ids.index(f'SKU:{nm}|card_rating')
                chunk = json.loads(service._build_chunk(session, {'d': 0, 'r': rating_index, 'n': 1, 'i': [], 'g': ''}, Event()).json_bytes)
                cells = chunk['rows'][0]['values']
                self.assertEqual(cells[0][1], '')
                self.assertEqual(cells[1][1], 4.75123456789123)
                self.assertIn('rating', [item['formatter_id'] for item in manifest['table_surface']['formatters']])
            finally:
                service.close()

            old_json = json.dumps(vars(stored), default=lambda obj: vars(obj), sort_keys=True)
            source.fail = True
            failed = block.build_plan(as_of_date='2026-10-03', source_keys=['card_rating'], metric_keys=KEYS, execution_mode="manual_operator")
            self.assertEqual(failed.sheets[0].rows, plan.sheets[0].rows)
            self.assertEqual(failed.metadata['server_cell_presentation'][f'SKU:{nm}|card_rating']['2026-10-04']['quality_state'], 'stale')
            # Group refresh must move only the selected cell's value and quality,
            # including preserved observation time, through persisted ready reads.
            sentinel = {'quality_state': 'missing', 'reason': 'untouched old date'}
            plan.metadata['server_cell_presentation'][f'SKU:{nm}|card_rating']['2026-10-03'] = dict(sentinel)
            plan.metadata['server_cell_presentation']['TOTAL|unrelated'] = {'2026-10-04': {'reason': 'untouched metric'}}
            def verify_group_merge(partial, quality, value):
                merged, _ = _merge_source_group_ready_snapshot(
                    previous_plan=plan, partial_plan=partial, source_group_id='wb_api',
                    source_keys=['card_rating'], metric_keys=KEYS,
                    refreshed_at='2026-10-04T12:01:00Z', previous_refreshed_at='2026-10-04T12:00:00Z',
                    selected_as_of_date='2026-10-04', business_date='2026-10-04')
                presentation = merged.metadata['server_cell_presentation']
                self.assertEqual(presentation[f'SKU:{nm}|card_rating']['2026-10-03'], sentinel)
                self.assertEqual(presentation['TOTAL|unrelated'], plan.metadata['server_cell_presentation']['TOTAL|unrelated'])
                for row in merged.sheets[0].rows:
                    self.assertEqual(row[2], rows[row[1]][2])
                    self.assertEqual(presentation[row[1]]['2026-10-04'], partial.metadata['server_cell_presentation'][row[1]]['2026-10-04'])
                state = runtime.load_current_state()
                expected = runtime.prepare_sheet_vitrina_ready_publication(bundle_version=state.bundle_version, as_of_date=merged.as_of_date)
                state, expected = bind_local_derive_publication(runtime, partial, state, expected)
                runtime.save_sheet_vitrina_ready_snapshot(current_state=state, refreshed_at='2026-10-04T12:01:00Z',
                    plan=merged, expected=expected, build_inputs=merged.metadata.get('publication_inputs'))
                actual = reader.build(page_route='/test', read_route='/test', as_of_date='2026-10-03',
                    output_row_ids=frozenset([f'SKU:{nm}|card_rating', 'TOTAL|avg_card_rating']))
                for row in actual.rows:
                    self.assertEqual(row.presentation_by_date['2026-10-04']['quality_state'], quality)
                    self.assertEqual(row.presentation_by_date['2026-10-04']['source_observed_at'], NOW.isoformat())
                self.assertEqual(next(row for row in actual.rows if row.row_id == f'SKU:{nm}|card_rating').values_by_date['2026-10-04'], value)
                service = WindowV3Service(reader)
                try:
                    owner = service._owner_key({'username': 'fixture'})
                    manifest = json.loads(service._build_manifest(['2026-10-04'], owner, Event()).json_bytes)
                    session = service._sessions[manifest['session_id']]
                    indices = [session.row_ids.index(f'SKU:{nm}|card_rating'), session.row_ids.index('TOTAL|avg_card_rating')]
                    days, scoped = service._slice_rows(session, 0, Event(), indices)
                    self.assertEqual(days, ['2026-10-04'])
                    for row in scoped.values():
                        self.assertEqual(row.presentation_by_date['2026-10-04']['quality_state'], quality)
                        self.assertEqual(row.presentation_by_date['2026-10-04']['source_observed_at'], NOW.isoformat())
                    chunk = json.loads(service._build_chunk(session, {'d': 0, 'r': 0, 'n': 2, 'i': indices, 'g': ''}, Event()).json_bytes)
                    self.assertEqual(chunk['rows'][0]['values'][0][1], value)
                    quality_index = chunk['value_encoding']['fields'].index('quality_state') + 1
                    for row in chunk['rows']:
                        self.assertEqual(row['values'][0][quality_index], quality)
                finally:
                    service.close()
            verify_group_merge(failed, 'stale', 4.75123456789123)
            # A successful all-missing response supersedes the old observation.
            source.fail = False
            source.values = [None]
            missing = block.build_plan(as_of_date='2026-10-03', source_keys=['card_rating'], metric_keys=KEYS, execution_mode="manual_operator")
            self.assertTrue(all(row[3] == '' for row in missing.sheets[0].rows))
            verify_group_merge(missing, 'missing', '')
            # Restore first observation to prove immutable rollover and history.
            runtime.save_temporal_source_slot_snapshot(source_key='card_rating', snapshot_date='2026-10-04', snapshot_role='accepted_current_snapshot', captured_at="2026-10-04T12:00:00Z", payload=stored)
            block.now_factory = lambda: datetime(2026, 10, 5, 12, tzinfo=timezone.utc)
            tomorrow = block.build_plan(as_of_date='2026-10-04', source_keys=['card_rating'], metric_keys=KEYS, execution_mode="manual_operator")
            self.assertEqual(tomorrow.sheets[0].rows[0][2], plan.sheets[0].rows[0][3])
            state = runtime.load_current_state()
            expected = runtime.prepare_sheet_vitrina_ready_publication(bundle_version=state.bundle_version, as_of_date=tomorrow.as_of_date)
            state, expected = bind_local_derive_publication(runtime, tomorrow, state, expected)
            runtime.save_sheet_vitrina_ready_snapshot(current_state=state, refreshed_at='2026-10-05T12:00:00Z',
                plan=tomorrow, expected=expected, build_inputs=tomorrow.metadata.get('publication_inputs'))
            reader.now_factory = block.now_factory
            past_contract = reader.build(page_route='/test', read_route='/test', date_from='2026-10-04', date_to='2026-10-04',
                output_row_ids=frozenset([f'SKU:{nm}|card_rating', 'TOTAL|avg_card_rating']))
            self.assertEqual(next(row for row in past_contract.rows if row.row_id == f'SKU:{nm}|card_rating').values_by_date['2026-10-04'], 4.75123456789123)
            service = WindowV3Service(reader)
            try:
                owner = service._owner_key({'username': 'fixture'})
                manifest = json.loads(service._build_manifest(['2026-10-04'], owner, Event()).json_bytes)
                session = service._sessions[manifest['session_id']]
                indices = [session.row_ids.index(f'SKU:{nm}|card_rating'), session.row_ids.index('TOTAL|avg_card_rating')]
                chunk = json.loads(service._build_chunk(session, {'d': 0, 'r': 0, 'n': 2, 'i': indices, 'g': ''}, Event()).json_bytes)
                self.assertEqual(chunk['rows'][0]['values'][0][1], 4.75123456789123)
                self.assertEqual(chunk['rows'][1]['values'][0][1], rows['TOTAL|avg_card_rating'][3])
            finally:
                service.close()

            after, _ = runtime.load_temporal_source_slot_snapshot(source_key='card_rating', snapshot_date='2026-10-04', snapshot_role='accepted_current_snapshot')
            self.assertEqual(json.dumps(vars(after), default=lambda obj: vars(obj), sort_keys=True), old_json)
            # Legacy persisted registry/read contract gets built-ins as blank rows.
            metrics = {m.metric_key:m for m in _effective_web_vitrina_metrics(runtime.load_current_state().metrics_v2)}
            old_rows = include_card_rating_rows([], config=runtime.load_current_state().config_v2, dates=['2026-10-01'], metrics=metrics)
            self.assertTrue(old_rows)
            self.assertTrue(all(row.values_by_date['2026-10-01'] == '' for row in old_rows))
            self.assertEqual({row.metric_key for row in old_rows}, set(KEYS))
            catalog = _catalog_core_rows(_HeaderScan([], frozenset(['2026-10-01']), False),
                block=SimpleNamespace(runtime=runtime), business_date='2026-10-04', selected_dates=['2026-10-01'])
            catalog = _catalog_finalize_rows(catalog, block=SimpleNamespace(runtime=runtime))
            self.assertTrue(set(KEYS) <= {row.metric_key for row in catalog})
            self.assertTrue(all(not row.values_by_date for row in catalog if row.metric_key in KEYS))
            # Persisted schedules contain timings, never a frozen API source list.
            schedules = SheetVitrinaV1AutoRefreshSchedulesBlock(runtime_dir=Path(tmp), now_factory=lambda: NOW)
            legacy = {'contract_name': 'sheet_vitrina_v1_auto_refresh_schedules', 'contract_version': 'v1',
                'schedules': [{'id': 'legacy', 'enabled': True, 'local_time_hhmm': '10:00',
                    'timezone': 'Asia/Yekaterinburg', 'created_at': '2026-04-20T06:00:00Z',
                    'updated_at': '2026-04-20T06:00:00Z'}]}
            schedules.path.write_text(json.dumps(legacy))
            original = schedules.path.read_bytes()
            self.assertEqual(schedules.get_schedule('legacy')['local_time_hhmm'], '10:00')
            # Run the same full/group collector with other APIs replaced offline.
            for name in ['sales_funnel_history_block', 'sf_period_block', 'spp_block',
                         'stocks_block', 'ads_compact_block', 'fin_report_daily_block',
                         'prices_snapshot_block', 'ads_bids_block']:
                setattr(block, name, SimpleNamespace(execute=lambda request: SimpleNamespace(result=None)))
            block.now_factory = lambda: NOW
            requests_before = len(source.calls)
            group_sources = set(WEB_VITRINA_SOURCE_GROUPS['wb_api']['source_keys'])
            group_live = block._load_live_sources(enabled_config=[item for item in runtime.load_current_state().config_v2 if item.enabled],
                temporal_slots=plan.temporal_slots, cost_price_state=None, source_keys=group_sources)
            self.assertEqual(len(source.calls), requests_before + 1)
            self.assertIn('card_rating', {status.source_key for status in group_live.statuses})
            self.assertEqual(schedules.path.read_bytes(), original)

            # Filtered group aggregation uses exactly the same unweighted policy.
            source.values = [4.75, 3.87, None]
            block.now_factory = lambda: NOW
            config = [item for item in runtime.load_current_state().config_v2 if item.enabled]
            slots = plan.temporal_slots
            live = block._load_live_sources(config, slots, None, execution_mode='manual_operator', current_web_source_sync_note=None, source_keys={'card_rating'})
            evaluator = _MetricEvaluator(enabled_config=config, metrics_by_key=metrics, formulas_by_id={}, live_sources=live)
            for group, members in evaluator.grouped_config.items():
                values = [evaluator.resolve_sku('card_rating', item.nm_id, 'today_current') for item in members]
                values = [value for value in values if value is not None]
                self.assertEqual(evaluator.resolve_group('avg_card_rating', group, 'today_current'), sum(values)/len(values) if values else None)


if __name__ == '__main__':
    unittest.main()
