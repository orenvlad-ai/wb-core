"""Pinned FF/capital presentation candidate. No active pointer or business writes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3

from packages.application.fbs_snapshot_cost import candidate_period_view, fingerprint
from packages.application.shared_sku_cost import build_shared_cost_day
from packages.application.shared_sku_cost_sources import capture_wb_component
from packages.application.sheet_vitrina_v1_own_product_capital import (
    OWN_PRODUCT_CAPITAL_STAGES, own_stage_metric_key, build_own_product_capital_metric_items,
)
from packages.application.warehouse_business_projection import (
    _projection_row_binding_incident_from_exact_versions,
)

SOURCE = "fbs_snapshot_inventory_presentation_v1"
COST = "our_wb_unit_cost_rub"
FBS_TOTAL = "inventory_fbs_total_qty_v1"
FBS_FACILITY = "inventory_fbs_facility_available_qty_v1:"
RETAINED_STAGES = tuple(s for s in OWN_PRODUCT_CAPITAL_STAGES if s != "FF")
ZERO = Decimal(0)
EXPLANATION = ("FBS — доступное количество из официального снимка WB; FBO — документальный остаток. "
               "Резерв и полный физический остаток FBS по этому снимку неизвестны. "
               "Себестоимость рассчитана по снимкам, поступлениям и расходам; оценка предварительная.")


def number(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_inventory_operand")
    return result


def text(value):
    return format(value, "f") if value is not None else None


def total(values):
    values = list(values)
    return None if any(v is None for v in values) else sum(values, ZERO)


def operands(rows, *, blocked=False):
    rows = list(rows)
    q = total(number(r.get("quantity")) for r in rows)
    c = None if blocked else total(number(r.get("capital_rub")) for r in rows)
    if q == 0 and c not in (0, None):
        raise ValueError("zero_quantity_with_capital")
    if q is not None and q > 0 and c == 0:
        c = None
    return {"quantity": text(q), "capital_rub": text(c),
            "wac_rub": text(c / q) if c is not None and q else None}


def capture_retained_stages(db_path: Path, *, day: str, wb_version_id: str, nm_ids: list[int],
                            connection: sqlite3.Connection | None = None) -> dict:
    """Read exact published stage cells once, without schema initializers.

    Missing rows stay missing. A published explicit zero is kept, including
    zero SKU rows absent from the sparse functional balance table.
    """
    conn = connection or sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        if connection is None:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
        versions = {(r[0], r[1]) for r in conn.execute(
            "SELECT v.version_id,v.business_effective_date FROM sheet_vitrina_v1_warehouse_functional_versions v "
            "JOIN sheet_vitrina_v1_warehouse_wb_snapshots s ON s.version_id=v.version_id "
            "WHERE v.status='good' AND v.version_id=? AND v.business_effective_date=s.snapshot_date", (wb_version_id,))}
        raw = {int(r["nm_id"]): dict(r) for r in conn.execute(
            "SELECT * FROM sheet_vitrina_v1_warehouse_business_projection_current_rows WHERE as_of_date=? AND nm_id>0", (day,))}
        from packages.application.warehouse_functional import _nomenclature_names_from_connection
        names = _nomenclature_names_from_connection(conn)
        # Internal document stages are closed-world projections. A zero may
        # be proven by a complete immutable stage payload, not by a missing
        # business-presentation row alone. Verify its full persisted contents.
        complete_stages = {}
        stage_keys = {"PRODUCTION":"production", "PRODUCTION_TO_FF":"china_to_ff",
                      "FF_TO_WB":"ff_to_wb", "WB_ACCEPTANCE_DISCREPANCY":"wb_acceptance_discrepancy"}
        for stage, key in stage_keys.items():
            model = conn.execute("SELECT payload_json,etag FROM sheet_vitrina_v1_warehouse_functional_read_models "
                                 "WHERE version_id=? AND warehouse_key=?", (wb_version_id, key)).fetchone()
            if not model or (wb_version_id, day) not in versions:
                continue
            payload = json.loads(model["payload_json"])
            saved = {int(r["nm_id"]): (number(r["quantity"]), number(r["capital_rub"])) for r in conn.execute(
                "SELECT nm_id,quantity,capital_rub FROM sheet_vitrina_v1_warehouse_functional_balances "
                "WHERE version_id=? AND warehouse_key=?", (wb_version_id, key))}
            visible = {int(r["nm_id"]): (number(r["quantity"]), number(r["capital_rub"])) for r in payload.get("balances", [])}
            if (payload.get("status") != "ready" or saved != visible
                    or len(visible) != len(payload.get("balances", []))
                    or number(payload.get("warehouse", {}).get("total_quantity")) != total(v[0] for v in saved.values())
                    or number(payload.get("warehouse", {}).get("total_capital_rub")) != total(v[1] for v in saved.values())):
                continue
            complete_stages[stage] = {"nm_ids": set(saved), "source": {
                "basis":"absent_from_complete_published_document_stage", "version_id":wb_version_id, "etag":model["etag"]}}
        rows = {}
        for nm in nm_ids:
            row = raw.get(nm)
            reason = _projection_row_binding_incident_from_exact_versions(row,
                expected_functional_version_id=wb_version_id, exact_versions=versions) if row else "stage_projection_missing"
            metrics = json.loads(row["metrics_json"]) if row and not reason else {}
            presentations = json.loads(row["presentation_json"]) if row and not reason else {}
            rows[str(nm)] = {"stages": {s: {
                "quantity": metrics.get(own_stage_metric_key(s, "qty")),
                "capital_rub": metrics.get(own_stage_metric_key(s, "capital_rub")),
                "wac_rub": metrics.get(own_stage_metric_key(s, "unit_cost_rub")),
                "presentation": {f: presentations.get(own_stage_metric_key(s, f), {})
                                 for f in ("qty", "capital_rub", "unit_cost_rub")},
            } for s in RETAINED_STAGES}, "reason": reason,
                "revision_id": row["revision_id"] if row else "", "identity": names.get(nm, {})}
            if reason == "stage_projection_missing":
                for stage, proof in complete_stages.items():
                    if nm not in proof["nm_ids"]:
                        rows[str(nm)]["stages"][stage].update(quantity="0", capital_rub="0", wac_rub=None,
                                                              zero_evidence=proof["source"])
        result = {"date": day, "wb_version_id": wb_version_id, "rows": rows}
        result["source_digest"] = fingerprint(result)
        return result
    finally:
        if connection is None:
            conn.rollback()
            conn.close()


class FbsInventorySnapshot:
    """One immutable in-memory version shared by FF, cards and Vitrina rows.

    Only explicit injection selects it. It cannot activate a policy or save a
    ready snapshot. Stage 6 owns publication and the final effective date.
    """
    __slots__ = ("_json",)

    def __init__(self, *, fbs_state: dict, wb_capture: dict, retained: dict, day: str):
        view = candidate_period_view(fbs_state, day)
        if not view["available"]:
            raise ValueError("exact_fbs_presentation_day_missing")
        shared = build_shared_cost_day(fbs_state, wb_capture, day)
        if (retained.get("date") != day or retained.get("wb_version_id") != wb_capture.get("version_id")
                or retained.get("source_digest") != fingerprint({k: v for k, v in retained.items() if k != "source_digest"})):
            raise ValueError("retained_stage_version_or_digest_mismatch")
        fbs_by_nm, fbo_by_nm = {}, {}
        for row in view["rows"].values():
            fbs_by_nm.setdefault(str(row["nm_id"]), []).append(row)
        for row in view["fbo_component"]["rows"].values():
            fbo_by_nm.setdefault(str(row["nm_id"]), []).append(row)
        if set(retained["rows"]) != set(fbs_by_nm):
            raise ValueError("retained_stage_catalog_mismatch")
        wb = {str(r["nm_id"]): r for r in wb_capture["rows"]}
        period = fbs_state["periods"].get(day, fbs_state["baseline"])
        quantity_snapshot = period["snapshot"]
        snapshot_rows = {(str(r["nm_id"]), r["facility_id"]): str(r["quantity"]) for r in quantity_snapshot["rows"]}
        if snapshot_rows != {(str(r["nm_id"]), r["facility_id"]): str(r["quantity"]) for r in view["rows"].values()}:
            raise ValueError("cost_quantity_snapshot_mismatch")
        blocked = bool(view.get("pending_documents")) or view["quality"] == "incomplete"
        sku_rows = {}
        with localcontext() as ctx:
            ctx.prec = 50
            for nm, fbs in fbs_by_nm.items():
                fbo = fbo_by_nm.get(nm, [])
                stage_row = retained["rows"][nm]
                stages = deepcopy(stage_row["stages"])
                # Shared cost and retained WB capital must refer to the same
                # quantity and valuation; neither silently replaces the other.
                expected_wb = wb.get(nm, {})
                if (stage_row.get("reason") == "stage_projection_missing" and wb_capture.get("authority_complete")
                        and expected_wb.get("status") == "available" and number(expected_wb.get("quantity")) == 0
                        and number(expected_wb.get("capital_rub")) == 0):
                    stages["WB"].update(quantity="0", capital_rub="0", wac_rub=None,
                        zero_evidence=deepcopy(expected_wb.get("source", {})))
                if not stage_row.get("reason") and expected_wb.get("status") != "missing":
                    for k in ("quantity", "capital_rub"):
                        observed, expected = number(stages["WB"].get(k)), number(expected_wb.get(k))
                        # The published presentation stores floats. Accept only
                        # its exact float representation, retaining Decimal money.
                        if observed != expected and (observed is None or expected is None or
                                k == "quantity" or observed != Decimal(str(float(expected)))):
                            raise ValueError("retained_wb_shared_cost_mismatch:" + nm)
                        stages["WB"][k] = text(expected)
                stages["FF"] = operands([*fbs, *fbo], blocked=blocked)
                for stage in RETAINED_STAGES:
                    stages[stage] = {**stages[stage], **{k: text(number(stages[stage].get(k)))
                        for k in ("quantity", "capital_rub", "wac_rub")}}
                overall = operands(stages.values())
                physical = expected_wb.get("components", {}).get("physical")
                sku_rows[nm] = {"nm_id": int(nm), "stages": stages, "total": overall,
                    "fbs": operands(fbs, blocked=blocked), "fbo": operands(fbo, blocked=blocked),
                    "locations": [{**r, "pool": "FBS"} for r in fbs] + [{**r, "pool": "FBO"} for r in fbo],
                    "identity": deepcopy(stage_row.get("identity", {})),
                    "shared_cost": deepcopy(shared["rows"][nm]), "wb_physical": text(number(physical)),
                    "stock_total": text(total([number(physical), number(operands(fbs)["quantity"])]))}
            totals = {"stages": {s: operands(r["stages"][s] for r in sku_rows.values()) for s in OWN_PRODUCT_CAPITAL_STAGES},
                "total": operands(r["total"] for r in sku_rows.values()),
                "fbs": operands((r["fbs"] for r in sku_rows.values()), blocked=blocked),
                "fbo": operands((r["fbo"] for r in sku_rows.values()), blocked=blocked),
                "shared_cost": operands(r["shared_cost"] for r in sku_rows.values()),
                "wb_physical": text(total(number(r["wb_physical"]) for r in sku_rows.values())),
                "stock_total": text(total(number(r["stock_total"]) for r in sku_rows.values()))}
        data = {"source": SOURCE, "candidate_only": True, "date": day, "quality": "preliminary",
            "rows": sku_rows, "totals": totals, "quantity_snapshot": deepcopy(quantity_snapshot),
            "cost_source": deepcopy(view["source"]), "cost_status": view["status"], "shared_cost_version": shared["version_id"],
            "fbs_state_fingerprint": view["state_fingerprint"], "retained_stage_digest": retained["source_digest"],
            "wb_source": deepcopy(wb_capture.get("source", {})), "pending_documents": deepcopy(view.get("pending_documents", []))}
        data["version_id"] = fingerprint(data)
        object.__setattr__(self, "_json", json.dumps(data, ensure_ascii=False, sort_keys=True))

    def __setattr__(self, key, value):
        raise AttributeError("immutable_inventory_snapshot")

    @property
    def date(self):
        return self.payload()["date"]

    def payload(self):
        return json.loads(self._json)

    def presentation(self, value, *, data=None):
        data = data or self.payload()
        return {"source": SOURCE, "candidate_only": True, "source_as_of_date": data["date"],
            "source_version_id": data["version_id"], "source_generation_id": data["quantity_snapshot"]["id"],
            "captured_at": data["quantity_snapshot"]["captured_at"], "quality_state": "preliminary",
            "state": "unconfirmed" if value is not None else "unavailable", "tone": "warning",
            "quality_label": "Предварительная оценка", "management_value": text(number(value)) or "",
            "reason": EXPLANATION if value is not None else "Оценка недоступна: нет согласованных данных. " + EXPLANATION}

    def metrics(self, nm=None):
        return self._metrics(self.payload(), nm)

    def _metrics(self, data, nm=None):
        item = data["totals"] if nm is None else data["rows"].get(str(nm))
        if item is None:
            return {}
        result = {"own_total_product_qty": item["total"]["quantity"],
            "own_total_product_capital_rub": item["total"]["capital_rub"],
            "own_avg_product_cost_rub": item["total"]["wac_rub"],
            COST: item["shared_cost"].get("unit_cost_rub", item["shared_cost"].get("wac_rub")),
            FBS_TOTAL: item["fbs"]["quantity"], "stock_total": item["stock_total"]}
        for s, values in item["stages"].items():
            for field, key in (("qty", "quantity"), ("capital_rub", "capital_rub"), ("unit_cost_rub", "wac_rub")):
                result[own_stage_metric_key(s, field)] = values[key]
        for r in (data["rows"].values() if nm is None else [item]):
            for loc in r["locations"]:
                if loc["pool"] == "FBS":
                    key = FBS_FACILITY + loc["facility_id"]
                    result[key] = text(number(result.get(key, "0")) + number(loc["quantity"]))
        if nm is None:
            result = {("avg_" + k if k.startswith("own_capital_") and k.endswith("unit_cost_rub") else "total_" + k): v
                      for k, v in result.items()}
        return result

    def apply_rows(self, rows, *, business_date):
        if business_date != self.date:
            raise ValueError("inventory_presentation_exact_date_required")
        data = self.payload()
        cache = {nm: self._metrics(data, None if nm == "TOTAL" else nm) for nm in ["TOTAL", *data["rows"]]}
        rows = list(rows)
        prototypes = {r.scope_key: r for r in rows if r.scope_kind in {"TOTAL", "SKU"}}
        existing = {r.row_id for r in rows}
        # Ready plans created before the capital catalog may lack these rows.
        # Add current cells using the existing public catalog, without archives.
        for prototype in prototypes.values():
            for metric in build_own_product_capital_metric_items():
                row_id = prototype.scope_key + "|" + metric.metric_key
                if metric.scope != prototype.scope_kind or row_id in existing:
                    continue
                rows.append(replace(prototype, row_id=row_id, metric_key=metric.metric_key,
                    metric_label=metric.label_ru, section="Товарный капитал",
                    row_order=metric.display_order, format=metric.format,
                    values_by_date={d: "" for d in prototype.values_by_date}, presentation_by_date={}))
                existing.add(row_id)
        result = []
        unknown = {k: None for k in self._metrics(data, next(iter(data["rows"])))}
        for row in rows:
            nm = "TOTAL" if row.scope_kind == "TOTAL" else str(row.nm_id)
            metric = cache.get(nm, unknown)
            if business_date not in row.values_by_date or row.metric_key not in metric:
                result.append(row)
                continue
            value = metric[row.metric_key]
            cell = self.presentation(value, data=data)
            result.append(replace(row, values_by_date={**row.values_by_date, business_date: float(value) if value is not None else ""},
                presentation_by_date={**row.presentation_by_date, business_date: cell}))
        return result

    def warehouse_detail(self):
        data = self.payload()
        rows = []
        for nm, item in data["rows"].items():
            ff, identity = item["stages"]["FF"], item["identity"]
            if number(ff["quantity"]) == 0:
                continue
            rows.append({"nm_id": int(nm), "warehouse_key": "ff", "line_id": data["version_id"] + ":" + nm,
                "sku": identity.get("sku") or nm, "nomenclature_name": identity.get("name", ""), "barcode": identity.get("barcode", ""),
                **ff, "average_unit_cost_rub": ff["wac_rub"], "physical_quantity": None, "reserved_quantity": None,
                "available_quantity": item["fbs"]["quantity"], "fbs_quantity": item["fbs"]["quantity"], "fbo_quantity": item["fbo"]["quantity"],
                "provenance_available": True, "provenance": {"version_id": data["version_id"], "quantity_source": data["quantity_snapshot"]["id"],
                    "cost_source": data["cost_source"], "locations": item["locations"]},
                "human_evidence": {"items": [{"document": "Оценка FBS по снимку и документальный FBO", "date": data["date"],
                    "quantity_source": "Официальный снимок FBS; FBO по документам",
                    "cost_source": "Принятая начальная себестоимость" if data["cost_status"] == "baseline" else "Средневзвешенная: начальная стоимость, поступления и расходы дня",
                    "quantity_contribution": ff["quantity"], "capital_contribution_rub": ff["capital_rub"],
                    "confirmation_status": "Предварительная оценка"}]}, "warning": "Предварительная оценка"})
        ff = data["totals"]["stages"]["FF"]
        warehouse = {"warehouse_key": "ff", "warehouse_name": "Склад FF", "sku_count": len(rows),
            "total_quantity": ff["quantity"], "total_capital_rub": ff["capital_rub"], "average_unit_cost_rub": ff["wac_rub"],
            "quantity_label": "FBS + FBO, всего", "updated_at": data["quantity_snapshot"]["captured_at"],
            "source_basis": "Снимок доступного FBS и документальный FBO", "status_label": "Предварительная оценка",
            "status_description": "Проверочный вариант перехода. " + EXPLANATION,
            "snapshot_inventory": {"fbs_quantity": data["totals"]["fbs"]["quantity"], "fbo_quantity": data["totals"]["fbo"]["quantity"]}}
        return {"status": "ready", "candidate_only": True, "read_model": SOURCE, "version_id": data["version_id"],
            "etag": '"' + data["version_id"] + '"', "warehouse": warehouse, "balances": rows, "documents": [],
            "documents_page": {"loaded": False}, "sync_presentation": {"tone": "warning"}}

    def planning_payload(self):
        data = self.payload()
        totals = data["totals"]
        facilities = {}
        for row in data["rows"].values():
            for loc in row["locations"]:
                if loc["pool"] == "FBS":
                    facilities[loc["facility_id"]] = facilities.get(loc["facility_id"], ZERO) + number(loc["quantity"])
        def metric(key, label, value):
            return {"metric_key": key, "label_ru": label, "value": float(value) if value is not None else None,
                    "available": value is not None, "quality": "snapshot_candidate"}
        return {"candidate_only": True, "version_id": data["version_id"], "metrics": [
            metric("wb_total", "Остаток WB: всего", totals["wb_physical"]),
            metric("fbs_total", "Остаток FBS: всего", totals["fbs"]["quantity"]),
            *(metric("fbs_facility:" + f, "Остаток FBS: " + f, q) for f, q in facilities.items()),
            metric("total", "Остаток: всего", totals["stock_total"])],
            "freshness": {"wb_fetched_at": data["wb_source"].get("fetched_at"), "fbs_updated_at": data["quantity_snapshot"]["captured_at"]},
            "formula": {"formula_ru": "WB на складе + доступный FBS; FBO в показатель FBS не добавляется.", "history_rule": EXPLANATION},
            "fbs": {"facilities": [{"facility_id": f, "name": f, "available": text(q),
                "stock_source": {"captured_at": data["quantity_snapshot"]["captured_at"], "generation_id": data["quantity_snapshot"]["id"]}}
                for f, q in facilities.items()]}}


def capture_inventory_snapshot(db_path: Path, *, fbs_state: dict, day: str, wb_version_id: str | None = None):
    view = candidate_period_view(fbs_state, day)
    if not view["available"]:
        raise ValueError("exact_fbs_presentation_day_missing")
    nm_ids = sorted({int(r["nm_id"]) for r in view["rows"].values()})
    wb = capture_wb_component(db_path, day=day, nm_ids=nm_ids, version_id=wb_version_id)
    if not wb["authority_complete"]:
        raise ValueError(wb["reason"])
    retained = capture_retained_stages(db_path, day=day, wb_version_id=wb["version_id"], nm_ids=nm_ids)
    return FbsInventorySnapshot(fbs_state=fbs_state, wb_capture=wb, retained=retained, day=day)
