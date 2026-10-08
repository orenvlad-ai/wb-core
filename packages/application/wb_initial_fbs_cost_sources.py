"""Read an accepted same-day FBS predecessor and verify its receipt operands."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
from time import monotonic

from packages.application.wb_initial_fbs_cost import digest


def capture(runtime, *, day, fetched_at, previous_version):
    # Imports are local: FBS sources use warehouse fingerprints, not a writer.
    from packages.application.fbs_accounting_runtime import load, path as accounting_path
    from packages.application.fbs_snapshot_cost_sources import capture_current
    deadline = monotonic() + 5
    try:
        with closing(sqlite3.connect(accounting_path(runtime.runtime_dir).as_uri() + "?mode=ro", uri=True, timeout=.2)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            conn.execute("BEGIN")
            book, version = load(runtime.runtime_dir, connection=conn)
        if monotonic() >= deadline:
            return {"available": False, "reason": "accepted_fbs_source_deadline"}
        if not book or not book.get("active") or book.get("publication_error"):
            return {"available": False, "reason": "accepted_fbs_predecessor_unavailable"}
        period = book["state"]["periods"].get(day)
        wb = book["wb_days"].get(day, {})
        if not period or wb.get("version_id") != previous_version or book["state"].get("pending_documents"):
            return {"available": False, "reason": "accepted_fbs_predecessor_binding_mismatch"}
        with closing(sqlite3.connect(Path(runtime.db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.set_progress_handler(lambda: int(monotonic() >= deadline), 1000)
            conn.execute("BEGIN")
            source = capture_current(Path(runtime.db_path), now=datetime.fromisoformat(fetched_at.replace("Z", "+00:00")),
                include_baseline=False, connection=conn)
        if not source["documents_complete"] or not source["quantity_snapshot"]["complete"]:
            return {"available": False, "reason": "initial_fbs_source_incomplete"}
        if monotonic() >= deadline:
            return {"available": False, "reason": "accepted_fbs_source_deadline"}
        documents = {d["document_id"]: d for d in source["documents"]}
        # Frozen opening/closed document ownership remains an accepted operand.
        admitted = dict(book["state"]["baseline"]["absorbed_documents"])
        for saved in book["state"]["periods"].values():
            admitted.update(saved["applied_documents"])
        if any(identity not in documents or documents[identity]["fingerprint"] != proof for identity, proof in admitted.items()):
            return {"available": False, "reason": "accepted_fbs_document_changed"}
        # A newly accepted document must first be priced by the FBS owner.
        if any(d["document_id"] not in admitted for d in source["documents"]):
            return {"available": False, "reason": "accepted_fbs_documents_pending"}
        stock = source["quantity_snapshot"]
        accepted_stock = period["snapshot"]
        if (not accepted_stock.get("complete") or accepted_stock.get("date") != day
            or not accepted_stock.get("id") or not accepted_stock.get("digest")):
            return {"available": False, "reason": "accepted_fbs_quantity_proof_missing"}
        accepted_quantities = {(int(r["nm_id"]), r["facility_id"]): str(r["quantity"]) for r in accepted_stock["rows"]}
        evidence = stock.get("facility_evidence", {})
        rows = []
        for original in period["rows"].values():
            row = dict(original)
            if accepted_quantities.get((int(row["nm_id"]), row["facility_id"]), "0") != str(row["quantity"]):
                return {"available": False, "reason": "accepted_fbs_quantity_binding_mismatch"}
            proofs = [{"document_id": identity, "fingerprint": proof}
                for identity, proof in admitted.items()
                if documents[identity]["kind"] == "china_acceptance"
                and any(int(e.get("nm_id", 0)) == int(row["nm_id"])
                    and e.get("facility_id") == row["facility_id"] and e.get("kind") == "receipt"
                    for e in documents[identity]["events"])]
            row["document_proofs"] = proofs
            row["official_facility_evidence"] = evidence.get(row["facility_id"])
            row["accepted_official_facility_evidence"] = accepted_stock.get("facility_evidence", {}).get(row["facility_id"])
            current_evidence, accepted_evidence = row["official_facility_evidence"], row["accepted_official_facility_evidence"]
            if current_evidence and accepted_evidence and any(
                current_evidence.get(k) != accepted_evidence.get(k) for k in ("facility_id", "mapping_id", "seller_warehouse_id")
            ):
                return {"available": False, "reason": "accepted_fbs_facility_mapping_changed"}
            # Keep only valuation operands, not the whole day's document history.
            rows.append({k: row.get(k) for k in ("nm_id", "facility_id", "quantity", "cost_mass_quantity",
                "wac_rub", "quality", "document_proofs", "official_facility_evidence", "accepted_official_facility_evidence")})
        return {"available": True, "business_date": day, "wb_version_id": previous_version,
            "prepared_at": book["prepared_at"], "book_version": version, "period_digest": digest(period),
            "official_fbs_generation_id": accepted_stock["id"], "official_fbs_generation_digest": accepted_stock["digest"],
            "verified_fbs_generation_id": stock["id"], "verified_fbs_generation_digest": stock["digest"], "rows": rows}
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return {"available": False, "reason": "accepted_fbs_source_unavailable"}
