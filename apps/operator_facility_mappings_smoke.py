#!/usr/bin/env python3
"""Exact native facility receipts, source CAS, staged activation and schema upgrade."""
import json,sqlite3,sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from packages.application import operator_facility_mappings as op
from packages.application.ff_pool_foundation import FACILITIES_TABLE,FACILITY_CHANGES_TABLE,_upgrade_facility_change_actions
from packages.application.ff_pool_surfaces import FfPoolSurface
from packages.application.ff_pool_dense_fbs import DenseFbsService
from packages.application.registry_upload_db_backed_runtime import RegistryUploadDbBackedRuntime
from apps.ff_pool_dense_fbs_smoke import _sku,_enable_writer,NOW


def fixture(root):
    runtime=RegistryUploadDbBackedRuntime(runtime_dir=root)
    runtime.save_nomenclature_item(_sku(101,updated_at=NOW))
    with sqlite3.connect(runtime.db_path) as conn:_enable_writer(conn);conn.commit()
    return runtime,FfPoolSurface(db_path=runtime.db_path,runtime_dir=root,timestamp_factory=lambda:NOW)


def envelope(**operands):
    return {'request_id':operands.pop('request_id'),**operands,'operator_wire_json':json.dumps(operands,separators=(',',':'))}


def submit(surface,action,payload,facility=''):
    return op.execute(surface,action=action,payload=payload,actor='fixture',request_scope='principal-A',entity_id=facility,
        native_write=lambda:surface.update_facility(facility,payload,actor='fixture') if facility else surface.create_facility(payload,actor='fixture'))


def upgrade(db):
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA foreign_keys=ON')
        sql=conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(FACILITY_CHANGES_TABLE,)).fetchone()[0]
        objects=[r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL AND type IN ('index','trigger')",(FACILITY_CHANGES_TABLE,))]
        rows=conn.execute(f'SELECT * FROM {FACILITY_CHANGES_TABLE}').fetchall()
        conn.execute(f'DROP TABLE {FACILITY_CHANGES_TABLE}')
        conn.execute(sql.replace(",'unchanged'",''))
        for row in rows:conn.execute(f"INSERT INTO {FACILITY_CHANGES_TABLE} VALUES({','.join('?' for _ in row)})",row)
        for statement in objects:conn.execute(statement)
        conn.execute(f'CREATE TABLE child(change_id TEXT REFERENCES {FACILITY_CHANGES_TABLE}(change_id) ON DELETE CASCADE,value TEXT)')
        conn.execute('INSERT INTO child VALUES(?,?)',(rows[0][0],'retained child'))
        conn.commit()
        old_sql=conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(FACILITY_CHANGES_TABLE,)).fetchone()[0]
        def interrupt(action,arg1,arg2,database,origin):
            return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_CREATE_INDEX and arg1=='ff_facility_changes_by_facility_time' else sqlite3.SQLITE_OK
        conn.set_authorizer(interrupt)
        try:_upgrade_facility_change_actions(conn)
        except sqlite3.DatabaseError:pass
        else:raise AssertionError('interrupted migration unexpectedly succeeded')
        conn.set_authorizer(None)
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert conn.execute('SELECT sql FROM sqlite_master WHERE name=?',(FACILITY_CHANGES_TABLE,)).fetchone()[0]==old_sql
        assert conn.execute(f'SELECT * FROM {FACILITY_CHANGES_TABLE}').fetchall()==rows
        assert conn.execute('SELECT value FROM child').fetchone()[0]=='retained child'
        _upgrade_facility_change_actions(conn)
        assert conn.execute(f'SELECT * FROM {FACILITY_CHANGES_TABLE}').fetchall()==rows
        assert conn.execute('SELECT value FROM child').fetchone()[0]=='retained child'
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()
        assert conn.execute('PRAGMA foreign_keys').fetchone()[0]==1
        actual=[r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND sql IS NOT NULL AND type IN ('index','trigger')",(FACILITY_CHANGES_TABLE,))]
        assert sorted(actual)==sorted(objects)
        try:conn.execute(f'DELETE FROM {FACILITY_CHANGES_TABLE}')
        except sqlite3.IntegrityError:pass
        else:raise AssertionError('native append-only trigger was lost')


def main():
    with TemporaryDirectory(prefix='operator-facility-') as raw:
        runtime,surface=fixture(Path(raw)/'runtime')
        create=envelope(request_id='facility-create-0001',name='Warehouse <script>',city='Москва',active=False)
        saved=submit(surface,'create',create)
        assert saved['status']=='accepted' and saved['acceptance']['durable_saved']
        fid=saved['acceptance']['source_ref']['entity_id']
        assert op.read(runtime.db_path,request_scope='principal-B',request_id=create['request_id'])['status']=='unknown'
        assert submit(surface,'create',create)==saved
        upgrade(runtime.db_path)
        detail=surface.facility_detail(fid)['facility']
        same=envelope(request_id='facility-unchanged-0001',name=detail['name'],expected_updated_at=detail['updated_at'],expected_source_digest=detail['operator_source_digest'])
        unchanged=submit(surface,'update',same,fid)
        assert unchanged['acceptance']['processing']['effect']=='unchanged'
        assert submit(surface,'update',same,fid)==unchanged
        pending=envelope(request_id='facility-activate-0001',active=True,expected_updated_at=detail['updated_at'],expected_source_digest=detail['operator_source_digest'])
        with patch.object(DenseFbsService,'_materialize',side_effect=AssertionError('HTTP source must not materialize')):
            accepted=submit(surface,'activate',pending,fid)
        assert accepted['acceptance']['state']=='processing'
        # A native queue state alone is not publication evidence.
        from packages.application.ff_pool_fbs_applicability import append_dense_intent_event
        intent_id=accepted['acceptance']['processing']['intent_id']
        with sqlite3.connect(runtime.db_path) as conn:
            append_dense_intent_event(conn,intent_id=intent_id,state='active',receipt={'facility_id':fid,'coverage_fingerprint':'not-published','activated_at':NOW},recorded_at=NOW)
            conn.commit()
        assert op.read(runtime.db_path,request_scope='principal-A',request_id=pending['request_id'])['acceptance']['state']=='processing'
        with sqlite3.connect(runtime.db_path) as conn:
            append_dense_intent_event(conn,intent_id=intent_id,state='resumable',receipt={'fixture':'resume exact plan'},recorded_at=NOW);conn.commit()
        assert not surface.facility_detail(fid)['facility']['active']
        assert submit(surface,'activate',pending,fid)==accepted
        drain=DenseFbsService(db_path=runtime.db_path,runtime_dir=surface.runtime_dir,timestamp_factory=lambda:NOW)
        assert drain.drain_facility_activations()['active']==1
        completed=op.read(runtime.db_path,request_scope='principal-A',request_id=pending['request_id'])
        assert completed['acceptance']['state']=='completed' and completed['acceptance']['processing']['published_active']
        assert surface.facility_detail(fid)['facility']['active']
        assert drain.drain_facility_activations()['captured']==0
        # Immutable exact completion survives a later source rename.
        current=surface.facility_detail(fid)['facility']
        rename=envelope(request_id='facility-rename-0001',name='Updated',expected_updated_at=current['updated_at'],expected_source_digest=current['operator_source_digest'])
        assert submit(surface,'update',rename,fid)['status']=='accepted'
        assert op.read(runtime.db_path,request_scope='principal-A',request_id=pending['request_id'])==completed
        # Same timestamp ABA does not bypass the full native source guard.
        stale=envelope(request_id='facility-stale-0001',name='Bad',expected_updated_at=detail['updated_at'],expected_source_digest=detail['operator_source_digest'])
        rejected=submit(surface,'update',stale,fid)
        assert rejected['status']=='rejected' and not rejected['acceptance']
        assert op.read(runtime.db_path,request_scope='principal-A',request_id=stale['request_id'])==rejected
        # Publication must repeat the full source guard after physical work.
        racing=submit(surface,'create',envelope(request_id='facility-race-create',name='Race',active=False))
        racing_id=racing['acceptance']['source_ref']['entity_id'];racing_detail=surface.facility_detail(racing_id)['facility']
        racing_request=envelope(request_id='facility-race-activate',active=True,expected_updated_at=racing_detail['updated_at'],expected_source_digest=racing_detail['operator_source_digest'])
        submit(surface,'activate',racing_request,racing_id)
        actual_materialize=drain._materialize
        def concurrent_source_change(intent):
            result=actual_materialize(intent)
            with sqlite3.connect(runtime.db_path) as conn:
                conn.execute(f'UPDATE {FACILITIES_TABLE} SET name=? WHERE facility_id=?',('Concurrent source after physical work',racing_id));conn.commit()
            return result
        with patch.object(drain,'_materialize',side_effect=concurrent_source_change):
            assert drain.drain_facility_activations()['blocked']==1
        assert not surface.facility_detail(racing_id)['facility']['active']
        assert op.read(runtime.db_path,request_scope='principal-A',request_id=racing_request['request_id'])['acceptance']['state']=='needs_attention'
        # Recoverable native resume uses the same staged intent and source.
        resumable=submit(surface,'create',envelope(request_id='facility-resume-create',name='Resume',active=False))
        resumable_id=resumable['acceptance']['source_ref']['entity_id'];resumable_detail=surface.facility_detail(resumable_id)['facility']
        resume_request=envelope(request_id='facility-resume-activate',active=True,expected_updated_at=resumable_detail['updated_at'],expected_source_digest=resumable_detail['operator_source_digest'])
        submit(surface,'activate',resume_request,resumable_id)
        with patch.object(drain,'_materialize',side_effect=sqlite3.OperationalError('database is locked')):
            assert drain.drain_facility_activations()['pending']==1
        assert op.read(runtime.db_path,request_scope='principal-A',request_id=resume_request['request_id'])['acceptance']['state']=='processing'
        restarted=DenseFbsService(db_path=runtime.db_path,runtime_dir=surface.runtime_dir,timestamp_factory=lambda:NOW)
        assert restarted.drain_facility_activations()['active']==1
        assert op.read(runtime.db_path,request_scope='principal-A',request_id=resume_request['request_id'])['acceptance']['state']=='completed'
        # A repeatedly pending first plan cannot starve a later saved plan.
        fair=[]
        for index in range(2):
            created=submit(surface,'create',envelope(request_id=f'facility-fair-create-{index}',name=f'Fair {index}',active=False))
            identity=created['acceptance']['source_ref']['entity_id'];data=surface.facility_detail(identity)['facility']
            request_data=envelope(request_id=f'facility-fair-activate-{index}',active=True,expected_updated_at=data['updated_at'],expected_source_digest=data['operator_source_digest'])
            submit(surface,'activate',request_data,identity);fair.append(identity)
        with patch.object(restarted,'_materialize',side_effect=sqlite3.OperationalError('database is locked')):
            assert restarted.drain_facility_activations(limit=1)['pending']==1
        assert restarted.drain_facility_activations(limit=1)['active']==1
        assert not surface.facility_detail(fair[0])['facility']['active'] and surface.facility_detail(fair[1])['facility']['active']
        assert restarted.drain_facility_activations(limit=1)['active']==1
        # GUI fields and full-source token share one RO snapshot even when an
        # independent connection commits an ABA timestamp change during detail.
        snapshot=submit(surface,'create',envelope(request_id='facility-snapshot-create',name='Snapshot before',active=False))
        snapshot_id=snapshot['acceptance']['source_ref']['entity_id']
        with sqlite3.connect(runtime.db_path) as conn:conn.execute('PRAGMA journal_mode=WAL')
        native_capture=op.facility_source
        def concurrent_detail_commit(conn,identity):
            with sqlite3.connect(runtime.db_path) as writer:
                writer.execute(f'UPDATE {FACILITIES_TABLE} SET name=? WHERE facility_id=?',('Snapshot after',identity));writer.commit()
            return native_capture(conn,identity)
        with patch.object(op,'facility_source',side_effect=concurrent_detail_commit):
            shown=surface.facility_detail(snapshot_id)['facility']
        assert shown['name']=='Snapshot before' and surface.facility_detail(snapshot_id)['facility']['name']=='Snapshot after'
        stale_snapshot=envelope(request_id='facility-snapshot-stale',name='Unsafe overwrite',expected_updated_at=shown['updated_at'],expected_source_digest=shown['operator_source_digest'])
        assert submit(surface,'update',stale_snapshot,snapshot_id)['status']=='rejected'
        # Delayed roster generation must not silently activate incomplete closure.
        other=submit(surface,'create',envelope(request_id='facility-create-0002',name='Delayed',active=False))
        other_id=other['acceptance']['source_ref']['entity_id'];other_detail=surface.facility_detail(other_id)['facility']
        request=envelope(request_id='facility-activate-0002',active=True,expected_updated_at=other_detail['updated_at'],expected_source_digest=other_detail['operator_source_digest'])
        submit(surface,'activate',request,other_id)
        with sqlite3.connect(runtime.db_path) as conn:
            conn.execute("UPDATE sheet_vitrina_v1_nomenclature_items SET updated_at='2026-08-26T09:00:00Z' WHERE nm_id=101");conn.commit()
        assert drain.drain_facility_activations()['blocked']==1
        assert not surface.facility_detail(other_id)['facility']['active']
        state=op.read(runtime.db_path,request_scope='principal-A',request_id=request['request_id'])
        assert state['acceptance']['durable_saved'] and state['acceptance']['state']=='needs_attention'
        assert op.journal_entries(runtime.db_path,request_scope='principal-B')==[]
        entries=op.journal_entries(runtime.db_path,request_scope='principal-A')
        assert all(value['durable_saved'] for value in entries) and len({value['operation_id'] for value in entries})==len(entries)
        assert len(entries)==15 # refused no-effect audit is excluded before journal counting
    print(json.dumps({'status':'ok','native_source':True,'native_dense_publisher':True,'prior_schema_upgrade_and_interrupted_rollback':True}))
if __name__=='__main__':main()
