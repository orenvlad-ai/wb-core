"""Missing-only finance diagnostics, exact source CAS, readback and rollback."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from copy import deepcopy
import json
import sqlite3
from tempfile import TemporaryDirectory
from apps.finance_daily_absence_smoke import fixture
from apps.finance_absence_publication import FinanceAbsencePublicationAdapter,capture_source,project
from apps.production_apply_launcher import execute
from packages.application.web_vitrina_management_history import digest

def rejects(fn,part):
    try:fn()
    except ValueError as exc:assert part in str(exc),str(exc)
    else:raise AssertionError('unsafe operation admitted')

def check_stale_proof():
    from apps.finance_absence_publication import capture_stale_source,STALE_MODE,STALE_DAY,STALE_RECOVERY,STALE_RECEIPT
    with TemporaryDirectory() as tmp:
        root=Path(tmp);(root/'evidence').mkdir();c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row
        c.executescript('''CREATE TABLE sheet_vitrina_v1_recovery_operations(operation_id,lifecycle_state,operation_kind,after_digest,plan_fingerprint,last_error,checkpoint_digest);
        CREATE TABLE sheet_vitrina_v1_recovery_artifacts(operation_id,artifact_kind,state,digest);
        CREATE TABLE sheet_vitrina_v1_recovery_undo_rows(operation_id,sequence_no,table_name,key_json,before_json,after_json,action,status);
        CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,snapshot_id,plan_json);''')
        images=[];newrows=[];nms=list(range(1000,1062));rows=[]
        for i,nm in enumerate(nms,1):
            key=dict(seller_id='seller',week_start='2026-10-12',week_end=STALE_DAY,nm_id=str(nm))
            old={**key,'formula_version':'wb_finance_weekly_sku_aggregate_v5','week_content_hash':'raw',
                'metrics_json':json.dumps({'profit_method_version':'wb_finance_profit_covered_revenue_v4_signed_deductions'}),
                'coverage_json':json.dumps({'daily_rows':[{'operation_date':STALE_DAY,'uncovered_sales_revenue_rub':'5.0000'}]})}
            newrows.append({**old,'metrics_json':json.dumps({'profit_method_version':'wb_finance_profit_full_revenue_v5_explicit_estimates'})})
            images.append(dict(sequence_no=i,table='wb_finance_weekly_sku_aggregates',key=key,before=old,after=None,action='restore_deleted'))
            c.execute('INSERT INTO sheet_vitrina_v1_recovery_undo_rows VALUES(?,?,?,?,?,?,?,?)',(STALE_RECOVERY,i,'wb_finance_weekly_sku_aggregates',json.dumps(key),json.dumps(old),None,'restore_deleted','verified'))
            rows.append(['x','SKU:'+str(nm)+'|sales_without_cost_rub',5.0])
        sid='2026-10-18__2026-10-22__sheet_vitrina_v1_temporal_live_v1__current'
        rows += [['x','TOTAL|total_sales_without_cost_rub',''],['x','SKU:1000|our_wb_unit_cost_rub','']]
        plan=dict(as_of_date=STALE_DAY,snapshot_id=sid,date_columns=[STALE_DAY],metadata={},sheets=[dict(sheet_name='DATA_VITRINA',header=['label','key',STALE_DAY],rows=rows)])
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)',('b',STALE_DAY,sid,json.dumps(plan)))
        c.execute('INSERT INTO sheet_vitrina_v1_recovery_operations VALUES(?,?,?,?,?,?,?)',(STALE_RECOVERY,'retained','canonical_cost_bounded_publication','fp','fp','',digest(images)))
        c.execute('INSERT INTO sheet_vitrina_v1_recovery_artifacts VALUES(?,?,?,?)',(STALE_RECOVERY,'undo','verified',digest(images)))
        receipt={'recovery':STALE_RECOVERY,'result':{'status':'applied','fingerprint':'fp','non_target_preserved':True}}
        (root/'evidence'/STALE_RECEIPT).write_text(json.dumps(receipt))
        week={'sync':dict(seller_id='seller',week_start='2026-10-12',week_end=STALE_DAY,content_hash='raw'),'wb_finance_weekly_sku_aggregates':newrows}
        source=dict(dates=[STALE_DAY],bundle_version='b',nm_ids=nms,weeks={'week':week},snapshot_id='source',observations={STALE_DAY:dict(week_proof='week',skus={str(n):'0.0000' for n in nms},kinds={str(n):'exact_daily_finance' for n in nms},total='0.0000')})
        stale=capture_stale_source(c,runtime=root,source=source)
        assert len(stale['cells'])==62
        out,changes=project(plan,source=source,operation_id='stale-test',stale=stale)
        assert len(changes)==63 and out['sheets'][0]['rows'][-1][-1]==''
        assert all(x['provenance']['certifies_exact_cost'] is False for x in changes)
        rejects(lambda:project(plan,source=source,operation_id='default'),'conflicts')
        wrong=deepcopy(plan);wrong['metadata']['manual']='new'
        rejects(lambda:project(wrong,source=source,operation_id='changed',stale=stale),'conflicts')
        bad=deepcopy(source);bad['weeks']['week']['wb_finance_weekly_sku_aggregates'][0]['week_content_hash']='different'
        rejects(lambda:capture_stale_source(c,runtime=root,source=bad),'raw-week-hash')
        bad=deepcopy(source);bad['weeks']['week']['wb_finance_weekly_sku_aggregates'][0]['metrics_json']='{"profit_method_version":"unapproved"}'
        rejects(lambda:capture_stale_source(c,runtime=root,source=bad),'policy-version')
        badreceipt=deepcopy(receipt);badreceipt['result']['fingerprint']='wrong'
        (root/'evidence'/STALE_RECEIPT).write_text(json.dumps(badreceipt))
        rejects(lambda:capture_stale_source(c,runtime=root,source=source),'receipt-linkage')
        (root/'evidence'/STALE_RECEIPT).write_text(json.dumps(receipt))
        c.execute("UPDATE sheet_vitrina_v1_recovery_operations SET checkpoint_digest='wrong'")
        rejects(lambda:capture_stale_source(c,runtime=root,source=source),'undo-digest')
        c.execute('UPDATE sheet_vitrina_v1_recovery_operations SET checkpoint_digest=?',(digest(images),))
        for kind in ('version','value','provenance','extra'):
            badplan=deepcopy(plan)
            if kind=='version':badplan['snapshot_id']='wrong'
            elif kind=='value':badplan['sheets'][0]['rows'][0][2]=99
            elif kind=='extra':badplan['sheets'][0]['rows'].append(['x','SKU:9999|sales_without_cost_rub',1])
            else:badplan['metadata']['server_cell_presentation']={'SKU:1000|sales_without_cost_rub':{STALE_DAY:{'source':'manual'}}}
            c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(json.dumps(badplan),))
            rejects(lambda:capture_stale_source(c,runtime=root,source=source),{'version':'ready-version','value':'prior-value-mismatch','provenance':'intervening-cell-provenance','extra':'unproven-populated-cell'}[kind])
        c.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(json.dumps(plan),))
        changed_images=deepcopy(images)
        changed_images[0]['before']['metrics_json']='{"profit_method_version":"unproven_old_policy"}'
        c.execute('UPDATE sheet_vitrina_v1_recovery_undo_rows SET before_json=? WHERE sequence_no=1',(json.dumps(changed_images[0]['before']),))
        c.execute('UPDATE sheet_vitrina_v1_recovery_operations SET checkpoint_digest=?',(digest(changed_images),))
        c.execute('UPDATE sheet_vitrina_v1_recovery_artifacts SET digest=?',(digest(changed_images),))
        rejects(lambda:capture_stale_source(c,runtime=root,source=source),'policy-version')
        c.execute('UPDATE sheet_vitrina_v1_recovery_undo_rows SET before_json=? WHERE sequence_no=1',(json.dumps(images[0]['before']),))
        c.execute('UPDATE sheet_vitrina_v1_recovery_operations SET checkpoint_digest=?',(digest(images),))
        c.execute('UPDATE sheet_vitrina_v1_recovery_artifacts SET digest=?',(digest(images),))
        c.execute("UPDATE sheet_vitrina_v1_recovery_undo_rows SET status='unknown' WHERE sequence_no=1")
        rejects(lambda:capture_stale_source(c,runtime=root,source=source),'undo-not-verified')
        c.execute("UPDATE sheet_vitrina_v1_recovery_undo_rows SET status='verified' WHERE sequence_no=1")
        c.execute("UPDATE sheet_vitrina_v1_recovery_undo_rows SET key_json='{}' WHERE sequence_no=1")
        rejects(lambda:capture_stale_source(c,runtime=root,source=source),'undo-digest')
        c.close()

def main():
    check_stale_proof()
    with TemporaryDirectory() as tmp:
        root=Path(tmp);db=root/'fixture.sqlite3';c=sqlite3.connect(db);c.row_factory=sqlite3.Row
        src=fixture();src.commit();src.backup(c);src.close()
        c.execute('CREATE TABLE registry_upload_current_state(slot,bundle_version)');c.execute("INSERT INTO registry_upload_current_state VALUES(1,'b')")
        c.execute('CREATE TABLE sheet_vitrina_v1_ready_snapshots(bundle_version,as_of_date,snapshot_id,plan_json)')
        days=['2026-10-19','2026-10-20'];nms=[497413000,497413001]
        keys=['SKU:497413000|sales_without_cost_rub','SKU:497413001|sales_without_cost_rub','TOTAL|total_sales_without_cost_rub',
              'SKU:497413000|our_wb_unit_cost_rub','TOTAL|total_proxy_profit_4_rub','SKU:497413000|cost_coverage_pct']
        plan={'date_columns':days,'metadata':{},'sheets':[{'sheet_name':'DATA_VITRINA','header':['label','key',*days],
            'rows':[['x',k,'',''] for k in keys]}]}
        plan['sheets'][0]['rows'][1][2]=0.0  # Include a real metadata-only write/readback.
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)',('b',days[-1],'s',json.dumps(plan)));c.commit()
        source=capture_source(c,dates=days,bundle_version='b',nm_ids=nms)
        assert source['observations'][days[0]]['total']=='500.0000' and source['observations'][days[1]]['total']=='0.0000'
        out,changes=project(plan,source=source,operation_id='p');assert len(changes)==6
        assert out['sheets'][0]['rows'][3:]==plan['sheets'][0]['rows'][3:]
        assert project(out,source=source,operation_id='again')[1]==[]
        existing=deepcopy(plan);existing['sheets'][0]['rows'][1][2]=0.0
        normalized,normalized_changes=project(existing,source=source,operation_id='presentation-only')
        change=next(x for x in normalized_changes if x['row_id']==keys[1] and x['date']==days[0])
        assert change['before']==change['after']==0.0 and isinstance(change['after'],float)
        assert normalized['sheets'][0]['rows'][1][2]==0.0
        assert project(normalized,source=source,operation_id='idempotent')[1]==[]
        manual=deepcopy(existing);manual['metadata']['server_cell_presentation']={keys[1]:{days[0]:{'source':'manual','reason':'accepted override'}}}
        rejects(lambda:project(manual,source=source,operation_id='manual'),'presentation-conflict')
        mixed=deepcopy(plan);mixed['sheets'][0]['rows'][0][2]='1'
        rejects(lambda:project(mixed,source=source,operation_id='bad'),'conflicts')
        rejects(lambda:capture_source(c,dates=['2026-10-26'],bundle_version='b',nm_ids=nms),'date-scope')
        rejects(lambda:capture_source(c,dates=days,bundle_version='b',nm_ids=[*nms,999999999]),'sku-unproven')
        adapter=FinanceAbsencePublicationAdapter();adapter.target=lambda request:(root,db)
        request=dict(source=source,dates=days,ready_dates=[days[-1]],source_sha256=digest(source),runtime_sha='test')
        preview=adapter.preview(request,'finance-repair-test')
        for invalid in ([],[days[-1],days[-1]],['2026-10-19','2026-10-20','2026-10-21','2026-10-22','2026-10-23']):
            rejects(lambda:adapter.build({**request,'ready_dates':invalid},'bad-batch',c),'batch-invalid')
        rejects(lambda:adapter.build({**request,'ready_dates':['2026-10-21']},'missing-batch',c),'scope-invalid')
        cells=out['metadata']['server_cell_presentation']
        assert cells[keys[0]][days[0]]['certifies_exact_cost'] is False
        assert 'одобренные оценки' in cells[keys[0]][days[0]]['reason']
        assert cells[keys[1]][days[0]]['diagnostic_evidence_kind']=='complete_week_no_operation'
        assert 'cost_basis' not in cells[keys[1]][days[0]]
        # Unselected ready records never enter the candidate or its prestate.
        c.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots VALUES(?,?,?,?)',('b','2026-10-21','unselected',json.dumps(plan)));c.commit()
        assert adapter.preview(request,'finance-repair-test')==preview

        c.execute("UPDATE wb_finance_weekly_reports SET content_hash='new'");c.commit()
        rejects(lambda:adapter.build(request,'drift',c),'source-drift')
        c.execute("UPDATE wb_finance_weekly_reports SET content_hash='reporthash'");c.commit()
        c.execute('UPDATE wb_finance_weekly_sync SET unchanged_sync_count=0');c.commit()
        rejects(lambda:adapter.build(request,'partial',c),'proof-unavailable')
        c.execute('UPDATE wb_finance_weekly_sync SET unchanged_sync_count=2');c.commit()
        before=c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]
        corrupt=deepcopy(plan);corrupt['metadata']['concurrent']='change';c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE snapshot_id='s'",(json.dumps(corrupt),));c.commit()
        rejects(lambda:adapter.apply(request,'finance-repair-test',preview),'candidate-cas')
        c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE snapshot_id='s'",(before,));c.commit()
        result=execute(action='apply',adapter_name='test',operation_id='finance-repair-test',request=request,adapters={'test':adapter},expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'])
        assert result['state']=='applied' and result['readback']['verified_cells']==6
        saved=c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]
        corrupt=json.loads(saved);corrupt['metadata']['server_cell_presentation'][keys[0]][days[0]]['week_proof']='wrong'
        c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE snapshot_id='s'",(json.dumps(corrupt),));c.commit()
        assert adapter.readback(request,'finance-repair-test')['state']=='ambiguous'
        c.execute("UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE snapshot_id='s'",(saved,));c.commit()
        adapter.rollback(request,'finance-repair-test')
        assert json.loads(c.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0])==plan
        c.close()
    print('finance_absence_publication_smoke: OK')
if __name__=='__main__':main()
