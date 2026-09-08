"""Read expense allocations from the same book that owns current FF prices.

Posted source documents remain immutable. Until publication, their retired
ledger shares must not masquerade as the applied snapshot-cost allocation.
Preview uses the actual document engine in memory, without a business write.
"""
from datetime import datetime, timezone
from decimal import Decimal

from packages.application.fbs_accounting_runtime import load
from packages.application.fbs_snapshot_cost import evaluate_candidate, fingerprint
from packages.application.fbs_snapshot_cost_sources import capture_current
from packages.business_time import current_business_date_iso

SOURCE = "fbs_snapshot_overhead_allocation_v1"


class OverheadAccountingView:
    def __init__(self, runtime_dir, db_path, *, now=None):
        self.book, self.version = load(runtime_dir)
        self.db_path = db_path
        self.now = now or datetime.now(timezone.utc)

    def resolve(self, summary, *, day, document_id="", posted=True):
        book = self.book
        if (not book or not book["active"] or day < book["effective_date"]
                or document_id in book["state"]["baseline"]["absorbed_documents"]):
            return None
        result = {"allocation_source": SOURCE, "accounting_version": self.version,
                  "business_date": day, "allocation_status": "pending",
                  "allocation_label": "Ожидает публикации в расчёте себестоимости",
                  "denominator_quantity": None, "denominator_sku_count": None,
                  "affected_sku_count": None, "allocation_total_rub": None,
                  "pool_allocations_rub": {}, "lines": []}
        try:
            period = book["state"]["periods"].get(day)
            if book.get("publication_error") and (not period or period["status"] != "closed"):
                raise ValueError("accounting_publication_failed")
            if not posted:
                if day != current_business_date_iso(self.now):
                    raise ValueError("preview_requires_current_accounting_day")
                capture = capture_current(self.db_path, now=self.now, include_baseline=False)
                identity = "preview-" + fingerprint({"day": day, "summary": summary})[7:]
                doc = {"document_id": identity, "kind": "pool_overhead", "business_date": day,
                       "posted_at": capture["captured_at"], "events": [], "cost_document": {
                           "domain": {k: summary[k] for k in ("facility_id", "scope", "amount_rub")},
                           "lines": [], "movements": [], "relations": [],
                           "expense_lines": [{"amount_rub": summary["amount_rub"]}]}}
                doc["fingerprint"] = fingerprint(doc)
                capture["documents"].append(doc)
                capture["source_digest"] = fingerprint(capture)
                candidate = evaluate_candidate(book["state"], capture)
                period = candidate["periods"].get(day)
                document_id = identity
                if candidate["pending_documents"]:
                    raise ValueError("preview_has_unresolved_documents")
            if not period or document_id not in period["applied_documents"]:
                return result
            if period["quality"] == "incomplete":
                raise ValueError("accounting_period_incomplete")
            allocations = [a for a in period["document_valuation"]["allocations"]
                           if a["document_id"] == document_id]
            if len(allocations) != 1:
                raise ValueError("accounting_allocation_missing_or_ambiguous")
            allocation = allocations[0]
            if allocation["document_fingerprint"] != period["applied_documents"][document_id]:
                raise ValueError("accounting_allocation_document_mismatch")
            weights, amounts = allocation["weights"], allocation["amounts_kopecks"]
            total = int(Decimal(str(summary["amount_rub"])) * 100)
            if allocation["total_kopecks"] != total or sum(amounts.values()) != total:
                raise ValueError("accounting_allocation_money_mismatch")
            pools = {"FBS", "FBO"} if summary["scope"] == "both" else {summary["scope"]}
            lines = []
            for key, amount in sorted(amounts.items()):
                facility, pool, nm = key.split(":")
                if facility != summary["facility_id"] or pool not in pools:
                    raise ValueError("accounting_allocation_scope_mismatch")
                lines.append({"document_id": document_id if posted else "", "line_no": len(lines) + 1,
                    "line_role": "overhead_allocation", "facility_id": facility, "pool": pool,
                    "nm_id": int(nm), "quantity": weights[key],
                    "capital_rub": str(Decimal(amount) / 100), "expense_rub": str(Decimal(amount) / 100)})
            return {**result, "allocation_status": "published" if posted else "preview",
                "allocation_label": ("Распределение учтено в себестоимости" if posted else
                    "Предварительное распределение; уточняется до закрытия дня"),
                "allocation_basis_id": allocation["id"], "denominator_quantity": sum(weights.values()),
                "denominator_sku_count": len({line["nm_id"] for line in lines}),
                "affected_sku_count": len({line["nm_id"] for line in lines if Decimal(line["expense_rub"])}),
                "allocation_total_rub": str(Decimal(total) / 100),
                "pool_allocations_rub": {p: str(Decimal(a) / 100) for p, a in allocation["pool_amounts_kopecks"].items()},
                "lines": lines}
        except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
            return {**result, "allocation_status": "unavailable", "allocation_reason": str(exc),
                    "allocation_label": "Распределение недоступно; требуется успешный пересчёт"}


def apply_summary(summary, allocation):
    if allocation is None:
        return summary
    return {**summary, **{k: v for k, v in allocation.items() if k != "lines"}}
