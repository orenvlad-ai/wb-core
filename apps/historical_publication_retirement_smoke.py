"""Diagnostics-only retirement, exact queue proof and no historical success."""
from contextlib import closing, contextmanager, nullcontext
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packages.application import historical_publication_retirement as retirement
from packages.application import fbs_accounting_historical_history as history
from packages.application import ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical, fingerprint
from packages.application.fbs_accounting_apply import FbsAccountingAdapter
from apps.production_apply_launcher import execute


class Retirement(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.db = self.root/'operational.sqlite3'
        self.operation = 'fixture-retire-unconfirmed'
        self.parent = 'wbc-583-finalization-preview-20261103'
        self.child = self.parent + ':publication'
        self.recipe = dict(contract='fbs_temporary_allocation_effect_intent_v1', operation_id=self.parent, attempt_id='1',
            expected_book='before', publication_operation_id=self.child, authority={'fixture':'source'}, unit={'fixture':'native'})
        self.recipe['recipe_digest'] = fingerprint(self.recipe)
        self.manifest = dict(contract=retirement.TEMPORARY_CONTRACT, operation_id=self.child, attempt_id='1', expected_book='before',
            after_book='after', book_operation_id='book-op', plan={'effect_binding':{key:self.recipe[key] for key in ('operation_id','attempt_id','recipe_digest')}},
            # This unselected text exercises skipped escaped strings and containers.
            candidate={'image': ('escape\\\"}[[]{}😀'*7000), 'nested':[None,True,{'z':42}]})
        self.manifest['manifest_digest'] = fingerprint(self.manifest)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'CREATE TABLE {retirement.TABLE}(operation_id,attempt_id,kind,state,book_version,expected_book,book_operation_id,inputs_json,diagnostics_json,created_at)')
            conn.execute(f'INSERT INTO {retirement.TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.parent,'1',self.recipe['contract'],'prepared',None,'before',None,canonical(self.recipe),'{}','2026-11-03'))
            conn.execute(f'INSERT INTO {retirement.TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
                (self.child,'1',self.manifest['contract'],'complete','after','before','book-op',canonical(self.manifest),'{}','2026-11-03'))
            conn.execute('CREATE TABLE preserved_business(quantity,amount)');conn.execute("INSERT INTO preserved_business VALUES(5368,'666175.50')")
        self.request = dict(action=retirement.ACTION, publication_operation_id=self.child, publication_attempt_id='1',
            publication_inputs_sha256=ready.digest(canonical(self.manifest)), parent_inputs_sha256=ready.digest(canonical(self.recipe)),
            publication_manifest_digest=self.manifest['manifest_digest'],recipe_digest=self.recipe['recipe_digest'],
            approval_reference='user: leave history unconfirmed; retire only technical queue block')
        self.enterContext(patch('packages.application.storage_registry.StoreRegistry.resolve',return_value=self.db))
        self.enterContext(patch('packages.application.fbs_accounting_apply.target',return_value=self.root))
        self.enterContext(patch('packages.application.fbs_historical_fbo_action._held',side_effect=lambda *a:nullcontext({'window_id':'held'})))
        self.enterContext(patch.object(ready,'check_pinned_authority'))
        self.native = self.enterContext(patch('packages.application.fbs_temporary_allocation_action._verify_unit',return_value=True))
        self.adapter = FbsAccountingAdapter()

    def rows(self):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            return conn.execute(f'SELECT * FROM {retirement.TABLE} ORDER BY rowid').fetchall(),conn.execute('SELECT * FROM preserved_business').fetchall()

    def update(self, **fields):
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'UPDATE {retirement.TABLE} SET '+','.join(key+'=?' for key in fields)+' WHERE operation_id=?', (*fields.values(),self.child))

    def apply(self):
        preview = self.adapter.preview(self.request,self.operation)
        return execute(action='apply',adapter_name='fixture',operation_id=self.operation,request=self.request,
            expected_prestate=preview['prestate_sha256'],expected_candidate=preview['candidate_sha256'],adapters={'fixture':self.adapter})

    def test_registered_preview_submit_readback_preserves_everything_except_child_diagnostics(self):
        before,business = self.rows()
        preview = self.adapter.preview(self.request,self.operation)
        self.assertEqual(self.rows(),(before,business))
        self.assertEqual(self.adapter.readback(self.request,self.operation)['state'],'not_submitted')
        result = self.apply()
        self.assertEqual(result['state'],'applied')
        self.assertEqual(result['readback']['history_status'],'retired_unconfirmed')
        after,after_business = self.rows()
        self.assertEqual(before[0],after[0]);self.assertEqual(business,after_business)
        self.assertEqual(before[1][:8]+before[1][9:],after[1][:8]+after[1][9:])
        diagnostics = json.loads(after[1][8]);proof=diagnostics[retirement.KEY]
        self.assertFalse(proof['history_confirmed']);self.assertFalse(result['readback']['scope_complete'])
        self.assertNotIn('temporary_allocation_scope_complete',diagnostics);self.assertNotIn('historical_history_ack',diagnostics)
        backup = Path(preview['recovery']['original_diagnostics'])
        self.assertEqual(json.loads(backup.read_text())['diagnostics_json'],'{}')
        self.assertEqual(backup.stat().st_mode & 0o777,0o600)
        for _ in range(2): self.assertEqual(self.adapter.readback(self.request,self.operation)['state'],'applied')
        with self.assertRaisesRegex(ValueError,'already_decided_readback_only'):self.adapter.apply(self.request,self.operation,preview)
        self.assertEqual(self.rows(),(after,after_business))
        self.assertIsNone(history.pending_receipt(SimpleNamespace(db_path=self.db)))
        # Explicit receipt creation cannot revive an uncertain old tail.
        with self.assertRaisesRegex(ValueError,'publication_retired'):history.HistoricalReceipt(SimpleNamespace(db_path=self.db),self.child,'1')

    def test_failed_commit_is_readback_only_and_keeps_old_tail_claim(self):
        claim = self.root/'historical-receipt-completions'/'claims'/'old-tail.json'
        claim.parent.mkdir(parents=True); claim.write_text('submitted/outcome_unknown; never resend')
        book = self.root/'preserved-book'; book.write_text('immutable existing accounting bytes')
        before = self.rows()
        transaction = retirement.sql.transaction
        @contextmanager
        def fail_commit(conn, **kwargs):
            with transaction(conn, **kwargs):
                yield conn
                raise sqlite3.OperationalError('fixture commit failure')
        with patch.object(retirement.sql, 'transaction', fail_commit):
            result = self.apply()
        self.assertEqual(result['state'],'ambiguous')
        self.assertEqual(result['reason'],'submit-ambiguous-readback-only')
        self.assertEqual(self.adapter.readback(self.request,self.operation)['state'],'ambiguous')
        self.assertEqual(self.rows(),before)
        self.assertEqual(book.read_text(),'immutable existing accounting bytes')
        self.assertEqual(claim.read_text(),'submitted/outcome_unknown; never resend')

    def test_source_native_scope_and_cas_drift_fail_without_writes(self):
        before = self.rows()
        for fields in ({'state':'prepared'},{'kind':retirement.FBO_CONTRACT},{'book_version':'other'},
                       {'diagnostics_json':canonical({'temporary_allocation_scope_complete':True})},
                       {'diagnostics_json':canonical({'historical_history_ack':{'native':'already-acknowledged'}})}):
            saved = self.rows()[0][1]
            self.update(**fields)
            with self.subTest(fields=fields),self.assertRaises(ValueError):self.adapter.preview(self.request,self.operation)
            self.update(**{key:saved[('operation_id','attempt_id','kind','state','book_version','expected_book','book_operation_id','inputs_json','diagnostics_json','created_at').index(key)] for key in fields})
        self.native.return_value=False
        with self.assertRaisesRegex(ValueError,'native_source_changed'):self.adapter.preview(self.request,self.operation)
        self.native.return_value=True
        preview=self.adapter.preview(self.request,self.operation)
        self.update(diagnostics_json='{"unexpected":"drift"}')
        with self.assertRaisesRegex(ValueError,'compare_failed'):self.adapter.apply(self.request,self.operation,preview)
        self.assertFalse(Path(preview['recovery']['original_diagnostics']).exists())
        self.update(diagnostics_json='{}');self.assertEqual(self.rows(),before)
        with closing(sqlite3.connect(self.db)) as conn, conn:conn.execute(f'UPDATE {retirement.TABLE} SET state=\'complete\' WHERE operation_id=?',(self.parent,))
        with self.assertRaisesRegex(ValueError,'prepared_parent'):self.adapter.preview(self.request,self.operation)

    def test_only_valid_exact_retirement_exempts_queue_and_leaves_other_child_pending(self):
        self.apply()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'INSERT INTO {retirement.TABLE} VALUES(?,?,?,?,?,?,?,?,?,?)',
                ('next-fbo-child','1',retirement.FBO_CONTRACT,'complete','book',None,None,'{}','{}','later'))
        with patch.object(history,'HistoricalReceipt',side_effect=lambda runtime,operation,attempt:(operation,attempt)):
            self.assertEqual(history.pending_receipt(SimpleNamespace(db_path=self.db)),('next-fbo-child','1'))
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(f'UPDATE {retirement.TABLE} SET diagnostics_json=? WHERE operation_id=\'next-fbo-child\'',(self.rows()[0][1][8],))
            # Copying an exact proof to another child does not retire that child.
            self.assertEqual(history.pending_receipt(SimpleNamespace(db_path=self.db)),('next-fbo-child','1'))
            with closing(sqlite3.connect(self.db)) as conn, conn:
                conn.execute(f'UPDATE {retirement.TABLE} SET diagnostics_json=\'{{}}\' WHERE operation_id=\'next-fbo-child\'')
            original=self.rows()[0][1]
            invalid=json.loads(original[8]);invalid[retirement.KEY]['history_confirmed']=True
            self.update(diagnostics_json=canonical(invalid))
            with self.assertRaisesRegex(ValueError,'multiple_unfinished'):history.pending_receipt(SimpleNamespace(db_path=self.db))
            self.update(diagnostics_json=original[8])
            # Same byte size and still JSON: only the input hash detects mutation.
            self.update(inputs_json=original[7].replace('book-op','book-xx'))
            with self.assertRaisesRegex(ValueError,'multiple_unfinished'):history.pending_receipt(SimpleNamespace(db_path=self.db))
            self.update(inputs_json=original[7])
            with closing(sqlite3.connect(self.db)) as conn, conn:conn.execute(f'UPDATE {retirement.TABLE} SET diagnostics_json=\'{{"parent":"drift"}}\' WHERE operation_id=?',(self.parent,))
            with self.assertRaisesRegex(ValueError,'multiple_unfinished'):history.pending_receipt(SimpleNamespace(db_path=self.db))

    def test_fbo_retirement_checks_native_and_exact_fee_sources(self):
        from packages.application import fbs_historical_fbo_publication as fbo
        recipe={**self.recipe,'contract':fbo.EFFECT_CONTRACT,'native_plan':{'fixture':'native-plan'},'fee_requests':[],'publication_attempt_id':'1'}
        recipe.pop('recipe_digest');recipe['recipe_digest']=fingerprint(recipe)
        manifest={**self.manifest,'contract':retirement.FBO_CONTRACT,
            'plan':{'effect_binding':{key:recipe[key] for key in ('operation_id','attempt_id','recipe_digest')},'native_plan':recipe['native_plan'],'fee_scope':{'exact':'fees'}}}
        manifest.pop('manifest_digest');manifest['manifest_digest']=fingerprint(manifest)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'UPDATE {retirement.TABLE} SET kind=?,inputs_json=? WHERE operation_id=?',(recipe['contract'],canonical(recipe),self.parent))
        self.update(kind=manifest['contract'],inputs_json=canonical(manifest))
        self.request.update(parent_inputs_sha256=ready.digest(canonical(recipe)),publication_inputs_sha256=ready.digest(canonical(manifest)),
            publication_manifest_digest=manifest['manifest_digest'],recipe_digest=recipe['recipe_digest'])
        with patch('packages.application.fbs_historical_fbo_native._verify_existing',return_value=True),patch.object(fbo,'capture_fee_scope',return_value={'wrong':'fees'}):
            with self.assertRaisesRegex(ValueError,'native_source_changed'):self.adapter.preview(self.request,self.operation)
        with patch('packages.application.fbs_historical_fbo_native._verify_existing',return_value=True),patch.object(fbo,'capture_fee_scope',return_value={'exact':'fees'}):
            self.assertEqual(self.apply()['state'],'applied')
        self.assertIsNone(history.pending_receipt(SimpleNamespace(db_path=self.db)))


class Projection(unittest.TestCase):
    def test_chunk_boundaries_escaped_literals_and_no_large_decode(self):
        source={'a':'padding'*15000+'\\\"{}[\\雪😀','plan':{'ignore':{'x':['\"']*10000},'effect_binding':{'operation_id':'exact','attempt_id':'1'}},'z':'skip'*100000}
        encoded=canonical(source).encode()
        reader=retirement._Projection(io.BytesIO(encoded))
        with patch.object(retirement.json,'loads',wraps=json.loads) as decoder:
            selected=reader.object({'plan':{'effect_binding':None}})
        self.assertEqual(selected,{'plan':{'effect_binding':source['plan']['effect_binding']}})
        self.assertEqual('sha256:'+reader.hashed.hexdigest(),'sha256:'+hashlib.sha256(encoded).hexdigest())
        self.assertTrue(all(len(call.args[0])<100 for call in decoder.call_args_list))
        self.assertFalse(reader.peek())
        with self.assertRaisesRegex(ValueError,'duplicate_key'):
            retirement._Projection(io.BytesIO(b'{"plan":{},"plan":{}}')).object({'plan':{}})
        with patch.object(retirement,'MAX_SMALL',20):
            with self.assertRaisesRegex(ValueError,'projection_size_limit'):
                retirement._Projection(io.BytesIO(b'{"selected":"'+b'x'*30+b'"}')).object({'selected':None})


if __name__=='__main__': unittest.main()
