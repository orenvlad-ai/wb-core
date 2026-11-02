"""Read and verify the one bounded native historical-FBO cohort.

Foundation movements are admitted only with the complete, hash-bound plan and
exact pool/aggregate readback. An operation type or source-system label alone
never bypasses the normal unmapped-FBO guard.
"""
from copy import deepcopy
from decimal import Decimal
import json

from packages.application.fbs_historical_fbo_native import (
    CONTRACT, _verify_existing, operation_id, OPERATIONS_TABLE, LINES_TABLE,
)
from packages.application.fbs_snapshot_cost import fingerprint

SOURCE_CONTRACT = "historical_fbo_native_cost_source_v1"


def capture_historical_fbo_documents(conn):
    # Older minimal readers have only an operation-identity table. They cannot
    # admit this typed source, and the ordinary unmapped-FBO check still rejects
    # every unowned movement. Do not require a migration for unaffected books.
    columns = {r[1] for r in conn.execute(f"PRAGMA table_info({OPERATIONS_TABLE})")}
    if "source_system" not in columns:
        return []
    rows = list(conn.execute(f"SELECT * FROM {OPERATIONS_TABLE} WHERE source_system=? ORDER BY operation_id",
                             ("historical_fbo_dispatch",)))
    if not rows:
        return []
    if len(rows) != 4:
        raise ValueError("historical_fbo_native_source_cohort_incomplete")
    headers = [dict(r) for r in rows]
    metadata = [json.loads(r["metadata_json"]) for r in headers]
    if ({m.get("effect_index") for m in metadata} != set(range(4))
            or any(m.get("contract") != CONTRACT for m in metadata)):
        raise ValueError("historical_fbo_native_source_identity_invalid")
    first = next(m for m in metadata if m["effect_index"] == 0)
    plan = first.get("plan_manifest")
    if (not isinstance(plan, dict) or plan.get("plan_digest") != fingerprint({k: v for k, v in plan.items() if k != "plan_digest"})
            or len(plan.get("effects", [])) != 4 or plan.get("status") != "prepared_unconnected"
            or not _verify_existing(conn, plan)):
        raise ValueError("historical_fbo_native_source_plan_invalid")
    if {r["operation_id"] for r in headers} != {operation_id(plan, i) for i in range(4)}:
        raise ValueError("historical_fbo_native_source_extra_operation")
    by_id = {r["operation_id"]: r for r in headers}
    result = []
    for index, effect in enumerate(plan["effects"]):
        identity = operation_id(plan, index)
        header = by_id[identity]
        movements = [dict(r) for r in conn.execute(f"SELECT * FROM {LINES_TABLE} WHERE operation_id=? ORDER BY line_no", (identity,))]
        if effect["kind"] == "historical_wb_dispatch":
            domain = {"facility_id": effect["pool_movements"][0]["facility_id"], "pool": "FBO",
                      "supply_id": effect["source_key"].rsplit(":", 1)[1], "source_key": effect["source_key"]}
            lines = [{"nm_id": m["nm_id"], "quantity": -m["quantity_delta"], "line_role": "shipped",
                      "facility_id": m["facility_id"], "pool": "FBO", "capital_rub": format(Decimal(m["capital_delta_rub"]).copy_negate(), 'f'),
                      "expense_rub": "0"} for m in movements]
            events = []
        elif effect["kind"] == "pool_reallocation":
            source, destination = effect["pool_movements"]
            domain = {"facility_id": source["facility_id"], "source_pool": "FBS", "destination_pool": "FBO"}
            lines = [{"nm_id": destination["nm_id"], "quantity": destination["quantity_delta"], "line_role": "reallocated",
                      "facility_id": destination["facility_id"], "pool": "FBO", "capital_rub": destination["capital_delta_rub"], "expense_rub": "0"}]
            events = [{"kind": "outgoing", "facility_id": source["facility_id"], "nm_id": source["nm_id"],
                       "quantity": -source["quantity_delta"], "capital_rub": "0",
                       "source": {"document_id": identity, "value_policy": "official_snapshot_quantity_only_no_legacy_capital"}}]
        else:
            raise ValueError("historical_fbo_native_source_kind_invalid")
        result.append({"document_id": identity, "operation_id": identity, "root_document_id": identity,
            "kind": effect["kind"], "document_kind": effect["kind"], "business_date": effect["business_date"],
            "posted_at": header["posted_at"], "fingerprint": fingerprint({"operation": header, "movements": movements}),
            "events": events, "historical_fbo_native": {"contract": SOURCE_CONTRACT, "plan_digest": plan["plan_digest"],
                "effect_index": index, "source_proof": deepcopy(plan["source_proof"])},
            "cost_document": {"contract": SOURCE_CONTRACT, "domain": domain, "lines": lines,
                              "expense_lines": [], "movements": movements, "relations": []}})
    return result
