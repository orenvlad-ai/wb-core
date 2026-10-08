"""Bounded native supplier completion cohorts and durable superseded outcomes."""
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.operator_supplier_processing_smoke import seed, prepare_source, edit
from packages.application import operator_supplier_processing as processing, operator_supplier_shipments as source
from packages.application.registry_upload_db_backed_runtime import _connect
from packages.application.business_data_heavy_admission import heavy_admitted
from packages.application.warehouse_functional_lock import warehouse_functional_job_lock


def main():
    with TemporaryDirectory(prefix='supplier-cohort-') as raw:
        rt = seed(raw)
        header, lines = prepare_source(rt)
        operations = [edit(rt, header, lines, 'bounded-source-'+str(i), 'revision-'+str(i))['acceptance']['operation_id'] for i in range(13)]
        with _connect(rt.db_path) as conn:
            conn.execute(f"INSERT INTO {processing.ATTEMPTS} VALUES(?,?,?,?)", (operations[-1], 'processing', 'supplier_exact_queue_pending', '0000'))
            conn.commit()
        visited = set()
        retired_times = {}
        with patch.object(processing, 'COHORT_LIMIT', 3), heavy_admitted(rt.runtime_dir, operation='fixture'), warehouse_functional_job_lock(rt.runtime_dir):
            for cycle in range(7):
                result = processing.reconcile(rt)
                assert len(result['operations']) <= 3, result
                if cycle == 0:
                    assert operations[-1] in {item['operation_id'] for item in result['operations']}
                for item in result['operations']:
                    identity = item['operation_id']
                    if item.get('terminal'):
                        assert identity != operations[-1]
                        assert identity not in visited, 'retired operation retried'
                        assert item['reason_code'] == processing.SUPERSEDED
                        visited.add(identity)
                with closing(source.readonly(rt.db_path)) as conn:
                    for row in conn.execute(f'SELECT * FROM {processing.ATTEMPTS} WHERE reason=?', (processing.SUPERSEDED,)):
                        if row['operation_id'] in retired_times:
                            assert retired_times[row['operation_id']] == row['checked_at']
                        retired_times[row['operation_id']] = row['checked_at']
            assert visited == set(operations[:-1]), visited
            assert operations[-1] not in visited
            pending = source.read_acceptance(rt.db_path, operations[-1], request_scope='alice-key')
            assert not pending['processing']['complete'] and not pending['processing'].get('terminal')
            old = source.read_acceptance(rt.db_path, operations[0], request_scope='alice-key')
            assert old['durable_saved'] and old['processing']['terminal'] and not old['processing']['complete']
            # A changed/invalid native pointer is not proven monotonic source.
            # Even though revision is greater, its fingerprint does not match
            # actual native source and must never retire this accepted action.
            with _connect(rt.db_path) as conn:
                conn.execute(f"UPDATE {source.intents.TABLE} SET revision=revision+1,source_fingerprint='invalid-unverified-source' WHERE shipment_id='source'")
                conn.commit()
            retry = processing.reconcile(rt)
            assert len(retry['operations']) == 1 and retry['operations'][0]['operation_id'] == operations[-1]
            assert retry['operations'][0]['state'] == 'processing' and not retry['operations'][0]['terminal']
            unverified = source.read_acceptance(rt.db_path, operations[-1], request_scope='alice-key')
            assert not unverified['processing'].get('terminal') and unverified['reason_code'] != 'source_superseded'
            with closing(source.readonly(rt.db_path)) as conn:
                attempt = processing.public_completion(conn, operations[-1])
                assert not attempt['terminal'] and attempt['reason_code'] == 'supplier_completion_source_changed'
    print('bounded stable cohort progresses past pending; exact newer-source CAS retires once; retained old GET; unverified newer pointer stays retry: OK')


if __name__ == '__main__':
    main()
