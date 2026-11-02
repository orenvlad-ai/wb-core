#!/usr/bin/env python3
"""Synthetic native WB replay and source-preserving historical stage tests."""
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
import json
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from packages.application import fbs_historical_fbo_stages as stage
from packages.application import warehouse_functional as wf
from packages.application.fbs_snapshot_cost import fingerprint


def fixture():
    raw=dict(supply_id='one',wb_supply_id='one',status_id=5,normalized_row_json=json.dumps(dict(status_id=5,actual_acceptance_date='2026-10-20')),
        raw_goods_json=json.dumps([dict(nmID=1,barcode='a',quantity=10,acceptedQuantity=8)]))
    movement=dict(supply_id='one',nm_id=1,quantity_delta=-10,capital_delta_rub='-1000',wac_rub='100')
    plan=dict(plan_digest=fingerprint('test-native'),effects=[dict(kind='historical_wb_dispatch',business_date='2026-10-19',pool_movements=[movement])])
    def actual(day,q,kind='hourly_wb_sync'):
        version=dict(version_id='v'+day,cutover_id=wf.FUNCTIONAL_CUTOVER_ID,version_kind=kind,status='good',effective_at=day+'T12:00:00Z',created_at=day+'T12:00:00Z')
        snapshot=dict(snapshot_id='s'+day,version_id=version['version_id'],snapshot_date=day,items_json=json.dumps([dict(nm_id=1,quantity=str(q),wb_contour_quantity=str(q),in_way_to_client='0',in_way_from_client='0')]))
        return dict(version=version,snapshot=snapshot)
    opening=actual('2026-10-15',100,'functional_cutover');current=actual('2026-10-21',90)
    balance=dict(version_id=opening['version']['version_id'],warehouse_key='wb',nm_id=1,quantity='100',capital_rub='5000',wac_rub='50',cost_covered_quantity='100',provenance_json='{}')
    return dict(native_plan=plan,supplies={'one':{'rawsource':raw}},cost_layers=[],acceptance_events=[],dates={'2026-10-21':current},
        wb_replay=dict(cutover=dict(cutover_id=wf.FUNCTIONAL_CUTOVER_ID,cutover_at='2026-10-15T12:00:00Z'),cutover_version=opening['version'],cutover_snapshot=opening['snapshot'],
            opening_balances=[balance],snapshots_by_day={'2026-10-15':opening,'2026-10-21':current},initial_anchors=[],acceptance_events=[],
            opening_cost_map=[dict(nm_id=1,wb_unit_cost_rub='50',quality='opening',provenance={})]))


class StageTests(unittest.TestCase):
    def test_native_acceptance_exact_base_with_pending_addons_and_shortage(self):
        source,events=stage._sources(fixture())
        self.assertEqual(events[0]['quantity'],'8');self.assertEqual(events[0]['capital_rub'],'800')
        self.assertEqual(source[0]['short_quantity'],'2')
        self.assertEqual(source[0]['provenance']['cost_freshness'],'preliminary')

    def test_confirmed_fee_components_count_once_unknown_is_not_zero(self):
        cap=fixture();cap['cost_layers']=[dict(wb_supply_id='one',nm_id=1,transit_cost_status='direct_zero_confirmed',transit_per_unit_rub=0,ff_services_per_unit_rub=3,ff_storage_per_unit_rub=0,wb_acceptance_per_accepted_unit_rub=2,inputs_hash='fee-proof')]
        source,events=stage._sources(cap)
        self.assertEqual(source[0]['pre_cost'],'103');self.assertEqual(events[0]['capital_rub'],'840')
        cap['cost_layers'][0]['wb_acceptance_per_accepted_unit_rub']=None
        source,events=stage._sources(cap)
        self.assertEqual(source[0]['accepted_cost'],'100');self.assertEqual(source[0]['provenance']['cost_freshness'],'preliminary')

    def test_known_ff_fees_survive_unknown_wb_addons_exactly_once(self):
        cap=fixture()
        cap['cost_layers']=[dict(wb_supply_id='one',nm_id=1,transit_cost_status='unknown_route',transit_per_unit_rub=None,
            ff_services_amount_total='30',ff_storage_amount_total='0',qty_denominator=10,ff_services_per_unit_rub='3',ff_storage_per_unit_rub='0',wb_acceptance_per_accepted_unit_rub=None,inputs_hash='known-fee',
            component_status_json=json.dumps({'historical_fbo_native_source':{'contract':'historical_fbo_frozen_native_cost_layers_v1',
                'unit_cost_rub':'100','capital_delta_rub':'-1000','quantity_delta':-10,'plan_digest':cap['native_plan']['plan_digest']}}))]
        sources,events=stage._sources(cap)
        self.assertEqual(sources[0]['pre_cost'],'103')
        self.assertEqual(events[0]['capital_rub'],'824')
        shortage=Decimal(sources[0]['short_quantity'])*Decimal(sources[0]['pre_cost'])
        self.assertEqual(Decimal(events[0]['capital_rub'])+shortage,Decimal('1030'))
        self.assertEqual(sources[0]['provenance']['missing_cost_components'],['wb_transit','wb_paid_acceptance'])
        self.assertEqual(sources[0]['provenance']['cost_freshness'],'preliminary')
        current=next(r for r in stage._scratch_replay(cap,events) if r['as_of_date']=='2026-10-21')
        self.assertEqual(current['quality'],'periodic_snapshot_wac_provisional')

    def test_long_native_money_uses_same_ratio_as_ordinary_ledger_reader(self):
        cap=fixture();move=cap['native_plan']['effects'][0]['pool_movements'][0]
        move['wac_rub']='125.48007301400825006997979533820774374424019620883'
        with localcontext() as context:
            context.prec=50
            move['capital_delta_rub']=str(Decimal(move['wac_rub'])*Decimal(-10))
        source,events=stage._sources(cap)
        native=wf._ff_ledger_line_cost_snapshot(dict(quantity_delta=-10,raw={'cost_snapshot':dict(
            unit_cost_rub=move['wac_rub'],capital_delta_rub=move['capital_delta_rub'],quality='exact_historical_fbo_dispatch_wac')}))
        self.assertEqual(Decimal(source[0]['pre_cost']),native['unit_cost_rub'])
        self.assertEqual(Decimal(events[0]['capital_rub']),native['unit_cost_rub']*8)

    def test_unknown_barcode_headline_quantity_is_preserved_without_sku_cost(self):
        cap=fixture();raw=cap['supplies']['one']['rawsource'];value=json.loads(raw['normalized_row_json'])
        value['accepted_quantity']=11;raw['normalized_row_json']=json.dumps(value)
        audit=stage._acceptance_audit(cap)[0]
        self.assertEqual(audit['identified_accepted_quantity'],'8')
        self.assertEqual(audit['unattributed_accepted_quantity'],'3')
        self.assertEqual(audit['unattributed_cost'],'unavailable')
        self.assertIsNone(audit['goods'][0]['unloadingQuantity'])

    def test_per_sku_overage_is_not_added_as_unproven_acceptance_money(self):
        cap=fixture();raw=cap['supplies']['one']['rawsource'];goods=json.loads(raw['raw_goods_json']);goods[0]['acceptedQuantity']=12;raw['raw_goods_json']=json.dumps(goods)
        source,events=stage._sources(cap)
        self.assertEqual(source[0]['unattributed_overage_quantity'],'2')
        self.assertEqual(events[0]['quantity'],'10');self.assertEqual(events[0]['capital_rub'],'1000')

    def test_duplicate_acceptance_is_a_readback_boundary(self):
        cap=fixture();cap['acceptance_events']=[dict(source_id='one:1',nm_id=1,quantity='8',capital_rub='800')]
        with self.assertRaisesRegex(ValueError,'selected_acceptance_already_posted'):stage._sources(cap)

    def test_replay_keeps_real_quantity_and_marks_missing_day_provisional(self):
        cap=fixture();_,events=stage._sources(cap);rows=stage._scratch_replay(cap,events)
        day20=next(r for r in rows if r['as_of_date']=='2026-10-20')
        day21=next(r for r in rows if r['as_of_date']=='2026-10-21')
        self.assertEqual(day20['quantity'],'100');self.assertTrue(day20['provenance']['snapshot_carried_forward'])
        self.assertEqual(day21['quantity'],'90');self.assertFalse(day21['provenance']['snapshot_carried_forward'])
        self.assertEqual(day21['quality'],'periodic_snapshot_wac_provisional')
        self.assertIn('2026-10-20',day21['provenance']['historical_fbo_quality']['missing_quantity_dates'])
        self.assertEqual(Decimal(day21['wac_rub']),Decimal(5800)/Decimal(108))

    def test_persisted_historical_event_replays_same_money_and_quality(self):
        cap=fixture();_,events=stage._sources(cap);first=stage._scratch_replay(cap,events)
        persisted=[]
        for event in events:
            row={k:v for k,v in event.items() if k!='provenance'}
            row.update(provenance_json=json.dumps(event['provenance']),created_at='2026-11-02T12:00:00Z');persisted.append(row)
        cap['wb_replay']['acceptance_events']=persisted
        # _scratch's supplemental display annotation is irrelevant to the
        # native owner: its own historical_fbo_quality survives source replay.
        second=stage._scratch_replay(cap,events)
        self.assertEqual(first,second)

    def test_foreign_retained_sources_and_quantity_are_preserved(self):
        cap=fixture();sources,events=stage._sources(cap);daily=stage._scratch_replay(cap,events)
        foreign=dict(source_id='foreign:1',flow_quantity='3',flow_capital_rub='999.000000000000000000000000001')
        saved=dict(version=dict(source_watermarks_json=json.dumps({'captured_at':'2026-10-21T10:00:00Z'})),balances=[
            dict(warehouse_key='wb',nm_id=1,quantity='90',capital_rub='4500',wac_rub='50',cost_covered_quantity='90',provenance={'source_records':[{'source':'official'}]}),
            dict(warehouse_key='ff_to_wb',nm_id=1,quantity='3',capital_rub=foreign['flow_capital_rub'],wac_rub='333',cost_covered_quantity='3',provenance={'source_records':[foreign]})])
        original=deepcopy(saved)
        with localcontext() as context:
            context.prec=160
            result,_=stage._stage_balances(saved,day='2026-10-21',sources=sources,daily=daily,documents=[])
        transit=next(r for r in result if r['warehouse_key']=='ff_to_wb')
        self.assertEqual(transit,original['balances'][1]);self.assertEqual(saved,original)
        discrepancy=next(r for r in result if r['warehouse_key']=='wb_acceptance_discrepancy')
        self.assertEqual(discrepancy['quantity'],'2');self.assertEqual(discrepancy['capital_rub'],'200')
        self.assertTrue(discrepancy['provenance']['source_records'][0]['paid_acceptance_excluded'])

    def test_cost_layer_requires_frozen_basis_and_exact_raw_fee_source(self):
        cap=fixture();cap['book_version']=fingerprint('book')
        move=cap['native_plan']['effects'][0]['pool_movements'][0]
        ref=dict(contract='historical_fbo_frozen_native_cost_layers_v1',plan_digest=cap['native_plan']['plan_digest'],source_book=cap['book_version'],
            nm_id=1,unit_cost_rub=move['wac_rub'],quantity_delta=move['quantity_delta'],capital_delta_rub=move['capital_delta_rub'])
        cap['fee_sources']={'fee':dict(upload={'validation_status':'ok','deleted_at':None},lines=[dict(match_status='ok',is_storage_line=0,matched_wb_supply_id='one',amount_with_vat='30')])}
        cap['cost_layers']=[dict(wb_supply_id='one',nm_id=1,component_status_json=json.dumps({'historical_fbo_native_source':ref}),
            qty_denominator=10,ff_upload_id='fee',ff_services_amount_total=30,ff_services_per_unit_rub=3,ff_storage_amount_total=0,ff_storage_per_unit_rub=0)]
        stage._validate_cost_layers(cap)
        cap['cost_layers'][0]['ff_services_amount_total']=0
        with self.assertRaisesRegex(ValueError,'cost_layer_fee_amount_stale'):stage._validate_cost_layers(cap)
        cap['cost_layers'][0]['ff_services_amount_total']=30
        ref['unit_cost_rub']='999';cap['cost_layers'][0]['component_status_json']=json.dumps({'historical_fbo_native_source':ref})
        with self.assertRaisesRegex(ValueError,'cost_layer_historical_basis_missing'):stage._validate_cost_layers(cap)

    def test_unknown_basis_and_snapshot_mismatch_fail_closed(self):
        cap=fixture();sources,events=stage._sources(cap);daily=stage._scratch_replay(cap,events)
        saved=dict(version=dict(source_watermarks_json='{"captured_at":"2026-10-21T10:00:00Z"}'),balances=[dict(warehouse_key='wb',nm_id=1,quantity='91',capital_rub='4500',wac_rub='50',cost_covered_quantity='91',provenance={})])
        with self.assertRaisesRegex(ValueError,'official_wb_quantity_changed'):
            stage._stage_balances(saved,day='2026-10-21',sources=sources,daily=daily,documents=[])
        cap['wb_replay']['opening_cost_map']=[]
        with self.assertRaisesRegex(ValueError,'opening_cost_map_missing'):stage._scratch_replay(cap,events)

if __name__=='__main__':unittest.main()
