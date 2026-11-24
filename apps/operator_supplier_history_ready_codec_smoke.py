"""Default bounded Ready codec and native recovery of legacy plain operands."""
from contextlib import contextmanager
from copy import deepcopy
import base64,hashlib,json,sqlite3,sys,unittest,zlib
import weakref
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps import operator_supplier_history_smoke as fixtures
from packages.application import operator_supplier_history as h,ready_publication as ready
from packages.application.fbs_snapshot_cost import canonical


def legacy_manifest(manifest):
    """Recreate the prior persisted wire shape without a production opt-out."""
    manifest=deepcopy(manifest)
    for target in manifest['targets']:
        target['expected']['plan_json']=h._ready_json(target['expected']['plan_json'])
        target['after_json']=h._ready_json(target['after_json'])
    manifest.pop('storage_encoding',None)
    manifest['manifest_digest']=h._large_fingerprint({k:v for k,v in manifest.items() if k!='manifest_digest'})
    h._verify(manifest)
    return manifest


@contextmanager
def legacy_preparations():
    prepare=h._prepare_publication
    with patch.object(h,'_prepare_publication',side_effect=lambda *args,**kwargs:legacy_manifest(prepare(*args,**kwargs))):
        yield


class CodecTests(unittest.TestCase):
    def test_exact_utf8_and_json_bytes_roundtrip(self):
        text=canonical({'x':'Кириллица «» \\"\n 😀'*2000,'q':['0.10000000000000000000001',None,True]})
        packed=h._pack_ready_json(text)
        self.assertEqual(h._ready_json(json.loads(canonical(packed))),text)
        self.assertEqual(packed['sha256'],ready.digest(text))
        self.assertEqual(packed['utf8_bytes'],len(text.encode()))
        self.assertLess(len(canonical(packed).encode()),len(text.encode())//5)
        self.assertEqual(h._ready_json(text),text);self.assertIsNone(h._ready_json(None))

    def test_corrupt_truncated_trailing_and_inflate_limits_reject(self):
        text='Я'*500;packed=h._pack_ready_json(text)
        variants=[]
        for key,value in [('utf8_bytes',999),('utf8_bytes',1001),('utf8_bytes',True),
                          ('sha256','sha256:'+'0'*64),('encoding','unknown'),('payload','!!!')]:
            variants.append({**packed,key:value})
        compressed=base64.b64decode(packed['payload'])
        for body in (compressed[:-1],compressed+b'trailing',compressed+compressed):
            variants.append({**packed,'payload':base64.b64encode(body).decode()})
        for value in variants:
            with self.subTest(value=value),self.assertRaises(ValueError):h._ready_json(value)
        with patch.object(h,'MAX_READY_SOURCE_BYTES',999),self.assertRaisesRegex(ValueError,'size_limit'):
            h._pack_ready_json(text)
        with patch.object(h,'MAX_READY_SOURCE_BYTES',999),self.assertRaisesRegex(ValueError,'size_limit'):
            h._ready_json(packed)
        # A tiny declared length prevents allocation of the much larger output.
        bomb={**packed,'utf8_bytes':1,'payload':base64.b64encode(zlib.compress(b'x'*1000000)).decode()}
        with self.assertRaisesRegex(ValueError,'compression_invalid'):h._ready_json(bomb)
        malformed={**packed,'utf8_bytes':1,'payload':base64.b64encode(zlib.compress(b'\xff')).decode(),
                   'sha256':'sha256:'+hashlib.sha256(b'\xff').hexdigest()}
        with self.assertRaises(ValueError):h._ready_json(malformed)


class NativeCodecTests(unittest.TestCase):
    def fixture(self):
        case=fixtures.Tests();case.setUp();self.addCleanup(case.doCleanups);return case

    def test_native_byte_parity_publish_cold_read_retry_and_membership(self):
        case=self.fixture();packed=case.prepare();legacy=legacy_manifest(packed)
        self.assertEqual(packed['storage_encoding'],h.READY_JSON_ENCODING)
        rebuilt,parameters=h._targets(case.runtime,packed['candidate'],now=fixtures.MOMENT,ready_encoding=None)
        self.assertEqual(rebuilt,legacy['targets']);self.assertEqual(parameters,legacy['parameter_proof'])
        self.assertEqual(packed['candidate'],legacy['candidate'])
        for original,stored in zip(legacy['targets'],packed['targets']):
            restored={**stored,'expected':{**stored['expected'],'plan_json':h._ready_json(stored['expected']['plan_json'])},
                      'after_json':h._ready_json(stored['after_json'])}
            self.assertEqual(restored,original)
        sizes={'legacy':len(canonical(legacy).encode()),'packed':len(canonical(packed).encode())}
        print('fixture_publication_bytes='+canonical(sizes))
        self.assertLess(sizes['packed'],sizes['legacy'])
        receipt=case.publish(json.loads(canonical(packed)))
        cold=h.SupplierHistory(case.runtime,packed['operation_id'],packed['attempt_id'])
        self.assertEqual(cold.binding(),receipt.binding())
        self.assertEqual(cold.validate_sources_readonly(now=fixtures.MOMENT),receipt.validate_sources_readonly(now=fixtures.MOMENT))
        self.assertEqual(case.publish(packed).binding(),receipt.binding())
        with ready.readonly(case.runtime.db_path) as conn:
            self.assertEqual(json.loads(h.publication_for(conn,packed['source_ref'])['inputs_json']),packed)
        corrupt=deepcopy(packed);corrupt['targets'][0]['after_json']['sha256']='sha256:'+'0'*64
        corrupt['manifest_digest']=h._large_fingerprint({k:v for k,v in corrupt.items() if k!='manifest_digest'})
        with self.assertRaisesRegex(ValueError,'digest_changed'):case.publish(corrupt)
        # The legacy shape remains fully readable, including after_json bytes.
        h._verify(legacy)

    def test_legacy_publish_cold_read_retry_and_membership(self):
        case=self.fixture();legacy=legacy_manifest(case.prepare())
        receipt=case.publish(legacy)
        cold=h.SupplierHistory(case.runtime,legacy['operation_id'],legacy['attempt_id'])
        self.assertEqual(cold.binding(),receipt.binding())
        self.assertEqual(cold.validate_sources_readonly(now=fixtures.MOMENT),receipt.validate_sources_readonly(now=fixtures.MOMENT))
        self.assertEqual(case.publish(json.loads(canonical(legacy))).binding(),receipt.binding())
        with ready.readonly(case.runtime.db_path) as conn:
            self.assertEqual(json.loads(h.publication_for(conn,legacy['source_ref'])['inputs_json']),legacy)

    def test_legacy_intent_response_loss_resume(self):
        case=fixtures.ReplanTests();self.addCleanup(case.doCleanups)
        with legacy_preparations():
            case.test_same_prepared_attempt_clock_and_lost_response_resume_exactly()

    def test_legacy_book_ghost_uses_original_saved_stages(self):
        case=fixtures.ReplanTests();self.addCleanup(case.doCleanups)
        with legacy_preparations():
            case.test_prepared_after_own_book_uses_original_stages()

    def test_legacy_completed_obligation_retirement(self):
        case=fixtures.ReplanTests();self.addCleanup(case.doCleanups)
        with legacy_preparations():
            case.test_actual_followup_keeps_root_new_attempt_all_original_dates()

    def test_legacy_book_ghost_to_packed_recovery(self):
        case=fixtures.CohortTests();self.addCleanup(case.doCleanups);native=case.fixture()
        old=legacy_manifest(native.prepare())
        def crash(point):
            if point=='after_book':raise RuntimeError('legacy book ghost')
        with self.assertRaisesRegex(RuntimeError,'legacy book ghost'):native.publish(old,inject=crash)
        new,_,_=fixtures.ReplanTests.followup(native,old)
        self.assertTrue(new['candidate']['recovery_inputs'])
        self.assertNotIn('storage_encoding',old)
        self.assertEqual(new['storage_encoding'],h.READY_JSON_ENCODING)
        native.publish(new)


class ObligationReferenceTests(unittest.TestCase):
    def fixture(self):
        manifest=dict(contract=h.CONTRACT,storage_encoding=h.READY_JSON_ENCODING,
            operation_id='supplier-history:source',attempt_id='old',source_ref={'operation_id':'source','source_digest':'sha256:source'},
            candidate=dict(code_authority=h.RETAINED_CODE_AUTHORITY,effect_dates=[fixtures.FIRST],
                functional={'business_date':fixtures.FIRST},candidate_book={},cohort_refs=[{'operation_id':'source','source_digest':'sha256:source'}]),
            targets=[dict(dates=[fixtures.FIRST],expected=asdict(ready.ExpectedReady('fixture',fixtures.FIRST,None)),after_json=h._pack_ready_json('{}'))],
            expected_book='before',book_operation_id='old-book',after_book=h._large_fingerprint({}),padding='Ж😀'*200)
        manifest['manifest_digest']=h._large_fingerprint(manifest)
        text=canonical(manifest)
        row=dict(operation_id=manifest['operation_id'],attempt_id='old',kind=h.CONTRACT,state='prepared',
            expected_book='before',book_operation_id='old-book',book_version=None,after_digest=None,
            inputs_json=text,diagnostics_json='{}',created_at='fixture')
        conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
        conn.execute('CREATE TABLE sheet_vitrina_v1_ready_publications('+','.join(row)+')')
        conn.execute('INSERT INTO sheet_vitrina_v1_ready_publications VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        item={key:row[key] for key in h.OBLIGATION_FIELDS}
        item.update(inputs_ref=h.retained_inputs_ref(text),manifest_digest=manifest['manifest_digest'],dates=[fixtures.FIRST],diagnostics_json='{}')
        return conn,row,item,manifest

    def test_reference_rehydrates_exact_bytes_and_legacy_shape(self):
        conn,row,item,manifest=self.fixture()
        self.assertEqual(h.load_obligation(conn,item),manifest)
        self.assertLess(len(canonical(item)),len(row['inputs_json'])//2)
        legacy={k:v for k,v in item.items() if k!='inputs_ref'};legacy['inputs_json']=row['inputs_json']
        self.assertEqual(h.load_obligation(conn,legacy),manifest)
        h.check_obligations(conn,{'candidate':{'obligations':[item]}})
        with self.assertRaisesRegex(ValueError,'formula_changed'):h._verify(manifest)
        h._verify(manifest,retained=True)
        for name in manifest['candidate']['code_authority']:
            corrupt=deepcopy(manifest);corrupt['candidate']['code_authority'][name]='0'*64
            corrupt['manifest_digest']=h._large_fingerprint({k:v for k,v in corrupt.items() if k!='manifest_digest'})
            with self.subTest(name=name),self.assertRaisesRegex(ValueError,'formula_changed'):h._verify(corrupt,retained=True)

    def test_body_identity_pin_limit_and_missing_source_fail_closed(self):
        conn,row,item,manifest=self.fixture()
        variants=[]
        for key,value in [('sha256','sha256:'+'0'*64),('utf8_bytes',item['inputs_ref']['utf8_bytes']+1),('schema','unknown')]:
            value_item=deepcopy(item);value_item['inputs_ref'][key]=value;variants.append(value_item)
        for key in h.OBLIGATION_FIELDS:
            value_item=deepcopy(item);value_item[key]='foreign';variants.append(value_item)
        ambiguous=deepcopy(item);ambiguous['inputs_json']=row['inputs_json'];variants.append(ambiguous)
        for value_item in variants:
            with self.subTest(value=value_item),self.assertRaises(ValueError):h.load_obligation(conn,value_item)
        with patch.object(h,'MAX_PUBLICATION_BYTES',item['inputs_ref']['utf8_bytes']-1),self.assertRaises(ValueError):
            h.load_obligation(conn,item)
        changed=row['inputs_json'].replace('Ж','Ш')
        self.assertEqual(len(changed.encode()),len(row['inputs_json'].encode()))
        conn.execute('UPDATE sheet_vitrina_v1_ready_publications SET inputs_json=?',(changed,))
        with self.assertRaisesRegex(ValueError,'prior_attempt_changed'):h.check_obligations(conn,{'candidate':{'obligations':[item]}})
        conn.execute('DELETE FROM sheet_vitrina_v1_ready_publications')
        with self.assertRaisesRegex(ValueError,'prior_attempt_changed'):h.load_obligation(conn,item)

    def test_discovery_and_pinned_headers_refuse_oversize_before_body_fetch(self):
        conn,row,item,manifest=self.fixture();fetched=[];body=row['inputs_json'].encode()
        def text(raw):
            if raw==body:fetched.append('body')
            return raw.decode()
        conn.text_factory=text
        with patch.object(h,'MAX_PUBLICATION_BYTES',len(body)-1):
            with self.assertRaisesRegex(ValueError,'prior_attempt_size_limit'):h.publication_for(conn,manifest['source_ref'])
            for pinned in (None,[{'operation_id':item['operation_id'],'attempt_id':item['attempt_id']}]):
                with patch.object(h.candidate.processing,'ref',return_value=manifest['source_ref']), \
                     patch.object(h.candidate,'attempt_identity',return_value='new'), \
                     self.subTest(pinned=bool(pinned)),self.assertRaisesRegex(ValueError,'prior_attempt_size_limit'):
                    h.candidate.retained_obligations(conn,[{'operation':{}}],runtime=None,pinned=pinned)
        self.assertEqual(fetched,[])

    def test_attempt_count_precedes_body_reads_and_retired_scan_releases_each_body(self):
        conn,row,item,manifest=self.fixture()
        conn.execute('DELETE FROM sheet_vitrina_v1_ready_publications')
        for number in range(33):
            saved={**row,'operation_id':'scan:'+str(number),'attempt_id':str(number)}
            conn.execute('INSERT INTO sheet_vitrina_v1_ready_publications VALUES('+','.join('?' for _ in saved)+')',tuple(saved.values()))
        refs=[];body=row['inputs_json'].encode()
        class TrackedBody(str):pass
        def text(raw):
            if raw!=body:return raw.decode()
            self.assertTrue(all(ref() is None for ref in refs),'retired scan retained the previous full body')
            value=TrackedBody(raw.decode());refs.append(weakref.ref(value));return value
        conn.text_factory=text
        # This test isolates transport bounds. Native retirement proofs have
        # their own real publish/replan/retirement and corruption tests above.
        with patch.object(h,'verified_retirement',lambda conn,row:True), \
             patch.object(h.candidate.processing,'ref',return_value=manifest['source_ref']), \
             patch.object(h.candidate,'attempt_identity',return_value='new'):
            with self.assertRaisesRegex(ValueError,'attempt_scope_limit'):h.publication_for(conn,manifest['source_ref'])
            self.assertEqual(refs,[])
            with self.assertRaisesRegex(ValueError,'attempt_scope_limit'):
                h.candidate.retained_obligations(conn,[{'operation':{}}],runtime=None)
            self.assertEqual(refs,[])
            conn.execute("DELETE FROM sheet_vitrina_v1_ready_publications WHERE attempt_id='32'")
            self.assertIsNone(h.publication_for(conn,manifest['source_ref']))
            self.assertEqual(len(refs),32);self.assertTrue(all(ref() is None for ref in refs));refs.clear()
            self.assertEqual(h.candidate.retained_obligations(conn,[{'operation':{}}],runtime=None),[])
            self.assertEqual(len(refs),32);self.assertTrue(all(ref() is None for ref in refs))

    def test_known_previous_algorithm_ghost_replans_and_retires_without_embedded_inputs(self):
        helper=fixtures.ReplanTests();self.addCleanup(helper.doCleanups);case=helper.fixture()
        def crash(point):
            if point=='after_book':raise RuntimeError('old algorithm book ghost')
        with patch.object(h.candidate,'code_authority',return_value=h.RETAINED_CODE_AUTHORITY):
            old=case.prepare()
            with self.assertRaisesRegex(RuntimeError,'old algorithm'):case.publish(old,inject=crash)
        new,_,_=helper.followup(case,old)
        self.assertTrue(new['candidate']['recovery_inputs'])
        for item in new['candidate']['obligations']:
            self.assertIn('inputs_ref',item);self.assertNotIn('inputs_json',item)
        for day in old['candidate']['effect_dates']:
            self.assertEqual(new['candidate']['editions'][day]['saved'],old['candidate']['editions'][day]['saved'])
        self.assertLess(h._publication_json_size(new),h.MAX_PUBLICATION_BYTES)
        receipt=case.publish(new)
        self.assertEqual(set(receipt.dates),set(old['candidate']['effect_dates']))
        with ready.readonly(case.runtime.db_path) as conn:
            row=conn.execute('SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?',
                (old['operation_id'],old['attempt_id'])).fetchone()
            self.assertTrue(h.verified_retirement(conn,row))


if __name__=='__main__':unittest.main()
