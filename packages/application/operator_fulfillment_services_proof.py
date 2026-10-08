"""RO proof of one fulfillment action through native cost/accounting/Finance.

No flags, submitted evidence, publishers, constructors with schema bootstrap,
or universal cycle success stand in for exact saved target readback.
"""
from contextlib import closing, nullcontext
from datetime import date
from decimal import Decimal
import json

from packages.application import operator_fulfillment_services as receipt


def _finance(runtime, *, seller_id, queue_ref, shared_version, handoff=None, stack=None):
    from packages.application.wb_finance_weekly import (
        WbFinanceWeeklyBlock, _nomenclature_identity_index, _resolve_finance_nm_id, _operation_date,
    )
    block = WbFinanceWeeklyBlock(runtime.runtime_dir, seller_id=seller_id)
    owner = stack.enter_context(closing(block._connect_stale_cost_plan())) if stack is not None else None
    with nullcontext(owner) if owner is not None else closing(block._connect_stale_cost_plan()) as conn:
        block._assert_readonly_plan_connection(conn)
        before = block._sqlite_data_version_token(conn)
        conn.execute("BEGIN")
        aliases, ambiguous, _, _ = _nomenclature_identity_index(conn)
        targets = set()
        scope_ids = {str(nm) for nm in queue_ref["affected_nm_ids"]}
        effective = date.fromisoformat(queue_ref["effective_date"])
        rows = conn.execute("SELECT week_start,week_end,raw_json FROM wb_finance_weekly_raw_rows "
                            "WHERE seller_id=? AND week_end>=? ORDER BY week_start,rrd_id",
                            (seller_id, effective.isoformat()))
        for row in rows:
            operation = json.loads(row["raw_json"])
            nm, _, problem = _resolve_finance_nm_id(operation, alias_to_nm=aliases, ambiguous_aliases=ambiguous)
            if (problem and str(operation.get("docTypeName") or "").casefold() in {"продажа", "возврат"}
                    and Decimal(str(operation.get("quantity") or 0)) != 0):
                raise ValueError("fulfillment_finance_scope_unknown")
            if problem or str(nm) not in scope_ids:
                continue
            day, day_source = _operation_date(operation, date.fromisoformat(row["week_start"]))
            if day_source == "week_start_fallback":
                raise ValueError("fulfillment_finance_operation_date_unknown")
            if day >= effective:
                targets.add((seller_id, row["week_start"], row["week_end"]))
        if shared_version and (block.shared_cost_snapshot is None
                or block.shared_cost_snapshot.metadata()["version_id"] != shared_version):
            raise ValueError("fulfillment_finance_accounting_version_mismatch")
        dependency = block._finance_source_dependency_fingerprint(conn, target_keys=targets, force_reload=True)
        current = block._finance_target_images(conn, targets)
        expected = {}
        for _, start, end in sorted(targets):
            projection = block._build_week_target_projection(conn, week_start=date.fromisoformat(start), week_end=date.fromisoformat(end))
            if projection["coverage"]["unmatched_units"]:
                raise ValueError("fulfillment_finance_cost_incomplete")
            for table, image in projection["images"].items():
                expected.setdefault(table, {"columns": image["columns"], "rows": []})["rows"].extend(image["rows"])
        expected = block._canonicalize_finance_target_images(conn, expected)
        def business_image(images):
            result = {}
            for table, image in images.items():
                values = []
                for row in image["rows"]:
                    item = dict(zip(image["columns"], row))
                    for key in ("calculated_at", "checked_at"):
                        item.pop(key, None)  # native calculation clocks are not business operands
                    values.append({key: json.loads(value) if key.endswith("_json") and value else value
                                   for key, value in item.items()})
                if values:
                    result[table] = sorted(values, key=receipt.canonical)
            return result
        if business_image(current) != business_image(expected):
            raise ValueError("fulfillment_finance_target_projection_pending")
        result = {"status": "verified" if targets else "not_applicable", "seller_id": seller_id,
                "target_weeks": [list(key) for key in sorted(targets)],
                "source_dependency": dependency, "target_digest": receipt.digest(business_image(current)),
                "non_target_digest": block._finance_state_digest(conn, target_keys=targets, target_only=False)}
        conn.commit()  # End the pinned snapshot before checking the same live observer.
        if block._sqlite_data_version_token(conn) != before:
            raise ValueError("fulfillment_finance_read_snapshot_changed")
        if handoff is not None:
            handoff.append((conn, block._sqlite_data_version_token, before))
        return result


def read_native_proof(runtime, operation_id, *, seller_id="canonical", now=None, connection=None,
                      _handoff=None, _stack=None):
    """Read current exact action proof only. No bootstrap, delivery, or mutation."""
    from packages.application.shared_sku_cost_sources import capture_wb_component
    from packages.application import fbs_accounting_runtime as accounting, ready_publication
    owner = nullcontext(connection) if connection is not None else closing(receipt.readonly(runtime.db_path))
    with owner as conn:
        if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise ValueError("fulfillment_proof_query_only_required")
        token = lambda c: int(c.execute("PRAGMA main.data_version").fetchone()[0])
        before = token(conn)
        row = conn.execute(f"SELECT * FROM {receipt.TABLE} WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise ValueError("fulfillment_receipt_missing")
        source = json.loads(row["source_json"])
        if receipt._current(conn, row, source) != "current":
            raise ValueError("fulfillment_completion_source_changed")
        request, queue = receipt._request(conn, row)
        if request and request["status"] == "not_applicable":
            # Exact native eligibility terminal; source complete with no derived mutation.
            proof = {"source_ref": receipt._ref(row, source), "status": "not_applicable",
                     "reason_code": request["error"], "scope_digest": receipt.digest(json.loads(request["scope_json"]))}
            conn.commit()
            if token(conn) != before:
                raise ValueError("fulfillment_read_snapshot_changed")
            if _handoff is not None:
                _handoff.append((conn, token, before))
            return proof
        if not queue or queue["status"] != "complete" or queue.get("error"):
            raise ValueError("fulfillment_functional_publication_pending")
        saved = conn.execute(f"SELECT proof.* FROM {receipt.FUNCTIONAL_PROOFS} proof "
            "JOIN sheet_vitrina_v1_warehouse_functional_versions version ON version.version_id=proof.warehouse_version_id "
            "WHERE proof.operation_id=? ORDER BY version.published_at DESC,version.created_at DESC,version.version_id DESC LIMIT 1",
            (operation_id,)).fetchone()
        if not saved or receipt.digest(json.loads(saved["proof_json"])) != saved["proof_digest"]:
            raise ValueError("fulfillment_correlated_functional_proof_missing")
        functional = json.loads(saved["proof_json"])
        if (functional["source_digest"] != row["source_digest"]
                or functional["source_ref"] != receipt._ref(row, source)
                or functional["queue_ref"] != receipt._queue_ref(queue)
                or functional["overlay"] != receipt._overlay_evidence(conn, request)):
            raise ValueError("fulfillment_correlated_source_overlay_changed")
        version = conn.execute("SELECT * FROM sheet_vitrina_v1_warehouse_functional_versions WHERE version_id=?",
                               (functional["warehouse_version_id"],)).fetchone()
        if not version or version["status"] != "good" or version["plan_fingerprint"] != functional["plan_fingerprint"]:
            raise ValueError("fulfillment_functional_version_missing")
        day = functional["business_date"]
        ids = functional["queue_ref"]["affected_nm_ids"]
        daily = [dict(item) for item in conn.execute(
            "SELECT as_of_date,nm_id,quantity,wac_rub,capital_rub,quality,fingerprint "
            "FROM sheet_vitrina_v1_warehouse_wb_daily_cost WHERE cutover_id=? AND as_of_date>=? AND as_of_date<=? "
            "AND nm_id IN ("+','.join('?' for _ in ids)+") ORDER BY as_of_date,nm_id",
            ('warehouse_functional_cutover_v1',functional["queue_ref"]["effective_date"],day,*ids))]
        if daily != functional["daily_cost_rows"]:
            raise ValueError("fulfillment_dated_cost_publication_changed")
        wb = capture_wb_component(runtime.db_path, day=day, nm_ids=functional["queue_ref"]["affected_nm_ids"],
                                  version_id=version["version_id"], connection=conn)
        if not wb["complete"] or not wb["authority_complete"]:
            raise ValueError("fulfillment_exact_wb_authority_incomplete:" + wb["reason"])
        book, book_version = accounting.load(runtime.runtime_dir)
        if not book or not book["active"]:
            raise ValueError("fulfillment_accounting_not_active")
        bound = book["wb_days"].get(day)
        if (not bound or not bound["complete"] or bound["version_id"] != version["version_id"]
                or bound["source_digest"] != capture_wb_component(runtime.db_path, day=day,
                    nm_ids=bound["requested_nm_ids"], version_id=version["version_id"], connection=conn)["source_digest"]):
            raise ValueError("fulfillment_accounting_exact_version_pending")
        # A current green book cannot stand in for a changed closed historical day.
        # Current continuation is supported; an older changed cost needs its own
        # admitted dated authority/book revision from the closed-history owner.
        for item in daily:
            prior_day = item["as_of_date"]
            if prior_day < day and Decimal(item["quantity"]) > 0:
                prior = book["wb_days"].get(prior_day)
                if not prior or not prior["complete"]:
                    raise ValueError("fulfillment_historical_authority_required:"+prior_day)
                operand = next((r for r in prior["rows"] if r["nm_id"] == item["nm_id"]), None)
                if (not operand or operand["status"] != "available" or Decimal(operand["quantity"]) <= 0
                        or Decimal(operand["capital_rub"])/Decimal(operand["quantity"]) != Decimal(item["wac_rub"])):
                    raise ValueError("fulfillment_historical_publication_required:"+prior_day)
        economics = accounting.current_publication_receipt(runtime, now=now)
        if not economics or economics["accounting_version"] != book_version or economics["business_date"] != day:
            raise ValueError("fulfillment_accounting_ready_pending")
        published = conn.execute("SELECT * FROM sheet_vitrina_v1_ready_publications WHERE operation_id=? AND attempt_id=?",
                                 (economics["operation_id"], economics["attempt_id"])).fetchone()
        ready = conn.execute("SELECT plan_json FROM sheet_vitrina_v1_ready_snapshots WHERE bundle_version=? AND as_of_date=?",
                             (published["bundle_version"], published["as_of_date"])).fetchone() if published else None
        if (not published or published["state"] != "complete" or not ready
                or published["book_version"] != book_version or ready_publication.digest(ready[0]) != published["after_digest"]):
            raise ValueError("fulfillment_dated_ready_receipt_mismatch")
        shared_version = accounting.ActiveSharedCostSnapshot(list(book["shared_days"].values()),
            effective_date=book["effective_date"]).metadata()["version_id"]
        finance = _finance(runtime, seller_id=seller_id, queue_ref=functional["queue_ref"], shared_version=shared_version,
                           handoff=_handoff, stack=_stack)
        if accounting.load(runtime.runtime_dir)[1] != book_version:
            raise ValueError("fulfillment_accounting_readback_changed")
        proof = {"status": "verified", "source_ref": receipt._ref(row, source),
                "functional": functional, "wb_authority": {"source_digest": wb["source_digest"], "source": wb["source"]},
                "economics": economics, "finance": finance,
                "publication": {"operation_id": published["operation_id"], "attempt_id": published["attempt_id"],
                    "book_version": book_version, "bundle_version": published["bundle_version"],
                    "ready_as_of_date": published["as_of_date"], "business_date": day,
                    "after_digest": published["after_digest"], "finished_at": published["finished_at"]}}
        conn.commit()
        if token(conn) != before:
            raise ValueError("fulfillment_read_snapshot_changed")
        if _handoff is not None:
            _handoff.append((conn, token, before))
            book_conn = _stack.enter_context(closing(receipt.readonly(accounting.path(runtime.runtime_dir))))
            book_before = token(book_conn)
            pointer = book_conn.execute("SELECT version FROM accounting_current WHERE singleton=1").fetchone()[0]
            book_conn.commit()
            if pointer != book_version or token(book_conn) != book_before:
                raise ValueError("fulfillment_accounting_readback_changed")
            _handoff.append((book_conn, token, book_before))
        return proof
