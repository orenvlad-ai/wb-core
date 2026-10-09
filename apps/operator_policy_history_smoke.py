"""Synthetic native policy versions and independently verified dated publication."""
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import datetime,timezone
from pathlib import Path
import json,sqlite3,sys,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps import operator_policy_smoke as native_fixture
DAY,NOW=native_fixture.DAY,native_fixture.NOW
from apps import vitrina_incident_rematerialization_smoke as fixture
from packages.application import operator_policy as op,operator_policy_history as h
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.owned_history_worker_capability import HistoryDelegationError

class Tests(unittest.TestCase):
    def setUp(self):
        self.case=native_fixture.Tests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.runtime=self.case.runtime;self.entry=self.case.entry
        self.dates=h.days_between('2026-08-07',DAY)
        payload={'effective_date':self.dates[0],'tax_rate':'0.3','commission_rate':'0.2','buyout_rate':'0.8','acquiring_rate':'0.015','logistics_per_order_rub':'50','return_logistics_per_non_buyout_rub':'50','comment':'synthetic historical policy'}
        # Preserve actual native required operands via the fixture's preview.
        base=self.case.entry.calculation_parameters_block.preview_version(payload)
        payload['preview_fingerprint']=base['preview_fingerprint'];self.identity=self.case.identity()
        op.accept(self.entry,'legacy_proxy',payload,actor='operator',operation_id=self.identity)
        with closing(op.readonly(self.runtime.db_path)) as conn:self.command=op.command_from_row(conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(self.identity,)).fetchone(),1)
        with heavy_admitted(self.runtime.runtime_dir,operation='fixture'):
            op._source_apply(self.entry,self.command);op._publish_economics(self.entry,self.command)
        self.install_ready()
    def install_ready(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            bundle,day,encoded=conn.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone()
            raw=json.loads(encoded);plan=asdict(fixture._plan([101,202]));plan.update(plan_version='fixture-native-dated-v1',snapshot_id='fixture-dated',as_of_date=DAY,date_columns=self.dates,metadata=deepcopy(raw['metadata']));plan['temporal_slots']=[{'slot_key':d,'slot_label':d,'column_date':d} for d in self.dates]
            data=plan['sheets'][0];data['header']=['label','key',*self.dates];data['rows']=[[r[0],r[1],*[r[2] for _ in self.dates]] for r in raw['sheets'][0]['rows']];data['row_count']=len(data['rows']);data['column_count']=len(data['header'])
            for key,value in list(plan['metadata'].items()):
                if isinstance(value,dict) and DAY in value:
                    plan['metadata'][key]={d:deepcopy(value[DAY]) for d in self.dates}
            for row,by_date in plan['metadata'].get('server_cell_presentation',{}).items():
                if DAY in by_date:
                    cell=by_date[DAY];plan['metadata']['server_cell_presentation'][row]={d:{**deepcopy(cell),'source_as_of_date':d} for d in self.dates}
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(plan),))
            conn.execute('INSERT INTO registry_upload_current_state(slot,bundle_version,activated_at) VALUES(1,?,?)',(bundle,NOW));conn.commit()
    def prepare(self):return h.prepare(self.runtime,self.command,today=DAY,created_at=NOW)
    def publish(self,manifest):
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'):return h.publish(self.runtime,manifest)
    def test_exact_twenty_dates_native_publication_is_not_history_completion(self):
        manifest=self.prepare();self.assertEqual(manifest['dates'],self.dates)
        receipt=self.publish(manifest);read=receipt.validate_sources_readonly(now=self.entry.now_factory());self.assertEqual(set(read['dated']),set(self.dates))
        self.assertEqual(op.read(self.runtime.db_path,self.identity,actor='operator')['state'],'needs_attention')
        with closing(op.readonly(self.runtime.db_path)) as conn:self.assertIsNone(op.binding(conn,self.identity,'publication'))
        again=h.PolicyHistory(self.runtime,receipt.operation_id,receipt.attempt_id);self.assertEqual(again.binding(),receipt.binding())
    def test_publication_does_not_change_cost_quantity_or_other_input_cells(self):
        manifest=self.prepare()
        for target in manifest['targets']:
            before=json.loads(target['expected']['plan_json']);after=json.loads(target['after_json'])
            b={r[1]:r[2:] for r in before['sheets'][0]['rows']};a={r[1]:r[2:] for r in after['sheets'][0]['rows']}
            changed={k for k in b if b[k]!=a[k]};self.assertTrue(changed)
            self.assertTrue(all('proxy_' in k for k in changed));self.assertEqual(before['metadata'].get('fbs_accounting_bindings'),after['metadata'].get('fbs_accounting_bindings'))
    def test_rehashed_foreign_candidate_fails_independent_rebuild(self):
        manifest=self.prepare();payload=json.loads(manifest['targets'][0]['after_json']);payload['sheets'][0]['rows'][0][2]=987654
        manifest['targets'][0]['after_json']=op.canonical(payload);manifest['manifest_digest']=op.digest({k:v for k,v in manifest.items() if k!='manifest_digest'})
        with self.assertRaisesRegex(ValueError,'independent_rebuild_mismatch'):self.publish(manifest)
        with closing(op.readonly(self.runtime.db_path)) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],0)
    def test_rehashed_missing_date_is_not_partial_completion(self):
        manifest=self.prepare();manifest['dates'].pop();manifest['targets'][0]['dates'].pop();manifest['manifest_digest']=op.digest({k:v for k,v in manifest.items() if k!='manifest_digest'})
        with self.assertRaisesRegex(ValueError,'affected_date_scope_changed'):self.publish(manifest)
    def test_late_ready_race_preserves_entire_publication(self):
        manifest=self.prepare()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            payload=json.loads(manifest['targets'][0]['expected']['plan_json']);payload['sheets'][0]['rows'][0][2]='foreign';conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(payload),));conn.commit()
        with self.assertRaisesRegex(Exception,'ready_target_changed'):self.publish(manifest)
    def test_unknown_source_revision_does_not_authorize_history(self):
        manifest=self.prepare()
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:conn.execute('UPDATE '+op.NATIVE['legacy_proxy']+' SET fingerprint=?',('sha256:foreign',));conn.commit()
        with self.assertRaisesRegex(ValueError,'source_inputs_changed'):self.publish(manifest)
    def test_generic_history_flag_cannot_finish_operator(self):
        receipt=self.publish(self.prepare())
        with self.assertRaisesRegex(HistoryDelegationError,'not_supervised'):receipt._acknowledge_verified_native({'completed':True})
        with closing(op.readonly(self.runtime.db_path)) as conn:
            native=op.binding(conn,self.identity);self.assertFalse(h.publication_proof_valid(conn,native,{'consumer':h.CONTRACT,'exact_version':native['identity'],'readback_verified':True,'native_operation_id':receipt.operation_id,'native_attempt_id':receipt.attempt_id}))
    def accept_native(self,day):
        payload={**self.command.source['payload'],'effective_date':day}
        payload['preview_fingerprint']=self.entry.calculation_parameters_block.preview_version(payload)['preview_fingerprint']
        identity=self.case.identity();op.accept(self.entry,'legacy_proxy',payload,actor='operator',operation_id=identity)
        with closing(op.readonly(self.runtime.db_path)) as conn:command=op.command_from_row(conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(identity,)).fetchone(),1)
        with heavy_admitted(self.runtime.runtime_dir,operation='fixture'):
            op._source_apply(self.entry,command);op._publish_economics(self.entry,command)
        return command
    def drop_first_date(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            payload=json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone()[0]);payload['date_columns'].pop(0);payload['temporal_slots'].pop(0);sheet=payload['sheets'][0];sheet['header'].pop(2);sheet['column_count']-=1
            for row in sheet['rows']:row.pop(2)
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(payload),));conn.commit()
    def test_missing_old_operand_does_not_starve_next_valid_native_source(self):
        second=self.accept_native('2026-08-08');self.drop_first_date()
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'):receipt=h.pending(self.runtime,now=self.entry.now_factory())
        self.assertEqual(receipt.manifest['command_id'],second.operation_id)
        first=op.read(self.runtime.db_path,self.identity,actor='operator');self.assertEqual(first['state'],'needs_attention');self.assertIn('dated_ready_inputs_missing',first['reason_code'])
        with closing(op.readonly(self.runtime.db_path)) as conn:self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],1)
    def test_more_than_cohort_limit_progresses_across_cycles(self):
        # Real native no-change commands, not invented queue flags. Their old
        # operand is unavailable; a later valid dated source must get a turn.
        blocked=[self.command]+[self.accept_native(self.dates[0]) for _ in range(h.MAX_COHORT)]
        second=self.accept_native('2026-08-08');self.drop_first_date()
        # Native accepted_at is immutable. Fixture IDs are ordered and the
        # clock is pinned, so the valid source is beyond the scan bound.
        now=self.entry.now_factory()
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'):
            self.assertIsNone(h.pending(self.runtime,now=now))
            from datetime import timedelta
            receipt=h.pending(self.runtime,now=now+timedelta(seconds=1))
        self.assertEqual(receipt.manifest['command_id'],second.operation_id)
    def test_prior_receipt_superseded_source_is_attention_not_cycle_failure(self):
        prior=self.publish(self.prepare());second=self.accept_native(self.dates[0])
        # A no-change native version is the same source; make an actual second
        # authorized changed parameter version at that exact date.
        payload={**second.source['payload'],'tax_rate':'0.31'};payload['preview_fingerprint']=self.entry.calculation_parameters_block.preview_version(payload)['preview_fingerprint']
        identity=self.case.identity();op.accept(self.entry,'legacy_proxy',payload,actor='operator',operation_id=identity)
        with closing(op.readonly(self.runtime.db_path)) as conn:command=op.command_from_row(conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(identity,)).fetchone(),1)
        with heavy_admitted(self.runtime.runtime_dir,operation='fixture'):
            op._source_apply(self.entry,command);op._publish_economics(self.entry,command)
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'):receipt=h.pending(self.runtime,now=self.entry.now_factory())
        self.assertEqual(receipt.manifest['command_id'],identity)
        first=op.read(self.runtime.db_path,self.identity,actor='operator');self.assertEqual(first['state'],'needs_attention');self.assertEqual(first['reason_code'],'native_source_drift')
    def test_old_code_authority_attention_preserves_manifest_and_next_source_progress(self):
        old=self.publish(self.prepare());old_manifest=deepcopy(old.manifest)
        second=self.accept_native('2026-08-08')
        changed={**h.code_authority(),'synthetic_next_code_epoch':'a'*64}
        with patch.object(h,'code_authority',return_value=changed),heavy_admitted(self.runtime.runtime_dir,operation='cycle'):
            receipt=h.pending(self.runtime,now=self.entry.now_factory())
        self.assertEqual(receipt.manifest['command_id'],second.operation_id)
        first=op.read(self.runtime.db_path,self.identity,actor='operator')
        self.assertEqual(first['state'],'needs_attention');self.assertEqual(first['reason_code'],'policy_history_formula_changed')
        self.assertIsNone(first['processing_receipt']['publication']);self.assertEqual(old._read()[0],old_manifest)
    def test_new_ready_book_envelope_preserves_exact_native_dated_lineage(self):
        from apps.fbs_accounting_runtime_smoke import RuntimeTests,capture,wb
        from packages.application import fbs_accounting_runtime as accounting
        case=RuntimeTests();case.setUp();self.addCleanup(case.doCleanups)
        case.day=self.dates[0];case.now=datetime(2026,8,7,14,tzinfo=timezone.utc);case.image=capture(case.day);case.wb=wb(case.day)
        book,_=case.opening();first=accounting._save_book(self.runtime.runtime_dir,book,expected=None,operation_id='fixture-book-first')
        day=self.dates[0]
        def rebind(version,target):
            with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                raw=json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone()[0]);meta=raw['metadata'];payload=book['presentations'][day]
                meta.setdefault('fbs_accounting_bindings',{})[day]={'book_version':version,'presentation_version':payload['version_id'],'effective_date':book['effective_date'],'date':day,'quality':payload['quality'],'source':accounting.SOURCE,'ready_target':target}
                meta.setdefault('fbs_accounting_targets',{})[day]=target;conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(raw),));conn.commit()
        rebind(first,'original-target');receipt=self.publish(self.prepare())
        # Native immutable book commit keeps this closed prefix while its
        # envelope changes; selected ready rebinds to the new exact revision.
        next_book=deepcopy(book);next_book['fixture_envelope_revision']=2
        second=accounting._save_book(self.runtime.runtime_dir,next_book,expected=first,operation_id='fixture-book-second');rebind(second,'next-target')
        self.assertEqual(set(receipt.validate_sources_readonly(now=self.entry.now_factory())['dated']),set(self.dates))
        next_book['wb_days'][day]['fixture_foreign_operand']=1
        third=accounting._save_book(self.runtime.runtime_dir,next_book,expected=second,operation_id='fixture-book-third');rebind(third,'foreign-target')
        with self.assertRaisesRegex(ValueError,'dated_book_lineage_changed'):receipt.validate_sources_readonly(now=self.entry.now_factory())
    def bind_book(self):
        from apps.fbs_accounting_runtime_smoke import RuntimeTests,capture,wb
        from packages.application import fbs_accounting_runtime as accounting
        case=RuntimeTests();case.setUp();self.addCleanup(case.doCleanups)
        day=self.dates[0];case.day=day;case.now=datetime.fromisoformat(day+'T14:00:00+00:00');case.image=capture(day);case.wb=wb(day)
        book,_=case.opening()
        version=accounting._save_book(self.runtime.runtime_dir,book,expected=None,operation_id='native-policy-book-fixture')
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            raw=json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone()[0]);meta=raw['metadata'];payload=book['presentations'][day]
            meta.setdefault('fbs_accounting_bindings',{})[day]={'book_version':version,'presentation_version':payload['version_id'],'effective_date':book['effective_date'],'date':day,'quality':payload['quality'],'source':accounting.SOURCE,'ready_target':'native-book-target'}
            meta.setdefault('fbs_accounting_targets',{})[day]='native-book-target'
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(raw),));conn.commit()
        return book,version

    def test_prepare_publish_and_readonly_validate_native_book_once_outside_writer(self):
        from packages.application import fbs_accounting_runtime as accounting
        self.bind_book();original=accounting.load;loads=[]
        def load(*args,**kwargs):
            self.assertIn('connection',kwargs)
            with closing(sqlite3.connect(self.runtime.db_path,timeout=0)) as competing:
                competing.execute('BEGIN IMMEDIATE');competing.rollback()
            loads.append(kwargs['version']);return original(*args,**kwargs)
        with patch.object(accounting,'load',load):
            manifest=self.prepare();self.assertEqual(len(loads),1)
            receipt=self.publish(manifest);self.assertEqual(len(loads),2)
            proof=receipt.validate_sources_readonly(now=self.entry.now_factory());self.assertEqual(len(loads),3)
            self.assertEqual(set(proof['dated']),set(self.dates))
        self.assertTrue(manifest['targets'][0]['dated_book_lineage'][self.dates[0]])

    def test_native_book_opening_replacement_refuses_and_closes_reader(self):
        import os
        from packages.application import fbs_accounting_runtime as accounting
        book,old_version=self.bind_book();manifest=self.prepare()
        target=accounting.path(self.runtime.runtime_dir)
        replacement_root=self.runtime.runtime_dir/'opening-replacement'
        with accounting.writer_lock(replacement_root):
            new_version=accounting._save_book(replacement_root,{**book,'opening_native_envelope':True},expected=None,operation_id='opening-replacement')
        incoming=accounting.path(replacement_root);connect=sqlite3.connect;opened=[];injected=[]
        def changed(*args,**kwargs):
            observer=connect(*args,**kwargs)
            if str(args[0]).startswith(target.as_uri()+'?mode=ro') and not injected:
                opened.append(observer)
                self.assertEqual(observer.execute('SELECT version FROM accounting_current').fetchone()[0],old_version)
                before=target.stat()
                with accounting.writer_lock(self.runtime.runtime_dir):os.replace(incoming,target)
                after=target.stat()
                self.assertNotEqual((before.st_dev,before.st_ino),(after.st_dev,after.st_ino))
                injected.append(True)
            return observer
        with patch.object(sqlite3,'connect',changed),self.assertRaisesRegex(ValueError,'dated_book_observer_changed'):
            self.publish(manifest)
        self.assertEqual(injected,[True])
        with self.assertRaises(sqlite3.ProgrammingError):opened[0].execute('SELECT 1')
        with self.assertRaisesRegex(ValueError,'bound_revision_missing'):accounting.load(self.runtime.runtime_dir,version=old_version)
        self.assertEqual(accounting.load(self.runtime.runtime_dir)[1],new_version)
        # Failure closes the reader before a subsequent actual native writer.
        with accounting.writer_lock(self.runtime.runtime_dir),closing(connect(target,timeout=0)) as writer:
            writer.execute('BEGIN EXCLUSIVE');writer.execute('UPDATE accounting_current SET version=version');writer.commit()
        with closing(connect(self.runtime.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],0)

    def test_publication_book_and_ready_race_after_cache_preparation_rolls_back(self):
        from contextlib import contextmanager
        from packages.application import fbs_accounting_runtime as accounting
        self.bind_book();manifest=self.prepare();original=h.prepared_book_lineage
        for mutation in ('book','ready','source'):
            @contextmanager
            def changed(*args,**kwargs):
                with original(*args,**kwargs) as books:
                    if mutation=='book':
                        with sqlite3.connect(accounting.path(self.runtime.runtime_dir)) as writer:writer.execute("UPDATE accounting_current SET version='foreign-pointer'")
                    else:
                        with sqlite3.connect(self.runtime.db_path) as writer:
                            if mutation=='source':writer.execute('UPDATE '+op.NATIVE['legacy_proxy']+" SET fingerprint='foreign'")
                            else:
                                raw=json.loads(manifest['targets'][0]['expected']['plan_json']);raw['sheets'][0]['rows'][0][2]='foreign'
                                writer.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(raw),))
                    yield books
            with self.subTest(mutation=mutation),patch.object(h,'prepared_book_lineage',changed),self.assertRaisesRegex(Exception,'dated_book_observer_changed|ready_target_changed|source_inputs_changed'):self.publish(manifest)
            with closing(sqlite3.connect(self.runtime.db_path)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM sheet_vitrina_v1_ready_publications WHERE kind=?',(h.CONTRACT,)).fetchone()[0],0)
                if mutation=='ready':conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(manifest['targets'][0]['expected']['plan_json'],))
                elif mutation=='source':conn.execute('UPDATE '+op.NATIVE['legacy_proxy']+' SET fingerprint=?',(manifest['native']['row']['fingerprint'],))
                conn.commit()

    def test_missing_prepared_book_key_never_falls_back_under_writer(self):
        from contextlib import contextmanager
        from packages.application import fbs_accounting_runtime as accounting
        self.bind_book();manifest=self.prepare();original=h.prepared_book_lineage;load=accounting.load;calls=[]
        @contextmanager
        def missing(*args,**kwargs):
            with original(*args,**kwargs) as books:
                books.cache.clear();yield books
        def native_load(*args,**kwargs):
            self.assertIn('connection',kwargs)
            with closing(sqlite3.connect(self.runtime.db_path,timeout=0)) as competitor:competitor.execute('BEGIN IMMEDIATE');competitor.rollback()
            calls.append(True);return load(*args,**kwargs)
        with patch.object(h,'prepared_book_lineage',missing),patch.object(accounting,'load',native_load),self.assertRaisesRegex(ValueError,'dated_book_cache_missing'):self.publish(manifest)
        self.assertEqual(calls,[True])

    def test_later_native_envelope_after_committed_publication_is_not_false_failure(self):
        from contextlib import contextmanager
        from packages.application import fbs_accounting_runtime as accounting
        book,version=self.bind_book();manifest=self.prepare();lock=accounting.writer_lock;advanced=[]
        @contextmanager
        def released(*args,**kwargs):
            with lock(*args,**kwargs):yield
            next_book={**book,'next_native_envelope':True}
            advanced.append(accounting._save_book(self.runtime.runtime_dir,next_book,expected=version,operation_id='native-after-policy-commit'))
        with patch.object(accounting,'writer_lock',released):receipt=self.publish(manifest)
        self.assertEqual(len(advanced),1)
        self.assertEqual(set(receipt.validate_sources_readonly(now=self.entry.now_factory())['dated']),set(self.dates))

    def test_unavailable_old_date_is_not_current_substitution(self):
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:
            row=conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone();payload=json.loads(row[0]);payload['date_columns'].pop(0);payload['temporal_slots'].pop(0);sheet=payload['sheets'][0];sheet['header'].pop(2);sheet['column_count']-=1
            for r in sheet['rows']:r.pop(2)
            conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(payload),));conn.commit()
        with self.assertRaisesRegex(ValueError,'dated_ready_inputs_missing'):self.prepare()

class IncidentTests(unittest.TestCase):
    def setUp(self):
        self.case=native_fixture.IncidentTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.runtime=self.case.runtime;self.case.accept();self.case.drain()
        with closing(op.readonly(self.runtime.db_path)) as conn:self.command=op.command_from_row(conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(self.case.identity,)).fetchone(),1)
    def test_exact_native_incident_zero_after_closed_interval(self):
        from packages.application.registry_upload_db_backed_runtime import _deserialize_temporal_source_payload
        case=native_fixture.IncidentTests();case.setUp();self.addCleanup(case.doCleanups)
        dates=h.days_between('2026-08-07',DAY)
        payload={'base_revision':0,'active':True,'excluded_wb_warehouse_ids':[101],'reason':'synthetic ended interval','effective_from':dates[0],'effective_to':'2026-08-20','status':'active','change_effective_from':dates[0]}
        identity='oppolicy_'+'c'*32
        op.accept(case.entry,'wb_incident_policy',payload,actor='operator',operation_id=identity)
        with closing(op.readonly(case.runtime.db_path)) as conn:
            command=op.command_from_row(conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(identity,)).fetchone(),1)
            stock=conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='stocks' AND snapshot_date=?",(DAY,)).fetchone()[0]
        for day in dates:
            dated=_deserialize_temporal_source_payload(stock);dated.snapshot_date=day
            case.runtime.save_temporal_source_snapshot(source_key='stocks',snapshot_date=day,captured_at=NOW,payload=dated)
        with closing(sqlite3.connect(case.runtime.db_path)) as conn:
            before=json.loads(conn.execute('SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots').fetchone()[0]);before['date_columns']=dates;before['temporal_slots']=[{'slot_key':d,'slot_label':d,'column_date':d} for d in dates];sheet=before['sheets'][0];sheet['header']=['label','key',*dates];sheet['rows']=[[r[0],r[1],*[r[2] for _ in dates]] for r in sheet['rows']];sheet['column_count']=len(sheet['header']);conn.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=?',(op.canonical(before),));conn.commit()
        with heavy_admitted(case.runtime.runtime_dir,operation='fixture'):op._source_apply(case.entry,command)
        m=h.prepare(case.runtime,command,today=DAY,created_at=NOW);self.assertEqual(m['dates'],dates)
        target=m['targets'][0];after=json.loads(target['after_json']);rows={r[1]:r for r in after['sheets'][0]['rows']};header=after['sheets'][0]['header']
        from packages.application.sheet_vitrina_v1_incident_stocks import INCIDENT_STOCK_METRIC_KEYS
        incident=[k for k in rows if k.startswith('SKU:') and k.split('|',1)[1] in INCIDENT_STOCK_METRIC_KEYS and 'incident' in k and 'effective' not in k]
        self.assertTrue(incident)
        for day in dates:
            self.assertEqual(target['evidence'][day]['policy_revision'],1)
            if day>'2026-08-20':self.assertTrue(all(rows[k][header.index(day)]==0 for k in incident))
        with heavy_admitted(case.runtime.runtime_dir,operation='cycle'):r=h.publish(case.runtime,m)
        self.assertEqual(set(r.validate_sources_readonly(now=case.entry.now_factory())['dated']),set(dates))
        self.fixture_case,self.published_receipt=case,r
    def test_incident_real_readonly_evaluator_publication_and_raw_stock_cas(self):
        m=h.prepare(self.runtime,self.command,today=DAY,created_at=NOW)
        with closing(op.readonly(self.runtime.db_path)) as conn:original=conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='stocks'").fetchone()[0]
        with heavy_admitted(self.runtime.runtime_dir,operation='cycle'):r=h.publish(self.runtime,m)
        r.validate_sources_readonly(now=self.case.entry.now_factory())
        with closing(op.readonly(self.runtime.db_path)) as conn:self.assertEqual(original,conn.execute("SELECT payload_json FROM temporal_source_snapshots WHERE source_key='stocks'").fetchone()[0])
        with closing(sqlite3.connect(self.runtime.db_path)) as conn:conn.execute("UPDATE temporal_source_snapshots SET captured_at='foreign' WHERE source_key='stocks'");conn.commit()
        with self.assertRaisesRegex(ValueError,'source_inputs_changed'):r.validate_sources_readonly(now=self.case.entry.now_factory())

class PrioritySeamTests(unittest.TestCase):
    """Only orchestration is mocked; actual dated authority is covered below."""
    def test_one_selected_source_in_ff_policy_supplier_order(self):
        from apps.operator_supplier_history_cycle_smoke import SeamTests, cycle_receipt, finance_result
        from packages.application.business_data_procedure_admission import admitted_write
        from packages.application.warehouse_functional_lock import require_warehouse_job_owner
        for selected in ('ff', 'policy', 'supplier', 'ordinary'):
            with self.subTest(selected=selected):
                case=SeamTests();case.setUp()
                try:
                    def pending_policy(*args,**kwargs):
                        require_warehouse_job_owner(case.rt.runtime_dir)
                        return case.supplier if selected=='policy' else None
                    def build(**kwargs):
                        case.events.append(('build',kwargs));return {'status':'success'}
                    def finance(*args,**kwargs):
                        case.events.append('finance');return finance_result()
                    with patch('packages.application.web_vitrina_snapshot_admission.process_identity',return_value='synthetic-seam-process'), \
                         admitted_write(case.rt.runtime_dir), heavy_admitted(case.rt.runtime_dir,operation='cycle'), \
                         patch('packages.application.operator_policy_history.pending',side_effect=pending_policy) as pp, \
                         patch('packages.application.operator_supplier_history.pending',return_value=case.supplier if selected=='supplier' else None) as sp, \
                         patch('apps.web_vitrina_history_candidate_build.build_owned_cycle_history',side_effect=build), \
                         patch('apps.warehouse_functional_runner._recalculate_downstream_finance_cost',side_effect=finance), \
                         patch('packages.application.fbs_accounting_historical_cycle.finalize',return_value={'status':'complete'}) as finalize:
                        case.entry._cycle_history(None,cycle_receipt(),{},historical_receipt=object() if selected=='ff' else None)
                    self.assertEqual(pp.call_count, 0 if selected=='ff' else 1)
                    self.assertEqual(sp.call_count, 0 if selected in ('ff','policy') else 1)
                    self.assertEqual(finalize.call_count, int(selected=='ff'))
                    options=next(v[1] for v in case.events if isinstance(v,tuple))
                    self.assertEqual('policy_receipt' in options, selected=='policy')
                    self.assertEqual('supplier_receipt' in options, selected=='supplier')
                    self.assertEqual('finance' in case.events, selected=='supplier')
                finally:case.doCleanups()

@unittest.skipUnless(sys.platform=='linux','actual inherited kernel History authority required')
class LinuxTests(unittest.TestCase):
    def test_actual_supervised_child_all_twenty_dates_and_atomic_operator_ack(self):self.actual_completion(bound_book=True)
    def test_actual_source_race_after_consume_blocks_final_ack(self):self.actual_completion(race='stocks')
    def test_actual_ready_race_after_consume_blocks_final_ack(self):self.actual_completion(race='ready')
    def test_actual_book_race_after_ack_cache_preparation_refuses(self):self.actual_completion(race='book',bound_book=True)
    def actual_completion(self,*,race=None,bound_book=False):
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from packages.application import owned_history_worker as supervisor
        from packages.application.web_vitrina_snapshot_admission import ApiJobMarkers
        from packages.application.web_vitrina_history_store import HistoryStore
        from packages.application.ready_publication import ensure_publication_schema
        import hashlib
        case=IncidentTests();case.setUp();self.addCleanup(case.doCleanups)
        if bound_book:
            original_prepare=h.prepare
            def prepare_bound(runtime,command,**kwargs):
                from types import SimpleNamespace
                holder=SimpleNamespace(runtime=runtime,dates=h.days_between(command.source['effective_date'],DAY),addCleanup=self.addCleanup)
                Tests.bind_book(holder)
                return original_prepare(runtime,command,**kwargs)
            with patch.object(h,'prepare',side_effect=prepare_bound):case.test_exact_native_incident_zero_after_closed_interval()
        else:case.test_exact_native_incident_zero_after_closed_interval()
        runtime=case.fixture_case.runtime;receipt=case.published_receipt;now=case.fixture_case.entry.now_factory()
        # A real app startup has these native schemas before any RO History
        # child. Bootstrap the same blocks only in this isolated fixture.
        from packages.application.own_product_capital import OwnProductCapitalBlock
        from packages.application.calculation_parameters import CalculationParametersBlock
        from packages.application.calculation_parameters_v4 import ProxyV4ParametersBlock
        OwnProductCapitalBlock(runtime=runtime);legacy=CalculationParametersBlock(runtime=runtime)
        legacy.ensure_initial_version(created_at=NOW,created_by='synthetic policy fixture')
        ProxyV4ParametersBlock(runtime=runtime,now_factory=case.fixture_case.entry.now_factory)
        # The earlier accepted legacy policy has no dated Proxy operands in
        # this stock-only fixture. It must not starve the valid incident.
        payload={'effective_date':receipt.dates[0],'tax_rate':'0.3','commission_rate':'0.2','buyout_rate':'0.8','acquiring_rate':'0.015','logistics_per_order_rub':'50','return_logistics_per_non_buyout_rub':'50','comment':'blocked native legacy source'}
        entry=case.fixture_case.entry;entry.calculation_parameters_block=legacy
        payload['preview_fingerprint']=legacy.preview_version(payload)['preview_fingerprint'];blocked='oppolicy_'+'0'*31+'1'
        op.accept(entry,'legacy_proxy',payload,actor='operator',operation_id=blocked)
        with closing(op.readonly(runtime.db_path)) as conn:command=op.command_from_row(conn.execute('SELECT * FROM '+op.TABLE+' WHERE operation_id=?',(blocked,)).fetchone(),1)
        with heavy_admitted(runtime.runtime_dir,operation='cycle'):
            op._source_apply(entry,command);op._publish_economics(entry,command)
            recovered=h.pending(runtime,now=now)
        self.assertEqual(recovered.binding(),receipt.binding());self.assertEqual(op.read(runtime.db_path,blocked,actor='operator')['state'],'needs_attention')
        with TemporaryDirectory() as directory,closing(sqlite3.connect(runtime.db_path)) as keeper:
            keeper.execute('PRAGMA journal_mode=WAL');ensure_publication_schema(keeper);keeper.commit();keeper.execute('SELECT 1 FROM sqlite_master').fetchone()
            root=Path(directory)/'candidate';root.mkdir(mode=0o700)
            for name in ('.web-vitrina-finished-builder.lock','.wb-finance-daily-worker.lock'):(runtime.runtime_dir/name).touch(mode=0o600)
            markers=ApiJobMarkers(runtime.runtime_dir);marker=markers.start('policy-history-fixture','cycle');cycle={**markers.owner,'job_id':'policy-history-fixture','operation':'cycle'}
            self.addCleanup(lambda:markers.finish(marker))
            contract=json.loads((ROOT/'artifacts/registry_upload_http_entrypoint/input/web_vitrina_history_runtime.json').read_text())
            # Isolated source fixture pins this checkout's actual formula code.
            # Root regenerates the governed deployed contract on integration.
            contract['candidate_root']=str(root);contract['formula_code_hashes']={relative:hashlib.sha256((ROOT/relative).read_bytes()).hexdigest() for relative in contract['formula_code_hashes']}
            contract['formula_epoch']='wbc0069k16-reviewed-native-v1:'+hashlib.sha256(json.dumps(contract['formula_code_hashes'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            path=root/'contract.json';path.write_text(json.dumps(contract))
            config=SimpleNamespace(candidate_root=root,runtime_contract=path,formula_epoch=contract['formula_epoch'],budget_seconds=120,max_recomputes=4)
            backfill=tuple(d for d in receipt.dates if d<'2026-08-25')
            with heavy_admitted(runtime.runtime_dir,operation='cycle'),patch('apps.web_vitrina_history_candidate_build.runtime_storage_admission'),patch('apps.web_vitrina_finished_snapshot_build.systemd_admission',return_value='idle'):
                with supervisor.owned_history_worker(runtime=runtime,config=config,cycle_owner=cycle) as worker:
                    invoke=worker._invoke
                    def diagnostic(mode,*args):
                        result=invoke(mode,*args)
                        if result.get('result',{}).get('status')=='failed':
                            raise AssertionError('actual History '+mode+' failed: '+str(result['result']))
                        return result
                    from packages.application import owned_history_native_ack as ack
                    original_consume=ack.consume_policy_history_ack
                    def consume_then_race(*args,**kwargs):
                        proof=original_consume(*args,**kwargs)
                        if race and race!='book':
                            # Separate writer commits after authentic one-use
                            # supervisor consume, before final BEGIN IMMEDIATE.
                            with closing(sqlite3.connect(runtime.db_path)) as writer:
                                if race=='stocks':writer.execute("UPDATE temporal_source_snapshots SET captured_at='foreign' WHERE source_key='stocks' AND snapshot_date=?",(receipt.dates[0],))
                                else:
                                    row=writer.execute('SELECT bundle_version,as_of_date,plan_json FROM sheet_vitrina_v1_ready_snapshots LIMIT 1').fetchone();raw=json.loads(row[2]);raw['sheets'][0]['rows'][0][2]='foreign'
                                    writer.execute('UPDATE sheet_vitrina_v1_ready_snapshots SET plan_json=? WHERE bundle_version=? AND as_of_date=?',(op.canonical(raw),row[0],row[1]))
                                writer.commit()
                        return proof
                    from contextlib import contextmanager
                    from packages.application import fbs_accounting_runtime as accounting
                    in_ack=[];book_loads=[];original_ack=h.PolicyHistory._acknowledge_verified_native;original_books=h.prepared_book_lineage;original_load=accounting.load
                    def tracked_load(*args,**kwargs):
                        if in_ack:
                            self.assertIn('connection',kwargs)
                            with closing(sqlite3.connect(runtime.db_path,timeout=0)) as contender:contender.execute('BEGIN IMMEDIATE');contender.rollback()
                            book_loads.append(kwargs['version'])
                        return original_load(*args,**kwargs)
                    @contextmanager
                    def books(*args,**kwargs):
                        with original_books(*args,**kwargs) as prepared:
                            if in_ack and race=='book':
                                with sqlite3.connect(accounting.path(runtime.runtime_dir)) as foreign:foreign.execute("UPDATE accounting_current SET version='foreign-after-preparation'")
                            yield prepared
                    def native_ack(receipt,proof):
                        in_ack.append(True)
                        try:return original_ack(receipt,proof)
                        finally:in_ack.clear()
                    with patch.object(worker,'_invoke',side_effect=diagnostic),patch.object(ack,'consume_policy_history_ack',side_effect=consume_then_race),patch.object(h.PolicyHistory,'_acknowledge_verified_native',native_ack),patch.object(h,'prepared_book_lineage',books),patch.object(accounting,'load',tracked_load):
                        if race:
                            with self.assertRaisesRegex(ValueError,'source_inputs_changed|dated_ready_changed|dated_book_observer_changed'):worker.complete(now,backfill_dates=backfill,policy_receipt=receipt,total_seconds=300)
                        else:result=worker.complete(now,backfill_dates=backfill,policy_receipt=receipt,total_seconds=300)
                    if bound_book:self.assertEqual(len(book_loads),1)
                    self.assertIsNone(worker._process)
                    if not race:self.assertGreater(result['portions'],1)
            operation=op.read(runtime.db_path,receipt.manifest['command_id'],actor='operator')
            if race:
                self.assertNotEqual(operation['state'],'completed',operation)
                self.assertIsNone(operation['processing_receipt']['publication'])
                self.assertNotIn('policy_history_ack',receipt._read()[1]);return
            self.assertEqual(operation['state'],'completed',operation)
            proof=operation['processing_receipt']['publication'];self.assertEqual(set(proof['native_history']),set(receipt.dates))
            store=HistoryStore(root/'history');edition=store.edition()
            self.assertTrue(all(proof['native_history'][d]['object_digest']==edition['days'][d] for d in receipt.dates))
            with self.assertRaisesRegex(HistoryDelegationError,'not_supervised'):receipt._acknowledge_verified_native(proof)

if __name__=='__main__':unittest.main()
