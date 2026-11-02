"""Reviewed, bounded physical handoffs using the existing FBS lifecycle.

This is a connection-owned part of an ordinary open-day allocation unit. It
neither starts nor commits a transaction, creates accounting days, changes WB,
advances the collector cursor, nor invents an earlier WB status observation.
The caller holds the warehouse admission, warehouse writer and accounting book
locks before BEGIN IMMEDIATE, and commits allocation/handoffs/return together.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from packages.adapters.wb_fbs_orders import WbFbsOrderStatus
from packages.application import ff_pool_fbs_lifecycle as lifecycle
from packages.application import wb_fbs_orders as orders
from packages.application.fbs_accounting_runtime import load, SCHEMA
from packages.application.fbs_snapshot_cost import fingerprint, canonical, _last_closed
from packages.application.ff_pool_documents import DOCUMENTS_TABLE, DOCUMENT_LINES_TABLE
from packages.application.ff_pool_foundation import (
    OPERATIONS_TABLE, LINES_TABLE, BALANCES_TABLE, FEATURE_EPOCHS_TABLE,
    canonical_decimal_text, canonical_decimal_ratio_text,
)
from packages.application.fbs_historical_fbo_native import capture_native_guard
from packages.application.operator_warehouse_documents import assert_reservations
from packages.application.warehouse_functional_lock import require_warehouse_job_owner

CONTRACT = "ff_pool_fbs_reviewed_historical_handoff_v1"


def require(value, code):
    if not value:
        raise ValueError("historical_fbs_handoff_" + code)


def _copy(value):
    return json.loads(canonical(value))


def _sha(value):
    return isinstance(value, str) and len(value) == 71 and value.startswith("sha256:") and all(
        c in "0123456789abcdef" for c in value[7:])


def read_reviewed_handoff_evidence(*, official_path, terminal_path, anchors_path, approval_reference):
    """Read the reviewed direct-anchor artifact and hash-check its primary files.

    This is local file IO only. A Raxs wave-created timestamp is deliberately
    ignored: the dated shipment header supplies the business date.
    """
    files = {}
    def read(path):
        path = Path(path)
        data = path.read_bytes()
        require(len(data) <= 4 * 1024**2, "primary_file_size_limit")
        files[path.name] = "sha256:" + hashlib.sha256(data).hexdigest()
        return data
    official = json.loads(read(official_path))
    terminal = json.loads(read(terminal_path))
    anchors = json.loads(read(anchors_path))
    require(isinstance(anchors, list) and len(anchors) == 5, "reviewed_anchor_artifact_required")
    directory = Path(anchors_path).parent
    groups = []
    for anchor in anchors:
        order_file, shipment_file = anchor["direct_anchor_file"], anchor["shipment_file"]
        require(Path(order_file).name == order_file and Path(shipment_file).name == shipment_file, "primary_file_name_invalid")
        order_text, shipment_text = read(directory / order_file).decode(), read(directory / shipment_file).decode()
        require(files[order_file] == "sha256:" + anchor["direct_anchor_sha256"]
                and files[shipment_file] == "sha256:" + anchor["shipment_sha256"], "reviewed_primary_file_changed")
        order_id, wave, shipment = int(anchor["anchor_order_id"]), str(anchor["raxs_wave_id"]), str(anchor["raxs_shipment_id"])
        require(f"link Description: {order_id}," in order_text and re.search(r"\btext " + re.escape(wave) + r"\s*$", order_text, re.M)
                and f"/client/shipments/{shipment}" in shipment_text and f"Волна №{wave}" in shipment_text,
                "direct_primary_identity_conflict")
        date = re.findall(r"text Дата и время:\s*\n[^\n]*text (\d{2}\.\d{2}\.\d{4})", shipment_text)
        require(len(date) == 1, "shipment_business_date_missing")
        supply = str(anchor["wb_supply_id"])
        selected = [row for row in official["orders"] if row.get("supplyId") == supply]
        require(sorted(int(row["id"]) for row in selected) == sorted(anchor["order_ids"]), "reviewed_group_order_identity_conflict")
        groups.append(dict(supply_id=supply, anchor_order_id=order_id, wave_id=wave, shipment_id=shipment,
            business_date=datetime.strptime(date[0], "%d.%m.%Y").date().isoformat(),
            authority="direct_order_wave_shipment", date_authority="shipment_or_mh3_business_date",
            order_source_file=order_file, shipment_source_file=shipment_file, date_source_file=shipment_file,
            quantity_by_nm=anchor["quantity_by_nm_id"]))
    return build_handoff_evidence(official, terminal, groups=groups, source_files=files, approval_reference=approval_reference)


def build_handoff_evidence(official, terminal, *, groups, source_files, approval_reference):
    """Normalize reviewed primary evidence, without copying browser dumps to Git.

    Each group is an explicitly reviewed direct order→wave→shipment binding.
    ``source_files`` pins content hashes; the caller obtains these from the
    captured primary files, rather than asserting a link from vector equality.
    Shipment business dates are supplied separately from wave-created times.
    """
    pages = official.get("pages", [])
    require(pages and pages[0]["cursor"] == 0 and all(
        right["cursor"] == left["next_cursor"] for left, right in zip(pages, pages[1:])), "page_chain_invalid")
    require(terminal.get("coverage_terminal") is True and terminal.get("returned") == 0
            and terminal.get("next_cursor") == 0 and terminal.get("orders") == []
            and terminal.get("statuses") == [] and terminal["cursor"] == pages[-1]["next_cursor"]
            and terminal["date_to"] == official["date_to"], "terminal_coverage_required")
    require(source_files and all(_sha(v) for v in source_files.values()) and approval_reference,
            "reviewed_primary_source_hashes_required")
    raw = {int(row["id"]): row for row in official["orders"]}
    statuses = {int(row.get("id", row.get("order_id", 0))): row for row in official["statuses"]}
    require(len(raw) == len(official["orders"]) == len(statuses) == len(official["statuses"]) == 33
            and set(raw) == set(statuses), "exact_33_order_scope_required")
    normalized = []
    # Evidence exports may use ISO offsets; native observations require UTC Z.
    # Normalize before deriving observation identities, retaining the instant
    # and fractional seconds without rewriting the reviewed source files.
    observed_time = datetime.fromisoformat(official["at"].replace("Z", "+00:00"))
    require(observed_time.tzinfo is not None, "observation_timezone_required")
    observed_at = observed_time.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    start = int(datetime.fromisoformat(official["date_from"].replace("Z", "+00:00")).timestamp())
    end = int(datetime.fromisoformat(official["date_to"].replace("Z", "+00:00")).timestamp())
    for oid, row in sorted(raw.items()):
        observation = orders._normalize_order(row, observed_at=observed_at, date_from=start, date_to=end, cursor=0)
        require(observation is not None and len(observation["skus"]) == 1, "exact_fbs_order_identity_required")
        status = statuses[oid]
        status = orders._normalize_status(WbFbsOrderStatus(oid, status.get("supplier_status", status.get("supplierStatus", "")),
            status.get("wb_status", status.get("wbStatus", ""))), order=observation, observed_at=observed_at)
        normalized.append({"order": observation, "status": status})
    group_index = {str(group["supply_id"]): _copy(group) for group in groups}
    require(len(group_index) == len(groups) == 5, "five_unique_dispatch_groups_required")
    assigned = []
    for supply, group in sorted(group_index.items()):
        require(group["authority"] == "direct_order_wave_shipment" and group["date_authority"] == "shipment_or_mh3_business_date",
                "direct_binding_and_shipment_date_required")
        require(group["order_source_file"] in source_files and group["shipment_source_file"] in source_files
                and group["date_source_file"] in source_files and group["wave_id"] and group["shipment_id"],
                "group_primary_sources_required")
        anchor = int(group["anchor_order_id"])
        require(anchor in raw and raw[anchor].get("supplyId") == supply, "anchor_order_supply_conflict")
        members = [x for x in normalized if x["order"]["supply_id"] == supply]
        require(members and all(x["status"]["supplier_status"] == "complete" for x in members), "dispatch_current_status_conflict")
        actual_vector = {}
        for member in members:
            nm = str(member["order"]["nm_id"])
            actual_vector[nm] = actual_vector.get(nm, 0) + 1
            member["dispatch"] = group
            assigned.append(member["order"]["order_id"])
        require(actual_vector == {str(k): int(v) for k, v in group["quantity_by_nm"].items() if int(v)},
                "dispatch_vector_conflict")
        datetime.strptime(group["business_date"], "%Y-%m-%d")
    excluded = [x for x in normalized if x["order"]["order_id"] not in assigned]
    require(len(assigned) == len(set(assigned)) == 32 and len(excluded) == 1
            and not excluded[0]["order"]["supply_id"] and excluded[0]["status"]["supplier_status"] == "new"
            and excluded[0]["status"]["wb_status"] == "declined_by_client", "exact_32_handoffs_and_one_decline_required")
    result = dict(contract=CONTRACT, orders=normalized, groups=list(group_index.values()),
        coverage={"date_from": official["date_from"], "date_to": official["date_to"], "pages": pages,
                  "terminal": {k: terminal[k] for k in ("date_to", "cursor", "returned", "next_cursor", "coverage_terminal")}},
        source_files=source_files, approval_reference=approval_reference,
        official_payload_digest=fingerprint(official), terminal_payload_digest=fingerprint(terminal))
    return {**result, "evidence_digest": fingerprint(result)}


def _boundary(book, *, allocation_date, handoff_dates):
    require(book and book.get("schema") == SCHEMA and book.get("active") is True, "active_book_required")
    # Only the ordinary unfinished next day is admitted. This does not relax
    # the healthy-book gate of the separate historical-FBO monetary writer.
    require(not book.get("publication_error") or book["publication_error"].get("reason") == "incomplete_period_cannot_close",
            "unrelated_book_error")
    state = book["state"]
    last, _ = _last_closed(state)
    require((datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)).date().isoformat() == allocation_date,
            "allocation_must_follow_last_closed_day")
    require(all(day >= allocation_date for day in handoff_dates) and all(
        period["status"] != "closed" for day, period in state["periods"].items() if day >= allocation_date),
        "open_day_boundary_changed")
    require(all(day not in state["periods"] for day in handoff_dates), "handoff_days_must_be_unpublished")


def _document(conn, document_id):
    row = conn.execute(f"SELECT * FROM {DOCUMENTS_TABLE} WHERE document_id=?", (document_id,)).fetchone()
    require(row is not None, "native_document_missing")
    document = dict(row)
    require(fingerprint(json.loads(document["posted_manifest_json"])) == document["posted_manifest_sha256"],
            "native_document_manifest_corrupt")
    lines = [dict(r) for r in conn.execute(f"SELECT * FROM {DOCUMENT_LINES_TABLE} WHERE document_id=? ORDER BY line_no", (document_id,))]
    movements = [dict(r) for r in conn.execute(f"SELECT * FROM {LINES_TABLE} WHERE operation_id=? ORDER BY line_no", (document["operation_id"],))]
    operation = conn.execute(f"SELECT * FROM {OPERATIONS_TABLE} WHERE operation_id=?", (document["operation_id"],)).fetchone()
    require(operation is not None, "native_document_operation_missing")
    return {"document": document, "lines": lines, "movements": movements, "operation": dict(operation)}


def _capture_guard(conn, *, nms, order_ids):
    scope = {"effects": [{"pool_movements": [{"nm_id": nm, "facility_id": fid} for fid, nm in nms]}]}
    result = {"native": capture_native_guard(conn, scope)}
    # These additional source tables are not all part of reservation checking.
    # Pin global sequence allocation too: event identities retain real native
    # observation sequences, without silently borrowing an unrelated cursor.
    nm_ids = sorted({nm for _, nm in nms})
    slots = ",".join("?" for _ in order_ids)
    nm_slots = ",".join("?" for _ in nm_ids)
    for table in (orders.IDENTITY_MAPPINGS_TABLE, orders.IDENTITY_EVIDENCE_TABLE, orders.STATUS_OBSERVATIONS_TABLE,
                  "sheet_vitrina_v1_ff_pool_cutover_manifests", lifecycle.MAPPING_EXTENSIONS_TABLE,
                  lifecycle.MAPPING_EXTENSION_ALLOCATIONS_TABLE, "sheet_vitrina_v1_warehouse_functional_active",
                  "sheet_vitrina_v1_warehouse_functional_versions", "sheet_vitrina_v1_warehouse_functional_balances"):
        where, args = "", ()
        if table in (orders.IDENTITY_EVIDENCE_TABLE, orders.STATUS_OBSERVATIONS_TABLE):
            where, args = f" WHERE order_id IN ({slots})", order_ids
        elif table == orders.IDENTITY_MAPPINGS_TABLE:
            where, args = f" WHERE source_nm_id IN ({nm_slots}) OR target_nm_id IN ({nm_slots})", [*nm_ids, *nm_ids]
        elif table == "sheet_vitrina_v1_warehouse_functional_versions":
            where = " WHERE version_id IN (SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active)"
        elif table == "sheet_vitrina_v1_warehouse_functional_balances":
            where, args = f" WHERE version_id IN (SELECT version_id FROM sheet_vitrina_v1_warehouse_functional_active) AND nm_id IN ({nm_slots})", nm_ids
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table}{where} ORDER BY rowid", args).fetchmany(100001)]
        require(len(rows) <= 100000, "guard_row_limit")
        result[table] = rows
    result["sequence_watermarks"] = {}
    for table in (orders.OBSERVATIONS_TABLE, orders.STATUS_OBSERVATIONS_TABLE):
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        value = int(sequence[0]) if sequence else 0
        require(value >= conn.execute(f"SELECT COALESCE(MAX(observation_sequence),0) FROM {table}").fetchone()[0], "source_sequence_invalid")
        result["sequence_watermarks"][table] = value
    require(len(canonical(result)) <= 64 * 1024**2, "guard_size_limit")
    return result


def prepare_historical_handoffs(conn, *, runtime_dir, evidence, allocation_document_id, original_document_id,
                                posted_at, approval_reference):
    """Read-only plan after the exact ordinary FBO→FBS allocation is visible."""
    require(conn.in_transaction and conn.row_factory is sqlite3.Row, "bound_snapshot_required")
    require(evidence.get("contract") == CONTRACT and evidence.get("evidence_digest") == fingerprint(
        {k: v for k, v in evidence.items() if k != "evidence_digest"}) and approval_reference, "evidence_digest_invalid")
    require(len(evidence["orders"]) == 33 and sum("dispatch" in x for x in evidence["orders"]) == 32,
            "bounded_cohort_required")
    lifecycle._require_utc(posted_at)
    allocation, original = _document(conn, allocation_document_id), _document(conn, original_document_id)
    a, o = allocation["document"], original["document"]
    require(a["document_kind"] == "pool_reallocation" and o["document_kind"] == "china_acceptance"
            and o["business_date"] < a["business_date"] and int(a["idempotency_epoch"]) == int(o["idempotency_epoch"]),
            "native_allocation_and_original_receipt_required")
    domain = json.loads(a["posted_manifest_json"])["domain"]
    require(domain["source_pool"] == "FBO" and domain["destination_pool"] == "FBS"
            and domain["physical_total_unchanged"] is True and Decimal(domain["expense_rub"]) == 0,
            "zero_expense_fbo_to_fbs_required")
    facility = domain["facility_id"]
    allocated = {int(row["nm_id"]): row for row in allocation["lines"]}
    nms = sorted({int(x["order"]["nm_id"]) for x in evidence["orders"]})
    require(len(nms) == 4 and set(allocated) == set(nms) and len(allocation["lines"]) == 4,
            "four_sku_allocation_required")
    require(sum(int(row["quantity"]) for row in allocated.values()) == 5400, "exact_5400_allocation_required")
    sources = {}
    for nm, line in sorted(allocated.items()):
        source_lines = [row for row in original["lines"] if row["facility_id"] == facility and row["pool"] == "FBO"
                        and row["nm_id"] == nm and row["line_role"] == "accepted_pool_allocation"]
        require(len(source_lines) == 1 and line["line_role"] == "reallocated" and line["pool"] == "FBS"
                and line["facility_id"] == facility and Decimal(line["expense_rub"]) == 0
                and int(line["quantity"]) == int(source_lines[0]["quantity"])
                and Decimal(line["capital_rub"]) == Decimal(source_lines[0]["capital_rub"]), "original_native_mass_mismatch")
        native_original = [row for row in original["movements"] if row["nm_id"] == nm
                           and row["facility_id"] == facility and row["pool"] == "FBO"]
        require(len(native_original) == 1 and native_original[0]["quantity_delta"] == line["quantity"]
                and Decimal(native_original[0]["capital_delta_rub"]) == Decimal(line["capital_rub"]),
                "original_foundation_mass_mismatch")
        pair = [row for row in allocation["movements"] if row["nm_id"] == nm]
        require(len(pair) == 2 and sorted((row["pool"], row["quantity_delta"], Decimal(row["capital_delta_rub"])) for row in pair)
                == [("FBO", -line["quantity"], -Decimal(line["capital_rub"])), ("FBS", line["quantity"], Decimal(line["capital_rub"]))]
                and all(row["facility_id"] == facility for row in pair), "allocation_native_pair_mismatch")
        balance = conn.execute(f"SELECT * FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?", (facility, nm)).fetchone()
        require(balance is not None and balance["quantity"] == line["quantity"]
                and Decimal(balance["capital_rub"]) == Decimal(line["capital_rub"])
                and balance["source_watermark"] == a["operation_id"]
                and balance["projection_epoch"] == a["idempotency_epoch"], "untouched_allocation_boundary_required")
        sources[str(nm)] = {"quantity": int(line["quantity"]), "capital_rub": line["capital_rub"],
            "wac_rub": canonical_decimal_ratio_text(line["capital_rub"], line["quantity"]), "balance": dict(balance)}
    book, book_version = load(Path(runtime_dir))
    dates = sorted({x["dispatch"]["business_date"] for x in evidence["orders"] if "dispatch" in x})
    expected_dates = [(datetime.strptime(a["business_date"], "%Y-%m-%d") + timedelta(days=n)).date().isoformat() for n in (1, 2)]
    require(dates == expected_dates and [sum(x.get("dispatch", {}).get("business_date") == day for x in evidence["orders"]) for day in dates] == [17, 15],
            "exact_17_then_15_dated_handoffs_required")
    _boundary(book, allocation_date=a["business_date"], handoff_dates=dates)
    manifest_row = conn.execute("SELECT manifest_json FROM sheet_vitrina_v1_ff_pool_cutover_manifests ORDER BY cutover_at DESC,cutover_id DESC LIMIT 1").fetchone()
    require(manifest_row is not None, "applied_cutover_required")
    manifest = json.loads(manifest_row[0]); lifecycle._require_approved_policy(manifest)
    epoch = conn.execute(f"SELECT * FROM {FEATURE_EPOCHS_TABLE} ORDER BY epoch DESC LIMIT 1").fetchone()
    require(epoch and epoch["writer_enabled"] == 1 and epoch["epoch"] == a["idempotency_epoch"] == manifest["feature_epoch"],
            "active_writer_epoch_required")
    ids = [x["order"]["order_id"] for x in evidence["orders"]]
    slots = ",".join("?" for _ in ids)
    require(conn.execute(f"SELECT 1 FROM {orders.OBSERVATIONS_TABLE} WHERE order_id IN ({slots}) LIMIT 1", ids).fetchone() is None
            and conn.execute(f"SELECT 1 FROM {lifecycle.CURRENT_TABLE} WHERE order_id IN ({slots}) LIMIT 1", ids).fetchone() is None,
            "cohort_must_be_unadmitted")
    guard = _capture_guard(conn, nms=[(facility, nm) for nm in nms], order_ids=ids)
    planned = []
    for rank, item in enumerate(sorted(evidence["orders"], key=lambda x: x["order"]["order_id"]), 1):
        row = _copy(item)
        row["order_sequence"] = guard["sequence_watermarks"][orders.OBSERVATIONS_TABLE] + rank
        row["status_sequence"] = guard["sequence_watermarks"][orders.STATUS_OBSERVATIONS_TABLE] + rank
        row["mapped_order"] = {"order_id": row["order"]["order_id"], "quantity": 1, "facility_id": facility, "nm_id": row["order"]["nm_id"]}
        observation = row["order"]
        warehouses = conn.execute(f"SELECT mapping_id FROM {orders.WAREHOUSE_MAPPINGS_TABLE} WHERE seller_warehouse_id=? AND active=1 ORDER BY mapping_id",
                                  (observation["warehouse_id"],)).fetchall()
        identities = conn.execute(f"SELECT mapping_id FROM {orders.IDENTITY_MAPPINGS_TABLE} WHERE source_nm_id=? AND source_chrt_id=? AND source_barcode=? AND source_sku=? AND active=1 ORDER BY mapping_id",
            (observation["nm_id"], observation["chrt_id"], observation["skus"][0], observation["seller_sku"])).fetchall()
        require(warehouses and identities, "exact_current_mapping_required")
        mapped = lifecycle._canonical_mapping_for_identity(conn, manifest=manifest, order_id=observation["order_id"],
            warehouse_id=observation["warehouse_id"], office_id=observation["office_id"], source_nm_id=observation["nm_id"],
            source_chrt_id=observation["chrt_id"], barcode=observation["skus"][0], seller_sku=observation["seller_sku"],
            warehouse_mapping_id=warehouses[0][0], identity_mapping_id=identities[0][0])
        require(mapped["facility_id"] == facility and mapped["target_nm_id"] == observation["nm_id"], "exact_current_mapping_conflict")
        row["wac_rub"] = sources[str(row["order"]["nm_id"])]["wac_rub"]
        planned.append(row)
    debit_plan = {"documents": [{"movements": [{"facility_id": facility, "pool": "FBS", "nm_id": row["order"]["nm_id"],
         "quantity_delta": -1} for row in planned if "dispatch" in row]}]}
    assert_reservations(conn, plan=debit_plan, epoch=epoch["epoch"])
    result = dict(contract=CONTRACT, status="prepared_connection_owned", evidence=evidence, orders=planned,
        source_book_version=book_version, allocation=allocation, original=original, allocation_date=a["business_date"],
        facility_id=facility, feature_epoch=int(epoch["epoch"]), manifest=manifest, native_guard=guard,
        sources=sources, posted_at=posted_at, approval_reference=approval_reference, publication_complete=False)
    require(len(canonical(result).encode()) <= 16 * 1024**2, "plan_size_limit")
    return {**result, "plan_digest": fingerprint(result)}


def _event_evidence(plan, row):
    return dict(contract=CONTRACT, plan_digest=plan["plan_digest"], order_id=row["order"]["order_id"],
        order_revision=row["order"]["source_revision"], status_digest=row["status"]["status_digest"],
        current_supplier_status=row["status"]["supplier_status"], current_wb_status=row["status"]["wb_status"],
        status_observed_at=row["status"]["observed_at"], status_transition_at=None,
        source_business_date=row["dispatch"]["business_date"], dispatch=row["dispatch"],
        evidence_digest=plan["evidence"]["evidence_digest"], allocation_document_id=plan["allocation"]["document"]["document_id"],
        original_document_id=plan["original"]["document"]["document_id"],
        source_book_version=plan["source_book_version"], frozen_wac_rub=row["wac_rub"],
        proves_physical_handoff=True, proves_status_history=False, mutates_wb=False)


def _event_details(plan, row):
    evidence = _event_evidence(plan, row)
    first = min(item["order"]["order_id"] for item in plan["orders"] if "dispatch" in item)
    return {**evidence, **({"plan_manifest": plan} if row["order"]["order_id"] == first else {})}


def _excluded_evidence(plan, row):
    return dict(contract=CONTRACT, plan_digest=plan["plan_digest"], excluded_order_id=row["order"]["order_id"],
        reason="new_declined_without_supply_no_physical_handoff", proves_status_history=False)


def _event_id(plan, row):
    dispatch = "dispatch" in row
    identity = dict(cutover_id=plan["manifest"]["cutover_id"], order_id=row["order"]["order_id"],
        episode_sequence=row["status_sequence"], event_type="handoff_debit" if dispatch else "cancel_noop",
        source_order_observation_sequence=row["order_sequence"], source_status_observation_sequence=row["status_sequence"],
        evidence_digest=fingerprint(_event_evidence(plan, row) if dispatch else _excluded_evidence(plan, row)))
    return "ffbf_" + fingerprint(identity)[7:35]


def verify_existing_handoffs(conn, plan):
    require(plan.get("contract") == CONTRACT and plan.get("plan_digest") == fingerprint(
        {k: v for k, v in plan.items() if k != "plan_digest"}), "plan_digest_invalid")
    rows = [row for row in plan["orders"] if "dispatch" in row]
    found = [conn.execute(f"SELECT * FROM {lifecycle.EVENTS_TABLE} WHERE event_id=?", (_event_id(plan, row),)).fetchone() for row in rows]
    if not any(found):
        return False
    require(all(found), "partial_existing_cohort")
    require(_document(conn, plan["allocation"]["document"]["document_id"]) == plan["allocation"]
            and _document(conn, plan["original"]["document"]["document_id"]) == plan["original"], "existing_native_source_document_conflict")
    for row in plan["orders"]:
        observation = conn.execute(f"SELECT * FROM {orders.OBSERVATIONS_TABLE} WHERE observation_id=?", (row["order"]["observation_id"],)).fetchone()
        status = conn.execute(f"SELECT * FROM {orders.STATUS_OBSERVATIONS_TABLE} WHERE observation_id=?", (row["status"]["observation_id"],)).fetchone()
        require(observation is not None and observation["observation_sequence"] == row["order_sequence"]
                and all(observation[key] == value for key, value in row["order"].items() if key not in {"skus"})
                and status is not None and status["observation_sequence"] == row["status_sequence"]
                and all(status[key] == value for key, value in row["status"].items() if key != "source_observed_at"), "existing_source_observation_conflict")
        if "dispatch" not in row:
            noop = conn.execute(f"SELECT * FROM {lifecycle.EVENTS_TABLE} WHERE event_id=?", (_event_id(plan, row),)).fetchone()
            require(noop and noop["event_type"] == "cancel_noop" and noop["physical_quantity_delta"] == 0
                    and Decimal(noop["capital_delta_rub"]) == 0 and json.loads(noop["details_json"]) == _excluded_evidence(plan, row),
                    "excluded_order_noop_conflict")
    for row, event in zip(rows, found):
        require(json.loads(event["details_json"]) == _event_details(plan, row) and event["physical_quantity_delta"] == -1
                and event["supplier_status"] == row["status"]["supplier_status"] and event["wb_status"] == row["status"]["wb_status"]
                and event["frozen_wac_rub"] == row["wac_rub"] and Decimal(event["capital_delta_rub"]) == Decimal(row["wac_rub"]).copy_negate()
                and event["occurred_at"] == plan["posted_at"], "existing_event_conflict")
        op_id = "ffbo_" + hashlib.sha256(event["event_id"].encode()).hexdigest()[:28]
        operation = conn.execute(f"SELECT * FROM {OPERATIONS_TABLE} WHERE operation_id=?", (op_id,)).fetchone()
        movements = conn.execute(f"SELECT * FROM {LINES_TABLE} WHERE operation_id=?", (op_id,)).fetchall()
        require(operation and operation["operation_type"] == "fbs_physical_handoff" and operation["source_id"] == str(row["order"]["order_id"])
                and operation["source_revision"] == event["event_id"] and operation["business_date"] == row["dispatch"]["business_date"]
                and len(movements) == 1 and movements[0]["quantity_delta"] == -1 and movements[0]["pool"] == "FBS"
                and movements[0]["facility_id"] == plan["facility_id"] and movements[0]["nm_id"] == row["order"]["nm_id"]
                and Decimal(movements[0]["capital_delta_rub"]) == Decimal(row["wac_rub"]).copy_negate(), "existing_physical_effect_conflict")
        current = conn.execute(f"SELECT * FROM {lifecycle.CURRENT_TABLE} WHERE cutover_id=? AND order_id=?",
                               (plan["manifest"]["cutover_id"], row["order"]["order_id"])).fetchone()
        require(current and current["debit_event_id"] == event["event_id"] and current["state"] in {"fulfilled", "fulfilled_reconciliation"}
                and current["facility_id"] == plan["facility_id"] and current["nm_id"] == row["order"]["nm_id"], "existing_lifecycle_debit_conflict")
    return True


def read_historical_handoff_plan(conn, *, plan_digest=None):
    """Recover the saved immutable plan only after exact whole-cohort readback."""
    require(conn.in_transaction and conn.row_factory is sqlite3.Row, "bound_snapshot_required")
    where, args = "", ()
    if plan_digest is not None:
        where, args = " AND json_extract(details_json,'$.plan_digest')=?", (plan_digest,)
    rows = conn.execute(f"""SELECT details_json FROM {lifecycle.EVENTS_TABLE}
        WHERE json_extract(details_json,'$.contract')=?
          AND json_type(details_json,'$.plan_manifest')='object' {where}
        ORDER BY event_sequence LIMIT 2""", (CONTRACT, *args)).fetchall()
    require(len(rows) == 1, "one_saved_handoff_manifest_required")
    plan = json.loads(rows[0][0])["plan_manifest"]
    require(verify_existing_handoffs(conn, plan), "saved_handoff_cohort_incomplete")
    return plan


def _persist_observations(conn, planned):
    fields = ("observation_id order_id source_revision supply_id delivery_type source_created_at warehouse_id office_id nm_id chrt_id seller_sku "
        "rid_sha256 order_uid_sha256 skus_json cargo_type cross_border_type is_zero_order observed_at collector_date_from collector_date_to collector_cursor").split()
    for row in planned:
        item = row["order"]
        conn.execute(f"INSERT INTO {orders.OBSERVATIONS_TABLE}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", [item[k] for k in fields])
    statuses = [row["status"] for row in planned]
    orders._persist_status_transitions(conn, statuses)
    fields = "observation_id order_id order_revision status_digest supplier_status wb_status positive_quantity observed_at".split()
    for item in statuses:
        conn.execute(f"INSERT INTO {orders.STATUS_OBSERVATIONS_TABLE}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", [item[k] for k in fields])
    # This method uses no collector instance state and never touches its cursor.
    orders.WbFbsOrdersCollector._persist_identity_evidence(None, conn, [row["order"] for row in planned])


def apply_historical_handoffs(conn, plan, *, runtime, owner_token, fault=None):
    """Append within the caller's already locked transaction; never commit it."""
    require_warehouse_job_owner(runtime.runtime_dir, owner_token)
    require(conn.in_transaction and conn.row_factory is sqlite3.Row and conn.execute("PRAGMA query_only").fetchone()[0] == 0
            and Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve() == Path(runtime.db_path).resolve(),
            "owned_main_write_transaction_required")
    if verify_existing_handoffs(conn, plan):
        return {"status": "native_handoffs_present_pending_publication", "idempotent": True, "physical_debits": 0, "publication_complete": False}
    book, version = load(runtime.runtime_dir)
    require(version == plan["source_book_version"], "book_compare_and_swap_failed")
    dates = sorted({row["dispatch"]["business_date"] for row in plan["orders"] if "dispatch" in row})
    _boundary(book, allocation_date=plan["allocation_date"], handoff_dates=dates)
    ids = [row["order"]["order_id"] for row in plan["orders"]]
    require(_capture_guard(conn, nms=[(plan["facility_id"], int(nm)) for nm in plan["sources"]], order_ids=ids) == plan["native_guard"],
            "native_compare_and_swap_failed")
    require(_document(conn, plan["allocation"]["document"]["document_id"]) == plan["allocation"]
            and _document(conn, plan["original"]["document"]["document_id"]) == plan["original"], "native_source_document_changed")
    assert_reservations(conn, plan={"documents": [{"movements": [dict(facility_id=plan["facility_id"], pool="FBS", nm_id=row["order"]["nm_id"],
        quantity_delta=-1) for row in plan["orders"] if "dispatch" in row]}]}, epoch=plan["feature_epoch"])
    _persist_observations(conn, plan["orders"])
    handoffs = []
    for row in plan["orders"]:
        observation, status = row["order"], row["status"]
        order_source = conn.execute(f"SELECT * FROM {orders.OBSERVATIONS_TABLE} WHERE observation_id=?", (observation["observation_id"],)).fetchone()
        status_source = conn.execute(f"SELECT * FROM {orders.STATUS_OBSERVATIONS_TABLE} WHERE observation_id=?", (status["observation_id"],)).fetchone()
        require(order_source["observation_sequence"] == row["order_sequence"] and status_source["observation_sequence"] == row["status_sequence"],
                "source_observation_sequence_changed")
        mapped = lifecycle._map_order(conn, plan["manifest"], (observation["observation_id"], observation["order_id"], observation["source_revision"],
            observation["source_created_at"], observation["observed_at"], observation["warehouse_id"], observation["nm_id"], observation["chrt_id"],
            observation["skus_json"], observation["office_id"]))
        require(mapped["facility_id"] == plan["facility_id"] and mapped["nm_id"] == observation["nm_id"], "mapped_identity_changed")
        if "dispatch" not in row:
            lifecycle._append_event(conn, manifest=plan["manifest"], order=row["mapped_order"], episode_sequence=row["status_sequence"],
                event_type="cancel_noop", state="cancelled_noop", supplier_status=status["supplier_status"], wb_status=status["wb_status"],
                status_digest=status["status_digest"], source_revision=observation["source_revision"], source_observed_at=status["observed_at"],
                wac=Decimal(row["wac_rub"]), physical_delta=0, occurred_at=plan["posted_at"],
                evidence=_excluded_evidence(plan, row),
                source_order_observation_sequence=row["order_sequence"], source_status_observation_sequence=row["status_sequence"])
            continue
        event = lifecycle._append_event(conn, manifest=plan["manifest"], order=row["mapped_order"], episode_sequence=row["status_sequence"],
            event_type="handoff_debit", state="fulfilled", supplier_status=status["supplier_status"], wb_status=status["wb_status"],
            status_digest=status["status_digest"], source_revision=observation["source_revision"], source_observed_at=status["observed_at"],
            wac=Decimal(row["wac_rub"]), physical_delta=-1, occurred_at=plan["posted_at"], evidence=_event_evidence(plan, row),
            details_evidence=_event_details(plan, row),
            source_order_observation_sequence=row["order_sequence"], source_status_observation_sequence=row["status_sequence"])
        require(not event["idempotent"] and event["event_id"] == _event_id(plan, row), "event_identity_conflict")
        handoffs.append((row, event["event_id"]))
    # Admit the full exact cohort before materialization: every current source
    # order then has its honest physical proof when ordinary coverage is read.
    if fault: fault("after_events", 0)
    for index, (row, event_id) in enumerate(handoffs):
        lifecycle._apply_exact_physical_delta(conn, manifest=plan["manifest"], order=row["mapped_order"], event_id=event_id,
            quantity_delta=-1, wac=Decimal(row["wac_rub"]), occurred_at=plan["posted_at"], source_business_date=row["dispatch"]["business_date"])
        if fault: fault("after_physical", index)
    lifecycle._record_current_parity(conn, manifest=plan["manifest"], checked_at=plan["posted_at"])
    require(verify_existing_handoffs(conn, plan), "native_readback_failed")
    for nm, source in plan["sources"].items():
        count = sum(row["order"]["nm_id"] == int(nm) and "dispatch" in row for row in plan["orders"])
        balance = conn.execute(f"SELECT quantity,capital_rub FROM {BALANCES_TABLE} WHERE facility_id=? AND pool='FBS' AND nm_id=?", (plan["facility_id"], int(nm))).fetchone()
        with localcontext() as context:
            context.prec = 160
            expected = Decimal(source["capital_rub"]) - Decimal(source["wac_rub"]) * count
        require(balance[0] == source["quantity"] - count and Decimal(balance[1]) == expected, "pool_readback_failed")
    return {"status": "native_handoffs_present_pending_publication", "idempotent": False, "physical_debits": 32,
            "publication_complete": False, "plan_digest": plan["plan_digest"]}
