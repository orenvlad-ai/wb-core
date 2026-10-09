"""Independent pinned OLD helper parity on disposable native dated inputs."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json,sys,unittest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from packages.application import historical_dated_inputs as new, ready_publication as ready
from packages.application.registry_upload_db_backed_runtime import _connect
from apps import operator_supplier_history_smoke as native_fixture
DAY,FIRST=native_fixture.DAY,native_fixture.FIRST


class Tests(unittest.TestCase):
    def setUp(self):
        fixture=json.loads((ROOT/'apps/fixtures/historical_dated_inputs_67138346.json').read_text())
        self.assertEqual(fixture['source_commit'],'671383466c81246bd19dd73384448bde48cc1f00')
        self.old={'json':json,'MAX_BYTES':160*1024**2,'op':SimpleNamespace(canonical=lambda value:json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')))}
        exec("def require(condition,code):\n    if not condition:raise ValueError('policy_history_'+code)\n"+fixture['helper_source'],self.old)
        self.fixture=native_fixture.Tests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.runtime=self.fixture.runtime

    def parity(self,name,*args):
        def observe(call):
            try:return ('value',call(*args))
            except Exception as error:return ('error',type(error).__name__,str(error))
        old=observe(self.old[name]);actual=observe(getattr(new,name));self.assertEqual(actual,old)
        return actual

    def test_native_ready_generations_missing_dates_and_byte_bound(self):
        with ready.readonly(self.runtime.db_path) as conn:
            observed=self.parity('selected',conn,[FIRST,DAY],DAY)
            self.assertEqual(observed[0],'value');self.assertTrue(observed[1])
            self.assertEqual(self.parity('selected',conn,['1900-01-01'],DAY)[0],'error')
        with _connect(self.runtime.db_path) as conn:
            row=dict(conn.execute('SELECT * FROM sheet_vitrina_v1_ready_snapshots ORDER BY refreshed_at DESC LIMIT 1').fetchone())
            row['as_of_date']='2026-07-23';row['refreshed_at']='2026-07-26T12:00:00Z'
            conn.execute('INSERT INTO sheet_vitrina_v1_ready_snapshots('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()));conn.commit()
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(self.parity('selected',conn,[FIRST,DAY],DAY)[0],'value')
            self.old['MAX_BYTES']=1
            original=new.MAX_BYTES;new.MAX_BYTES=1
            try:self.assertEqual(self.parity('selected',conn,[FIRST],DAY),('error','ValueError','policy_history_source_size_limit'))
            finally:new.MAX_BYTES=original
        with _connect(self.runtime.db_path) as conn:
            conn.execute('DELETE FROM registry_upload_current_state');conn.commit()
        self.old['MAX_BYTES']=160*1024**2
        with ready.readonly(self.runtime.db_path) as conn:
            self.assertEqual(self.parity('selected',conn,[FIRST],DAY),('error','ValueError','policy_history_active_bundle_missing'))

    def test_dated_slices_and_independent_immutable_book_lineage(self):
        with ready.readonly(self.runtime.db_path) as conn:
            row=self.old['selected'](conn,[FIRST,DAY],DAY)[0][0]
        encoded=row['plan_json'];payload=json.loads(encoded)
        for day in (FIRST,DAY):
            self.assertEqual(self.parity('dated_slice',encoded,day)[0],'value')
            lineage=self.parity('book_lineage',self.runtime,encoded,day)
            self.assertEqual(lineage[0],'value');self.assertIsNotNone(lineage[1])
        self.assertEqual(self.parity('dated_slice',encoded,'1900-01-01'),('error','ValueError','policy_history_dated_column_missing'))
        for mutation in ('target','book','presentation','source','date'):
            changed=deepcopy(payload);bound=changed['metadata']['fbs_accounting_bindings'][FIRST]
            if mutation=='target':changed['metadata'].setdefault('fbs_accounting_targets',{})[FIRST]='foreign'
            else:bound[{'book':'book_version','presentation':'presentation_version','source':'source','date':'date'}[mutation]]='foreign'
            self.assertEqual(self.parity('book_lineage',self.runtime,new.canonical(changed),FIRST)[0],'error')
        unbound=deepcopy(payload);unbound['metadata']['fbs_accounting_bindings'].pop(FIRST)
        self.assertEqual(self.parity('book_lineage',self.runtime,new.canonical(unbound),FIRST),('value',None))

    def test_authority_binds_actual_helper_bytes(self):
        import hashlib
        from packages.application.operator_supplier_history_candidate import code_authority
        self.assertEqual(code_authority()['historical_dated_inputs.py'],hashlib.sha256(Path(new.__file__).read_bytes()).hexdigest())


if __name__=='__main__':unittest.main()
