"""Regression: a failed collector cannot close a stale dated API payload."""
from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.adapters.seller_portal_web_source_collector import replay_headers, _response_json
from packages.adapters.web_source_current_sync import ClosedDaySourceState
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from packages.application.sheet_vitrina_v1_live_plan import (
    SheetVitrinaV1LivePlanBlock, _web_source_observation_is_closed, _next_closure_retry)
from packages.contracts.seller_funnel_snapshot_block import SellerFunnelSnapshotSuccess, SellerFunnelSnapshotItem
from packages.contracts.web_source_snapshot_block import WebSourceSnapshotSuccess, WebSourceSnapshotItem

DAY = '2026-09-11'
NOW = datetime(2026,9,13,0,tzinfo=timezone.utc)


class Sync:
    def __init__(self, error=None, fetched_at='2026-09-12T22:00:00Z'):
        self.error, self.fetched_at, self.calls = error, fetched_at, 0
    def ensure_closed_day_snapshot(self, *, source_key, snapshot_date):
        self.calls += 1
        if self.error: raise RuntimeError(self.error)
        return ClosedDaySourceState(source_key, snapshot_date, 1, self.fetched_at)


def capture(plan, source, payload):
    return plan._capture_temporal_source_with_acceptance(
        source_key=source, temporal_slot='yesterday_closed', temporal_policy='dual_day_capable',
        column_date=DAY, requested_nm_ids=[1,2], loader=lambda:payload, execution_mode='auto_daily',
        accepted_role='accepted_closed_day_snapshot', allow_persisted_retry=True, current_web_source_sync_note=None)


def main():
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
            plan.closed_day_web_source_sync=Sync()
            status,result=capture(plan,source,payload)
            assert status.kind=='success' and _web_source_observation_is_closed(source,result,DAY)
            state=runtime.load_temporal_source_closure_state(source_key=source,target_date=DAY,slot_kind='yesterday_closed')
            assert state.state=='success' and state.next_retry_at is None
            capture(plan,source,payload)
            assert plan.closed_day_web_source_sync.calls==1, 'qualified closed source must be preserved'
    assert _next_closure_retry(NOW,999,'error') == (None,'closure_exhausted')
    print('seller portal repair smoke: header alias, failed collection, stale accepted/cache, rollover, bounded retry and dated success passed')


if __name__=='__main__': main()
