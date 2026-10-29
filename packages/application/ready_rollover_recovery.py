"""Explicit, previewed recovery of a previously published default tail.

The completed cycle and its exact ready receipt are witnesses, not permission
for arbitrary covering snapshots. No book, inventory or served history changes.
"""
from dataclasses import asdict, replace
from pathlib import Path
import json
import re
import os
import stat
from datetime import date

from packages.application import ready_publication as ready
from packages.application import ready_rollover


def preview(runtime, *, cycle_id, day):
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.fbs_accounting_degraded import materialize_degraded_accounting
    from packages.application.fbs_accounting_historical_publication import _promoted_envelope
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan, _serialize_sheet_vitrina_plan
    def require(ok, reason):
        if not ok:
            raise ready.ReadyPublicationConflict('ready_rollover_recovery_' + reason)
    require(date.fromisoformat(day).isoformat() == day, 'day_invalid')
    require(bool(re.fullmatch('[a-f0-9]{32}', cycle_id)), 'cycle_id_invalid')
    path = Path(runtime.runtime_dir) / 'sheet-vitrina-cycles' / (cycle_id + '.json')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= 4*1024**2, 'witness_limit')
        raw = stream.read(4*1024**2 + 1)
        require(len(raw.encode()) <= 4*1024**2, 'witness_limit')
    cycle = json.loads(raw)
    versions = cycle['final_versions']
    require(cycle.get('cycle_id') == cycle_id and cycle.get('status') in {'complete','degraded'} and bool(cycle.get('finished_at')), 'cycle_incomplete')
    require(versions.get('history_status') == 'published'
        and bool(versions.get('history_edition_id')) and bool(versions.get('history_vector_digest'))
        and versions['history_window_from'] <= day <= versions['history_window_to']
        and versions['as_of_date'] < day, 'day_not_previously_published')
    book, version = accounting.load(runtime.runtime_dir)
    periods = (book or {}).get('state', {}).get('periods', {})
    open_days = [d for d,v in periods.items() if v.get('status') == 'open']
    require(book and book.get('active') and open_days
        and book.get('publication_error', {}).get('reason') == 'incomplete_period_cannot_close', 'accounting_proof_unavailable')
    proof = {'book_version': version, 'reason': 'incomplete_period_cannot_close',
        'invalid_from_date': min(open_days), 'closed_dates': sorted(d for d,v in periods.items() if v.get('status') == 'closed')}
    require(day >= proof['invalid_from_date'] and day not in proof['closed_dates'], 'day_outside_unavailable_scope')
    authority = ready.capture_authority(runtime.runtime_dir, db_path=runtime.db_path)
    with ready.readonly(runtime.db_path) as conn:
        active = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
        require(active and active[0] == versions['bundle_version'], 'bundle_changed')
        size = conn.execute('SELECT length(CAST(plan_json AS BLOB)) FROM '+ready.TABLE+' WHERE bundle_version=? AND as_of_date=?',
            (versions['bundle_version'],versions['as_of_date'])).fetchone()
        require(size and size[0] <= ready_rollover.MAX_BYTES//2, 'donor_limit')
        donor = ready.capture_expected(conn, bundle_version=versions['bundle_version'], as_of_date=versions['as_of_date'], authority=authority)
        target = ready.capture_expected(conn, bundle_version=versions['bundle_version'], as_of_date=day, authority=authority)
        require(not target.exists, 'target_exists')
        from packages.application.web_vitrina_window_v3 import _select_bindings
        from packages.business_time import default_business_as_of_date
        def bounded_recovery(connection, header, date):
            size = connection.execute('SELECT length(CAST(plan_json AS BLOB)) FROM '+ready.TABLE+' WHERE bundle_version=? AND as_of_date=?',header.key).fetchone()[0]
            require(size <= ready_rollover.MAX_BYTES, 'source_limit')
            from packages.application.web_vitrina_window_v3 import _covering_recovery
            return _covering_recovery(connection,header,date)
        bindings, default = _select_bindings(conn,ready_rollover.headers(conn),[day],donor.bundle_version,default_business_as_of_date(),recovery_check=bounded_recovery)
        require(bindings[0].source_key is None and default is not None and default.as_of_date > day, 'day_not_missing_after_advance')
        receipt = conn.execute('''SELECT state,bundle_version,as_of_date,after_digest,finished_at FROM sheet_vitrina_v1_ready_publications
            WHERE operation_id=? AND attempt_id=?''', (versions['publication_operation_id'], versions['publication_attempt_id'])).fetchone()
        require(receipt and tuple(receipt) == ('complete',donor.bundle_version,donor.as_of_date,
            ready.digest(donor.plan_json),versions['refreshed_at']), 'publication_witness_changed')
        row = conn.execute('SELECT activated_at,refreshed_at,snapshot_id FROM '+ready.TABLE+' WHERE bundle_version=? AND as_of_date=?',
            (donor.bundle_version,donor.as_of_date)).fetchone()
        require(row[1] == versions['refreshed_at'] and row[2] == versions['snapshot_id'], 'donor_identity_changed')
        source = _deserialize_sheet_vitrina_plan(donor.plan_json)
        require(source.as_of_date == donor.as_of_date and day in source.date_columns and any(s.column_date == day and s.slot_key == 'today_current' for s in source.temporal_slots), 'not_prior_current_column')
        promoted = materialize_degraded_accounting(_deserialize_sheet_vitrina_plan(_promoted_envelope(donor,day)), proof=proof)
        promoted = ready_rollover.preserve_binding_targets(promoted)
        promoted = replace(promoted,metadata={**promoted.metadata,
            'ready_publication_target': {'bundle_version': target.bundle_version, 'as_of_date':day},
            'ready_rollover': {'status':'retained_prior_default','source_as_of_date':donor.as_of_date,
                'source_digest':ready.digest(donor.plan_json),'source_refreshed_at':row[1], 'witness_cycle_id':cycle_id}})
        candidate = dict(expected=asdict(target), donor=asdict(donor), plan_json=_serialize_sheet_vitrina_plan(promoted),
            activated_at=row[0],refreshed_at=row[1],snapshot_id=promoted.snapshot_id,plan_version=promoted.plan_version)
        manifest = dict(contract='ready_rollover_recovery_v1',cycle_id=cycle_id,day=day,
            witness_digest=ready.digest(raw),book_version=version,history=ready.capture_history(conn),
            candidate=candidate,accounting_proof=proof)
    return {**manifest,'preview_digest':ready.digest(ready.canonical(manifest))}


def apply(runtime, *, reviewed, operation_id, finished_at):
    from packages.application.business_data_heavy_admission import heavy_admitted
    from packages.application.warehouse_functional_lock import warehouse_functional_write_lock
    from packages.application import fbs_accounting_runtime as accounting
    from packages.application.fbs_accounting_degraded import degraded_accounting_scope, current_degraded_accounting
    from packages.application.registry_upload_db_backed_runtime import _connect, _validate_timestamp
    _validate_timestamp(finished_at, field_name="finished_at")
    if not operation_id or not reviewed.get('preview_digest'):
        raise ready.ReadyPublicationConflict('ready_rollover_recovery_review_required')
    with heavy_admitted(runtime.runtime_dir, operation='cycle'):
        fresh = preview(runtime,cycle_id=reviewed['cycle_id'],day=reviewed['day'])
        if fresh != reviewed:
            raise ready.ReadyPublicationConflict('ready_rollover_recovery_preview_changed')
        versions = dict(fbs_accounting_status='unavailable',fbs_accounting_reason='incomplete_period_cannot_close',fbs_book=fresh['book_version'])
        item = dict(fresh['candidate'])
        item['expected'] = ready.ExpectedReady(**item['expected'])
        item['donor'] = ready.ExpectedReady(**item['donor'])
        with degraded_accounting_scope(runtime,versions), warehouse_functional_write_lock(runtime.runtime_dir,timeout_seconds=5), accounting.writer_lock(runtime.runtime_dir), _connect(runtime.db_path) as conn:
            conn.execute('PRAGMA busy_timeout=0')
            conn.execute('BEGIN IMMEDIATE')
            current_degraded_accounting(runtime)
            active = conn.execute('SELECT bundle_version FROM registry_upload_current_state WHERE slot=1').fetchone()
            if not active or active[0] != item['expected'].bundle_version:
                raise ready.ReadyPublicationConflict('ready_rollover_recovery_bundle_changed')
            if ready.capture_history(conn) != fresh['history']:
                raise ready.ReadyPublicationConflict('ready_rollover_recovery_history_changed')
            ready_rollover.apply(conn,[item],operation_id=operation_id,finished_at=finished_at,book_version=fresh['book_version'])
            ready.check_pinned_authority(conn,item['expected'].authority)
            conn.commit()
    return {'status':'complete','operation_id':operation_id+':rollover:'+fresh['day'],
        'attempt_id':'1','as_of_date':fresh['day'],'after_digest':ready.digest(item['plan_json'])}
