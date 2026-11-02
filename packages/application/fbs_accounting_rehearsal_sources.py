"""Bounded native history/Ready operands shared by private accounting rehearsals.

Selectors retain the production policy. Unconsumed Ready bodies and superseded,
unfinalized quantity components are not numerical inputs to a rehearsal. Their
small source metadata remains fenced; no production row is changed or replaced.
"""
from packages.application import ready_publication as ready
from packages.application import fbs_accounting_historical_publication as publication
from packages.application.sheet_vitrina_v1_inventory_history import CAPTURES_TABLE,COMPONENTS_TABLE,FINALIZATIONS_TABLE


def select_rehearsal_sources(conn,*,dates,now,extra_ready_keys=()):
    """Return fixed row predicates and selector queries for the same RO snapshot."""
    publication.stages.require(conn.in_transaction and conn.execute('PRAGMA query_only').fetchone()[0]==1,
                               'historical_rehearsal_query_only_required')
    proof={}
    rows,selected=publication._selected_ready_rows(conn,plan={'scope':{'dates':list(dates)}},
        authority=None,current_envelope=None,now=now,selection_proof=proof,keys_only=True)
    keys={(r['bundle_version'],r['as_of_date']) for r in rows}
    # Keep the original default even if an exact dated row wins every target.
    # Otherwise pruning the archive could change fallback selection semantics.
    if proof['default_key']:keys.add(tuple(proof['default_key']))
    keys.update(tuple(key) for key in extra_ready_keys)
    publication.stages.require(0<len(keys)<=publication.MAX_READY_HEADERS,'historical_rehearsal_ready_scope_invalid')
    clauses=[];args=[];total=0
    for bundle,day in sorted(keys):
        row=conn.execute(f'SELECT length(CAST(plan_json AS BLOB)) FROM {ready.TABLE} WHERE bundle_version=? AND as_of_date=?',(bundle,day)).fetchone()
        publication.stages.require(row is not None,'historical_rehearsal_ready_source_missing')
        total+=row[0]
        publication.stages.require(total<=publication.MAX_BYTES,'historical_rehearsal_ready_body_budget')
        clauses.append('(bundle_version=? AND as_of_date=?)');args.extend((bundle,day))
    # Native quantity readers use latest finalized/day, or latest capture/day
    # for the current day and accepted-column backfill. Keep all finalizations,
    # including superseded ones, so their immutable capture references survive.
    inventory_ids=f'''SELECT capture_id FROM {FINALIZATIONS_TABLE}
        UNION SELECT capture_id FROM (SELECT capture_id,
          ROW_NUMBER() OVER(PARTITION BY business_date ORDER BY capture_sequence DESC) AS rank
          FROM {CAPTURES_TABLE}) WHERE rank=1'''
    filters={ready.TABLE:(' OR '.join(clauses),args),COMPONENTS_TABLE:(f'capture_id IN ({inventory_ids})',())}
    return dict(filters=filters,selection_queries=proof['queries'],selected=selected,
                ready_keys=sorted(keys),ready_bytes=total)
