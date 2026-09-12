"""Regression: a failed collector cannot close a stale dated API payload."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import traceback
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.adapters.seller_portal_web_source_collector import replay_headers, _response_json, _post_report
from packages.adapters.web_source_current_sync import ClosedDaySourceState, _serving_item_digest, qualified_serving_state, serving_payload_matches
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import (
    SheetVitrinaV1LivePlanBlock, _web_source_observation_is_closed, _next_closure_retry)
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotSuccess, SellerFunnelSnapshotItem
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotSuccess, WebSourceSnapshotItem

DAY = '2026-09-11'
NOW = datetime(2026,9,13,0,tzinfo=timezone.utc)


class Sync:
    def __init__(self, error=None, fetched_at='2026-09-12T22:00:00Z', payload=None):
        self.error, self.fetched_at, self.calls, self.payload = error, fetched_at, 0, payload
    def ensure_closed_day_snapshot(self, *, source_key, snapshot_date):
        self.calls += 1
        if self.error: raise RuntimeError(self.error)
        return ClosedDaySourceState(source_key, snapshot_date, 1, self.fetched_at, {str(i.nm_id):_serving_item_digest(i) for i in self.payload.items})


def capture(plan, source, payload):
    return plan._capture_temporal_source_with_acceptance(
        source_key=source, temporal_slot='yesterday_closed', temporal_policy='dual_day_capable',
        column_date=DAY, requested_nm_ids=[1,2], loader=lambda:payload, execution_mode='auto_daily',
        accepted_role='accepted_closed_day_snapshot', allow_persisted_retry=True, current_web_source_sync_note=None)


def serving_lineage_checks():
    from copy import deepcopy
    from dataclasses import asdict
    from datetime import timedelta
    from unittest.mock import patch
    from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync, WebSourceCurrentSyncConfig
    stamp=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    raw=[{'nm_id':1,'date_from':DAY,'date_to':DAY,'views_current':100,'ctr_current':10,'orders_current':1,'position_avg':1,'raw_json':{'old':True},'fetched_at':stamp}]
    handoff=[{k:v for k,v in raw[0].items() if k!='fetched_at'} | {'handoff_synced_at':stamp}]
    def state(*_):
        return ClosedDaySourceState('web_source_snapshot',DAY,**qualified_serving_state('web_source_snapshot',DAY,raw,handoff))
    def api(_):return {'date_from':DAY,'date_to':DAY,'items':[{k:v for k,v in handoff[0].items() if k in {'nm_id','views_current','ctr_current','orders_current','position_avg'}}]}
    class Sync(ShellBackedWebSourceCurrentSync):
        def __init__(self):
            super().__init__(config=WebSourceCurrentSyncConfig('force',Path('/absent'),Path('/absent'),'http://localhost',1,'',''),closed_day_source_state_loader=state)
            self.commands=[]
        def _ensure_seller_portal_session_ready(self):pass
        def _has_sales_funnel_snapshot(self,day):return True
        def _run(self,command,**kw):
            self.commands.append(command)
            if 'run_web_source_handoff.py' in command:raise RuntimeError('fake handoff failure')
            raw[0].update(views_current=900,raw_json={'new':True},fetched_at=datetime.now(timezone.utc).isoformat())
    sync=Sync()
    with patch('packages.adapters.web_source_current_sync._fetch_json',side_effect=api):
        for attempt in range(2):
            try:sync.ensure_snapshot(DAY)
            except RuntimeError as exc:assert str(exc)=='fake handoff failure'
            else:raise AssertionError('failed handoff accepted from raw-only clock')
            assert len(sync.commands)==2*(attempt+1) and not sync.observed_states
        handoff[:]=[{k:v for k,v in raw[0].items() if k!='fetched_at'} | {'handoff_synced_at':datetime.now(timezone.utc).isoformat()}]
        sync.ensure_snapshot(DAY)
        assert len(sync.commands)==4 and state().row_count==1
    qualified=state()
    stale=WebSourceSnapshotSuccess('success',DAY,DAY,1,[WebSourceSnapshotItem(1,100,10,1,1)])
    assert not serving_payload_matches(qualified,stale)
    assert serving_payload_matches(qualified,api(None))
    # The handoff generation can change between probe and live-plan payload read.
    with TemporaryDirectory() as tmp:
        plan=SheetVitrinaV1LivePlanBlock.__new__(SheetVitrinaV1LivePlanBlock)
        plan.runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp));plan.now_factory=lambda:NOW
        plan.current_web_source_sync=SimpleNamespace(observed_states={('web_source_snapshot',DAY):qualified})
        status,payload=plan._capture_temporal_source_with_acceptance(source_key='web_source_snapshot',temporal_slot='today_current',
            temporal_policy='dual_day_capable',column_date=DAY,requested_nm_ids=[1],loader=lambda:stale,execution_mode='auto_daily',
            accepted_role='accepted_current_snapshot',allow_persisted_retry=False,current_web_source_sync_note=None)
        assert status.kind in {'error','incomplete'} and (payload is None or payload.source_fetched_at is None), (status,payload)
        assert plan.runtime.load_temporal_source_slot_snapshot(source_key='web_source_snapshot',snapshot_date=DAY,snapshot_role='accepted_current_snapshot') == (None,None)
    original=deepcopy(handoff)
    handoff[0]['raw_json']={'unrelated':'report'}
    assert state().row_count==0
    handoff[:]=original
    handoff[0]['handoff_synced_at']=stamp
    assert state().row_count==0


def main():
    serving_lineage_checks()
    from apps.seller_portal_web_source_collect import write_source
    try:write_source({'snapshot_date':DAY,'source_key':'seller_funnel_snapshot','completeness':'complete',
        'request_period':{'start':DAY,'end':DAY},'items':[{'nm_id':1,'view_count':0,'open_card_count':0}]},env={})
    except ValueError as exc:assert str(exc)=='source_zero_signal_not_accepted'
    else:raise AssertionError('unusable all-zero observation reached the database')
    for header in ('seller-lk','wb-seller-lk'):
        h=replay_headers({'Content-Type':'application/json','AuthorizeV3':'private',header:'supplier'})
        assert h[header]=='supplier' and len(h)==3
    for value in ({'content-type':'a','authorizev3':'b'}, {'content-type':'a','authorizev3':'b','seller-lk':'x','wb-seller-lk':'y'}):
        try: replay_headers(value)
        except RuntimeError: pass
        else: raise AssertionError('missing/conflicting identity accepted')
    for err, accepted in (({'errors':None},True),({'errors':['failed']},False)):
        response=SimpleNamespace(status=200,json=lambda:{'error':False,'additionalErrors':err,'data':{}})
        try: _response_json(response)
        except RuntimeError: assert not accepted
        else: assert accepted
    class BrokenTransport:
        def post(self, *args, **kwargs):
            raise RuntimeError('Call log authorizev3=FAKE_AUTH_MARKER seller-lk=FAKE_SUPPLIER_MARKER')
    try:
        _post_report(BrokenTransport(), 'http://localhost:1')
    except RuntimeError:
        rendered=traceback.format_exc()
        assert 'FAKE_AUTH_MARKER' not in rendered and 'FAKE_SUPPLIER_MARKER' not in rendered
    else:raise AssertionError('transport failure expected')
    from packages.adapters.web_source_current_sync import ShellBackedWebSourceCurrentSync, WebSourceCurrentSyncConfig
    from datetime import timedelta
    current=[ClosedDaySourceState('web_source_snapshot',DAY,1,datetime.now(timezone.utc).isoformat())]
    sync=ShellBackedWebSourceCurrentSync(config=WebSourceCurrentSyncConfig('off',Path('/absent'),Path('/absent'),'http://localhost',1,'',''),
        closed_day_source_state_loader=lambda *_:current[0])
    assert sync._current_source_is_fresh('web_source_snapshot',DAY)
    current[0]=ClosedDaySourceState('web_source_snapshot',DAY,1,(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat())
    assert not sync._current_source_is_fresh('web_source_snapshot',DAY) and not sync.observed_states
    sync.observed_states[('web_source_snapshot',DAY)]=current[0]
    sync.ensure_snapshot(DAY)
    assert not sync.observed_states
    cases={
        'seller_funnel_snapshot':SellerFunnelSnapshotSuccess('success',DAY,1,[SellerFunnelSnapshotItem(1,'item','code',100,10,10)]),
        'web_source_snapshot':WebSourceSnapshotSuccess('success',DAY,DAY,1,[WebSourceSnapshotItem(1,100,10,1,1)]),
    }
    for source,payload in cases.items():
        with TemporaryDirectory() as tmp:
            runtime=RegistryUploadDbBackedRuntime(runtime_dir=Path(tmp))
            plan=SheetVitrinaV1LivePlanBlock.__new__(SheetVitrinaV1LivePlanBlock)
            plan.runtime=runtime;plan.now_factory=lambda:NOW;plan.current_web_source_sync=SimpleNamespace(observed_states={})
            plan.closed_day_web_source_sync=Sync('template_required_headers_missing')
            # Both legacy acceptance and a new cache capture are stale evidence.
            for role in ('accepted_current_snapshot','accepted_closed_day_snapshot'):
                runtime.save_temporal_source_slot_snapshot(source_key=source,snapshot_date=DAY,snapshot_role=role,captured_at='2026-09-12T21:00:00Z',payload=payload)
            runtime.save_temporal_source_snapshot(source_key=source,snapshot_date=DAY,captured_at='2026-09-12T21:00:00Z',payload=payload)
            runtime.save_temporal_source_closure_state(source_key=source,target_date=DAY,slot_kind='yesterday_closed',state='success',attempt_count=12,next_retry_at=None,last_reason='legacy',last_attempt_at='2026-09-12T21:00:00Z',last_success_at='2026-09-12T21:00:00Z',accepted_at='2026-09-12T21:00:00Z')
            status,result=capture(plan,source,payload)
            state=runtime.load_temporal_source_closure_state(source_key=source,target_date=DAY,slot_kind='yesterday_closed')
            assert status.kind=='incomplete' and result.items[0].nm_id==payload.items[0].nm_id, (status, result)
            assert state.state=='closure_retrying' and state.attempt_count==1 and state.accepted_at is None
            old,_=runtime.load_temporal_source_slot_snapshot(source_key=source,snapshot_date=DAY,snapshot_role='accepted_closed_day_snapshot')
            assert old.source_fetched_at is None
            capture(plan,source,payload)
            assert plan.closed_day_web_source_sync.calls==1, 'backoff must avoid repeated collection'
            # Move to the persisted exact-date retry; today has already rolled.
            plan.now_factory=lambda:datetime(2026,9,13,1,tzinfo=timezone.utc)
            plan.closed_day_web_source_sync=Sync(payload=payload)
            status,result=capture(plan,source,payload)
            assert status.kind=='success' and _web_source_observation_is_closed(source,result,DAY)
            state=runtime.load_temporal_source_closure_state(source_key=source,target_date=DAY,slot_kind='yesterday_closed')
            assert state.state=='success' and state.next_retry_at is None
            capture(plan,source,payload)
            assert plan.closed_day_web_source_sync.calls==1, 'qualified closed source must be preserved'
    assert _next_closure_retry(NOW,999,'error') == (None,'closure_exhausted')
    print('seller portal repair smoke: header alias, failed collection, stale accepted/cache, rollover, bounded retry and dated success passed')


if __name__=='__main__': main()
