"""Query-only saved sources for the default-off FBS snapshot cost candidate.

Published facility costs may initialize a candidate once. Later captures expose
document money, never a replacement WAC or quantity from the legacy ledger.
No service constructor, schema creation or active consumer is used here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from packages.application.official_fbs_stock_read import read_complete_official_fbs_stock
from packages.application.own_product_capital import _inventory_cost_stage_evidence
from packages.application.warehouse_functional import _watermark
from packages.business_time import current_business_date_iso

CONTRACT = "fbs_snapshot_cost_sources_v2"
PREFIX = "sheet_vitrina_v1_"
ERRORS = (sqlite3.Error, ValueError, TypeError, KeyError, InvalidOperation)


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode()).hexdigest()


def _money(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_document_money")
    return result


def _quantity(value: Any) -> int:
    result = _money(value)
    if result != result.to_integral_value():
        raise ValueError("invalid_document_quantity")
    return int(result)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, args)]


def capture_current(db_path: Path, *, now: datetime, include_baseline: bool = True) -> dict[str, Any]:
    """Capture full-catalog stock, documents and optional baseline in one RO txn.

The exact document manifest is captured with a digest-matched published pool
state. A timestamp cutoff alone never establishes the initialization boundary.
    After initialization pass ``include_baseline=False``: the old pool quantity,
    WAC and published costs are then not queried at all.
"""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone_aware_now_required")
    day = current_business_date_iso(now)
    captured_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    result: dict[str, Any] = {
        "contract": CONTRACT, "business_date": day, "captured_at": captured_at,
        "quantity_snapshot": {"date": day, "captured_at": "", "id": "",
                              "digest": "", "complete": False, "rows": []},
        "documents_complete": False, "documents": [], "documents_reason": "",
        "baseline_costs": {"available": False, "version_id": "", "reason": (
            "sources_unavailable" if include_baseline else "not_requested_after_initialization"),
                           "rows": [], "fbo_rows": [], "document_manifest": {}},
    }
    conn = None
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        with localcontext() as context:
            context.prec = 50  # matches the published official FBS estimate
            try:
                stock = read_complete_official_fbs_stock(conn, universe=None, day=day, now=now)
                snapshot_rows = [
                    {"nm_id": int(nm), "facility_id": str(facility), "quantity": int(quantity)}
                    for nm, sku in sorted(stock["skus"].items())
                    for facility, quantity in sorted(sku["facilities"].items())
                ]
                if not snapshot_rows:
                    raise ValueError("empty_complete_official_stock")
                result["quantity_snapshot"] = {
                    "date": stock["date"], "captured_at": stock["captured_at"],
                    "id": stock["generation_id"], "digest": stock["generation_digest"],
                    "complete": True, "rows": snapshot_rows,
                    "source": stock["source"], "facility_evidence": stock["facility_evidence"],
                }
            except ERRORS as exc:
                result["quantity_snapshot"]["reason"] = str(exc)
            try:
                documents = _documents(conn)
                result.update(documents=documents, documents_complete=True)
            except ERRORS as exc:
                result["documents_reason"] = str(exc)
            if include_baseline and result["quantity_snapshot"]["complete"] and result["documents_complete"]:
                result["baseline_costs"] = _baseline(
                    conn, day=day, quantities=result["quantity_snapshot"]["rows"],
                    documents=result["documents"],
                )
    except ERRORS as exc:
        result["reason"] = str(exc)
    finally:
        if conn is not None:
            conn.close()
    # Re-reading identical facts changes capture time, not source identity.
    result["source_digest"] = _digest({
        "contract": CONTRACT, "business_date": day,
        "quantity_snapshot": result["quantity_snapshot"],
        "documents_complete": result["documents_complete"],
        "documents": result["documents"], "documents_reason": result["documents_reason"],
    })
    return result


def _documents(conn: sqlite3.Connection) -> list[dict]:
    # FBO's auxiliary book must remain completely document-owned. Unlike the
    # intentionally excluded FBS observer, an unmapped FBO movement is a source
    # contract change, not an operand to import from the old ledger.
    unmapped = conn.execute(
        f"SELECT 1 FROM {PREFIX}ff_pool_movement_lines m "
        f"LEFT JOIN {PREFIX}ff_pool_documents d ON d.operation_id=m.operation_id "
        "WHERE m.pool='FBO' AND d.document_id IS NULL LIMIT 1"
    ).fetchone()
    if unmapped is not None:
        raise ValueError("unmapped_fbo_document_movement")
    documents = _rows(conn, f"SELECT document_id,document_kind,root_document_id,operation_id,"
                      "source_system,source_type,source_id,source_revision,idempotency_epoch,business_date,posted_at,"
                      f"posted_manifest_sha256,posted_manifest_json FROM {PREFIX}ff_pool_documents ORDER BY document_id")
    lines = _rows(conn, f"SELECT document_id,line_no,line_role,facility_id,pool,nm_id,quantity,"
                  f"capital_rub,expense_rub,metadata_json FROM {PREFIX}ff_pool_document_lines "
                  "ORDER BY document_id,line_no")
    expenses = _rows(conn, f"SELECT document_id,expense_line_no,amount_rub,basis,source_file_sha256,"
                     f"metadata_json FROM {PREFIX}ff_pool_document_expense_lines "
                     "ORDER BY document_id,expense_line_no")
    relations = _rows(conn, f"SELECT parent_document_id,child_document_id,root_document_id,relation_type "
                      f"FROM {PREFIX}ff_pool_document_relations ORDER BY child_document_id,relation_type")
    movements = _rows(conn, f"SELECT m.operation_id,m.line_no,m.facility_id,m.pool,m.nm_id,"
                      f"m.quantity_delta,m.capital_delta_rub,m.metadata_json FROM {PREFIX}ff_pool_movement_lines m "
                      f"JOIN {PREFIX}ff_pool_documents d USING(operation_id) "
                      "ORDER BY m.operation_id,m.line_no")
    operations = {row["operation_id"] for row in conn.execute(
        f"SELECT o.operation_id FROM {PREFIX}warehouse_business_operations o "
        f"JOIN {PREFIX}ff_pool_documents d USING(operation_id)"
    )}
    by_id: dict[str, list[dict]] = {}
    by_expense: dict[str, list[dict]] = {}
    by_relation: dict[str, list[dict]] = {}
    by_movement: dict[str, list[dict]] = {}
    for row in lines:
        by_id.setdefault(row["document_id"], []).append(row)
    for row in expenses:
        by_expense.setdefault(row["document_id"], []).append(row)
    for row in relations:
        by_relation.setdefault(row["child_document_id"], []).append(row)
    for row in movements:
        by_movement.setdefault(row["operation_id"], []).append(row)
    result = []
    seen = set()
    for document in documents:
        identity = str(document["document_id"])
        if not identity or identity in seen or document["operation_id"] not in operations:
            raise ValueError("document_operation_identity_missing_or_duplicate")
        seen.add(identity)
        if not document["source_revision"] or not document["posted_manifest_sha256"]:
            raise ValueError("document_revision_missing")
        if date.fromisoformat(document["business_date"]).isoformat() != document["business_date"]:
            raise ValueError("invalid_document_business_date")
        if datetime.fromisoformat(document["posted_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("invalid_document_posted_at")
        own_lines = by_id.get(identity, [])
        own_expenses = by_expense.get(identity, [])
        own_movements = by_movement.get(document["operation_id"], [])
        posted = _verified_manifest(document)
        # Read the authoritative scope and total, not the old allocation's
        # nonzero SKU set or old pool split. Both-pool scope can have allocated
        # lines in only one pool in the legacy state.
        domain = posted.get("domain", {})
        if document["document_kind"] == "pool_overhead":
            if (not domain.get("facility_id") or domain.get("scope") not in {"FBS", "FBO", "both"}
                    or _money(domain.get("amount_rub")) <= 0):
                raise ValueError("overhead_authoritative_header_missing")
            if _money(domain["amount_rub"]) != sum(
                (_money(expense["amount_rub"]) for expense in own_expenses), Decimal(0)
            ):
                raise ValueError("overhead_authoritative_amount_mismatch")
        # Its verified hash owns the manifest bytes; do not duplicate potentially
        # large before-state payloads in every normalized document.
        document = {key: value for key, value in document.items() if key != "posted_manifest_json"}
        fingerprint = _digest({"document": document, "lines": own_lines,
                               "expenses": own_expenses, "relations": by_relation.get(identity, []),
                               "movements": own_movements})
        normalized = {**document, "kind": document["document_kind"], "fingerprint": fingerprint,
                      "events": _events(document, own_lines, own_expenses, own_movements),
                      "cost_document": {
                          "contract": "ff_pool_posted_cost_document_v1",
                          "domain": domain, "lines": own_lines, "expense_lines": own_expenses,
                          "relations": by_relation.get(identity, []), "movements": own_movements,
                          "posted_manifest_sha256": document["posted_manifest_sha256"],
                      }}
        result.append(normalized)
    if (set(by_id) | set(by_expense)) - seen:
        raise ValueError("orphan_document_lines")
    return result


def _verified_manifest(document: dict) -> dict:
    serialized = document["posted_manifest_json"]
    # The writer hashes and stores the same serialized bytes. Parsing changes
    # integer dict keys to strings: re-sorting them can change their order
    # (e.g. 999 before 1000 becomes "1000" before "999") and is NOT the writer's
    # fingerprint convention. Verify the exact saved bytes before decoding.
    exact_hash = "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    posted = json.loads(serialized)
    if not isinstance(posted, dict) or exact_hash != document["posted_manifest_sha256"]:
        raise ValueError("posted_document_manifest_hash_mismatch")
    if not isinstance(posted.get("domain", {}), dict):
        raise ValueError("posted_document_domain_invalid")
    # The exact opening contract predates the general document header. Validate
    # identities when present without inventing missing opening-manifest fields.
    for key in ("document_id", "document_kind", "root_document_id", "business_date"):
        if key in posted and posted[key] != document[key]:
            raise ValueError("posted_document_manifest_identity_mismatch")
    source = posted.get("source")
    if source is not None:
        if not isinstance(source, dict) or any(source.get(key) != document[column] for key, column in (
            ("system", "source_system"), ("type", "source_type"), ("id", "source_id"),
            ("revision", "source_revision"), ("idempotency_epoch", "idempotency_epoch"),
        )):
            raise ValueError("posted_document_manifest_source_mismatch")
    return posted


def _events(document: dict, lines: list[dict], expenses: list[dict], movements: list[dict]) -> list[dict]:
    kind = document["document_kind"]
    if kind == "pool_overhead":
        # Preserve documentary integrity, but legacy SKU shares were allocated
        # using old pool quantities. They cannot become new cost operands even
        # though the monetary document is immutable and its header is valid.
        if (not expenses or any(line["line_role"] != "overhead_allocation" for line in lines)
                or any(_money(line["capital_rub"]) != _money(line["expense_rub"]) for line in lines)
                or sum((_money(line["capital_rub"]) for line in lines), Decimal(0))
                != sum((_money(expense["amount_rub"]) for expense in expenses), Decimal(0))):
            raise ValueError("approved_expense_allocations_incomplete")
    if kind in {"china_acceptance", "pool_overhead"}:
        expected: dict[tuple, tuple[int, Decimal]] = {}
        for line in lines:
            key = (line["facility_id"], line["pool"], int(line["nm_id"]))
            quantity = _quantity(line["quantity"]) if kind == "china_acceptance" else 0
            capital = _money(line["capital_rub"])
            if kind == "china_acceptance":
                if line["line_role"] != "accepted_pool_allocation":
                    raise ValueError("unsupported_receipt_line_role")
                capital += _money(line["expense_rub"])
            prior_quantity, prior_capital = expected.get(key, (0, Decimal(0)))
            expected[key] = (prior_quantity + quantity, prior_capital + capital)
        actual: dict[tuple, tuple[int, Decimal]] = {}
        for movement in movements:
            key = (movement["facility_id"], movement["pool"], int(movement["nm_id"]))
            prior_quantity, prior_capital = actual.get(key, (0, Decimal(0)))
            actual[key] = (prior_quantity + _quantity(movement["quantity_delta"]),
                           prior_capital + _money(movement["capital_delta_rub"]))
        if not expected or expected != actual:
            raise ValueError("document_money_movement_mismatch")
    result = []
    for line in lines:
        if line["pool"] != "FBS":
            continue
        if not line["facility_id"] or int(line["nm_id"]) <= 0:
            raise ValueError("document_fbs_identity_missing")
        source = {key: document[key] for key in (
            "document_id", "operation_id", "root_document_id", "source_system", "source_type",
            "source_id", "source_revision", "posted_manifest_sha256",
        )}
        source["line_no"] = int(line["line_no"])
        event = {"nm_id": int(line["nm_id"]), "facility_id": str(line["facility_id"]),
                 "kind": "unsupported", "quantity": 0, "capital_rub": "0", "source": source,
                 "reason": "document_cost_policy_not_supported"}
        if kind == "china_acceptance" and line["line_role"] == "accepted_pool_allocation":
            quantity = _quantity(line["quantity"])
            value = _money(line["capital_rub"]) + _money(line["expense_rub"])
            if quantity <= 0 or value <= 0:
                raise ValueError("receipt_document_cost_missing")
            event.update(kind="receipt", quantity=quantity, capital_rub=format(value, "f"), reason="")
            source["value_policy"] = "independent_supplier_document_capital_plus_expense"
        elif kind == "pool_overhead":
            event["reason"] = "new_expense_allocation_basis_required"
            source["value_policy"] = "legacy_pool_allocation_excluded_from_candidate_cost"
        elif kind == "transfer_shipment" and line["line_role"] == "shipped":
            event.update(kind="outgoing", quantity=_quantity(line["quantity"]), reason="")
            source["value_policy"] = "official_snapshot_quantity_only_no_legacy_capital"
        elif kind in {"transfer_receipt", "transfer_cancellation", "pool_reallocation"}:
            event["reason"] = "new_source_cost_reference_required"
        elif (kind == "pool_inventory" and _quantity(line["quantity"]) == 0
              and _money(line["capital_rub"]) == 0 and _money(line["expense_rub"]) == 0
              and not any(m["pool"] == "FBS" and m["nm_id"] == line["nm_id"]
                          and m["facility_id"] == line["facility_id"] for m in movements)):
            continue  # explicit dense zero initialization has no cost event
        result.append(event)
    # A reallocation's document line names only its destination. Preserve the
    # source FBS outgoing fact without importing its frozen legacy capital.
    if kind == "pool_reallocation":
        for movement in movements:
            if movement["pool"] == "FBS" and int(movement["quantity_delta"]) < 0:
                result.append({"nm_id": int(movement["nm_id"]),
                               "facility_id": str(movement["facility_id"]), "kind": "outgoing",
                               "quantity": -int(movement["quantity_delta"]), "capital_rub": "0",
                               "source": {"document_id": document["document_id"],
                                          "line_no": movement["line_no"],
                                          "value_policy": "official_snapshot_quantity_only_no_legacy_capital"}})
    addressed = {(event["nm_id"], event["facility_id"]) for event in result}
    for movement in movements:
        key = (int(movement["nm_id"]), str(movement["facility_id"]))
        if movement["pool"] == "FBS" and key not in addressed:
            result.append({"nm_id": key[0], "facility_id": key[1], "kind": "unsupported",
                           "quantity": 0, "capital_rub": "0",
                           "reason": "fbs_movement_without_supported_document_line",
                           "source": {"document_id": document["document_id"],
                                      "operation_id": document["operation_id"],
                                      "source_revision": document["source_revision"],
                                      "line_no": movement["line_no"]}})
            addressed.add(key)
    return result


def _baseline(conn: sqlite3.Connection, *, day: str, quantities: list[dict], documents: list[dict]) -> dict:
    manifest = {document["document_id"]: document["fingerprint"] for document in documents}
    result = {"available": False, "version_id": "", "reason": "", "rows": [], "fbo_rows": [],
              "document_manifest": manifest, "document_manifest_digest": _digest(manifest)}
    try:
        published = _rows(conn, f"SELECT nm_id,json_extract(provenance_json,'$.functional_version_id') version_id "
                          f"FROM {PREFIX}warehouse_business_projection_current_rows WHERE as_of_date=?", (day,))
        # Official inventory also retains hidden SKUs. They need not have a
        # displayed projection row; every positive official key still requires
        # its exact cost below, while a zero key may have no known cost.
        versions = {row["version_id"] for row in published}
        if not published or len(versions) != 1 or not next(iter(versions)):
            raise ValueError("published_current_version_unavailable")
        version_id = next(iter(versions))
        result["version_id"] = version_id
        active = conn.execute(f"SELECT version_id FROM {PREFIX}warehouse_functional_active WHERE slot=1").fetchone()
        if active is None or active["version_id"] != version_id:
            raise ValueError("published_active_version_mismatch")
        version = conn.execute(f"SELECT source_watermarks_json FROM {PREFIX}warehouse_functional_versions "
                               "WHERE version_id=? AND status='good' AND business_effective_date=?",
                               (version_id, day)).fetchone()
        if version is None:
            raise ValueError("published_exact_date_good_version_missing")
        pool = _rows(conn, f"SELECT facility_id,pool,nm_id,projection_epoch,quantity,capital_rub,"
                     f"wac_rub,source_watermark,updated_at FROM {PREFIX}ff_pool_balances "
                     "ORDER BY facility_id,pool,nm_id")
        watermark = json.loads(version["source_watermarks_json"]).get("ff_pool_detail")
        if not pool or watermark != _watermark(pool, "updated_at"):
            raise ValueError("published_pool_detail_digest_mismatch")
        ff = _rows(conn, f"SELECT * FROM {PREFIX}warehouse_functional_balances "
                   "WHERE version_id=? AND warehouse_key='ff' ORDER BY nm_id", (version_id,))
        locations = {}
        for row in ff:
            evidence = _inventory_cost_stage_evidence(row, public_stage="FF")
            if _money(row["quantity"]) != _money(row["cost_covered_quantity"]):
                raise ValueError("published_ff_cost_coverage_incomplete")
            if evidence["location_status"] != "exact":
                if _money(row["quantity"]) == 0 and _money(row["capital_rub"]) == 0:
                    continue
                raise ValueError("published_ff_location_evidence_unavailable")
            for location in evidence["locations"]:
                key = (int(row["nm_id"]), location["facility_id"], location["pool"])
                if key in locations:
                    raise ValueError("published_ff_location_duplicate")
                locations[key] = location
        pool_index = {(int(row["nm_id"]), row["facility_id"], row["pool"]): row for row in pool}
        if len(pool_index) != len(pool):
            raise ValueError("current_pool_location_duplicate")
        for key, location in locations.items():
            actual = pool_index.get(key)
            if (actual is None or _money(actual["quantity"]) != _money(location["quantity"])
                    or _money(actual["capital_rub"]) != _money(location["capital_rub"])):
                raise ValueError("published_location_current_pool_mismatch")
        for row in pool:
            if row["pool"] != "FBO":
                continue
            key = (int(row["nm_id"]), row["facility_id"], "FBO")
            basis = locations.get(key)
            quantity, capital = _quantity(row["quantity"]), _money(row["capital_rub"])
            if ((quantity == 0) != (capital == 0)
                    or (quantity > 0 and (basis is None or basis["wac_rub"] is None))):
                raise ValueError("fbo_initial_published_cost_missing")
            # Positive operands belong to immutable published locations. Dense
            # zero rows are bound by the exact whole-pool digest even when the
            # zero aggregate had no published location array.
            result["fbo_rows"].append({
                "nm_id": int(row["nm_id"]), "facility_id": str(row["facility_id"]),
                "quantity": _quantity(basis["quantity"]) if basis else 0,
                "capital_rub": str(basis["capital_rub"]) if basis else "0",
                "wac_rub": basis["wac_rub"] if basis else None,
                "source": {"version_id": version_id, "pool_detail_digest": watermark["digest"],
                           "basis": "immutable_published_ff_location" if basis else "exact_published_pool_zero",
                           "document_manifest_digest": result["document_manifest_digest"]},
            })
        for row in quantities:
            basis = locations.get((row["nm_id"], row["facility_id"], "FBS"))
            wac = basis["wac_rub"] if basis else None
            if int(row["quantity"]) > 0 and (wac is None or _money(wac) <= 0):
                raise ValueError("positive_official_stock_without_published_facility_cost")
            result["rows"].append({"nm_id": row["nm_id"], "facility_id": row["facility_id"],
                                   "wac_rub": wac,
                                   "quality": "published_facility_cost" if wac else "zero_without_cost_basis",
                                   "source": {"version_id": version_id, "pool_detail_digest": watermark["digest"],
                                              "basis": "immutable_published_ff_location",
                                              "document_manifest_digest": result["document_manifest_digest"]}})
        result["available"] = True
    except ERRORS as exc:
        result.update(reason=str(exc), rows=[], fbo_rows=[])
    return result
