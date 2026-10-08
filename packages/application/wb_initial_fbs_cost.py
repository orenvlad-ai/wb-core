"""Conservative snapshot valuation, never evidence of an FBS stock movement."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json

QUALITY = "snapshot_fbs_wb_initial_provisional"
POLICY = "accepted_fbs_snapshot_initial_wb_valuation_v1"


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def decimal(value):
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_initial_valuation_operand")
    return result


def resolve(nm_id, item, basis, *, business_date, snapshot_id, fetched_at, previous_version):
    """Return a priced provisional anchor or a typed reason for unavailable cost.

    Every admitted FBS facility contributes its documented cost mass, even if
    its last available unit has gone. Available stock is never treated as a
    receipt and this function cannot manufacture a transfer or change FF stock.
    """
    def missing(reason):
        return {"available": False, "reason": reason}
    try:
        if decimal(item.get("quantity", "0")) != 0:
            return missing("initial_physical_wb_cost_unproved")
        if decimal(item.get("in_way_to_client", "0")) + decimal(item.get("in_way_from_client", "0")) <= 0:
            return missing("initial_wb_transit_missing")
        if not basis.get("available"):
            return missing(basis.get("reason", "accepted_fbs_predecessor_unavailable"))
        if basis.get("business_date") != business_date or basis.get("wb_version_id") != previous_version:
            return missing("accepted_fbs_predecessor_binding_mismatch")
        if datetime.fromisoformat(basis["prepared_at"].replace("Z", "+00:00")) > datetime.fromisoformat(fetched_at.replace("Z", "+00:00")):
            return missing("accepted_fbs_predecessor_time_mismatch")
        rows = [r for r in basis["rows"] if int(r["nm_id"]) == nm_id]
        if not rows:
            return missing("accepted_fbs_sku_missing")
        facilities = set()
        mass = capital = Decimal(0)
        selected = []
        with localcontext() as context:
            context.prec = 160
            for row in rows:
                facility = row["facility_id"]
                if facility in facilities:
                    return missing("accepted_fbs_facility_duplicate")
                facilities.add(facility)
                quantity, weight = decimal(row["quantity"]), decimal(row["cost_mass_quantity"])
                if quantity == 0 and weight == 0:
                    continue
                wac = decimal(row["wac_rub"])
                if wac <= 0 or weight <= 0 or row["quality"] not in {"preliminary_snapshot_wac", "closed_snapshot_wac"}:
                    return missing("accepted_fbs_cost_coverage_missing")
                if not row.get("document_proofs"):
                    return missing("accepted_fbs_receipt_proof_missing")
                if not row.get("official_facility_evidence") or not row.get("accepted_official_facility_evidence"):
                    return missing("official_fbs_facility_binding_missing")
                mass += weight
                capital += weight * wac
                selected.append(row)
            if mass <= 0:
                return missing("accepted_fbs_cost_mass_missing")
            wac = capital / mass
        anchor = {"policy": POLICY, "nm_id": nm_id, "business_date": business_date,
            "snapshot_id": snapshot_id, "wac_rub": format(wac, "f"), "quality": QUALITY,
            "book_version": basis["book_version"], "period_digest": basis["period_digest"],
            "predecessor_version_id": previous_version, "prepared_at": basis["prepared_at"],
            "official_fbs_generation_id": basis["official_fbs_generation_id"],
            "official_fbs_generation_digest": basis["official_fbs_generation_digest"],
            "verified_fbs_generation_id": basis["verified_fbs_generation_id"],
            "verified_fbs_generation_digest": basis["verified_fbs_generation_digest"],
            "facilities": selected, "proves_physical_movement": False,
            "quantity_source": "official_wb_snapshot_only"}
        anchor["proof_digest"] = digest(anchor)
        return {"available": True, "wac_rub": anchor["wac_rub"], "anchor": anchor}
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return missing("accepted_fbs_cost_proof_invalid")


def valid_anchor(anchor):
    """A persisted valuation anchor is authenticated independently of its row."""
    try:
        return (anchor.get("policy") == POLICY and anchor.get("quality") == QUALITY
            and anchor.get("proves_physical_movement") is False
            and decimal(anchor["wac_rub"]) > 0
            and anchor.get("proof_digest") == digest({k: v for k, v in anchor.items() if k != "proof_digest"}))
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return False
