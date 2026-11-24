"""Default bounded Ready codec and native recovery of legacy plain operands."""
from contextlib import contextmanager
from copy import deepcopy
import base64,hashlib,json,sqlite3,sys,unittest,zlib
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
    prepare=h.prepare
    with patch.object(h,'prepare',side_effect=lambda *args,**kwargs:legacy_manifest(prepare(*args,**kwargs))):
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


if __name__=='__main__':unittest.main()
