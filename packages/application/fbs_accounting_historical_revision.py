"""Pure, unconnected historical receipt revision planning.

No IO, clock, writer, runtime constructor or policy activation occurs here.
The caller supplies an unpacked admitted book and a capture_current v2 capture
augmented with ``posted_manifest_json_by_id`` from the SAME read snapshot.
Those exact persisted strings are required for every captured document: the
normal adapter intentionally omits them after checking their bytes.

``captured_at`` is observation metadata, not a cost operand. Source material
includes the business day, original posted_at, source revision, quantity
snapshot identity/timestamps and exact document bytes. Validation accepts a
later observation of identical material, but never an earlier frontier. It
replays using the plan's pinned observation time, so a harmless repeated read
does not change the numerical candidate or its hash. This is not authority
to publish: a future writer must bind confirmation, storage/code authority,
expected book/ready targets, recovery and downstream receipts separately.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import islice
import json
from typing import Iterable

from packages.application.fbs_snapshot_cost import (
    POLICY, SCHEMA, _capture, canonical, close_candidate_period,
    evaluate_candidate, fingerprint,
)
from packages.application.fbs_snapshot_cost_sources import _events, _verified_manifest
from packages.application.fbs_inventory_presentation import FbsInventorySnapshot
from packages.application.shared_sku_cost import build_shared_cost_day

CONTRACT = "fbs_accounting_historical_receipt_revision_plan_v1"
BOOK_SCHEMA = "fbs_active_snapshot_accounting_v1"
MAX_DAYS = 366
MAX_DOCUMENTS = 10000
MAX_LOCATIONS = 2048
MAX_BOOK_BYTES = 64 * 1024**2
MAX_CAPTURE_BYTES = 32 * 1024**2
MAX_PLAN_BYTES = 128 * 1024**2
RECEIPT_KINDS = frozenset({"china_acceptance", "transfer_receipt"})
HEADER_FIELDS = (
    "document_id", "document_kind", "root_document_id", "operation_id",
    "source_system", "source_type", "source_id", "source_revision",
    "idempotency_epoch", "business_date", "posted_at", "posted_manifest_sha256",
)
BOOK_DAY_FIELDS = ("shared_days", "wb_days", "retained_days", "presentations")


class HistoricalRevisionError(ValueError):
    """A bounded, explicit refusal; never permission to bypass another guard."""

    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code, self.details = code, details


def _require(condition, code, **details):
    if not condition:
        raise HistoricalRevisionError(code, **details)


def _time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.tzinfo is not None, "observation_timezone_missing")
    return result


def _date(value):
    _require(type(value) is str and date.fromisoformat(value).isoformat() == value,
             "business_date_invalid")
    return date.fromisoformat(value)


def _bounded(value, limit, code):
    _require(len(canonical(value).encode("utf-8")) <= limit, code)


def _material(capture):
    # Mirrors the adapter's source_digest exactly; baseline_costs are NOT
    # admitted again. Exact bytes are an additional independent proof.
    payload = {key: capture[key] for key in (
        "contract", "business_date", "quantity_snapshot", "documents_complete",
        "documents", "documents_reason",
    )}
    _require(capture["source_digest"] == fingerprint(payload), "capture_source_digest_mismatch")
    result = {"capture": payload, "posted_manifest_json_by_id": capture["posted_manifest_json_by_id"],
              "current_dated_inputs": capture.get("current_dated_inputs")}
    if "native_requests_by_id" in capture:
        result["native_requests_by_id"] = {i: {k: v for k, v in r.items() if k != "state"}
                                           for i, r in capture["native_requests_by_id"].items()}
    if "posted_actors_by_id" in capture:
        result["posted_actors_by_id"] = capture["posted_actors_by_id"]
    return result


def _verify_request_operands(doc, capture):
    """Bind every NEW cohort document to its exact native request authority.

    B/C's adapter reads these request bytes on the SAME source snapshot. Native
    expense rows are not serialized in posted.lines: original request expenses
    (or the native overhead domain's single generated expense) own them.
    """
    from packages.application.ff_pool_documents import _expense_lines, _cents_text
    posted = json.loads(capture["posted_manifest_json_by_id"][doc["document_id"]])
    request = capture.get("native_requests_by_id", {}).get(posted["request_id"])
    _require(type(request) is dict and request.get("state") in {"posted", "replay", "complete"}
             and request.get("actor") and request.get("accepted_at") and request.get("posted_at")
             and request.get("request_identity"), "native_cohort_request_authority_missing", document_id=doc["document_id"])
    _require(all(request[k] == doc[k] for k in ("source_system", "source_type", "source_id", "source_revision",
                 "idempotency_epoch", "business_date")) and request["request_id"] == posted["request_id"]
             and request["posted_document_id"] == posted["primary_document_id"]
             and request["posted_at"] == doc["posted_at"]
             and _time(request["accepted_at"]) <= _time(doc["posted_at"]),
             "native_cohort_request_identity_mismatch", document_id=doc["document_id"])
    _require(capture.get("posted_actors_by_id", {}).get(doc["document_id"]) == request["actor"],
             "native_cohort_actor_mismatch", document_id=doc["document_id"])
    original = {k: v for k, v in posted.items() if k not in {"document_id", "document_role", "lines"}}
    original["document_kind"] = request["document_kind"]
    _require(request["posted_manifest_sha256"] == fingerprint(original), "native_cohort_request_manifest_mismatch")
    manifest, domain = json.loads(request["request_payload_json"]), posted.get("domain", {})
    kind = doc["kind"]
    if doc["document_id"] != posted["primary_document_id"]:
        expenses = []
    elif kind == "pool_overhead":
        from packages.application.ff_pool_documents import OVERHEAD_EXPENSE_CATEGORY_LABELS_RU
        category = manifest["category"]
        label, comment = OVERHEAD_EXPENSE_CATEGORY_LABELS_RU[category], str(manifest.get("comment") or "").strip()
        amount = _cents_text(int(Decimal(str(manifest["amount_rub"])) * 100))
        _require(Decimal(amount) == Decimal(str(manifest["amount_rub"]))
                 and all(domain[k] == manifest[k] for k in ("facility_id", "scope", "category", "source_mode"))
                 and domain["amount_rub"] == amount and domain["category_label_ru"] == label
                 and domain["comment"] == comment, "native_overhead_request_domain_mismatch")
        payment = manifest.get("payment_evidence") or {}
        _require(domain.get("payment_evidence", {}) == payment, "native_overhead_payment_evidence_mismatch")
        expenses = [{"amount_cents": int(Decimal(amount) * 100), "basis": label + (": " + comment if comment else ""),
                     "source_file_sha256": request["source_sha256"], "metadata": {
                         "category": category, "category_label_ru": label, "comment": comment,
                         "source_mode": manifest["source_mode"],
                         "payment_fingerprint": str(payment.get("payment_fingerprint") or ""),
                         "fingerprint_version": str(payment.get("fingerprint_version") or ""),
                         "parser_version": str(payment.get("parser_version") or "")}}]
    else:
        expenses = _expense_lines(manifest.get("expenses") or [])
    expected = [{"document_id": doc["document_id"], "expense_line_no": n,
                 "amount_rub": _cents_text(v["amount_cents"]), "basis": v["basis"],
                 "source_file_sha256": v["source_file_sha256"], "metadata_json": canonical(v["metadata"])}
                for n, v in enumerate(expenses, 1)]
    actual = [{**v, "metadata_json": canonical(json.loads(v["metadata_json"]))}
              for v in doc["cost_document"]["expense_lines"]]
    _require(expected == actual, "native_cohort_expense_operand_mismatch", document_id=doc["document_id"])


def _verify_native_operands(header, raw, posted):
    """Bind DB operands to the actual native writer's immutable serialization.

    Ordinary _apply_plan numbers lines by position and serializes money as
    decimal text and metadata as a JSON object. Exact cutover opening predates
    those lines: it writes allocations to movement rows, not document lines.
    Neither contract can be inferred from an arbitrary line-free manifest.
    """
    identity = header["document_id"]
    contract = posted.get("contract_name")
    if contract == "ff_pool_exact_opening_v1":
        _require(header["document_kind"] == "facility_pool_opening"
                 and header["root_document_id"] == identity
                 and header["source_system"] == "ff_pool_cutover"
                 and header["source_type"] == "cutover_manifest"
                 and posted.get("feature_epoch") == header["idempotency_epoch"]
                 and raw["lines"] == raw["expense_lines"] == raw["relations"] == [],
                 "opening_posted_operand_contract_mismatch", document_id=identity)
        allocations = posted.get("allocations")
        _require(type(allocations) is list, "opening_posted_allocation_proof_missing", document_id=identity)
        expected = [{"operation_id": header["operation_id"], "line_no": v["line_no"],
                     "facility_id": v["facility_id"], "pool": v["pool"], "nm_id": v["nm_id"],
                     "quantity_delta": v["quantity"], "capital_delta_rub": v["capital_rub"],
                     "metadata_json": canonical({"allocation_digest": v["allocation_digest"]})}
                    for v in allocations]
        _require(raw["movements"] == expected, "opening_posted_allocation_mismatch", document_id=identity)
        return None
    _require(contract == "ff_pool_business_documents_v1" and type(posted.get("lines")) is list,
             "document_posted_line_proof_missing_or_unsupported", document_id=identity)
    _require(all(posted.get(key) == header[key] for key in
                 ("document_id", "document_kind", "root_document_id", "business_date"))
             and type(posted.get("source")) is dict and header["operation_id"] == identity,
             "document_posted_header_proof_missing", document_id=identity)
    _require(all(v["operation_id"] == header["operation_id"] and v["line_no"] == n
                 for n, v in enumerate(raw["movements"], 1))
             and all(v["document_id"] == identity and v["expense_line_no"] == n
                     for n, v in enumerate(raw["expense_lines"], 1)),
             "document_native_normalized_identity_mismatch", document_id=identity)
    expected = []
    for number, line in enumerate(posted["lines"], 1):
        _require(type(line) is dict and type(line.get("metadata")) is dict
                 and type(line.get("nm_id")) is int and type(line.get("quantity")) is int
                 and type(line.get("capital_rub")) is str and type(line.get("expense_rub")) is str,
                 "document_posted_line_contract_mismatch", document_id=identity, line_no=number)
        expected.append({"document_id": identity, "line_no": number,
            **{key: line[key] for key in ("line_role", "facility_id", "pool", "nm_id", "quantity", "capital_rub", "expense_rub")},
            "metadata_json": canonical(line["metadata"])})
    # Normalize metadata JSON only: whitespace/key order are not money operands.
    # Money text and position/role/IDs use the writer's exact emitted values.
    actual = [{**v, "metadata_json": canonical(json.loads(v["metadata_json"]))} for v in raw["lines"]]
    _require(canonical(actual) == canonical(expected), "document_posted_line_operand_mismatch", document_id=identity)
    cohort = posted.get("documents")
    _require(type(cohort) is list and cohort and type(posted.get("request_id")) is str
             and posted["request_id"] and type(posted.get("document_role")) is str
             and posted["document_role"], "document_posted_request_proof_missing", document_id=identity)
    _require(len({v["document_id"] for v in cohort}) == len(cohort), "document_posted_request_member_duplicate", document_id=identity)
    own = [v for v in cohort if v["document_id"] == identity]
    _require(len(own) == 1, "document_posted_request_member_missing", document_id=identity)
    relation = own[0].get("relation")
    expected_relations = [] if relation is None else [{**relation, "child_document_id": identity,
                                                      "root_document_id": header["root_document_id"]}]
    _require(own[0] == {"document_id": identity, "document_kind": header["document_kind"],
              "document_role": posted["document_role"], "root_document_id": header["root_document_id"],
              "relation": relation, "line_count": len(actual), "movement_count": len(raw["movements"]),
              "expense_line_count": len(raw["expense_lines"])}
             and raw["relations"] == expected_relations,
             "document_posted_request_member_mismatch", document_id=identity)
    return {k: v for k, v in posted.items() if k not in {"document_id", "document_kind", "document_role", "root_document_id", "lines"}}


def _verify_documents(capture):
    _, _, documents = _capture(capture)
    proofs = capture["posted_manifest_json_by_id"]
    _require(type(proofs) is dict and set(proofs) == set(documents), "document_exact_bytes_scope_mismatch")
    _require(len(documents) <= MAX_DOCUMENTS, "document_scope_limit")
    operations, sources = set(), {}
    for identity, doc in documents.items():
        header = {key: doc[key] for key in HEADER_FIELDS}
        _require(doc["kind"] == header["document_kind"], "document_kind_mismatch", document_id=identity)
        _require(all(header[key] for key in ("operation_id", "source_system", "source_type",
                                           "source_id", "source_revision", "root_document_id")),
                 "document_source_identity_missing", document_id=identity)
        source = tuple(header[key] for key in ("source_system", "source_type", "source_id",
                                              "source_revision", "idempotency_epoch"))
        _require(header["operation_id"] not in operations,
                 "document_operation_identity_duplicate", document_id=identity)
        operations.add(header["operation_id"])
        _require(type(proofs[identity]) is str, "document_exact_bytes_missing", document_id=identity)
        posted = _verified_manifest({**header, "posted_manifest_json": proofs[identity]})
        raw = doc["cost_document"]
        _require(raw["contract"] == "ff_pool_posted_cost_document_v1"
                 and raw["posted_manifest_sha256"] == header["posted_manifest_sha256"]
                 and raw["domain"] == posted.get("domain", {}),
                 "document_domain_proof_mismatch", document_id=identity)
        owner = _verify_native_operands(header, raw, posted)
        sources.setdefault(source, []).append((header, owner))
        normalized = {"document": header, "lines": raw["lines"],
                      "expenses": raw["expense_lines"], "relations": raw["relations"],
                      "movements": raw["movements"]}
        _require(doc["fingerprint"] == fingerprint(normalized), "document_fingerprint_mismatch", document_id=identity)
        _require(doc["events"] == _events(header, raw["lines"], raw["expense_lines"], raw["movements"]),
                 "document_events_proof_mismatch", document_id=identity)
        _require(_time(doc["posted_at"]) <= _time(capture["captured_at"]),
                 "document_after_observation", document_id=identity)
    for group in sources.values():
        first_header, first_owner = group[0]
        if first_owner is None:
            _require(len(group) == 1, "document_request_source_ownership_conflict")
            continue
        _require(first_owner is not None and all(owner == first_owner
                 and header["root_document_id"] == first_header["root_document_id"]
                 and header["business_date"] == first_header["business_date"]
                 for header, owner in group), "document_request_source_ownership_conflict")
        member_ids = {v["document_id"] for v in first_owner["documents"]}
        _require(member_ids == {header["document_id"] for header, _ in group},
                 "document_request_source_cohort_mismatch")
        _require(first_owner["primary_document_id"] in member_ids,
                 "document_request_primary_identity_missing")
    return documents


def _manifest_known(state):
    known = dict(state["baseline"]["absorbed_documents"])
    for day, period in sorted(state["periods"].items()):
        for identity, value in period["applied_documents"].items():
            _require(identity not in known or known[identity] == value,
                     "book_document_revision_conflict", document_id=identity, business_date=day)
            known[identity] = value
    return known


def _verify_receipt(doc):
    raw = doc["cost_document"]
    roles = {"china_acceptance": "accepted_pool_allocation", "transfer_receipt": "received"}
    lines = raw["lines"]
    _require(lines and all(v["line_role"] == roles[doc["kind"]]
                           and Decimal(str(v["quantity"])) > 0 for v in lines),
             "receipt_positive_lines_required", document_id=doc["document_id"])
    if doc["kind"] == "china_acceptance":
        _require(all(Decimal(str(v["capital_rub"])) + Decimal(str(v["expense_rub"])) > 0
                     for v in lines), "receipt_independent_cost_missing", document_id=doc["document_id"])


def _prefix(book, first):
    return {"baseline": book["state"]["baseline"],
            "periods": {d: p for d, p in book["state"]["periods"].items() if d < first},
            **{field: {d: v for d, v in book[field].items() if d < first}
               for field in BOOK_DAY_FIELDS}}


def _changed(before, after):
    return {key: {"before": deepcopy(before.get(key)), "after": deepcopy(after.get(key)),
                  "before_digest": fingerprint(before.get(key)),
                  "after_digest": fingerprint(after.get(key))}
            for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}


def _presentation_metrics(payload):
    if payload is None:
        return {}
    instance = FbsInventorySnapshot.__new__(FbsInventorySnapshot)
    return {"TOTAL": instance._metrics(payload),
            **{f"SKU:{nm}": instance._metrics(payload, nm) for nm in payload["rows"]}}


def _day_targets(before, after, day):
    old = before["state"]["periods"].get(day)
    new = after["state"]["periods"][day]
    old_rows = old["rows"] if old else {}
    old_auxiliary = old["document_cost_state"] if old else {"fbo_rows": {}, "transfers": {}}
    old_valuation = old["document_valuation"] if old else {"allocations": []}
    old_shared = before["shared_days"].get(day, {})
    old_presentation = before["presentations"].get(day)
    result = {
        "fbs_locations": _changed(old_rows, new["rows"]),
        "fbo_locations": _changed(old_auxiliary["fbo_rows"], new["document_cost_state"]["fbo_rows"]),
        "transfer_references": _changed(old_auxiliary["transfers"], new["document_cost_state"]["transfers"]),
        "allocations": _changed({v["document_id"]: v for v in old_valuation["allocations"]},
                                {v["document_id"]: v for v in new["document_valuation"]["allocations"]}),
        "before_document_valuation_digest": fingerprint(old["document_valuation"] if old else None),
        "after_document_valuation_digest": fingerprint(new["document_valuation"]),
        "shared_skus": _changed(old_shared.get("rows", {}), after["shared_days"][day]["rows"]),
        "presentation_skus": _changed(old_presentation["rows"] if old_presentation else {}, after["presentations"][day]["rows"]),
        "presentation_metrics": _changed(_presentation_metrics(old_presentation),
                                          _presentation_metrics(after["presentations"][day])),
        "before_period_digest": fingerprint(old), "after_period_digest": fingerprint(new),
        "before_shared_version": old_shared.get("version_id"),
        "after_shared_version": after["shared_days"][day]["version_id"],
        "before_presentation_version": old_presentation.get("version_id") if old_presentation else None,
        "after_presentation_version": after["presentations"][day]["version_id"],
    }
    result["non_target_digest"] = fingerprint({
        category: {k: v for k, v in original.items() if k not in result[category]}
        for category, original in (
            ("fbs_locations", old_rows),
            ("fbo_locations", old_auxiliary["fbo_rows"]),
            ("transfer_references", old_auxiliary["transfers"]),
            ("shared_skus", old_shared.get("rows", {})),
            ("presentation_skus", old_presentation["rows"] if old_presentation else {}),
        )})
    return result


def _build(book, capture, receipt_ids, observation, current_ids=(), auxiliary_ids=()):
    _bounded(book, MAX_BOOK_BYTES, "book_size_limit")
    _bounded(capture, MAX_CAPTURE_BYTES, "capture_size_limit")
    _require(book.get("schema") == BOOK_SCHEMA and book.get("active") is True,
             "active_accounting_book_required")
    state = book["state"]
    _require(state.get("schema") == SCHEMA and state.get("policy") == POLICY
             and state.get("candidate_only") is True, "accounting_policy_mismatch")
    _require(not book.get("publication_error"), "book_publication_error")
    baseline = state["baseline"]
    _require(baseline["id"] == fingerprint({k: v for k, v in baseline.items() if k != "id"}), "baseline_digest_mismatch")
    _require(book["effective_date"] == baseline.get("calculation_start_date", baseline["business_date"]),
             "book_effective_date_mismatch")
    _require(capture.get("contract") == "fbs_snapshot_cost_sources_v2", "verified_source_capture_required")
    documents = _verify_documents(capture)
    material = _material(capture)
    _require(_time(capture["captured_at"]) >= _time(state["last_document_capture_at"]), "document_observation_time_regression")
    _require(_time(capture["captured_at"]) >= _time(observation)
             and _time(observation) >= _time(state["last_document_capture_at"]), "plan_observation_frontier_regression")
    _require(receipt_ids and all(type(i) is str and i for i in receipt_ids)
             and len(receipt_ids) == len(set(receipt_ids)), "receipt_identity_scope_invalid")
    known = _manifest_known(state)
    for identity, digest in known.items():
        _require(identity in documents and documents[identity]["fingerprint"] == digest,
                 "absorbed_document_missing_or_changed", document_id=identity)
    for identity, seen in state["observed_documents"].items():
        _require(identity in documents and documents[identity]["fingerprint"] == seen["fingerprint"],
                 "observed_document_missing_or_changed", document_id=identity)
    for identity in receipt_ids:
        _require(identity in documents, "receipt_document_missing", document_id=identity)
        _require(identity not in known, "receipt_already_absorbed", document_id=identity)
        _require(documents[identity]["kind"] in RECEIPT_KINDS, "receipt_kind_not_supported", document_id=identity)
        _verify_receipt(documents[identity])
    for ids in (current_ids, auxiliary_ids):
        _require(len(ids) == len(set(ids)) and all(type(i) is str and i for i in ids), "cohort_identity_scope_invalid")
    selected = set(receipt_ids) | set(current_ids) | set(auxiliary_ids)
    _require(len(selected) == len(receipt_ids) + len(current_ids) + len(auxiliary_ids), "cohort_identity_overlap")
    _require(set(documents) - set(known) == selected, "unapproved_new_document_scope",
             document_ids=sorted(set(documents) - set(known) - selected))
    for identity in current_ids:
        _require(identity not in known and documents[identity]["business_date"] == capture["business_date"],
                 "additional_current_source_date_mismatch", document_id=identity)
    for identity in auxiliary_ids:
        posted = json.loads(capture["posted_manifest_json_by_id"][identity])
        owners = [json.loads(capture["posted_manifest_json_by_id"][i]) for i in receipt_ids]
        _require(any(posted["request_id"] == owner["request_id"] and identity in {v["document_id"] for v in owner["documents"]}
                     for owner in owners), "auxiliary_receipt_source_not_selected", document_id=identity)
    if current_ids or auxiliary_ids or "native_requests_by_id" in capture:
        for identity in selected:
            _verify_request_operands(documents[identity], capture)
    first = min(documents[i]["business_date"] for i in receipt_ids)
    start = baseline.get("calculation_start_date")
    _require(_date(first) >= _date(start) if start else _date(first) > _date(baseline["business_date"]),
             "receipt_before_accepted_opening", business_date=first)
    periods = state["periods"]
    _require(periods, "historical_periods_missing")
    saved_last, last = max(periods), capture["business_date"]
    advance = (_date(last) - _date(saved_last)).days
    _require(advance >= 0, "capture_book_day_mismatch", business_date=saved_last)
    _require(advance <= 1, "historical_period_gap", business_date=(date.fromisoformat(saved_last) + timedelta(days=1)).isoformat())
    _require(saved_last == max(book["shared_days"]) == max(book["presentations"]), "book_tail_binding_mismatch")
    _require(periods[saved_last]["status"] == "open", "current_open_period_required")
    for identity in receipt_ids:
        day = documents[identity]["business_date"]
        _require(day in periods and periods[day]["status"] == "closed", "receipt_closed_date_required", business_date=day)
    count = (_date(last) - _date(first)).days + 1
    _require(1 <= count <= MAX_DAYS and len(periods) <= MAX_DAYS, "historical_date_scope_limit")
    days = [(date.fromisoformat(first) + timedelta(days=n)).isoformat() for n in range(count)]
    prefix_start = start or (date.fromisoformat(baseline["business_date"]) + timedelta(days=1)).isoformat()
    expected_prefix = (date.fromisoformat(first) - date.fromisoformat(prefix_start)).days
    _require(0 <= expected_prefix <= MAX_DAYS, "historical_prefix_scope_limit")
    _require(sorted(d for d in periods if d < first) == [
        (date.fromisoformat(prefix_start) + timedelta(days=n)).isoformat() for n in range(expected_prefix)],
        "historical_prefix_gap")
    inputs = {}
    snapshots, wb_inputs, retained_inputs = {}, {}, {}
    for day in days:
        if day > saved_last:
            extra = capture.get("current_dated_inputs", {})
            _require(set(extra) == {"wb_capture", "retained_capture"}, "current_dated_inputs_missing", business_date=day)
            period, snapshot = None, capture["quantity_snapshot"]
            wb, retained = extra["wb_capture"], extra["retained_capture"]
        else:
            _require(day in periods, "historical_period_gap", business_date=day)
            period = periods[day]
            _require(period["status"] == ("open" if day == saved_last else "closed"), "historical_status_gap", business_date=day)
            snapshot = period.get("snapshot", {})
            _require(all(field in book and day in book[field] for field in BOOK_DAY_FIELDS),
                     "historical_dated_input_missing", business_date=day)
            wb, retained = book["wb_days"][day], book["retained_days"][day]
        _require(snapshot.get("complete") is True and snapshot.get("date") == day
                 and snapshot.get("id") and snapshot.get("digest") and snapshot.get("rows"),
                 "historical_quantity_source_missing", business_date=day)
        _require(len(snapshot["rows"]) <= MAX_LOCATIONS, "location_scope_limit", business_date=day)
        _require(wb.get("business_date") == day and wb.get("version_id") and wb.get("source_digest")
                 and wb.get("complete") is True and wb.get("authority_complete", True) is True,
                 "historical_wb_source_missing", business_date=day)
        _require(retained.get("date") == day and retained.get("wb_version_id") == wb["version_id"],
                 "historical_retained_source_missing", business_date=day)
        inputs[day] = {"quantity_snapshot": fingerprint(snapshot), "wb_capture": fingerprint(wb),
                       "retained_capture": fingerprint(retained), "before_period": fingerprint(period),
                       "source_role": "saved_same_date" if period else "exact_new_current_capture"}
        snapshots[day], wb_inputs[day], retained_inputs[day] = snapshot, wb, retained
    if advance == 0:
        _require(capture["quantity_snapshot"] == periods[last]["snapshot"], "current_quantity_frontier_changed")
    # Old observations may include the exact newly-seen receipt as pending. Do
    # not erase an unrelated diagnostic just to make the replay closable.
    _require(all(p.get("document_id") in set(receipt_ids) | set(auxiliary_ids) and p.get("reason") == "late_closed_period_document"
                 for p in state["pending_documents"]), "unrelated_pending_document")
    candidate = deepcopy(book)
    replay = deepcopy(state)
    replay["periods"] = {d: deepcopy(p) for d, p in periods.items() if d < first}
    _require(all(p["status"] == "closed" for p in replay["periods"].values()), "historical_prefix_not_closed")
    replay["pending_documents"] = []
    replay["last_document_capture_at"] = observation
    for day in days:
        local = {**capture, "captured_at": observation, "business_date": day,
                 "quantity_snapshot": deepcopy(snapshots[day])}
        # NEW current facts retain their original date. A future observed fact
        # is intentionally invisible before its date, not a missing source.
        if current_ids or auxiliary_ids:
            visible = {i for i, d in documents.items() if i in baseline["absorbed_documents"] or d["business_date"] <= day}
            local["documents"] = [deepcopy(d) for d in capture["documents"] if d["document_id"] in visible]
            replay["observed_documents"] = {i: v for i, v in replay["observed_documents"].items() if i in visible}
        local["source_digest"] = fingerprint({"historical_revision_material": fingerprint(material),
                                              "business_date": day, "quantity_snapshot": inputs[day]["quantity_snapshot"]})
        replay = evaluate_candidate(replay, local)
        _require(replay["last_attempt"].get("target_date", replay["last_attempt"]["date"]) == day
                 and replay["last_attempt"]["status"] == "evaluated" and day in replay["periods"],
                 "historical_replay_date_unavailable", business_date=day)
        _require(not replay["pending_documents"] and replay["periods"][day]["quality"] == "preliminary",
                 "historical_replay_incomplete", business_date=day, pending=deepcopy(replay["pending_documents"]))
        if day != last:
            replay = close_candidate_period(replay, day, today=last)
        shared = build_shared_cost_day(replay, wb_inputs[day], day)
        _require(shared["quality"] == "complete", "historical_shared_cost_incomplete", business_date=day)
        presentation = FbsInventorySnapshot(fbs_state=replay, wb_capture=wb_inputs[day],
                                            retained=retained_inputs[day], day=day).payload()
        _require(presentation["totals"]["total"]["capital_rub"] is not None,
                 "historical_presentation_incomplete", business_date=day)
        candidate["shared_days"][day] = shared
        candidate["presentations"][day] = presentation
        candidate["wb_days"][day] = deepcopy(wb_inputs[day])
        candidate["retained_days"][day] = deepcopy(retained_inputs[day])
    candidate.update(state=replay, prepared_at=observation, source_digest=capture["source_digest"])
    # Old publication inputs/authority must never authorize this candidate.
    # B must capture its own exact material and authority under its owner.
    candidate.pop("publication_inputs", None)
    candidate.pop("publication_authority", None)
    prefix = _prefix(book, first)
    _require(_prefix(candidate, first) == prefix, "immutable_prefix_changed")
    candidate["historical_revision"] = {"contract": CONTRACT, "before_book_digest": fingerprint(book),
        "material_digest": fingerprint(material), "receipt_document_ids": sorted(receipt_ids),
        "date_from": first, "date_to": last, "dated_inputs": inputs}
    targets = {d: _day_targets(book, candidate, d) for d in days}
    plan = {"contract": CONTRACT, "status": "ready", "candidate_only": True,
            "database_written": False, "receipt_document_ids": sorted(receipt_ids),
            "observation": {"captured_at": observation, "book_document_capture_at": state["last_document_capture_at"]},
            "source_proof": {"material_digest": fingerprint(material), "adapter_source_digest": capture["source_digest"],
                "document_manifest": {i: d["fingerprint"] for i, d in sorted(documents.items())},
                "receipt_sources": {i: {key: documents[i][key] for key in HEADER_FIELDS} for i in sorted(receipt_ids)}},
            "before_book_digest": fingerprint(book), "after_book_digest": fingerprint(candidate),
            "writer_requirements": ["confirmed_source_authority", "durable_owner_generation",
                                    "exact_current_book_cas", "fresh_publication_material_authority",
                                    "exact_retained_stage_applicability_or_revision_proof",
                                    "target_recovery", "dated_downstream_receipts"],
            "derived_stage_applicability": {
                "status": "not_proven_by_cost_planner",
                "source_set_digest": fingerprint({i: d["fingerprint"] for i, d in sorted(documents.items())}),
                "retained_digests_by_date": {d: inputs[d]["retained_capture"] for d in days},
                "rule": "exact_saved_digest_proves_identity_not_applicability_to_new_receipt_set",
                "stages": ["PRODUCTION", "PRODUCTION_TO_FF", "FF_TO_WB", "WB_ACCEPTANCE_DISCREPANCY"],
            },
            "immutable_prefix_digest": fingerprint(prefix), "dated_inputs": inputs,
            "scope": {"date_from": first, "date_to": last, "dates": days,
                      "replay_policy": POLICY, "affected_nm_ids": sorted({int(nm) for v in targets.values() for nm in v["shared_skus"]}),
                      "downstream_totals_required": True, "downstream_group_membership": "must_be_bound_by_publication_planner"},
            "targets": targets, "candidate_book": candidate}
    if current_ids or auxiliary_ids:
        plan["current_document_ids"], plan["auxiliary_document_ids"] = sorted(current_ids), sorted(auxiliary_ids)
        plan["cohort_date_policy"] = "exact_original_date_no_unselected_late_absorption"
    _bounded(plan, MAX_PLAN_BYTES, "plan_size_limit")
    return plan


def build_historical_revision_plan(book: dict, capture: dict, *, receipt_document_ids: Iterable[str],
                                   current_document_ids: Iterable[str] = (), auxiliary_document_ids: Iterable[str] = ()) -> dict:
    """Plan ONLY exact NEW receipts against saved same-date suffix operands.

    This plans valuations, FBO references, shared costs and presentations. WB
    and retained-stage captures stay byte-identical; this does not reconstruct
    missing physical movements or revise supplier stage source facts. A saved
    stage digest proves its identity, NOT applicability after a new receipt:
    publication must prove exact dated applicability or rebuild native stages
    to prevent counting the same goods/capital in transit and at FF. Unrelated
    NEW documents, current quantity frontier changes and missing old dates are
    explicit blocks, rather than silently expanding the authorized source set.
    """
    ids = []
    try:
        requested = list(islice(iter(receipt_document_ids), MAX_DOCUMENTS + 1))
        _require(len(requested) <= MAX_DOCUMENTS, "receipt_identity_scope_limit", limit=MAX_DOCUMENTS)
        ids = requested
        current = list(islice(iter(current_document_ids), MAX_DOCUMENTS + 1))
        auxiliary = list(islice(iter(auxiliary_document_ids), MAX_DOCUMENTS + 1))
        _require(len(current) + len(auxiliary) + len(ids) <= MAX_DOCUMENTS, "cohort_identity_scope_limit")
        plan = _build(book, capture, ids, capture["captured_at"], current, auxiliary)
    except HistoricalRevisionError as error:
        plan = {"contract": CONTRACT, "status": "blocked", "candidate_only": True,
                "database_written": False, "receipt_document_ids": ids,
                "blocker": {"code": error.code, **error.details}}
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        plan = {"contract": CONTRACT, "status": "blocked", "candidate_only": True,
                "database_written": False, "receipt_document_ids": ids,
                "blocker": {"code": "invalid_historical_input", "reason": str(error)[:200]}}
    plan["plan_fingerprint"] = fingerprint(plan)
    return plan


def validate_historical_revision_plan(plan: dict, *, book: dict, capture: dict) -> dict:
    """Independently reconstruct the numerical plan; caller hashes are not trust.

    A later read of identical source material is acceptable. A changed book,
    document set, source revision, same-date source or capture business day
    refuses the pinned plan. Freshly rebuilding a different candidate belongs
    to the caller, never an automatic publication retry here.
    """
    _require(plan.get("contract") == CONTRACT and plan.get("status") == "ready", "ready_revision_plan_required")
    _require(plan.get("plan_fingerprint") == fingerprint({k: v for k, v in plan.items() if k != "plan_fingerprint"}),
             "revision_plan_fingerprint_mismatch")
    try:
        fresh = _build(book, capture, plan["receipt_document_ids"], plan["observation"]["captured_at"],
                       plan.get("current_document_ids", ()), plan.get("auxiliary_document_ids", ()))
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        raise HistoricalRevisionError("revision_material_changed", reason=str(error)[:200]) from error
    fresh["plan_fingerprint"] = fingerprint(fresh)
    _require(fresh == plan, "revision_candidate_changed")
    return deepcopy(fresh)
