"""Exact own-state deltas for the existing posted-overhead S1–S5 commits.

No durable state machine is introduced. These images only fence concurrent edits
of the native rows the existing finalizer owns between its durable transactions.
"""
from __future__ import annotations
import json
from packages.application.dependency_proof import CompletionProofError


def _fail():
    raise CompletionProofError('overhead_completion_progress_changed')


def _unchanged(before,after,allowed=()):
    if {k:v for k,v in before.items() if k not in allowed}!={k:v for k,v in after.items() if k not in allowed}:
        _fail()


def _appended(before,after,*,matches):
    # Events are immutable: preserve every old row and admit at most one exact
    # stage event. Keys/entire row images prevent replacing an old event.
    old={row['event_id'] if 'event_id' in row else row['transition_id']:row for row in before}
    new={row['event_id'] if 'event_id' in row else row['transition_id']:row for row in after}
    if any(new.get(key)!=row for key,row in old.items()):_fail()
    added=[row for key,row in new.items() if key not in old]
    if len(added)>1 or any(not matches(row) for row in added):_fail()


class NativeProgressFence:
    def __init__(self,frozen):
        self.source=frozen
        self.expected=None
        self.proof=None
        self.fingerprint=None

    def image(self,conn):
        from packages.application.operator_ff_overhead import TABLE
        from packages.application.ff_pool_documents import REQUESTS_TABLE,TARGETED_RECALC_QUEUE_TABLE,WORKFLOW_EVENTS_TABLE
        request=self.source['request_id'];operation=self.source['recovery_operation_id']
        def one(sql,args):
            row=conn.execute(sql,args).fetchone()
            if row is None:_fail()
            return dict(row)
        return dict(
            queue=one(f'SELECT * FROM {TARGETED_RECALC_QUEUE_TABLE} WHERE queue_id=?',(self.source['queue_id'],)),
            acceptance=one(f'SELECT state,reason_code,updated_at,receipt_json FROM {TABLE} WHERE request_id=?',(request,)),
            request=one(f'SELECT state,posted_at,replay_at,completed_at,updated_at,error_code,error_details_json FROM {REQUESTS_TABLE} WHERE request_id=?',(request,)),
            recovery=one('SELECT * FROM sheet_vitrina_v1_recovery_operations WHERE operation_id=?',(operation,)),
            events=[dict(r) for r in conn.execute(f"SELECT * FROM {WORKFLOW_EVENTS_TABLE} WHERE action_type='facility_pool_document' AND identity=? ORDER BY event_id",(request,)).fetchall()],
            transitions=[dict(r) for r in conn.execute('SELECT * FROM sheet_vitrina_v1_recovery_transitions WHERE operation_id=? ORDER BY transition_id',(operation,)).fetchall()])

    def check(self,conn):
        image=self.image(conn)
        if self.expected is not None and image!=self.expected:_fail()
        return image

    def begin(self,conn,stage):
        image=self.check(conn)
        if self.expected is None:
            if stage!='S1' or image['acceptance']['state'] not in ('accepted','processing','delayed'):_fail()
            self.expected=image
        return image

    def bind_proof(self,proof,fingerprint):
        self.proof=json.loads(json.dumps(proof))
        self.fingerprint=fingerprint

    def _assert_proven(self,image):
        q=image['queue']
        if (q['status']!='complete' or q['economics_status']!='complete' or q['finance_status']!='complete'
                or q['economics_error'] or q['finance_error'] or q['finance_source_fingerprint']!=self.fingerprint
                or not q['economics_finished_at'] or not q['finance_finished_at']):_fail()
        try: receipt=json.loads(image['acceptance']['receipt_json'])
        except (ValueError,TypeError):_fail()
        if receipt.get('native_completion')!=self.proof:_fail()
        return receipt

    def finish(self,conn,stage,before):
        from packages.application.ff_pool_documents import _fingerprint
        after=self.image(conn)
        receipt=self._assert_proven(after)
        unchanged={'queue','acceptance','request','recovery','events','transitions'}
        if stage=='S1':
            unchanged-={'queue','acceptance'}
            _unchanged(before['queue'],after['queue'],('economics_status','economics_finished_at','economics_error','finance_status','finance_finished_at','finance_error','finance_source_fingerprint'))
            _unchanged(before['acceptance'],after['acceptance'],('receipt_json',))
        elif stage=='S2':
            unchanged-={'request','events'}
            _unchanged(before['request'],after['request'],('state','replay_at','updated_at','error_code','error_details_json'))
            expected='replay' if before['request']['state']=='posted' else before['request']['state']
            if after['request']['state']!=expected:_fail()
            _appended(before['events'],after['events'],matches=lambda row:row['stage']=='replay' and row['status']=='running')
        elif stage=='S3':
            unchanged-={'recovery','transitions'}
            allowed=('lifecycle_state','state_version','next_action','after_digest','writer_state','timer_state','rollback_available','updated_at','last_heartbeat_at')
            _unchanged(before['recovery'],after['recovery'],allowed)
            old=before['recovery'];new=after['recovery']
            if (new['lifecycle_state']!='retained' or new['state_version']!=old['state_version']+(old['lifecycle_state']!='retained')
                    or new['after_digest']!=self._after_digest()):_fail()
            _appended(before['transitions'],after['transitions'],matches=lambda row:row['to_state']=='retained' and row['state_version']==new['state_version'])
        elif stage=='S4':
            unchanged-={'request','events'}
            _unchanged(before['request'],after['request'],('state','completed_at','updated_at','error_code','error_details_json'))
            if after['request']['state']!='complete' or after['recovery']['lifecycle_state']!='retained':_fail()
            _appended(before['events'],after['events'],matches=lambda row:row['stage']=='replay' and row['status']=='complete' and _fingerprint(json.loads(row['details_json']))==self._after_digest())
        elif stage=='S5':
            unchanged-={'acceptance'}
            if (after['acceptance']['state']!='completed' or after['acceptance']['reason_code']
                    or after['request']['state']!='complete' or after['recovery']['lifecycle_state']!='retained'
                    or after['recovery']['after_digest']!=self._after_digest()
                    or receipt.get('native_state')!='complete' or receipt.get('native_posted') is not True
                    or receipt.get('document_id')!=self.source['posted_manifest']['document_id']
                    or receipt.get('native_recovery')!=dict(operation_id=self.source['recovery_operation_id'],lifecycle='retained',after_digest=self._after_digest())):_fail()
        elif stage!='readback':_fail()
        for key in unchanged:
            if before[key]!=after[key]:_fail()
        # This exact proposed image is only consumed after caller commit returns.
        # Any failed/ambiguous commit exits the invocation and starts fresh proof.
        self.expected=after

    def _after_digest(self):
        from packages.application.ff_pool_documents import _fingerprint
        return _fingerprint(self.source['native_after_image'])
