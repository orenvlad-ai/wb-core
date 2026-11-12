"""Native wire, narrow source scope, immutable-byte and one-submit regressions."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.web_vitrina_history_store_smoke import setup_units, vector_for, DAYS, expect_error
from apps.web_vitrina_history_group_repair_smoke import object_rows
from apps.web_vitrina_source_history_repair import WebSourceHistoryRepairAdapter, request_digest, CONTRACT_PATH
from apps.production_apply_contract import AmbiguousSubmit
from packages.application.web_vitrina_history_compiler import digest
from packages.application.web_vitrina_history_store import HistoryStore, HistoryUnavailable, _atomic
from packages.application.web_vitrina_view_model import _FORMATTER_LIBRARY
from packages.application.web_vitrina_source_history_repair import (
    SKU, TOTAL, plan_repair, publish_repair, verify_candidate, project_day,
    repaired_edition,
    verify_retained,
)


def fixture(root):
    catalog, units = setup_units()
    for scope in ('SKU:100','SKU:101','TOTAL','GROUP:clean'):
        metrics = {k for keys in (SKU if scope.startswith('SKU:') else TOTAL).values() for k in keys}
        for key in sorted(metrics):
            rid = scope+'|'+key
            structural = deepcopy(catalog['rows']['TOTAL|stock'])
            structural.update(row_id=rid, row_kind='sku' if scope.startswith('SKU:') else 'group' if scope.startswith('GROUP:') else 'total',
                group_id='group:clean' if scope != 'TOTAL' else 'all')
            structural['values'].update(metric_key=[key], nm_id=[int(scope[4:]) if scope.startswith('SKU:') else None])
            catalog['rows'][rid] = structural; catalog['order'].append(rid)
            for unit in units.values():
                unit['cells'][rid] = [999,'999','number','number_default','renderer:number:number_default','','','','','','','',None,'','','']
                unit['members'].append(rid)
    catalog['presentation'] = {'formatters': [asdict(_FORMATTER_LIBRARY[k]) for k in ('number_default','percent_default','empty_default')],
        'renderers': [{'renderer_id': f'renderer:{kind}:{key}', 'formatter_id': key}
                      for kind,key in [('number','number_default'),('percent','percent_default'),('empty','empty_default')]]}
    source = HistoryStore(root/'source', max_store_bytes=64*1024**2)
    vector = vector_for(units)
    result = source.update(vector=vector, catalog=catalog, compile_day=lambda d:deepcopy(units[d]), revalidate=lambda:vector)
    # Deliberately non-default compression must survive byte-identically.
    for ref in source.edition()['days'].values():
        with sqlite3.connect(source.root/'objects'/(ref+'.sqlite3')) as conn:
            blob = conn.execute("SELECT payload FROM cells WHERE row_id='TOTAL|stock'").fetchone()[0]
            conn.execute("UPDATE cells SET payload=? WHERE row_id='TOTAL|stock'", (zlib.compress(zlib.decompress(blob),9),))
    return source, result['edition_id'], catalog, units, vector


def observations(day):
    return [dict(source_key='seller_funnel_snapshot', snapshot_date=day, source_fetched_at='2026-11-12T08:00:00+00:00', completeness='complete',
        items=[dict(nm_id=100,view_count=3,open_card_count=1,ctr=33), dict(nm_id=101,view_count=0,open_card_count=0,ctr=0)]),
        dict(source_key='web_source_snapshot',snapshot_date=day,source_fetched_at='2026-11-12T08:00:00+00:00',completeness='complete',
        items=[dict(nm_id=100,views_current=100,ctr_current=7,orders_current=8,position_avg=31)])]


def plan(source, base, day=DAYS[0], op='source-repair-test'):
    return plan_repair(source, expected_base=base, observations=observations(day), rosters={day:[100,101]}, operation_id=op,
        authority={'publication_request_sha256':'sha256:'+digest(day)})


def main():
    with tempfile.TemporaryDirectory(prefix='source-history-repair-') as raw:
        root=Path(raw);source,base,catalog,units,vector=fixture(root)
        pointer=(source.root/'CURRENT.json').read_bytes();before=deepcopy(source.edition())
        old={d:object_rows(source,r) for d,r in before['days'].items()}
        files={str(p):p.read_bytes() for p in source.root.rglob('*') if p.is_file()}
        preview=plan(source,base); patch0=preview['patches'][DAYS[0]]
        assert files=={str(p):p.read_bytes() for p in source.root.rglob('*') if p.is_file()}
        assert patch0['SKU:100|ctr'][:5]==[.333333,'0.333333','percent','percent_default','renderer:percent:percent_default']
        assert patch0['TOTAL|ctr'][0]==patch0['GROUP:clean|ctr'][0]==.333333
        assert patch0['SKU:101|ctr'][0]=='' and patch0['SKU:101|ctr'][8]=='undefined'
        assert patch0['SKU:100|ctr_current'][0]==.07
        assert patch0['SKU:101|orders_current'][0]=='' and patch0['SKU:101|orders_current'][12]==1
        assert patch0['TOTAL|total_orders_current'][0]==8 and patch0['TOTAL|total_orders_current'][11:13]==['partial',1]
        assert patch0['TOTAL|avg_position_avg'][0]==31
        # Production archived catalogs retain only the rating formatter. The
        # ordinary history reader supplies built-ins for declared renderers.
        archived_catalog=deepcopy(catalog)
        archived_catalog['presentation']['formatters']=[asdict(_FORMATTER_LIBRARY['rating'])]
        original_catalog=deepcopy(archived_catalog)
        assert project_day(DAYS[0],archived_catalog,units[DAYS[0]],observations(DAYS[0]),[100,101],'archived-assets')==patch0
        assert archived_catalog==original_catalog
        absent_renderer=deepcopy(archived_catalog)
        absent_renderer['presentation']['renderers']=[r for r in absent_renderer['presentation']['renderers'] if r['formatter_id']!='percent_default']
        expect_error(lambda:project_day(DAYS[0],absent_renderer,units[DAYS[0]],observations(DAYS[0]),[100,101],'missing-renderer'),HistoryUnavailable,'presentation_asset_missing')
        wrong_binding=deepcopy(archived_catalog)
        next(r for r in wrong_binding['presentation']['renderers'] if r['formatter_id']=='percent_default')['formatter_id']='number_default'
        expect_error(lambda:project_day(DAYS[0],wrong_binding,units[DAYS[0]],observations(DAYS[0]),[100,101],'wrong-renderer'),HistoryUnavailable,'presentation_asset_missing')
        zeros=observations(DAYS[0])
        zeros[1]['items'][0].update(views_current=0,ctr_current=0)
        zero_patch=project_day(DAYS[0],catalog,units[DAYS[0]],zeros,[100,101],'source-zero-ctr')
        for rid in ('TOTAL|avg_ctr_current','GROUP:clean|avg_ctr_current'):
            assert zero_patch[rid][0]==0 and zero_patch[rid][8]!='undefined'
        zeros[1]['items'][0]['ctr_current']=1
        unknown_patch=project_day(DAYS[0],catalog,units[DAYS[0]],zeros,[100,101],'source-unknown-ctr')
        for rid in ('TOTAL|avg_ctr_current','GROUP:clean|avg_ctr_current'):
            assert unknown_patch[rid][0]=='' and unknown_patch[rid][8]=='undefined'
        assert not any('operation_id' in c[7] or 'source_digest' in c[7] for c in patch0.values())
        candidate=HistoryStore(root/'candidate',max_store_bytes=64*1024**2)
        new=publish_repair(source,candidate,preview,revalidate=lambda:True)
        current=source.edition();verify_candidate(source,source,preview,current)
        group_base=deepcopy(before)
        group_base['group_repair']={'base_objects':{d:digest('older-'+d) for d in DAYS}}
        retained=repaired_edition(group_base,current['days'],preview['claim'])
        assert retained['group_repair']==group_base['group_repair']
        assert retained['group_repair_retained_days']=={d:before['days'][d] for d in DAYS[1:]}
        # Subsequent narrow repairs retain only still-identical old refs;
        # replacing the last one drops the obsolete whole-object claim.
        changed_refs={d:digest('replacement-'+d) for d in DAYS}
        exhausted=repaired_edition(retained,changed_refs,preview['claim'])
        assert 'group_repair' not in exhausted and 'group_repair_retained_days' not in exhausted
        assert source._current()=={'current':new,'previous':base}
        assert {k:v for k,v in current.items() if k not in ('days','source_repair')}=={k:v for k,v in before.items() if k not in ('days','source_repair')}
        assert current['days'][DAYS[1]]==before['days'][DAYS[1]]
        for d,r in current['days'].items():
            blobs,metadata=object_rows(source,r)
            assert metadata==old[d][1]
            assert blobs['TOTAL|stock']==old[d][0]['TOTAL|stock']
        expect_error(lambda:publish_repair(source,candidate,preview,revalidate=lambda:True),HistoryUnavailable,'superseded')
        second=plan(source,new,DAYS[1],op='source-repair-next')
        second_id=publish_repair(source,HistoryStore(root/'candidate2'),second,revalidate=lambda:True)
        assert set(source.edition()['source_repair'])==set(DAYS[:2])
        # Exact unchanged source cells reuse the object; a new receipt need not
        # rewrite its immutable cells or invent a new native proof.
        noop=plan(source,second_id,DAYS[1],op='source-repair-same')
        assert noop['changed_cells']==0
        same=publish_repair(source,HistoryStore(root/'candidate3'),noop,revalidate=lambda:True)
        assert source.edition()['days'][DAYS[1]]==source.edition(second_id)['days'][DAYS[1]]
        # A still proves after disjoint B and after ordinary retention expires.
        for edition_path in (source.root/'editions').glob('*.json'):
            os.utime(edition_path,(1,1))
        with source._writer():source._collect(pin_ttl_seconds=0)
        verify_retained(source,preview,source.edition())
        assert source.edition(base)['days']==before['days']
        aborted=plan(source,same,DAYS[2],op='source-repair-abort');calls=[]
        def late_abort():
            calls.append(1);return len(calls)==1
        expect_error(lambda:publish_repair(source,HistoryStore(root/'late'),aborted,revalidate=late_abort),HistoryUnavailable,'superseded')
        assert source._current()['current']==same
        expect_error(lambda:publish_repair(source,HistoryStore(root/'expired'),aborted,revalidate=lambda:True,deadline=time.monotonic()-1),HistoryUnavailable,'deadline')
        broken=deepcopy(aborted);broken['patches'][DAYS[2]]['SKU:100|ctr'][0]=123
        expect_error(lambda:publish_repair(source,HistoryStore(root/'corrupt'),broken,revalidate=lambda:True),HistoryUnavailable,'patch_proof')
        assert source._current()['current']==same
        expect_error(lambda:plan_repair(source,expected_base=same,observations=observations(DAYS[0]),rosters={DAYS[0]:[100]},operation_id='bad-roster',authority={}),HistoryUnavailable,'roster_drift')
        # A rolling native recompilation invalidates claims only for replaced
        # objects. The untouched archive retains its prior source provenance.
        rolled=source.update(vector=vector,catalog=catalog,compile_day=lambda d:deepcopy(units[d]),revalidate=lambda:vector,
            business_date='2026-05-20',backfill_dates=[DAYS[1]],expected_base=same)
        # Native proof was already equal, so no recompilation is invented.
        assert source.edition()['days'][DAYS[1]]==source.edition(same)['days'][DAYS[1]]
        changed_vector=deepcopy(vector);changed_vector['dates'][DAYS[1]]=digest('new-native-day')
        source.update(vector=changed_vector,catalog=catalog,compile_day=lambda d:deepcopy(units[d]),revalidate=lambda:changed_vector,
            business_date='2026-05-20',backfill_dates=[DAYS[1]],expected_base=source._current()['current'])
        assert set(source.edition()['source_repair'])=={DAYS[0]}
        pins=source.root/'SOURCE_REPAIR_PINS.json';saved_pins=pins.read_bytes()
        pins.write_text('[]')
        expect_error(lambda:source._collect(pin_ttl_seconds=0),HistoryUnavailable,'pins_invalid')
        pins.write_bytes(saved_pins)
    check_adapter_one_submit()
    check_native_authority()
    check_real_targets()
    print('web_vitrina_source_history_repair_smoke: OK')


def check_adapter_one_submit():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw);source,base,_,_,_=fixture(root)
        request={'expected_base_edition':base,'observations':observations(DAYS[0]),'audited_nm_ids_by_date':{DAYS[0]:[100,101]}}
        class Adapter(WebSourceHistoryRepairAdapter):
            def targets(self,request,operation):
                return root,source,HistoryStore(root/'candidate'),root/'evidence'/(operation+'.json'),root/'contract',{'formula_epoch':'fixture'}
            def authority(self,request,runtime,*,retained=False):return {'native':'verified'}
            @contextmanager
            def admission(self,request,runtime):yield
            def check_window(self,request,runtime):pass
        adapter=Adapter();op='test-source-history'
        with patch('apps.web_vitrina_source_history_repair.runtime_storage_admission'):
            preview=adapter.preview(request,op)
            assert adapter.readback(request,op)['state']=='not_submitted'
            adapter.apply(request,op,preview)
            assert adapter.readback(request,op)['state']=='applied'
            assert adapter.preview(request,op)['candidate_sha256']==preview['candidate_sha256']
            pointer=(source.root/'CURRENT.json').read_bytes()
            expect_error(lambda:adapter.apply(request,op,preview),AmbiguousSubmit)
            assert (source.root/'CURRENT.json').read_bytes()==pointer
            assert adapter.readback(request,op)['state']=='applied'
            next_request={'expected_base_edition':source._current()['current'],'observations':observations(DAYS[1]),
                'audited_nm_ids_by_date':{DAYS[1]:[100,101]}}
            next_op='test-next-source-history'
            adapter.apply(next_request,next_op,adapter.preview(next_request,next_op))
            for path in (source.root/'editions').glob('*.json'):os.utime(path,(1,1))
            with source._writer():source._collect(pin_ttl_seconds=0)
            assert adapter.readback(request,op)['state']=='applied'
            assert adapter.readback(next_request,next_op)['state']=='applied'


def check_native_authority():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw);backup=root/'native.json'
        published={'phase':'publication','observations':observations(DAYS[0]),'source_sha256':'sha256:'+digest('source'),
            'dates':[DAYS[0]],'nm_ids':[100,101],'supplier_identity_sha256':digest('supplier'),
            'runtime_dir':str(root),'runtime_sha':'a'*40,'storage_manifest_sha256':'sha256:'+digest('manifest'),
            'history_roster':{'edition_id':digest('base'),'dates':{DAYS[0]:{'nm_ids':[100,101]}}}}
        backup.write_text(json.dumps({'request':published,'candidate':{'after_images':{}}}))
        request={**published,'phase':'history','publication_operation_id':'native-source-publication',
            'publication_request_sha256':request_digest(published),'expected_base_edition':digest('base'),
            'audited_nm_ids_by_date':{DAYS[0]:[100,101]},'runtime_contract_sha256':'sha256:'+digest('contract')}
        with patch('apps.web_vitrina_source_history_repair.validate_observations'), \
             patch('apps.web_vitrina_source_history_repair.WebSourcePublicationAdapter') as native:
            native.return_value._backup.return_value=backup
            native.return_value.readback.return_value={'state':'applied'}
            adapter=WebSourceHistoryRepairAdapter()
            assert adapter.authority(request,root)['publication_operation_id']=='native-source-publication'
            native.return_value.readback.return_value={'state':'ambiguous'}
            expect_error(lambda:adapter.authority(request,root),ValueError,'publication-unproven')
            native.return_value.readback.return_value={'state':'applied'}
            changed=deepcopy(request);changed['audited_nm_ids_by_date'][DAYS[0]]=[100]
            expect_error(lambda:adapter.authority(changed,root),ValueError,'roster-unbound')
            changed=deepcopy(request);changed['observations'][0]['items'][0]['view_count']=999
            expect_error(lambda:adapter.authority(changed,root),ValueError,'request-unbound')
            adapter.authority(request,root,retained=True)
            native.return_value.readback.assert_called_with(published,'native-source-publication',target_only=True)


def check_real_targets():
    with tempfile.TemporaryDirectory() as raw:
        root=Path(raw).resolve();runtime=root/'state';runtime.mkdir()
        contract=root/'app'/CONTRACT_PATH;contract.parent.mkdir(parents=True)
        content=json.dumps({'candidate_root':str(root/'extra'/'web-vitrina-history')}).encode()
        contract.write_bytes(content)
        request={'runtime_contract':str(contract),'runtime_contract_sha256':'sha256:'+hashlib.sha256(content).hexdigest()}
        with patch('apps.web_vitrina_source_history_repair.WebSourcePublicationAdapter') as native:
            native.return_value.target.return_value=(runtime,runtime/'native.sqlite3')
            actual=WebSourceHistoryRepairAdapter().targets(request,'valid-source-history')
            assert actual[4]==contract and actual[1].root==root/'extra/web-vitrina-history/history'
            wrong={**request,'runtime_contract':str(runtime/CONTRACT_PATH)}
            expect_error(lambda:WebSourceHistoryRepairAdapter().targets(wrong,'valid-source-history'),ValueError,'contract-path-invalid')


if __name__=='__main__':main()
