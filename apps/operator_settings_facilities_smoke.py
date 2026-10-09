"""Settings uses the existing native 06c authority, including restart and drain."""
import sys,sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from apps.operator_facility_mappings_http_smoke import setup,NOW
from apps.operator_facility_mappings_smoke import envelope
from packages.application.registry_upload_http_entrypoint import RegistryUploadHttpEntrypoint
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application import operator_facility_mappings as op
from packages.application.ff_pool_foundation import FACILITIES_TABLE


def main():
    with TemporaryDirectory(prefix='settings-facility-native-') as raw:
        rt,entry=setup(raw)
        preview=entry.handle_ff_pool_facility_create_preview_request({'request_id':'settings-preview-01','name':'Settings native','city':'City','active':False},actor='fixture')
        assert not preview.get('acceptance')
        body=envelope(request_id='settings-create-01',confirm=True,preview_request_id=preview['request_id'],preview_fingerprint=preview['preview_fingerprint'])
        saved=entry.handle_ff_pool_facility_create_confirm_request(preview['request_id'],preview_fingerprint=preview['preview_fingerprint'],actor='fixture',request_scope='settings-user',operator_payload=body)
        fid=saved['acceptance']['source_ref']['entity_id'];assert saved['acceptance']['durable_saved']
        restart=RegistryUploadHttpEntrypoint(runtime_dir=rt.runtime_dir,runtime=rt);restart.ff_pool_surface=entry.ff_pool_surface
        before=rt.db_path.read_bytes()
        with patch('packages.application.ff_pool_foundation.ensure_ff_pool_foundation_schema',side_effect=AssertionError('RO bootstrap')):
            assert restart.handle_ff_facility_operation_read(request_scope='settings-user',request_id=body['request_id'])==saved
        assert before==rt.db_path.read_bytes()
        source=entry.ff_pool_surface.facility_detail(fid)['facility']
        activate=envelope(request_id='settings-activate-01',active=True,expected_updated_at=source['updated_at'],expected_source_digest=source['operator_source_digest'])
        with patch.object(DenseFbsService,'_materialize',side_effect=AssertionError('form heavy work')):
            accepted=restart.handle_ff_pool_facility_update_request(fid,activate,actor='fixture',request_scope='settings-user')
        assert accepted['acceptance']['state']=='processing' and not entry.ff_pool_surface.facility_detail(fid)['facility']['active']
        assert op.read(rt.db_path,request_scope='foreign',request_id=activate['request_id'])['status']=='unknown'
        drain=DenseFbsService(db_path=rt.db_path,runtime_dir=rt.runtime_dir,timestamp_factory=lambda:NOW)
        assert drain.drain_facility_activations()['active']==1
        completed=restart.handle_ff_facility_operation_read(request_scope='settings-user',request_id=activate['request_id'])
        assert completed['acceptance']['processing']['published_active']
        assert completed['acceptance']['operation_id']==accepted['acceptance']['operation_id']
        assert restart.handle_ff_pool_facility_update_request(fid,activate,actor='fixture',request_scope='settings-user')==completed
        with sqlite3.connect(rt.db_path) as conn:assert conn.execute(f'SELECT COUNT(*) FROM {FACILITIES_TABLE}').fetchone()[0]==1
    print('Settings native06c source/restart/principal/no-heavy/dense exact completion: PASS')
if __name__=='__main__':main()
