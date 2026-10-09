#!/usr/bin/env python3
"""Native exact admission/worker/retention; synthetic provider, stdlib only."""
import sys,json,multiprocessing,copy,tracemalloc
from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime,timezone
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application import operator_complaint_runs as op,operator_feedback_complaint_schedules as schedules
from packages.application import sheet_vitrina_v1_feedbacks_auto_complaints as native
from packages.application.sheet_vitrina_v1_feedbacks_complaints import SheetVitrinaV1FeedbacksComplaintsBlock
NOW=datetime(2026,5,8,8,tzinfo=timezone.utc)
class Feedbacks:
    def __init__(self):self.reads=0
    def build(self,**kwargs):self.reads+=1;return {'rows':[]}
class AI:
    def run(self,*args,**kwargs):raise AssertionError('no rows -> no AI')
def fixture(path):
    feedbacks=Feedbacks();block=native.SheetVitrinaV1FeedbacksAutoComplaintsBlock(runtime_dir=path,feedbacks_block=feedbacks,feedbacks_ai_block=AI(),complaints_block=SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=path),now_factory=lambda:NOW)
    block.store.save_schedules([{'id':'one','enabled':False}]);scope=op.RunScope(path,'operator','fixture','seller-portal-primary')
    return block,scope,feedbacks

def body(block,number=1):return dict(operation_id='complaint-run:'+f'{number:032x}',schedule_id='one',expected_source_revision=schedules.revision(block.store.read()))
def admit(block,scope,payload):return block.run_now(payload,operator_command=op.command(payload,scope))
def worker(block,result):
    run=block.store.get_run(result['run_id'])
    # Execute the actual native worker and actual portal admission lock. Only
    # storage credential validation/provider are synthetic; no browser opens.
    with patch.object(native,'seller_portal_storage_state_path',return_value=block.runtime_dir/'synthetic-storage.json'),patch.object(native,'validate_storage_state_path_for_runtime'):
        block._run_and_persist(run['run_id'],run[op.META]['schedule'])
def hold(*args,**kwargs):return SimpleNamespace(start=lambda:None)

def admit_process(path,payload,ready,start,result):
    runtime=Path(path)
    block=native.SheetVitrinaV1FeedbacksAutoComplaintsBlock(runtime_dir=runtime,feedbacks_block=Feedbacks(),feedbacks_ai_block=AI(),complaints_block=SheetVitrinaV1FeedbacksComplaintsBlock(runtime_dir=runtime),now_factory=lambda:NOW)
    ready.put(True);start.wait(15)
    with patch.object(native,'admitted_thread',side_effect=hold) as spawned:
        accepted=admit(block,op.RunScope(runtime,'operator','fixture','seller-portal-primary'),payload)
        result.put((accepted['run_id'],spawned.call_count))

def additional():
    from apps.sheet_vitrina_v1_feedbacks_auto_complaints_smoke import FakeFeedbacksBlock,FakeAiBlock,_row
    with TemporaryDirectory() as tmp:
        block,scope,feedbacks=fixture(Path(tmp));payload=body(block)
        ctx=multiprocessing.get_context('spawn');ready=ctx.Queue();result=ctx.Queue();start=ctx.Event()
        children=[ctx.Process(target=admit_process,args=(tmp,payload,ready,start,result)) for _ in range(2)]
        for child in children:child.start()
        for _ in children:ready.get(timeout=15)
        start.set();answers=[result.get(timeout=15) for _ in children]
        for child in children:child.join(15);assert child.exitcode==0
        ready.close();ready.join_thread();result.close();result.join_thread()
        assert len({item[0] for item in answers})==1 and sum(item[1] for item in answers)==1
        worker(block,{'run_id':answers[0][0]});assert op.read(payload['operation_id'],scope)['acceptance']['state']=='completed'
    with TemporaryDirectory() as tmp:
        block,scope,_=fixture(Path(tmp));calls=[]
        block.feedbacks_block=FakeFeedbacksBlock([_row('native-child','2026-05-08T06:30:00Z',1)])
        block.feedbacks_ai_block=FakeAiBlock({'native-child':'yes'})
        def submit(payload):
            calls.append(dict(payload));return dict(aggregate=dict(submitted_count=1,skipped_count=0,error_count=0),rows=[dict(feedback_id='native-child',submitted=True,submit_clicked=True)])
        block.complaints_block.submit_runner=submit
        with patch.object(native,'admitted_thread',side_effect=hold):saved=admit(block,scope,body(block));worker(block,saved)
        exact=op.read(saved['operation_id'],scope);assert exact['acceptance']['state']=='completed' and exact['acceptance']['processing']['submit_confirmed_count']==1
        assert not exact['acceptance']['external_confirmed'] and calls[0]['run_id']==saved['run_id']+'_submit'
        assert block.store.non_retryable_attempt_feedback_ids()=={'native-child'}
        detail=block.get_run(saved['run_id'])['run'];assert detail['attempts'][0]['reason']=='submitted_confirmed' and detail['attempts'][0]['run_id']==saved['run_id']
        with patch.object(native,'seller_portal_storage_state_path',return_value=Path(tmp)/'synthetic-storage.json'),patch.object(native,'validate_storage_state_path_for_runtime'):
            block._start_run(block.store.read()['schedules'][0],trigger_source='scheduled',due_at=NOW,async_run=False)
        assert len(calls)==1 and len(op.items(selected={op.DOMAIN},scope=scope))==1
        # Keep source counts but refuse completed when retained underlying proof
        # disappears/changes. Generic status patches are not native owner seal.
        report=block.store.report_root/saved['run_id']/'sheet_vitrina_v1_feedbacks_auto_complaints_run.json'
        report.write_text('{}');assert op.read(saved['operation_id'],scope)['acceptance']['state']=='needs_attention'
    with TemporaryDirectory() as tmp:
        block,scope,_=fixture(Path(tmp))
        with patch.object(native,'admitted_thread',side_effect=hold):saved=admit(block,scope,body(block))
        block.store.update_run(saved['run_id'],dict(status='completed',finished_at='2026-05-08T08:00:00Z'))
        assert op.read(saved['operation_id'],scope)['acceptance']['state']=='needs_attention'
        raw=block.store.get_run(saved['run_id']);raw['attempts']=[dict(feedback_id='f'*160,action='a'*80,reason='r'*240,complaint_journal_ref='j'*160,run_id='x'*160,evidence_refs=['e'*260]*20) for _ in range(200)]
        full=native._normalize_run(raw);bounded=op.finish(full)
        maximum=len(op.encoded(bounded));assert maximum<60000 and len(op.encoded(full))<op.COMPLETION_RESERVE
        print('Bounded 200 maximal native dedup attempts compact bytes:',maximum,'full result bytes:',len(op.encoded(full)),'manual admission-headroom record capacity:',(op.MAX_BYTES-op.COMPLETION_RESERVE-op.CONFIG_CAPACITY)//maximum)

def projection_regressions():
    from packages.application import operator_complaint_source_projection as projection
    with TemporaryDirectory() as tmp:
        path=Path(tmp).resolve()/schedules.FILENAME
        valid={'runs':[{'ignored':'diagnostic','nested':[True,False,None,1.5]},{op.META:{'x':'中文'},'exact':'kept'}],'schedules':[]}
        path.write_bytes(json.dumps(valid,ensure_ascii=False).encode())
        expected={**valid,'runs':[valid['runs'][1]]}
        assert schedules.raw(path)==expected
        # Skipped automatic text is bigger than one materialized-value bound,
        # yet is validated using bounded chunks, not loaded as a whole record.
        with path.open('wb') as out:
            out.write(b'{"runs":[{"diagnostic":"')
            for _ in range(160):out.write(b'x'*65536)
            out.write(b'"}]}')
        tracemalloc.start();assert schedules.raw(path)=={'runs':[]};_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        assert peak<1024*1024,peak
        invalids=[b'{',b'{"runs":[{"a":1,"a":2}]}',b'{"runs":[{"a":1,"\\u0061":2}]}',
            b'{"runs":[],"runs":[]}',b'{"runs":[{"x":"\\z"}]}',b'{"runs":[{"x":"\xff"}]}',
            b'{"runs":[{"x":NaN}]}',b'{"runs":[{"x":1e999}]}',b'{"runs":[{"x":01}]}',
            b'{"runs":[{"x":1.}]}',b'{"runs":[{"x":trueX}]}',b'{"runs":[{}],}',b'{"runs":[]}x',
            b'{"runs":[{"x":'+b'['*66+b'0'+b']'*66+b'}]}',b'{"runs":[{"'+b'x'*1025+b'":1}]}',
            b'{"runs":[{"x":"'+b'x'*65530+b'\xff"}]}']
        for invalid in invalids:
            path.write_bytes(invalid)
            try:schedules.raw(path)
            except (ValueError,UnicodeError):pass
            else:raise AssertionError(('invalid skipped/native JSON accepted',invalid[:90]))
        path.write_bytes(json.dumps(valid).encode())
        with patch.object(projection,'MAX_PROJECTION_BYTES',10):
            try:schedules.raw(path)
            except ValueError as error:assert 'size_exceeded' in str(error)
            else:raise AssertionError('retained manual projection was silently dropped')
        def replace():
            replacement=path.with_suffix('.tmp');replacement.write_bytes(path.read_bytes());replacement.replace(path)
        with patch.object(projection,'during_read',side_effect=replace):
            try:schedules.raw(path)
            except ValueError as error:assert 'source_changed' in str(error)
            else:raise AssertionError('atomic source replacement was not fenced')
        def rewrite():path.write_bytes(path.read_bytes()+b' ')
        with patch.object(projection,'during_read',side_effect=rewrite):
            try:schedules.raw(path)
            except ValueError as error:assert 'source_changed' in str(error)
            else:raise AssertionError('in-place source write was not fenced')
        link=Path(tmp).resolve()/'symlink';link.symlink_to(path)
        try:schedules.raw(link)
        except ValueError:pass
        else:raise AssertionError('symlink source followed')
        # No special-file reads and no unbounded object key/number tokens.
        import os
        fifo=Path(tmp).resolve()/'fifo';os.mkfifo(fifo)
        try:projection.read(fifo)
        except ValueError as error:assert 'source_not_regular' in str(error)
        else:raise AssertionError('nonregular source read')
        path.write_bytes(b'{"runs":[{"x":123456789}]}')
        with patch.object(projection,'MAX_NUMBER_BYTES',4):
            try:schedules.raw(path)
            except ValueError as error:assert 'number_bound' in str(error)
            else:raise AssertionError('unbounded skipped number')
        path.write_bytes(b'{"runs":[{"x":1,"y":2,"z":3}]}')
        with patch.object(projection,'MAX_KEYS',2):
            try:schedules.raw(path)
            except ValueError as error:assert 'key_count_bound' in str(error)
            else:raise AssertionError('unbounded skipped object key set')
        print('Native JSON strict skipped validation/depth/token/projection bounds, UTF8, duplicate keys, atomic/in-place source fences; 10MiB skipped peak bytes:',peak)


def automatic_growth_regression(*, target_mebibytes=46, admits=True):
    # Original independent witness: retained native manual history near 54MiB
    # plus 10 real automatic add_run writes grows the physical source >64MiB.
    # No provider or reader-limit patch; full automatic evidence is preserved.
    with TemporaryDirectory(prefix='complaint-growth-') as tmp:
        block,scope,_=fixture(Path(tmp))
        with patch.object(native,'admitted_thread',side_effect=hold):
            first=admit(block,scope,body(block));worker(block,first)
        source=block.store.read();original=block.store.get_run(first['run_id']);model=copy.deepcopy(original)
        for key in ('operator_native_terminal','operator_terminal_compact','operator_full_result_digest'):model.pop(key,None)
        model['attempts']=[dict(feedback_id=f'{n:04d}'+'f'*156,action='submitted_confirmed') for n in range(200)]
        size=len(op.encoded(source));sequence=2
        while size<target_mebibytes*1024*1024:
            cmd=copy.deepcopy(original[op.META]['command']);cmd['operation_id']='complaint-run:'+f'{sequence:032x}'
            row=copy.deepcopy(model);row['run_id']=op.native_id(cmd);row=op.finish(native._normalize_run(op.attach(row,cmd,original[op.META]['schedule'])))
            op.validate(row);assert op.terminal_valid(row);source['runs'].append(row);size+=len(op.encoded(row))+1;sequence+=1
        block.store._write_unlocked(source)
        last=first
        with patch.object(native,'admitted_thread',side_effect=hold) as spawned:
            if admits:
                last=admit(block,scope,body(block,sequence));worker(block,last)
            else:
                before=block.store.path.read_bytes()
                try:admit(block,scope,body(block,sequence))
                except op.NotSaved as error:assert error.code=='complaint_run_capacity_exceeded'
                else:raise AssertionError('future schedule/terminal headroom not reserved')
                assert not spawned.called and block.store.path.read_bytes()==before
        attempt=dict(feedback_id='a'*160,action='submitted_confirmed',reason='submitted_confirmed',evidence_refs=['synthetic-report-reference-'+('e'*230)]*20)
        automatic=dict(trigger_source='scheduled',status='completed',finished_at='2026-05-08T08:00:00Z',attempts=[dict(attempt,feedback_id=f'{n:04d}'+'a'*156) for n in range(200)])
        automatic_count=0
        while block.store.path.stat().st_size<=op.MAX_BYTES:
            block.store.add_run(dict(automatic,run_id=f'automatic-capacity-{automatic_count}'));automatic_count+=1
            assert automatic_count<25
        manual_count=sequence if admits else sequence-1
        assert op.read(first['operation_id'],scope)['acceptance']['state']=='completed'
        assert op.read(last['operation_id'],scope)['acceptance']['state']=='completed'
        assert len(op.items(selected={op.DOMAIN},scope=scope))==manual_count
        # Native full state/dedup and diagnostics remain intact after restart;
        # next manual admission counts its retained projection, not auto bytes.
        restarted=native.JsonFileFeedbacksAutoComplaintsStore(Path(tmp),now_factory=lambda:NOW)
        retained=restarted.read();assert len(retained['runs'])==manual_count+automatic_count
        auto=next(row for row in retained['runs'] if row['run_id']=='automatic-capacity-0')
        assert len(auto['attempts'][0]['evidence_refs'])==20
        assert '0000'+'a'*156 in restarted.non_retryable_attempt_feedback_ids()
        assert op.read(first['operation_id'],scope)['acceptance']['state']=='completed'
        if admits:
            with patch.object(native,'admitted_thread',side_effect=hold):new=admit(block,scope,body(block,sequence+1));worker(block,new)
            assert op.read(new['operation_id'],scope)['acceptance']['state']=='completed'
            manual_count+=1
        assert op.read(first['operation_id'],scope)['acceptance']['state']=='completed'
        print('Actual native automatic growth physical bytes:',block.store.path.stat().st_size,'retained manual IDs:',manual_count,'automatic full records:',automatic_count,'new admission:',admits,'earliest/restarted/new GET+all scoped journal IDs preserved: PASS')


def main():
    with TemporaryDirectory(prefix='complaint-runs-') as tmp:
        block,scope,feedbacks=fixture(Path(tmp));payload=body(block)
        with patch.object(native,'admitted_thread',side_effect=hold) as spawned:
            with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:admit(block,scope,payload),range(2)))
            assert results[0]==results[1] and spawned.call_count==1
            result=results[0];assert result['acceptance']['state']=='processing' and not feedbacks.reads
            before=block.store.path.read_bytes();assert admit(block,scope,payload)==result and block.store.path.read_bytes()==before
            try:admit(block,scope,body(block,2))
            except op.NotSaved as exc:assert exc.code=='complaint_run_native_busy'
            else:raise AssertionError('busy borrowed another job')
            for foreign in (replace(scope,actor='foreign'),replace(scope,account='foreign'),replace(scope,account_scope='foreign')):assert op.read(payload['operation_id'],foreign)['status']=='unknown'
            try:admit(block,replace(scope,actor='foreign'),payload)
            except op.NotSaved as exc:assert exc.code=='complaint_run_identity_conflict'
            else:raise AssertionError('foreign actor reused identity')
            with patch.object(native.JsonFileFeedbacksAutoComplaintsStore,'__init__',side_effect=AssertionError('GET owner bootstrap')):assert op.read(payload['operation_id'],scope)['acceptance']['state']=='processing'
            assert block.store.path.read_bytes()==before
            worker(block,result);done=op.read(payload['operation_id'],scope);assert done['acceptance']['state']=='completed',done
            assert done['acceptance']['native_state']=='no_new_feedbacks' and done['acceptance']['external_confirmed'] is False
            worker(block,result);assert feedbacks.reads==1
            # Never erase the earliest same-ID evidence after native automatic
            # retention, restart or >64 ordinary daily manual completions.
            for n in range(2,82):worker(block,admit(block,scope,body(block,n)))
            for n in range(230):block.store.add_run(dict(run_id=f'automatic_{n}',trigger_source='scheduled',status='completed'))
            source=block.store.read();assert sum(bool(r.get(op.META)) for r in source['runs'])==81
            assert sum(not r.get(op.META) for r in source['runs'])==200
            earliest=op.read(payload['operation_id'],scope);assert earliest==done
            native.JsonFileFeedbacksAutoComplaintsStore(Path(tmp),now_factory=lambda:NOW)
            assert op.read(payload['operation_id'],scope)==done
            assert admit(block,scope,payload)['run_id']==result['run_id'] and spawned.call_count==81
            size=len(block.store.path.read_bytes());terminal_bytes=len(op.encoded(next(r for r in source['runs'] if r.get(op.META))))
            assert terminal_bytes<5000,(terminal_bytes,size)
            print('Measured native terminal noop bytes:',terminal_bytes,'81 manual +200 automatic total bytes:',size)
            pending=admit(block,scope,body(block,82));assert pending['acceptance']['state']=='processing'
            native.JsonFileFeedbacksAutoComplaintsStore(Path(tmp),now_factory=lambda:NOW)
            restarted=op.read(pending['operation_id'],scope);assert restarted['acceptance']['state']=='needs_attention'
            worker(block,pending);assert feedbacks.reads==81
            blocked=body(block,83);before=block.store.path.read_bytes()
            with patch.object(op,'MAX_BYTES',1):
                try:admit(block,scope,blocked)
                except op.NotSaved as exc:assert exc.code=='complaint_run_capacity_exceeded'
                else:raise AssertionError('capacity admitted')
            assert block.store.path.read_bytes()==before
            # Post-replace ack loss leaves durable same-ID source; retrying GET
            # does not need a thread or the currently edited schedule.
            original=schedules.atomic_write
            def lost(path,value):original(path,value);raise OSError('synthetic post-replace lost reply')
            with patch.object(schedules,'atomic_write',side_effect=lost):
                recovered=admit(block,scope,blocked)
            assert recovered['acceptance']['state']=='processing'
            assert op.read(blocked['operation_id'],scope)['acceptance']['durable_saved']
            native.JsonFileFeedbacksAutoComplaintsStore(Path(tmp),now_factory=lambda:NOW)
            assert op.read(blocked['operation_id'],scope)['acceptance']['state']=='needs_attention'
            # Tampered operand/current native outcome never produces green done.
            raw=schedules.raw(block.store.path);saved=next(r for r in raw['runs'] if r.get(op.META) and r[op.META]['command']['operation_id']==payload['operation_id']);saved['status']='completed'
            block.store.path.write_text(json.dumps(raw));assert op.read(payload['operation_id'],scope)['acceptance']['state']=='needs_attention'
    # Measure all normalized 64 schedule rows, including maximal recognized
    # native runtime strings/stats; reserve is not guessed from typical rows.
    stats=native._run_stats({**{key:10**127 for key in native._run_stats({}) if key.endswith('count')},'blocker_reason':'😀'*200})
    schedule=native._normalize_schedule(dict(id='😀'*80,enabled=False,last_run_at='😀'*80,last_success_at='😀'*80,last_due_at='😀'*80,last_status='😀'*80,last_run_id='😀'*160,last_stats=stats),now='2026-05-08T08:00:00Z',now_factory=lambda:NOW)
    metadata_bytes=len(op.encoded([schedule]*64));assert metadata_bytes<op.CONFIG_METADATA_CAPACITY
    print('Measured64 normalized maximal native runtime schedule metadata bytes:',metadata_bytes,'reserved:',op.CONFIG_METADATA_CAPACITY)
    additional()
    projection_regressions()
    automatic_growth_regression()
    automatic_growth_regression(target_mebibytes=54,admits=False)
    print('operator_complaint_runs_smoke: PASS exact native admission/claim/noop worker/restart/81 terminal IDs/230 automatic retention/capacity/lost reply/foreign scope/terminal binding')
if __name__=='__main__':main()
