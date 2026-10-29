"""Preserve accepted default columns when a newer ready becomes the default.

Promotion retains source timestamps and temporal roles. It does not certify a
closed day or rebuild any source facts, accounting bindings, or inventory.
"""
from dataclasses import replace
import json

from packages.application import ready_publication as ready

MAX_HEADERS = 10000
MAX_BYTES = 32 * 1024**2


def headers(conn):
    from packages.application.web_vitrina_window_v3 import _ReadyHeader
    count, size = conn.execute(f'''SELECT count(*),coalesce(sum(length(CAST(
        coalesce(json_extract(plan_json,'$.date_columns'),'')||bundle_version||as_of_date||snapshot_id||activated_at||refreshed_at AS BLOB))),0)
        FROM {ready.TABLE}''').fetchone()
    if count > MAX_HEADERS or size > MAX_BYTES:
        raise ready.ReadyPublicationConflict('ready_rollover_header_limit')
    return [_ReadyHeader(r[0],r[1],r[2],r[3],r[4],tuple(json.loads(r[5] or '[]')),'{}',0)
        for r in conn.execute(f'''SELECT bundle_version,as_of_date,snapshot_id,activated_at,refreshed_at,json_extract(plan_json,'$.date_columns')
        FROM {ready.TABLE} ORDER BY activated_at DESC,refreshed_at DESC,as_of_date DESC,bundle_version DESC''')]


def preserve_binding_targets(plan):
    """Retained bindings still name their original canonical ready owner."""
    targets = dict(plan.metadata.get('fbs_accounting_targets', {}))
    for day, binding in plan.metadata.get('fbs_accounting_bindings', {}).items():
        if binding.get('ready_target'):
            targets[day] = dict(binding['ready_target'])
    return replace(plan, metadata={**plan.metadata, 'fbs_accounting_targets': targets})


def prepare(conn, *, plan, expected, default_date, degraded=None):
    from packages.application.web_vitrina_window_v3 import _ReadyHeader, _select_bindings, _selected_default_header
    from packages.application.fbs_accounting_historical_publication import _promoted_envelope
    from packages.application.registry_upload_db_backed_runtime import _deserialize_sheet_vitrina_plan, _serialize_sheet_vitrina_plan
    from packages.application.fbs_accounting_degraded import materialize_degraded_accounting
    bundle = expected.bundle_version
    selected_headers = headers(conn)
    prior = _selected_default_header(selected_headers, bundle, default_date)
    if prior is None or plan.as_of_date <= prior.as_of_date:
        return []
    after = [h for h in selected_headers if h.key != (bundle, plan.as_of_date)] + [
        _ReadyHeader(bundle, plan.as_of_date, plan.snapshot_id, '', '', tuple(plan.date_columns), '{}', 0)]
    if _selected_default_header(after, bundle, default_date).key == prior.key:
        return []
    dates = [d for d in prior.date_columns if d < plan.as_of_date]
    consumed = 0
    def recovery(connection, header, day):
        nonlocal consumed
        size = connection.execute(f"SELECT length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?", header.key).fetchone()[0]
        consumed += size
        if consumed > MAX_BYTES:
            raise ready.ReadyPublicationConflict('ready_rollover_source_limit')
        from packages.application.web_vitrina_window_v3 import _covering_recovery
        return _covering_recovery(connection, header, day)
    before_bindings, _ = _select_bindings(conn, selected_headers, dates, bundle, default_date, recovery_check=recovery)
    after_bindings, _ = _select_bindings(conn, after, dates, bundle, default_date, recovery_check=recovery)
    lost = [b.date for b, a in zip(before_bindings, after_bindings)
            if b.source_key == prior.key and a.source_key is None]
    if not lost:
        return []
    size = conn.execute(f'SELECT length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?', prior.key).fetchone()[0]
    if consumed + size * (len(lost) + 1) > MAX_BYTES:
        raise ready.ReadyPublicationConflict('ready_rollover_source_limit')
    donor = ready.capture_expected(conn, bundle_version=bundle, as_of_date=prior.as_of_date, authority=expected.authority)
    result = []
    for day in lost:
        target = ready.capture_expected(conn, bundle_version=bundle, as_of_date=day, authority=expected.authority)
        if target.exists:
            raise ready.ReadyPublicationConflict('ready_rollover_target_exists')
        promoted = _deserialize_sheet_vitrina_plan(_promoted_envelope(donor, day))
        promoted = preserve_binding_targets(materialize_degraded_accounting(promoted, proof=degraded))
        promoted = replace(promoted, metadata={**promoted.metadata,
            'ready_publication_target': {'bundle_version': bundle, 'as_of_date': day},
            'ready_rollover': {'source_as_of_date': prior.as_of_date, 'source_digest': ready.digest(donor.plan_json),
                'source_refreshed_at': prior.refreshed_at, 'status': 'retained_prior_default'}})
        result.append(dict(expected=target, donor=donor, plan_json=_serialize_sheet_vitrina_plan(promoted),
            activated_at=prior.activated_at, refreshed_at=prior.refreshed_at,
            snapshot_id=promoted.snapshot_id, plan_version=promoted.plan_version))
    return result


def check(conn, candidates):
    for item in candidates:
        ready.check_expected(conn, item['donor'])
        ready.check_expected(conn, item['expected'])


def apply(conn, candidates, *, operation_id, finished_at, book_version):
    check(conn, candidates)
    for item in candidates:
        target = item['expected']
        identity = operation_id + ':rollover:' + target.as_of_date
        ready.record_intent(conn, operation_id=identity, attempt_id='1', kind='ready_rollover', expected=target,
            inputs={'donor': {'bundle_version': item['donor'].bundle_version,
                'as_of_date': item['donor'].as_of_date, 'digest': ready.digest(item['donor'].plan_json),
                'authority': item['donor'].authority}}, expected_book=book_version,
            book_required=False, ready_required=True, created_at=finished_at)
        ready.replace_ready(conn, **{k: v for k, v in item.items() if k != 'donor'})
        ready.complete_publication(conn, operation_id=identity, attempt_id='1', book_version=book_version,
            after_digest=ready.digest(item['plan_json']), finished_at=finished_at)
