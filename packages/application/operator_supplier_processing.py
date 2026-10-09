"""Correlate admitted supplier revisions with real native publications.

These immutable acknowledgements are not cost authority or a second queue.
The native source, preparation intent, WarehouseFunctional, dated accounting
and Finance publishers remain the owners of every business fact.
"""
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path

from packages.application import operator_supplier_shipments as source

FUNCTIONAL = "sheet_vitrina_v1_operator_supplier_functional_proofs"
COMPLETIONS = "sheet_vitrina_v1_operator_supplier_completions"
ATTEMPTS = "sheet_vitrina_v1_operator_supplier_completion_attempts"
QUEUE = "sheet_vitrina_v1_warehouse_targeted_recalc_queue"
COHORT_LIMIT = 32
SUPERSEDED = "supplier_completion_superseded"


def ensure_schema(conn):
    conn.execute(f"CREATE TABLE IF NOT EXISTS {FUNCTIONAL}(operation_id TEXT NOT NULL,version_id TEXT NOT NULL,proof_json TEXT NOT NULL,proof_digest TEXT NOT NULL,PRIMARY KEY(operation_id,version_id))")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {COMPLETIONS}(operation_id TEXT PRIMARY KEY,proof_json TEXT NOT NULL,proof_digest TEXT NOT NULL,completed_at TEXT NOT NULL)")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {ATTEMPTS}(operation_id TEXT PRIMARY KEY,state TEXT NOT NULL,reason TEXT NOT NULL,checked_at TEXT NOT NULL)")
    for table in (FUNCTIONAL, COMPLETIONS):
        for event in ("UPDATE", "DELETE"):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{event.lower()}_immutable BEFORE {event} ON {table} BEGIN SELECT RAISE(ABORT,'supplier completion acknowledgement is immutable'); END")


def operation(conn, operation_id):
    """Resolve only admitted actions; never synthesize receipts for old sources."""
    from packages.application import operator_supplier_factual_dates as factual, operator_supplier_financial as financial
    row = conn.execute(f"SELECT * FROM {financial.SCOPES} WHERE operation_id=?",(operation_id,)).fetchone() if source._exists(conn,financial.SCOPES) else None
    if row:
        child=conn.execute(f"SELECT * FROM {financial.CHILDREN} WHERE operation_id=?",(row["parent_operation_id"],)).fetchone()
        if not child or source.digest(json.loads(child["source_json"]))!=child["source_digest"]:
            raise ValueError("supplier_financial_source_receipt_changed")
        return {"operation_id":operation_id,"shipment_id":row["shipment_id"],"domain":financial.DOMAIN,
                "action":"financial","revision":child["revision"],"source_digest":child["source_digest"],
                "saved_source":json.loads(row["source_json"]),"intent":json.loads(row["intent_json"]),"intent_kind":row["intent_kind"]}
    row = conn.execute(f"SELECT * FROM {source.TABLE} WHERE operation_id=?", (operation_id,)).fetchone() if source._exists(conn, source.TABLE) else None
    if row:
        saved = json.loads(row["source_json"])
        return {"operation_id": operation_id, "shipment_id": row["shipment_id"], "domain": source.DOMAIN,
            "action": row["action"], "revision": row["revision"], "source_digest": row["source_digest"],
            "saved_source": saved, "intent": json.loads(row["intent_json"])}
    row = conn.execute(f"SELECT * FROM {factual.TABLE} WHERE correction_id=? AND status='accepted' LIMIT 1", (operation_id,)).fetchone() if source._exists(conn, factual.TABLE) else None
    if not row:
        raise ValueError("supplier_admitted_operation_missing")
    job = conn.execute(f"SELECT * FROM {factual.JOB} WHERE correction_id=?", (operation_id,)).fetchone()
    if not job or job["actor"] != row["actor"] or job["shipment_id"] != row["shipment_id"] or job["request_fingerprint"] != row["request_fingerprint"]:
        raise ValueError("supplier_exact_factual_job_changed")
    applied = factual.applied_proof(conn, row)
    if not applied:
        raise ValueError("supplier_factual_apply_pending")
    saved = applied["source_after"]["supplier"]
    return {"operation_id": operation_id, "shipment_id": row["shipment_id"], "domain": factual.DOMAIN,
        "action": "factual_date", "revision": row["request_fingerprint"], "source_digest": source.digest(saved),
        "saved_source": saved, "intent": applied["preparation_intent"]}


def ref(op):
    return {key: op[key] for key in ("domain", "operation_id", "shipment_id", "action", "revision", "source_digest")}


def current(conn, op):
    if op['domain']=='supplier_financial_document':
        from packages.application.operator_supplier_financial import child_current
        if not child_current(conn,op):return False
    bound = op["intent"]
    if op.get("intent_kind")=="cny":
        from packages.application import cny_preparation_intents as cny
        row=conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone()
        return bool(row and row["revision"]==bound["revision"] and row["source_fingerprint"]==bound["source_fingerprint"] and cny._matches(conn,dict(row)))
    if not bound:
        return source.digest(source.capture(conn, op["shipment_id"])) == op["source_digest"]
    # Price-check audit fields and source clocks are not native cost operands.
    # Bind the admitted immutable cost source to the SAME monotonic native
    # preparation revision, not to a newer operator/source-only audit version.
    if source.intents._fingerprint(op["saved_source"]["preparation_source"]) != bound["source_fingerprint"]:
        return False
    row = conn.execute(f"SELECT * FROM {source.intents.TABLE} WHERE shipment_id=?", (op["shipment_id"],)).fetchone()
    return bool(row and row["revision"] == bound["revision"] and row["source_fingerprint"] == bound["source_fingerprint"]
                and source.intents._request_source_matches(conn, dict(row)))


def superseded(conn, op):
    """Only a proven newer monotonic native source can retire this action."""
    if op['domain']=='supplier_financial_document':
        from packages.application.operator_supplier_financial import child_superseded
        if child_superseded(conn,op):return True
    bound = op["intent"]
    if op.get("intent_kind")=="cny":
        from packages.application import cny_preparation_intents as cny
        row=conn.execute(f"SELECT * FROM {cny.TABLE} WHERE account_id='account'").fetchone()
        return bool(row and row["revision"]>bound["revision"] and cny._matches(conn,dict(row)))
    if not bound or source.intents._fingerprint(op["saved_source"]["preparation_source"]) != bound["source_fingerprint"]:
        return False
    row = conn.execute(f"SELECT * FROM {source.intents.TABLE} WHERE shipment_id=?", (op["shipment_id"],)).fetchone()
    return bool(row and row["revision"] > bound["revision"]
                and source.intents._request_source_matches(conn, dict(row)))


def captured_cohort(conn):
    """Bound one invocation; oldest last-check progresses despite pending work."""
    from packages.application import operator_supplier_factual_dates as factual
    candidates = []
    if source._exists(conn, source.TABLE):
        candidates.append(f"SELECT operation_id,accepted_at FROM {source.TABLE} WHERE intent_json<>'{{}}'")
    if source._exists(conn, factual.APPLIED):
        candidates.append(f"SELECT a.correction_id operation_id,MIN(r.accepted_at) accepted_at FROM {factual.APPLIED} a JOIN {factual.TABLE} r USING(correction_id) GROUP BY a.correction_id")
    from packages.application import operator_supplier_financial as financial
    if source._exists(conn,financial.SCOPES):
        candidates.append(f"SELECT s.operation_id,c.accepted_at FROM {financial.SCOPES} s JOIN {financial.CHILDREN} c ON c.operation_id=s.parent_operation_id")
    if not candidates:
        return []
    return [row[0] for row in conn.execute("WITH admitted AS ("+" UNION ALL ".join(candidates)+f") SELECT admitted.operation_id FROM admitted LEFT JOIN {COMPLETIONS} c USING(operation_id) LEFT JOIN {ATTEMPTS} a USING(operation_id) WHERE c.operation_id IS NULL AND COALESCE(a.reason,'')<>? GROUP BY admitted.operation_id ORDER BY COALESCE(a.checked_at,admitted.accepted_at),admitted.operation_id LIMIT ?", (SUPERSEDED, COHORT_LIMIT))]


def queue_for(conn, op):
    bound = op["intent"]
    is_cny=op.get("intent_kind")=="cny"
    revision = ("cny-preparation:" if is_cny else "supplier-preparation:")+str(bound["revision"])+":"+bound["source_fingerprint"]
    rows = conn.execute(f"SELECT * FROM {QUEUE} WHERE stable_source_id=? AND source_revision=? ORDER BY queue_id",
                        (("cny_document:account_replay" if is_cny else "supplier_shipment:"+op["shipment_id"]), revision)).fetchall()
    if len(rows) != 1:
        raise ValueError("supplier_exact_queue_pending")
    queue = dict(rows[0])
    if (queue["affected_nm_ids_json"] != bound["affected_nm_ids_json"] or queue["effective_date"] != bound["effective_date"]
            or queue["status"] != "complete" or queue["error"]):
        raise ValueError("supplier_exact_functional_publication_pending")
    return queue


def queue_ref(queue):
    return {**{key: queue[key] for key in ("queue_id", "stable_source_id", "source_revision", "effective_date")},
            "affected_nm_ids": json.loads(queue["affected_nm_ids_json"])}


def cost_state(conn, version_id, shipment_id):
    from packages.application.warehouse_functional import _effective_supplier_cost_state
    row = _effective_supplier_cost_state(conn, version_id=version_id, shipment_id=shipment_id)
    if row is None:
        # Native presentation's certified-only accessor intentionally hides
        # provisional calculations. Published provisional cost is still a
        # real result; completing its publication does not certify expenses.
        row = conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_supplier_cost_states WHERE version_id=? AND shipment_id=?", (version_id, shipment_id)).fetchone()
    return {key: row[key] for key in ("shipment_id", "source_fingerprint", "calculation_fingerprint", "expenses_complete", "calculation_available")} if row else None


def daily_rows(conn, queue, day):
    ids = queue_ref(queue)["affected_nm_ids"]
    return [dict(row) for row in conn.execute("SELECT as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,fingerprint FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=? AND as_of_date>=? AND as_of_date<=? AND nm_id IN ("+','.join('?' for _ in ids)+") ORDER BY as_of_date,nm_id",
        ('warehouse_functional_cutover_v1', queue["effective_date"], day, *ids))] if ids else []


def balance_rows(conn, version_id, ids):
    return [dict(row) for row in conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_balances WHERE version_id=? AND nm_id IN ("+','.join('?' for _ in ids)+") ORDER BY warehouse_key,nm_id", (version_id, *ids))] if ids else []


def record_functional_publication(conn, *, plan, version_id):
    """Called under the actual native source/plan CAS, before publisher COMMIT.

    Retain only exact newly consumed or previously correlated unfinished actions.
    This hook does SQL reads; it never recomputes allocations inside the writer.
    """
    from packages.application import operator_supplier_factual_dates as factual, operator_supplier_financial as financial
    if not source._exists(conn, FUNCTIONAL):
        return
    if not conn.in_transaction:
        raise ValueError("supplier_functional_proof_requires_native_writer")
    ids = set()
    for request in plan.get("targeted_recalc_requests") or []:
        if source._exists(conn,financial.SCOPES):
            native_id=str(request.get("stable_source_id") or "")
            parts=str(request.get("source_revision") or "").split(":",2)
            if len(parts)==3 and parts[0] in {"cny-preparation","supplier-preparation"} and parts[1].isdigit():
                kind="cny" if parts[0]=="cny-preparation" else "supplier"
                rows=conn.execute(f"SELECT operation_id FROM {financial.SCOPES} WHERE intent_kind=? AND json_extract(intent_json,'$.revision')=? AND json_extract(intent_json,'$.source_fingerprint')=? AND (?='cny_document:account_replay' OR shipment_id=?)",(kind,int(parts[1]),parts[2],native_id,native_id.removeprefix('supplier_shipment:')))
                ids.update(row[0] for row in rows)
        shipment = str(request.get("stable_source_id") or "").removeprefix("supplier_shipment:")
        if str(request.get("stable_source_id") or "") != "supplier_shipment:"+shipment:
            continue
        try:
            prefix, revision, fingerprint = str(request["source_revision"]).split(":", 2)
            if prefix != "supplier-preparation" or revision != str(int(revision)):
                continue
        except (KeyError, ValueError):
            continue
        if source._exists(conn, source.TABLE):
            ids.update(row[0] for row in conn.execute(f"SELECT operation_id FROM {source.TABLE} WHERE shipment_id=? AND json_extract(intent_json,'$.revision')=? AND json_extract(intent_json,'$.source_fingerprint')=?", (shipment, int(revision), fingerprint)))
        if source._exists(conn, factual.APPLIED):
            ids.update(row[0] for row in conn.execute(f"SELECT DISTINCT a.correction_id FROM {factual.APPLIED} a JOIN {factual.TABLE} r USING(correction_id) WHERE r.shipment_id=? AND json_extract(a.proof_json,'$.preparation_intent.revision')=? AND json_extract(a.proof_json,'$.preparation_intent.source_fingerprint')=?", (shipment, int(revision), fingerprint)))
    # Native new requests above are exact. Refresh only a bounded oldest
    # retained unfinished cohort; newer actual publications advance its cursor.
    ids.update(row[0] for row in conn.execute(f"SELECT p.operation_id FROM {FUNCTIONAL} p JOIN sheet_vitrina_v1_warehouse_functional_versions v USING(version_id) LEFT JOIN {COMPLETIONS} c USING(operation_id) LEFT JOIN {ATTEMPTS} a USING(operation_id) WHERE c.operation_id IS NULL AND COALESCE(a.reason,'')<>? GROUP BY p.operation_id ORDER BY MAX(v.published_at),p.operation_id LIMIT ?", (SUPERSEDED, COHORT_LIMIT)))
    covered = set((plan.get("wb_snapshot") or {}).get("requested_nm_ids") or [])
    for identity in sorted(ids):
        try:
            op = operation(conn, identity)
        except ValueError:
            continue  # A different factual job/source revision is not this publication.
        if not op["intent"] or not current(conn, op):
            continue
        queue = queue_for(conn, op)
        if not set(queue_ref(queue)["affected_nm_ids"]) <= covered:
            continue
        proof = {"source_ref": ref(op), "queue_ref": queue_ref(queue), "version_id": version_id,
                 "plan_fingerprint": plan["plan_fingerprint"], "business_date": plan["effective_date"],
                 "wb_supply_source_digest": plan["wb_supply_source_digest"],
                 "cost_projection_digest": plan.get("operator_supplier_cost_projection_digest"),
                 "cost_state": cost_state(conn, version_id, op["shipment_id"]),
                 "balance_rows": balance_rows(conn, version_id, queue_ref(queue)["affected_nm_ids"]),
                 "daily_cost_rows": daily_rows(conn, queue, plan["effective_date"])}
        # The actual publisher pins the original native before-version, not a
        # later refresh's zero diff. This is dependency evidence, not a queue
        # flag or an acknowledgement that historical outputs are complete.
        origin = conn.execute(f"SELECT proof_json FROM {FUNCTIONAL} WHERE operation_id=? ORDER BY rowid LIMIT 1", (identity,)).fetchone()
        original = json.loads(origin[0]) if origin else {}
        proof["origin_before_version_id"] = original.get("origin_before_version_id", plan.get("base_active_version_id"))
        prior = conn.execute(f"SELECT proof_digest FROM {FUNCTIONAL} WHERE operation_id=? AND version_id=?", (identity, version_id)).fetchone()
        if prior and prior[0] != source.digest(proof):
            raise ValueError("supplier_functional_proof_conflict")
        conn.execute(f"INSERT OR IGNORE INTO {FUNCTIONAL} VALUES(?,?,?,?)", (identity, version_id, source._json(proof), source.digest(proof)))


def public_completion(conn, operation_id):
    """GET reads immutable retained completion, never computes native costs."""
    if not source._exists(conn, COMPLETIONS):
        return None
    row = conn.execute(f"SELECT * FROM {COMPLETIONS} WHERE operation_id=?", (operation_id,)).fetchone()
    if row:
        proof = json.loads(row["proof_json"])
        op = operation(conn, operation_id)
        if source.digest(proof) != row["proof_digest"] or proof["source_ref"] != ref(op):
            return {"state": "needs_attention", "reason_code": "supplier_completion_corrupt", "complete": False}
        effect = proof.get("effect", "current_cost_published")
        return {"state": "completed", "reason_code": ("supplier_derived_no_change" if effect == "derived_no_change" else "supplier_exact_cost_published"), "complete": True,
                "effect": effect,
                "completed_at": row["completed_at"], "proof_digest": row["proof_digest"],
                "calculation": {"version_id": proof["functional"]["version_id"],
                    "source_fingerprint": (proof["functional"]["cost_state"] or {}).get("source_fingerprint"),
                    "calculation_fingerprint": (proof["functional"]["cost_state"] or {}).get("calculation_fingerprint"),
                    "expenses_complete": bool(proof["functional"]["cost_state"]["expenses_complete"]) if proof["functional"]["cost_state"] else None,
                    "calculation_available": bool(proof["functional"]["cost_state"]["calculation_available"]) if proof["functional"]["cost_state"] else None,
                    "certification": proof["certification"],
                    "quality": proof["certification"]["status_code"] if proof["certification"] else "not_applicable"}}
    attempt = conn.execute(f"SELECT * FROM {ATTEMPTS} WHERE operation_id=?", (operation_id,)).fetchone()
    return {"state": attempt["state"], "reason_code": attempt["reason"], "complete": False,
            "terminal": attempt["reason"] == SUPERSEDED} if attempt else None


def _after_native_proof():
    """Inert production seam for deterministic inter-connection test commits."""


COMPLETION_COHORT_LIMIT = 8
COMPLETION_COHORT_MAX_BYTES = 8 * 1024**2


def _prepare_completion(runtime, operation_id, *, seller_id, now, stack, finance_cohort=None):
    from packages.application.operator_supplier_cost_proof import read_native_proof
    observer = stack.enter_context(closing(source.readonly(runtime.db_path)))
    op = operation(observer, operation_id)
    if not current(observer, op):
        raise ValueError("supplier_completion_source_changed")
    saved = public_completion(observer, operation_id)
    if saved and saved["complete"]:
        return {"saved": saved}
    observer.commit();observer.execute("BEGIN")
    handoff = []
    proof = read_native_proof(runtime, operation_id, seller_id=seller_id, now=now, connection=observer, handoff=handoff, stack=stack, finance_cohort=finance_cohort)
    _after_native_proof()
    return {'proof': proof, 'handoff': handoff, 'bytes': len(source._json(proof).encode())}


def _validate_completion(conn, operation_id, proof, handoff):
    # All source/main observer reads must precede the first receipt DML in a
    # cohort. Writer source CAS remains authoritative after that boundary.
    op = operation(conn, operation_id)
    if not current(conn, op) or ref(op) != proof["source_ref"]:
        raise ValueError("supplier_completion_source_changed")
    if any(observe(live) != token for live, observe, token in handoff):
        raise ValueError("supplier_completion_handoff_changed")
    evaluated = proof.get("native_cost_evaluation")
    if evaluated:
        from packages.application import ready_publication as ready, operator_supplier_history_candidate as candidate
        if evaluated["code_authority"] != candidate.code_authority():
            raise ValueError("supplier_history_formula_changed")
        from packages.application.fbs_accounting_historical_stages import check_query_fence
        check_query_fence(conn, evaluated["source_inputs"])
        check_query_fence(conn, evaluated["query_fence"])
        refs = evaluated["cohort_refs"]
        if ref(op) not in refs or any(ref(operation(conn, member["operation_id"])) != member
                or not current(conn, operation(conn, member["operation_id"])) for member in refs):
            raise ValueError("supplier_history_cohort_source_changed")
        own = evaluated["member_evaluation"].get(operation_id)
        if not own or (proof.get("effect") == "derived_no_change" and
                (not own["current_no_change"] or any(value["before"] != value["after"] for value in own["dated"].values()))):
            raise ValueError("supplier_history_own_effect_proof_changed")
        observed = evaluated["current_evaluation"]
        if (source.digest(candidate.business_image(balance_rows(conn, observed["before_version"], evaluated["queue_ref"]["affected_nm_ids"]))) != observed["before_digest"]
                or source.digest(candidate.business_image(balance_rows(conn, observed["after_version"], evaluated["queue_ref"]["affected_nm_ids"]))) != observed["after_digest"]):
            raise ValueError("supplier_history_native_cost_evaluation_changed")
    if proof.get('supplier_history'):
        from packages.application.operator_supplier_history import completed_proof
        historical=completed_proof(conn,ref(op));expected=proof['supplier_history']
        if not historical or historical['manifest']['manifest_digest']!=expected['manifest_digest'] or historical['ack']['native']!=expected['native_history'] or historical['manifest']['candidate']['effect_dates']!=expected['dates']:
            raise ValueError('supplier_history_completion_changed')
    queue = queue_for(conn, op)
    functional = proof["functional"]
    retained = conn.execute(f"SELECT proof_digest FROM {FUNCTIONAL} WHERE operation_id=? AND version_id=?", (operation_id, functional["version_id"])).fetchone()
    if not retained or retained[0] != source.digest(functional) or queue_ref(queue) != functional["queue_ref"]:
        raise ValueError("supplier_completion_native_identity_changed")
    publication = proof["publication"]
    published = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?", (publication["operation_id"], publication["attempt_id"])).fetchone()
    ready = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?", (publication["bundle_version"], publication["ready_as_of_date"])).fetchone()
    from packages.application.ready_publication import digest as ready_digest
    pointer = handoff[-1][0].execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()
    if (not published or published["state"] != "complete" or published["book_version"] != publication["book_version"]
            or published["after_digest"] != publication["after_digest"] or not ready or ready_digest(ready[0]) != publication["after_digest"]
            or not pointer or pointer[0] != publication["book_version"]):
        raise ValueError("supplier_completion_publication_changed")
    if any(observe(live) != token for live, observe, token in handoff):
        raise ValueError("supplier_completion_handoff_changed")


def _check_independent_handoff(runtime, handoff):
    """Never read an operational observer after DML/cache spill in its writer."""
    from pathlib import Path
    operational = Path(runtime.db_path).resolve()
    for live, observe, expected in handoff:
        owner = getattr(observe, '__self__', None)
        if owner is not None and hasattr(owner, 'check_independent'):
            owner.check_independent()
            continue
        paths = {str(row[1]): Path(row[2]).resolve() for row in live.execute('PRAGMA database_list') if row[1]!='temp'}
        if isinstance(expected, dict):
            for schema, path in paths.items():
                if path != operational and live.execute('PRAGMA '+schema+'.data_version').fetchone()[0] != expected[schema]:
                    raise ValueError('supplier_completion_handoff_changed')
        elif paths.get('main') != operational and observe(live) != expected:
            raise ValueError('supplier_completion_handoff_changed')


def _insert_completion(conn, operation_id, proof):
    conn.execute(f"INSERT OR IGNORE INTO {COMPLETIONS} VALUES(?,?,?,?)", (operation_id, source._json(proof), source.digest(proof), datetime.now(timezone.utc).isoformat()))


def record_completion(runtime, operation_id, *, seller_id="canonical", now=None):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    require_heavy_owner(runtime.runtime_dir);require_warehouse_job_owner(runtime.runtime_dir)
    with ExitStack() as stack:
        prepared = _prepare_completion(runtime, operation_id, seller_id=seller_id, now=now, stack=stack)
        if 'saved' in prepared:return prepared['saved']
        with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5), closing(sqlite3.connect(runtime.db_path, timeout=2)) as conn:
            conn.row_factory = sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
            _validate_completion(conn, operation_id, prepared['proof'], prepared['handoff'])
            _insert_completion(conn, operation_id, prepared['proof'])
            _check_independent_handoff(runtime, prepared['handoff'])
            conn.commit()
    return {'state': 'completed', 'complete': True}


def _record_attempt(conn, identity, exc):
    reason = str(exc).split(':')[0]
    state = 'processing' if reason.endswith(('_pending', '_changed', '_missing')) else 'needs_attention'
    if not conn.execute(f'SELECT 1 FROM {COMPLETIONS} WHERE operation_id=?', (identity,)).fetchone():
        try:retired = superseded(conn, operation(conn, identity))
        except ValueError:retired = False
        if retired:state, reason = 'needs_attention', SUPERSEDED
        prior = conn.execute(f'SELECT reason FROM {ATTEMPTS} WHERE operation_id=?', (identity,)).fetchone()
        if not prior or prior[0] != SUPERSEDED:
            conn.execute(f'INSERT OR REPLACE INTO {ATTEMPTS} VALUES(?,?,?,?)', (identity, state, reason, datetime.now(timezone.utc).isoformat()))
    return {'state': state, 'complete': False, 'reason_code': reason, 'terminal': reason == SUPERSEDED}


def reconcile(runtime, *, seller_id="canonical", now=None):
    from packages.application.business_data_heavy_admission import require_heavy_owner
    from packages.application.warehouse_functional_lock import require_warehouse_job_owner, warehouse_functional_write_lock
    from packages.application import operator_supplier_factual_dates as factual
    if not Path(getattr(runtime, "db_path", "")).is_file():
        return {"status": "no_op", "operations": []}
    with closing(source.readonly(runtime.db_path)) as conn:
        if not source._exists(conn, FUNCTIONAL):
            return {"status": "no_op", "operations": []}
        identities = captured_cohort(conn)
    if not identities:
        return {"status": "no_op", "operations": []}
    require_heavy_owner(runtime.runtime_dir);require_warehouse_job_owner(runtime.runtime_dir)
    from packages.application.operator_supplier_cost_proof import FinanceProofCohort
    results = [];position = 0
    while position < len(identities):
        prepared, failures, order, oversized = {}, {}, [], None
        with ExitStack() as stack:
            finance = FinanceProofCohort(runtime, seller_id=seller_id, stack=stack)
            size = 0
            while position < len(identities) and len(order) < COMPLETION_COHORT_LIMIT:
                identity = identities[position]
                try:
                    item = _prepare_completion(runtime, identity, seller_id=seller_id, now=now, stack=stack, finance_cohort=finance)
                    if item.get('bytes', 0) + size > COMPLETION_COHORT_MAX_BYTES:
                        oversized = identity
                        break
                    prepared[identity] = item;size += item.get('bytes', 0)
                except (ValueError, sqlite3.OperationalError) as exc:
                    failures[identity] = exc
                order.append(identity);position += 1
            try:finance.seal()
            except (ValueError, sqlite3.OperationalError) as exc:
                for identity, item in list(prepared.items()):
                    if 'saved' not in item:failures[identity] = exc;del prepared[identity]
            if order:
                values = {}
                try:
                    with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5), closing(sqlite3.connect(runtime.db_path, timeout=2)) as conn:
                        conn.row_factory = sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                        # Validate every main observer before any receipt/attempt
                        # DML. One invalid source must not starve valid siblings.
                        for identity in order:
                            item = prepared.get(identity)
                            if item is None:continue
                            if 'saved' in item:values[identity] = item['saved'];continue
                            try:_validate_completion(conn, identity, item['proof'], item['handoff'])
                            except (ValueError, sqlite3.OperationalError) as exc:failures[identity] = exc
                        for identity in order:
                            item = prepared.get(identity)
                            if identity in failures:
                                values[identity] = _record_attempt(conn, identity, failures[identity])
                            elif 'saved' not in item:
                                _check_independent_handoff(runtime, item['handoff'])
                                _insert_completion(conn, identity, item['proof'])
                                values[identity] = {'state':'completed', 'complete':True}
                        for identity in order:
                            item = prepared.get(identity)
                            if item and 'saved' not in item and identity not in failures:
                                _check_independent_handoff(runtime, item['handoff'])
                        conn.commit()
                except (ValueError, sqlite3.OperationalError) as exc:
                    # Independent raw/book drift invalidates this uncommitted
                    # cohort; persist retry outcomes after its rollback.
                    with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5), closing(sqlite3.connect(runtime.db_path, timeout=2)) as conn:
                        conn.row_factory = sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                        for identity in order:
                            item = prepared.get(identity)
                            values[identity] = item['saved'] if item and 'saved' in item else _record_attempt(conn, identity, failures.get(identity, exc))
                        conn.commit()
                results.extend({'operation_id':identity, **values[identity]} for identity in order)
        if oversized is not None:
            # A large valid source retains its existing single-operation path.
            # No shared observer/cache survives the preceding writer commit.
            try:value = record_completion(runtime, oversized, seller_id=seller_id, now=now)
            except (ValueError, sqlite3.OperationalError) as exc:
                with warehouse_functional_write_lock(runtime.runtime_dir, timeout_seconds=5), closing(sqlite3.connect(runtime.db_path, timeout=2)) as conn:
                    conn.row_factory = sqlite3.Row;conn.execute('BEGIN IMMEDIATE')
                    value = _record_attempt(conn, oversized, exc);conn.commit()
            results.append({'operation_id':oversized, **value});position += 1
    return {'status':'pending' if any(not value['complete'] for value in results) else 'ok', 'operations':results}
